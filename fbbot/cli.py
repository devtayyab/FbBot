"""Command line: login, scan, review, post, run, loop, status, history, try.

Supports single-account bot mode and multi-agent collaborative swarm mode.
"""
from __future__ import annotations

import argparse
import datetime as dt
import random
import subprocess
import sys
import time
import webbrowser

from .browser import NotLoggedIn, open_browser
from .config import HISTORY_CSV, get_agent_config, get_agents, load_config
from .store import Store

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _browser(agent_cfg: dict, agent_id: str = "default", headless: bool | None = None):
    return open_browser(
        agent_id=agent_id,
        headless=agent_cfg["scan"]["headless"] if headless is None else headless,
        channel=agent_cfg.get("browser_channel", "chrome"),
    )


def _notify(message: str) -> None:
    """Show a macOS notification; ignore errors on other systems."""
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "FB Bot"'],
            check=False, capture_output=True, timeout=5,
        )
    except Exception:
        pass


def _pick_primary_agent(agents: dict[str, dict], store: Store, index: int, forced_agent: str | None = None) -> str:
    """Select which agent acts as primary commenter for a post (round-robin among eligible agents)."""
    agent_ids = list(agents.keys())
    if forced_agent and forced_agent in agents:
        return forced_agent
    eligible = [
        aid for aid in agent_ids
        if store.posted_today_by_agent(aid) < int(agents[aid]["post"]["max_per_day"])
    ]
    pool = eligible or agent_ids
    return pool[index % len(pool)]


def _coordinate_post(
    store: Store,
    post_id: str,
    post_text: str,
    agents: dict[str, dict],
    drafters: dict,
    primary_info: dict,
) -> int:
    """Have other agents evaluate an approved/posted comment and decide: better comment, like+reply, like, or skip."""
    primary_content = primary_info["content"]
    primary_owner = primary_info.get("agent_id", "default")
    primary_action_id = primary_info.get("action_id")
    primary_name = agents.get(primary_owner, {}).get("name", primary_owner)
    created_count = 0

    for sec_id, sec_drafter in drafters.items():
        if sec_id == primary_owner or store.has_agent_acted(post_id, sec_id):
            continue

        sec_cfg = agents[sec_id]
        collab_cfg = sec_cfg.get("collaboration", {})
        existing_replies = store.get_existing_replies(post_id)

        try:
            collab = sec_drafter.analyze_collaboration(
                post_text=post_text,
                primary_comment=primary_content,
                primary_agent_name=primary_name,
                existing_replies=existing_replies,
            )
        except Exception as exc:
            if isinstance(exc, sec_drafter.setup_errors):
                raise SystemExit(f"AI model setup problem for {sec_id} (check API key/model): {exc}")
            print(f"  collaboration check failed for {sec_id}: {exc!r}")
            continue

        if not collab or collab.action == "skip":
            store.add_action(
                post_id=post_id,
                agent_id=sec_id,
                action_type="skip",
                target_action_id=primary_action_id,
                status="skipped",
                analysis=collab.model_dump() if collab else None,
            )
            continue

        action = collab.action
        # Respect agent collaboration feature flags cleanly
        if action == "comment" and not collab_cfg.get("allow_better_comment", True):
            action = "like_and_reply" if collab.reply else "like"
        if action == "like_and_reply":
            allow_like = collab_cfg.get("allow_like", True)
            allow_reply = collab_cfg.get("allow_reply", True)
            if not collab_cfg.get("allow_like_and_reply", True) or (not allow_like and allow_reply):
                action = "reply" if allow_reply else ("like" if allow_like else "skip")
            elif allow_like and not allow_reply:
                action = "like"
            elif not allow_like and not allow_reply:
                action = "skip"
        elif action == "reply" and not collab_cfg.get("allow_reply", True):
            action = "like" if collab_cfg.get("allow_like", True) else "skip"
        elif action == "like" and not collab_cfg.get("allow_like", True):
            action = "skip"

        if action == "skip":
            store.add_action(
                post_id=post_id,
                agent_id=sec_id,
                action_type="skip",
                target_action_id=primary_action_id,
                status="skipped",
                analysis=collab.model_dump(),
            )
            continue

        delay_mins = int(collab_cfg.get("min_delay_after_primary_mins", 15))
        base_time = dt.datetime.now()
        if primary_info.get("executed_at"):
            try:
                base_time = dt.datetime.fromisoformat(primary_info["executed_at"])
            except ValueError:
                pass
        sched_time = (base_time + dt.timedelta(minutes=delay_mins)).isoformat(timespec="seconds")

        sec_auto = sec_cfg["post"]["auto_post"]
        sec_min_rank = CONFIDENCE_RANK[sec_cfg["post"]["auto_post_min_confidence"]]
        sec_auto_approved = sec_auto and CONFIDENCE_RANK[collab.confidence] >= sec_min_rank
        sec_status = "approved" if sec_auto_approved else "pending"
        sec_by = "auto" if sec_auto_approved else None

        content = collab.reply if action in ("comment", "reply", "like_and_reply") else ""
        store.add_action(
            post_id=post_id,
            agent_id=sec_id,
            action_type=action,
            content=content,
            target_action_id=primary_action_id,
            target_agent_id=primary_owner,
            target_comment_text="" if action == "comment" else primary_content,
            confidence=collab.confidence,
            analysis=collab.model_dump(),
            status=sec_status,
            approved_by=sec_by,
            delay_minutes=delay_mins,
            scheduled_after=sched_time,
        )
        created_count += 1
        labels = {
            "comment": f"BETTER COMMENT: '{content}'",
            "like_and_reply": f"LIKE + REPLY: '{content}'",
            "reply": f"REPLY: '{content}'",
            "like": "LIKE",
        }
        print(f"  -> [{sec_id}] Planned {labels.get(action, action)} on [{primary_name}] (delay {delay_mins}m)")

    return created_count


# ---- login ----------------------------------------------------------------

def cmd_login(cfg: dict, args) -> None:
    agents = get_agents(cfg)
    forced = getattr(args, "agent", None)

    if forced:
        if forced not in agents:
            print(f"Note: '{forced}' is not in config.yaml agents ({', '.join(agents.keys())}), logging in anyway.")
        targets = [forced]
    elif len(agents) > 1:
        print("Configured agents:", ", ".join(agents.keys()))
        choice = input("Which agent to log in? (enter agent id or 'all') [all]: ").strip() or "all"
        if choice.lower() == "all":
            targets = list(agents.keys())
        elif choice in agents:
            targets = [choice]
        else:
            raise SystemExit(f"Unknown agent '{choice}'. Choose from: {', '.join(agents.keys())}")
    else:
        targets = list(agents.keys())

    for agent_id in targets:
        agent_cfg = agents.get(agent_id) or get_agent_config(cfg, agent_id)
        print(f"Logging in Facebook for agent '{agent_id}' ({agent_cfg.get('name', agent_id)})...")
        with _browser(agent_cfg, agent_id=agent_id, headless=False) as ctx:
            page = ctx.new_page()
            page.goto("https://www.facebook.com/")
            input(f"Log in to Facebook for agent '{agent_id}' in the browser window (incl. 2FA), then press Enter... ")
        print(f"Saved. Profile for agent '{agent_id}' is ready.")


# ---- scan -----------------------------------------------------------------

def cmd_scan(cfg: dict, args) -> None:
    from .drafter import Drafter  # imported here so `login` works without an API key
    from .scraper import scrape_group

    agents = get_agents(cfg)
    agent_ids = list(agents.keys())
    forced_agent = getattr(args, "agent", None)
    if forced_agent and forced_agent not in agents:
        raise SystemExit(f"Unknown agent '{forced_agent}'. Configured agents: {', '.join(agent_ids)}")

    scout_id = forced_agent or agent_ids[0]
    scout_cfg = agents[scout_id]
    drafters = {aid: Drafter(acfg) for aid, acfg in agents.items()}

    max_age = dt.timedelta(hours=cfg["scan"]["max_post_age_hours"])
    new_count = drafted = auto_approved = draft_errors = 0
    coordinated_actions = 0

    with Store() as store, _browser(scout_cfg, agent_id=scout_id) as ctx:
        baseline = store.is_empty()
        rotation_idx = store.total_posts()

        for group_url in cfg["groups"]:
            print(f"Scanning {group_url} (Scout: {scout_id})...")
            posts = scrape_group(ctx, group_url, cfg["scan"]["scrolls"], debug=args.debug)
            print(f"  {len(posts)} posts loaded")

            for post in posts:
                post_id = post["post_id"]
                if post.get("comments") and store.has_post(post_id):
                    store.save_existing_comments(post_id, post["comments"])

                # Process brand-new post
                if not store.has_post(post_id):
                    new_count += 1
                    created = post.get("created_at")
                    if created and dt.datetime.now() - dt.datetime.fromtimestamp(created) > max_age:
                        store.add_post(post, group_url, "ignored", "older than max_post_age_hours", baseline=baseline)
                        continue
                    if not post.get("text"):
                        store.add_post(post, group_url, "ignored", "no text (photo or link only)", baseline=baseline)
                        continue

                    first_time = store.add_post(post, group_url, "new", baseline=baseline)
                    primary_id = _pick_primary_agent(agents, store, rotation_idx, forced_agent=forced_agent)
                    rotation_idx += 1
                    primary_cfg = agents[primary_id]
                    primary_drafter = drafters[primary_id]

                    ext_comments = post.get("comments") or []
                    if ext_comments:
                        # Existing comment(s) from Facebook members already present!
                        # Evaluate whether primary agent can write a better comment or like+reply.
                        first_ext = ext_comments[0]
                        try:
                            collab = primary_drafter.analyze_collaboration(
                                post_text=post["text"],
                                primary_comment=first_ext["text"],
                                primary_agent_name=first_ext.get("author") or "a group member",
                                existing_replies=[c["text"] for c in ext_comments[1:]],
                            )
                        except Exception as exc:
                            store.discard_post(post_id)
                            if isinstance(exc, primary_drafter.setup_errors):
                                raise SystemExit(f"AI model setup problem (check API key/model): {exc}")
                            draft_errors += 1
                            print(f"  drafting failed, will retry next scan: {exc!r}")
                            continue

                        if not collab or collab.action == "skip":
                            store.save_analysis(
                                post_id,
                                collab.model_dump() if collab else {"skip_reason": "existing comments sufficient"},
                                "ignored",
                            )
                            continue

                        drafted += 1
                        auto = primary_cfg["post"]["auto_post"]
                        min_rank = CONFIDENCE_RANK[primary_cfg["post"]["auto_post_min_confidence"]]
                        is_auto = auto and CONFIDENCE_RANK[collab.confidence] >= min_rank
                        status = "approved" if is_auto else "pending"
                        by = "auto" if is_auto else None
                        content = collab.reply if collab.action in ("comment", "reply", "like_and_reply") else ""

                        store.save_analysis(post_id, collab.model_dump(), status, primary_agent_id=primary_id)
                        if collab.action == "comment" and is_auto:
                            store.approve(post_id, content, by="auto", primary_agent_id=primary_id)
                            auto_approved += 1
                        elif is_auto:
                            auto_approved += 1

                        store.add_action(
                            post_id=post_id,
                            agent_id=primary_id,
                            action_type=collab.action,
                            content=content,
                            target_agent_id=first_ext.get("author") or "group_member",
                            target_comment_text="" if collab.action == "comment" else first_ext["text"],
                            confidence=collab.confidence,
                            analysis=collab.model_dump(),
                            status=status,
                            approved_by=by,
                        )
                        tag = " [first-time poster]" if first_time else ""
                        print(f"  + [{primary_id}] {collab.action.upper()} (existing FB comment){tag}: {content or '(like)'}")
                    else:
                        # No existing comments yet -> draft primary standalone comment
                        try:
                            analysis = primary_drafter.analyze(post["text"], first_time_poster=first_time)
                        except Exception as exc:
                            store.discard_post(post_id)
                            if isinstance(exc, primary_drafter.setup_errors):
                                raise SystemExit(f"AI model setup problem (check API key/model): {exc}")
                            draft_errors += 1
                            print(f"  drafting failed, will retry next scan: {exc!r}")
                            continue

                        if analysis is None:
                            store.set_status(post_id, "ignored", "model declined to draft")
                            continue
                        if not analysis.relevant:
                            store.save_analysis(post_id, analysis.model_dump(), "ignored")
                            continue

                        drafted += 1
                        auto = primary_cfg["post"]["auto_post"]
                        min_rank = CONFIDENCE_RANK[primary_cfg["post"]["auto_post_min_confidence"]]
                        is_auto = auto and CONFIDENCE_RANK[analysis.confidence] >= min_rank
                        status = "approved" if is_auto else "pending"
                        by = "auto" if is_auto else None

                        store.save_analysis(post_id, analysis.model_dump(), status, primary_agent_id=primary_id)
                        if is_auto:
                            store.approve(post_id, analysis.reply, by="auto", primary_agent_id=primary_id)
                            auto_approved += 1

                        store.add_action(
                            post_id=post_id,
                            agent_id=primary_id,
                            action_type="comment",
                            content=analysis.reply,
                            confidence=analysis.confidence,
                            analysis=analysis.model_dump(),
                            status=status,
                            approved_by=by,
                        )
                        tag = " [first-time poster]" if first_time else ""
                        print(f"  + [{primary_id}] {post.get('author_name') or '?'}{tag}: {analysis.reply}")

                # Coordinate secondary agents ONLY once primary comment is approved/posted (never while unreviewed 'pending')
                if len(agents) > 1 and post.get("text"):
                    primary_info = store.get_primary_comment(post_id, include_pending=False)
                    if primary_info:
                        coordinated_actions += _coordinate_post(
                            store, post_id, post["text"], agents, drafters, primary_info
                        )

        pending = len(store.pending_actions())
    print(
        f"New posts: {new_count}, replies drafted: {drafted}, auto-approved: {auto_approved}, "
        f"coordinated actions: {coordinated_actions}, drafting errors: {draft_errors}, waiting for review: {pending}"
    )
    if pending:
        _notify(f"{pending} draft replies waiting: python bot.py review")


# ---- review ---------------------------------------------------------------

def cmd_review(cfg: dict, args) -> None:
    agent_filter = getattr(args, "agent", None)
    with Store() as store:
        _review(store, agent_id=agent_filter)


def _review(store: Store, agent_id: str | None = None) -> None:
    pending = store.pending_actions(agent_id=agent_id)
    if not pending:
        legacy_pending = store.by_status("pending") if not agent_id else []
        # Only review legacy rows that don't have agent_actions
        legacy_only = [
            r for r in legacy_pending
            if not store.get_post_actions(r["post_id"])
        ]
        if not legacy_only:
            print("Nothing to review.")
            return
        _review_legacy(store, legacy_only)
        return

    print(f"{len(pending)} actions to review. Keys: [a]pprove  [e]dit  [s]kip  [o]pen post  [q]uit\n")
    for row in pending:
        print("=" * 70)
        action_type = row["action_type"].upper()
        aid = row["agent_id"]
        post_url = row["url"]
        target_comment = row["live_target_comment"] or row["target_comment_text"] or ""

        if action_type == "COMMENT":
            lbl = "BETTER COMMENT" if row["target_action_id"] or row["target_agent_id"] else "PRIMARY COMMENT"
            print(f"[{aid}] {lbl} on post by {row['post_author'] or '?'}\n{post_url}\n")
            print((row["post_text"] or "")[:800])
            print(f"\nDraft: {row['content']}")
        elif action_type == "LIKE":
            print(f"[{aid}] LIKE on [{row['target_agent_id']}]'s comment\n{post_url}\n")
            print(f"Target Comment: {target_comment}")
        elif action_type in ("REPLY", "LIKE_AND_REPLY"):
            lbl = "LIKE + THREADED REPLY" if action_type == "LIKE_AND_REPLY" else "THREADED REPLY"
            print(f"[{aid}] {lbl} to [{row['target_agent_id']}]'s comment\n{post_url}\n")
            print(f"Target Comment: {target_comment}")
            print(f"\nReply Draft: {row['content']}")

        while True:
            choice = input("> ").strip().lower()
            if choice == "a":
                store.approve_action(row["action_id"], by="review")
                break
            if choice == "e":
                if action_type == "LIKE":
                    print("Like action cannot be edited. Approve [a] or Skip [s].")
                    continue
                new = input("Your edit: ").strip()
                if new:
                    store.approve_action(row["action_id"], content=new, by="edited")
                    break
            elif choice == "s":
                store.set_action_status(row["action_id"], "skipped")
                break
            elif choice == "o":
                webbrowser.open(post_url)
            elif choice == "q":
                return
    print("Done. Approved actions execute with: python bot.py post")


def _review_legacy(store: Store, pending: list) -> None:
    for row in pending:
        tag = "  (first-time poster)" if row["first_time_poster"] else ""
        print("=" * 70)
        print(f"{row['author_name'] or '?'}{tag}\n{row['url']}\n")
        print((row["text"] or "")[:1200])
        print(f"\nDraft: {row['draft']}")
        while True:
            choice = input("> ").strip().lower()
            if choice == "a":
                store.approve(row["post_id"], row["draft"], by="review")
                break
            if choice == "e":
                new = input("Your comment: ").strip()
                if new:
                    store.approve(row["post_id"], new, by="edited")
                    break
            elif choice == "s":
                store.set_status(row["post_id"], "skipped")
                break
            elif choice == "o":
                webbrowser.open(row["url"])
            elif choice == "q":
                return


# ---- post -----------------------------------------------------------------

def cmd_post(cfg: dict, args) -> None:
    with Store() as store:
        _post_multi(cfg, args, store)


def _post_multi(cfg: dict, args, store: Store) -> None:
    from .poster import (
        PostError,
        PostUnverified,
        like_and_reply_comment,
        like_comment,
        post_comment,
        reply_to_comment,
    )

    agents = get_agents(cfg)
    target_agent = getattr(args, "agent", None)
    if target_agent and target_agent not in agents:
        raise SystemExit(f"Unknown agent '{target_agent}'. Configured agents: {', '.join(agents.keys())}")
    agents_to_run = [target_agent] if target_agent else list(agents.keys())

    total_executed = 0
    newly_posted_primary_posts: list[str] = []

    for agent_id in agents_to_run:
        agent_cfg = agents[agent_id]
        pc = agent_cfg["post"]
        due = store.due_actions(agent_id=agent_id)
        if not due:
            continue

        room_today = pc["max_per_day"] - store.posted_today_by_agent(agent_id)
        batch = due[: max(0, min(pc["max_per_run"], room_today))]
        if not batch:
            print(f"[{agent_id}] Daily limit ({pc['max_per_day']}) reached; {len(due)} waiting.")
            continue

        print(f"\n--- Posting batch for [{agent_id}] ({len(batch)} actions) ---")
        with _browser(agent_cfg, agent_id=agent_id) as ctx:
            for i, act in enumerate(batch):
                if i:
                    wait = random.uniform(pc["min_delay_seconds"], pc["max_delay_seconds"])
                    print(f"  [{agent_id}] waiting {wait:.0f}s")
                    time.sleep(wait)

                action_type = act["action_type"]
                post_url = act["url"]
                target_comment = act["live_target_comment"] or act["target_comment_text"] or ""
                print(f"[{agent_id}] Executing {action_type.upper()} on {post_url}")

                try:
                    if action_type == "comment":
                        print(f"  Top-level comment: {act['content']}")
                        post_comment(ctx, post_url, act["content"], pc["comment_box_labels"], dry_run=args.dry_run)
                    elif action_type == "like":
                        print(f"  Liking comment: {target_comment[:45]}...")
                        like_comment(ctx, post_url, target_comment, dry_run=args.dry_run)
                    elif action_type == "reply":
                        print(f"  Replying to: {target_comment[:45]}... -> {act['content']}")
                        reply_to_comment(
                            ctx, post_url, target_comment, act["content"],
                            pc["comment_box_labels"], dry_run=args.dry_run
                        )
                    elif action_type == "like_and_reply":
                        print(f"  Liking & Replying to: {target_comment[:45]}... -> {act['content']}")
                        like_and_reply_comment(
                            ctx, post_url, target_comment, act["content"],
                            pc["comment_box_labels"], dry_run=args.dry_run
                        )

                    if not args.dry_run:
                        store.mark_action_executed(act["action_id"])
                        total_executed += 1
                        if action_type == "comment":
                            newly_posted_primary_posts.append(act["post_id"])
                        print("  Success")
                except PostUnverified as exc:
                    if not args.dry_run:
                        store.mark_action_executed(act["action_id"], unverified_error=str(exc))
                        if action_type == "comment":
                            newly_posted_primary_posts.append(act["post_id"])
                    print(f"  Unverified: {exc}")
                except PostError as exc:
                    if not args.dry_run:
                        store.set_action_status(act["action_id"], "failed", error=str(exc))
                    print(f"  Failed: {exc}")

    # Legacy queue (only posts without agent_actions rows)
    legacy_approved = store.legacy_approved()
    if legacy_approved and not target_agent:
        print("Checking legacy queue...")
        _post_legacy(cfg, args, store, legacy_approved)

    # After primary comments go live, trigger secondary agents' offline collaboration evaluation
    if not args.dry_run and newly_posted_primary_posts and len(agents) > 1:
        _coordinate_after_post(agents, store, newly_posted_primary_posts)

    if not args.dry_run:
        store.export_history(HISTORY_CSV)


def _coordinate_after_post(agents: dict[str, dict], store: Store, post_ids: list[str]) -> None:
    """Evaluate collaboration for secondary agents right after a primary comment is posted."""
    from .drafter import Drafter

    drafters = {aid: Drafter(acfg) for aid, acfg in agents.items()}
    total_planned = 0
    for post_id in set(post_ids):
        post = store.get_post(post_id)
        if not post or not post["text"]:
            continue
        primary_info = store.get_primary_comment(post_id, include_pending=False)
        if not primary_info:
            continue
        total_planned += _coordinate_post(store, post_id, post["text"], agents, drafters, primary_info)
    if total_planned:
        pending = len(store.pending_actions())
        print(f"Coordinated {total_planned} follow-up agent action(s). Pending review: {pending}")


def _post_legacy(cfg: dict, args, store: Store, approved: list) -> None:
    from .poster import PostError, PostUnverified, post_comment

    pc = cfg["post"]
    room_today = pc["max_per_day"] - store.posted_today()
    batch = approved[: max(0, min(pc["max_per_run"], room_today))]
    if not batch:
        return

    with _browser(cfg, agent_id="default") as ctx:
        for i, row in enumerate(batch):
            if i:
                wait = random.uniform(pc["min_delay_seconds"], pc["max_delay_seconds"])
                print(f"  waiting {wait:.0f}s")
                time.sleep(wait)
            print(f"Commenting on {row['url']}\n  {row['final_comment']}")
            try:
                post_comment(ctx, row["url"], row["final_comment"], pc["comment_box_labels"], dry_run=args.dry_run)
            except PostUnverified as exc:
                store.mark_unverified(row["post_id"], str(exc))
                print(f"  unverified: {exc}")
                continue
            except PostError as exc:
                if not args.dry_run:
                    store.set_status(row["post_id"], "failed", str(exc))
                print(f"  failed: {exc}")
                continue
            if not args.dry_run:
                store.mark_posted(row["post_id"])
                print("  posted")


# ---- run / loop / status / try ---------------------------------------------

def cmd_run(cfg: dict, args) -> None:
    """One hourly cycle: scan for new posts and coordinate actions, then execute due actions."""
    print(f"=== {dt.datetime.now():%Y-%m-%d %H:%M} ===")
    cmd_scan(cfg, args)
    cmd_post(cfg, args)


def cmd_loop(cfg: dict, args) -> None:
    """Keep running `run` every N minutes until you press Ctrl+C."""
    while True:
        try:
            cmd_run(cfg, args)
        except NotLoggedIn:
            raise
        except Exception as exc:
            print(f"Cycle failed: {exc!r}")
        print(f"Next run in {args.minutes} minutes.\n")
        time.sleep(args.minutes * 60)


def cmd_status(cfg: dict, args) -> None:
    with Store() as store:
        agents = get_agents(cfg)
        print("=== Facebook Bot Swarm Status ===")
        print(f"Configured Agents: {len(agents)} ({', '.join(agents.keys())})")
        print("Posts in DB:", store.counts() or "none yet")
        print("\nAgent Activity Today:")
        for aid, acfg in agents.items():
            posted = store.posted_today_by_agent(aid)
            due = len(store.due_actions(agent_id=aid))
            waiting_delay = store.waiting_delay_count(aid)
            pending = len(store.pending_actions(agent_id=aid))
            max_d = acfg["post"]["max_per_day"]
            print(
                f"  [{aid}] '{acfg.get('name', aid)}': {posted}/{max_d} posted today | "
                f"{due} ready now | {waiting_delay} waiting delay | {pending} pending review"
            )

        print("\nRecent Issues:")
        issues = store.recent_action_issues(limit=5)
        for row in issues:
            print(f"  [{row['agent_id']}] {row['action_type']} {row['status']}: {row['url']} -> {row['error']}")
        if not issues:
            for status in ("failed", "unverified"):
                for row in store.by_status(status)[-3:]:
                    print(f"  {status}: {row['url']} -> {row['error']}")


def cmd_history(cfg: dict, args) -> None:
    """Show the posts and engagements the swarm executed, newest first, and refresh the CSV."""
    with Store() as store:
        rows = store.history(limit=args.limit)
        total = store.export_history(HISTORY_CSV)
    if not rows:
        print("No comments or engagements posted yet.")
        return
    for r in rows:
        mark = "" if r["status"] == "posted" else f"  [{r['status']}]"
        act = r["action_type"].upper()
        print(f"{r['commented_at']}  [{r['agent_id']}] {act}  on {r['author_name'] or '?'}  ({r['approved_by'] or '?'}){mark}")
        print(f"  post:    {(r['post_text'] or '').replace(chr(10), ' ')[:110]}")
        if r["final_comment"]:
            print(f"  comment: {r['final_comment']}")
        print(f"  {r['url']}\n")
    print(f"Showing {len(rows)} of {total}. Full history: {HISTORY_CSV}")


def cmd_try(cfg: dict, args) -> None:
    """Draft a reply for any text, or test collaboration with an existing comment."""
    from .drafter import Drafter

    agents = get_agents(cfg)
    agent_id = getattr(args, "agent", None) or list(agents.keys())[0]
    if agent_id not in agents:
        raise SystemExit(f"Unknown agent '{agent_id}'. Configured agents: {', '.join(agents.keys())}")
    drafter = Drafter(agents[agent_id])

    if args.existing_comment:
        print(f"Testing collaborative reaction for agent '{agent_id}'...")
        collab = drafter.analyze_collaboration(
            post_text=args.text,
            primary_comment=args.existing_comment,
            primary_agent_name=args.primary_name or "Ali",
        )
        print(collab.model_dump_json(indent=2) if collab else "Declined.")
    else:
        print(f"Testing primary drafting for agent '{agent_id}'...")
        analysis = drafter.analyze(args.text)
        print(analysis.model_dump_json(indent=2) if analysis else "Declined.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bot.py", description="Facebook travel group multi-agent helper")
    sub = parser.add_subparsers(dest="command", required=True)

    p_login = sub.add_parser("login", help="log in to Facebook once in the bot's browser (or for a specific agent)")
    p_login.add_argument("--agent", help="agent id to log in (e.g. agent_1, agent_2)")

    p_scan = sub.add_parser("scan", help="find new posts and coordinate drafts among agents")
    p_scan.add_argument("--agent", help="force a specific agent to scout and take primary comments")
    p_scan.add_argument("--debug", action="store_true", help="save raw Facebook data to data/debug/")

    p_review = sub.add_parser("review", help="approve, edit or skip drafted comments, likes and replies")
    p_review.add_argument("--agent", help="filter pending items for a specific agent")

    p_post = sub.add_parser("post", help="post approved comments, likes and replies")
    p_post.add_argument("--agent", help="only execute actions for this specific agent")
    p_post.add_argument("--dry-run", action="store_true", help="open posts and find buttons, do not click or type")

    p_run = sub.add_parser("run", help="scan, coordinate, then execute actions")
    p_run.add_argument("--agent", help="force a specific agent for primary scouting/posting")
    p_run.add_argument("--debug", action="store_true")
    p_run.add_argument("--dry-run", action="store_true")

    p_loop = sub.add_parser("loop", help="run every N minutes in this terminal")
    p_loop.add_argument("--minutes", type=int, default=60)
    p_loop.add_argument("--debug", action="store_true")
    p_loop.add_argument("--dry-run", action="store_true")

    sub.add_parser("status", help="show counts, agent activities, and recent failures")

    p_hist = sub.add_parser("history", help="list posts commented on and update CSV")
    p_hist.add_argument("--limit", type=int, default=20)

    p_try = sub.add_parser("try", help="draft a reply or test collaborative reaction without Facebook")
    p_try.add_argument("text")
    p_try.add_argument("--agent", help="which agent should test")
    p_try.add_argument("--existing-comment", help="existing comment text to test collaboration (like/reply/comment)")
    p_try.add_argument("--primary-name", help="name of commenter who posted existing comment")

    args = parser.parse_args(argv)
    needs_groups = args.command in ("scan", "run", "loop")
    needs_destination = needs_groups or args.command == "try"
    cfg = load_config(require_groups=needs_groups, require_destination=needs_destination)
    handler = globals()[f"cmd_{args.command}"]
    try:
        handler(cfg, args)
    except NotLoggedIn as exc:
        print(f"Stopped: {exc}")
        _notify("Facebook login needed: python bot.py login")
        sys.exit(2)

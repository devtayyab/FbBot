"""Command line: login, scan, review, post, run, loop, status, history, try."""
from __future__ import annotations

import argparse
import datetime as dt
import random
import subprocess
import sys
import time
import webbrowser

from .browser import NotLoggedIn, open_browser
from .config import HISTORY_CSV, load_config
from .store import Store

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _browser(cfg: dict, headless: bool | None = None):
    return open_browser(
        headless=cfg["scan"]["headless"] if headless is None else headless,
        channel=cfg.get("browser_channel", "chrome"),
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


# ---- login ----------------------------------------------------------------

def cmd_login(cfg: dict, args) -> None:
    with _browser(cfg, headless=False) as ctx:
        page = ctx.new_page()
        page.goto("https://www.facebook.com/")
        input("Log in to Facebook in the browser window (incl. 2FA), then press Enter here... ")
    print("Saved. The bot will reuse this login.")


# ---- scan -----------------------------------------------------------------

def cmd_scan(cfg: dict, args) -> None:
    from .drafter import Drafter  # imported here so `login` works without an API key
    from .scraper import scrape_group

    drafter = Drafter(cfg)
    max_age = dt.timedelta(hours=cfg["scan"]["max_post_age_hours"])
    auto = cfg["post"]["auto_post"]
    min_rank = CONFIDENCE_RANK[cfg["post"]["auto_post_min_confidence"]]
    new_count = drafted = auto_approved = draft_errors = 0

    with Store() as store, _browser(cfg) as ctx:
        baseline = store.is_empty()
        for group_url in cfg["groups"]:
            print(f"Scanning {group_url}")
            posts = scrape_group(ctx, group_url, cfg["scan"]["scrolls"], debug=args.debug)
            print(f"  {len(posts)} posts loaded")
            for post in posts:
                if store.has_post(post["post_id"]):
                    continue
                new_count += 1
                created = post.get("created_at")
                if created and dt.datetime.now() - dt.datetime.fromtimestamp(created) > max_age:
                    store.add_post(post, group_url, "ignored", "older than max_post_age_hours", baseline=baseline)
                    continue
                if not post.get("text"):
                    store.add_post(post, group_url, "ignored", "no text (photo or link only)", baseline=baseline)
                    continue

                first_time = store.add_post(post, group_url, "new", baseline=baseline)
                try:
                    analysis = drafter.analyze(post["text"], first_time_poster=first_time)
                except Exception as exc:  # API or parse error: retry this post next scan
                    store.discard_post(post["post_id"])
                    if isinstance(exc, drafter.setup_errors):
                        # Every post would fail the same way: stop instead of trying each one.
                        raise SystemExit(f"AI model setup problem (check the API key in .env and llm settings): {exc}")
                    draft_errors += 1
                    print(f"  drafting failed, will retry next scan: {exc!r}")
                    continue
                if analysis is None:
                    store.set_status(post["post_id"], "ignored", "model declined to draft")
                    continue
                if not analysis.relevant:
                    store.save_analysis(post["post_id"], analysis.model_dump(), "ignored")
                    continue

                drafted += 1
                store.save_analysis(post["post_id"], analysis.model_dump(), "pending")
                if auto and CONFIDENCE_RANK[analysis.confidence] >= min_rank:
                    store.approve(post["post_id"], analysis.reply, by="auto")
                    auto_approved += 1
                tag = " [first-time poster]" if first_time else ""
                print(f"  + {post.get('author_name') or '?'}{tag}: {analysis.reply}")

        pending = len(store.by_status("pending"))
    print(
        f"New posts: {new_count}, replies drafted: {drafted}, auto-approved: {auto_approved}, "
        f"drafting errors: {draft_errors}, waiting for review: {pending}"
    )
    if pending:
        _notify(f"{pending} draft replies waiting: python bot.py review")


# ---- review ---------------------------------------------------------------

def cmd_review(cfg: dict, args) -> None:
    with Store() as store:
        _review(store)


def _review(store: Store) -> None:
    pending = store.by_status("pending")
    if not pending:
        print("Nothing to review.")
        return
    print(f"{len(pending)} drafts to review. Keys: [a]pprove  [e]dit  [s]kip  [o]pen post  [q]uit\n")
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
    print("Done. Approved comments go out with: python bot.py post")


# ---- post -----------------------------------------------------------------

def cmd_post(cfg: dict, args) -> None:
    with Store() as store:
        _post(cfg, args, store)


def _post(cfg: dict, args, store: Store) -> None:
    from .poster import PostError, PostUnverified, post_comment

    pc = cfg["post"]
    approved = store.by_status("approved")
    room_today = pc["max_per_day"] - store.posted_today()
    batch = approved[: max(0, min(pc["max_per_run"], room_today))]
    if not approved:
        print("No approved comments to post.")
        return
    if not batch:
        print(f"Daily limit of {pc['max_per_day']} comments reached; {len(approved)} wait for tomorrow.")
        return

    with _browser(cfg) as ctx:
        for i, row in enumerate(batch):
            if i:
                wait = random.uniform(pc["min_delay_seconds"], pc["max_delay_seconds"])
                print(f"  waiting {wait:.0f}s")
                time.sleep(wait)
            print(f"Commenting on {row['url']}\n  {row['final_comment']}")
            try:
                post_comment(ctx, row["url"], row["final_comment"], pc["comment_box_labels"], dry_run=args.dry_run)
            except PostUnverified as exc:
                store.mark_unverified(row["post_id"], str(exc))  # in history, counts toward max_per_day
                print(f"  unverified: {exc}")
                continue
            except PostError as exc:
                if not args.dry_run:  # a dry run must not change the queue
                    store.set_status(row["post_id"], "failed", str(exc))
                print(f"  failed: {exc}")
                continue
            if not args.dry_run:
                store.mark_posted(row["post_id"])
                print("  posted")
    if not args.dry_run:
        store.export_history(HISTORY_CSV)
        print(f"History updated: {HISTORY_CSV}")


# ---- run / loop / status / try ---------------------------------------------

def cmd_run(cfg: dict, args) -> None:
    """One hourly cycle: scan for new posts, then post approved comments."""
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
        except Exception as exc:  # keep the loop alive on one bad cycle
            print(f"Cycle failed: {exc!r}")
        print(f"Next run in {args.minutes} minutes.\n")
        time.sleep(args.minutes * 60)


def cmd_status(cfg: dict, args) -> None:
    with Store() as store:
        counts = store.counts()
        print("Posts by status:", counts or "none yet")
        print("Posted today:", store.posted_today())
        for status in ("failed", "unverified"):
            for row in store.by_status(status)[-5:]:
                print(f"  {status}: {row['url']} -> {row['error']}")


def cmd_history(cfg: dict, args) -> None:
    """Show the posts the bot commented on, newest first, and refresh the CSV."""
    with Store() as store:
        rows = store.history(limit=args.limit)
        total = store.export_history(HISTORY_CSV)
    if not rows:
        print("No comments posted yet.")
        return
    for r in rows:
        mark = "" if r["status"] == "posted" else f"  [{r['status']}]"
        print(f"{r['posted_at']}  {r['author_name'] or '?'}  ({r['approved_by'] or '?'}){mark}")
        print(f"  post:    {(r['text'] or '').replace(chr(10), ' ')[:110]}")
        print(f"  comment: {r['final_comment']}")
        print(f"  {r['url']}\n")
    print(f"Showing {len(rows)} of {total}. Full history: {HISTORY_CSV}")


def cmd_try(cfg: dict, args) -> None:
    """Draft a reply for any text, to tune knowledge.md and the tone."""
    from .drafter import Drafter

    analysis = Drafter(cfg).analyze(args.text)
    print(analysis.model_dump_json(indent=2) if analysis else "The model declined to draft a reply.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bot.py", description="Facebook travel group helper")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("login", help="log in to Facebook once in the bot's browser")
    p = sub.add_parser("scan", help="find new posts and draft replies")
    p.add_argument("--debug", action="store_true", help="save raw Facebook data to data/debug/")
    sub.add_parser("review", help="approve, edit or skip drafted replies")
    p = sub.add_parser("post", help="post approved comments")
    p.add_argument("--dry-run", action="store_true", help="open posts and find the comment box, type nothing")
    p = sub.add_parser("run", help="scan, then post (what the hourly schedule runs)")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("loop", help="run every N minutes in this terminal")
    p.add_argument("--minutes", type=int, default=60)
    p.add_argument("--debug", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("status", help="show counts and recent failures")
    p = sub.add_parser("history", help="list posts the bot commented on and write data/comment_history.csv")
    p.add_argument("--limit", type=int, default=20, help="how many to show (the CSV always has all)")
    p = sub.add_parser("try", help="draft a reply for some text without touching Facebook")
    p.add_argument("text")

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

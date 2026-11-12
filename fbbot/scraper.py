"""Read new posts from a Facebook group feed.

Facebook's HTML uses random class names that change often, so instead of
parsing the page we read the JSON data Facebook itself loads:
  1. JSON embedded in <script type="application/json"> on first load
  2. /api/graphql/ responses fired while we scroll the feed
Every post in that data is a dict with "__typename": "Story" and a post URL.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
from collections import deque
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

from .browser import ensure_logged_in
from .config import DEBUG_DIR

POST_URL_RE = re.compile(r"/groups/([^/?#]+)/(?:posts|permalink)/(\d+)")
# A shared post nests the original post here. Its text, time and author are not the
# sharer's, and it may belong to another group, so the parser never looks inside it.
SKIP_KEYS = frozenset({"attached_story"})


# ---- JSON helpers ---------------------------------------------------------

def parse_blob(text: str) -> list:
    """Parse a Facebook JSON payload (may hold several JSON docs, one per line)."""
    text = text.strip()
    if text.startswith("for (;;);"):
        text = text[len("for (;;);"):]
    try:
        return [json.loads(text)]
    except json.JSONDecodeError:
        pass
    docs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return docs


def _walk(obj):
    """Yield every dict inside obj."""
    stack = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(v for k, v in cur.items() if k not in SKIP_KEYS)
        elif isinstance(cur, list):
            stack.extend(cur)


def _nearest(obj, pick):
    """Breadth-first search: return pick(d) for the closest dict where it is not None."""
    queue = deque([obj])
    while queue:
        cur = queue.popleft()
        if isinstance(cur, dict):
            found = pick(cur)
            if found is not None:
                return found
            queue.extend(v for k, v in cur.items() if k not in SKIP_KEYS)
        elif isinstance(cur, list):
            queue.extend(cur)
    return None


def _pick_message(d: dict):
    msg = d.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("text"), str) and msg["text"].strip():
        return msg["text"].strip()
    return None


def _pick_actor(d: dict):
    actors = d.get("actors")
    if isinstance(actors, list) and actors and isinstance(actors[0], dict) and actors[0].get("name"):
        return actors[0]
    return None


def _pick_time(d: dict):
    t = d.get("creation_time")
    if isinstance(t, int) and t > 1_000_000_000:
        return t
    return None


def extract_posts(payload) -> dict[str, dict]:
    """Find group posts in one Facebook JSON payload, keyed by post_id."""
    found: dict[str, dict] = {}
    for d in _walk(payload):
        if d.get("__typename") != "Story":
            continue
        url = d.get("url") or d.get("wwwURL")
        if not isinstance(url, str):
            continue
        match = POST_URL_RE.search(url)
        if not match:
            continue
        post_id = str(d.get("post_id") or match.group(2))
        record = {
            "post_id": post_id,
            "url": url.split("?")[0],
            "text": _nearest(d, _pick_message),
            "created_at": _nearest(d, _pick_time),
        }
        actor = _nearest(d, _pick_actor)
        if actor:
            record["author_name"] = actor.get("name")
            record["author_id"] = str(actor["id"]) if actor.get("id") else None
        _merge_into(found, record)
    return found


def _merge_into(found: dict[str, dict], record: dict) -> None:
    current = found.setdefault(record["post_id"], {})
    for key, value in record.items():
        if value and not current.get(key):
            current[key] = value


# ---- browser --------------------------------------------------------------

def _newest_first(group_url: str) -> str:
    parts = urlsplit(group_url)
    query = dict(parse_qsl(parts.query))
    query["sorting_setting"] = "CHRONOLOGICAL"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def scrape_group(ctx: BrowserContext, group_url: str, scrolls: int, debug: bool = False) -> list[dict]:
    """Open the group sorted by new posts, scroll, and return the posts found."""
    page = ctx.new_page()
    responses = []
    page.on("response", lambda r: responses.append(r) if "/api/graphql" in r.url else None)
    try:
        page.goto(_newest_first(group_url), wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        ensure_logged_in(page)

        payloads: list = []
        for blob in page.locator('script[type="application/json"]').all_text_contents():
            if '"Story"' in blob:
                payloads.extend(parse_blob(blob))

        for _ in range(scrolls):
            page.mouse.wheel(0, 2_500)
            page.wait_for_timeout(random.randint(1_500, 3_000))  # give the feed time to load

        raw_bodies = []
        for response in responses:
            try:
                body = response.text()
            except Exception:  # body already discarded by the browser
                continue
            raw_bodies.append(body)
            payloads.extend(parse_blob(body))

        posts: dict[str, dict] = {}
        for payload in payloads:
            for record in extract_posts(payload).values():
                _merge_into(posts, record)

        if debug or not posts:
            _dump_debug(group_url, page.content(), raw_bodies, posts)
        return list(posts.values())
    finally:
        page.close()


def _dump_debug(group_url: str, html: str, bodies: list[str], posts: dict) -> None:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = DEBUG_DIR / stamp
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "page.html").write_text(html)
    (folder / "graphql.txt").write_text("\n\n----\n\n".join(bodies))
    (folder / "posts.json").write_text(json.dumps(posts, indent=2, ensure_ascii=False))
    (folder / "group.txt").write_text(group_url)
    print(f"  debug files saved to {folder}")

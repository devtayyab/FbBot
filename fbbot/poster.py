"""Post an approved comment on a Facebook group post."""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Locator, Page

from .browser import ensure_logged_in

# aria-label words of the comment box in common Facebook languages.
# Add yours in config.yaml under post.comment_box_labels if it is missing.
COMMENT_WORDS = [
    "comment", "kommentar", "kommentieren", "comentario", "comentar", "commentaire",
    "commento", "yorum", "تعليق", "تبصرہ", "تبصرہ کریں",
]
LIKE_WORDS = [
    "like", "react", "پسند", "پسند کریں", "me gusta", "j'aime", "gefällt mir", "curtir",
]
REPLY_WORDS = [
    "reply", "replies", "جواب", "جواب دیں", "responder", "répondre", "antworten", "rispondi",
]
# Never type into these: Messenger chat boxes would send a private message.
BLOCK_WORDS = re.compile(r"message|nachricht|messenger|chat|mensaje|پیغام|رسالة", re.I)


class PostError(RuntimeError):
    pass


class PostUnverified(PostError):
    """Enter was pressed and the box cleared, but the comment is not visible; it may be live."""


def _label(box: Locator) -> str:
    return " ".join(
        filter(None, [box.get_attribute("aria-label"), box.get_attribute("aria-placeholder")])
    )


def _find_comment_box(page: Page, pattern: re.Pattern) -> Locator | None:
    boxes = page.locator('div[role="textbox"][contenteditable="true"]')
    for i in range(boxes.count()):
        box = boxes.nth(i)
        if not box.is_visible():
            continue
        label = _label(box)
        if BLOCK_WORDS.search(label):
            continue
        if pattern.search(label):
            return box
    return None


def _find_comment_button(page: Page, pattern: re.Pattern) -> Locator | None:
    """The "Comment" action button, not the "12 comments" counter or a chat button."""
    buttons = page.get_by_role("button", name=pattern)
    for i in range(min(buttons.count(), 15)):
        button = buttons.nth(i)
        if not button.is_visible():
            continue
        name = (button.get_attribute("aria-label") or button.inner_text() or "").strip()
        if re.search(r"\d", name) or BLOCK_WORDS.search(name):
            continue
        return button
    return None


def post_comment(ctx: BrowserContext, url: str, text: str, extra_labels: list[str], dry_run: bool = False) -> None:
    # Drop blank labels: an empty alternative would match every text box on the page.
    words = [w.strip() for w in COMMENT_WORDS + list(extra_labels or []) if w and w.strip()]
    pattern = re.compile("|".join(re.escape(w) for w in words), re.I)
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        ensure_logged_in(page)

        box = _find_comment_box(page, pattern)
        if box is None:
            # Some layouts only show the box after pressing the "Comment" button.
            button = _find_comment_button(page, pattern)
            if button is not None:
                button.click()
                page.wait_for_timeout(2_000)
                box = _find_comment_box(page, pattern)
        if box is None:
            raise PostError("comment box not found (comments off, or your language is missing in comment_box_labels)")

        if dry_run:
            print(f"    dry run: found comment box '{_label(box)}', nothing typed")
            return

        box.click()
        page.keyboard.type(text, delay=35)  # Facebook's editor ignores pasted-in values
        page.wait_for_timeout(800)
        page.keyboard.press("Enter")
        page.wait_for_timeout(6_000)

        if box.is_visible() and box.inner_text().strip():
            raise PostError("comment text is still in the box; Facebook did not accept it")
        snippet = text[:40]
        if not page.get_by_text(snippet, exact=False).count():
            raise PostUnverified("submitted, but the comment is not visible on the page; check the post by hand")
    finally:
        page.close()


def like_comment(ctx: BrowserContext, url: str, target_comment_text: str, dry_run: bool = False) -> None:
    """Find a specific comment on a post and click the Like button."""
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        ensure_logged_in(page)

        snippet = target_comment_text.strip()[:40]
        target_el = None
        for _ in range(5):
            candidates = page.get_by_text(snippet, exact=False)
            if candidates.count():
                target_el = candidates.first
                break
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(1500)

        if not target_el or not target_el.is_visible():
            raise PostError(f"Target comment '{snippet}' not found on page to like")

        target_el.scroll_into_view_if_needed()
        page.wait_for_timeout(1000)

        # Look for comment article or card container
        container = target_el.locator("xpath=./ancestor-or-self::div[contains(@role, 'article')][1]")
        if not container.count():
            container = target_el.locator("xpath=./ancestor::div[3]")

        like_pattern = re.compile("|".join(re.escape(w) for w in LIKE_WORDS), re.I)
        like_btn = container.get_by_role("button", name=like_pattern).first
        if not like_btn.count():
            like_btn = page.get_by_role("button", name=like_pattern).first

        if not like_btn or not like_btn.is_visible():
            raise PostError("Like button not found for target comment")

        if dry_run:
            print(f"    dry run: located Like button for '{snippet}', click skipped")
            return

        like_btn.click()
        page.wait_for_timeout(2000)
    finally:
        page.close()


def reply_to_comment(
    ctx: BrowserContext,
    url: str,
    target_comment_text: str,
    reply_text: str,
    extra_labels: list[str] | None = None,
    dry_run: bool = False,
) -> None:
    """Find a specific comment, click 'Reply', type the reply and submit it."""
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        ensure_logged_in(page)

        snippet = target_comment_text.strip()[:40]
        target_el = None
        for _ in range(5):
            candidates = page.get_by_text(snippet, exact=False)
            if candidates.count():
                target_el = candidates.first
                break
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(1500)

        if not target_el or not target_el.is_visible():
            raise PostError(f"Target comment '{snippet}' not found on page to reply to")

        target_el.scroll_into_view_if_needed()
        page.wait_for_timeout(1000)

        container = target_el.locator("xpath=./ancestor-or-self::div[contains(@role, 'article')][1]")
        if not container.count():
            container = target_el.locator("xpath=./ancestor::div[3]")

        reply_pattern = re.compile("|".join(re.escape(w) for w in REPLY_WORDS), re.I)
        reply_btn = container.get_by_role("button", name=reply_pattern).first
        if not reply_btn.count():
            reply_btn = container.get_by_text(reply_pattern).first

        if not reply_btn or not reply_btn.is_visible():
            raise PostError("Reply button not found under target comment")

        if dry_run:
            print(f"    dry run: located Reply button for '{snippet}', typing skipped")
            return

        reply_btn.click()
        page.wait_for_timeout(2000)

        words = [w.strip() for w in COMMENT_WORDS + list(extra_labels or []) if w and w.strip()]
        box_pattern = re.compile("|".join(re.escape(w) for w in words), re.I)
        box = _find_comment_box(page, box_pattern)
        if box is None:
            box = page.locator(':focus')
        if box is None or not box.is_visible():
            raise PostError("Reply comment box did not appear after clicking Reply")

        box.click()
        page.keyboard.type(reply_text, delay=35)
        page.wait_for_timeout(800)
        page.keyboard.press("Enter")
        page.wait_for_timeout(6000)

        if box.is_visible() and box.inner_text().strip():
            raise PostError("Reply text is still in the box; Facebook did not accept it")
    finally:
        page.close()

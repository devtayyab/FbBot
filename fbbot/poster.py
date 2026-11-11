"""Post an approved comment on a Facebook group post."""
from __future__ import annotations

import re

from playwright.sync_api import BrowserContext, Locator, Page

from .browser import ensure_logged_in

# aria-label words of the comment box in common Facebook languages.
# Add yours in config.yaml under post.comment_box_labels if it is missing.
COMMENT_WORDS = [
    "comment", "kommentar", "kommentieren", "comentario", "comentar", "commentaire",
    "commento", "yorum", "تعليق", "تبصرہ", "تبصرہ کریں",
]
# Never type into these: Messenger chat boxes would send a private message.
BLOCK_WORDS = re.compile(r"message|nachricht|messenger|chat|mensaje|پیغام|رسالة", re.I)


class PostError(RuntimeError):
    pass


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


def post_comment(ctx: BrowserContext, url: str, text: str, extra_labels: list[str], dry_run: bool = False) -> None:
    pattern = re.compile("|".join(re.escape(w) for w in COMMENT_WORDS + list(extra_labels)), re.I)
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        ensure_logged_in(page)

        box = _find_comment_box(page, pattern)
        if box is None:
            # Some layouts only show the box after pressing the "Comment" button.
            button = page.get_by_role("button", name=pattern).first
            if button.count():
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
            raise PostError("submitted, but the comment is not visible on the page; check the post by hand")
    finally:
        page.close()

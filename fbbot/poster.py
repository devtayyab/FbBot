"""Post comments, likes, and threaded replies on Facebook group posts."""
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
UNLIKE_WORDS = re.compile(r"remove|unlike|undo|ہٹائیں|quitar|supprimer|entfernen", re.I)
REPLY_WORDS = [
    "reply", "replies", "جواب", "جواب دیں", "responder", "répondre", "antworten", "rispondi",
]
REPLY_BOX_WORDS = [
    "reply", "write a reply", "جواب", "responder", "répondre", "antworten", "rispondi",
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


def _find_comment_container(page: Page, target_comment_text: str) -> tuple[Locator, str]:
    """Scroll to find a comment matching target_comment_text and return its container locator."""
    clean = " ".join(target_comment_text.strip().split())
    snippets = [clean[:45]]
    if len(clean) > 25:
        snippets.append(clean[:25])

    target_el = None
    matched_snippet = snippets[0]
    for _ in range(5):
        for snip in snippets:
            candidates = page.get_by_text(snip, exact=False)
            for i in range(candidates.count()):
                cand = candidates.nth(i)
                if cand.is_visible():
                    target_el = cand
                    matched_snippet = snip
                    break
            if target_el is not None:
                break
        if target_el is not None:
            break
        page.mouse.wheel(0, 1_500)
        page.wait_for_timeout(1_500)

    if target_el is None or not target_el.is_visible():
        raise PostError(f"Target comment '{snippets[0]}' not found on page")

    target_el.scroll_into_view_if_needed()
    page.wait_for_timeout(800)

    # Closest role="article" ancestor is the comment itself (not the outer post).
    container = target_el.locator("xpath=./ancestor-or-self::div[@role='article'][1]")
    if not container.count():
        container = target_el.locator("xpath=./ancestor::div[3]")
    return container, matched_snippet


def _find_like_button(container: Locator) -> tuple[Locator | None, bool]:
    """Find the Like button inside a comment container. Returns (button, already_liked)."""
    like_pattern = re.compile("|".join(re.escape(w) for w in LIKE_WORDS), re.I)
    buttons = container.get_by_role("button", name=like_pattern)
    for i in range(min(buttons.count(), 10)):
        btn = buttons.nth(i)
        if not btn.is_visible():
            continue
        name = (btn.get_attribute("aria-label") or btn.inner_text() or "").strip()
        if BLOCK_WORDS.search(name):
            continue
        pressed = (btn.get_attribute("aria-pressed") or "").lower() == "true"
        if pressed or UNLIKE_WORDS.search(name):
            return btn, True
        return btn, False
    return None, False


def _find_reply_button(container: Locator) -> Locator | None:
    reply_pattern = re.compile("|".join(re.escape(w) for w in REPLY_WORDS), re.I)
    buttons = container.get_by_role("button", name=reply_pattern)
    for i in range(min(buttons.count(), 10)):
        btn = buttons.nth(i)
        if not btn.is_visible():
            continue
        name = (btn.get_attribute("aria-label") or btn.inner_text() or "").strip()
        if BLOCK_WORDS.search(name):
            continue
        return btn
    txt = container.get_by_text(reply_pattern).first
    if txt.count() and txt.is_visible():
        return txt
    return None


def _find_reply_box(page: Page, container: Locator, extra_labels: list[str] | None = None) -> Locator | None:
    """Locate the nested reply textbox without accidentally grabbing the post's main comment box."""
    # 1. Facebook auto-focuses the reply input right after clicking Reply.
    focused = page.locator('div[role="textbox"][contenteditable="true"]:focus')
    if focused.count() and focused.first.is_visible():
        lbl = _label(focused.first)
        if not BLOCK_WORDS.search(lbl):
            return focused.first

    # 2. Search within the comment thread (container's parent) for a reply or comment box.
    words = [w.strip() for w in REPLY_BOX_WORDS + COMMENT_WORDS + list(extra_labels or []) if w and w.strip()]
    any_pattern = re.compile("|".join(re.escape(w) for w in words), re.I)
    thread = container.locator("xpath=..")
    boxes = thread.locator('div[role="textbox"][contenteditable="true"]')
    for i in range(boxes.count()):
        box = boxes.nth(i)
        if not box.is_visible():
            continue
        lbl = _label(box)
        if not BLOCK_WORDS.search(lbl) and (not lbl or any_pattern.search(lbl)):
            return box

    # 3. Last resort: match explicit reply-specific label anywhere on the page (never main comment box).
    reply_words = [w.strip() for w in REPLY_BOX_WORDS + list(extra_labels or []) if w and w.strip()]
    reply_pattern = re.compile("|".join(re.escape(w) for w in reply_words), re.I)
    return _find_comment_box(page, reply_pattern)


def _submit_reply(page: Page, box: Locator, reply_text: str) -> None:
    box.click()
    page.keyboard.type(reply_text, delay=35)
    page.wait_for_timeout(800)
    page.keyboard.press("Enter")
    page.wait_for_timeout(6_000)

    if box.is_visible() and box.inner_text().strip():
        raise PostError("Reply text is still in the box; Facebook did not accept it")
    snippet = reply_text.strip()[:40]
    if snippet and not page.get_by_text(snippet, exact=False).count():
        raise PostUnverified("Reply submitted, but not visible on the page; check the post by hand")


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
    """Find a specific comment on a post and click its Like button."""
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        ensure_logged_in(page)

        container, snippet = _find_comment_container(page, target_comment_text)
        like_btn, already_liked = _find_like_button(container)
        if like_btn is None:
            raise PostError(f"Like button not found inside comment '{snippet}'")

        if already_liked:
            print(f"    comment '{snippet}' is already liked; skipping click")
            return

        if dry_run:
            print(f"    dry run: located Like button for '{snippet}', click skipped")
            return

        like_btn.click()
        page.wait_for_timeout(2_000)
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

        container, snippet = _find_comment_container(page, target_comment_text)
        reply_btn = _find_reply_button(container)
        if reply_btn is None:
            raise PostError(f"Reply button not found under comment '{snippet}'")

        if dry_run:
            print(f"    dry run: located Reply button for '{snippet}', typing skipped")
            return

        reply_btn.click()
        page.wait_for_timeout(2_000)

        box = _find_reply_box(page, container, extra_labels)
        if box is None or not box.is_visible():
            raise PostError("Reply comment box did not appear after clicking Reply")

        _submit_reply(page, box, reply_text)
    finally:
        page.close()


def like_and_reply_comment(
    ctx: BrowserContext,
    url: str,
    target_comment_text: str,
    reply_text: str,
    extra_labels: list[str] | None = None,
    dry_run: bool = False,
) -> None:
    """Find a specific comment, Like it (if not already liked), then click Reply and submit."""
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)
        ensure_logged_in(page)

        container, snippet = _find_comment_container(page, target_comment_text)
        like_btn, already_liked = _find_like_button(container)
        reply_btn = _find_reply_button(container)

        if like_btn is None:
            raise PostError(f"Like button not found inside comment '{snippet}'")
        if reply_btn is None:
            raise PostError(f"Reply button not found under comment '{snippet}'")

        if dry_run:
            print(f"    dry run: located Like + Reply buttons for '{snippet}', action skipped")
            return

        if not already_liked:
            like_btn.click()
            page.wait_for_timeout(1_200)

        reply_btn.click()
        page.wait_for_timeout(2_000)

        box = _find_reply_box(page, container, extra_labels)
        if box is None or not box.is_visible():
            raise PostError("Reply comment box did not appear after clicking Reply")

        _submit_reply(page, box, reply_text)
    finally:
        page.close()

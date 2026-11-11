"""SQLite storage: every post the bot has seen, its draft and what happened to it.

Post status flow:
    ignored              not a visitor/question, too old, or no text
    pending  -> approved -> posted | unverified | failed
             -> skipped

Comment history = every row with posted_at set (posted or unverified).
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import sqlite3

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_id           TEXT PRIMARY KEY,
    group_url         TEXT NOT NULL,
    url               TEXT NOT NULL,
    author_id         TEXT,
    author_name       TEXT,
    text              TEXT,
    created_at        INTEGER,            -- unix seconds from Facebook, may be NULL
    first_seen        TEXT NOT NULL,      -- local ISO time the bot first saw it
    first_time_poster INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL,
    analysis          TEXT,               -- JSON from the drafter
    draft             TEXT,               -- reply Claude suggested
    final_comment     TEXT,               -- reply you approved (maybe edited)
    approved_by       TEXT,               -- auto | review | edited
    error             TEXT,
    posted_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);

CREATE TABLE IF NOT EXISTS authors (
    author_id  TEXT PRIMARY KEY,
    name       TEXT,
    first_seen TEXT NOT NULL,
    post_count INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path=DB_PATH):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was first created."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(posts)")}
        if "approved_by" not in columns:
            self.db.execute("ALTER TABLE posts ADD COLUMN approved_by TEXT")
        self.db.execute("UPDATE posts SET status = 'unverified' WHERE status = 'failed' AND posted_at IS NOT NULL")
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- scanning -------------------------------------------------------

    def has_post(self, post_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,)).fetchone() is not None

    def is_empty(self) -> bool:
        """True before the first scan has stored anything."""
        return self.db.execute("SELECT 1 FROM posts LIMIT 1").fetchone() is None

    def add_post(
        self, post: dict, group_url: str, status: str, error: str | None = None, baseline: bool = False
    ) -> bool:
        """Insert a newly seen post. Returns True if the author was never seen before.

        On the baseline (first) scan every author is unknown, so nobody is flagged:
        "first-time poster" means the first post since the bot started watching.
        """
        first_time = False
        author_id = post.get("author_id")
        if author_id:
            row = self.db.execute("SELECT post_count FROM authors WHERE author_id = ?", (author_id,)).fetchone()
            if row is None:
                first_time = not baseline
                self.db.execute(
                    "INSERT INTO authors (author_id, name, first_seen, post_count) VALUES (?, ?, ?, 1)",
                    (author_id, post.get("author_name"), _now()),
                )
            else:
                self.db.execute("UPDATE authors SET post_count = post_count + 1 WHERE author_id = ?", (author_id,))
        self.db.execute(
            """INSERT INTO posts (post_id, group_url, url, author_id, author_name, text, created_at,
                                  first_seen, first_time_poster, status, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                post["post_id"], group_url, post["url"], author_id, post.get("author_name"),
                post.get("text"), post.get("created_at"), _now(), int(first_time), status, error,
            ),
        )
        self.db.commit()
        return first_time

    def discard_post(self, post_id: str) -> None:
        """Undo add_post (incl. the author count) so the next scan tries this post again."""
        row = self.db.execute(
            "SELECT author_id, first_time_poster FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        if row is None:
            return
        if row["author_id"]:
            if row["first_time_poster"]:
                self.db.execute("DELETE FROM authors WHERE author_id = ?", (row["author_id"],))
            else:
                self.db.execute(
                    "UPDATE authors SET post_count = post_count - 1 WHERE author_id = ?", (row["author_id"],)
                )
        self.db.execute("DELETE FROM posts WHERE post_id = ?", (post_id,))
        self.db.commit()

    def save_analysis(self, post_id: str, analysis: dict, status: str) -> None:
        self.db.execute(
            "UPDATE posts SET analysis = ?, draft = ?, status = ? WHERE post_id = ?",
            (json.dumps(analysis, ensure_ascii=False), analysis.get("reply") or None, status, post_id),
        )
        self.db.commit()

    # ---- review & posting -----------------------------------------------

    def by_status(self, status: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM posts WHERE status = ? ORDER BY first_seen", (status,)
        ).fetchall()

    def approve(self, post_id: str, comment: str, by: str = "review") -> None:
        self.db.execute(
            "UPDATE posts SET status = 'approved', final_comment = ?, approved_by = ? WHERE post_id = ?",
            (comment, by, post_id),
        )
        self.db.commit()

    def set_status(self, post_id: str, status: str, error: str | None = None) -> None:
        self.db.execute("UPDATE posts SET status = ?, error = ? WHERE post_id = ?", (status, error, post_id))
        self.db.commit()

    def mark_posted(self, post_id: str) -> None:
        self.db.execute(
            "UPDATE posts SET status = 'posted', posted_at = ?, error = NULL WHERE post_id = ?", (_now(), post_id)
        )
        self.db.commit()

    def mark_unverified(self, post_id: str, error: str) -> None:
        """Comment was submitted but not seen on the page. It is in the history and counts toward the daily cap."""
        self.db.execute(
            "UPDATE posts SET status = 'unverified', posted_at = ?, error = ? WHERE post_id = ?",
            (_now(), error, post_id),
        )
        self.db.commit()

    # ---- history --------------------------------------------------------

    def history(self, limit: int | None = None) -> list[sqlite3.Row]:
        """Posts the bot commented on, newest first."""
        sql = "SELECT * FROM posts WHERE posted_at IS NOT NULL ORDER BY posted_at DESC"
        if limit:
            sql += f" LIMIT {int(limit)}"
        return self.db.execute(sql).fetchall()

    def export_history(self, path) -> int:
        """Write the full comment history to a CSV file (opens in Excel or Numbers). Returns row count."""
        rows = self.history()
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "commented_at", "status", "group", "post_url", "author", "post_written_at",
                "post_text", "questions", "travel_dates", "draft", "comment", "approved_by",
            ])
            for r in rows:
                analysis = json.loads(r["analysis"]) if r["analysis"] else {}
                written = (
                    dt.datetime.fromtimestamp(r["created_at"]).isoformat(timespec="minutes") if r["created_at"] else ""
                )
                writer.writerow([
                    r["posted_at"], r["status"], r["group_url"], r["url"], r["author_name"] or "", written,
                    r["text"] or "", "; ".join(analysis.get("questions") or []), analysis.get("travel_dates") or "",
                    r["draft"] or "", r["final_comment"] or "", r["approved_by"] or "",
                ])
        return len(rows)

    def posted_today(self) -> int:
        """Comments submitted today, including ones Facebook may have taken but we could not verify."""
        today = dt.date.today().isoformat()
        row = self.db.execute(
            "SELECT COUNT(*) FROM posts WHERE posted_at IS NOT NULL AND substr(posted_at, 1, 10) = ?", (today,)
        ).fetchone()
        return row[0]

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) FROM posts GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}

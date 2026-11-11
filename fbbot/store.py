"""SQLite storage: every post the bot has seen, its draft and what happened to it.

Post status flow:
    ignored              not a visitor/question, too old, or no text
    pending  -> approved -> posted | failed
             -> skipped
"""
from __future__ import annotations

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

    def close(self) -> None:
        self.db.close()

    # ---- scanning -------------------------------------------------------

    def has_post(self, post_id: str) -> bool:
        return self.db.execute("SELECT 1 FROM posts WHERE post_id = ?", (post_id,)).fetchone() is not None

    def add_post(self, post: dict, group_url: str, status: str, error: str | None = None) -> bool:
        """Insert a newly seen post. Returns True if the author was never seen before."""
        first_time = False
        author_id = post.get("author_id")
        if author_id:
            row = self.db.execute("SELECT post_count FROM authors WHERE author_id = ?", (author_id,)).fetchone()
            if row is None:
                first_time = True
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

    def approve(self, post_id: str, comment: str) -> None:
        self.db.execute(
            "UPDATE posts SET status = 'approved', final_comment = ? WHERE post_id = ?", (comment, post_id)
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

    def posted_today(self) -> int:
        today = dt.date.today().isoformat()
        row = self.db.execute(
            "SELECT COUNT(*) FROM posts WHERE status = 'posted' AND substr(posted_at, 1, 10) = ?", (today,)
        ).fetchone()
        return row[0]

    def counts(self) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) FROM posts GROUP BY status").fetchall()
        return {r[0]: r[1] for r in rows}

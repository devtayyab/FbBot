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
    primary_agent_id  TEXT,               -- agent that posted the primary comment
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

CREATE TABLE IF NOT EXISTS agent_actions (
    action_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id             TEXT NOT NULL,
    agent_id            TEXT NOT NULL,
    action_type         TEXT NOT NULL, -- 'comment', 'reply', 'like', 'skip'
    target_agent_id     TEXT,          -- agent being replied to or liked
    target_comment_text TEXT,          -- parent comment text snippet
    content             TEXT,          -- text to post (empty for 'like' or 'skip')
    confidence          TEXT,          -- 'high', 'medium', 'low'
    analysis            TEXT,          -- JSON details
    status              TEXT NOT NULL, -- 'pending', 'approved', 'posted', 'unverified', 'failed', 'skipped'
    approved_by         TEXT,          -- 'auto', 'review', 'edited'
    error               TEXT,
    scheduled_after     TEXT,          -- ISO timestamp when this action can execute
    created_at          TEXT NOT NULL,
    executed_at         TEXT,
    FOREIGN KEY (post_id) REFERENCES posts(post_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_actions_post ON agent_actions(post_id);
CREATE INDEX IF NOT EXISTS idx_agent_actions_agent ON agent_actions(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_actions_status ON agent_actions(status);
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
        """Add columns and tables introduced after a database was first created."""
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(posts)")}
        if "approved_by" not in columns:
            self.db.execute("ALTER TABLE posts ADD COLUMN approved_by TEXT")
        if "primary_agent_id" not in columns:
            self.db.execute("ALTER TABLE posts ADD COLUMN primary_agent_id TEXT")
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

    # ---- multi-agent coordination actions -------------------------------

    def add_action(
        self,
        post_id: str,
        agent_id: str,
        action_type: str,
        content: str = "",
        target_agent_id: str | None = None,
        target_comment_text: str | None = None,
        confidence: str = "medium",
        analysis: dict | None = None,
        status: str = "pending",
        approved_by: str | None = None,
        scheduled_after: str | None = None,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO agent_actions (
                   post_id, agent_id, action_type, target_agent_id, target_comment_text,
                   content, confidence, analysis, status, approved_by, scheduled_after, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                post_id, agent_id, action_type, target_agent_id, target_comment_text,
                content, confidence, json.dumps(analysis, ensure_ascii=False) if analysis else None,
                status, approved_by, scheduled_after, _now(),
            ),
        )
        self.db.commit()
        return cur.lastrowid

    def get_post_actions(self, post_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM agent_actions WHERE post_id = ? ORDER BY action_id", (post_id,)
        ).fetchall()

    def has_agent_acted(self, post_id: str, agent_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM agent_actions WHERE post_id = ? AND agent_id = ? LIMIT 1",
            (post_id, agent_id),
        ).fetchone()
        return row is not None

    def get_primary_comment(self, post_id: str) -> dict | None:
        """Find the primary top-level comment made on this post by our agents."""
        # 1. Check agent_actions
        row = self.db.execute(
            """SELECT * FROM agent_actions
               WHERE post_id = ? AND action_type = 'comment' AND status IN ('posted', 'approved', 'unverified')
               ORDER BY action_id LIMIT 1""",
            (post_id,),
        ).fetchone()
        if row:
            return {
                "agent_id": row["agent_id"],
                "content": row["content"],
                "status": row["status"],
                "executed_at": row["executed_at"],
            }
        # 2. Check posts table for legacy / default agent comment
        p = self.db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if p and (p["final_comment"] or p["draft"]) and p["status"] in ("posted", "approved", "unverified"):
            return {
                "agent_id": p["primary_agent_id"] or "default",
                "content": p["final_comment"] or p["draft"],
                "status": p["status"],
                "executed_at": p["posted_at"],
            }
        return None

    def pending_actions(self, agent_id: str | None = None) -> list[sqlite3.Row]:
        sql = """SELECT a.*, p.url, p.text as post_text, p.author_name as post_author
                 FROM agent_actions a
                 JOIN posts p ON a.post_id = p.post_id
                 WHERE a.status = 'pending'"""
        params = []
        if agent_id:
            sql += " AND a.agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY a.action_id"
        return self.db.execute(sql, params).fetchall()

    def due_actions(self, agent_id: str | None = None) -> list[sqlite3.Row]:
        """Approved actions whose scheduled time has arrived."""
        now = _now()
        sql = """SELECT a.*, p.url, p.text as post_text, p.author_name as post_author
                 FROM agent_actions a
                 JOIN posts p ON a.post_id = p.post_id
                 WHERE a.status = 'approved'
                   AND (a.scheduled_after IS NULL OR a.scheduled_after <= ?)"""
        params = [now]
        if agent_id:
            sql += " AND a.agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY a.action_id"
        return self.db.execute(sql, params).fetchall()

    def approve_action(self, action_id: int, content: str | None = None, by: str = "review") -> None:
        if content is not None:
            self.db.execute(
                "UPDATE agent_actions SET status = 'approved', content = ?, approved_by = ? WHERE action_id = ?",
                (content, by, action_id),
            )
        else:
            self.db.execute(
                "UPDATE agent_actions SET status = 'approved', approved_by = ? WHERE action_id = ?",
                (by, action_id),
            )
        self.db.commit()

    def set_action_status(self, action_id: int, status: str, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE agent_actions SET status = ?, error = ? WHERE action_id = ?",
            (status, error, action_id),
        )
        self.db.commit()

    def mark_action_executed(self, action_id: int, unverified_error: str | None = None) -> None:
        now = _now()
        status = "unverified" if unverified_error else "posted"
        self.db.execute(
            "UPDATE agent_actions SET status = ?, error = ?, executed_at = ? WHERE action_id = ?",
            (status, unverified_error, now, action_id),
        )
        self.db.commit()

    def posted_today_by_agent(self, agent_id: str) -> int:
        today = dt.date.today().isoformat()
        row = self.db.execute(
            """SELECT COUNT(*) FROM agent_actions
               WHERE agent_id = ? AND executed_at IS NOT NULL
                 AND action_type IN ('comment', 'reply')
                 AND substr(executed_at, 1, 10) = ?""",
            (agent_id, today),
        ).fetchone()
        return row[0] if row else 0

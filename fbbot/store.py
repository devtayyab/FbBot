"""SQLite storage: every post the bot has seen, its drafts and multi-agent actions.

Post status flow:
    ignored              not a visitor/question, too old, or no text
    pending  -> approved -> posted | unverified | failed
             -> skipped

Multi-agent action flow (`agent_actions`):
    pending  -> approved -> posted | unverified | failed
             -> skipped

Comment history = every executed comment/reply/like in agent_actions or legacy posts.
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
    draft             TEXT,               -- reply drafted
    final_comment     TEXT,               -- reply approved (maybe edited)
    approved_by       TEXT,               -- auto | review | edited
    primary_agent_id  TEXT,               -- agent that posted the primary comment
    existing_comments TEXT,               -- JSON list of pre-existing Facebook comments
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
    action_type         TEXT NOT NULL, -- 'comment', 'reply', 'like', 'like_and_reply', 'skip'
    target_action_id    INTEGER,       -- parent agent_actions.action_id (if targeting our agent's comment)
    target_agent_id     TEXT,          -- agent or author being replied to or liked
    target_comment_text TEXT,          -- parent comment text snippet
    content             TEXT,          -- text to post (empty for 'like' or 'skip')
    confidence          TEXT,          -- 'high', 'medium', 'low'
    analysis            TEXT,          -- JSON details
    status              TEXT NOT NULL, -- 'pending', 'approved', 'posted', 'unverified', 'failed', 'skipped'
    approved_by         TEXT,          -- 'auto', 'review', 'edited'
    error               TEXT,
    delay_minutes       INTEGER NOT NULL DEFAULT 0,
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
        post_cols = {row[1] for row in self.db.execute("PRAGMA table_info(posts)")}
        if "approved_by" not in post_cols:
            self.db.execute("ALTER TABLE posts ADD COLUMN approved_by TEXT")
        if "primary_agent_id" not in post_cols:
            self.db.execute("ALTER TABLE posts ADD COLUMN primary_agent_id TEXT")
        if "existing_comments" not in post_cols:
            self.db.execute("ALTER TABLE posts ADD COLUMN existing_comments TEXT")

        act_cols = {row[1] for row in self.db.execute("PRAGMA table_info(agent_actions)")}
        if "target_action_id" not in act_cols:
            self.db.execute("ALTER TABLE agent_actions ADD COLUMN target_action_id INTEGER")
        if "delay_minutes" not in act_cols:
            self.db.execute("ALTER TABLE agent_actions ADD COLUMN delay_minutes INTEGER NOT NULL DEFAULT 0")

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

    def get_post(self, post_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()

    def is_empty(self) -> bool:
        """True before the first scan has stored anything."""
        return self.db.execute("SELECT 1 FROM posts LIMIT 1").fetchone() is None

    def total_posts(self) -> int:
        row = self.db.execute("SELECT COUNT(*) FROM posts").fetchone()
        return row[0] if row else 0

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

        existing_json = json.dumps(post["comments"], ensure_ascii=False) if post.get("comments") else None
        self.db.execute(
            """INSERT INTO posts (post_id, group_url, url, author_id, author_name, text, created_at,
                                  first_seen, first_time_poster, status, existing_comments, error)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                post["post_id"], group_url, post["url"], author_id, post.get("author_name"),
                post.get("text"), post.get("created_at"), _now(), int(first_time), status, existing_json, error,
            ),
        )
        self.db.commit()
        return first_time

    def save_existing_comments(self, post_id: str, comments: list[dict]) -> None:
        if not comments:
            return
        self.db.execute(
            "UPDATE posts SET existing_comments = ? WHERE post_id = ?",
            (json.dumps(comments, ensure_ascii=False), post_id),
        )
        self.db.commit()

    def get_existing_comments(self, post_id: str) -> list[dict]:
        row = self.db.execute("SELECT existing_comments FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        if not row or not row["existing_comments"]:
            return []
        try:
            data = json.loads(row["existing_comments"])
            return data if isinstance(data, list) else []
        except Exception:
            return []

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

    def save_analysis(self, post_id: str, analysis: dict, status: str, primary_agent_id: str | None = None) -> None:
        self.db.execute(
            "UPDATE posts SET analysis = ?, draft = ?, status = ?, primary_agent_id = COALESCE(?, primary_agent_id) WHERE post_id = ?",
            (json.dumps(analysis, ensure_ascii=False), analysis.get("reply") or None, status, primary_agent_id, post_id),
        )
        self.db.commit()

    # ---- legacy / post-level review & posting ----------------------------

    def by_status(self, status: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT * FROM posts WHERE status = ? ORDER BY first_seen", (status,)
        ).fetchall()

    def legacy_approved(self) -> list[sqlite3.Row]:
        """Approved posts that do not have a managed agent_actions record."""
        return self.db.execute(
            """SELECT p.* FROM posts p
               WHERE p.status = 'approved'
                 AND NOT EXISTS (
                     SELECT 1 FROM agent_actions a WHERE a.post_id = p.post_id AND a.action_type = 'comment'
                 )
               ORDER BY p.first_seen"""
        ).fetchall()

    def approve(self, post_id: str, comment: str, by: str = "review", primary_agent_id: str | None = None) -> None:
        self.db.execute(
            """UPDATE posts
               SET status = 'approved', final_comment = ?, approved_by = ?,
                   primary_agent_id = COALESCE(?, primary_agent_id)
               WHERE post_id = ?""",
            (comment, by, primary_agent_id, post_id),
        )
        self.db.commit()

    def set_status(self, post_id: str, status: str, error: str | None = None) -> None:
        self.db.execute("UPDATE posts SET status = ?, error = ? WHERE post_id = ?", (status, error, post_id))
        self.db.commit()

    def mark_posted(self, post_id: str, final_comment: str | None = None, approved_by: str | None = None) -> None:
        self.db.execute(
            """UPDATE posts
               SET status = 'posted', posted_at = ?, error = NULL,
                   final_comment = COALESCE(?, final_comment, draft),
                   approved_by = COALESCE(?, approved_by)
               WHERE post_id = ?""",
            (_now(), final_comment, approved_by, post_id),
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

    def history(self, limit: int | None = None) -> list[dict]:
        """Combined executed actions (multi-agent + legacy posts), newest first."""
        rows = self.db.execute(
            """SELECT a.executed_at AS commented_at, a.status, a.agent_id, a.action_type,
                      p.group_url, p.url, p.author_name, p.created_at AS post_created_at,
                      p.text AS post_text, p.analysis, p.draft,
                      a.content AS final_comment, a.approved_by, a.target_agent_id
               FROM agent_actions a
               JOIN posts p ON a.post_id = p.post_id
               WHERE a.executed_at IS NOT NULL
                 AND a.action_type IN ('comment', 'reply', 'like_and_reply', 'like')
               ORDER BY a.executed_at DESC"""
        ).fetchall()

        items = [dict(r) for r in rows]

        # Include legacy posts that have no corresponding agent_actions
        legacy_rows = self.db.execute(
            """SELECT p.posted_at AS commented_at, p.status,
                      COALESCE(p.primary_agent_id, 'default') AS agent_id,
                      'comment' AS action_type,
                      p.group_url, p.url, p.author_name, p.created_at AS post_created_at,
                      p.text AS post_text, p.analysis, p.draft,
                      p.final_comment, p.approved_by, NULL AS target_agent_id
               FROM posts p
               WHERE p.posted_at IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM agent_actions a WHERE a.post_id = p.post_id)
               ORDER BY p.posted_at DESC"""
        ).fetchall()

        items.extend(dict(r) for r in legacy_rows)
        items.sort(key=lambda x: x["commented_at"] or "", reverse=True)
        if limit:
            items = items[: int(limit)]
        return items

    def export_history(self, path) -> int:
        """Write the full comment & engagement history to CSV. Returns row count."""
        rows = self.history()
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            writer = csv.writer(fh)
            writer.writerow([
                "commented_at", "status", "agent_id", "action_type", "group", "post_url", "author",
                "post_written_at", "post_text", "questions", "travel_dates", "draft", "comment",
                "target_agent_id", "approved_by",
            ])
            for r in rows:
                analysis = json.loads(r["analysis"]) if r.get("analysis") else {}
                written = (
                    dt.datetime.fromtimestamp(r["post_created_at"]).isoformat(timespec="minutes")
                    if r.get("post_created_at") else ""
                )
                writer.writerow([
                    r["commented_at"], r["status"], r["agent_id"], r["action_type"],
                    r["group_url"], r["url"], r["author_name"] or "", written,
                    r["post_text"] or "", "; ".join(analysis.get("questions") or []),
                    analysis.get("travel_dates") or "", r["draft"] or "",
                    r["final_comment"] or "", r.get("target_agent_id") or "", r["approved_by"] or "",
                ])
        return len(rows)

    def posted_today(self) -> int:
        """Comments submitted today across legacy posts + agent_actions."""
        today = dt.date.today().isoformat()
        row = self.db.execute(
            """SELECT COUNT(*) FROM agent_actions
               WHERE executed_at IS NOT NULL
                 AND action_type IN ('comment', 'reply', 'like_and_reply')
                 AND substr(executed_at, 1, 10) = ?""",
            (today,),
        ).fetchone()
        act_count = row[0] if row else 0
        legacy_row = self.db.execute(
            """SELECT COUNT(*) FROM posts p
               WHERE p.posted_at IS NOT NULL AND substr(p.posted_at, 1, 10) = ?
                 AND NOT EXISTS (SELECT 1 FROM agent_actions a WHERE a.post_id = p.post_id)""",
            (today,),
        ).fetchone()
        return act_count + (legacy_row[0] if legacy_row else 0)

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
        target_action_id: int | None = None,
        target_agent_id: str | None = None,
        target_comment_text: str | None = None,
        confidence: str = "medium",
        analysis: dict | None = None,
        status: str = "pending",
        approved_by: str | None = None,
        delay_minutes: int = 0,
        scheduled_after: str | None = None,
    ) -> int:
        cur = self.db.execute(
            """INSERT INTO agent_actions (
                   post_id, agent_id, action_type, target_action_id, target_agent_id,
                   target_comment_text, content, confidence, analysis, status, approved_by,
                   delay_minutes, scheduled_after, created_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                post_id, agent_id, action_type, target_action_id, target_agent_id,
                target_comment_text, content, confidence,
                json.dumps(analysis, ensure_ascii=False) if analysis else None,
                status, approved_by, delay_minutes, scheduled_after, _now(),
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

    def get_primary_comment(self, post_id: str, include_pending: bool = False) -> dict | None:
        """Find the primary top-level comment made on this post by our agents (or legacy posts)."""
        statuses = ("posted", "approved", "unverified", "pending") if include_pending else ("posted", "approved", "unverified")
        placeholders = ",".join("?" for _ in statuses)
        row = self.db.execute(
            f"""SELECT * FROM agent_actions
                WHERE post_id = ? AND action_type = 'comment' AND status IN ({placeholders})
                ORDER BY action_id LIMIT 1""",
            (post_id, *statuses),
        ).fetchone()
        if row:
            return {
                "action_id": row["action_id"],
                "agent_id": row["agent_id"],
                "content": row["content"],
                "status": row["status"],
                "executed_at": row["executed_at"],
            }
        p = self.db.execute("SELECT * FROM posts WHERE post_id = ?", (post_id,)).fetchone()
        p_statuses = ("posted", "approved", "unverified", "pending") if include_pending else ("posted", "approved", "unverified")
        if p and (p["final_comment"] or p["draft"]) and p["status"] in p_statuses:
            return {
                "action_id": None,
                "agent_id": p["primary_agent_id"] or "default",
                "content": p["final_comment"] or p["draft"],
                "status": p["status"],
                "executed_at": p["posted_at"],
            }
        return None

    def get_existing_replies(self, post_id: str) -> list[str]:
        """Return replies already planned or posted on this post so subsequent agents don't repeat them."""
        rows = self.db.execute(
            """SELECT content FROM agent_actions
               WHERE post_id = ? AND action_type IN ('reply', 'like_and_reply')
                 AND status IN ('posted', 'approved', 'unverified', 'pending')
                 AND content IS NOT NULL AND content != ''
               ORDER BY action_id""",
            (post_id,),
        ).fetchall()
        return [r["content"] for r in rows]

    def posts_ready_for_collaboration(self) -> list[sqlite3.Row]:
        """Posts where our primary comment is approved/posted and has text."""
        return self.db.execute(
            """SELECT DISTINCT p.*
               FROM posts p
               JOIN agent_actions a ON a.post_id = p.post_id
               WHERE a.action_type = 'comment'
                 AND a.status IN ('posted', 'unverified', 'approved')
                 AND p.text IS NOT NULL AND p.text != ''"""
        ).fetchall()

    def pending_actions(self, agent_id: str | None = None) -> list[sqlite3.Row]:
        sql = """SELECT a.*, p.url, p.text AS post_text, p.author_name AS post_author,
                        COALESCE(t.content, a.target_comment_text) AS live_target_comment
                 FROM agent_actions a
                 JOIN posts p ON a.post_id = p.post_id
                 LEFT JOIN agent_actions t ON a.target_action_id = t.action_id
                 WHERE a.status = 'pending'
                   AND (a.target_action_id IS NULL OR t.status NOT IN ('skipped', 'failed'))"""
        params: list = []
        if agent_id:
            sql += " AND a.agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY a.action_id"
        return self.db.execute(sql, params).fetchall()

    def due_actions(self, agent_id: str | None = None) -> list[sqlite3.Row]:
        """Approved actions whose parent comment (if any) is posted AND scheduled time has arrived."""
        now = _now()
        sql = """SELECT a.*, p.url, p.text AS post_text, p.author_name AS post_author,
                        COALESCE(t.content, a.target_comment_text) AS live_target_comment
                 FROM agent_actions a
                 JOIN posts p ON a.post_id = p.post_id
                 LEFT JOIN agent_actions t ON a.target_action_id = t.action_id
                 WHERE a.status = 'approved'
                   AND (a.scheduled_after IS NULL OR a.scheduled_after <= ?)
                   AND (a.target_action_id IS NULL OR t.status IN ('posted', 'unverified'))"""
        params: list = [now]
        if agent_id:
            sql += " AND a.agent_id = ?"
            params.append(agent_id)
        sql += " ORDER BY a.action_id"
        return self.db.execute(sql, params).fetchall()

    def waiting_delay_count(self, agent_id: str) -> int:
        """Approved actions still waiting for their natural delay or parent comment."""
        now = _now()
        row = self.db.execute(
            """SELECT COUNT(*)
               FROM agent_actions a
               LEFT JOIN agent_actions t ON a.target_action_id = t.action_id
               WHERE a.agent_id = ? AND a.status = 'approved'
                 AND (
                     (a.scheduled_after IS NOT NULL AND a.scheduled_after > ?)
                     OR (a.target_action_id IS NOT NULL AND t.status NOT IN ('posted', 'unverified'))
                 )""",
            (agent_id, now),
        ).fetchone()
        return row[0] if row else 0

    def approve_action(self, action_id: int, content: str | None = None, by: str = "review") -> None:
        row = self.db.execute("SELECT * FROM agent_actions WHERE action_id = ?", (action_id,)).fetchone()
        if row is None:
            return
        final_text = content if content is not None else row["content"]
        self.db.execute(
            "UPDATE agent_actions SET status = 'approved', content = ?, approved_by = ? WHERE action_id = ?",
            (final_text, by, action_id),
        )
        # Keep posts table and dependent child actions in sync when a primary comment is edited/approved.
        if row["action_type"] == "comment":
            self.db.execute(
                """UPDATE posts
                   SET status = 'approved', final_comment = ?, approved_by = ?,
                       primary_agent_id = COALESCE(primary_agent_id, ?)
                   WHERE post_id = ? AND status IN ('pending', 'new', 'approved')""",
                (final_text, by, row["agent_id"], row["post_id"]),
            )
            self.db.execute(
                "UPDATE agent_actions SET target_comment_text = ? WHERE target_action_id = ?",
                (final_text, action_id),
            )
        self.db.commit()

    def set_action_status(self, action_id: int, status: str, error: str | None = None) -> None:
        row = self.db.execute("SELECT * FROM agent_actions WHERE action_id = ?", (action_id,)).fetchone()
        self.db.execute(
            "UPDATE agent_actions SET status = ?, error = ? WHERE action_id = ?",
            (status, error, action_id),
        )
        if row and row["action_type"] == "comment" and status in ("skipped", "failed"):
            # Cancel dependent pending/approved child actions if the parent comment didn't go out.
            self.db.execute(
                """UPDATE agent_actions
                   SET status = 'skipped', error = ?
                   WHERE target_action_id = ? AND status IN ('pending', 'approved')""",
                (f"parent comment {status}", action_id),
            )
            if status == "skipped":
                self.db.execute(
                    "UPDATE posts SET status = 'skipped' WHERE post_id = ? AND status IN ('pending', 'approved')",
                    (row["post_id"],),
                )
            elif status == "failed":
                self.db.execute(
                    "UPDATE posts SET status = 'failed', error = ? WHERE post_id = ? AND status IN ('pending', 'approved')",
                    (error, row["post_id"]),
                )
        self.db.commit()

    def mark_action_executed(self, action_id: int, unverified_error: str | None = None) -> None:
        now_dt = dt.datetime.now()
        now_iso = now_dt.isoformat(timespec="seconds")
        status = "unverified" if unverified_error else "posted"
        row = self.db.execute("SELECT * FROM agent_actions WHERE action_id = ?", (action_id,)).fetchone()
        self.db.execute(
            "UPDATE agent_actions SET status = ?, error = ?, executed_at = ? WHERE action_id = ?",
            (status, unverified_error, now_iso, action_id),
        )
        if row:
            if row["action_type"] == "comment":
                # Sync posts table if this is the first posted comment or legacy sync.
                self.db.execute(
                    """UPDATE posts
                       SET status = ?, posted_at = COALESCE(posted_at, ?),
                           final_comment = COALESCE(final_comment, ?),
                           approved_by = COALESCE(approved_by, ?),
                           primary_agent_id = COALESCE(primary_agent_id, ?),
                           error = ?
                       WHERE post_id = ?""",
                    (status, now_iso, row["content"], row["approved_by"], row["agent_id"], unverified_error, row["post_id"]),
                )
            # Re-anchor child actions' scheduled_after relative to the ACTUAL execution timestamp!
            children = self.db.execute(
                "SELECT action_id, delay_minutes FROM agent_actions WHERE target_action_id = ? AND status IN ('pending', 'approved')",
                (action_id,),
            ).fetchall()
            for child in children:
                delay = int(child["delay_minutes"] or 0)
                sched = (now_dt + dt.timedelta(minutes=delay)).isoformat(timespec="seconds")
                self.db.execute(
                    "UPDATE agent_actions SET scheduled_after = ?, target_comment_text = ? WHERE action_id = ?",
                    (sched, row["content"], child["action_id"]),
                )
        self.db.commit()

    def posted_today_by_agent(self, agent_id: str) -> int:
        today = dt.date.today().isoformat()
        row = self.db.execute(
            """SELECT COUNT(*) FROM agent_actions
               WHERE agent_id = ? AND executed_at IS NOT NULL
                 AND action_type IN ('comment', 'reply', 'like_and_reply')
                 AND substr(executed_at, 1, 10) = ?""",
            (agent_id, today),
        ).fetchone()
        return row[0] if row else 0

    def recent_action_issues(self, limit: int = 5) -> list[sqlite3.Row]:
        return self.db.execute(
            """SELECT a.agent_id, a.action_type, a.status, a.error, p.url
               FROM agent_actions a
               JOIN posts p ON a.post_id = p.post_id
               WHERE a.status IN ('failed', 'unverified')
               ORDER BY a.action_id DESC LIMIT ?""",
            (limit,),
        ).fetchall()

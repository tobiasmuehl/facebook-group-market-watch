"""SQLite storage. Two tables plus a cooldown table. That's the whole schema.

Parsed fields live in one JSON column because every watcher has different
fields — typed columns would mean a migration every time you edit watch.py.

ponytail: JSON blob + json_extract. Add SQLite generated columns if a query
gets measurably slow; at hobby volume (<1M posts) it won't.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

# Stages a post can die at. Stored so `watcher test` can show you exactly where
# each post was dropped — this is the whole debugging story.
STAGE_PENDING = "pending"
STAGE_KEYWORD = "keyword"        # gate 0: dropped by PREFILTER
STAGE_CLASSIFY = "classify"      # gate 1: dropped by cheap LLM classifier
STAGE_DEDUP = "dedup"            # dropped as a repost of an earlier post
STAGE_PARSED = "parsed"          # gate 2 done, survived to gate 3
STAGE_FAILED = "failed"          # LLM call or parse failed; retryable

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    post_url     TEXT PRIMARY KEY,
    group_id     TEXT NOT NULL,
    raw_text     TEXT NOT NULL DEFAULT '',
    image_urls   TEXT NOT NULL DEFAULT '[]',
    posted_at    TEXT,
    scraped_at   TEXT NOT NULL,
    stage        TEXT NOT NULL DEFAULT 'pending',
    reason       TEXT,
    parsed_json  TEXT,
    matched      INTEGER,
    text_hash    TEXT,
    image_sigs   TEXT NOT NULL DEFAULT '[]',
    attempts     INTEGER NOT NULL DEFAULT 0,
    claimed_by   TEXT,
    claimed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_posts_stage      ON posts(stage);
CREATE INDEX IF NOT EXISTS idx_posts_scraped_at ON posts(scraped_at DESC);
CREATE INDEX IF NOT EXISTS idx_posts_text_hash  ON posts(text_hash);

CREATE TABLE IF NOT EXISTS notifications (
    post_url TEXT PRIMARY KEY REFERENCES posts(post_url),
    sent_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_state (
    group_id        TEXT PRIMARY KEY,
    last_scraped_at TEXT NOT NULL
);
"""


def _now() -> str:
    # UTC always. Naive local timestamps were a real bug in the predecessor.
    return datetime.now(timezone.utc).isoformat()


def db_path() -> Path:
    path = Path(os.getenv("DB_PATH", "data/watcher.db"))
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def _row(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["image_urls"] = json.loads(out.get("image_urls") or "[]")
    out["image_sigs"] = json.loads(out.get("image_sigs") or "[]")
    if out.get("parsed_json"):
        out["parsed"] = json.loads(out["parsed_json"])
    else:
        out["parsed"] = None
    return out


# ── writes ───────────────────────────────────────────────────────────────────

def insert_posts(posts: list[dict[str, Any]]) -> int:
    """Insert scraped posts. Existing post_urls are left untouched.

    post_url is the primary key, which is the only dedup that has to be exact.
    Repost dedup (same item, new URL) happens later in the pipeline.
    """
    if not posts:
        return 0
    now = _now()
    rows = [
        (
            p["post_url"],
            p["group_id"],
            p.get("raw_text") or "",
            json.dumps(p.get("image_urls") or []),
            p.get("posted_at"),
            now,
        )
        for p in posts
        if p.get("post_url")
    ]
    with connect() as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO posts "
            "(post_url, group_id, raw_text, image_urls, posted_at, scraped_at) "
            "VALUES (?,?,?,?,?,?)",
            rows,
        )
        return conn.total_changes - before


def post_exists(post_url: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM posts WHERE post_url = ?", (post_url,)
        ).fetchone() is not None


def claim_pending(worker: str, limit: int = 50, lease_minutes: int = 30) -> list[dict[str, Any]]:
    """Claim pending posts for processing, with a lease.

    Without this, two `watcher run` processes parse the same post and you get
    duplicate Telegram alerts. The lease means a crashed worker's claims become
    available again instead of wedging forever.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=lease_minutes)).isoformat()
    now = _now()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT post_url FROM posts
            WHERE stage IN (?, ?)
              AND (claimed_by IS NULL OR claimed_at < ?)
            ORDER BY scraped_at ASC
            LIMIT ?
            """,
            (STAGE_PENDING, STAGE_FAILED, cutoff, limit),
        ).fetchall()
        urls = [r["post_url"] for r in rows]
        if not urls:
            return []
        conn.executemany(
            "UPDATE posts SET claimed_by = ?, claimed_at = ?, attempts = attempts + 1 "
            "WHERE post_url = ?",
            [(worker, now, u) for u in urls],
        )
        placeholders = ",".join("?" * len(urls))
        claimed = conn.execute(
            f"SELECT * FROM posts WHERE post_url IN ({placeholders})", urls
        ).fetchall()
        return [_row(r) for r in claimed]


def release_claims(worker: str) -> None:
    """Hand back claims we never got to — e.g. we hit an API quota wall.

    Posts go back to pending so they are retried after you top up, instead of
    being silently lost.
    """
    with connect() as conn:
        conn.execute(
            "UPDATE posts SET claimed_by = NULL, claimed_at = NULL "
            "WHERE claimed_by = ? AND stage IN (?, ?)",
            (worker, STAGE_PENDING, STAGE_FAILED),
        )


def set_stage(
    post_url: str,
    stage: str,
    reason: str | None = None,
    parsed: dict[str, Any] | None = None,
    matched: bool | None = None,
    text_hash: str | None = None,
    image_sigs: list[str] | None = None,
) -> None:
    fields: dict[str, Any] = {"stage": stage, "reason": reason, "claimed_by": None, "claimed_at": None}
    if parsed is not None:
        fields["parsed_json"] = json.dumps(parsed, ensure_ascii=False)
    if matched is not None:
        fields["matched"] = int(matched)
    if text_hash is not None:
        fields["text_hash"] = text_hash
    if image_sigs is not None:
        fields["image_sigs"] = json.dumps(image_sigs)
    assignments = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(
            f"UPDATE posts SET {assignments} WHERE post_url = ?",
            (*fields.values(), post_url),
        )


# ── dedup lookups ────────────────────────────────────────────────────────────

def find_duplicate_by_text(text_hash: str, lookback_days: int) -> str | None:
    if not text_hash:
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    with connect() as conn:
        row = conn.execute(
            "SELECT post_url FROM posts WHERE text_hash = ? AND scraped_at >= ? LIMIT 1",
            (text_hash, cutoff),
        ).fetchone()
        return row["post_url"] if row else None


def recent_image_sigs(lookback_days: int) -> list[tuple[str, list[str]]]:
    """(post_url, signatures) for recent posts that have any images."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()
    with connect() as conn:
        rows = conn.execute(
            "SELECT post_url, image_sigs FROM posts "
            "WHERE scraped_at >= ? AND image_sigs != '[]'",
            (cutoff,),
        ).fetchall()
        return [(r["post_url"], json.loads(r["image_sigs"] or "[]")) for r in rows]


# ── notifications ────────────────────────────────────────────────────────────

def already_notified(post_url: str) -> bool:
    with connect() as conn:
        return conn.execute(
            "SELECT 1 FROM notifications WHERE post_url = ?", (post_url,)
        ).fetchone() is not None


def record_notification(post_url: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO notifications (post_url, sent_at) VALUES (?, ?)",
            (post_url, _now()),
        )


# ── group cooldown ───────────────────────────────────────────────────────────

def cooldown_remaining_seconds(group_id: str, min_interval_minutes: int) -> int:
    if min_interval_minutes <= 0:
        return 0
    with connect() as conn:
        row = conn.execute(
            "SELECT last_scraped_at FROM group_state WHERE group_id = ?", (group_id,)
        ).fetchone()
    if not row:
        return 0
    last = datetime.fromisoformat(row["last_scraped_at"])
    elapsed = (datetime.now(timezone.utc) - last).total_seconds()
    return max(0, int(min_interval_minutes * 60 - elapsed))


def touch_group(group_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO group_state (group_id, last_scraped_at) VALUES (?, ?) "
            "ON CONFLICT(group_id) DO UPDATE SET last_scraped_at = excluded.last_scraped_at",
            (group_id, _now()),
        )


# ── reads ────────────────────────────────────────────────────────────────────

def parsed_posts(limit: int = 1000) -> list[dict[str, Any]]:
    """Posts that made it through extraction — the input to `watcher rematch`."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM posts WHERE parsed_json IS NOT NULL "
            "ORDER BY scraped_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_row(r) for r in rows]


def matched_posts(limit: int = 100, offset: int = 0, q: str = "") -> tuple[list[dict[str, Any]], int]:
    where = "matched = 1"
    params: list[Any] = []
    if q:
        where += " AND (raw_text LIKE ? OR parsed_json LIKE ?)"
        params += [f"%{q}%", f"%{q}%"]
    with connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) c FROM posts WHERE {where}", params).fetchone()["c"]
        rows = conn.execute(
            f"SELECT * FROM posts WHERE {where} ORDER BY scraped_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return [_row(r) for r in rows], total


def stats() -> dict[str, int]:
    with connect() as conn:
        by_stage = {
            r["stage"]: r["c"]
            for r in conn.execute("SELECT stage, COUNT(*) c FROM posts GROUP BY stage")
        }
        by_stage["total"] = conn.execute("SELECT COUNT(*) c FROM posts").fetchone()["c"]
        by_stage["matched"] = conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE matched = 1"
        ).fetchone()["c"]
        by_stage["notified"] = conn.execute(
            "SELECT COUNT(*) c FROM notifications"
        ).fetchone()["c"]
        return by_stage


def purge(retention_days: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
    with connect() as conn:
        conn.execute(
            "DELETE FROM notifications WHERE post_url IN "
            "(SELECT post_url FROM posts WHERE scraped_at < ? AND matched IS NOT 1)",
            (cutoff,),
        )
        cur = conn.execute(
            "DELETE FROM posts WHERE scraped_at < ? AND matched IS NOT 1", (cutoff,)
        )
        return cur.rowcount

"""Local state in SQLite: reported postings (90-day memory), pending overflow, small caches, run markers."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

DEFAULT_DB = Path.home() / ".local" / "share" / "job-copilot" / "state.sqlite"


class Store:
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")  # the run and the bot share this file
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, first_seen TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cache (name TEXT PRIMARY KEY, at TEXT NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS pending (pos INTEGER PRIMARY KEY, posting TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tokens (
                token TEXT PRIMARY KEY, thread_id TEXT NOT NULL, created TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS messages (name TEXT PRIMARY KEY, message_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS run_links (
                key TEXT PRIMARY KEY, run_id TEXT, route TEXT NOT NULL, score INTEGER,
                posting TEXT NOT NULL, created TEXT NOT NULL, promoted INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS applications (
                key TEXT PRIMARY KEY, title TEXT NOT NULL, company TEXT NOT NULL, url TEXT NOT NULL,
                status TEXT NOT NULL, updated TEXT NOT NULL);
            """
        )

    # --- button tokens and tracker -------------------------------------------------------------
    def new_token(self, thread_id: str, now: datetime) -> str:
        """An opaque, unguessable token for a button; maps back to one application thread."""
        token = secrets.token_urlsafe(12)
        with self.db:
            self.db.execute(
                "INSERT INTO tokens (token, thread_id, created) VALUES (?, ?, ?)",
                (token, thread_id, now.isoformat()),
            )
        return token

    def thread_for(self, token: str) -> str | None:
        row = self.db.execute("SELECT thread_id FROM tokens WHERE token = ?", (token,)).fetchone()
        return row[0] if row else None

    def set_status(self, key: str, title: str, company: str, url: str, status: str, now: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO applications (key, title, company, url, status, updated) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET status = excluded.status, updated = excluded.updated",
                (key, title, company, url, status, now.isoformat()),
            )

    def set_message(self, name: str, message_id: int) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO messages (name, message_id) VALUES (?, ?)", (name, message_id))

    def message(self, name: str) -> int | None:
        row = self.db.execute("SELECT message_id FROM messages WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def unmark_seen(self, keys: list[str]) -> None:
        with self.db:
            self.db.executemany("DELETE FROM seen WHERE key = ?", [(k,) for k in keys])

    def requeue(self, items: list[tuple[str, dict]]) -> None:
        """Put (key, posting-dict) items back at the front of the pending queue and forget they were seen,
        so a run that could not screen or deliver them leaves them for the next run."""
        if not items:
            return
        self.unmark_seen([k for k, _ in items])
        self.set_pending([p for _, p in items] + self.pending())

    # --- LangSmith links for owner feedback ----------------------------------------------------
    def link_run(self, key: str, run_id: str | None, route: str, score: int | None, posting: dict, now: datetime):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO run_links (key, run_id, route, score, posting, created) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (key, run_id, route, score, json.dumps(posting), now.isoformat()),
            )

    def run_link(self, key: str) -> dict | None:
        row = self.db.execute("SELECT run_id, route, score, posting FROM run_links WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return {"run_id": row[0], "route": row[1], "score": row[2], "posting": json.loads(row[3])}

    def unpromoted_feedback(self) -> list[dict]:
        """Postings the owner acted on (tracker status) that are not yet in the live eval dataset."""
        rows = self.db.execute(
            "SELECT r.key, r.route, r.score, r.posting, a.status FROM run_links r "
            "JOIN applications a ON a.key = r.key WHERE r.promoted = 0"
        ).fetchall()
        return [{"key": k, "route": ro, "score": s, "posting": json.loads(p), "status": st} for k, ro, s, p, st in rows]

    def mark_promoted(self, keys: list[str]) -> None:
        with self.db:
            self.db.executemany("UPDATE run_links SET promoted = 1 WHERE key = ?", [(k,) for k in keys])

    def status_counts(self) -> dict[str, int]:
        return dict(self.db.execute("SELECT status, COUNT(*) FROM applications GROUP BY status").fetchall())

    def get_meta(self, name: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE name = ?", (name,)).fetchone()
        return row[0] if row else None

    def set_meta(self, name: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta (name, value) VALUES (?, ?)", (name, value))

    # --- run markers ---------------------------------------------------------------------------
    def initialised(self) -> bool:
        """True once a real (non-dry) run has completed, even one that reported nothing."""
        return self.db.execute("SELECT 1 FROM meta WHERE name = 'initialised'").fetchone() is not None

    def mark_initialised(self, at: datetime) -> None:
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta (name, value) VALUES ('initialised', ?)", (at.isoformat(),))

    # --- seen ----------------------------------------------------------------------------------
    def seen_keys(self) -> set[str]:
        return {k for (k,) in self.db.execute("SELECT key FROM seen")}

    def mark_seen(self, keys: list[str], day: date) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO seen (key, first_seen) VALUES (?, ?)",
                [(k, day.isoformat()) for k in keys],
            )

    def prune(self, today: date, ttl_days: int) -> int:
        cutoff = (today - timedelta(days=ttl_days)).isoformat()
        with self.db:
            return self.db.execute("DELETE FROM seen WHERE first_seen < ?", (cutoff,)).rowcount

    # --- pending overflow ----------------------------------------------------------------------
    def pending(self) -> list[dict]:
        return [json.loads(p) for (p,) in self.db.execute("SELECT posting FROM pending ORDER BY pos")]

    def set_pending(self, postings: list[dict]) -> None:
        with self.db:
            self.db.execute("DELETE FROM pending")
            self.db.executemany(
                "INSERT INTO pending (pos, posting) VALUES (?, ?)",
                [(i, json.dumps(p)) for i, p in enumerate(postings)],
            )

    # --- cache ---------------------------------------------------------------------------------
    def cache_get(self, name: str, max_age: timedelta, now: datetime) -> object | None:
        row = self.db.execute("SELECT at, value FROM cache WHERE name = ?", (name,)).fetchone()
        if not row or now - datetime.fromisoformat(row[0]) >= max_age:
            return None
        return json.loads(row[1])

    def cache_put(self, name: str, value: object, now: datetime) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO cache (name, at, value) VALUES (?, ?, ?)",
                (name, now.isoformat(), json.dumps(value)),
            )

    def close(self) -> None:
        self.db.close()

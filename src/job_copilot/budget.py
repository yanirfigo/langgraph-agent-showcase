"""Daily model-call cap, persisted in SQLite so restarts and parallel subagents share one count."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from langchain.agents.middleware import AgentMiddleware

from .store import DEFAULT_DB


class BudgetExceeded(RuntimeError):
    pass


def count_model_call(db_path: Path, tz: str, limit: int) -> int:
    """Atomically add one call for today; raise BudgetExceeded once today's total passes the limit."""
    day = datetime.now(ZoneInfo(tz)).date().isoformat()
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with sqlite3.connect(db_path, timeout=10) as db:
        db.execute("CREATE TABLE IF NOT EXISTS usage (day TEXT PRIMARY KEY, model_calls INTEGER NOT NULL)")
        db.execute(
            "INSERT INTO usage (day, model_calls) VALUES (?, 1) "
            "ON CONFLICT(day) DO UPDATE SET model_calls = model_calls + 1",
            (day,),
        )
        (n,) = db.execute("SELECT model_calls FROM usage WHERE day = ?", (day,)).fetchone()
    if n > limit:
        raise BudgetExceeded(f"daily model-call cap reached ({limit})")
    return n


class DailyModelCallCap(AgentMiddleware):
    """Counts every model call any subagent makes; refuses the call once the daily cap is passed."""

    def __init__(self, limit: int, tz: str, db_path: Path = DEFAULT_DB) -> None:
        super().__init__()
        self.limit, self.tz, self.db_path = limit, tz, db_path

    def before_model(self, state, runtime):
        count_model_call(self.db_path, self.tz, self.limit)
        return None

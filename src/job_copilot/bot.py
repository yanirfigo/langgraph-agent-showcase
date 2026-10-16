"""Telegram listener: resumes application threads when the owner presses a button.

    uv run python -m job_copilot.bot

Only the owner's private chat is served. A button carries an opaque token (mapped to one thread in
SQLite) plus an action; the action is checked against what that thread is currently waiting for,
so a stale or forged button cannot push a thread anywhere it isn't already allowed to go.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from datetime import UTC, datetime

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .application import AFTER, FIRST, build_app_graph
from .config import load_config
from .feedback import record_feedback
from .logsafe import setup_process
from .observability import make_client, tracing
from .secrets import load_secrets
from .store import DEFAULT_DB, Store
from .subagents.draft import build_drafter
from .telegram import Telegram, TelegramError

log = logging.getLogger("job_copilot.bot")
CHECKPOINTS = DEFAULT_DB.with_name("checkpoints.sqlite")
HELP = "Job Copilot. Buttons on each job: Draft, Save, Not for me. Commands: /status, /help."


def recover(app, config, invoke: Callable) -> None:
    """A thread with a pending node but no pending interrupt was cut off mid-node (crash, restart).
    Finish it so it reaches its next pause; side-effect nodes are safe to re-run."""
    snap = app.get_state(config)
    if snap.next and not snap.interrupts:
        log.warning("finishing an interrupted step for %s", config["configurable"]["thread_id"])
        invoke(None, config)


def _stage(app, config) -> str | None:
    snap = app.get_state(config)
    if not snap.next or not snap.interrupts:
        return None
    value = snap.interrupts[0].value
    return value.get("stage") if isinstance(value, dict) else None


def handle_update(
    update: dict, *, tg: Telegram, store: Store, app, invoke: Callable = None, on_action: Callable = None
) -> str:
    """Handle one Telegram update. Returns a short outcome label (for logs and tests).
    `on_action(key, action)` is called after a press was applied (used for LangSmith feedback)."""
    invoke = invoke or (lambda cmd, config: app.invoke(cmd, config))
    if not tg.is_owner(update):
        log.warning("ignored update from a non-owner chat")
        return "ignored"

    if "callback_query" in update:
        cq = update["callback_query"]
        token, _, action = (cq.get("data") or "").partition(":")
        thread_id = store.thread_for(token)
        if action not in FIRST + AFTER or thread_id is None:
            tg.answer(cq["id"], "That button is no longer valid.")
            return "invalid"
        config = {"configurable": {"thread_id": thread_id}}
        recover(app, config, invoke)
        stage = _stage(app, config)
        allowed = FIRST if stage == "first" else AFTER if stage == "after_draft" else ()
        if action not in allowed:
            tg.answer(cq["id"], "Already handled.")
            return "stale"
        tg.answer(cq["id"], "Drafting… (about a minute)" if action == "draft" else "Done.")
        invoke(Command(resume=action), config)
        if on_action:
            on_action(app.get_state(config).values.get("key"), action)
        return action

    text = ((update.get("message") or {}).get("text") or "").strip().lower()
    if text in ("/status", "status"):
        counts = store.status_counts()
        tg.send("Tracker: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "empty"))
        return "status"
    if text in ("/start", "/help", "help"):
        tg.send(HELP)
        return "help"
    return "unhandled"


def main() -> None:
    setup_process()
    cfg, secrets = load_config(), load_secrets()
    if not (secrets.telegram_bot_token and secrets.telegram_chat_id):
        raise SystemExit("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required in secrets.env")
    store = Store()
    tg = Telegram(secrets.telegram_bot_token, secrets.telegram_chat_id)
    saver = SqliteSaver(sqlite3.connect(CHECKPOINTS, check_same_thread=False))
    app = build_app_graph(store, tg, build_drafter(cfg, secrets), saver, now=lambda: datetime.now(UTC))
    offset = int(store.get_meta("tg_offset") or 0)
    feedback_client = make_client(cfg, secrets)  # feedback = score + action name only

    def on_action(key: str | None, action: str) -> None:
        if key:
            record_feedback(feedback_client, store, key, action)

    log.info("bot started")
    while True:
        try:
            updates = tg.updates(offset)
        except TelegramError as e:
            log.warning("getUpdates failed: %s", e)
            time.sleep(10)
            continue
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            store.set_meta("tg_offset", str(offset))  # at-most-once: a crashing update is not retried forever
            try:
                with tracing(cfg, secrets, hidden=True):  # traces keep structure and timing, not CV/draft text
                    outcome = handle_update(u, tg=tg, store=store, app=app, on_action=on_action)
                log.info("update %s: %s", u["update_id"], outcome)
            except Exception:
                log.exception("update %s failed", u["update_id"])


if __name__ == "__main__":
    main()

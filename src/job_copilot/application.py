"""Per-posting application thread: the human-in-the-loop half of the agent.

    ask --draft--> draft (sends it) --> ask_after --applied--> record_applied --> END
     |  \\--save--> record_saved --> ask               \\--discard--> record_discarded --> ask
     \\--dismiss--> record_dismissed --> END

`ask` and `ask_after` only call interrupt(): the thread pauses, is checkpointed in SQLite, and is
resumed by the bot when the owner presses a button (even after a restart). Nodes with side effects
never contain an interrupt, never raise on Telegram problems (they always complete and reach the
next pause), and keep message ids in the store rather than in graph state. If a process dies
mid-node, the bot finishes the pending node on the next press (see bot.recover).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from datetime import datetime
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .ingest.models import Posting
from .store import Store
from .subagents.draft import DraftFn, draft_text
from .subagents.research import CheckedBrief
from .telegram import Button, Telegram, TelegramError

log = logging.getLogger(__name__)

FIRST = ("draft", "save", "dismiss")
AFTER = ("applied", "discard")
LABELS = {
    "draft": "✍ Draft",
    "save": "★ Save",
    "dismiss": "✕ Not for me",
    "applied": "✓ I applied",
    "discard": "↺ Discard",
}


class AppState(TypedDict, total=False):
    key: str
    posting: dict
    brief: dict | None
    choice: str


def thread_id_for(key: str) -> str:
    return "app-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def buttons(store: Store, thread_id: str, actions: tuple[str, ...], now: datetime) -> list[list[Button]]:
    token = store.new_token(thread_id, now)
    return [[Button(LABELS[a], f"{token}:{a}") for a in actions]]


def build_app_graph(store: Store, tg: Telegram, draft_fn: DraftFn, checkpointer, now: Callable[[], datetime]):
    def _posting(state: AppState) -> Posting:
        return Posting.model_validate(state["posting"])

    def _track(state: AppState, status: str) -> None:
        p = _posting(state)
        store.set_status(p.key, p.title, p.company, p.url, status, now())

    def _tid(state: AppState) -> str:
        return thread_id_for(state["key"])

    def ask(state: AppState) -> dict:
        choice = interrupt({"stage": "first", "key": state["key"]})
        return {"choice": choice if choice in FIRST else "invalid"}

    def after_ask(state: AppState) -> str:
        return {"draft": "draft", "save": "record_saved", "dismiss": "record_dismissed"}.get(state["choice"], "ask")

    def record_saved(state: AppState) -> dict:
        _track(state, "saved")
        tid = _tid(state)
        tg.set_buttons(store.message(tid), buttons(store, tid, ("draft", "dismiss"), now()))
        return {}

    def record_dismissed(state: AppState) -> dict:
        _track(state, "dismissed")
        tg.set_buttons(store.message(_tid(state)), None)
        return {}

    def draft(state: AppState) -> dict:
        p = _posting(state)
        brief = CheckedBrief.model_validate(state["brief"]) if state.get("brief") else None
        try:
            d = draft_fn(p, brief)
        except Exception as e:  # noqa: BLE001 - tell the owner, keep the thread usable
            log.warning("draft failed: %s", type(e).__name__)
            _notify(f"Couldn't draft this one ({type(e).__name__}). Try Draft again later.")
            return {"choice": "draft_failed"}
        tid = _tid(state)
        _track(state, "drafted")  # before the send: the tracker never lags a delivered draft
        try:
            store.set_message(tid + ":draft", tg.send(draft_text(p, d), buttons(store, tid, AFTER, now())))
        except TelegramError as e:
            log.warning("draft delivery failed: %s", e)
            return {"choice": "draft_failed"}
        return {"choice": "drafted"}

    def _notify(text: str) -> None:
        try:
            tg.send(text)
        except TelegramError as e:
            log.warning("notify failed: %s", e)

    def after_draft(state: AppState) -> str:
        return "ask_after" if state.get("choice") == "drafted" else "ask"

    def ask_after(state: AppState) -> dict:
        choice = interrupt({"stage": "after_draft", "key": state["key"]})
        return {"choice": choice if choice in AFTER else "invalid"}

    def after_ask_after(state: AppState) -> str:
        return {"applied": "record_applied", "discard": "record_discarded"}.get(state["choice"], "ask_after")

    def record_discarded(state: AppState) -> dict:
        _track(state, "draft discarded")
        tg.set_buttons(store.message(_tid(state) + ":draft"), None)
        return {}

    def record_applied(state: AppState) -> dict:
        _track(state, "applied")
        tid = _tid(state)
        tg.set_buttons(store.message(tid + ":draft"), None)
        tg.set_buttons(store.message(tid), None)
        return {}

    g = StateGraph(AppState)
    for name, fn in [
        ("ask", ask),
        ("record_saved", record_saved),
        ("record_dismissed", record_dismissed),
        ("draft", draft),
        ("ask_after", ask_after),
        ("record_applied", record_applied),
        ("record_discarded", record_discarded),
    ]:
        g.add_node(name, fn)
    g.add_edge(START, "ask")
    g.add_conditional_edges("ask", after_ask, ["draft", "record_saved", "record_dismissed", "ask"])
    g.add_edge("record_saved", "ask")
    g.add_edge("record_dismissed", END)
    g.add_conditional_edges("draft", after_draft, ["ask_after", "ask"])
    g.add_conditional_edges("ask_after", after_ask_after, ["record_applied", "record_discarded", "ask_after"])
    g.add_edge("record_discarded", "ask")
    g.add_edge("record_applied", END)
    return g.compile(checkpointer=checkpointer, name="application")


def offer(
    app, store: Store, tg: Telegram, text: str, posting: Posting, brief: CheckedBrief | None, now: datetime
) -> str | None:
    """Park a new application thread at `ask`, then send its message.

    Checkpoint first, message second: a button can never point at a thread that doesn't exist. If the
    send fails, the thread is deleted and TelegramError propagates so the caller can requeue the posting.
    """
    tid = thread_id_for(posting.key)
    config = {"configurable": {"thread_id": tid}}
    if app.get_state(config).values:  # already offered earlier: never duplicate
        return None
    app.invoke(
        {
            "key": posting.key,
            "posting": posting.model_dump(mode="json"),
            "brief": brief.model_dump(mode="json") if brief else None,
        },
        config,
    )
    try:
        store.set_message(tid, tg.send(text, buttons(store, tid, FIRST, now)))
    except TelegramError:
        app.checkpointer.delete_thread(tid)
        raise
    return tid

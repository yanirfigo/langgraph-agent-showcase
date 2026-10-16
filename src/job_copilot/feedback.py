"""Owner button presses -> LangSmith feedback on the posting's screening run.

The feedback carries only a score and the action name; no posting text, CV or draft. It is what
lets LangSmith show, per screener version, how often the owner agreed with the agent.
"""

from __future__ import annotations

import logging

from langsmith import Client

from .store import Store

log = logging.getLogger(__name__)

FEEDBACK_KEY = "owner_interest"
SCORES = {"dismiss": 0, "save": 1, "draft": 1, "applied": 1}  # "discard" says nothing about the posting


def record_feedback(client: Client | None, store: Store, key: str, action: str) -> bool:
    if client is None or action not in SCORES:
        return False
    link = store.run_link(key)
    if not link or not link.get("run_id"):
        return False
    try:
        client.create_feedback(link["run_id"], key=FEEDBACK_KEY, score=SCORES[action], value=action)
    except Exception as e:  # noqa: BLE001 - feedback is best effort; never block a button
        log.warning("feedback not recorded: %s", type(e).__name__)
        return False
    return True

"""The scheduled run graph: the LangGraph supervisor, written as code.

START --fan_out (Send x N)--> screen --> collect --to_research (Send x <=budget)--> research --> END
                                                \\-- nothing to research ------------------------> END

run.py invokes it once per scheduled run, builds the Telegram digest from its output and offers
each shown posting through the second graph, the per-posting application thread in application.py
(drafts and owner decisions; the bot process resumes it when the owner presses a button).

Every edge is a Python function. Subagents are called from `screen` and `research`; whether they
run at all and what happens to their output is decided here, never by a model.
"""

from __future__ import annotations

import operator
import uuid
from typing import Annotated, TypedDict

import langsmith as ls
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, ConfigDict

from .budget import BudgetExceeded
from .config import Config
from .guards import title_deal_breaker
from .ingest.models import Posting
from .routing import Route, route_for
from .subagents.research import CheckedBrief, ResearchFn
from .subagents.screener import FitScore, ScreenFn
from .timeouts import run_with_timeout


class Screened(BaseModel):
    model_config = ConfigDict(frozen=True)

    posting: Posting
    route: Route
    fit: FitScore | None = None  # None when code dropped it before any model call
    note: str = ""
    run_id: str | None = None  # LangSmith run of this posting's screening; owner feedback attaches here


class Researched(BaseModel):
    model_config = ConfigDict(frozen=True)

    key: str
    brief: CheckedBrief | None = None
    error: str | None = None


class RunState(TypedDict, total=False):
    new: list[Posting]
    screened: Annotated[list[Screened], operator.add]
    final: list[Screened]
    briefs: Annotated[list[Researched], operator.add]


class ScreenTask(TypedDict):
    posting: Posting


def build_graph(cfg: Config, screen_fn: ScreenFn, research_fn: ResearchFn | None = None):
    def fan_out(state: RunState) -> list[Send]:
        return [Send("screen", {"posting": p}) for p in state["new"][: cfg.limits.max_postings_per_run]]

    def screen(task: ScreenTask) -> dict:
        p = task["posting"]
        run_id = str(uuid.uuid4())
        meta = {"posting_key": p.key, "source": p.source, "roles": list(p.roles)}
        with ls.trace(
            "screen_posting",
            "chain",
            inputs={"title": p.title, "company": p.company},
            metadata=meta,
            tags=["screen"],
            run_id=run_id,
        ) as rt:
            s = _screen(p)
            rt.end(outputs={"route": s.route, "score": s.fit.score if s.fit else None, "note": s.note})
        return {"screened": [s.model_copy(update={"run_id": run_id})]}

    def _screen(p: Posting) -> Screened:
        if breaker := title_deal_breaker(p.title):
            return Screened(posting=p, route="drop", note=f"deal-breaker in title: {breaker}")
        try:
            fit = screen_fn(p)
        except BudgetExceeded:
            return Screened(posting=p, route="deferred", note="daily model budget reached")
        except Exception as e:  # noqa: BLE001 - one failed screen must not sink the batch
            return Screened(posting=p, route="digest", note=f"screener failed: {type(e).__name__}")
        return Screened(posting=p, route=route_for(fit, cfg), fit=fit)

    def collect(state: RunState) -> dict:
        """Apply the per-run research budget: best scores keep 'research', the rest go to the digest."""
        items = list(state.get("screened", []))
        research = sorted(
            (s for s in items if s.route == "research"),
            key=lambda s: s.fit.score if s.fit else 0,
            reverse=True,
        )
        over_budget = {id(s) for s in research[cfg.limits.max_research_per_run :]}
        final = [
            s.model_copy(update={"route": "digest", "note": "research budget used up"}) if id(s) in over_budget else s
            for s in items
        ]
        return {"final": final}

    def to_research(state: RunState) -> list[Send] | str:
        if research_fn is None:
            return END
        sends = [Send("research", {"posting": s.posting}) for s in state["final"] if s.route == "research"]
        return sends or END

    def research(task: ScreenTask) -> dict:
        p = task["posting"]
        try:
            brief = run_with_timeout(lambda: research_fn(p), cfg.limits.research_timeout_s)
            return {"briefs": [Researched(key=p.key, brief=brief)]}
        except TimeoutError:
            # The daemon worker stops at its own call limits and never blocks process exit.
            return {"briefs": [Researched(key=p.key, error="research timed out")]}
        except Exception as e:  # noqa: BLE001 - a failed brief must not sink the run
            return {"briefs": [Researched(key=p.key, error=f"{type(e).__name__}: {str(e)[:200]}")]}

    g = StateGraph(RunState)
    g.add_node("screen", screen)
    g.add_node("collect", collect)
    g.add_node("research", research)
    g.add_conditional_edges(START, fan_out, ["screen"])
    g.add_edge("screen", "collect")
    g.add_conditional_edges("collect", to_research, ["research", END])
    g.add_edge("research", END)
    return g.compile(name="job_copilot")

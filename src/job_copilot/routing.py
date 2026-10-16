"""Routing is plain Python over validated data. No model chooses where a posting goes."""

from __future__ import annotations

from typing import Literal

from .config import Config
from .subagents.screener import FitScore

Route = Literal["drop", "digest", "research", "deferred"]  # deferred: not screened this run (budget)


def route_for(fit: FitScore, cfg: Config) -> Route:
    if fit.deal_breaker or fit.score < cfg.thresholds.digest_min:
        return "drop"
    if fit.injection_suspected:
        return "digest"  # shown to the owner with a warning; never sent on to web research
    if fit.score >= cfg.thresholds.research_min:
        return "research"
    return "digest"

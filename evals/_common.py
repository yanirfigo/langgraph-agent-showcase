"""Shared eval setup: evals spend from their own daily budget, never the live agent's."""

from __future__ import annotations

from job_copilot.config import Config, load_config
from job_copilot.store import DEFAULT_DB

EVAL_USAGE_DB = DEFAULT_DB.with_name("eval-usage.sqlite")
EVAL_DAILY_MODEL_CALLS = 1500  # a full gate run is roughly 53 + 20x8 + 10x2 calls


def eval_config() -> Config:
    """The live config with the eval call cap; use together with usage_db=EVAL_USAGE_DB."""
    cfg = load_config()
    limits = cfg.limits.model_copy(update={"daily_model_calls": EVAL_DAILY_MODEL_CALLS})
    return cfg.model_copy(update={"limits": limits})

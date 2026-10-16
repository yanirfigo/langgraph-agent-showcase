"""Runtime configuration.

Thresholds, models, caps and the domain allowlist live in a YAML file in git. Code reads it;
no model ever sees or changes it. Validation fails loudly on anything out of range.
Deployment-specific values (e.g. the LangSmith workspace id) go in a private override file,
~/.config/job-copilot/config.local.yaml, which is deep-merged over the repo defaults: mappings
merge key by key, while any other value (a list included) replaces the default outright.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "config.yaml"
LOCAL_OVERRIDE = Path.home() / ".config" / "job-copilot" / "config.local.yaml"


class Models(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    screener: str
    research: str
    draft: str
    fallback: str


class Thresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    digest_min: int = Field(ge=0, le=100)
    research_min: int = Field(ge=0, le=100)

    @model_validator(mode="after")
    def _ordered(self) -> Thresholds:
        if self.research_min < self.digest_min:
            raise ValueError("research_min must be >= digest_min")
        return self


class Limits(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    max_postings_per_run: int = Field(ge=1, le=200)
    max_research_per_run: int = Field(ge=0, le=20)
    graph_recursion_limit: int = Field(ge=10, le=200)
    daily_model_calls: int = Field(ge=1, le=5000)
    screener_model_calls_per_run: int = Field(ge=1, le=5)
    research_model_calls_per_run: int = Field(ge=1, le=20)
    research_tool_calls_per_run: int = Field(ge=0, le=20)
    research_searches_per_run: int = Field(ge=0, le=10)
    research_timeout_s: int = Field(ge=30, le=900)
    max_digest_items: int = Field(ge=1, le=20)
    fetch_timeout_s: int = Field(ge=1, le=60)
    fetch_max_bytes: int = Field(ge=1024, le=2_000_000)


class LangSmith(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_url: str = Field(pattern=r"^https://(eu\.)?api\.smith\.langchain\.com$")
    workspace_id: str | None = Field(default=None, pattern=r"^[0-9a-f-]{36}$")  # needed for org-scoped keys
    project: str = Field(min_length=1, max_length=100)


class CompanyPass(BaseModel):
    """One company whose postings are all pulled, with the wider similar-roles net."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    domain: str = Field(max_length=253, pattern=r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
    name: str = Field(min_length=1, max_length=80)


class Sources(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    company_passes: tuple[CompanyPass, ...] = Field(default=(), max_length=10)

    @model_validator(mode="after")
    def _unique(self) -> Sources:
        domains = [cp.domain for cp in self.company_passes]
        if len(domains) != len(set(domains)):
            raise ValueError("company_passes domains must be unique")
        return self


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    langsmith: LangSmith
    models: Models
    thresholds: Thresholds
    limits: Limits
    sources: Sources = Sources()
    fetch_allowlist: tuple[str, ...]
    timezone: str


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Path = DEFAULT_CONFIG, local: Path | None = LOCAL_OVERRIDE) -> Config:
    with path.open(encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if local is not None and local.is_file():
        with local.open(encoding="utf-8") as fh:
            raw = _deep_merge(raw, yaml.safe_load(fh) or {})
    return Config.model_validate(raw)

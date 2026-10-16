"""Deterministic ingest: fetch, normalise, filter, dedupe. No model calls."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import httpx
from pydantic import ValidationError

from ..store import Store
from . import sources
from .filters import is_excluded, role_of
from .models import Posting
from .sources import Batch

if TYPE_CHECKING:
    from ..config import CompanyPass

log = logging.getLogger(__name__)

SEEN_TTL_DAYS = 90
TECHMAP_REFRESH = timedelta(hours=6)
OCEAN_DAYS_FIRST_RUN = 7
OCEAN_DAYS = 2


@dataclass
class IngestResult:
    first_run: bool
    new: list[Posting]  # what this run reports (capped)
    overflow: int  # new postings queued for the next run
    skipped: int = 0  # source rows that failed validation
    errors: list[str] = field(default_factory=list)


class AllSourcesFailed(RuntimeError):
    pass


Fetcher = Callable[[], Batch]


def _techmap_cached(client: httpx.Client, store: Store, now: datetime) -> Batch:
    cached = store.cache_get("techmap", TECHMAP_REFRESH, now)
    if isinstance(cached, list):
        try:
            return Batch([Posting.model_validate(x) for x in cached])
        except ValidationError:
            log.warning("techmap cache invalid; refetching")  # never let a bad cache stick for 6 h
    batch = sources.techmap(client)
    store.cache_put("techmap", [p.model_dump() for p in batch.postings], now)
    return batch


def default_fetchers(
    client: httpx.Client,
    store: Store,
    first_run: bool,
    now: datetime,
    company_passes: Sequence[CompanyPass] | None = None,
) -> dict[str, Fetcher]:
    """The source fetchers, in run order.

    `company_passes` defaults to `sources.company_passes` of the loaded config (repo defaults plus
    the local override). Order matters: company passes run first so their wider tagging wins the
    dedupe.
    """
    if company_passes is None:
        from ..config import load_config

        company_passes = load_config().sources.company_passes
    fetchers: dict[str, Fetcher] = {
        f"company:{cp.domain}": (lambda cp=cp: sources.company_pass(client, cp.domain, cp.name))
        for cp in company_passes
    }
    fetchers["oceanofjobs"] = lambda: sources.ocean(client, OCEAN_DAYS_FIRST_RUN if first_run else OCEAN_DAYS)
    fetchers["techmap"] = lambda: _techmap_cached(client, store, now)
    return fetchers


def _pending(store: Store) -> list[Posting]:
    out = []
    for raw in store.pending():
        try:
            out.append(Posting.model_validate(raw))
        except ValidationError:
            log.warning("dropping invalid pending posting")
    return out


def run_ingest(
    store: Store,
    fetchers: dict[str, Fetcher],
    *,
    first_run: bool,
    today: date,
    max_report: int,
    now: datetime,
    dry_run: bool = False,
) -> IngestResult:
    # Overflow from earlier runs comes first: the source window may no longer include it.
    collected: list[Posting] = _pending(store)
    errors: list[str] = []
    skipped = 0
    for name, fetch in fetchers.items():
        try:
            batch = fetch()
        except Exception as e:  # noqa: BLE001 - one bad source must not sink the others
            errors.append(f"{name}: {type(e).__name__}: {str(e)[:200]}")
            continue
        collected.extend(batch.postings)
        skipped += batch.skipped
    if len(errors) == len(fetchers) and not collected:
        raise AllSourcesFailed("; ".join(errors))

    seen = store.seen_keys()
    batch_keys: set[str] = set()
    fresh: list[Posting] = []
    for p in collected:
        if is_excluded(p.title):
            continue
        roles = p.roles or role_of(p.title)
        if not roles:
            continue
        if p.key in seen or p.key in batch_keys:
            continue
        batch_keys.add(p.key)
        fresh.append(p if p.roles else p.model_copy(update={"roles": roles}))

    report, overflow = fresh[:max_report], fresh[max_report:]
    if not dry_run:
        # Only what gets reported is marked seen; overflow is queued for the next run.
        store.mark_seen([p.key for p in report], today)
        store.set_pending([p.model_dump() for p in overflow])
        store.prune(today, SEEN_TTL_DAYS)
        store.mark_initialised(now)
    return IngestResult(first_run=first_run, new=report, overflow=len(overflow), skipped=skipped, errors=errors)

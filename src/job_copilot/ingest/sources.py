"""Source fetchers: Ocean of Jobs (incl. configured company passes) and TechMap CSVs.

Each fetcher takes an httpx.Client so tests can inject a MockTransport; no test touches the network.
Responses are streamed with a byte cap and an overall deadline. A row that fails validation is
skipped and counted, never fatal; a malformed payload fails only its own source.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import dataclass, field

import httpx
from pydantic import ValidationError

from .filters import role_of, wide_net
from .models import MAX_TITLE, Posting, sanitize

log = logging.getLogger(__name__)

OCEAN = "https://oceanofjobs.com/api/jobs"
TECHMAP = "https://raw.githubusercontent.com/mluggy/techmap/main/jobs/{cat}.csv"
COMPANY_SOURCE = "company-pass"
TECHMAP_CATEGORIES = (
    "business",
    "sales",
    "marketing",
    "product",
    "software",
    "procurement-operations",
    "project-management",
)
SOURCE_HOSTS = frozenset({"oceanofjobs.com", "raw.githubusercontent.com"})
USER_AGENT = "job-copilot/0.1 (+https://github.com/yanirfigo/langgraph-agent-showcase)"
OCEAN_MAX_BYTES = 10_000_000
TECHMAP_MAX_BYTES = 5_000_000
DEADLINE_S = 30.0


class FetchError(Exception):
    """A source response was too large, too slow, off-host, or malformed."""


@dataclass
class Batch:
    postings: list[Posting] = field(default_factory=list)
    skipped: int = 0


def _same_host_redirects(response: httpx.Response) -> None:
    if response.is_redirect:
        target = response.next_request.url.host if response.next_request else None
        if target not in SOURCE_HOSTS:
            raise FetchError(f"redirect to non-source host {target!r}")


def make_client(timeout_s: float = 15.0) -> httpx.Client:
    return httpx.Client(
        timeout=timeout_s,
        headers={"user-agent": USER_AGENT},
        follow_redirects=True,
        max_redirects=3,
        trust_env=False,  # ignore proxy settings from the environment
        event_hooks={"response": [_same_host_redirects]},
    )


def fetch_bytes(client: httpx.Client, url: str, *, params=None, max_bytes: int, deadline_s=DEADLINE_S) -> bytes:
    """GET with a hard byte cap and an overall deadline (httpx timeouts are per read)."""
    start = time.monotonic()
    with client.stream("GET", url, params=params) as r:
        r.raise_for_status()
        declared = r.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise FetchError(f"response too large: {declared} bytes")
        buf = bytearray()
        for chunk in r.iter_bytes():
            buf += chunk
            if len(buf) > max_bytes:
                raise FetchError(f"response exceeded {max_bytes} bytes")
            if time.monotonic() - start > deadline_s:
                raise FetchError(f"response exceeded {deadline_s:.0f} s")
    return bytes(buf)


def _jobs(payload: bytes) -> list[dict]:
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise FetchError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("jobs", []), list):
        raise FetchError("unexpected payload shape")
    return [x for x in data.get("jobs") or [] if isinstance(x, dict)]


def _salary(x: dict) -> str | None:
    if not x.get("salary_text"):
        return None
    return f"{x['salary_text']}{' (est.)' if x.get('salary_is_estimate') else ''}"


def _posted(x: dict) -> str | None:
    return str(x.get("posted_at") or x.get("first_seen") or "")[:10] or None


def _add(batch: Batch, make) -> None:
    try:
        batch.postings.append(make())
    except (ValidationError, ValueError, TypeError) as e:
        batch.skipped += 1
        log.debug("skipped invalid row: %s", e)


def ocean(client: httpx.Client, days: int) -> Batch:
    raw = fetch_bytes(
        client,
        OCEAN,
        params={"israel_only": 1, "max_age_days": days, "limit": 500},
        max_bytes=OCEAN_MAX_BYTES,
    )
    batch = Batch()
    for x in _jobs(raw):
        _add(
            batch,
            lambda x=x: Posting.from_raw(
                source="oceanofjobs",
                title=x.get("title"),
                company=x.get("company_name") or x.get("company_domain"),
                city=x.get("location"),
                url=x.get("url"),
                posted=_posted(x),
                salary=_salary(x),
                seniority=x.get("seniority"),
            ),
        )
    return batch


def company_pass(client: httpx.Client, domain: str, name: str) -> Batch:
    """Every posting of one company (Ocean of Jobs domain filter), kept when it matches a target
    role or the wider similar-roles net. Wider-net matches are tagged "wide-net"."""
    raw = fetch_bytes(
        client,
        OCEAN,
        params={"company": domain, "israel_only": 1, "limit": 500},
        max_bytes=OCEAN_MAX_BYTES,
    )
    batch = Batch()
    for x in _jobs(raw):
        title = sanitize(x.get("title"), MAX_TITLE)
        roles = role_of(title)
        if not title or not (roles or wide_net(title)):
            continue
        _add(
            batch,
            lambda x=x, title=title, roles=roles: Posting.from_raw(
                source=COMPANY_SOURCE,
                title=title,
                company=name,
                city=x.get("location"),
                url=x.get("url"),
                posted=_posted(x),
                salary=_salary(x),
                roles=roles or ("wide-net",),
            ),
        )
    return batch


def techmap(client: httpx.Client) -> Batch:
    """Only target-role rows are returned (the caller caches them)."""
    batch = Batch()
    for cat in TECHMAP_CATEGORIES:
        raw = fetch_bytes(client, TECHMAP.format(cat=cat), max_bytes=TECHMAP_MAX_BYTES)
        try:
            text = raw.decode("utf-8-sig")
            rows = list(csv.DictReader(io.StringIO(text)))
        except (UnicodeDecodeError, csv.Error) as e:
            raise FetchError(f"techmap {cat}: invalid CSV: {e}") from e
        for row in rows:
            title = sanitize(row.get("title"), MAX_TITLE)
            if not title or not role_of(title):
                continue
            _add(
                batch,
                lambda row=row, title=title: Posting.from_raw(
                    source="techmap",
                    title=title,
                    company=row.get("company"),
                    city=row.get("city"),
                    url=row.get("url"),
                    posted=row.get("updated"),
                    seniority=row.get("level"),
                ),
            )
    return batch

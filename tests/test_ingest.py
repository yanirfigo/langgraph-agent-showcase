import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from job_copilot.config import CompanyPass
from job_copilot.ingest import AllSourcesFailed, default_fetchers, run_ingest, sources
from job_copilot.ingest.filters import is_excluded
from job_copilot.ingest.models import Posting, clean_url
from job_copilot.store import Store

TODAY = date(2026, 10, 16)
NOW = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)

OCEAN_JOBS = [
    {
        "title": "GTM Engineer",
        "company_name": "Acme",
        "location": "Jerusalem",
        "url": "https://acme.example/jobs/1?utm_source=x&id=7",
        "posted_at": "2026-10-15T08:00:00Z",
    },
    {"title": "Backend Engineer", "company_name": "Acme", "url": "https://acme.example/jobs/2"},
    {"title": "Salesforce Intern", "company_name": "Beta", "url": "https://beta.example/j"},
    {"title": "Salesforce Developer", "company_name": "Gamma", "url": "http://gamma.example/plain"},
    {
        "title": "Business Systems Lead",
        "company_name": "Delta",
        "url": "https://delta.example/j",
        "salary_text": "40k",
        "salary_is_estimate": True,
    },
    {"title": "Business Systems Lead", "company_name": "Delta", "url": "https://delta.example/dup"},
    {"title": "NetSuite Admin", "url": "https://nocompany.example/j"},  # no company: kept by design
    {"title": "CRM Manager", "company_name": "Zeta", "url": "/relative/only"},  # invalid url: skipped
    {"title": "RevOps Lead", "company_name": "Old", "url": "https://old.example/j", "_age": 5},
    # Also in the company pass: the pass runs first, so its copy wins the dedupe.
    {"title": "IT Business Applications Manager", "company_name": "Example Corp", "url": "https://x.example/j"},
]
PASSES = (CompanyPass(domain="example-corp.com", name="Example Corp"),)
COMPANY_JOBS = [
    {"title": "IT Business Applications Manager", "url": "https://example-corp.example/jobs/1"},
    {"title": "ASIC Verification Engineer", "url": "https://example-corp.example/jobs/2"},
    {"title": "Business Systems Lead", "url": "https://example-corp.example/jobs/3"},
    {"title": "Solutions Architect, Enterprise", "url": "https://example-corp.example/jobs/4"},  # wide net only
]
CSV_HEAD = "title,company,city,url,updated,level\n"
TECHMAP_CSV = (
    "﻿" + CSV_HEAD + "RevOps Manager,Epsilon,Beersheba,https://epsilon.example/j,2026-10-14,Senior\n"
    "Accountant,Epsilon,Beersheba,https://epsilon.example/a,2026-10-14,\n"
)


def transport(fail=frozenset(), override=None):
    """Mock sources. `override` maps 'ocean'/'company'/'techmap' to a raw httpx.Response."""
    override = override or {}

    def handler(request: httpx.Request) -> httpx.Response:
        host, params = request.url.host, dict(request.url.params)
        if host == "oceanofjobs.com":
            name = "company" if params.get("company") == "example-corp.com" else "ocean"
        elif host == "raw.githubusercontent.com":
            name = "techmap"
        else:
            return httpx.Response(404)
        if name in fail:
            return httpx.Response(503)
        if name in override:
            return override[name]
        if name == "company":
            return httpx.Response(200, json={"jobs": COMPANY_JOBS})
        if name == "ocean":  # respect the age window like the real API
            days = int(params.get("max_age_days", 999))
            return httpx.Response(200, json={"jobs": [j for j in OCEAN_JOBS if j.get("_age", 0) <= days]})
        body = TECHMAP_CSV if request.url.path.endswith("/business.csv") else CSV_HEAD
        return httpx.Response(200, text=body)

    return httpx.MockTransport(handler)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.sqlite")
    yield s
    s.close()


def ingest(store, *, fail=frozenset(), override=None, max_report=40, dry_run=False, client=None):
    client = client or httpx.Client(transport=transport(fail, override))
    first = not store.initialised()
    return run_ingest(
        store,
        default_fetchers(client, store, first, NOW, PASSES),
        first_run=first,
        today=TODAY,
        max_report=max_report,
        now=NOW,
        dry_run=dry_run,
    )


def keys(result):
    return {(p.source, p.company, p.title) for p in result.new}


def test_filters_dedupes_and_tags(store):
    r = ingest(store)
    assert {(p.source, p.company, p.title, p.roles) for p in r.new} == {
        ("company-pass", "Example Corp", "IT Business Applications Manager", ("bizapps",)),
        ("company-pass", "Example Corp", "Business Systems Lead", ("bizapps",)),
        ("company-pass", "Example Corp", "Solutions Architect, Enterprise", ("wide-net",)),
        ("oceanofjobs", "Acme", "GTM Engineer", ("gtm",)),
        ("oceanofjobs", "Gamma", "Salesforce Developer", ("bizapps",)),
        ("oceanofjobs", "Delta", "Business Systems Lead", ("bizapps",)),
        ("oceanofjobs", "", "NetSuite Admin", ("bizapps",)),
        ("oceanofjobs", "Old", "RevOps Lead", ("gtm",)),
        ("techmap", "Epsilon", "RevOps Manager", ("gtm",)),
    }
    assert r.first_run and r.errors == [] and r.overflow == 0
    assert r.skipped == 1  # the relative-url row is counted, not silently lost


def test_company_passes_run_first_and_default_to_none(store):
    client = httpx.Client(transport=transport())
    assert list(default_fetchers(client, store, True, NOW, ())) == ["oceanofjobs", "techmap"]
    two = (*PASSES, CompanyPass(domain="other.example", name="Other"))
    assert list(default_fetchers(client, store, True, NOW, two)) == [
        "company:example-corp.com",
        "company:other.example",
        "oceanofjobs",
        "techmap",
    ]


def test_company_pass_requests_its_domain():
    seen = []

    def handler(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200, json={"jobs": COMPANY_JOBS})

    batch = sources.company_pass(httpx.Client(transport=httpx.MockTransport(handler)), "example-corp.com", "EC")
    assert seen == [{"company": "example-corp.com", "israel_only": "1", "limit": "500"}]
    assert {p.title for p in batch.postings} == {
        "IT Business Applications Manager",
        "Business Systems Lead",
        "Solutions Architect, Enterprise",
    }
    assert {p.company for p in batch.postings} == {"EC"}


def test_tracking_params_stripped_http_kept_as_link(store):
    urls = {p.url for p in ingest(store).new}
    assert "https://acme.example/jobs/1?id=7" in urls
    assert "http://gamma.example/plain" in urls


def test_second_run_reports_nothing_new(store):
    ingest(store)
    r = ingest(store)
    assert r.new == [] and not r.first_run


def test_first_run_ends_even_when_nothing_was_reported(store):
    ingest(
        store,
        override={
            "ocean": httpx.Response(200, json={"jobs": []}),
            "company": httpx.Response(200, json={"jobs": []}),
            "techmap": httpx.Response(200, text=CSV_HEAD),
        },
    )
    assert store.initialised()
    assert ingest(store).first_run is False


def test_dry_run_marks_nothing(store):
    ingest(store, dry_run=True)
    assert not store.initialised() and store.seen_keys() == set() and store.pending() == []


def test_overflow_survives_the_window_closing(store):
    r1 = ingest(store, max_report=2)  # first run: 7-day window includes the 5-day-old "RevOps Lead"
    assert len(r1.new) == 2 and r1.overflow == 7
    r2 = ingest(store, max_report=40)  # later run: 2-day window no longer returns it
    assert ("oceanofjobs", "Old", "RevOps Lead") in keys(r2)
    assert len(r1.new) + len(r2.new) == 9 and store.pending() == []


def test_partial_failure_is_reported(store):
    r = ingest(store, fail={"techmap"})
    assert any(e.startswith("techmap:") for e in r.errors) and r.new


@pytest.mark.parametrize(
    "bad",
    [
        httpx.Response(200, json=[1, 2, 3]),  # array instead of object
        httpx.Response(200, json={"jobs": [None, 7, "x"]}),  # non-dict items are ignored, not fatal
        httpx.Response(200, text="not json"),
    ],
)
def test_malformed_payload_fails_only_its_source(store, bad):
    r = ingest(store, override={"company": bad})
    assert ("oceanofjobs", "Acme", "GTM Engineer") in keys(r)


def test_oversized_response_is_refused(store, monkeypatch):
    monkeypatch.setattr(sources, "OCEAN_MAX_BYTES", 100)
    r = ingest(store)
    assert any("exceeded" in e or "too large" in e for e in r.errors)
    assert ("techmap", "Epsilon", "RevOps Manager") in keys(r)


def test_redirect_to_other_host_is_refused(store):
    def handler(request):
        if request.url.host == "oceanofjobs.com":
            return httpx.Response(302, headers={"location": "https://evil.example/jobs"})
        if request.url.host == "evil.example":
            return httpx.Response(200, json={"jobs": [OCEAN_JOBS[0]]})
        return httpx.Response(200, text=TECHMAP_CSV)

    client = sources.make_client()
    client._transport = httpx.MockTransport(handler)
    r = ingest(store, client=client)
    assert any("non-source host" in e for e in r.errors)
    assert not any(p.source in ("oceanofjobs", "company-pass") for p in r.new)


def test_all_sources_down_raises(store):
    with pytest.raises(AllSourcesFailed):
        ingest(store, fail={"ocean", "company", "techmap"})


def test_seen_entries_expire(store):
    store.mark_seen(["acme|gtm engineer"], date(2026, 6, 1))
    store.prune(TODAY, 90)
    assert "acme|gtm engineer" not in store.seen_keys()


def test_techmap_is_cached(store):
    ingest(store, dry_run=True)
    r = ingest(store, fail={"techmap"}, dry_run=True)  # served from cache, so no techmap error
    assert not any(e.startswith("techmap") for e in r.errors)
    assert any(p.source == "techmap" for p in r.new)


def test_poisoned_techmap_cache_is_refetched(store):
    store.cache_put("techmap", [{"title": "x", "url": "not a url", "source": "techmap"}], NOW)
    r = ingest(store, dry_run=True)
    assert ("techmap", "Epsilon", "RevOps Manager") in keys(r)


def test_cache_roundtrip_is_json(store):
    ingest(store, dry_run=True)
    raw = store.db.execute("SELECT value FROM cache WHERE name='techmap'").fetchone()[0]
    assert json.loads(raw)[0]["title"] == "RevOps Manager"


# --- untrusted field handling -------------------------------------------------------------------
def test_untrusted_fields_are_bounded_and_cleaned():
    p = Posting.from_raw(
        source="x",
        title="A\x1b[31m‮​\x7f\x9b" + "t" * 1000,
        company="C\x00o\nmpany",
        url="https://a.example/",
    )
    assert len(p.title) == 300
    assert not any(c in p.title for c in "\x1b‮​\x7f\x9b")
    assert p.company == "Co mpany"


def test_text_keeps_newlines():
    p = Posting.from_raw(source="x", title="t", url="https://a.example/", text="line1\nline2 ")
    assert p.text == "line1\nline2"


@pytest.mark.parametrize(
    "url",
    ["https://trusted.example@evil.example/j", "https://user:pw@a.example/", "ftp://a.example/", "/relative"],
)
def test_bad_urls_rejected(url):
    with pytest.raises(ValueError):
        Posting.from_raw(source="x", title="t", url=url)


def test_url_too_long_after_cleaning_is_rejected():
    with pytest.raises(ValueError):
        # 600 chars raw, but each "é" becomes "%C3%A9" when the query is re-encoded
        Posting.from_raw(source="x", title="t", url="https://a.example/?q=" + "é" * 600)


def test_long_url_is_rejected_not_truncated():
    with pytest.raises(ValueError):
        Posting.from_raw(source="x", title="t", url="https://a.example/" + "a" * 3000)


def test_clean_url_keeps_other_params():
    assert clean_url("https://a.example/p?utm_medium=m&x=1#f") == "https://a.example/p?x=1#f"


def test_hebrew_prefix_does_not_hide_excluded_acronym():
    assert is_excluded("דרוש/ה לתפקיד הSDR")  # JS-style ASCII word boundary


def test_pending_rows_are_validated(store):
    store.set_pending([{"bogus": True}])
    r = ingest(store, dry_run=True)
    assert r.new  # the invalid pending row is dropped, the run still works


def test_store_now_param_marks_time(store):
    ingest(store)
    at = store.db.execute("SELECT value FROM meta WHERE name='initialised'").fetchone()[0]
    assert datetime.fromisoformat(at) == NOW
    assert NOW - datetime.fromisoformat(at) < timedelta(seconds=1)

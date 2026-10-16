"""A run that fails after ingest marked postings seen must not lose them: they come back next run."""

import contextlib
from datetime import UTC, date, datetime

import pytest

from job_copilot import run
from job_copilot.config import load_config
from job_copilot.graph import Screened
from job_copilot.ingest.models import Posting
from job_copilot.ingest.sources import Batch
from job_copilot.secrets import Secrets
from job_copilot.store import Store
from job_copilot.subagents.screener import FitScore
from job_copilot.telegram import TelegramError

NOW = datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
TODAY = date(2026, 1, 5)
SECRETS = Secrets(anthropic_api_key="x")  # Telegram is faked below
POSTINGS = [
    Posting.from_raw(source="test", title="Business Applications Manager", company=c, url=f"https://{c}.example/j")
    for c in ("acme", "beta", "gamma")
]
ROUTES = {"acme": ("research", 80), "beta": ("digest", 60), "gamma": ("drop", 10)}


def cfg(max_items=1):
    c = load_config()
    return c.model_copy(update={"limits": c.limits.model_copy(update={"max_digest_items": max_items})})


class FakeGraph:
    def __init__(self, fail=None):
        self.fail, self.seen = fail, []

    def invoke(self, state, config):
        self.seen.append([p.key for p in state["new"]])
        if self.fail:
            raise self.fail
        final = []
        for p in state["new"]:
            route, score = ROUTES[p.company]
            final.append(Screened(posting=p, route=route, fit=FitScore(score=score, reason="r")))
        return {"final": final, "briefs": []}


class FakeTelegram:
    def __init__(self, fail_on=None, exc=TelegramError):
        self.fail_on, self.exc, self.sent = fail_on, exc, []

    def send(self, text, buttons=None):
        if self.fail_on and self.fail_on in text:
            raise self.exc("send failed")
        self.sent.append(text)
        return len(self.sent)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "state.sqlite")
    yield s
    s.close()


@pytest.fixture
def wire(monkeypatch):
    """Replace the model, network and Telegram pieces of run._run; return a setter for one run."""
    state = {}

    def setup(graph, tg, offer_fail=None):
        state.update(graph=graph, tg=tg, offered=[])

        def offer(app, store, tg, text, posting, brief, now):
            if offer_fail and posting.company == offer_fail[0]:
                raise offer_fail[1]
            state["offered"].append(posting.key)

        monkeypatch.setattr(run, "offer", offer)
        return state

    monkeypatch.setattr(run, "load_profile", lambda: "profile")
    monkeypatch.setattr(run, "build_screener", lambda *a: None)
    monkeypatch.setattr(run, "build_researcher", lambda *a: (None, None))
    monkeypatch.setattr(run, "build_graph", lambda *a: state["graph"])
    monkeypatch.setattr(run, "_delivery", lambda *a: (state["tg"], None))
    monkeypatch.setattr(run, "make_client", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(run, "default_fetchers", lambda *a: {"test": lambda: Batch(postings=list(POSTINGS))})
    return setup


def go(store, c=None):
    c = c or cfg()
    return run._run(c, SECRETS, store, cap=40, now=NOW, today=TODAY, dry_run=False)


KEYS = {p.key for p in POSTINGS}


def key(company):
    return next(p.key for p in POSTINGS if p.company == company)


def test_screening_crash_requeues_everything_and_next_run_sees_it(store, wire):
    wire(FakeGraph(fail=RuntimeError("model down")), FakeTelegram())
    assert go(store) == 1
    assert not (KEYS & store.seen_keys())
    assert {p["company"] for p in store.pending()} == {"acme", "beta", "gamma"}

    graph = FakeGraph()
    state = wire(graph, FakeTelegram())
    assert go(store) == 0
    assert set(graph.seen[0]) == KEYS  # seen again on the next run
    assert state["offered"] == [key("acme")]


def test_unexpected_telegram_error_requeues_undelivered(store, wire):
    wire(FakeGraph(), FakeTelegram(fail_on="Job Copilot:", exc=RuntimeError))  # the summary message
    assert go(store) == 1
    # gamma was screened out (dropped): it is done and stays seen; acme and beta were never delivered.
    assert store.seen_keys() & KEYS == {key("gamma")}
    assert {p["company"] for p in store.pending()} == {"acme", "beta"}


def test_crash_mid_offers_keeps_delivered_and_requeues_the_rest(store, wire):
    state = wire(FakeGraph(), FakeTelegram(), offer_fail=("beta", ValueError("boom")))
    assert go(store, cfg(max_items=2)) == 1
    assert state["offered"] == [key("acme")]
    assert store.seen_keys() & KEYS == {key("acme"), key("gamma")}
    assert [p["company"] for p in store.pending()] == ["beta"]


def test_failed_overflow_list_is_requeued(store, wire):
    tg = FakeTelegram(fail_on="Also scored")
    state = wire(FakeGraph(), tg)
    assert go(store) == 0
    assert state["offered"] == [key("acme")]
    assert key("beta") not in store.seen_keys()
    assert [p["company"] for p in store.pending()] == ["beta"]


def test_sent_overflow_list_is_not_requeued(store, wire):
    wire(FakeGraph(), FakeTelegram())
    assert go(store) == 0
    assert KEYS <= store.seen_keys() and store.pending() == []


def test_missing_profile_fails_before_anything_is_marked_seen(store, wire, monkeypatch):
    wire(FakeGraph(), FakeTelegram())

    def missing():
        raise FileNotFoundError("profile")

    monkeypatch.setattr(run, "load_profile", missing)
    with pytest.raises(FileNotFoundError):
        go(store)
    assert store.seen_keys() == set() and store.pending() == [] and not store.initialised()

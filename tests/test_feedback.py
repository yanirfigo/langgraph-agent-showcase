import sys
from datetime import UTC, datetime
from pathlib import Path

from job_copilot.feedback import FEEDBACK_KEY, record_feedback
from job_copilot.store import Store

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "evals"))
from promote_feedback import LABEL, disagreement  # noqa: E402

NOW = datetime(2026, 10, 17, 9, 0, tzinfo=UTC)


class FakeClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    def create_feedback(self, run_id, **kw):
        if self.fail:
            raise RuntimeError("network")
        self.calls.append((run_id, kw))


def test_feedback_goes_to_the_screening_run(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    store.link_run("acme|lead", "run-123", "research", 88, {"title": "Lead"}, NOW)
    c = FakeClient()
    assert record_feedback(c, store, "acme|lead", "dismiss")
    assert record_feedback(c, store, "acme|lead", "applied")
    assert c.calls == [
        ("run-123", {"key": FEEDBACK_KEY, "score": 0, "value": "dismiss"}),
        ("run-123", {"key": FEEDBACK_KEY, "score": 1, "value": "applied"}),
    ]
    assert not record_feedback(c, store, "acme|lead", "discard")  # says nothing about the posting
    assert not record_feedback(c, store, "unknown|key", "save")
    assert not record_feedback(None, store, "acme|lead", "save")  # no LangSmith key: silently off
    assert not record_feedback(FakeClient(fail=True), store, "acme|lead", "save")  # never raises
    store.close()


def test_unpromoted_feedback_joins_tracker_and_runs(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    store.link_run("a|x", "r1", "research", 80, {"title": "X"}, NOW)
    store.link_run("b|y", "r2", "digest", 60, {"title": "Y"}, NOW)
    store.set_status("a|x", "X", "a", "https://a.example", "dismissed", NOW)
    rows = store.unpromoted_feedback()
    assert [(r["key"], r["status"], r["route"]) for r in rows] == [("a|x", "dismissed", "research")]
    store.mark_promoted(["a|x"])
    assert store.unpromoted_feedback() == []
    store.close()


def test_promotion_labels_and_disagreements():
    assert LABEL["dismissed"] == "no" and LABEL["drafted"] == "fit"
    assert disagreement("no", "research") and disagreement("fit", "drop")
    assert disagreement("fit", "research") is None and disagreement("no", "digest") is None

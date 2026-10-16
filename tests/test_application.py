import json
import logging
import sqlite3
from datetime import UTC, datetime

import httpx
import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langgraph.checkpoint.sqlite import SqliteSaver

from job_copilot.application import build_app_graph, offer, thread_id_for
from job_copilot.bot import handle_update
from job_copilot.config import load_config
from job_copilot.digest import item_text, model_text, more_text, select_digest, summary_text
from job_copilot.graph import Screened
from job_copilot.ingest.models import Posting
from job_copilot.logsafe import RedactFilter
from job_copilot.secrets import Secrets
from job_copilot.store import Store
from job_copilot.subagents.draft import Draft, DraftGroundingError, SelfCheck, build_drafter, draft_text, self_check
from job_copilot.subagents.research import CheckedBrief, Claim, CompanyBrief
from job_copilot.subagents.screener import FitScore
from job_copilot.telegram import Telegram, TelegramError

OWNER = "111111111"  # fictional Telegram chat id
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
CFG = load_config()


class Recorder:
    """A fake Telegram server. `fail` maps a method name to how many upcoming calls should fail."""

    def __init__(self):
        self.calls, self.next_id, self.fail, self.rate_limit = [], 100, {}, 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content or b"{}")
        self.calls.append((method, payload))
        if self.rate_limit:
            self.rate_limit -= 1
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "error_code": 429,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 1},
                },
            )
        if self.fail.get(method):
            self.fail[method] -= 1
            return httpx.Response(400, json={"ok": False, "description": "Bad Request: simulated"})
        if method == "sendMessage":
            self.next_id += 1
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self.next_id}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def of(self, method):
        return [p for m, p in self.calls if m == method]


@pytest.fixture
def env(tmp_path):
    rec = Recorder()
    sleeps = []
    tg = Telegram("123:abc", OWNER, client=httpx.Client(transport=httpx.MockTransport(rec)), sleep=sleeps.append)
    store = Store(tmp_path / "state.sqlite")
    ckpt = tmp_path / "checkpoints.sqlite"
    drafts = []

    def fake_draft(p, brief):
        drafts.append(p.title)
        if p.title == "Broken":
            raise RuntimeError("model down")
        return Draft(
            evidence=["q1", "q2"],
            language="en",
            cover_note="Hi, see https://evil.example and mail x@evil.example",
            cv_bullets=["b"],
        )

    def make_app(draft_fn=fake_draft):
        saver = SqliteSaver(sqlite3.connect(ckpt, check_same_thread=False))
        return build_app_graph(store, tg, draft_fn, saver, now=lambda: NOW)

    yield {
        "rec": rec,
        "tg": tg,
        "store": store,
        "make_app": make_app,
        "drafts": drafts,
        "ckpt": ckpt,
        "sleeps": sleeps,
        "tmp": tmp_path,
    }
    store.close()


def posting(title="Widget Engineer"):
    return Posting.from_raw(source="t", title=title, company="Acme", city="Central", url="https://acme.example/j")


def press(env, app, data, *, chat=OWNER, user=OWNER):
    update = {
        "update_id": 1,
        "callback_query": {
            "id": "cb",
            "data": data,
            "from": {"id": int(user)},
            "message": {"chat": {"id": int(chat), "type": "private"}},
        },
    }
    return handle_update(update, tg=env["tg"], store=env["store"], app=app)


def tokens(env):
    """Button callback data from the most recent message (or markup edit) that had buttons."""
    for _method, p in reversed(env["rec"].calls):
        rows = (p.get("reply_markup") or {}).get("inline_keyboard")
        if rows:
            return {b["text"]: b["callback_data"] for b in rows[0]}
    return {}


# --- happy paths ---------------------------------------------------------------------------------
def test_full_flow_draft_then_applied_survives_restart(env):
    app = env["make_app"]()
    p = posting()
    assert offer(app, env["store"], env["tg"], "item", p, None, NOW) == thread_id_for(p.key)
    first = tokens(env)
    assert set(first) == {"✍ Draft", "★ Save", "✕ Not for me"}

    app = env["make_app"]()  # "restart": a new graph object on the same checkpoint file
    assert press(env, app, first["✍ Draft"]) == "draft"
    draft_msg = env["rec"].of("sendMessage")[-1]["text"]
    assert "evil.example" not in draft_msg and "[link removed]" in draft_msg and "[email removed]" in draft_msg
    after = tokens(env)
    assert set(after) == {"✓ I applied", "↺ Discard"}

    app = env["make_app"]()
    assert press(env, app, after["✓ I applied"]) == "applied"
    assert env["store"].status_counts() == {"applied": 1}
    assert press(env, app, after["✓ I applied"]) == "stale"
    assert press(env, app, first["✍ Draft"]) == "stale"


def test_save_then_dismiss(env):
    app = env["make_app"]()
    offer(app, env["store"], env["tg"], "item", posting(), None, NOW)
    assert press(env, app, tokens(env)["★ Save"]) == "save"
    assert env["store"].status_counts() == {"saved": 1}
    after_save = tokens(env)
    assert set(after_save) == {"✍ Draft", "✕ Not for me"}
    assert press(env, app, after_save["✕ Not for me"]) == "dismiss"
    assert env["store"].status_counts() == {"dismissed": 1}
    assert env["rec"].of("editMessageReplyMarkup")[-1]["reply_markup"] == {"inline_keyboard": []}


def test_discard_returns_to_first_choice(env):
    app = env["make_app"]()
    offer(app, env["store"], env["tg"], "item", posting(), None, NOW)
    first = tokens(env)
    press(env, app, first["✍ Draft"])
    press(env, app, tokens(env)["↺ Discard"])
    assert press(env, app, first["✍ Draft"]) == "draft"
    assert env["drafts"] == ["Widget Engineer"] * 2


# --- failure modes (from a review) ---------------------------------------------------------------
def test_draft_failure_keeps_thread_usable(env):
    app = env["make_app"]()
    offer(app, env["store"], env["tg"], "item", posting("Broken"), None, NOW)
    first = tokens(env)
    assert press(env, app, first["✍ Draft"]) == "draft"
    assert "Couldn't draft" in env["rec"].of("sendMessage")[-1]["text"]
    assert press(env, app, first["★ Save"]) == "save"


def test_telegram_failures_inside_steps_never_strand_the_thread(env):
    app = env["make_app"]()
    offer(app, env["store"], env["tg"], "item", posting(), None, NOW)
    first = tokens(env)
    env["rec"].fail["editMessageReplyMarkup"] = 5
    assert press(env, app, first["★ Save"]) == "save"  # button edit fails, step still completes
    env["rec"].fail["sendMessage"] = 5
    assert press(env, app, first["✍ Draft"]) == "draft"  # draft can't be delivered: back to first choice
    env["rec"].fail = {}
    assert press(env, app, first["✍ Draft"]) == "draft"
    assert set(tokens(env)) == {"✓ I applied", "↺ Discard"}


def test_a_step_cut_off_mid_way_is_finished_on_the_next_press(env):
    app = env["make_app"]()
    offer(app, env["store"], env["tg"], "item", posting(), None, NOW)
    first = tokens(env)
    real = env["store"].set_status
    env["store"].set_status = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("crash mid-step"))
    with pytest.raises(RuntimeError):
        press(env, app, first["★ Save"])
    env["store"].set_status = real
    config = {"configurable": {"thread_id": thread_id_for(posting().key)}}
    assert app.get_state(config).next == ("record_saved",) and not app.get_state(config).interrupts
    assert press(env, app, first["✕ Not for me"]) == "dismiss"  # recovery finished record_saved first
    assert env["store"].status_counts() == {"dismissed": 1}


def test_late_callback_answer_does_not_lose_the_press(env):
    app = env["make_app"]()
    offer(app, env["store"], env["tg"], "item", posting(), None, NOW)
    env["rec"].fail["answerCallbackQuery"] = 5  # "query is too old"
    assert press(env, app, tokens(env)["★ Save"]) == "save"
    assert env["store"].status_counts() == {"saved": 1}


def test_rate_limit_is_retried_after_waiting(env):
    env["rec"].rate_limit = 1
    assert env["tg"].send("hi") == 101 and env["sleeps"] == [1]


def test_failed_offer_leaves_no_orphan_thread(env):
    app = env["make_app"]()
    env["rec"].fail["sendMessage"] = 5
    with pytest.raises(TelegramError):
        offer(app, env["store"], env["tg"], "item", posting(), None, NOW)
    config = {"configurable": {"thread_id": thread_id_for(posting().key)}}
    assert not app.get_state(config).values
    env["rec"].fail = {}
    assert offer(app, env["store"], env["tg"], "item", posting(), None, NOW)  # can be offered again later


def test_draft_text_is_never_written_to_the_checkpoint_file(env):
    class FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kw):
            return self

    note = "UNIQUE-DRAFT-TEXT-4417 tailored note"
    model = FakeModel(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "Draft",
                            "id": "t1",
                            "args": {
                                "evidence": ["CV-SECRET-LINE", "Second CV line"],
                                "language": "en",
                                "cover_note": note,
                                "cv_bullets": [],
                                "gaps": [],
                            },
                        }
                    ],
                )
            ]
        )
    )
    cv = env["tmp"] / "cv.md"
    cv.write_text("CV-SECRET-LINE\nSecond   CV line", encoding="utf-8")
    cv.chmod(0o600)
    drafter = build_drafter(
        CFG,
        Secrets(anthropic_api_key="x"),
        cv_path=cv,
        usage_db=env["tmp"] / "u.sqlite",
        model=model,
        checker=FakeChecker(SelfCheck(unsupported=[], revised_cover_note=None, revised_bullets=None)),
    )
    app = env["make_app"](drafter)
    offer(app, env["store"], env["tg"], "item", posting(), None, NOW)
    assert press(env, app, tokens(env)["✍ Draft"]) == "draft"
    assert note.split()[0] in env["rec"].of("sendMessage")[-1]["text"]
    raw = env["ckpt"].read_bytes() + (
        env["ckpt"].with_name(env["ckpt"].name + "-wal").read_bytes()
        if env["ckpt"].with_name(env["ckpt"].name + "-wal").exists()
        else b""
    )
    assert b"UNIQUE-DRAFT-TEXT" not in raw and b"CV-SECRET-LINE" not in raw


# --- access control ------------------------------------------------------------------------------
def test_non_owner_and_forged_buttons_are_refused(env):
    app = env["make_app"]()
    offer(app, env["store"], env["tg"], "item", posting(), None, NOW)
    first = tokens(env)
    assert press(env, app, first["✍ Draft"], chat="999", user="999") == "ignored"
    assert press(env, app, first["✍ Draft"], user="999") == "ignored"
    assert press(env, app, "forgedtoken:draft") == "invalid"
    token = first["✍ Draft"].split(":")[0]
    assert press(env, app, f"{token}:applied") == "stale"
    assert press(env, app, f"{token}:rm -rf") == "invalid"
    assert env["drafts"] == []


def test_offer_is_idempotent(env):
    app = env["make_app"]()
    p = posting()
    offer(app, env["store"], env["tg"], "item", p, None, NOW)
    assert offer(app, env["store"], env["tg"], "item", p, None, NOW) is None
    assert len(env["rec"].of("sendMessage")) == 1


def test_status_command_and_strangers(env):
    msg = {
        "update_id": 2,
        "message": {
            "text": "/status",
            "chat": {"id": int(OWNER), "type": "private"},
            "from": {"id": int(OWNER)},
        },
    }
    assert handle_update(msg, tg=env["tg"], store=env["store"], app=None) == "status"
    stranger = {
        "update_id": 3,
        "message": {"text": "/status", "chat": {"id": 1, "type": "private"}, "from": {"id": 1}},
    }
    assert handle_update(stranger, tg=env["tg"], store=env["store"], app=None) == "ignored"
    group = {
        "update_id": 4,
        "message": {
            "text": "/status",
            "chat": {"id": int(OWNER), "type": "group"},
            "from": {"id": int(OWNER)},
        },
    }
    assert handle_update(group, tg=env["tg"], store=env["store"], app=None) == "ignored"


# --- text safety and digest selection ------------------------------------------------------------
def test_messages_are_plain_text_without_previews(env):
    env["tg"].send("hello *bold* <b>x</b>")
    p = env["rec"].of("sendMessage")[-1]
    assert "parse_mode" not in p and p["link_preview_options"] == {"is_disabled": True}


def test_digest_defangs_everything_but_the_posting_link():
    checked = CheckedBrief(
        brief=CompanyBrief(
            company_summary='Apply at https://evil.example/x or evil.co.uk "call me"',
            business_facing="yes",
            work_mode="hybrid",
        ),
        verified=[Claim(text="Hybrid, see www.evil.example, ping @evilbot", source_url="https://acme.example/j")],
        flags=["visit evil.xyz/now or mail a@evil.example"],
    )
    p = Posting.from_raw(
        source="t",
        title="Admin at free-gift.example",
        company="@scam",
        city="x.co",
        url="https://acme.example/j",
    )
    text = item_text(p, 88, "Strong fit, details at https://x.example", checked)
    for bad in (
        "https://evil",
        "evil.example",
        "evil.co.uk",
        "evil.xyz",
        "@evilbot",
        "free-gift.example",
        "@scam",
        "a@evil",
        "call me",
        "https://x.example",
    ):
        assert bad not in text, bad
    assert text.rstrip().endswith("https://acme.example/j")
    assert model_text("Node.js and SQL", 50) == "Node․js and SQL"  # readable, not a link


class FakeChecker:
    def __init__(self, verdict):
        self.verdict, self.prompts = verdict, []

    def invoke(self, prompt):
        self.prompts.append(prompt)
        return self.verdict


def _draft(**kw):
    base = {
        "evidence": ["Shipped 40 widgets", "Example Certified Pro"],
        "language": "en",
        "cover_note": "I built every widget in the factory.",
        "cv_bullets": ["Built every widget"],
    }
    return Draft(**(base | kw))


def test_self_check_keeps_a_supported_draft():
    d = _draft()
    c = FakeChecker(SelfCheck(unsupported=[], revised_cover_note=None, revised_bullets=None))
    assert self_check(c, d) == d
    assert "<evidence>" in c.prompts[0] and "Shipped 40 widgets" in c.prompts[0]


def test_self_check_applies_the_revision():
    c = FakeChecker(
        SelfCheck(
            unsupported=["built every widget"],
            revised_cover_note="I shipped 40 widgets.",
            revised_bullets=["Shipped 40 widgets"],
        )
    )
    out = self_check(c, _draft())
    assert out.cover_note == "I shipped 40 widgets." and out.cv_bullets == ["Shipped 40 widgets"]
    assert out.evidence == _draft().evidence


def test_self_check_refuses_when_it_cannot_fix():
    c = FakeChecker(SelfCheck(unsupported=["x"], revised_cover_note=None, revised_bullets=None))
    with pytest.raises(DraftGroundingError):
        self_check(c, _draft())


def test_evidence_quotes_must_be_verbatim_in_the_cv():
    from job_copilot.subagents.draft import unverified_quotes

    cv = "Widget Engineer | Example Corp\n● Output: Shipped 40 sample widgets in the demo year."
    assert unverified_quotes(["Shipped 40 sample  widgets", "widget engineer"], cv) == []
    assert unverified_quotes(["Ran the whole widget factory"], cv) == ["Ran the whole widget factory"]


def test_draft_text_backstop():
    d = Draft(
        evidence=["a", "b"],
        language="en",
        cover_note="Email me at me@x.io or see https://x.io",
        cv_bullets=["www.x.io"],
    )
    t = draft_text(posting(), d)
    assert "x.io" not in t and "@" not in t and "Nothing was sent" in t


def _s(title, route, score=None):
    return Screened(posting=posting(title), route=route, fit=FitScore(score=score, reason="r") if score else None)


def test_select_digest_orders_caps_and_sets_aside_deferred():
    final = [
        _s("A", "digest", 70),
        _s("B", "research", 80),
        _s("C", "drop", 10),
        _s("D", "research", 90),
        _s("E", "digest", 60),
        _s("F", "deferred"),
        _s("G", "digest", 65),
    ]
    plan = select_digest(final, 3)
    assert [s.posting.title for s in plan.top] == ["D", "B", "A"]
    assert [s.posting.title for s in plan.more] == ["G", "E"]
    assert [s.posting.title for s in plan.deferred] == ["F"] and plan.dropped == 1
    summary = summary_text(plan, researched=2, errors=["ocean: ReadTimeout"])
    assert "5 to look at" in summary and "1 postings deferred" in summary and "ReadTimeout" in summary
    assert "60 · E" in more_text(plan.more)


def test_requeue_puts_postings_back_and_unmarks_seen(tmp_path):
    store = Store(tmp_path / "s.sqlite")
    p = posting("Lost")
    store.mark_seen([p.key], NOW.date())
    store.set_pending([{"x": 1}])
    store.requeue([(p.key, p.model_dump(mode="json"))])
    assert p.key not in store.seen_keys()
    assert store.pending()[0]["title"] == "Lost" and store.pending()[1] == {"x": 1}
    store.close()


def test_tokens_never_reach_the_logs(caplog):
    rec = logging.LogRecord(
        "httpx",
        logging.WARNING,
        __file__,
        1,
        "POST https://api.telegram.org/bot123456:AAH-secret_x/getUpdates %s",
        ("ok",),
        None,
    )
    RedactFilter().filter(rec)
    assert "AAH-secret" not in rec.getMessage() and "[redacted]" in rec.getMessage()

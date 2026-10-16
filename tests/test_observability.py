import os

from langsmith import traceable
from langsmith.run_helpers import get_tracing_context

from job_copilot.config import load_config
from job_copilot.observability import make_client, tracing
from job_copilot.secrets import Secrets


def test_no_key_means_no_client_and_tracing_off():
    cfg = load_config()
    s = Secrets(anthropic_api_key="sk-test")
    assert make_client(cfg, s) is None
    with tracing(cfg, s) as client:
        assert client is None
        assert get_tracing_context()["enabled"] is False

        @traceable
        def f():
            return 1

        assert f() == 1  # runs normally, nothing sent


def test_client_is_explicit_not_environment():
    cfg = load_config()
    c = make_client(cfg, Secrets(anthropic_api_key="sk-test", langsmith_api_key="lsv2_sk_test"))
    assert c is not None and c.api_url == cfg.langsmith.api_url
    assert "LANGSMITH_API_KEY" not in os.environ and "LANGSMITH_TRACING" not in os.environ

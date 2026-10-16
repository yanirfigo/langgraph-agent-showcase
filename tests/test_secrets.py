import os

import pytest

from job_copilot.secrets import SecretsError, load_secrets

posix_only = pytest.mark.skipif(os.name != "posix", reason="file modes are POSIX-only")


def _secrets_file(tmp_path, body="ANTHROPIC_API_KEY=sk-test\n", mode=0o600):
    p = tmp_path / "secrets.env"
    p.write_text(body, encoding="utf-8")
    p.chmod(mode)
    return p


def test_loads_without_touching_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = load_secrets(_secrets_file(tmp_path))
    assert s.anthropic_api_key == "sk-test"
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_key_is_hidden_from_repr(tmp_path):
    assert "sk-test" not in repr(load_secrets(_secrets_file(tmp_path)))


def test_missing_key_fails(tmp_path):
    with pytest.raises(SecretsError, match="ANTHROPIC_API_KEY"):
        load_secrets(_secrets_file(tmp_path, body="LANGSMITH_API_KEY=x\n"))


def test_missing_file_fails(tmp_path):
    with pytest.raises(SecretsError, match="not found"):
        load_secrets(tmp_path / "nope.env")


@posix_only
def test_group_readable_file_is_refused(tmp_path):
    with pytest.raises(SecretsError, match="chmod 600"):
        load_secrets(_secrets_file(tmp_path, mode=0o640))

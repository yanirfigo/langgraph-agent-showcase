from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from job_copilot.config import DEFAULT_CONFIG, load_config


def test_repo_config_is_valid():
    cfg = load_config(local=None)  # repo defaults only, independent of this machine
    assert cfg.thresholds.research_min >= cfg.thresholds.digest_min
    assert cfg.fetch_allowlist


def _write(tmp_path: Path, mutate) -> Path:
    raw = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    mutate(raw)
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return p


def test_rejects_unknown_keys(tmp_path):
    p = _write(tmp_path, lambda r: r.update(surprise=True))
    with pytest.raises(ValidationError):
        load_config(p, local=None)


def test_rejects_inverted_thresholds(tmp_path):
    p = _write(tmp_path, lambda r: r["thresholds"].update(digest_min=90, research_min=10))
    with pytest.raises(ValidationError):
        load_config(p, local=None)


def test_local_override_is_deep_merged(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text("langsmith:\n  workspace_id: 00000000-0000-0000-0000-000000000000\n", encoding="utf-8")
    cfg = load_config(local=local)
    assert cfg.langsmith.workspace_id == "00000000-0000-0000-0000-000000000000"
    assert cfg.langsmith.project == load_config(local=None).langsmith.project  # siblings kept


def test_repo_default_has_no_company_passes():
    assert load_config(local=None).sources.company_passes == ()


def test_local_override_list_replaces_company_passes(tmp_path):
    local = tmp_path / "local.yaml"
    local.write_text(
        "sources:\n  company_passes:\n    - domain: example-corp.com\n      name: Example Corp\n",
        encoding="utf-8",
    )
    cfg = load_config(local=local)
    assert [(c.domain, c.name) for c in cfg.sources.company_passes] == [("example-corp.com", "Example Corp")]
    assert cfg.limits == load_config(local=None).limits  # other sections untouched


@pytest.mark.parametrize(
    "passes",
    [
        [{"domain": "not a domain", "name": "X"}],
        [{"domain": "example-corp.com", "name": ""}],
        [{"domain": "example-corp.com", "name": "X", "extra": 1}],
        [{"domain": "example-corp.com", "name": "A"}, {"domain": "example-corp.com", "name": "B"}],
    ],
)
def test_rejects_bad_company_passes(tmp_path, passes):
    p = _write(tmp_path, lambda r: r["sources"].update(company_passes=passes))
    with pytest.raises(ValidationError):
        load_config(p, local=None)


def test_rejects_out_of_range_limits(tmp_path):
    p = _write(tmp_path, lambda r: r["limits"].update(max_postings_per_run=100000))
    with pytest.raises(ValidationError):
        load_config(p, local=None)

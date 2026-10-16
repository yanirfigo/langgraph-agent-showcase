from job_copilot import demo
from job_copilot.digest import INJECTION_WARNING


def test_offline_demo_runs_end_to_end(capsys):
    demo.main()
    out = capsys.readouterr().out
    assert "deal-breaker in title: contract / temporary" in out  # dropped by code, no model call
    assert "evil.example" not in out  # defanged before "Telegram"
    assert "Draft for: Business Systems Engineer (HubSpot)" in out  # the HITL thread resumed
    assert "tracker: {'drafted': 1}" in out
    assert "text aimed at an AI" in out  # the planted injection is flagged, not researched


def test_injection_warning_text_is_plain():
    assert "http" not in INJECTION_WARNING

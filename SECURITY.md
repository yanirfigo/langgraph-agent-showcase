# Security

This agent reads untrusted text (job postings, web pages) and holds personal data, so security is
part of the design rather than an add-on. The threat model is in
[docs/THREAT_MODEL.md](docs/THREAT_MODEL.md); the controls and the failures behind them are in
[docs/DECISIONS.md](docs/DECISIONS.md) (D6-D11, D15).

## Reporting a vulnerability

Please open a GitHub security advisory on this repository (Security tab, "Report a vulnerability")
rather than a public issue. Include steps to reproduce. Prompt-injection or SSRF bypasses of the
research tool are especially welcome; a reproducible case will be added to the red-team eval.

If the "Report a vulnerability" button is not available, open an issue asking for a contact,
without any details of the vulnerability.

## Scope notes

- No secret, key or personal data is stored in this repository. Deployments keep them in
  `~/.config/job-copilot/` (directory 700, files 600).
- The red-team suite (`evals/redteam_research.py`) and the guard tests (`tests/test_web_guard.py`)
  are the executable form of the security claims; a change that weakens a guard should fail them.

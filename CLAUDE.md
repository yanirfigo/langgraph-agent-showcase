# Instructions for Claude Code sessions in this repo

A production-style LangGraph agent (see README.md). The design rules below are what make it safe;
treat them as non-negotiable when changing code.

## Non-negotiable design rules

- **Routing is Python.** Conditional edges are plain functions over validated state. Never let a
  model choose the next node, and never use an LLM supervisor or handoff tools.
- **Least privilege per subagent.** The screener and draft subagents have no tools. Research has
  read-only web tools only. Never add a tool that sends, emails, applies, writes files or runs
  shell commands.
- **Model output is data.** Validate it with pydantic `response_format`; never execute it or use
  it as a URL, path or command without a code check.
- **Untrusted text** (postings, web pages, Telegram) is quoted as data in prompts and defanged
  before it reaches Telegram; the red-team eval must pass before any release.
- **Secrets** come from `job_copilot.secrets.load_secrets()` and are passed explicitly. Never set
  `ANTHROPIC_API_KEY` in `os.environ`, a shell profile or a unit file: other tools on the same
  host (e.g. Claude Code on a subscription login) would silently switch to API billing.
- **Limits** (calls, tools, recursion, postings, daily cap) come from `config/config.yaml`.
- **Personal data stays out of git**: secrets, CV, profile, labels and deployment ids live in
  `~/.config/job-copilot/` (mode 600). Never commit keys or personal data.
- **Evals gate changes.** Any prompt, model or threshold change runs `evals/gate.py` first.

## LangSmith

Tracing is off unless code wraps a run in `observability.tracing(cfg, secrets)`; never set
`LANGSMITH_TRACING`/`LANGSMITH_API_KEY` in an environment. Org-scoped service keys (`lsv2_sk_…`)
must name the workspace on every call (403 otherwise): set `langsmith.workspace_id` in the private
`~/.config/job-copilot/config.local.yaml`.

## Workflow

- `uv sync`, `uv run ruff check`, `uv run ruff format --check`, `uv run pytest` before every commit. Tests never hit the network.
- Deploy on the host as the service user: `git pull && bash deploy/install.sh` (runs tests first).
- `uv run python -m job_copilot.demo` runs the whole agent offline with fake models.

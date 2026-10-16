# Threat model

Job Copilot reads untrusted text (job postings, web pages) and holds sensitive personal data (a CV,
private preferences). It runs on a small home server shared with other services.

## Assets

| Asset | Why it matters |
|---|---|
| CV | Personal data; read only when drafting, hidden from LangSmith traces, never logged |
| Profile | Non-identifying by design (no name, contacts, employers or dates); sent to the model and visible in screener traces |
| Anthropic and LangSmith keys | Billing and data access |
| Telegram bot token | Anyone holding it can talk to the bot |
| Other services on the host | Co-hosted services, their data and the LAN must not be reachable through this agent |

## Entry points (all untrusted)

1. Job postings from the source APIs.
2. Web pages fetched during research.
3. Telegram updates (anyone can message a bot).
4. Model output (treated as untrusted data until validated).

## Worst cases and controls

| Worst case | Controls |
|---|---|
| A posting injects instructions that make the agent act | Screener has no tools; routing is Python, not model choice; outputs are pydantic-validated; red-team eval is a release gate |
| The agent contacts someone or applies on the owner's behalf | No send, email, apply, shell or file-write tool exists anywhere in the code |
| SSRF: fetch_page reaches the host's own services or the LAN | https on port 443 only, no userinfo; per-run host allowlist; DNS resolved and every address must be public (private, loopback, link-local and 100.64.0.0/10 refused); the connection is pinned to the checked IP; redirects followed by hand and re-checked per hop; size, content-type and time caps |
| CV leaks into LangSmith traces | CV is read only in the draft node; trace payloads hidden there; PII redaction on outputs |
| Keys leak into other processes on the host | Mode-600 file read into memory; never exported to the environment; own Linux user |
| A stranger drives the bot | Updates from any chat id other than the owner's are dropped before the graph runs |
| Runaway loops or spend | Model and tool call limits per subagent, graph recursion limit, max postings per run, daily call cap, Console workspace spend limit |
| A compromised dependency | Hash-pinned uv.lock, `uv sync --frozen`, deliberate upgrades only after evals pass |
| The agent process is compromised | Runs as a dedicated user: no sudo, no docker, cannot read other users' homes or other services' data; systemd sandboxing |

## Out of scope

Physical access to the host; compromise of Anthropic, LangSmith or Telegram themselves.

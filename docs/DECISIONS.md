# Decision log

Short records of the decisions that shape this agent, and of the failures that forced some of them.
Each entry: the decision, why, and what it prevents.

## Architecture

**D1. The supervisor is code, not a model.** LangGraph `StateGraph` with conditional edges that are
plain Python functions over validated state; `Send` for parallel fan-out. No LLM supervisor, no
handoff tools. *Why*: cheaper, deterministic, testable without a model, and a prompt injection can't
reroute the graph. *Prevents*: an attacker-written posting steering the agent into research or drafting.

**D2. Subagents get the minimum tools each.** Screener, research and draft are `create_agent`
graphs; the self-check is a single structured-output model call. Screener: no tools. Research: two
read-only web tools. Draft and self-check: none. No tool exists that sends, applies, emails, writes
files or runs commands. *Prevents*: any injection from becoming an action.

**D3. Model tiering.** Haiku screens (most calls), Sonnet researches (few), Opus drafts (only on a
button press). *Why*: cost scales with the cheap path; quality is spent where a human will read it.

**D4. Deterministic guards before any model call.** Title deal-breakers (contract, maternity cover)
drop postings in code; ingest is plain Python with tests. *Why*: free, exact, immune to injection.

**D5. A suspected injection goes to the digest with a warning, never to research.** Dropping would
lose real postings on false positives; researching would widen the attack surface.

## Security

**D6. One guarded egress point.** `web_search` runs server-side at Anthropic; the only local network
tool is `fetch_page`: https:443, per-run allowlist, all resolved IPs public (incl. NAT64 decoding),
**connection pinned to the approved IP** with SNI/Host/certificate on the real name, per-hop redirect
checks, no compressed bodies, raw reads with a byte cap, one wall-clock deadline, generic errors.
*Found in review*: the first version re-resolved DNS inside the HTTP client (a rebinding window), let
transport errors reach the model (a port-probe oracle), and decompressed before the size cap.

**D7. Search results allow exact URLs only** (plus a bare homepage). *Found in review*: allowing a
search-result host plus its subdomains gave an attacker a beacon/exfiltration channel.

**D8. Claims are verified in code.** A research claim is "verified" only if its source page was
fetched in this run and the page text supports it; search-only claims are labelled unverified;
claims citing unseen URLs are removed. *Found in review*: "cited" used to mean only "the URL
appeared in search results".

**D9. Untrusted text is defanged before Telegram.** Telegram auto-links URLs, bare domains, emails
and @mentions even without formatting, so these are neutralised in posting fields and all model
text; quoted spans in the model's own commentary are removed. *Found in red-teaming*: the research
model reported an injection by quoting it, which would have delivered attacker text to the owner.

**D10. Secrets never enter the environment; logs redact tokens.** *Failure*: the HTTP client logged
every request URL at INFO, and Telegram puts the bot token in the URL, so the token reached the
system journal. Fixed (HTTP loggers at WARNING plus a redacting filter), verified at zero, and the
token was rotated.

**D11. Process isolation.** A dedicated Linux user with no sudo or docker; systemd units with
`ProtectSystem=strict`, `ProtectHome=read-only` and only the data directory writable,
`NoNewPrivileges`, `RestrictSUIDSGID`, `LockPersonality`; memory caps on every unit and run-time
caps on the oneshot jobs.

## Human in the loop

**D12. One LangGraph thread per offered posting, paused with `interrupt()`, checkpointed in SQLite.**
The bot resumes it with `Command(resume=...)` on a button press, also after restarts.

**D13. Side-effect nodes never contain an interrupt and never raise on Telegram errors.** A thread
cut off mid-node (crash, reboot) is finished on the next press. *Found in review*: a single
Telegram hiccup used to strand a thread forever ("Already handled").

**D14. Checkpoint first, then send.** A button can never point at a thread that doesn't exist; a
failed send deletes the thread and requeues the posting. *Found in review*.

**D15. The draft agent runs with `checkpointer=False`.** *Found in review*: as a subgraph it
inherited the parent's SQLite checkpointer and wrote CV-derived draft text to disk.

**D16. Buttons carry opaque tokens; the action must match what the thread is waiting for.** Only the
owner's private chat is served. Stale or forged buttons cannot move a thread.

## Quality

**D17. Evals are a release gate** (`evals/gate.py`): screener on labelled sample postings, a red team
(19 planted injections + 1 clean control),
and draft grounding. Any prompt, model or threshold change runs it first.

**D18. Drafts are grounded by construction.** The model must first copy 2-6 verbatim CV quotes
(checked in code), write only from them, then a Haiku self-check rewrites any claim the quotes don't
support. *Measured*: prompt wording alone left 1 in 5 drafts with a stretched claim ("administers X"
written as "owns X"); grounding by construction brought it to zero.

**D19. Label conflicts are resolved by a human, not by tuning thresholds.** When a label and the
stated profile disagreed (a deal-breaker term, an adjacent job title, a location outside the target
regions), a human decided which one was right; thresholds stayed put. Tuning thresholds to pass the eval would have overfit it.

**D20. LLM judges get room to think and must justify failures.** *Failure*: a judge returned empty
JSON because its reasoning hit `max_tokens`; another graded the draft's highlight bullets as part of
the letter. Fixed with native JSON-schema output, a larger budget, separated inputs, and "a fail must
name a concrete issue". Tone is advisory until the judge is calibrated against human ratings.

## Operations

**D21. Evals spend from their own daily budget.** *Failure*: a day of eval runs used 468/400 of the
live agent's daily model-call cap, which would have left the next scheduled digest with nothing.

**D22. Undelivered postings go back to the queue.** Postings deferred by the daily cap or whose
Telegram send failed are requeued, also when the run fails after ingest. Postings the screener
dropped stay "seen" on purpose. *Found in review*.

**D23. Shadow mode before cut-over.** A migration pattern: the new agent runs side by side with the
simpler system it replaces, and `tools/shadow_report.py` compares what each one reported; cut-over
requires the new agent to miss nothing the old system found.

**D24. Personal data never in git.** Secrets, CV, profile and deployment ids live in
`~/.config/job-copilot/` (directory 700, files 600); eval pools and labels live in the
gitignored `data/` directory; the repo holds code, defaults and fictional examples only.

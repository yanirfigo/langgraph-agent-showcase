"""One scheduled run: ingest -> screen -> research -> Telegram digest with buttons.

    uv run python -m job_copilot.run [--dry-run] [--limit N]

--dry-run fetches, scores and researches but marks nothing as seen and prints the digest instead
of sending it. --limit caps how many new postings are screened (never above the configured cap).
Postings that were not screened (daily budget) or could not be delivered are put back in the
queue and un-marked as seen, so a bad run never loses them. Everything that can fail before any
model call (profile, delivery setup) happens before ingest marks anything seen, and if the run fails
after ingest, every posting not yet delivered is requeued.
"""

from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from langgraph.checkpoint.sqlite import SqliteSaver

from .application import build_app_graph, offer
from .bot import CHECKPOINTS
from .config import load_config
from .digest import item_text, more_text, select_digest, summary_text
from .graph import build_graph
from .ingest import AllSourcesFailed, default_fetchers, run_ingest
from .ingest.sources import make_client
from .logsafe import setup_process
from .observability import tracing
from .profile import load_profile
from .secrets import load_secrets
from .store import Store
from .subagents.draft import build_drafter
from .subagents.research import build_researcher
from .subagents.screener import build_screener
from .telegram import Telegram, TelegramError

log = logging.getLogger("job_copilot.run")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="job_copilot.run")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args(argv)
    setup_process()

    cfg, secrets = load_config(), load_secrets()
    if not args.dry_run and not (secrets.telegram_bot_token and secrets.telegram_chat_id):
        log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required (nothing was fetched or spent)")
        return 2
    cap = min(args.limit or cfg.limits.max_postings_per_run, cfg.limits.max_postings_per_run)
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo(cfg.timezone)).date()
    store = Store()
    try:
        return _run(cfg, secrets, store, cap=cap, now=now, today=today, dry_run=args.dry_run)
    finally:
        store.close()


def _delivery(cfg, secrets, store: Store):
    """Telegram client and the application graph that offers postings with buttons."""
    tg = Telegram(secrets.telegram_bot_token, secrets.telegram_chat_id)
    saver = SqliteSaver(sqlite3.connect(CHECKPOINTS, check_same_thread=False))
    app = build_app_graph(store, tg, build_drafter(cfg, secrets), saver, now=lambda: datetime.now(UTC))
    return tg, app


def _run(cfg, secrets, store: Store, *, cap: int, now: datetime, today, dry_run: bool) -> int:
    # Everything that can fail without touching a posting runs before ingest marks anything seen.
    graph = build_graph(cfg, build_screener(cfg, secrets, load_profile()), build_researcher(cfg, secrets)[0])
    tg, app = (None, None) if dry_run else _delivery(cfg, secrets, store)

    first_run = not store.initialised()
    try:
        with make_client(cfg.limits.fetch_timeout_s) as http:
            ingest = run_ingest(
                store,
                default_fetchers(http, store, first_run, now, cfg.sources.company_passes),
                first_run=first_run,
                today=today,
                max_report=cap,
                now=now,
                dry_run=dry_run,
            )
    except AllSourcesFailed as e:
        log.error("all sources failed: %s", e)
        return 1
    log.info("ingest: %d new, %d overflow, errors=%s", len(ingest.new), ingest.overflow, ingest.errors)

    # Marked seen but not delivered yet; whatever is left here at the end (or on a crash) is requeued.
    undelivered = {p.key: p.model_dump(mode="json") for p in ingest.new}
    try:
        return _report(cfg, secrets, store, graph, tg, app, ingest, undelivered, now=now, dry_run=dry_run)
    except BaseException as e:
        if not dry_run:  # a dry run marked nothing seen
            store.requeue(list(undelivered.items()))
        if not isinstance(e, Exception):
            raise
        log.exception("run failed after ingest; undelivered postings were requeued")
        return 1


def _report(cfg, secrets, store: Store, graph, tg, app, ingest, undelivered: dict, *, now: datetime, dry_run: bool):
    with tracing(cfg, secrets):
        out = graph.invoke({"new": ingest.new}, {"recursion_limit": cfg.limits.graph_recursion_limit})

    final = out.get("final", [])
    briefs = {b.key: b for b in out.get("briefs", [])}
    plan = select_digest(final, cfg.limits.max_digest_items)
    researched = sum(1 for b in briefs.values() if b.brief)
    summary = summary_text(plan, researched, ingest.errors)

    def text_for(s):
        b = briefs.get(s.posting.key)
        note = s.note or (f"research failed: {b.error}" if b and b.error else "")
        return item_text(
            s.posting,
            s.fit.score if s.fit else None,
            s.fit.reason if s.fit else None,
            b.brief if b else None,
            note,
            injection=bool(s.fit and s.fit.injection_suspected),
        )

    if dry_run:
        print(summary)
        for s in plan.top:
            print("\n---\n" + text_for(s))
        if plan.more:
            print("\n---\n" + more_text(plan.more))
        return 0

    # Screened out (dropped) postings are done: they stay seen. Deferred ones stay undelivered.
    planned = {s.posting.key for s in plan.top + plan.more + plan.deferred}
    for key in [k for k in undelivered if k not in planned]:
        del undelivered[key]
    for s in plan.top + plan.more:  # link each shown posting to its screening run for owner feedback
        store.link_run(
            s.posting.key, s.run_id, s.route, s.fit.score if s.fit else None, s.posting.model_dump(mode="json"), now
        )
    if plan.top or plan.deferred or ingest.errors:
        try:
            tg.send(summary)
        except TelegramError as e:
            log.warning("summary not sent: %s", e)
    sent = 0
    for s in plan.top:
        b = briefs.get(s.posting.key)
        try:
            offer(app, store, tg, text_for(s), s.posting, b.brief if b else None, datetime.now(UTC))
            sent += 1
            undelivered.pop(s.posting.key, None)
        except TelegramError as e:
            log.warning("offer failed, requeued: %s", e)
    if plan.more:
        try:
            tg.send(more_text(plan.more))
            for s in plan.more:
                undelivered.pop(s.posting.key, None)
        except TelegramError as e:
            log.warning("overflow list not sent, requeued: %s", e)
    requeue = list(undelivered.items())
    store.requeue(requeue)
    undelivered.clear()  # requeued: nothing left for the failure path to put back
    log.info("digest: %d sent, %d listed, %d requeued", sent, len(plan.more), len(requeue))
    return 0


if __name__ == "__main__":
    sys.exit(main())

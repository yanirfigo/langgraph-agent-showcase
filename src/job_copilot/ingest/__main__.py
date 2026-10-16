"""CLI: `uv run python -m job_copilot.ingest [--dry-run] [--db PATH]`. Prints new postings as JSON lines."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from ..config import load_config
from ..store import DEFAULT_DB, Store
from . import AllSourcesFailed, default_fetchers, run_ingest
from .sources import make_client


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="job_copilot.ingest")
    ap.add_argument("--dry-run", action="store_true", help="don't mark anything as seen")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    args = ap.parse_args(argv)

    cfg = load_config()
    now = datetime.now(UTC)
    today = now.astimezone(ZoneInfo(cfg.timezone)).date()
    store = Store(args.db)
    try:
        first_run = not store.initialised()
        with make_client(cfg.limits.fetch_timeout_s) as client:
            result = run_ingest(
                store,
                default_fetchers(client, store, first_run, now),
                first_run=first_run,
                today=today,
                max_report=cfg.limits.max_postings_per_run,
                now=now,
                dry_run=args.dry_run,
            )
    except AllSourcesFailed as e:
        print(f"all sources failed: {e}", file=sys.stderr)
        return 1
    finally:
        store.close()

    for p in result.new:
        print(json.dumps(p.model_dump(exclude_none=True), ensure_ascii=False))
    summary = {
        "first_run": result.first_run,
        "new": len(result.new),
        "overflow": result.overflow,
        "skipped_invalid_rows": result.skipped,
        "errors": result.errors,
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

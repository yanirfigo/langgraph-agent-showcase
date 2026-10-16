"""Shadow-mode report: this agent vs the legacy job watcher it replaces, over the same days.

The legacy watcher's seen.json ({"company|title": "YYYY-MM-DD"}) is piped in on stdin:

    cat <legacy-watcher>/seen.json \\
      | ssh <service-user>@<host> 'cd ~/job-copilot && .venv/bin/python tools/shadow_report.py --since YYYY-MM-DD'

Cut-over rule: the new agent must miss nothing the legacy watcher found.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from job_copilot.store import Store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", type=date.fromisoformat, required=True)
    args = ap.parse_args(argv)
    legacy = {k for k, d in json.load(sys.stdin).items() if date.fromisoformat(d) >= args.since}
    store = Store()
    try:
        ours = {k for (k,) in store.db.execute("SELECT key FROM seen WHERE first_seen >= ?", (args.since.isoformat(),))}
        shown = {
            k for (k,) in store.db.execute("SELECT key FROM run_links WHERE created >= ?", (args.since.isoformat(),))
        }
        acted = dict(store.db.execute("SELECT key, status FROM applications"))
    finally:
        store.close()
    only_legacy = sorted(legacy - ours)
    print(f"since {args.since}: legacy reported {len(legacy)}, Job Copilot ingested {len(ours)}, shown {len(shown)}")
    only_ours = len(ours - legacy)
    print(f"found by both: {len(legacy & ours)} | only legacy: {len(only_legacy)} | only Job Copilot: {only_ours}")
    for k in only_legacy[:20]:
        print("  missed by Job Copilot:", k)
    if acted:
        print(
            "owner actions: "
            + ", ".join(f"{s} {sum(1 for v in acted.values() if v == s)}" for s in sorted(set(acted.values())))
        )
    print("cut-over check: Job Copilot should miss nothing the legacy watcher found (only-legacy == 0).")
    return 0 if not only_legacy else 1


if __name__ == "__main__":
    sys.exit(main())

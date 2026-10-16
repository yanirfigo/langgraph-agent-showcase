"""The release gate: run every eval; any failure blocks a change to prompts, models or thresholds.

    uv run python evals/gate.py data/pool.jsonl data/labels.json

Runs on the host (it needs the keys, the profile and the CV). Each eval is recorded in LangSmith.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    pool, labels = argv
    suites = [
        ("screener (fit-labels + live)", [HERE / "screener_eval.py", pool, labels]),
        ("research red-team", [HERE / "redteam_research.py"]),
        ("draft quality", [HERE / "draft_eval.py", pool, labels]),
    ]
    results = []
    for name, cmd in suites:
        print(f"=== {name} ===", flush=True)
        proc = subprocess.run([sys.executable, *map(str, cmd)], capture_output=True, text=True)  # noqa: S603
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        for ln in lines[-8:]:
            print("  " + ln)
        if proc.returncode not in (0, 1):
            print("  stderr: " + (proc.stderr.strip().splitlines() or ["?"])[-1][:300])
        results.append((name, proc.returncode == 0))
    print("=== summary ===")
    for name, ok in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    passed = all(ok for _, ok in results)
    print("RELEASE GATE:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

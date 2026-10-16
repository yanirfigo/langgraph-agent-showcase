"""Promote real owner decisions into the LangSmith dataset `fit-labels-live`.

    uv run python evals/promote_feedback.py [--dry-run]

Every posting the owner acted on in Telegram becomes a labelled example: dismissed -> "no";
saved, drafted, applied -> "fit". Disagreements with the agent's route are reported, because those
are the examples that teach the most. Runs weekly (deploy/systemd/job-copilot-promote.timer); screener_eval.py grades
the screener on fit-labels and fit-labels-live together.
"""

from __future__ import annotations

import argparse
import sys

from job_copilot.config import load_config
from job_copilot.observability import make_client
from job_copilot.secrets import load_secrets
from job_copilot.store import Store

DATASET = "fit-labels-live"
LABEL = {"dismissed": "no", "saved": "fit", "drafted": "fit", "applied": "fit", "draft discarded": "fit"}


def disagreement(label: str, route: str) -> str | None:
    if label == "fit" and route == "drop":
        return "agent dropped a posting the owner wanted"
    if label == "no" and route == "research":
        return "agent researched a posting the owner dismissed"
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    cfg, secrets = load_config(), load_secrets()
    store = Store()
    try:
        rows = [r for r in store.unpromoted_feedback() if r["status"] in LABEL]
        if not rows:
            print("nothing new to promote")
            return 0
        examples = []
        for r in rows:
            label = LABEL[r["status"]]
            note = disagreement(label, r["route"])
            print(
                f"{label:4} {r['route']:8} {str(r['score']):>4}  {r['posting']['title'][:60]}"
                + (f"  <- {note}" if note else "")
            )
            examples.append(
                {
                    "inputs": {"posting": r["posting"]},
                    "outputs": {"label": label, "note": f"owner: {r['status']}"},
                    "metadata": {"source": "telegram", "agent_route": r["route"], "disagreement": bool(note)},
                }
            )
        if args.dry_run:
            print(f"dry run: {len(examples)} examples not uploaded")
            return 0
        client = make_client(cfg, secrets)
        if client is None:
            print("LANGSMITH_API_KEY missing", file=sys.stderr)
            return 2
        if not client.has_dataset(dataset_name=DATASET):
            client.create_dataset(DATASET, description="Owner decisions from Telegram (promote_feedback.py)")
        ds = client.read_dataset(dataset_name=DATASET)
        client.create_examples(
            dataset_id=ds.id,
            inputs=[e["inputs"] for e in examples],
            outputs=[e["outputs"] for e in examples],
            metadata=[e["metadata"] for e in examples],
        )
        store.mark_promoted([r["key"] for r in rows])
        print(f"promoted {len(examples)} examples to {DATASET}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())

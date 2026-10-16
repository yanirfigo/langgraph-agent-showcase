"""Screener eval against the owner's labels, recorded as a LangSmith experiment.

    uv run python evals/screener_eval.py data/pool.jsonl data/labels.json

Gate: no posting labelled "fit" is dropped, and >= 80% of postings routed to research
are labelled "fit" (strict: "maybe" counts as a miss). Exit code 1 if the gate fails.
"""

from __future__ import annotations

import hashlib
import json
import sys

sys.path.insert(0, __import__("os").path.dirname(__file__))
from pathlib import Path

from _common import EVAL_USAGE_DB, eval_config  # noqa: E402
from langsmith import Client

from job_copilot.guards import title_deal_breaker
from job_copilot.ingest.models import Posting
from job_copilot.observability import make_client
from job_copilot.profile import load_profile
from job_copilot.routing import route_for
from job_copilot.secrets import load_secrets
from job_copilot.subagents.screener import build_screener

DATASET = "fit-labels"
LIVE_DATASET = "fit-labels-live"
GATE_RESEARCH_PRECISION = 0.8


def load_examples(pool: Path, labels: Path) -> list[dict]:
    by_key = {x["key"]: x for x in json.loads(labels.read_text(encoding="utf-8"))["labels"]}
    out = []
    for line in pool.read_text(encoding="utf-8").splitlines():
        p = Posting.model_validate_json(line)
        if p.key in by_key:
            lab = by_key[p.key]
            out.append(
                {
                    "inputs": {"posting": p.model_dump(mode="json")},
                    "outputs": {"label": lab["label"], "note": lab.get("note", "")},
                }
            )
    return out


def sync_dataset(client: Client, examples: list[dict]) -> str:
    """Create or refresh the dataset; skip the upload when the content is unchanged."""
    digest = hashlib.sha256(json.dumps(examples, sort_keys=True).encode()).hexdigest()[:16]
    if client.has_dataset(dataset_name=DATASET):
        ds = client.read_dataset(dataset_name=DATASET)
        if (ds.description or "").endswith(digest):
            return ds.name
        client.delete_dataset(dataset_id=ds.id)
    ds = client.create_dataset(DATASET, description=f"Owner fit labels for the screener. content={digest}")
    client.create_examples(
        dataset_id=ds.id,
        inputs=[e["inputs"] for e in examples],
        outputs=[e["outputs"] for e in examples],
    )
    return ds.name


def main(pool: Path, labels: Path) -> int:
    cfg, secrets = eval_config(), load_secrets()
    client = make_client(cfg, secrets)
    if client is None:
        print("LANGSMITH_API_KEY missing in secrets.env", file=sys.stderr)
        return 2
    screen = build_screener(cfg, secrets, load_profile(), usage_db=EVAL_USAGE_DB)

    def target(inputs: dict) -> dict:
        p = Posting.model_validate(inputs["posting"])
        if breaker := title_deal_breaker(p.title):
            return {"route": "drop", "score": None, "reason": f"title deal-breaker: {breaker}"}
        f = screen(p)
        return {
            "route": route_for(f, cfg),
            "score": f.score,
            "reason": f.reason,
            "injection_suspected": f.injection_suspected,
            "deal_breaker": f.deal_breaker,
        }

    def route_matches_label(outputs: dict, reference_outputs: dict) -> dict:
        label, route = reference_outputs["label"], (outputs or {}).get("route")
        if route is None:  # the target errored
            return {"key": "route_matches_label", "score": 0, "comment": "target error"}
        ok = {"fit": route != "drop", "no": route == "drop", "maybe": True}[label]
        return {"key": "route_matches_label", "score": int(ok)}

    datasets = [sync_dataset(client, load_examples(pool, labels))]
    if client.has_dataset(dataset_name=LIVE_DATASET):  # owner decisions promoted from Telegram
        datasets.append(LIVE_DATASET)
    rows = []
    for name in datasets:
        results = client.evaluate(
            target,
            data=name,
            evaluators=[route_matches_label],
            experiment_prefix=f"screener-{cfg.models.screener}-{name}",
            metadata={
                "model": cfg.models.screener,
                "digest_min": cfg.thresholds.digest_min,
                "research_min": cfg.thresholds.research_min,
            },
            max_concurrency=4,
        )
        print(f"experiment: {results.experiment_name}")
        rows += [
            (r["example"].outputs["label"], r["run"].outputs or {}, r["example"].inputs["posting"]["title"])
            for r in results
        ]
    fit_total = sum(1 for lab, _, _ in rows if lab == "fit")
    fit_dropped = [t for lab, o, t in rows if lab == "fit" and o.get("route") == "drop"]
    research = [(lab, t) for lab, o, t in rows if o.get("route") == "research"]
    precision = sum(1 for lab, _ in research if lab == "fit") / len(research) if research else 0.0
    no_total = sum(1 for lab, _, _ in rows if lab == "no")
    no_dropped = sum(1 for lab, o, _ in rows if lab == "no" and o.get("route") == "drop")

    errors = [t for _, o, t in rows if not o.get("route")]
    print(f"datasets: {', '.join(datasets)} | runs: {len(rows)}, errored: {len(errors)}")
    print(f"fit recall (not dropped): {fit_total - len(fit_dropped)}/{fit_total}")
    print(f"research precision (strict): {precision:.2f} over {len(research)} routed to research")
    print(f"'no' postings dropped: {no_dropped}/{no_total}")
    for t in fit_dropped:
        print(f"  MISSED FIT: {t}")
    for lab, t in research:
        if lab != "fit":
            print(f"  research but labelled {lab}: {t}")
    passed = not errors and not fit_dropped and precision >= GATE_RESEARCH_PRECISION
    print("GATE:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))

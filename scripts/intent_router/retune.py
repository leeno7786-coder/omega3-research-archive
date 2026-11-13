"""Retune a trained router's decision rule on its own validation split, with agent lanes costlier.

    .\\.venv-brain\\Scripts\\python.exe scripts\\intent_router\\retune.py models\\intent\\<run> ^
        --weights 1 2 3 5 --max-escalation 0.30 [--write]

The run's ONNX bundle scores its recorded validation split on CPU; the training data must
still hash to the run's dataset digest, so the split is exactly the one it was tuned on.
For each agent-lane weight the decision rule is re-tuned on validation. The chosen weight has
the fewest validation misroutes into the coding or browser lane among weights whose validation
escalation rate stays within --max-escalation (ties: fewer costly misroutes, then lower
standard cost). Only after the choice is the blind gate scored, once, and reported in
aggregate: its misrouted texts are never printed, so it stays blind.

--write creates ``<run>-retuned-w<weight>`` (bundle files hard-linked, router.json rewritten,
pins and metrics recomputed) for promote.py.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parents[1]
sys.path[:0] = [str(SCRIPT_DIR), str(REPO)]

import train  # noqa: E402
from dataset import ROUTES, eval_rows, gate_rows, training_rows  # noqa: E402
from global_loop.intent.router import IntentRouter, decide, softmax  # noqa: E402

SUMMARY_KEYS = ("n", "mean_cost", "escalation_rate", "costly_misroutes", "confident_costly_misroutes",
                "misroutes_by_type", "route_recall", "known_argmax_accuracy", "mixed_intent_exact_match")


def logits(router: IntentRouter, rows) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    parts = [router.logits(row.text, row.context) for row in rows]
    return tuple(np.concatenate([part[i] for part in parts]) for i in range(3))  # type: ignore[return-value]


def agent_misroutes(rows, decisions: np.ndarray) -> int:
    costs = train.cost_table(rows)[np.arange(len(rows)), decisions]
    agent_columns = {ROUTES.index(route) for route in train.AGENT_ROUTES}
    return int(sum(1 for cost, choice in zip(costs, decisions) if cost > 1 and int(choice) in agent_columns))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--weights", type=float, nargs="+", default=[1.0, 2.0, 3.0, 5.0])
    parser.add_argument("--max-escalation", type=float, default=0.30)
    parser.add_argument("--write", action="store_true")
    options = parser.parse_args()
    run = options.run_dir.resolve()
    artifact = json.loads((run / "artifact.json").read_text(encoding="utf-8"))
    config = json.loads((run / "router.json").read_text(encoding="utf-8"))

    rows, errors = training_rows()
    if errors:
        raise SystemExit("dataset errors; run dataset.py first:\n" + "\n".join(errors[:20]))
    if train.dataset_digest(rows) != artifact["dataset_digest"]:
        raise SystemExit("refusing: the training data changed since this run, so its validation split "
                         "cannot be reproduced")
    run_options = artifact["options"]
    _, val, _ = train.split_rows(rows, run_options["val_fraction"], run_options["cal_fraction"], run_options["seed"])
    if len(val) != artifact["splits"]["val"]:
        raise SystemExit(f"refusing: reproduced validation has {len(val)} rows, the run had {artifact['splits']['val']}")

    router = IntentRouter(run)
    calibration = config["temperatures"]
    val_logits = logits(router, val)
    primary = softmax(val_logits[0] / calibration["primary_temperature"])
    certainty = softmax(val_logits[2] / calibration["certainty_temperature"])

    candidates = []
    for weight in options.weights:
        decision = train.tune_decision(val, primary, certainty, table=train.weighted_cost_table(val, weight))
        thresholds = np.array([decision["thresholds"][route] for route in ROUTES])
        decisions = decide(primary, certainty, thresholds, decision["margin"], decision["known_threshold"])
        candidate = {"weight": weight, "decision": decision,
                     "val_agent_misroutes": agent_misroutes(val, decisions),
                     "val_costly_misroutes": int((train.row_costs(val, decisions) > 1).sum())}
        candidates.append(candidate)
        print(json.dumps(candidate))
    eligible = [c for c in candidates if c["decision"]["validation_escalation_rate"] <= options.max_escalation]
    if not eligible:
        raise SystemExit(f"no weight keeps validation escalation within {options.max_escalation}")
    chosen = min(eligible, key=lambda c: (c["val_agent_misroutes"], c["val_costly_misroutes"],
                                          c["decision"]["validation_mean_cost"], c["weight"]))
    print(f"chosen on validation: weight {chosen['weight']}")

    decision = chosen["decision"]
    intent_thresholds = config["intent_thresholds"]
    gate, gate_errors = gate_rows()
    evaluation, eval_errors = eval_rows()
    if gate_errors or eval_errors:
        raise SystemExit("gate/eval errors:\n" + "\n".join((gate_errors + eval_errors)[:20]))
    metrics = {
        "validation": train.report(val, val_logits, calibration, decision, intent_thresholds),
        "gate": train.report(gate, logits(router, gate), calibration, decision, intent_thresholds) if gate else None,
        "eval": train.report(evaluation, logits(router, evaluation), calibration, decision, intent_thresholds)
        if evaluation else None,
    }
    for split in ("validation", "eval", "gate"):
        if metrics[split]:
            print(split, json.dumps({key: metrics[split][key] for key in SUMMARY_KEYS}))
    if metrics["gate"]:
        print("gate misroute costs (aggregate only)",
              sorted((float(e["cost"]) for e in metrics["gate"]["errors"]), reverse=True))

    if not options.write:
        return 0
    weight_label = f"{chosen['weight']:g}".replace(".", "p")
    target = run.with_name(f"{run.name}-retuned-w{weight_label}")
    if target.exists():
        raise SystemExit(f"refusing: {target} already exists")
    shutil.copytree(run, target, copy_function=os.link)
    new_config = dict(config, thresholds=decision["thresholds"], margin=decision["margin"],
                      known_threshold=decision["known_threshold"])
    for name, payload in (("router.json", new_config),):
        path = target / name
        path.unlink()  # break the hard link before writing
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    new_artifact = dict(artifact, run=target.name)
    new_artifact["metrics"] = dict(artifact["metrics"], decision=decision, **metrics,
                                   retune={"source_run": artifact["run"], "candidates": candidates,
                                           "chosen_weight": chosen["weight"],
                                           "max_escalation": options.max_escalation})
    new_artifact["pins"] = {path.relative_to(target).as_posix(): train.sha256(path)
                            for path in sorted(target.rglob("*")) if path.is_file() and path.name != "artifact.json"}
    artifact_path = target / "artifact.json"
    artifact_path.unlink()
    artifact_path.write_text(json.dumps(new_artifact, indent=2), encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Promote a verified router run into the brain's pinned intent-router bundle.

Run with the brain interpreter, which is also what proves ONNX parity on CPU:

    .\\.venv-brain\\Scripts\\python.exe scripts\\intent_router\\promote.py models\\intent\\<run>

Refuses unless:

* every file the run recorded still matches its sha256 pin;
* router.onnx reproduces the PyTorch parity probes within tolerance (verify_onnx.py);
* the blind gate set (data/v2/gate, written from LABELS.md alone and never used to
  diagnose a run) made no costly misroute, i.e. no decision worse than escalating.
  ``--allow-costly-misroutes N`` relaxes only this gate, and only explicitly.

An optional development evaluation (data/v2/eval) is recorded when present but never
gates. On success the bundle files are copied atomically to
``models/intent/router/`` and ``config/intent_router.pin.json`` records their digests,
the run and both evaluation summaries.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO = SCRIPT_DIR.parents[1]
sys.path[:0] = [str(REPO), str(SCRIPT_DIR)]

from global_loop.intent.router import BUNDLE_FILES, PIN_FILENAME  # noqa: E402
from verify_onnx import verify  # noqa: E402

AGENT_ROUTES = ("CODING_AGENT", "BROWSER_AGENT")
EVAL_SUMMARY_KEYS = ("n", "mean_cost", "escalation_rate", "costly_misroutes", "confident_costly_misroutes",
                     "misroutes_by_type", "route_recall", "known_argmax_accuracy", "mixed_intent_exact_match")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--allow-costly-misroutes", type=int, default=0)
    parser.add_argument("--forbid-agent-misroutes", action="store_true",
                        help="refuse any blind-gate misroute into the coding or browser lane")
    parser.add_argument("--max-confident-misroutes", type=int, default=None,
                        help="refuse more blind-gate costly misroutes made at >= 0.9 confidence than this")
    parser.add_argument("--min-route-recall", nargs="*", default=[], metavar="ROUTE=RECALL",
                        help="refuse a blind-gate recall below RECALL for ROUTE")
    parser.add_argument("--note", default="", help="why this run is promoted, recorded in the pin")
    parser.add_argument("--models-root", type=Path, default=REPO / "models")
    parser.add_argument("--pin", type=Path, default=REPO / "config" / PIN_FILENAME)
    options = parser.parse_args()
    run = options.run_dir.resolve()

    artifact = json.loads((run / "artifact.json").read_text(encoding="utf-8"))
    drifted = [name for name, digest in artifact["pins"].items() if sha256(run / name) != digest]
    if drifted:
        raise SystemExit(f"refusing: files changed since training: {drifted}")
    parity = verify(run)
    if not parity["ok"]:
        raise SystemExit(f"refusing: ONNX parity failed {parity['max_abs_diff']}")
    metrics = artifact.get("metrics") or {}
    gate = metrics.get("gate")
    if not gate:
        raise SystemExit("refusing: the run recorded no blind gate evaluation (data/v2/gate)")
    if gate["costly_misroutes"] > options.allow_costly_misroutes:
        raise SystemExit(f"refusing: {gate['costly_misroutes']} costly misroute(s) on the blind "
                         f"gate: {gate['misroutes_by_type']}")
    if options.forbid_agent_misroutes:
        into_agents = {kind: count for kind, count in gate["misroutes_by_type"].items()
                       if kind.split("->")[-1] in AGENT_ROUTES}
        if into_agents:
            raise SystemExit(f"refusing: blind-gate misroutes into an agent lane: {into_agents}")
    if options.max_confident_misroutes is not None:
        confident = gate.get("confident_costly_misroutes")
        if confident is None or confident > options.max_confident_misroutes:
            raise SystemExit(f"refusing: {confident} confident costly misroute(s) on the blind gate "
                             f"(allowed {options.max_confident_misroutes})")
    minimum_recall: dict[str, float] = {}
    for spec in options.min_route_recall:
        route, _, value = spec.partition("=")
        minimum_recall[route] = float(value)
        recall = (gate.get("route_recall") or {}).get(route)
        if recall is None or recall < minimum_recall[route]:
            raise SystemExit(f"refusing: blind-gate {route} recall {recall} is below {minimum_recall[route]}")
    evaluation = metrics.get("eval")

    target = options.models_root / "intent" / "router"
    staging = target.with_name("router.staging")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    for name in BUNDLE_FILES:
        shutil.copy2(run / name, staging / name)
    digests = {name: sha256(staging / name) for name in BUNDLE_FILES}
    previous = target.with_name("router.previous")
    if target.exists():
        shutil.rmtree(previous, ignore_errors=True)
        target.rename(previous)
    staging.rename(target)

    pin = {
        "schema": 1,
        "model": artifact["model"],
        "base_revision": artifact["base_revision"],
        "run": artifact["run"],
        "dataset_digest": artifact["dataset_digest"],
        "promoted_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "files": digests,
        "parity_max_abs_diff": parity["max_abs_diff"],
        "decision": artifact["metrics"]["decision"],
        "gate": {key: gate.get(key) for key in EVAL_SUMMARY_KEYS},
        "promotion": {
            "allowed_costly_misroutes": options.allow_costly_misroutes,
            "forbid_agent_misroutes": options.forbid_agent_misroutes,
            "max_confident_misroutes": options.max_confident_misroutes,
            "min_route_recall": minimum_recall,
            "note": options.note,
        },
        "eval": {key: evaluation.get(key) for key in EVAL_SUMMARY_KEYS} if evaluation else None,
    }
    pin_tmp = options.pin.with_suffix(".tmp")
    pin_tmp.write_text(json.dumps(pin, indent=2) + "\n", encoding="utf-8")
    pin_tmp.replace(options.pin)
    print(json.dumps({"promoted": artifact["run"], "bundle": str(target), "pin": str(options.pin),
                      "gate": pin["gate"], "eval": pin["eval"]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Phase 7M — metacognitive pressure signals from the graph.

analyze_memory_pressure() reads the COMPILED graph and reports evidence-backed pressure
CANDIDATES. It is strictly advisory:
  - it does NOT open episodes
  - it does NOT choose an action
  - it only reports candidates with their evidence; the Pressure layer decides to ignite.

Honest-signals discipline: only emit a signal when the graph actually backs it (refuse to
fabricate). Deterministic — same graph => identical output. Guardian-free; imports nothing
from the cascade.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from memory.taxonomy import graph_store as G, paths as P

_FAILURE_KINDS = frozenset({"tool_failure", "skill_failure", "bridge_failure", "safety_block"})

# failure-event-kind -> (cluster signal name, recommended pressure)
_CLUSTER_MAP = {
    "skill_failure": ("skill_failure_cluster", "capability_gap"),
    "tool_failure": ("tool_failure_cluster", "tool_failure"),
    "bridge_failure": ("bridge_failure_cluster", "tool_failure"),
    "safety_block": ("safety_block_cluster", "requires_operator_input"),
}

# deterministic urgency order for choosing the single top recommended_pressure
_PRIORITY = {
    "memory_reconciliation": 0,
    "safety_block_cluster": 0,
    "sense_resolution": 1,
    "skill_failure_cluster": 2,
    "repeated_failure": 3,
    "bridge_failure_cluster": 4,
    "tool_failure_cluster": 5,
    "incomplete_task": 6,
    "unverified_change": 7,
    "memory_gap": 8,
    "repeated_continuation_pressure": 9,
    "low_confidence_claims": 10,
}

_CLUSTER_THRESHOLD = 2
_VERDICT_PRESSURE = {
    "unknown": ("memory_gap", "memory_gap"),
    "ambiguous": ("sense_resolution", "sense_resolution"),
    "conflict": ("memory_reconciliation", "memory_reconciliation"),
}


def _signal(signal: str, recommended_pressure: str, evidence: Any, detail: dict) -> dict:
    return {
        "signal": signal,
        "recommended_pressure": recommended_pressure,
        "evidence_event_ids": sorted(set(evidence)),
        "detail": detail,
    }


def _read_snapshot_hash(root: Path | str | None) -> str:
    from memory.projection.generation import active_graph_snapshot_hash  # Cut 13: manifest hash under generation mode

    published = active_graph_snapshot_hash(root)
    if published is not None:
        return published
    f = P.compiled_root(root) / "latest_snapshot_hash.txt"
    return f.read_text(encoding="utf-8").strip() if f.exists() else "uncompiled"


def analyze_memory_pressure(root: Path | str | None = None) -> dict:
    """Return evidence-backed pressure candidates derived from the compiled graph."""
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(root):  # Cut 13: one generation for graph + snapshot hash
        return _analyze_memory_pressure_in_transaction(root)


def _analyze_memory_pressure_in_transaction(root: Path | str | None) -> dict:
    conn = G.connect_for_read(root)
    try:
        events = [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, event_kind, episode_id, payload_json FROM events"
            )
        ]
        claims = [
            dict(r)
            for r in conn.execute(
                "SELECT subject, predicate, object, confidence, source_event_id FROM claims"
            )
        ]
    finally:
        conn.close()

    payloads = {e["event_id"]: json.loads(e["payload_json"]) for e in events}
    signals: list[dict] = []

    # repeated_failure — same failure_class seen 2+ times
    by_class: dict[str, list[str]] = defaultdict(list)
    by_kind: dict[str, list[str]] = defaultdict(list)
    for e in events:
        if e["event_kind"] not in _FAILURE_KINDS:
            continue
        p = payloads[e["event_id"]]
        fc = str(p.get("failure_class") or p.get("kind") or "")
        if fc:
            by_class[fc].append(e["event_id"])
        by_kind[e["event_kind"]].append(e["event_id"])

    for fc, ids in sorted(by_class.items()):
        if len(ids) >= _CLUSTER_THRESHOLD:
            signals.append(
                _signal(
                    "repeated_failure",
                    "repair_needed",
                    ids,
                    {"failure_class": fc, "count": len(ids)},
                )
            )

    # per-kind failure clusters
    for kind, (name, pressure) in _CLUSTER_MAP.items():
        ids = by_kind.get(kind, [])
        if len(ids) >= _CLUSTER_THRESHOLD:
            signals.append(_signal(name, pressure, ids, {"count": len(ids)}))

    # unverified_change — a successful patch that changed files but ran no tests
    for e in events:
        if e["event_kind"] != "qwen_skill_result":
            continue
        p = payloads[e["event_id"]]
        if str(p.get("status")) == "success" and p.get("files_changed") and not p.get("tests_run"):
            signals.append(
                _signal(
                    "unverified_change",
                    "verification_needed",
                    [e["event_id"]],
                    {"files_changed": list(p.get("files_changed") or [])},
                )
            )

    # incomplete_task — an outcome that needs more work / was not satisfied
    for e in events:
        if e["event_kind"] != "global_b_skill_update":
            continue
        p = payloads[e["event_id"]]
        if p.get("more_work_needed") or not p.get("intent_satisfied"):
            signals.append(
                _signal(
                    "incomplete_task",
                    "incomplete_task",
                    [e["event_id"]],
                    {"unresolved_blockers": list(p.get("unresolved_blockers") or [])},
                )
            )

    # repeated_continuation_pressure — many created_pressure claims
    cp = [c for c in claims if c["predicate"] == "created_pressure"]
    if len(cp) >= _CLUSTER_THRESHOLD:
        signals.append(
            _signal(
                "repeated_continuation_pressure",
                "repair_needed",
                [c["source_event_id"] for c in cp],
                {"count": len(cp)},
            )
        )

    # low_confidence_claims — structural claims default to 1.0, so this only fires on real low confidence
    low = [c for c in claims if float(c["confidence"]) < 0.5]
    if low:
        signals.append(
            _signal(
                "low_confidence_claims",
                "confidence_reinforcement",
                [c["source_event_id"] for c in low],
                {"count": len(low)},
            )
        )

    # deterministic ordering by urgency then name
    signals.sort(key=lambda s: (_PRIORITY.get(s["signal"], 99), s["signal"]))

    evidence = sorted({eid for s in signals for eid in s["evidence_event_ids"]})
    recommended = signals[0]["recommended_pressure"] if signals else ""

    return {
        "signals": signals,
        "recommended_pressure": recommended,
        "evidence_event_ids": evidence,
        "snapshot_hash": _read_snapshot_hash(root),
    }


def analyze_recall_verdict_pressure(
    root: Path | str | None = None,
    *,
    limit: int = 50,
) -> dict:
    """Return pressure candidates from recent non-verified recall verdicts."""
    try:
        from memory.recall_verifier import verdict_log_path

        path = verdict_log_path(root=root)
    except Exception:
        path = None
    if path is None or not path.exists():
        return {
            "signals": [],
            "recommended_pressure": "",
            "evidence_event_ids": [],
            "snapshot_hash": _read_snapshot_hash(root),
        }

    records: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines()[-max(1, int(limit or 1)) :]:
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            records.append(item)

    signals: list[dict] = []
    for item in records:
        status = str(item.get("status") or "").strip().lower()
        mapped = _VERDICT_PRESSURE.get(status)
        if not mapped:
            continue
        signal, pressure = mapped
        verdict_id = str(item.get("verdict_id") or "")
        signals.append(
            _signal(
                signal,
                pressure,
                [verdict_id] if verdict_id else [],
                {
                    "status": status,
                    "query": str(item.get("query") or ""),
                    "reason": str(item.get("reason") or ""),
                    "cycle_id": str(item.get("cycle_id") or ""),
                },
            )
        )

    signals.sort(key=lambda s: (_PRIORITY.get(s["signal"], 99), s["signal"]))
    evidence = sorted({eid for s in signals for eid in s["evidence_event_ids"]})
    recommended = signals[0]["recommended_pressure"] if signals else ""
    return {
        "signals": signals,
        "recommended_pressure": recommended,
        "evidence_event_ids": evidence,
        "snapshot_hash": _read_snapshot_hash(root),
    }

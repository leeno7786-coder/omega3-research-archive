"""Deterministic ingest/recall replay harness.

This module builds a small isolated corpus twice and verifies that the
SourceLedger -> canonical memory -> graph -> DME -> RecallVerifier path produces
the same normalized report. It is intentionally outside the live runtime path.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.symphony_time import clear_fake_time, set_fake_time
from memory.taxonomy.normalize import canonical_json

_REPLAY_TIME = "2026-01-01T00:00:00+00:00"


@dataclass(frozen=True)
class ReplayRecordSpec:
    label: str
    text: str
    coord: tuple[int, int, int]
    title: str
    summary: str
    domain: str = ""
    subtopic: str = ""
    tags: tuple[str, ...] = ()
    source_type: str = "deterministic_replay"
    origin_id: str = ""
    memory_type: str = "concept"
    enrichment: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReplayQuerySpec:
    label: str
    text: str
    expected_label: str = ""
    expected_status: str = "verified"
    allowed_lobes: tuple[int, ...] | None = None
    denied_lobes: tuple[int, ...] = (6, 11)
    allowed_subdomains: tuple[int, ...] | None = None
    limit: int = 5
    success_mode: str = "exact"


DEFAULT_RECORDS: tuple[ReplayRecordSpec, ...] = (
    ReplayRecordSpec(
        label="identity_name",
        text="Omega identifies the primary operator as Lee for this deterministic replay.",
        coord=(0, 1, 1),
        title="Operator identity name",
        summary="The primary operator is Lee in this replay corpus.",
        domain="operator",
        subtopic="traits",
        tags=("identity", "operator", "name"),
        source_type="operator_chat",
        origin_id="replay-identity-name",
    ),
    ReplayRecordSpec(
        label="persona_mentor",
        text="Mara is the operator's testing mentor and prefers concise replay reports.",
        coord=(1, 1, 1),
        title="Mara testing mentor",
        summary="Mara is the testing mentor who prefers concise replay reports.",
        domain="mara",
        subtopic="person_stub",
        tags=("persona", "mentor", "testing"),
        source_type="operator_chat",
        origin_id="replay-persona-mentor",
    ),
    ReplayRecordSpec(
        label="knowledge_atp",
        text=(
            "Mitochondrial cristae host ATP synthase complexes that drive "
            "oxidative phosphorylation in eukaryotic cells."
        ),
        coord=(2, 0, 1),
        title="ATP synthase in cristae",
        summary="Cristae host ATP synthase for oxidative phosphorylation.",
        domain="biology",
        subtopic="cellular_energy",
        tags=("biology", "atp", "cristae", "oxidative"),
        source_type="operator_chat",
        origin_id="replay-knowledge-atp",
    ),
    ReplayRecordSpec(
        label="coding_exitstack",
        text=(
            "Python contextlib ExitStack composes nested context managers "
            "and guarantees cleanup callbacks on stack close."
        ),
        coord=(8, 0, 1),
        title="Python ExitStack cleanup",
        summary="ExitStack composes context managers and cleanup callbacks.",
        domain="python",
        subtopic="context_managers",
        tags=("python", "exitstack", "cleanup", "context"),
        source_type="autonomous_learning",
        origin_id="replay-coding-exitstack",
    ),
    ReplayRecordSpec(
        label="temporal_checkpoint",
        text="On 2026-07-04 the memory replay baseline became a required CI gate.",
        coord=(9, 0, 1),
        title="Replay CI baseline date",
        summary="The replay baseline became a required CI gate on 2026-07-04.",
        domain="calendar",
        subtopic="milestone",
        tags=("temporal", "ci", "baseline"),
        source_type="operator_chat",
        origin_id="replay-temporal-checkpoint",
    ),
    ReplayRecordSpec(
        label="browser_upload_artifact",
        text="The uploaded browser artifact says the blue widget serial is BW-1742.",
        coord=(7, 0, 1),
        title="Browser upload widget serial",
        summary="The blue widget serial is BW-1742.",
        domain="browser_upload",
        subtopic="artifact",
        tags=("browser", "upload", "artifact", "widget"),
        source_type="browser_upload",
        origin_id="replay-browser-upload",
    ),
    ReplayRecordSpec(
        label="ambiguous_agent_robotics",
        text="In robotics, an agent is an embodied planner that selects actions.",
        coord=(2, 23, 1),
        title="Agent robotics sense",
        summary="A robotics agent is an embodied planner that selects actions.",
        domain="robotics",
        subtopic="robotics",
        tags=("agent", "robotics", "planner"),
        source_type="operator_chat",
        origin_id="replay-agent-robotics",
    ),
    ReplayRecordSpec(
        label="ambiguous_agent_software",
        text="In software, an agent is an autonomous process that runs tools.",
        coord=(8, 0, 2),
        title="Agent software sense",
        summary="A software agent is an autonomous process that runs tools.",
        domain="python",
        subtopic="agents",
        tags=("agent", "software", "tools"),
        source_type="operator_chat",
        origin_id="replay-agent-software",
    ),
    ReplayRecordSpec(
        label="conflict_color_blue",
        text="The replay badge color is blue.",
        coord=(2, 12, 1),
        title="Replay badge color",
        summary="The replay badge color is blue.",
        domain="facts",
        subtopic="conflicts",
        tags=("conflict", "badge", "color"),
        source_type="operator_chat",
        origin_id="replay-conflict-blue",
    ),
    ReplayRecordSpec(
        label="conflict_color_green",
        text="The replay badge color is green.",
        coord=(2, 12, 2),
        title="Replay badge color",
        summary="The replay badge color is green.",
        domain="facts",
        subtopic="conflicts",
        tags=("conflict", "badge", "color"),
        source_type="operator_chat",
        origin_id="replay-conflict-green",
    ),
    ReplayRecordSpec(
        label="persona_codename",
        text="The operator project codename for this branch is deterministic recall.",
        coord=(1, 0, 1),
        title="Operator project codename",
        summary="The project codename is deterministic recall.",
        domain="operator",
        subtopic="project_context",
        tags=("operator", "project", "codename"),
        source_type="operator_chat",
        origin_id="replay-persona-codename",
    ),
    ReplayRecordSpec(
        label="situational_internal",
        text="Internal situational event: AUTO retried an operator task after a transient bridge fault.",
        coord=(4, 0, 1),
        title="Internal AUTO retry event",
        summary="AUTO retried an operator task after a transient bridge fault.",
        domain="system",
        subtopic="internal_events",
        tags=("situational", "auto", "internal"),
        source_type="system_event",
        origin_id="replay-situational-internal",
    ),
    ReplayRecordSpec(
        label="journal_deepsleep_private",
        text="DeepSleep private journal note: consolidate noisy replay traces out of freeform recall.",
        coord=(6, 0, 1),
        title="DeepSleep private replay note",
        summary="Private journal note for replay consolidation.",
        domain="deepsleep",
        subtopic="journal",
        tags=("journal", "deepsleep", "private"),
        source_type="deepsleep_journal",
        origin_id="replay-journal-private",
    ),
)


DEFAULT_QUERIES: tuple[ReplayQuerySpec, ...] = (
    ReplayQuerySpec(
        label="identity_query",
        text="who is the primary operator Lee replay",
        expected_label="identity_name",
        allowed_lobes=(0,),
    ),
    ReplayQuerySpec(
        label="persona_query",
        text="Mara testing mentor concise replay reports",
        expected_label="persona_mentor",
        allowed_lobes=(1,),
    ),
    ReplayQuerySpec(
        label="knowledge_query",
        text="ATP synthase cristae oxidative",
        expected_label="knowledge_atp",
        allowed_lobes=(2,),
        allowed_subdomains=(0,),
    ),
    ReplayQuerySpec(
        label="coding_query",
        text="Python ExitStack cleanup callbacks",
        expected_label="coding_exitstack",
        allowed_lobes=(8,),
    ),
    ReplayQuerySpec(
        label="temporal_query",
        text="when did replay baseline become required CI gate",
        expected_label="temporal_checkpoint",
        allowed_lobes=(9,),
    ),
    ReplayQuerySpec(
        # THE RECALL-LANE RULE (operator 2026-07-18): lobe 7 (vision) recalls
        # only through the dedicated visual pipeline — an EXPLICIT allow on the
        # general engine must return nothing (the boundary is structural).
        label="browser_upload_query",
        text="blue widget serial BW-1742 upload",
        expected_status="safe_closed",
        allowed_lobes=(7,),
    ),
    ReplayQuerySpec(
        label="ambiguous_polyseme_query",
        text="agent",
        expected_status="ambiguous",
        denied_lobes=(),
    ),
    ReplayQuerySpec(
        label="conflict_query",
        text="replay badge color",
        expected_status="conflict",
        allowed_lobes=(2,),
    ),
    ReplayQuerySpec(
        label="unknown_no_hit_query",
        text="nonexistent replay zirconium anchor",
        expected_status="unknown",
        allowed_lobes=(5,),
        denied_lobes=(),
    ),
    ReplayQuerySpec(
        label="freeform_boundary_query",
        text="Lee Mara ATP ExitStack CI gate private journal internal AUTO retry",
        allowed_lobes=(0, 1, 2, 8, 9),
        denied_lobes=(4, 6, 7, 11),
        success_mode="graph_only",
    ),
    ReplayQuerySpec(
        label="journal_exclusion_query",
        text="DeepSleep private replay consolidation journal",
        expected_status="safe_closed",
        denied_lobes=(6, 11),
    ),
    ReplayQuerySpec(
        # THE RECALL-LANE RULE (operator, re-affirmed 2026-07-18): the journal
        # is a METACOGNITIVE STATE, not a memory — an EXPLICIT allowed_lobes=(6,)
        # must return nothing. (Pre-ruling, this query pinned the override hole
        # as a feature: explicit journal asks VERIFIED.)
        label="journal_explicit_query",
        text="DeepSleep private replay consolidation journal",
        expected_status="safe_closed",
        allowed_lobes=(6,),
        denied_lobes=(),
    ),
)


# ── VERDICT-GROUNDING GAP: CLOSED (operator adjudication 2026-07-18) ──
# The 2026-07-17 bisect-proven gap (post-Slice-4b the verdict VERIFIED
# provenance-real but off-topic residue — "replay badge color" grounded the
# ATP-biology record at 0.02; the journal query grounded identity records at
# up to 0.956 — and no score floor separates noise from signal) was closed by
# the operator-adjudicated verdict topicality gate in memory/recall_verifier:
#   1. router-as-topicality-authority — the routed region (caller
#      allowed_lobes, else the deterministic route_memory_tool lobe) must be
#      recallable; a query whose region is NON_RECALLABLE grounds NOTHING;
#   2. deterministic topicality floor — a verified candidate sharing zero
#      query content-tokens cannot ground.
# Both demote to STRICT UNKNOWN (fail-closed; no visible "weak" status).
# The mechanism below stays: any future gap must be declared here VISIBLY
# (never silently green), and a LEAK of a gap's forbidden records remains a
# hard failure regardless.
KNOWN_VERDICT_GAPS: frozenset[str] = frozenset()

# Per-gap forbidden record labels: declaring a label in KNOWN_VERDICT_GAPS
# requires naming the records that must NEVER surface while the gap is open
# (a leak is a hard failure, never a gap). Empty while no gaps are declared.
_GAP_FORBIDDEN_LABELS: dict[str, tuple[str, ...]] = {}


def _forbidden_ids_for(label: str, written_by_label: Mapping[str, Mapping[str, Any]]) -> list[str]:
    return [
        str((written_by_label.get(record_label) or {}).get("id") or "")
        for record_label in _GAP_FORBIDDEN_LABELS.get(label, ())
    ]


def run_determinism_replay(
    *,
    base_dir: Path | str | None = None,
    keep_roots: bool = False,
    records: Iterable[ReplayRecordSpec] = DEFAULT_RECORDS,
    queries: Iterable[ReplayQuerySpec] = DEFAULT_QUERIES,
) -> dict[str, Any]:
    """Run two isolated replays and compare their normalized reports."""
    owned_temp: tempfile.TemporaryDirectory[str] | None = None
    if base_dir is None:
        owned_temp = tempfile.TemporaryDirectory(prefix="omega-memory-replay-")
        base = Path(owned_temp.name)
    else:
        base = Path(base_dir).resolve()
        if base.exists():
            shutil.rmtree(base)
        base.mkdir(parents=True, exist_ok=True)

    set_fake_time(iso=_REPLAY_TIME)
    try:
        first = run_single_replay(base / "run_a", records=records, queries=queries)
        second = run_single_replay(base / "run_b", records=records, queries=queries)
        comparison = compare_replay_reports(first, second)
        mutations = run_mutation_probes(base / "mutations", records=records, queries=queries)
        ok = bool(first["ok"] and second["ok"] and comparison["equal"] and mutations["ok"])
        report = {
            "type": "memory_determinism_replay_report",
            "ok": ok,
            "comparison": comparison,
            "runs": [first, second],
            "mutation_probes": mutations,
            "tamper_probe": mutations,
            "known_gaps": sorted(
                set(first.get("known_gaps", ())) | set(second.get("known_gaps", ()))
            ),
            "errors": [
                item
                for item in (
                    *first.get("errors", ()),
                    *second.get("errors", ()),
                    *comparison.get("errors", ()),
                    *mutations.get("errors", ()),
                )
            ],
        }
        return report
    finally:
        clear_fake_time()
        if owned_temp is not None and not keep_roots:
            owned_temp.cleanup()


def run_single_replay(
    root: Path | str,
    *,
    records: Iterable[ReplayRecordSpec] = DEFAULT_RECORDS,
    queries: Iterable[ReplayQuerySpec] = DEFAULT_QUERIES,
) -> dict[str, Any]:
    """Build one isolated replay root and return its normalized report."""
    root_path = Path(root)
    if root_path.exists():
        shutil.rmtree(root_path)
    root_path.mkdir(parents=True, exist_ok=True)

    # Provision the memory schema into the isolated root: reindex validates
    # against <root>/schema/memory_schema.json once OMEGA_ROOT re-points here —
    # without the copy every replay logged "schema not found, skipping
    # validation" and ran unvalidated.
    schema_src = Path(__file__).resolve().parents[1] / "schema" / "memory_schema.json"
    if schema_src.is_file():
        schema_dst = root_path / "schema" / "memory_schema.json"
        schema_dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(schema_src, schema_dst)

    errors: list[str] = []
    with _isolated_memory_modules(root_path):
        written = [_write_record(root_path, spec) for spec in records]
        written_by_label = {item["label"]: item for item in written}

        from memory.engine.reindex import reindex_and_compile
        from memory.dme.adapter import parity_report
        from memory.engine import walker

        # The one published projection owner returns its graph snapshot. Calling the
        # fixed-name taxonomy compiler after this would either rewrite behind the
        # served generation or, correctly, be rejected by the projection path guard.
        snapshot = reindex_and_compile(root=root_path)
        walker._coord_index_mtime = 0.0
        walker.load_coord_index()
        from memory.projection.generation import projection_read_transaction

        with projection_read_transaction(root_path):
            parity = parity_report(root=root_path).to_dict()

        query_reports = [_run_query(root_path, query, written_by_label) for query in queries]

    fingerprints = _projection_fingerprints(root_path, written)
    scoring = _scoring_report(query_reports)
    boundaries = _boundary_assertions(query_reports)
    known_gaps = sorted(
        str(item.get("label") or "") for item in query_reports if item.get("known_gap")
    ) + list(boundaries.get("known_gaps") or [])
    ok = (
        not errors
        and bool(snapshot.snapshot_hash)
        and parity.get("status") == "ok"
        and all(item.get("ok") or item.get("known_gap") for item in query_reports)
        and boundaries["ok"]
    )
    return {
        "type": "memory_determinism_replay_run",
        "ok": ok,
        "record_count": len(written),
        "records": written,
        "snapshot": snapshot.to_dict(),
        "dme_parity": parity,
        "fingerprints": fingerprints,
        "queries": query_reports,
        "scoring": scoring,
        "boundary_assertions": boundaries,
        "known_gaps": known_gaps,  # visible in every report — never silently green
        "errors": errors,
    }


def compare_replay_reports(first: Mapping[str, Any], second: Mapping[str, Any]) -> dict[str, Any]:
    first_hash = _report_hash(first)
    second_hash = _report_hash(second)
    errors = [] if first_hash == second_hash else ["normalized replay reports differ"]
    return {
        "equal": first_hash == second_hash,
        "first_hash": first_hash,
        "second_hash": second_hash,
        "errors": errors,
    }


def run_mutation_probes(
    root: Path | str,
    *,
    records: Iterable[ReplayRecordSpec] = DEFAULT_RECORDS,
    queries: Iterable[ReplayQuerySpec] = DEFAULT_QUERIES,
) -> dict[str, Any]:
    """Tamper every projection layer and ensure recall fails closed."""
    query_list = tuple(q for q in queries if q.expected_status == "verified" and q.expected_label)
    if not query_list:
        return {"ok": False, "errors": ["mutation probes require at least one verified query"]}

    probes = [
        ("canonical_file", _mutate_canonical_file),
        ("source_ledger", _mutate_source_ledger),
        ("graph_snapshot", _mutate_graph_snapshot),
        ("dme_sram_index", _mutate_dme_sram_index),
        ("sense_registry", _mutate_sense_registry),
    ]
    results = []
    for name, mutator in probes:
        probe_root = Path(root) / name
        built = run_single_replay(probe_root, records=records, queries=query_list)
        first_query = query_list[0]
        expected_record = next(
            item for item in built["records"] if item["label"] == first_query.expected_label
        )
        mutator(probe_root, expected_record)
        _unload_replay_dme_session(probe_root)
        with _isolated_memory_modules(probe_root):
            from memory.engine import walker

            walker.load_coord_index()
            after = _run_query(probe_root, first_query, {expected_record["label"]: expected_record})
        graph_verdict = after["graph_verdict"]
        dme_verdict = after["dme_verdict"]
        expected_id = str(expected_record["id"])
        probe_ok = _mutation_outcome_ok(name, graph_verdict, dme_verdict, expected_id)
        results.append(
            {
                "name": name,
                "ok": probe_ok,
                "graph_status": graph_verdict.get("status"),
                "dme_status": dme_verdict.get("status"),
                "graph_target_exposed": _payload_contains(graph_verdict, expected_id),
                "dme_target_exposed": _payload_contains(dme_verdict, expected_id),
            }
        )
    ok = all(item["ok"] for item in results)
    return {
        "type": "memory_determinism_mutation_probes",
        "ok": ok,
        "probes": results,
        "errors": [] if ok else ["one or more projection mutations did not fail closed"],
    }


def run_tamper_probe(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Backward-compatible alias for older callers."""
    return run_mutation_probes(*args, **kwargs)


def _write_record(root: Path, spec: ReplayRecordSpec) -> dict[str, Any]:
    from memory.core import build_memory_dict
    from memory.engine.validate_write import atomic_write
    from memory.source_ledger.store import record_source

    artifact = record_source(
        spec.text,
        source_type=spec.source_type,
        origin_id=spec.origin_id or spec.label,
        root=root,
    )
    metadata = {
        "type": spec.memory_type,
        "title": spec.title,
        "summary": spec.summary,
        "domain": spec.domain,
        "subtopic": spec.subtopic,
        "tags": list(spec.tags),
        "canonical": True,
        "origin_id": spec.origin_id or spec.label,
        "confidence": 0.95,
        **artifact.citation(),
    }
    enrichment = {"canonical": True, **dict(spec.enrichment or {})}
    record = build_memory_dict(
        spec.text,
        list(spec.coord),
        spec.source_type,
        metadata=metadata,
        enrichment=enrichment,
    )
    path = Path(atomic_write(record, update_index=True))
    return {
        "label": spec.label,
        "id": record["id"],
        "coord": list(record["coord"]),
        "path": _relative_path(path, root),
        "source_id": artifact.source_id,
        "span_hash": artifact.span_hash,
        "source_content_hash": artifact.content_hash,
    }


def _within_lobe_bounds(
    verdict_dict: Mapping[str, Any],
    allowed_lobes,
    denied_lobes,
) -> bool:
    """Every admitted candidate's lobe must respect the query's contract —
    deny wins over allow, and an empty admission trivially complies."""
    allowed = set(allowed_lobes) if allowed_lobes is not None else None
    denied = set(denied_lobes or ())
    payload = verdict_dict.get("answer_payload") or {}
    for cand in payload.get("candidates") or ():
        coord = list(cand.get("coord") or [])
        if not coord:
            return False
        lobe = int(coord[0])
        if lobe in denied:
            return False
        if allowed is not None and lobe not in allowed:
            return False
    return True


def _run_query(
    root: Path,
    query: ReplayQuerySpec,
    written_by_label: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    from memory.dme.adapter import recall_candidates
    from memory.recall_verifier import verify_candidates
    from memory.taxonomy.recall import recall

    expected = written_by_label.get(query.expected_label) if query.expected_label else None
    expected_id = str((expected or {}).get("id") or "")

    packet = recall(
        query.text,
        allowed_lobes=query.allowed_lobes,
        denied_lobes=query.denied_lobes,
        allowed_subdomains=query.allowed_subdomains,
        limit=query.limit,
        root=root,
        touch=False,
    )
    graph_candidates = [_candidate_from_packet_result(item) for item in packet.results]
    graph_verdict = verify_candidates(
        query.text,
        graph_candidates,
        meta={"candidate_source": "graph_replay"},
        root=root,
        allowed_lobes=query.allowed_lobes,
    )

    dme_projection_unavailable = False
    try:
        dme_candidates = recall_candidates(
            query.text,
            allowed_lobes=query.allowed_lobes,
            denied_lobes=query.denied_lobes,
            allowed_subdomains=query.allowed_subdomains,
            limit=query.limit,
            root=root,
        )
    except Exception as exc:
        from memory.projection.generation import GenerationIntegrityError

        if not isinstance(exc, GenerationIntegrityError):
            raise
        # A corrupt Program 0 generation makes this derived lane unavailable.
        # The replay harness records that state as an empty DME contribution;
        # the verifier then produces the same no-payload unknown it owes callers.
        dme_projection_unavailable = True
        dme_candidates = []
    dme_verdict = verify_candidates(
        query.text,
        dme_candidates,
        meta={"candidate_source": "dme_replay"},
        root=root,
        allowed_lobes=query.allowed_lobes,
    )

    graph_ids = [str(item.get("node_id") or "") for item in packet.results]
    dme_ids = [str(item.get("memory_id") or "") for item in dme_candidates]
    graph_verdict_dict = _normalize_paths(graph_verdict.to_dict(), root)
    dme_verdict_dict = _normalize_paths(dme_verdict.to_dict(), root)
    if query.expected_status == "verified" and query.success_mode == "exact":
        ok = (
            graph_verdict.status == "verified"
            and dme_verdict.status == "verified"
            and graph_ids[:1] == [expected_id]
            and expected_id in dme_ids
        )
    elif query.expected_status == "verified" and query.success_mode == "any_verified":
        ok = (
            graph_verdict.status == "verified"
            and dme_verdict.status == "verified"
            and (not expected_id or expected_id in graph_ids)
            and (not expected_id or expected_id in dme_ids)
        )
    elif query.expected_status == "verified" and query.success_mode == "graph_only":
        # The DME leg's contract here is the LOBE BOUNDARY, not refusal: the
        # graded Thought Engine legitimately surfaces token-spread candidates
        # the verifier proves real (find aggressively / verify provenance).
        # The old fail-closed pin encoded the strict-intersection engine.
        ok = (
            graph_verdict.status == "verified"
            and (not expected_id or graph_ids[:1] == [expected_id])
            and (
                _fails_closed(dme_verdict.to_dict())
                or _within_lobe_bounds(dme_verdict_dict, query.allowed_lobes, query.denied_lobes)
            )
        )
    elif query.expected_status == "safe_closed":
        ok = _fails_closed(graph_verdict.to_dict()) and _fails_closed(dme_verdict.to_dict())
    else:
        ok = (
            graph_verdict.status == query.expected_status
            and dme_verdict.status == query.expected_status
            and graph_verdict.answer_payload is None
            and dme_verdict.answer_payload is None
        )

    known_gap = False
    if not ok and query.label in KNOWN_VERDICT_GAPS:
        # The SAFETY half must still hold: the offending records themselves may
        # never surface in ANY payload — a leak is a hard failure, never a gap.
        forbidden = _forbidden_ids_for(query.label, written_by_label)
        leaked = any(
            _payload_contains(verdict, forbidden_id)
            for verdict in (graph_verdict_dict, dme_verdict_dict)
            for forbidden_id in forbidden
            if forbidden_id
        )
        known_gap = not leaked

    return {
        "label": query.label,
        "text": query.text,
        "expected_id": expected_id,
        "expected_status": query.expected_status,
        "success_mode": query.success_mode,
        "graph_top_ids": graph_ids,
        "dme_top_ids": dme_ids,
        "graph_expected_rank": _rank(graph_ids, expected_id),
        "dme_expected_rank": _rank(dme_ids, expected_id),
        "graph_packet_hash": _hash_value(_normalize_paths(packet.to_dict(), root)),
        "graph_verdict": graph_verdict_dict,
        "dme_verdict": dme_verdict_dict,
        "dme_projection_unavailable": dme_projection_unavailable,
        "ok": ok,
        "known_gap": known_gap,
    }


def _scoring_report(query_reports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    reports = list(query_reports)
    total_expected = len(reports)
    verified_expected = sum(
        1
        for item in reports
        if item.get("expected_status") == "verified"
        and (item.get("graph_verdict") or {}).get("status") == "verified"
        and (item.get("dme_verdict") or {}).get("status") == "verified"
    )
    safe_fail_closed = {
        status: sum(
            1
            for item in reports
            if (item.get("graph_verdict") or {}).get("status") == status
            and (item.get("dme_verdict") or {}).get("status") == status
        )
        for status in ("unknown", "ambiguous", "conflict")
    }
    safe_fail_closed["safe_closed"] = sum(
        1 for item in reports if item.get("expected_status") == "safe_closed"
    )
    return {
        "verified_expected": verified_expected,
        "total_expected": total_expected,
        "ratio": f"{verified_expected}/{total_expected}",
        "safe_fail_closed": safe_fail_closed,
        "recall_success_excludes_safe_fail_closed": True,
    }


def _boundary_assertions(query_reports: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    by_label = {str(item.get("label") or ""): item for item in query_reports}
    errors: list[str] = []
    freeform = by_label.get("freeform_boundary_query") or {}
    if (freeform.get("graph_verdict") or {}).get("status") != "verified":
        errors.append("freeform boundary did not verify allowed public lobes")
    for source in ("graph_verdict", "dme_verdict"):
        candidates = ((freeform.get(source) or {}).get("answer_payload") or {}).get(
            "candidates"
        ) or []
        lobes = {int((item.get("coord") or [None])[0]) for item in candidates if item.get("coord")}
        if lobes & {4, 6, 7, 11}:
            errors.append(
                f"{source} leaked excluded situational/journal/vision/dev lobes: {sorted(lobes)}"
            )
    known_gaps: list[str] = []
    journal = by_label.get("journal_exclusion_query") or {}
    if not _fails_closed(journal.get("graph_verdict") or {}):
        if journal.get("known_gap"):
            known_gaps.append(
                "journal/deepsleep fail-closed verdict (post-Slice-4b "
                "verdict-grounding gap — journal content itself did NOT leak)"
            )
        else:
            errors.append("journal/deepsleep exclusion did not fail closed by default")
    # THE RECALL-LANE RULE (operator 2026-07-18): journal (6) and vision (7) are
    # structurally outside the query-recall lane — an EXPLICIT allow must fail
    # closed on BOTH lanes. (This assertion INVERTED with the ruling: it used to
    # require the explicit journal ask to verify.)
    for label, what in (
        ("journal_explicit_query", "metacognitive journal"),
        ("browser_upload_query", "vision"),
    ):
        item = by_label.get(label) or {}
        for source in ("graph_verdict", "dme_verdict"):
            if not _fails_closed(item.get(source) or {}):
                errors.append(f"{label}:{source} breached the {what} recall boundary")
    return {"ok": not errors, "errors": errors, "known_gaps": known_gaps}


def _mutate_canonical_file(root: Path, expected_record: Mapping[str, Any]) -> None:
    path = root / str(expected_record["path"])
    data = json.loads(path.read_text(encoding="utf-8"))
    provenance = dict(data.get("provenance") or {})
    provenance["span_hash"] = "tampered-span-hash"
    data["provenance"] = provenance
    data["text"] = "TAMPERED canonical text that should not verify."
    path.write_text(canonical_json(data), encoding="utf-8")


def _mutate_source_ledger(root: Path, expected_record: Mapping[str, Any]) -> None:
    from memory.source_ledger.store import artifact_path

    source_path = artifact_path(str(expected_record["source_id"]), root=root)
    data = json.loads(source_path.read_text(encoding="utf-8"))
    content = str(data.get("content") or "")
    replacement = "X" if not content.startswith("X") else "Y"
    data["content"] = replacement + content[1:] if content else replacement
    source_path.write_text(canonical_json(data), encoding="utf-8")


def _mutate_graph_snapshot(root: Path, expected_record: Mapping[str, Any]) -> None:
    path = _published_artifact_path(root, "graph_snapshot.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("leaves", "nodes"):
        if isinstance(data.get(key), list):
            data[key] = [
                item for item in data[key] if item.get("memory_id") != expected_record["id"]
            ]
    path.write_text(canonical_json(data), encoding="utf-8")


def _mutate_dme_sram_index(root: Path, expected_record: Mapping[str, Any]) -> None:
    row_path = _published_artifact_path(root, "sram_index.json")
    rows = json.loads(row_path.read_text(encoding="utf-8"))
    for row in rows:
        if str(row.get("memory_id") or "") == str(expected_record["id"]):
            row["memory_id"] = "tampered.dme.row"
            row["title"] = "Tampered DME row"
            break
    row_path.write_text(canonical_json(rows), encoding="utf-8")
    sram_path = _published_artifact_path(root, "sram.bin")
    raw = bytearray(sram_path.read_bytes())
    if raw:
        raw[0] = (raw[0] + 1) % 256
    sram_path.write_bytes(bytes(raw))


def _published_artifact_path(root: Path, artifact: str) -> Path:
    """Return the active Program 0 artifact path for deliberate tamper probes."""
    from memory.projection.generation import current_read_context, projection_read_transaction

    with projection_read_transaction(root):
        ctx = current_read_context()
        view = ctx.view if ctx is not None else None
        if view is None:
            raise RuntimeError("determinism replay tamper probes require a published generation")
        return view.path(artifact)


def _unload_replay_dme_session(root: Path) -> None:
    """Drop only this scratch replay's soft-resident DME state after tampering."""
    from memory.dme import recall_hook

    key = recall_hook._root_key(root)
    with recall_hook._lock:
        entry = recall_hook._sessions.get(key)
        if entry is None:
            return
        if entry.refcount > 0:
            entry.stale = True
            entry.expires_at = None
            return
        recall_hook._unload_entry(entry)
        recall_hook._sessions.pop(key, None)


def _mutate_sense_registry(root: Path, expected_record: Mapping[str, Any]) -> None:
    from memory.sense_registry import sense_registry_path

    path = sense_registry_path(root=root)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")
    rows = [
        line
        for line in path.read_text(encoding="utf-8").splitlines()
        if expected_record["id"] not in line
    ]
    rows.append(canonical_json({"sense_id": "tampered", "term": "agent", "memory_id": "missing"}))
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")


def _fails_closed(verdict: Mapping[str, Any]) -> bool:
    return (
        str(verdict.get("status") or "") in {"unknown", "ambiguous", "conflict"}
        and verdict.get("answer_payload") is None
    )


def _no_payload_unless_verified(verdict: Mapping[str, Any]) -> bool:
    status = str(verdict.get("status") or "")
    if status == "verified":
        return verdict.get("answer_payload") is not None
    return _fails_closed(verdict)


def _mutation_outcome_ok(
    name: str,
    graph_verdict: Mapping[str, Any],
    dme_verdict: Mapping[str, Any],
    expected_id: str,
) -> bool:
    if name in {"canonical_file", "source_ledger"}:
        return not _payload_contains(graph_verdict, expected_id) and not _payload_contains(
            dme_verdict, expected_id
        )
    if name == "dme_sram_index":
        return _no_payload_unless_verified(graph_verdict) and not _payload_contains(
            dme_verdict, expected_id
        )
    return _no_payload_unless_verified(graph_verdict) and _no_payload_unless_verified(dme_verdict)


def _payload_contains(verdict: Mapping[str, Any], memory_id: str) -> bool:
    payload = verdict.get("answer_payload") if isinstance(verdict, Mapping) else None
    candidates = (payload or {}).get("candidates") if isinstance(payload, Mapping) else []
    return any(str(item.get("memory_id") or "") == str(memory_id) for item in candidates or [])


def _candidate_from_packet_result(result: Mapping[str, Any]) -> dict[str, Any]:
    payload = result.get("payload") if isinstance(result.get("payload"), Mapping) else {}
    return {
        "memory_id": str(result.get("node_id") or ""),
        "coord": list(payload.get("coord") or []),
        "score": float(result.get("score") or 0.0),
        "title": str(result.get("label") or ""),
        "summary": str(payload.get("summary") or ""),
        "domain": payload.get("domain"),
        "subtopic": payload.get("subtopic"),
        "tags": list(payload.get("tags") or []),
    }


def _projection_fingerprints(root: Path, written: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    memory_root = root / "memory"
    source_dir = memory_root / "source_ledger" / "artifacts"
    from memory.projection.generation import current_read_context, projection_read_transaction

    written_paths = [root / str(item["path"]) for item in written]
    with projection_read_transaction(root):
        ctx = current_read_context()
        view = ctx.view if ctx is not None else None
        if view is None:
            raise RuntimeError("determinism replay requires a published Program 0 generation")
        coord_index = json.loads(view.read_bytes("coord_index.snapshot.json").decode("utf-8"))
        snapshot_hash = view.manifest.content.graph_snapshot_hash
        return {
            "canonical_records": _hash_files(root, written_paths),
            "source_ledger": _hash_files(root, sorted(source_dir.glob("*.json"))),
            "coord_index_normalized": _hash_value(_normalize_paths(coord_index, root)),
            "graph_snapshot": _hash_bytes(view.read_bytes("graph_snapshot.json")),
            "latest_snapshot_hash": _hash_bytes((snapshot_hash + "\n").encode("utf-8")),
            "dme_sram": _hash_bytes(view.read_bytes("sram.bin")),
            "dme_meta": _hash_bytes(view.read_bytes("sram_meta.json")),
            "dme_row_index": _hash_bytes(view.read_bytes("sram_index.json")),
            "dme_manifest": _hash_bytes(view.read_bytes("dme_manifest.json")),
            "dme_coordinate_registry": _hash_bytes(view.read_bytes("coordinate_registry.json")),
        }


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@contextmanager
def _isolated_memory_modules(root: Path):
    import core.paths as core_paths
    from core import timekeeper
    from memory.engine import validate_write, walker
    from memory import retina, subdomain_flag

    # Writable replay profiles do not contain the product's pinned model.
    # Seat the required process-resident encoder before redirecting OMEGA_ROOT.
    retina.encoder()

    memory_root = root / "memory"
    saved = {
        "core_omega_root": core_paths.OMEGA_ROOT,
        "guardian_state_root": core_paths.GUARDIAN_STATE_ROOT,
        "timekeeper_state": timekeeper._state,
        "timekeeper_landed_floor": timekeeper._landed_floor,
        "timekeeper_last_seq": timekeeper._last_seq,
        "validate_memory_root": validate_write.MEMORY_ROOT,
        "validate_tmp_dir": validate_write.TMP_DIR,
        "validate_index_path": validate_write.INDEX_PATH,
        "walker_memory_root": walker.MEMORY_ROOT,
        "walker_index_path": walker.INDEX_PATH,
        "walker_mtime": walker._coord_index_mtime,
        "walker_generation": walker._coord_index_generation,
        "dynamic_raw_cache": subdomain_flag._RAW_CACHE,
        "dynamic_flag_cache": subdomain_flag._FLAG_CACHE,
    }
    core_paths.OMEGA_ROOT = root
    core_paths.GUARDIAN_STATE_ROOT = root / "guardian" / "state"
    timekeeper.reset_for_tests()
    validate_write.MEMORY_ROOT = memory_root
    validate_write.TMP_DIR = memory_root / "tmp_writes"
    validate_write.INDEX_PATH = memory_root / "indexes" / "coord_index.json"
    walker.MEMORY_ROOT = memory_root
    walker.INDEX_PATH = memory_root / "indexes" / "coord_index.json"
    subdomain_flag.reset_flag_cache()
    _reset_walker_index(walker)
    try:
        yield
    finally:
        core_paths.OMEGA_ROOT = saved["core_omega_root"]
        core_paths.GUARDIAN_STATE_ROOT = saved["guardian_state_root"]
        with timekeeper._LOCK:
            timekeeper._state = saved["timekeeper_state"]
            timekeeper._landed_floor = saved["timekeeper_landed_floor"]
            timekeeper._last_seq = saved["timekeeper_last_seq"]
        validate_write.MEMORY_ROOT = saved["validate_memory_root"]
        validate_write.TMP_DIR = saved["validate_tmp_dir"]
        validate_write.INDEX_PATH = saved["validate_index_path"]
        walker.MEMORY_ROOT = saved["walker_memory_root"]
        walker.INDEX_PATH = saved["walker_index_path"]
        walker._coord_index_mtime = saved["walker_mtime"]
        walker._coord_index_generation = saved["walker_generation"]
        subdomain_flag._RAW_CACHE = saved["dynamic_raw_cache"]
        subdomain_flag._FLAG_CACHE = saved["dynamic_flag_cache"]
        _reset_walker_index(walker)


def _reset_walker_index(walker_module: Any) -> None:
    with walker_module._COORD_INDEX_LOCK:
        walker_module.COORD_INDEX.clear()
    walker_module._coord_index_mtime = 0.0


def _normalize_paths(value: Any, root: Path) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _normalize_paths(val, root) for key, val in sorted(value.items())}
    if isinstance(value, tuple):
        return [_normalize_paths(item, root) for item in value]
    if isinstance(value, list):
        return [_normalize_paths(item, root) for item in value]
    if isinstance(value, str):
        return _normalize_path_string(value, root)
    return value


def _normalize_path_string(value: str, root: Path) -> str:
    try:
        path = Path(value)
    except (OSError, ValueError):
        return value
    if not path.is_absolute():
        return value
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return value


def _relative_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _rank(ids: list[str], expected_id: str) -> int | None:
    try:
        return ids.index(expected_id) + 1
    except ValueError:
        return None


def _report_hash(report: Mapping[str, Any]) -> str:
    comparable = {key: value for key, value in report.items() if key not in {"type", "errors"}}
    return _hash_value(comparable)


def _hash_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _hash_file(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash_files(root: Path, paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((Path(p) for p in paths), key=lambda p: _relative_path(p, root)):
        rel = _relative_path(path, root)
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.exists() else b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _read_json(path: Path) -> Any:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))

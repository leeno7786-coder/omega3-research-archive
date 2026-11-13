"""
Memory API — Canonical Adapter Layer
======================================

The single surface that the cognitive system uses to talk to memory.
Everything that crosses the boundary between cognition and persistence
goes through these functions.

Public API:

    read_memory(query)          -> RetrievalResult  (verdict-gated by default)
    recall_verified(query)      -> RecallVerdict
    write_memory(envelope, propose) -> WriteReceipt
    promote_memory(staging_ref, evidence_refs, approver_id)
                                    -> PromotionResult
    rollback_memory(staging_ref)    -> dict
    decay_tick(now_ts)              -> dict
    resolve_contradiction(...)      -> ResolutionResult
    memory_health()                 -> MemoryHealth

Contract guarantees:

    - Every call requires a non-empty `cycle_id`. Missing or empty
      cycle_id raises `ValueError`.
    - Every recall touches the StrengthIndex for retrieved memories
      (reconsolidation). Diagnostic peeks should call walker_query
      directly instead.
    - Fresh writes enter through `intake_memory` and remain in episode SRAM
      until DeepSleep drains them. `write_memory` is the drain-only commit
      seam; its runtime guard rejects direct producer calls.
    - DeepSleep commits cognitive self-writes Guardian-free. Guardian remains
      the untrusted external human-authored ingress boundary.
    - `propose=True` is reserved for explicit user-chat ingress and
      stages the envelope for Guardian arbitration.

Architectural rules:
    - Schemas live in `schema.phase_io` (for phase I/O) and
      `schema/memory_schema.json` (for persisted memory).
      The dataclasses below are API-shape only and never persisted.
    - Paths derive from `core.paths.OMEGA_ROOT` — no env vars, no
      relative paths.
    - This module MUST NOT be redefined elsewhere.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from core.paths import OMEGA_ROOT
from core.symphony_time import now_iso
from memory.promotion import (
    create_staging,
    commit_promotion,
    rollback_promotion,
    list_staged,
)
from memory.consolidation import run_consolidation_once

log = logging.getLogger("omega.memory.api")


_USER_CHAT_SOURCE_PREFIXES = (
    "user_chat",
    "user:",
    "ui:operator",
    "operator_chat",
    "hud.chat",
    "hud.operator_chat",
    "brain.user_chat",
)


# =====================================================================
# API dataclasses (shape only — never persisted)
# =====================================================================


@dataclass
class QuerySpec:
    """Input to read_memory. cycle_id required.

    Lane-discipline filters (per project_lane_discipline_doctrine):
        allowed_lobes — only return candidates whose coord[0] is in
                        this list. Populated by the recall route
                        (auto_loop.mril_tools) from the remember-tool
                        registry so each query's recall stays in one
                        lane. None = no constraint (legacy callers);
                        [] = explicit empty deny-all.
        denied_lobes  — explicit deny on these lobes regardless of
                        allowed_lobes. None = the default deny
                        (6, 11) (journal + dev) so recall never
                        sees Omega-private journal entries or dev
                        artifacts unless the scope names those lanes;
                        [] = explicitly deny nothing. Deny takes
                        precedence over allow.
        allowed_subdomains — when set, recall seeds/scopes to these Y
                        values within the allowed lobe(s) and skips the
                        SetFit classifier re-seed (deterministic MRIL).
    """

    cycle_id: str
    text: str
    episode_id: Optional[str] = None
    modality: str = "text"
    task_scope: Optional[str] = None
    recall_policy_id: Optional[str] = None
    domain: Optional[str] = None
    limit: int = 10
    allowed_lobes: Optional[List[int]] = None
    denied_lobes: Optional[List[int]] = None
    allowed_subdomains: Optional[List[int]] = None


@dataclass
class RetrievalCandidate:
    id: str
    coord: List[int]
    score: float
    provenance: Dict[str, Any] = field(default_factory=dict)
    title: Optional[str] = None
    summary: Optional[str] = None
    # Verified canonical text is retained for field-specific answer shaping.
    # Ordinary recall still renders summary/title; plugin and artifact records
    # need their bounded evidence excerpt to answer what actually happened.
    text: Optional[str] = None
    domain: Optional[str] = None
    subtopic: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    contradiction: Optional[str] = None
    # Canonical-file recall can recover a cited record while graph/DME
    # projections are cold.  The path is carried explicitly to the verifier;
    # it is never treated as proof by itself.
    path: Optional[str] = None
    filesystem_fallback: bool = False
    semantic_recall: bool = False
    # Guardian flag info (set when commit_flagged ran on this memory).
    # The voice_box reads flag_response_mode and renders the matching
    # ResponseClass posture instead of treating the memory as plain
    # knowledge — this is what binds recall to the immune response.
    flagged: bool = False
    flag_reason: Optional[str] = None
    flag_response_mode: Optional[str] = None
    # True when this candidate was reached by walker.expand_links
    # following an entity_linker .links edge from a seed candidate.
    # The recall route uses this flag to ensure preview/snippet
    # building surfaces relationship-walked records to voice_box even
    # when they score below the score-ranked top-N — the explicit edge
    # IS the relevance signal the score model can't reflect.
    via_link: bool = False
    # Revision history attached at recall time when this candidate is
    # the HEAD of a supersedes chain. Each entry is a dict with keys
    # {id, version, title, summary, created_at}, oldest-first. Voice
    # box uses this to articulate "X (was Y)" without the model having
    # to detect the revision itself by comparing peer candidates.
    history: List[Dict[str, Any]] = field(default_factory=list)
    version: int = 1
    # Final-score alias kept under the legacy name for downstream
    # consumers that still walk `candidate.rerank_score`. After BGE
    # retirement (2026-05-08, walker arbitration replaced cross-encoder),
    # this field carries the lane-arbitration `final_score` — same number
    # the walker uses to rank, exposed under both names. New consumers
    # should read `meta["arbitration"]["top"][i]["final_score"]`.
    rerank_score: Optional[float] = None
    # Cut 3 (memory/recall_basis.py): id of the RecallBasis derived for this
    # candidate inside the DME generation that scored it — a routing/topicality
    # proof the verifier independently revalidates. NEVER source proof; it
    # lives beside provenance, not inside source_proof / evidence_strength.
    recall_basis_id: Optional[str] = None
    # Host-verified SourceLedger proof survives typed RetrievalResult paths so
    # remember_* can carry its citation into answer context.
    source_proof: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    cycle_id: str
    candidates: List[RetrievalCandidate]
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CanonicalEnvelope:
    """Pre-persistence shape used by write_memory.

    NOT a schema-valid memory dict. The promotion pipeline translates
    this to the canonical schema-valid form at staging time via
    `memory.core.build_memory_dict`.
    """

    cycle_id: str
    coord: List[int]
    type: str
    payload: Dict[str, Any]  # text, title, summary, tags, domain, etc.
    source: str = "memory.api"
    confidence: float = 0.5
    provenance: List[str] = field(default_factory=list)
    id: Optional[str] = None
    timestamp_created: Optional[str] = None
    decay_policy: Optional[str] = None
    contradiction_state: Optional[str] = None
    permissions: Dict[str, Any] = field(default_factory=dict)
    version: int = 1
    strength_index: float = 0.5


@dataclass
class WriteReceipt:
    cycle_id: str
    staging_ref: Optional[str]
    path: Optional[str]
    validation_status: str  # "held" | "staged" | "committed" | "rejected"
    message: Optional[str] = None
    # Cut 4 (memory/projection_delta.py): native citizenship posture. A
    # committed write may be canonically valid while its DME projection is
    # still pending — that lag is legal and named here, never hidden.
    projection_pending: bool = False
    projection_delta_id: Optional[str] = None
    projection_dirty: bool = False
    # Durable-ingress provenance. A held receipt is truthful only after its
    # intake journal row has been fsynced; a committed receipt is durable by
    # canonical write. The event and journal identities are distinct.
    durable: bool = False
    journal_record_id: Optional[str] = None
    event_id: Optional[str] = None
    replayed: bool = False


_INTAKE_ACCEPTED_STATUSES = frozenset({"held", "committed"})


def intake_receipt_accepted(receipt: WriteReceipt) -> bool:
    """Return whether an intake joined the live SRAM-to-filesystem lane.

    ``held`` means the envelope is durably owned by the current episode bucket;
    ``committed`` is possible only while DeepSleep is draining that bucket.
    Staging is not this lane, and rejection is never success.
    """
    return str(getattr(receipt, "validation_status", "") or "") in _INTAKE_ACCEPTED_STATUSES


@dataclass
class PromotionResult:
    cycle_id: str
    staging_ref: str
    committed: bool
    canonical_ref: Optional[str] = None
    rollback_plan: Optional[Dict[str, Any]] = None
    reasons: Optional[List[str]] = None


@dataclass
class ResolutionResult:
    canonical_ref: str
    resolved: bool
    notes: str = ""
    evidence_refs: List[str] = field(default_factory=list)
    # AUDIT-3: ids that were durably appended to the deepsleep
    # candidate queue. Empty when nothing got queued (validation
    # failed, no valid evidence, etc). Lets callers verify the
    # queueing happened without re-reading the journal file.
    queued_to: List[str] = field(default_factory=list)
    # Subset of evidence_refs that did NOT resolve to a real memory
    # file. Surfaced so callers can flag corrupt input upstream.
    invalid_evidence_refs: List[str] = field(default_factory=list)


@dataclass
class MemoryHealth:
    snapshot_at: str
    coord_index_path: str
    coord_index_exists: bool
    strength_records: int
    strength_dirty: int
    staging_count: int
    issues: List[str] = field(default_factory=list)
    # Compatibility-named semantic-substrate state. Boot reports it only after
    # the in-process Ettin encoder is seated; health overlays the cached verdict
    # with the live retina resident flag. No runtime embedding worker is involved.
    embedding_substrate: str = "unknown"  # "ok" | "degraded" | "failed" | "unknown"
    embedding_substrate_detail: str = ""  # human-readable reason / endpoint / model
    # Cut 4 (memory/projection_delta.py): native projection backlog. Lag is
    # legal and must be NAMED (16 §6) — a nonzero backlog is reported, never
    # hidden; a dirty marker means "rebuild required before claiming current".
    # No active-generation / watermark field here: Cut 13 owns generation
    # identity. Embedding health above does not gate these fields.
    projection_pending_count: int = 0
    projection_malformed_count: int = 0
    projection_dirty: bool = False
    projection_dirty_reason: str = ""
    native_citizenship_enabled: bool = False
    # Memory-organ Cut 13 (D-13-14/49/58/64/71/74/77; 16 §6): the active generation and its
    # coverage. ``projection_current`` is NEVER True while lag > 0, pending > 0, dirty, an
    # obligation is open, an intent is live, or the lane names any reason. ``projection_generation``
    # is "legacy" when NO manifest exists, the generation id when it is served, "unavailable"
    # when a manifest exists but cannot be served (invalid manifest is never legacy).
    projection_generation: str = "legacy"
    semantic_dme_lane: str = "legacy"                 # legacy | available | unavailable
    semantic_dme_lane_reason: str = ""
    projection_current: bool = False
    projection_watermark_commit_seq: Optional[int] = None
    projection_latest_canonical_commit_seq: Optional[int] = None
    projection_lag_commits: Optional[int] = None
    projection_head_clock: Optional[dict] = None
    projection_live_clock: Optional[dict] = None
    projection_reserved_seq: Optional[int] = None
    projection_landed_seq: Optional[int] = None
    projection_coverage_seal: Optional[dict] = None
    projection_unsequenced_pending: int = 0
    projection_unsequenced_posture: str = "covered_by_full_rebuild"
    projection_oldest_pending_age_s: Optional[float] = None
    projection_blocked_on_obligations: bool = False
    projection_page0_utilization: dict = field(default_factory=dict)
    projection_live_intents: List[str] = field(default_factory=list)
    projection_program0: Optional[dict] = None     # bootstrap state / epoch / cursor / malformed receipts / recovery pending
    projection_pending_partial_bytes: int = 0      # unterminated trailing outbox fragment beyond the settled cursor (pending, never parsed)


# =====================================================================
# Helpers
# =====================================================================


def _require_cycle_id(cycle_id: Optional[str]) -> None:
    if not cycle_id or not isinstance(cycle_id, str):
        raise ValueError("memory API requires non-empty cycle_id")


def _envelope_to_dict(envelope: CanonicalEnvelope) -> Dict[str, Any]:
    """Serialise the envelope dataclass for the promotion pipeline."""
    return {
        "cycle_id": envelope.cycle_id,
        "id": envelope.id,
        "coord": list(envelope.coord),
        "type": envelope.type,
        "payload": dict(envelope.payload or {}),
        "source": envelope.source,
        "confidence": float(envelope.confidence),
        "provenance": list(envelope.provenance or []),
        "timestamp_created": envelope.timestamp_created or now_iso(),
        "decay_policy": envelope.decay_policy,
        "contradiction_state": envelope.contradiction_state,
        "permissions": dict(envelope.permissions or {}),
        "version": int(envelope.version),
        "strength_index": float(envelope.strength_index),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_user_chat_source(source: str) -> bool:
    """True only for external user-chat ingress sources."""
    src = str(source or "").strip().lower()
    for prefix in _USER_CHAT_SOURCE_PREFIXES:
        if prefix.endswith(":"):
            if src.startswith(prefix):
                return True
            continue
        if src == prefix or src.startswith(prefix + ":"):
            return True
    return False


def _strength_index_state() -> Dict[str, int]:
    from memory.strength_index import ensure_strength_index

    si = ensure_strength_index()
    with si._lock:  # type: ignore[attr-defined]
        return {
            "records": len(si._records),  # type: ignore[attr-defined]
            "dirty": int(si._dirty),  # type: ignore[attr-defined]
        }


def _active_profile_root() -> Path:
    """Return the profile root owned by the active filesystem walker.

    The walker is Omega's canonical filesystem authority and is deliberately
    redirectable for isolated profiles/tests.  Recall projections, canonical
    fallback, and verification must all resolve against that same root; mixing
    a redirected walker with the process-level ``OMEGA_ROOT`` can leak records
    across profiles.
    """
    try:
        from memory.engine import walker as memory_walker

        memory_root = Path(memory_walker.MEMORY_ROOT)
        if memory_root.name == "memory":
            return memory_root.parent
    except Exception:
        pass
    return OMEGA_ROOT


# =====================================================================
# read_memory
# =====================================================================


def walker_query(query_spec):
    """Diagnostic legacy walker adapter.

    Cognitive reads use the v2 taxonomy recall path in ``read_memory``. This
    re-export keeps old diagnostic/tests able to monkeypatch or call the
    side-effect-free walker adapter directly.
    """
    from memory.engine.walker import walker_query as _walker_query

    return _walker_query(query_spec)


def _canonical_path_for_commit(memory_id: str, commit_info: Mapping[str, Any] | None) -> Optional[Path]:
    """Resolve the canonical file a commit landed at: walker COORD_INDEX first,
    else the commit's own target_path. None when unknown. Never raises."""
    info = dict(commit_info or {})
    try:
        from memory.engine.walker import COORD_INDEX, load_coord_index, resolve_coord_index_path

        load_coord_index()
        entry = COORD_INDEX.get(memory_id) or {}
        path = resolve_coord_index_path(entry.get("path")) if entry.get("path") else None
    except Exception:  # noqa: BLE001
        path = None
    if path is None:
        raw_target = info.get("target_path")
        path = Path(raw_target) if raw_target else None
    return path


_UNPROTECTED_HOOK_REPORT = {
    "ran": False,
    "skipped": True,
    "reason": "no durable recovery channel for the hook window (journal and dirty marker both failed)",
    "supersedes": None,
    "entity_links": 0,
    "mutation_uncertain": False,
    "errors": [],
}


def _open_projection_obligation(memory_id: str, commit_info: Mapping[str, Any] | None, *, owner: str) -> Dict[str, Any]:
    """Cut 4 rework 2/3: BEFORE the write owner runs its canonical-mutating
    post-commit hooks, journal a durable projection obligation for a native
    commit that has projection work pending, and return the ACTUAL durability
    posture {"journal_id", "channel": wal|dirty|none|not_applicable,
    "durable", "hooks_allowed"}.

    ``hooks_allowed`` is the owner's law (CUT4_REVIEW_REWORK_3 §3): the
    canonical-mutating hook window may be entered only when a durable recovery
    fact covers it — an open obligation entry OR a durable rebuild-required
    marker. With NEITHER, the already-valid canonical write is preserved and
    the mutating hooks are NOT run (skipping hooks keeps the commit-time delta
    truthful: no bytes change). Not applicable (legacy / no projection work /
    idempotent) → hooks allowed as before. Never raises."""
    info = dict(commit_info or {})
    posture: Dict[str, Any] = {"journal_id": None, "channel": "not_applicable", "durable": False, "hooks_allowed": True}
    if not memory_id or not bool(info.get("projection_pending", False)):
        return posture
    try:
        from memory.projection_delta import open_projection_obligation

        path = _canonical_path_for_commit(memory_id, info)
        if path is None:
            # cannot even locate the canonical file → cannot protect the window
            posture.update(channel="none", hooks_allowed=False)
        else:
            got = open_projection_obligation(memory_id, path, owner)
            posture.update(journal_id=got.get("journal_id"), channel=got.get("channel"),
                           durable=bool(got.get("durable")), hooks_allowed=bool(got.get("durable")))
    except Exception as exc:  # noqa: BLE001
        log.error("projection obligation open failed for %s (%s)", memory_id, exc)
        posture.update(channel="none", hooks_allowed=False)
    if not posture["hooks_allowed"]:
        log.critical(
            "write owner %s: hook window for %s is UNPROTECTED (no durable recovery channel) — "
            "canonical write preserved; canonical-mutating post-commit hooks SKIPPED",
            owner, memory_id,
        )
        try:
            from guardian.terminal_feedback import emit_data

            emit_data("memory", "post_commit_hooks", status="skipped", ok=False,
                      reason="unprotected_hook_window", memory_id=memory_id, owner=owner)
        except Exception:  # noqa: BLE001
            pass
    return posture


def _native_projection_posture(
    memory_id: str,
    commit_info: Mapping[str, Any] | None,
    *,
    hook_report: Mapping[str, Any] | None,
    obligation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Cut 4 rework: after the write owner's post-commit hooks have run, publish
    the FINAL canonical state as projection work and return the receipt
    posture {projection_pending, projection_delta_id, projection_dirty}.
    Starts from the commit-time posture (commit_info) and, when native
    projection work is pending, refreshes it from publish_final_projection_state
    (final post-hook bytes; superseded prior represented; peer mutation →
    dirty). Rework 2: settles (closes) the owner's projection obligation ONLY
    when that final publication is durable or durable dirty evidence exists;
    otherwise the obligation stays open for boot. Never raises; never claims
    currentness."""
    info = dict(commit_info or {})
    posture = {
        "projection_pending": bool(info.get("projection_pending", False)),
        "projection_delta_id": info.get("projection_delta_id") or None,
        "projection_dirty": bool(info.get("projection_dirty", False)),
    }
    if not posture["projection_pending"] or not memory_id:
        return posture
    try:
        from memory.projection_delta import publish_final_projection_state, settle_projection_obligation

        path = _canonical_path_for_commit(memory_id, info)
        if path is None:
            return posture
        final = publish_final_projection_state(memory_id, path, hook_report=hook_report)
        if final.get("delta_id"):
            posture["projection_delta_id"] = final["delta_id"]
        if final.get("projection_dirty"):
            posture["projection_dirty"] = True
        settle_projection_obligation(obligation_id, memory_id, final)
    except Exception as exc:  # noqa: BLE001 — receipt degradation only
        log.warning("native projection posture refresh skipped for %s (%s)", memory_id, exc)
    return posture


def _recall_basis_id_of(row: Any) -> Optional[str]:
    """Cut 3: the recall_basis_id a candidate row carries — explicit field first,
    else the DME merge's provenance annotation. None when absent."""
    if not isinstance(row, Mapping):
        return None
    explicit = row.get("recall_basis_id")
    if explicit:
        return str(explicit)
    prov = row.get("provenance")
    if isinstance(prov, Mapping) and prov.get("recall_basis_id"):
        return str(prov.get("recall_basis_id"))
    return None


def _compile_routing_cue_receipt(
    query: Any,
    recall_plan: Any,
    profile_root: Path | str | None,
    *,
    generator: Any = None,
) -> Any:
    """Cut 2: one bounded CueReceipt per recall, compiled from the already
    declared RecallPlan (deterministic + entity terms) plus — only behind
    OMEGA_DME_LANGUAGE_CUES — the optional language-organ generator, which
    receives nothing but (query_text, max_terms).

    Read-only against the CoordinateRegistry (lookup/near_matches). Never
    mutates ``query``. Fail-open: any error → None and the DME lane runs on
    its legacy token resolution alone."""
    from memory.dewey_map import NON_RECALLABLE_LOBES
    from memory.dme.coordinate_registry import load_registry
    from memory import routing_cues as rc

    text = str(getattr(query, "text", "") or "")
    generated: tuple = ()
    generator_used = False
    generator_version = ""
    if rc.language_cues_enabled():
        gen = generator if generator is not None else rc.default_cue_generator()
        if gen is not None:
            generator_used = True
            generated, generator_version = rc.generate_terms(
                text, generator=gen, version=rc.default_cue_generator_version() or None
            )
    denied = set(query.denied_lobes if query.denied_lobes is not None else (6, 11))
    denied |= set(NON_RECALLABLE_LOBES)
    return rc.build_cue_receipt(
        query_text=text,
        deterministic_terms=rc.deterministic_terms_from_plan(recall_plan),
        entity_terms=rc.entity_terms_from_plan(recall_plan),
        generated_terms=generated,
        registry=load_registry(profile_root),
        allowed_lobes=query.allowed_lobes,
        denied_lobes=denied,
        generator_used=generator_used,
        generator_version=generator_version,
    )


def _independent_recall_evidence(payload: Mapping[str, Any]) -> bool:
    """Recall consumers must not treat Omega's own inference as external evidence."""
    from memory.cognition_write import is_independent_evidence

    source_type = payload.get("source_type")
    if not source_type:
        nested = payload.get("provenance")
        if isinstance(nested, Mapping):
            source_type = nested.get("source_type")
    return is_independent_evidence(str(source_type or ""))


def read_memory(query: QuerySpec, *, verdict_gate: bool = True) -> RetrievalResult:
    """Run a deterministic retrieval. Touches StrengthIndex on every hit
    (reconsolidation) — diagnostic peeks should call walker_query
    directly to avoid the side effect.

    When ``verdict_gate`` is True (default), only VERIFIED canonical hits
    are returned upstream; non-verified outcomes are empty with verdict
    metadata attached (INV-MEM-007).

    Memory-organ Cut 13 (D-13-19/28): the whole read runs inside ONE reentrant
    ``projection_read_transaction`` — every derived surface (taxonomy graph,
    routing-cue registry, DME session, hop planes) is bound to one generation
    view for the operation's lifetime; ``recall_verified`` opens the outer
    transaction so basis verification stays on the same generation. When a
    manifest exists but the generation cannot be served, its reason remains
    explicit. A required perception pass that cannot run raises
    ``RetinaUnavailable`` rather than returning a successful combined recall.
    """
    _require_cycle_id(query.cycle_id)
    profile_root = _active_profile_root()
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(profile_root) as projection_ctx:
        return _read_memory_in_transaction(query, verdict_gate=verdict_gate, profile_root=profile_root, projection_ctx=projection_ctx)


def _projection_lane_meta(projection_ctx) -> dict:
    if projection_ctx is None:
        return {}
    if projection_ctx.mode == "generation":
        return {"projection_lane": "generation", "projection_generation_id": projection_ctx.generation_id}
    if projection_ctx.mode == "unavailable":
        return {"projection_lane": f"unavailable({projection_ctx.reason})", "projection_generation_id": None}
    return {"projection_lane": "legacy", "projection_generation_id": None}


def _read_memory_in_transaction(query: QuerySpec, *, verdict_gate: bool, profile_root: Path, projection_ctx) -> RetrievalResult:
    from memory.projection.generation import GenerationIntegrityError
    from memory.retina import RetinaUnavailable
    lane_unavailable_reason: str | None = None
    # Compile recall plan (posture-aware query prior)
    # Inline import avoids circular dependency at module load time.
    try:
        from memory.recall_compiler import compile_recall_plan
        from memory.recall_receipt import build_recall_receipt
        from memory.evidence_set import EvidenceSet

        recall_plan = compile_recall_plan(query)
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "recall_plan_compilation_failed",
            ok=False,
            error=str(exc),
        )
        log.warning("read_memory: recall plan compilation skipped (%s)", exc)
        recall_plan = None

    from memory.taxonomy import recall as taxrecall

    from guardian.terminal_feedback import phase_span

    with phase_span("memory", "taxonomy_recall", cycle_id=query.cycle_id):
        try:
            packet = taxrecall.recall(
                query=query.text,
                allowed_lobes=query.allowed_lobes,
                denied_lobes=(query.denied_lobes if query.denied_lobes is not None else (6, 11)),
                allowed_subdomains=query.allowed_subdomains,
                lane_weights=recall_plan.lane_weights if recall_plan else None,
                limit=query.limit or 10,
                root=profile_root,
                touch=False,  # read_memory will touch the strength index during post-passes
            )
        except GenerationIntegrityError as exc:
            # Cut 13: the derived lane is unavailable — canonical recall + verifier continue.
            from memory.taxonomy.schemas import RecallPacket

            lane_unavailable_reason = str(exc)
            log.warning("read_memory: projection lane unavailable, continuing on canonical truth (%s)", exc)
            packet = RecallPacket(query=query.text, query_hash="", query_kind="unavailable", snapshot_hash="", results=())

    # Phase 4 cut 12 — the graph half of the episode's snapshot-read pin. The
    # compiled taxonomy is forward-only: a recompile REPLACES the snapshot this
    # episode pinned at admission, and there is no earlier graph left to read.
    # So the recall is served the next snapshot and the divergence is recorded
    # into the receipt. Observational: this changes nothing about what came
    # back, only whether the episode can tell that its memory moved mid-thought.
    from memory import epoch as memory_epoch

    memory_epoch.observe(memory_epoch.SURFACE_GRAPH, str(packet.snapshot_hash or ""))

    truncation_receipts = [
        dict(receipt)
        for receipt in (getattr(packet, "truncation_receipts", ()) or ())
        if isinstance(receipt, dict)
    ]
    candidates_raw = []
    for r in packet.results:
        payload = r["payload"] or {}
        candidates_raw.append(
            {
                "id": r["node_id"],
                "coord": payload.get("coord"),
                "score": r["score"],
                "title": r["label"],
                "summary": payload.get("summary"),
                "text": payload.get("text") or payload.get("text_prefix") or "",
                "tags": payload.get("tags") or [],
                "domain": payload.get("domain"),
                "subtopic": payload.get("subtopic"),
                "contradiction": payload.get("contradiction"),
                "flagged": payload.get("flagged", False),
                "flag_reason": payload.get("flag_reason"),
                "flag_response_mode": payload.get("flag_response_mode"),
                "via_link": payload.get("via_link", False),
                "version": payload.get("version", 1),
                "provenance": {
                    "evidence": list(r["evidence_event_ids"]),
                    "independent_evidence": _independent_recall_evidence(payload),
                    **{
                        key: payload.get(key)
                        for key in (
                            "source_id",
                            "source_type",
                            "source_content_hash",
                            "source_session_id",
                            "source_session_timestamp",
                        )
                        if payload.get(key) is not None
                    },
                },
            }
        )

    # Perception before consolidation is now STRUCTURAL (operator 2026-07-16): a fact stated
    # this turn is held in the per-episode SRAM bucket (memory.episode_sram) and only lands
    # in system memory at DeepSleep/close — after this turn's recall. So recall never sees
    # this turn's own intake, and no recall-time exclusion filter is needed here.

    # DME recall hook: load packs → merge DME candidates → unload.
    # Lifecycle is tied to this recall call (refcount-safe for nested use).
    # Omega parity (fail-open): DME lane failures degrade to graph/lexical
    # candidates only; the verifier still gates output. Do not abort recall.
    dme_meta: dict = {}
    from core.paths import MEMORY_ROOT

    # Cut 2: bounded symbolic routing cues — compiled AFTER the RecallPlan and
    # BEFORE the DME merge, from declared plan structure (+ optional shadow
    # generator). Routing only: CUE != CLAIM. Fail-open to legacy resolution.
    cue_receipt = None
    try:
        cue_receipt = _compile_routing_cue_receipt(query, recall_plan, MEMORY_ROOT)
    except Exception as exc:
        log.warning("read_memory: routing cue compilation skipped (%s)", exc)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data("memory", "routing_cue_compilation_failed", ok=False, error=str(exc))
        except Exception:
            pass
        cue_receipt = None

    try:
        # Import the submodule directly (not via memory.dme package exports) so a
        # heavy adapter import failure cannot hide a present recall_hook module.
        from memory.dme.recall_hook import (
            DmeRecallHookStats,
            apply_dme_recall_hook,
            last_recall_hook_stats,
            record_recall_hook_stats,
        )
    except ImportError as exc:
        log.warning(
            "read_memory: DME recall_hook import failed — wheel/install may be "
            "incomplete or blocked (%s)",
            exc,
        )
        dme_meta = {
            "dme_ran": False,
            "dme_skipped": True,
            "dme_error": f"import_error: {exc}",
        }
    else:
        try:
            before_dme_ids = [str(item.get("id") or "") for item in candidates_raw]
            candidates_raw, dme_stats = apply_dme_recall_hook(
                query, candidates_raw, root=MEMORY_ROOT, cue_receipt=cue_receipt
            )
            from memory.recall_truncation import build_truncation_receipt

            truncation_receipts.append(
                build_truncation_receipt(
                    "dme_candidate_merge",
                    before_dme_ids,
                    [str(item.get("id") or "") for item in candidates_raw],
                )
            )
            dme_meta = dme_stats.to_meta()
            truncation_receipts.extend(
                list(dme_meta.pop("dme_truncation_receipts", ()) or ())
            )
        except Exception as exc:
            log.debug("read_memory: DME recall hook skipped (%s)", exc)
            prior = last_recall_hook_stats()
            if not prior.error:
                record_recall_hook_stats(DmeRecallHookStats(error=str(exc)))
            dme_meta = {
                "dme_ran": False,
                "dme_skipped": True,
                "dme_error": str(exc),
            }

    # Pre-verifier anchor filter (RATIFIED 2026-07-04): a candidate reaches
    # the verifier only with a lexical anchor OR strong edge OR hop <= 1 OR
    # compiler-proven bridge. Removal-only — an emptied list flows to UNKNOWN
    # like any empty recall, so abstention is never blocked.
    anchor_dropped = 0
    try:
        from memory.recall_anchor import filter_anchor_basis
        from memory.recall_truncation import build_truncation_receipt

        before_anchor_ids = [str(item.get("id") or "") for item in candidates_raw]
        candidates_raw, _anchor_out = filter_anchor_basis(query.text, candidates_raw)
        anchor_dropped = len(_anchor_out)
        truncation_receipts.append(
            build_truncation_receipt(
                "pre_verifier_anchor_basis",
                before_anchor_ids,
                [str(item.get("id") or "") for item in candidates_raw],
            )
        )
    except Exception as exc:
        log.debug("read_memory: anchor filter skipped (%s)", exc)

    # The "seed" of a delegated recall is its top-scored candidate (taxrecall
    # returns results score-ordered). Carried in meta for backward-compat +
    # the read_memory telemetry summary (replaces walker_query's seed_* keys).
    _seed = candidates_raw[0] if candidates_raw else {}
    meta = {
        "snapshot_hash": packet.snapshot_hash,
        "resolved_entities": list(packet.resolved_entities),
        **(_projection_lane_meta(projection_ctx) if lane_unavailable_reason is None
           else {"projection_lane": f"unavailable({lane_unavailable_reason})", "projection_generation_id": None}),
        "seed_label": str(_seed.get("title") or _seed.get("id") or ""),
        "seed_coord": list(_seed.get("coord") or []),
        "seed_confidence": float(_seed.get("score") or 0.0),
        "anchor_dropped": anchor_dropped,
        "candidate_truncation_receipts": truncation_receipts,
        **dme_meta,
        **({"routing_cues": cue_receipt.to_dict()} if cue_receipt is not None else {}),
    }

    # ── Lane-discipline locals for the arbitration post-pass ─────────────────
    # Primary lobe filtering already happened inside taxrecall.recall (allowed/
    # denied passed above). These locals re-apply the SAME discipline to the
    # arbitration lanes that run over the coord snapshot below. Deny wins over
    # allow so journal(6)/dev(11) are never reachable from a normal recall.
    allowed = query.allowed_lobes
    allowed_set = set(allowed) if allowed is not None else None
    denied = set(query.denied_lobes if query.denied_lobes is not None else (6, 11))

    def _lobe_from_coord(coord) -> Optional[int]:
        try:
            return int((coord or [])[0])
        except (TypeError, ValueError, IndexError):
            return None

    def _passes_lobe_filters(coord, *, missing_lobe: Optional[int] = None) -> bool:
        x = _lobe_from_coord(coord)
        if x is None:
            x = missing_lobe
        if x is None:
            return not (allowed_set is not None or denied)
        if denied and x in denied:
            return False
        return allowed_set is None or x in allowed_set

    def _norm_coord(raw):
        c = list(raw or [0, 0, 0])
        if len(c) < 3:
            c = c + [0] * (3 - len(c))
        elif len(c) > 3:
            c = c[:3]
        return c

    candidates = [
        RetrievalCandidate(
            id=c.get("id", ""),
            coord=_norm_coord(c.get("coord")),
            score=float(c.get("score") or 0.0),
            provenance=dict(c.get("provenance") or {}),
            title=c.get("title"),
            summary=c.get("summary"),
            text=c.get("text"),
            domain=c.get("domain"),
            subtopic=c.get("subtopic"),
            tags=list(c.get("tags") or []),
            contradiction=c.get("contradiction"),
            flagged=bool(c.get("flagged", False)),
            flag_reason=c.get("flag_reason"),
            flag_response_mode=c.get("flag_response_mode"),
            via_link=bool(c.get("via_link", False)),
            version=int(c.get("version", 1) or 1),
            recall_basis_id=_recall_basis_id_of(c),
        )
        for c in candidates_raw
    ]

    # Attach revision history to chain-head candidates. Walker already
    # filtered superseded records out of the candidate pool, so every
    # candidate IS a head; we walk backward from its `supersedes`
    # pointer to enumerate older versions.
    #
    # Per-candidate failures (disk read, walk error) only drop history
    # for THAT candidate (.warning) — the rest of the recall set is
    # unaffected. Outer-import failure means the entire feature is
    # disabled (.error) — operator needs to know revision articulation
    # ("X — was Y") just stopped working.
    try:
        from memory.engine.walker import (
            coord_index_generation as _sup_index_generation,
            coord_index_snapshot as _sup_index_snapshot,
            load_meta_by_path as _sup_load_meta,
            load_coord_index as _sup_load_idx,
        )
        from memory.supersedes import collect_history

        _sup_load_idx()
        # Cut 12: ONE snapshot of the coord index for this whole attach pass,
        # not the live dict. Two reasons, and both are the epoch property:
        # every candidate in a recall must be resolved against the SAME index
        # generation (otherwise one recall's answers straddle two memory
        # states), and AUDIT-5's own note says a concurrent reload can mutate
        # COORD_INDEX under an iterating reader. The snapshot is a shallow copy
        # taken under the walker's lock. Deliberately NOT gated on
        # OMEGA_MEMORY_EPOCHS: this is a read-consistency fix, and killing epoch
        # bookkeeping must not hand the race back.
        _SUP_COORD_INDEX = _sup_index_snapshot()
        memory_epoch.observe(
            memory_epoch.SURFACE_COORD_INDEX, int(_sup_index_generation())
        )
        attached = 0
        for c in candidates:
            if not c.id:
                continue
            entry = _SUP_COORD_INDEX.get(c.id)
            if not entry:
                continue
            try:
                head_meta = _sup_load_meta(entry.get("path"))
            except Exception as exc:
                log.warning(
                    "history attach: load_meta failed for candidate "
                    "%s: %s — skipping history for this record",
                    c.id,
                    exc,
                )
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "memory",
                    "history_attach_failed",
                    ok=False,
                    memory_id=c.id,
                    error=str(exc),
                )
                continue
            if not head_meta or not head_meta.get("supersedes"):
                continue
            try:
                hist = collect_history(
                    head_meta,
                    _SUP_COORD_INDEX,
                    _sup_load_meta,
                )
            except Exception as exc:
                log.warning(
                    "history attach: collect_history raised for %s: "
                    "%s — chain history unavailable for this record",
                    c.id,
                    exc,
                )
                hist = []
            if hist:
                c.history = hist
                c.version = int(head_meta.get("version", 1) or 1)
                attached += 1
        if attached:
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "memory",
                    "history_attach_success",
                    status="ok",
                    ok=True,
                    cycle_id=query.cycle_id,
                    summary=f"attached={attached}",
                    attached=attached,
                    candidate_count=len(candidates),
                )
            except Exception:
                pass
    except ImportError as exc:
        # Permanent disablement — promote so the regression surfaces.
        log.error(
            "history attach: ImportError disables revision history for ALL recalls until fixed: %s",
            exc,
        )
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "history_attach_disabled",
            ok=False,
            error=str(exc),
        )
    except Exception as exc:
        # Unhandled outer exception — promote so we don't silently
        # lose revision articulation across the whole pipeline.
        log.error(
            "history attach: unhandled exception disables revision history this cycle: %s: %s",
            type(exc).__name__,
            exc,
        )
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "history_attach_disabled",
            ok=False,
            error=str(exc),
        )

    meta = dict(meta)
    meta.setdefault("touched", 0)
    meta.setdefault("confidence_decayed", 0)
    meta.setdefault("sustained_floor_demoted", 0)

    # ── Posture-aware metadata ─────────────────────────────────────
    if recall_plan is not None:
        meta["posture"] = recall_plan.posture.name
        meta["recall_plan"] = {
            "intent": recall_plan.intent,
            "query_type": recall_plan.query_type,
            "coord_hypotheses": recall_plan.coord_hypotheses,
            "exact_terms": recall_plan.exact_terms,
            "expanded_terms": recall_plan.expanded_terms,
            "entities": recall_plan.entities,
            "task_tags": recall_plan.task_tags,
            "lane_weights": recall_plan.lane_weights,
            "required_evidence_roles": recall_plan.required_evidence_roles,
            "allowed_evidence_roles": recall_plan.allowed_evidence_roles,
            "min_strength": recall_plan.min_strength,
            "limit_per_lane": recall_plan.limit_per_lane,
            "final_limit": recall_plan.final_limit,
        }

        # ── Lane execution + posture-aware arbitration ────────────
        # Run real lanes against the coord index, merge their hits
        # with the walker baseline (treated as the "walker" lane),
        # arbitrate the merged set against the plan's posture,
        # build the receipt from real winners.
        #
        # This lives BESIDE the existing BGE arbitration (below).
        # Lane arbitration produces `meta["recall_winners"]` and
        # populates `meta["recall_receipt"]` with real reasons; BGE
        # arbitration still rebuilds `candidates` order from its
        # rerank pass, preserving prior behavior for callers that
        # walk `candidates` directly.
        try:
            from memory.recall_lanes import (
                run_recall_lanes,
                LaneHit,
            )
            from memory.recall_arbitrator import arbitrate_evidence
            from memory.engine.walker import (
                coord_index_snapshot,
                load_meta_by_path,
            )

            snapshot = coord_index_snapshot()
            filtered_snapshot = snapshot
            if allowed_set is not None or denied:
                filtered_snapshot = {
                    mid: entry
                    for mid, entry in snapshot.items()
                    if _passes_lobe_filters(entry.get("coord"))
                }
            lane_hits = run_recall_lanes(
                recall_plan,
                filtered_snapshot,
                load_meta_by_path,
                truncation_receipts=truncation_receipts,
            )
            if allowed_set is not None or denied:
                filtered_lane_hits = []
                for hit in lane_hits:
                    meta_coord = (hit.metadata or {}).get("coord")
                    if meta_coord is None:
                        entry = filtered_snapshot.get(hit.memory_id)
                        meta_coord = entry.get("coord") if entry else None
                    if _passes_lobe_filters(meta_coord):
                        filtered_lane_hits.append(hit)
                lane_hits = filtered_lane_hits

            ev_set = EvidenceSet()
            # Seed evidence with walker baseline candidates so the
            # arbitrator can compare lane-derived signal against the
            # existing similarity surface. The walker is treated as
            # an additional "embedding-style" lane named "walker".
            for c in candidates:
                ev_set.add_hit(
                    LaneHit(
                        memory_id=c.id,
                        lane="walker",
                        score=float(c.score) if c.score else 0.0,
                        reason=f"walker baseline score={c.score:.3f}",
                        metadata={
                            "coord": c.coord,
                            "title": c.title,
                            "type": None,  # walker doesn't carry type
                        },
                    )
                )
            for hit in lane_hits:
                ev_set.add_hit(hit)

            # Enrich evidence meta with full loaded records when
            # available — gives the arbitrator type/tags/text fields
            # that the lane-attached metadata doesn't carry.
            for mid, ev in list(ev_set.records.items()):
                entry = filtered_snapshot.get(mid)
                if not entry:
                    continue
                try:
                    full_meta = load_meta_by_path(entry.get("path"))
                except Exception:
                    full_meta = None
                if full_meta:
                    # Prefer fully-loaded record over lane-attached
                    # metadata (which may only have coord/title/type).
                    merged = dict(ev.meta or {})
                    merged.update(full_meta)
                    ev_set.add_meta(mid, merged)

            winners = arbitrate_evidence(
                ev_set,
                recall_plan,
                truncation_receipts=truncation_receipts,
            )
            meta["recall_winners"] = [
                {
                    "memory_id": w.memory_id,
                    "lanes": list(w.lanes),
                    "lane_scores": dict(w.lane_scores),
                    "reasons": list(w.reasons),
                    "final_score": float(w.final_score),
                }
                for w in winners
            ]
            receipt = build_recall_receipt(query, recall_plan, ev_set, winners)
            meta["recall_receipt"] = receipt
            meta["candidate_truncation_receipts"] = list(truncation_receipts)
            try:
                from guardian.terminal_feedback import emit_data

                lane_names = sorted({str(h.lane) for h in lane_hits if getattr(h, "lane", None)})
                emit_data(
                    "memory",
                    "lane_arbitration",
                    cycle_id=query.cycle_id,
                    summary=(f"winners={len(winners)} posture={recall_plan.posture.name}"),
                    winner_count=len(winners),
                    posture=recall_plan.posture.name,
                    lane_hit_count=len(lane_hits),
                    candidate_count=len(candidates),
                )
                emit_data(
                    "memory",
                    "lanes_executed",
                    cycle_id=query.cycle_id,
                    summary=f"lanes={lane_names}",
                    lanes_executed=lane_names,
                    lane_hit_count=len(lane_hits),
                )
            except Exception:
                pass
        except RetinaUnavailable:
            # Required perception could not run. Preserve its original cause at
            # the public recall boundary instead of reporting walker-only success.
            raise
        except Exception as exc:
            log.warning("read_memory: lane arbitration skipped (%s)", exc)
            # Fall back to walker-only receipt so the meta key is
            # always present and downstream consumers don't need to
            # null-check.
            try:
                from memory.evidence_set import MemoryEvidence

                fallback_set = EvidenceSet()
                for c in candidates:
                    fallback_set.records[c.id] = MemoryEvidence(
                        memory_id=c.id,
                        lanes=["walker"],
                        lane_scores={"walker": float(c.score)},
                        reasons=[
                            f"walker baseline (lane arbitration unavailable: {type(exc).__name__})"
                        ],
                        meta={"coord": c.coord, "title": c.title, "type": None},
                    )
                fallback_winners = list(fallback_set.to_list())
                meta["recall_receipt"] = build_recall_receipt(
                    query,
                    recall_plan,
                    fallback_set,
                    fallback_winners,
                )
                meta["recall_winners"] = []
                try:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "memory",
                        "lane_arbitration_degraded",
                        cycle_id=query.cycle_id,
                        status="degraded",
                        ok=False,
                        error=str(exc),
                        summary=f"fallback={type(exc).__name__}",
                        winner_count=0,
                        error_type=type(exc).__name__,
                    )
                except Exception:
                    pass
            except Exception:
                meta["recall_receipt"] = None
                meta["recall_winners"] = []

    # ── Walker-native arbitration metadata ─────────────────────────
    # 2026-05-08: BGE cross-encoder dropped. The lane arbitration
    # above (recall_compiler → recall_lanes → recall_arbitrator)
    # already produces deterministic, posture-aware ranking via the
    # walker's composite scoring (geometric × link × recency × tag ×
    # provenance × domain × strength) plus multi-lane convergence
    # bonuses, suppression penalties, and answerability scoring. The
    # cross-encoder rerank was a SECOND arbitration that re-ordered
    # by text similarity — overriding the posture decision and forcing
    # the posture-aware-final-reorder workaround below.
    #
    # With per-lobe SetFit specialists at 97% routing accuracy, the
    # walker's coord prediction is precise enough that a cross-encoder
    # rerank adds noise, not signal. The deterministic walker IS the
    # ranker (per `project_omega_mantra_deterministic_home`: "recall
    # becomes simple because it goes from guessing to exacting
    # structure every time").
    #
    # This shim builds the `meta["arbitration"]` shape downstream
    # consumers (Curiosity / DeepSleep / Guardian) read, sourcing
    # from the lane-arbitration winners already in
    # `meta["recall_winners"]`. Same shape, no BGE dependency, no
    # second arbitration pass.
    if recall_plan is not None:
        try:
            winners_for_arb = meta.get("recall_winners") or []
            top_arb: List[Dict[str, Any]] = []
            rerank_by_id: Dict[str, float] = {}
            for rank, w in enumerate(winners_for_arb[:3], start=1):
                mid = w.get("memory_id")
                if not mid:
                    continue
                final = float(w.get("final_score") or 0.0)
                lane_scores = w.get("lane_scores") or {}
                # Surface the strongest contributing lane as a "summary"
                # so the receipt explanation reads cleanly.
                top_lane = ""
                if isinstance(lane_scores, dict) and lane_scores:
                    top_lane = max(lane_scores.items(), key=lambda kv: kv[1])[0]
                top_arb.append(
                    {
                        "memory_id": mid,
                        "rank": rank,
                        "final_score": final,
                        # rerank_score is now alias for final_score — same number,
                        # different name, kept for downstream consumers that
                        # still read `arbitration.top[i].rerank_score`.
                        "rerank_score": final,
                        "strength": lane_scores.get("strength", 0.0),
                        "recency": lane_scores.get("temporal", 0.0),
                        "contradiction": 0.0,
                        "summary": (f"lane={top_lane} final={final:.2f} lanes={len(lane_scores)}"),
                    }
                )
                rerank_by_id[mid] = final
            meta["rerank_present"] = False  # cross-encoder is gone
            meta["arbitration"] = {
                "explanation": (
                    f"walker-native arbitration: posture={recall_plan.posture.name} "
                    f"winners={len(winners_for_arb)} top3={len(top_arb)}"
                ),
                "top": top_arb,
            }
            # Mirror final_score onto candidates as rerank_score for any
            # legacy consumer still walking `candidates[i].rerank_score`.
            for cand in candidates:
                if cand.id in rerank_by_id:
                    cand.rerank_score = rerank_by_id[cand.id]
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "walker_arbitration_failed",
                status="degraded",
                ok=False,
                error=str(exc),
            )
            log.warning("read_memory: walker arbitration shim failed (%s)", exc)
            meta["rerank_present"] = False
            meta["arbitration"] = None
    else:
        meta["rerank_present"] = False
        meta["arbitration"] = None

    # ── Posture-aware FINAL reorder ────────────────────────────────
    # The lane arbitration above produced `meta["recall_winners"]`
    # using the recall plan's posture (preferred lobes, suppressed
    # lobes, lane weights, evidence-role fit, multi-lane convergence,
    # answerability). BGE then runs and re-orders `candidates` by
    # text-similarity rerank score — which OVERRIDES the posture
    # decision: a "who is the operator" query then surfaces the most
    # text-similar memory regardless of whether it answers an
    # identity question.
    #
    # The architectural rule: declared contracts beat heuristic
    # signals. Posture is the declared contract — "this query is an
    # identity question, prefer lobes 0/1, suppress 5/8/10/11."
    # BGE's rerank_score is a signal, not a decision. So:
    #   * BGE runs, attaches rerank_score to every candidate (signal).
    #   * Then THIS step reorders `candidates` so lane-arbitration
    #     winners are at the front in their arbitrated order.
    #   * The remaining candidates (BGE's tail) keep their BGE-derived
    #     order. So consumers that walk past the winners still get a
    #     reasonable rerank-driven tail.
    # Result: `candidates[0]` carries the posture-correct memory.
    # `meta["recall_winners"]` (already set) and `meta["arbitration"]
    # ["top"]` (already set) both stay available for consumers that
    # want either signal independently.
    winners_meta = meta.get("recall_winners") or []
    if winners_meta:
        winner_ids: List[str] = [
            w.get("memory_id") for w in winners_meta if isinstance(w, dict) and w.get("memory_id")
        ]
        if winner_ids:
            winner_id_set = set(winner_ids)
            front = []
            seen_ids: set = set()
            # Walk winner_ids in arbitrated order; pull matching
            # candidate. If a winner isn't in the candidate list (can
            # happen when the lane found a memory the walker didn't
            # surface), it stays in `recall_winners` metadata but
            # doesn't appear in `candidates` — preserves the contract
            # that `candidates` is the walker's view of the corpus.
            for wid in winner_ids:
                if wid in seen_ids:
                    continue
                for cand in candidates:
                    if cand.id == wid:
                        front.append(cand)
                        seen_ids.add(wid)
                        break
            tail = [c for c in candidates if c.id not in winner_id_set]
            candidates = front + tail

    # Aggregate flag info for downstream phases. The response path
    # reads these to decide response posture: if any of
    # the recalled memories carry a flag, the voice_box should render
    # the bounded posture instead of treating the recall as plain
    # knowledge. The HIGHEST-priority flag wins (REJECT > QUARANTINE >
    # FLAG > none — but quarantine never reaches recall, so FLAG is
    # the only level we see here).
    flagged = [c for c in candidates if c.flagged]
    if flagged:
        # Pick the first flagged candidate's response_mode as the
        # dominant posture. (A future refinement could weight by score.)
        meta["recall_flag_present"] = True
        meta["recall_flag_response_mode"] = flagged[0].flag_response_mode
        meta["recall_flag_count"] = len(flagged)
    else:
        meta["recall_flag_present"] = False

    result = RetrievalResult(
        cycle_id=query.cycle_id,
        candidates=candidates,
        meta=meta,
    )
    result = _merge_canonical_recall(query, result, root=profile_root)
    candidates = result.candidates
    meta = result.meta
    flagged = [candidate for candidate in candidates if candidate.flagged]
    meta["recall_flag_present"] = bool(flagged)
    if flagged:
        meta["recall_flag_response_mode"] = flagged[0].flag_response_mode
        meta["recall_flag_count"] = len(flagged)
    else:
        meta.pop("recall_flag_response_mode", None)
        meta.pop("recall_flag_count", None)

    # Cut 12 — the snapshot-read receipt. Written HERE, after the canonical
    # merge, because that merge is itself a read: a receipt taken before it
    # would describe a recall that is not the one being returned. It answers
    # one question — was this recall served the memory state its episode
    # pinned, or a later one? ``{"pinned": False}`` is the honest answer for
    # reads outside an episode (HUD peeks, diagnostics, boot): they have no
    # epoch to be served at, and inventing one would be worse than saying so.
    meta["memory_epoch"] = memory_epoch.recall_receipt()

    n_hits = len(candidates)
    rr_on = bool(meta.get("rerank_present", False))
    seed_lbl = str(meta.get("seed_label") or "")
    seed_cf = float(meta.get("seed_confidence", 0.0) or 0.0)
    top_final = None
    arb = meta.get("arbitration") or {}
    tops = arb.get("top") if isinstance(arb, dict) else None
    if tops:
        try:
            top_final = float(tops[0].get("final_score"))
        except (TypeError, ValueError, IndexError):
            top_final = None
    summary = (
        f"hits={n_hits} rerank={'on' if rr_on else 'off'} seed={seed_lbl or '?'} conf={seed_cf:.2f}"
    )
    if top_final is not None:
        summary += f" top_final={top_final:.3f}"
    posture_name = str(meta.get("posture") or "")
    rw = meta.get("recall_winners")
    recall_winner_n = len(rw) if isinstance(rw, list) else 0
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "read_memory",
            cycle_id=query.cycle_id,
            summary=summary,
            query_text=str(getattr(query, "text", "") or "")[:200],
            hits=n_hits,
            posture=posture_name,
            seed_label=seed_lbl,
            top_final=top_final,
            recall_winner_n=recall_winner_n,
        )
    except Exception:
        pass
    if verdict_gate:
        result = _apply_recall_verdict_gate(
            query,
            result,
            root=profile_root,
            include_historical_sources=bool(
                getattr(recall_plan, "include_historical_sources", False)
            ),
        )
    return result


def _retrieve_memory(query: QuerySpec) -> RetrievalResult:
    """Internal recall path without the upstream verdict gate."""
    return read_memory(query, verdict_gate=False)


def _merge_canonical_recall(
    query: QuerySpec,
    result: RetrievalResult,
    *,
    root: Path | str | None = None,
) -> RetrievalResult:
    """Rank graph/DME membership and cold canonical files on one scale.

    Canonical files remain the source of truth.  Graph and DME contribute
    candidate membership through ``include_ids``; the filesystem scorer then
    evaluates those ids alongside the bounded canonical corpus.  Every result
    still passes through ``memory.recall_verifier`` before cognition sees it.
    """
    from memory.canonical_recall import (
        last_semantic_admission,
        search_canonical_files,
    )

    existing = list(result.candidates or [])
    existing_by_id = {candidate.id: candidate for candidate in existing}
    include_ids = tuple(existing_by_id)
    retrieval_limit = max(query.limit, min(128, max(1, query.limit) * 4))
    meta = dict(result.meta or {})
    canonical_truncation_receipts: list[dict[str, Any]] = []

    try:
        ranked = search_canonical_files(
            Path(root) if root is not None else _active_profile_root(),
            query.text,
            limit=retrieval_limit,
            evidence_limit=query.limit,
            include_ids=include_ids,
            allowed_lobes=query.allowed_lobes,
            denied_lobes=query.denied_lobes,
            allowed_subdomains=query.allowed_subdomains,
            truncation_receipts=canonical_truncation_receipts,
        )
        semantic = last_semantic_admission()
    except Exception as exc:
        log.warning("canonical recall merge failed: %s", exc)
        meta["canonical_recall"] = {
            "filesystem_fallback_used": False,
            "graph_membership_count": len(include_ids),
            "ranked_count": 0,
            "status": "error",
            "error": type(exc).__name__,
        }
        return RetrievalResult(
            cycle_id=result.cycle_id,
            candidates=existing,
            meta=meta,
        )

    merged: list[RetrievalCandidate] = []
    for item in ranked:
        memory_id = str(item.get("memory_id") or "")
        if not memory_id:
            continue
        coord = list(item.get("coord") or [])
        if len(coord) == 2:
            coord.append(0)
        elif len(coord) > 3:
            coord = coord[:3]
        previous = existing_by_id.get(memory_id)
        candidate = RetrievalCandidate(
            id=memory_id,
            coord=coord or (list(previous.coord) if previous else [0, 0, 0]),
            score=float(item.get("score") or 0.0),
            provenance=dict(item.get("provenance") or {}),
            title=str(item.get("title") or ""),
            summary=str(item.get("summary") or ""),
            text=str(item.get("text") or ""),
            domain=item.get("domain"),
            subtopic=item.get("subtopic"),
            tags=list(item.get("tags") or []),
            contradiction=item.get("contradiction"),
            path=str(item.get("path") or "") or None,
            filesystem_fallback=bool(item.get("filesystem_fallback")),
            semantic_recall=bool(item.get("semantic_recall")),
            flagged=bool(item.get("flagged")),
            flag_reason=item.get("flag_reason"),
            flag_response_mode=item.get("flag_response_mode"),
            version=int(item.get("version") or 1),
            recall_basis_id=_recall_basis_id_of(item),
        )
        if previous is not None:
            if not candidate.provenance:
                candidate.provenance = dict(previous.provenance or {})
            if not candidate.source_proof:
                candidate.source_proof = dict(previous.source_proof or {})
            if not candidate.recall_basis_id:
                candidate.recall_basis_id = previous.recall_basis_id
            candidate.subtopic = previous.subtopic
            candidate.tags = list(previous.tags or [])
            candidate.contradiction = previous.contradiction
            candidate.flagged = previous.flagged
            candidate.flag_reason = previous.flag_reason
            candidate.flag_response_mode = previous.flag_response_mode
            candidate.via_link = previous.via_link
            candidate.history = list(previous.history or [])
            candidate.version = previous.version
            # Preserve the lane-arbitration final score across the canonical
            # merge; the filesystem scorer is not a second ranking authority.
            candidate.rerank_score = previous.rerank_score
        candidate.provenance["independent_evidence"] = _independent_recall_evidence(
            candidate.provenance
        )
        merged.append(candidate)

    # Canonical filesystem scoring supplies membership and canonical fields;
    # it is not a second ranking authority.  The lane arbitration performed
    # before this merge is the declared decision for the current posture, so
    # keep those winners ahead of any filesystem-ranked tail.  Without this
    # reapplication, a similar-text canonical record could silently replace
    # the posture winner at ``candidates[0]``.
    winner_ids = [
        winner.get("memory_id")
        for winner in meta.get("recall_winners") or []
        if isinstance(winner, dict) and winner.get("memory_id")
    ]
    if winner_ids and merged:
        merged_by_id = {candidate.id: candidate for candidate in merged}
        winner_id_set = set(winner_ids)
        ordered_winners = []
        seen_ids: set[str] = set()
        for memory_id in winner_ids:
            if memory_id in seen_ids:
                continue
            candidate = merged_by_id.get(memory_id)
            if candidate is not None:
                ordered_winners.append(candidate)
                seen_ids.add(memory_id)
        merged = ordered_winners + [
            candidate for candidate in merged if candidate.id not in winner_id_set
        ]

    cold_ids = {
        candidate.id
        for candidate in merged
        if candidate.filesystem_fallback and candidate.id not in existing_by_id
    }
    canonical_meta: Dict[str, Any] = {
        "filesystem_fallback_used": bool(cold_ids),
        "filesystem_fallback_count": len(cold_ids),
        "graph_membership_count": len(include_ids),
        "ranked_count": len(merged),
        "status": "ok",
    }
    if semantic is not None:
        canonical_meta["semantic"] = {
            "status": semantic.status,
            "seed_count": semantic.seed_count,
            "closure_count": semantic.closure_count,
            "query_dim": semantic.query_dim,
            "plane_edges": semantic.plane_edges,
        }
    meta["canonical_recall"] = canonical_meta
    meta["candidate_truncation_receipts"] = [
        *list(meta.get("candidate_truncation_receipts") or []),
        *canonical_truncation_receipts,
    ]
    return RetrievalResult(
        cycle_id=result.cycle_id,
        candidates=merged or existing,
        meta=meta,
    )


def _apply_verified_recall_side_effects(
    *,
    cycle_id: str,
    episode_id: str | None = None,
    verified_raw: List[Dict[str, Any]],
    meta: Dict[str, Any],
) -> Dict[str, Any]:
    """Schedule verified-memory reconsolidation for episode DeepSleep.

    Runtime recall is a read path.  Confidence decay, lifecycle demotion, and
    StrengthIndex reinforcement are memory maintenance and therefore run only
    after the owning episode closes.
    """
    ids: list[str] = []
    seen: set[str] = set()
    for item in verified_raw or []:
        memory_id = str(item.get("memory_id") or "").strip()
        if memory_id and memory_id not in seen:
            ids.append(memory_id)
            seen.add(memory_id)

    meta = dict(meta or {})
    scheduled = 0
    if ids:
        try:
            from memory.episode_sram import hold_verified_recall_for_episode

            scheduled = hold_verified_recall_for_episode(episode_id or cycle_id, ids)
        except Exception as exc:
            log.warning("verified recall maintenance deferral skipped (%s)", exc)
            meta["recall_side_effect_error"] = f"{type(exc).__name__}: {exc}"

    meta["touched"] = 0
    meta["confidence_decayed"] = 0
    meta["sustained_floor_demoted"] = 0
    meta["recall_maintenance_deferred"] = scheduled
    meta["recall_maintenance_episode_id"] = str(episode_id or cycle_id)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "recall_maintenance_deferred",
            cycle_id=cycle_id,
            summary=f"scheduled={scheduled}",
            scheduled=scheduled,
            candidate_count=len(ids),
        )
    except Exception:
        pass
    return meta


def _apply_recall_verdict_gate(
    query: QuerySpec,
    result: RetrievalResult,
    *,
    root: Path | str | None = None,
    include_historical_sources: bool = False,
) -> RetrievalResult:
    from memory.recall_verifier import append_verdict_log, verify_retrieval_result

    profile_root = Path(root) if root is not None else _active_profile_root()
    verdict = verify_retrieval_result(
        query.text,
        result,
        root=profile_root,
        allowed_lobes=getattr(query, "allowed_lobes", None),
        include_historical_sources=include_historical_sources,
        # Cut 3: lets the verifier re-derive the executed query-coordinate set
        # with the exact lobe policy and plan inputs of this recall
        denied_lobes=getattr(query, "denied_lobes", None),
        query_spec=query,
    )
    try:
        append_verdict_log(verdict, cycle_id=query.cycle_id, root=profile_root)
    except Exception:
        log.debug("recall verdict log append failed", exc_info=True)

    meta = dict(result.meta or {})
    meta["recall_verdict"] = verdict.to_dict()
    posture_winners = meta.get("recall_winners")
    admitted_raw = list((verdict.answer_payload or {}).get("candidates") or [])
    retrieval_trace = getattr(verdict, "retrieval_trace", {}) or {}
    accounting = retrieval_trace.get("candidate_accounting", {})
    admitted_count = len(admitted_raw)
    retrieved_count = int(
        accounting.get(
            "retrieved_count",
            admitted_count + len(verdict.rejected_hits),
        )
    )
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "read_memory_verdict",
            cycle_id=query.cycle_id,
            summary=f"status={verdict.status}",
            status=verdict.status,
            reason=verdict.reason,
            retrieved_count=retrieved_count,
            admitted_count=admitted_count,
            rejected_count=len(verdict.rejected_hits),
        )
    except Exception:
        pass

    if verdict.status != "verified" or not verdict.answer_payload:
        meta["touched"] = 0
        meta["confidence_decayed"] = 0
        meta["sustained_floor_demoted"] = 0
        if isinstance(posture_winners, list):
            # ``recall_winners`` is the public set of posture-aware winners
            # admitted to cognition.  A rejected verdict must not leave
            # rejected lane winners visible beside an empty answer.
            meta["recall_winners"] = []
        return RetrievalResult(
            cycle_id=query.cycle_id,
            candidates=[],
            meta=meta,
        )

    verified_raw = admitted_raw
    if isinstance(posture_winners, list):
        admitted_ids = {
            str(item.get("memory_id") or "")
            for item in verified_raw
            if isinstance(item, Mapping) and item.get("memory_id")
        }
        meta["recall_winners"] = [
            winner
            for winner in posture_winners
            if isinstance(winner, Mapping)
            and str(winner.get("memory_id") or "") in admitted_ids
        ]
    meta = _apply_verified_recall_side_effects(
        cycle_id=query.cycle_id,
        episode_id=query.episode_id,
        verified_raw=verified_raw,
        meta=meta,
    )
    # The gate verifies provenance — it must not strip the enrichment
    # fields the recall pipeline attached upstream (lane-arbitration
    # rerank_score alias, link-walk and flag markers, supersedes
    # history). Rebuilding candidates from the verifier payload lost
    # them silently (test_walker_recall 4e: rerank_score=None after the
    # verifier era) — carry them over from the pre-gate candidate with
    # the same memory id.
    pre_by_id = {c.id: c for c in (result.candidates or [])}
    gated: list[RetrievalCandidate] = []
    for item in verified_raw:
        coord = list(item.get("coord") or [])
        if len(coord) == 2:
            coord = coord + [0]
        elif len(coord) > 3:
            coord = coord[:3]
        cand = RetrievalCandidate(
            id=str(item.get("memory_id") or ""),
            coord=coord or [0, 0, 0],
            score=float(item.get("score") or 0.0),
            provenance={
                "evidence_strength": (item.get("source_proof") or {}).get(
                    "evidence_strength", "rederive"
                ),
                "path": item.get("path"),
                "sense_ids": list(item.get("sense_ids") or []),
            },
            title=str(item.get("title") or ""),
            summary=str(item.get("summary") or ""),
            domain=item.get("domain"),
            subtopic=item.get("subtopic"),
            tags=list(item.get("tags") or []),
            contradiction=item.get("contradiction"),
            flagged=bool(item.get("flagged", False)),
            flag_reason=item.get("flag_reason"),
            flag_response_mode=item.get("flag_response_mode"),
            path=str(item.get("path") or "") or None,
            filesystem_fallback=bool(item.get("filesystem_fallback")),
            semantic_recall=bool(item.get("semantic_recall")),
            source_proof=dict(item.get("source_proof") or {}),
        )
        pre = pre_by_id.get(cand.id)
        if pre is not None:
            cand.provenance = {**dict(pre.provenance or {}), **cand.provenance}
            cand.rerank_score = pre.rerank_score
            cand.via_link = pre.via_link
            if "flagged" not in item:
                cand.flagged = pre.flagged
            if "flag_reason" not in item:
                cand.flag_reason = pre.flag_reason
            if "flag_response_mode" not in item:
                cand.flag_response_mode = pre.flag_response_mode
            if not cand.subtopic:
                cand.subtopic = pre.subtopic
            if not cand.text:
                cand.text = pre.text
            if not cand.tags:
                cand.tags = list(pre.tags or [])
            if not cand.contradiction:
                cand.contradiction = pre.contradiction
            cand.history = list(pre.history or [])
            cand.version = pre.version
            cand.path = pre.path or cand.path
            cand.filesystem_fallback = pre.filesystem_fallback
            cand.semantic_recall = pre.semantic_recall
            cand.recall_basis_id = pre.recall_basis_id
            if not cand.source_proof:
                cand.source_proof = dict(pre.source_proof or {})
        cand.provenance["independent_evidence"] = _independent_recall_evidence(
            cand.provenance
        )
        gated.append(cand)
    return RetrievalResult(
        cycle_id=query.cycle_id,
        candidates=gated,
        meta=meta,
    )


def recall_verified(query: QuerySpec) -> Any:
    """Verify the live canonical files behind deterministic recall hits.

    The gate-first contract is stable: VERIFIED/UNKNOWN/AMBIGUOUS/CONFLICT.
    Phase 1 uses re-derive verification over the current canonical corpus.
    """
    _require_cycle_id(query.cycle_id)
    # Cut 13 (D-13-19/28): recall_verified OWNS the projection-read transaction — the
    # retrieval, the DME/registry reads and the final basis verification all see ONE
    # generation; read_memory reuses (does not reopen) it.
    from memory.projection.generation import projection_read_transaction

    with projection_read_transaction(_active_profile_root()):
        result = _retrieve_memory(query)
        from memory.recall_verifier import append_verdict_log, verify_retrieval_result

        try:
            from memory.recall_compiler import compile_recall_plan

            recall_plan = compile_recall_plan(query)
            include_historical_sources = bool(
                getattr(recall_plan, "include_historical_sources", False)
            )
        except Exception:
            include_historical_sources = False
        verdict = verify_retrieval_result(
            query.text,
            result,
            allowed_lobes=getattr(query, "allowed_lobes", None),
            include_historical_sources=include_historical_sources,
            denied_lobes=getattr(query, "denied_lobes", None),
            query_spec=query,
        )
    if verdict.status == "verified" and verdict.answer_payload:
        _apply_verified_recall_side_effects(
            cycle_id=query.cycle_id,
            episode_id=query.episode_id,
            verified_raw=list((verdict.answer_payload or {}).get("candidates") or []),
            meta=dict(getattr(result, "meta", None) or {}),
        )
    try:
        from memory.recall_verifier import append_verdict_log

        append_verdict_log(verdict, cycle_id=query.cycle_id)
    except Exception:
        pass
    try:
        from guardian.terminal_feedback import emit_data

        retrieval_trace = getattr(verdict, "retrieval_trace", {}) or {}
        accounting = retrieval_trace.get("candidate_accounting", {})
        admitted_count = len((verdict.answer_payload or {}).get("candidates") or ())
        emit_data(
            "memory",
            "recall_verified",
            cycle_id=query.cycle_id,
            summary=f"status={verdict.status}",
            status=verdict.status,
            retrieved_count=int(
                accounting.get(
                    "retrieved_count",
                    admitted_count + len(verdict.rejected_hits),
                )
            ),
            admitted_count=admitted_count,
            rejected_count=len(verdict.rejected_hits),
            evidence_count=len(verdict.evidence),
            reason=verdict.reason,
        )
    except Exception:
        pass
    return verdict


# =====================================================================
# write_memory
# =====================================================================


def _emit_write_memory_data(envelope: CanonicalEnvelope, receipt: WriteReceipt) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "write_memory",
            cycle_id=envelope.cycle_id,
            summary=f"status={receipt.validation_status}",
            coord=list(envelope.coord) if envelope.coord else None,
            type=envelope.type,
            validation_status=receipt.validation_status,
            staging_ref=receipt.staging_ref,
            canonical_ref=receipt.path,
            durable=bool(receipt.durable),
            journal_record_id=receipt.journal_record_id,
            event_id=receipt.event_id,
            replayed=bool(receipt.replayed),
        )
    except Exception:
        pass


def _canonical_binding_exists(record: Any) -> bool:
    """Reusing a receipt requires this event's immutable claim, not just its ID."""
    from memory.intake_journal import verify_canonical_binding

    verdict, _, detail = verify_canonical_binding(record)
    if verdict not in {"ok", "missing", "no_identity"}:
        from memory.intake_journal import IntakeJournalError

        raise IntakeJournalError(
            f"journalled commit {record.record_id} has no verifiable canonical record: "
            f"{verdict} ({detail})"
        )
    return verdict == "ok"


def _replayed_intake_receipt(envelope: CanonicalEnvelope, record: Any) -> WriteReceipt:
    from memory import intake_journal

    common = {
        "cycle_id": envelope.cycle_id,
        "staging_ref": None,
        "durable": True,
        "journal_record_id": record.record_id,
        "event_id": record.event_id,
        "replayed": True,
    }
    if record.status == intake_journal.COMMITTED:
        if not record.memory_id or not _canonical_binding_exists(record):
            raise intake_journal.IntakeJournalError(
                f"journalled commit {record.record_id} has no verifiable canonical record"
            )
        return WriteReceipt(
            path=record.canonical_path or record.memory_id,
            validation_status="committed",
            message="replayed: source event is already canonically committed",
            **common,
        )
    if record.status == intake_journal.REJECTED:
        return WriteReceipt(
            path=None,
            validation_status="rejected",
            message=f"replayed: source event was rejected: {record.note or 'rejected'}",
            **common,
        )
    raise intake_journal.IntakeJournalError(
        f"source event {record.event_id} has unsupported terminal state {record.status}"
    )


def intake_memory(
    envelope: CanonicalEnvelope,
    *,
    propose: bool = False,
    bypass_guardian: bool = True,
    skip_embed: bool = False,
    event_id: Optional[str] = None,
) -> WriteReceipt:
    """Atomically accept incoming memory into the episode SRAM lane.

    This function never writes the canonical filesystem. DeepSleep drains the held
    envelope through :func:`write_memory`; until then the receipt says ``held`` and
    carries no canonical path.
    """
    _require_cycle_id(envelope.cycle_id)
    from memory import intake_journal
    from memory.episode_sram import (
        hold_memory,
        is_draining,
        owner_for_intake,
        remove_journal_record,
    )

    if is_draining():
        return write_memory(
            envelope,
            propose=propose,
            bypass_guardian=bypass_guardian,
            skip_embed=skip_embed,
        )

    owner_episode_id, owner_pipeline_id = owner_for_intake()
    record, replayed = intake_journal.journal_memory_intake(
        envelope,
        owner_episode_id=owner_episode_id,
        owner_pipeline_id=owner_pipeline_id,
        write_options={
            "propose": bool(propose),
            "bypass_guardian": bool(bypass_guardian),
            "skip_embed": bool(skip_embed),
        },
        event_id=event_id,
    )
    if (
        record.status == intake_journal.PENDING
        and (
            record.owner_episode_id != owner_episode_id
            or record.owner_pipeline_id != owner_pipeline_id
        )
    ):
        from memory import episode_sram

        if episode_sram.is_open(record.owner_episode_id):
            raise intake_journal.IntakeJournalError(
                f"source event {record.event_id} is still owned by "
                f"{record.owner_episode_id}/{record.owner_pipeline_id}"
            )
        record = intake_journal.reown_record(
            record.record_id,
            episode_id=owner_episode_id,
            pipeline_id=owner_pipeline_id,
            reason="re-presented after its prior SRAM owner disappeared",
        )
    if record.status in intake_journal.TERMINAL_STATUSES:
        remove_journal_record(record.record_id)
        receipt = _replayed_intake_receipt(envelope, record)
        _emit_write_memory_data(envelope, receipt)
        return receipt

    sram_ref = hold_memory(
        envelope,
        propose=propose,
        bypass_guardian=bypass_guardian,
        skip_embed=skip_embed,
        journal_record=record,
    )
    receipt = WriteReceipt(
        cycle_id=envelope.cycle_id,
        staging_ref=sram_ref,
        path=None,
        validation_status="held",
        message="held in episode SRAM for DeepSleep consolidation",
        durable=True,
        journal_record_id=record.record_id,
        event_id=record.event_id,
        replayed=replayed,
    )
    _emit_write_memory_data(envelope, receipt)
    return receipt


def write_memory(
    envelope: CanonicalEnvelope,
    *,
    propose: bool = False,
    bypass_guardian: bool = True,
    skip_embed: bool = False,
    update_index: bool = True,
    run_post_commit_hooks: bool = True,
    write_ordinal: int | None = None,
    stamp_observed: bool = True,
    enable_supersedes: bool = True,
) -> WriteReceipt:
    """Atomically persist one SRAM-held envelope during the DeepSleep drain."""
    from memory.episode_sram import is_draining

    if not is_draining():
        raise RuntimeError(
            "write_memory is restricted to the DeepSleep SRAM drain; "
            "producers must call intake_memory"
        )
    return _commit_memory(
        envelope,
        propose=propose,
        bypass_guardian=bypass_guardian,
        skip_embed=skip_embed,
    )


def _commit_memory(
    envelope: CanonicalEnvelope,
    *,
    propose: bool = False,
    bypass_guardian: bool = True,
    skip_embed: bool = False,
) -> WriteReceipt:
    """Canonical write implementation; reachable only through ``write_memory``."""
    _require_cycle_id(envelope.cycle_id)
    # Trust boundary — STRUCTURAL, not opt-in (doctrine: human-authored input is
    # untrusted, the operator included, because they are human). A user-chat /
    # human source can NEVER take the direct-commit bypass; it is FORCED through
    # the Guardian verifier (decide_request → verify_promotion) regardless of the
    # propose / bypass_guardian / skip_embed a caller passed. Only Omega-internal
    # cognition and Omega-mediated (AUTO-digested, bounded) web intake write
    # direct. This makes the boundary un-bypassable: a future human-ingress writer
    # that forgets to opt in is caught here by its source alone.
    if _is_user_chat_source(envelope.source):
        if not propose or bypass_guardian or skip_embed:
            log.info(
                "write_memory: human source %r forced through Guardian verifier "
                "(caller passed propose=%s bypass_guardian=%s skip_embed=%s)",
                envelope.source,
                propose,
                bypass_guardian,
                skip_embed,
            )
        propose = True
        bypass_guardian = False
        skip_embed = False
    if propose and skip_embed:
        raise ValueError("skip_embed applies only to direct commits (propose=False)")
    env_dict = _envelope_to_dict(envelope)

    if propose:
        if not _is_user_chat_source(envelope.source):
            receipt = WriteReceipt(
                cycle_id=envelope.cycle_id,
                staging_ref=None,
                path=None,
                validation_status="rejected",
                message=(
                    "Guardian proposal path is reserved for user-chat ingress; "
                    "internal memory writes must use propose=False, bypass_guardian=True"
                ),
            )
            _emit_write_memory_data(envelope, receipt)
            return receipt
        # Step A — stage. Failures here never reach Guardian.
        try:
            staging_ref = create_staging(env_dict)
        except ValueError as exc:
            receipt = WriteReceipt(
                cycle_id=envelope.cycle_id,
                staging_ref=None,
                path=None,
                validation_status="rejected",
                message=f"staging refused: {exc}",
            )
            _emit_write_memory_data(envelope, receipt)
            return receipt
        except Exception:
            log.exception("write_memory staging failed")
            raise
        # Step B — atomic encoding (memory.api atomic encoding doctrine
        # + Omega Cognitive Law). Every Guardian-approved write must
        # complete the full 8-step pipeline (commit + index + embed +
        # self-vector + contradiction precheck + persistence anchor +
        # reflection eligibility + deepsleep candidacy) or roll back.
        # Staging without a decision violates atomicity and leaves
        # writes in limbo; that's how the pending-approval queue
        # accumulates unbounded.
        #
        # decide_request → on ALLOW → commit_promotion runs the
        # encoding chain via memory.encoding.encode_committed_memory.
        # Any encoding step failure inside that chain triggers
        # rollback_canonical_write per the doctrine.
        #
        # Inline import: guardian package depends on memory.api at
        # import time, so a top-level import here would be circular.
        from guardian.decision import decide_request

        approver = f"memory.api:{envelope.source or 'unknown'}"
        decision = decide_request(staging_ref, approver_id=approver)

        if decision.committed:
            from memory.post_commit import run_post_commit_memory_hooks

            _plan = getattr(decision, "rollback_plan", None) or {}
            # Cut 4 rework 2: durable projection obligation BEFORE the
            # canonical-mutating hooks; settled after the final publication.
            obligation = _open_projection_obligation(
                decision.canonical_ref or "", _plan, owner=f"memory.api:guardian:{decision.kind}",
            )
            obligation_id = obligation.get("journal_id")
            if obligation["hooks_allowed"]:
                hook_report = run_post_commit_memory_hooks(
                    decision.canonical_ref or "",
                    coord=list(envelope.coord or []),
                    source=envelope.source,
                )
            else:
                hook_report = dict(_UNPROTECTED_HOOK_REPORT, memory_id=decision.canonical_ref or "")
            # Cut 4 rework: Guardian ALLOW/FLAG canonical outcomes report the
            # ACTUAL native projection posture (commit_promotion/commit_flagged
            # place it in the rollback plan the Decision carries); quarantine
            # and rejection never reach this branch.
            posture = _native_projection_posture(
                decision.canonical_ref or "",
                _plan,
                hook_report=hook_report,
                obligation_id=obligation_id,
            )
            receipt = WriteReceipt(
                cycle_id=envelope.cycle_id,
                staging_ref=staging_ref,
                path=decision.canonical_ref,
                validation_status="committed",
                message=f"committed via guardian {decision.kind}",
                durable=True,
                **posture,
            )
            _emit_write_memory_data(envelope, receipt)
            return receipt

        # Guardian declined OR commit failed downstream. Either way,
        # nothing landed in canonical memory. Receipt names the
        # decision kind so callers can branch on REJECT vs FLAG vs
        # QUARANTINE without parsing the message string.
        receipt = WriteReceipt(
            cycle_id=envelope.cycle_id,
            staging_ref=None,
            path=None,
            validation_status="rejected",
            message="Guardian proposal writes are unavailable in standalone mode",
        )
        _emit_write_memory_data(envelope, receipt)
        return receipt

    env_dict = _envelope_to_dict(envelope)

    # Direct-commit path — stages then immediately promotes.
    # Runtime guard: only permitted when bypass_guardian=True.
    if not bypass_guardian:
        raise ValueError(
            "direct commit (propose=False) requires bypass_guardian=True. "
            "Guardian arbitration is reserved for explicit user-chat ingress."
        )
    try:
        staging_ref = create_staging(env_dict)
        committed, canonical_ref, commit_info = commit_promotion(
            staging_ref, "memory.api:direct", skip_embed=skip_embed
        )
        if not committed:
            raise RuntimeError("direct memory commit failed without a canonical reference")
        hook_report = None
        obligation_id = None
        if not bool(dict(commit_info or {}).get("idempotent")):
            from memory.post_commit import run_post_commit_memory_hooks

            # Cut 4 rework 2: durable projection obligation BEFORE the hooks
            obligation = _open_projection_obligation(canonical_ref or "", commit_info, owner="memory.api:direct")
            obligation_id = obligation.get("journal_id")
            if obligation["hooks_allowed"]:
                hook_report = run_post_commit_memory_hooks(
                    canonical_ref or "",
                    coord=list(envelope.coord or []),
                    source=envelope.source,
                )
            else:
                hook_report = dict(_UNPROTECTED_HOOK_REPORT, memory_id=canonical_ref or "")
    except Exception:
        log.exception("write_memory direct commit failed")
        raise

    # Cut 4 rework: the write owner publishes the FINAL post-hook canonical
    # state; the receipt reports the final delta / dirty posture.
    posture = _native_projection_posture(canonical_ref or "", commit_info, hook_report=hook_report,
                                         obligation_id=obligation_id)
    receipt = WriteReceipt(
        cycle_id=envelope.cycle_id,
        staging_ref=None,
        path=canonical_ref,
        validation_status="committed",
        message="committed directly (Guardian-free memory path)",
        durable=True,
        **posture,
    )
    _emit_write_memory_data(envelope, receipt)
    return receipt


# =====================================================================
# promote_memory
# =====================================================================


def promote_memory(
    staging_ref: str,
    evidence_refs: List[str],
    approver_id: str,
) -> PromotionResult:
    """Reject the unavailable Guardian proposal path in standalone mode."""
    if not staging_ref or not approver_id:
        raise ValueError("promote_memory requires staging_ref and approver_id")
    from memory.promotion import STAGING_DIR

    try:
        record = json.loads((STAGING_DIR / f"{staging_ref}.json").read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(
            f"promote_memory cannot read staging record {staging_ref!r}: {exc}"
        ) from exc
    source = str(((record.get("envelope") or {}).get("source")) or "")
    if not _is_user_chat_source(source):
        raise ValueError(
            "promote_memory is reserved for user-chat ingress staging; "
            f"source={source!r} is not allowed"
        )
    from guardian.decision import decide_request

    decision = decide_request(staging_ref, approver_id=approver_id, evidence_refs=evidence_refs)

    result = PromotionResult(
        cycle_id=decision.cycle_id,
        staging_ref=decision.staging_ref or staging_ref,
        committed=decision.committed,
        canonical_ref=decision.canonical_ref,
        rollback_plan={
            **(decision.rollback_plan or {}),
            "intervention_id": decision.intervention_id,
        },
        reasons=(None if decision.committed else [f"decision={decision.kind}", decision.reason]),
    )
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "promote_memory",
            cycle_id=decision.cycle_id,
            summary=f"kind={decision.kind} committed={decision.committed}",
            staging_ref=result.staging_ref,
            committed=result.committed,
            canonical_ref=result.canonical_ref,
            decision_kind=decision.kind,
        )
    except Exception:
        pass
    return result


# =====================================================================
# rollback_memory
# =====================================================================


def rollback_memory(staging_ref: str) -> Dict[str, Any]:
    """Drop a staged memory without committing. Idempotent."""
    return rollback_promotion(staging_ref)


# =====================================================================
# decay_tick
# =====================================================================


def decay_tick(now_ts: Optional[str] = None) -> Dict[str, Any]:
    """Run one consolidation pass — flushes StrengthIndex dirty records
    and (eventually) prunes stale staging files. Resource-capped.
    """
    report = run_consolidation_once(limit_resources=True)
    report["now_ts"] = now_ts or _now_iso()
    return report


# =====================================================================
# resolve_contradiction (placeholder)
# =====================================================================


def adjudicate_contradiction_winner(
    canonical: dict,
    challenger: dict,
) -> dict:
    """Deterministic winner selection per config/memory.yaml contradiction rules."""
    from core.memory_contradiction import adjudicate_contradiction_pair

    return adjudicate_contradiction_pair(canonical, challenger)


def resolve_contradiction(
    canonical_ref: str,
    evidence_refs: List[str],
) -> ResolutionResult:
    """Queue a detected contradiction for DeepSleep restructuring.

    Per `feedback_guardian_boundary_not_cognition.md` doctrine:
    Guardian guards the boundary; the mind solves problems. Contradiction
    resolution is internal cognition, NOT boundary defense — so this
    function does NOT call decide_request. It queues the contradiction
    to the deepsleep_candidates journal and returns. DeepSleep's
    consolidation pass eventually picks up the candidates, picks a
    winner via core.policy.memory_policy.resolve_contradiction (the
    deterministic policy engine), and proposes a restructuring write.
    THAT proposal goes through Guardian — because it IS a write.

    What this function does:
      1. Validates canonical_ref resolves to a real memory file
         (path_for_id check + read).
      2. Validates each evidence_ref the same way; invalid refs are
         skipped with a note (not a hard failure).
      3. Queues canonical + each valid evidence to the deepsleep
         candidates journal with a `contradiction:...` reason string
         so DeepSleep can group them on consumption.
      4. Returns a ResolutionResult capturing what got queued + what
         was invalid + notes for the operator.

    `resolved` is ALWAYS False on a successful queue — this function
    queues, it does not resolve. `resolved=True` would imply Guardian
    or this layer adjudicated the contradiction; that's the doctrine
    violation we're explicitly avoiding.

    No L0 vector pre-filter happens here. DeepSleep does its own
    similarity check during consolidation; pre-filtering at queue
    time would be doing DeepSleep's job in the wrong layer.
    """
    from memory.engine.walker import path_for_id
    from memory.queues import enqueue_deepsleep_candidate

    evidence_refs = list(evidence_refs or [])
    notes_lines: List[str] = []
    queued_to: List[str] = []
    invalid_evidence: List[str] = []

    # 1. Validate canonical
    canonical_path = path_for_id(canonical_ref)
    if not canonical_path:
        return ResolutionResult(
            canonical_ref=canonical_ref,
            resolved=False,
            notes=f"canonical_ref={canonical_ref!r} does not resolve; nothing queued",
            evidence_refs=evidence_refs,
            queued_to=[],
            invalid_evidence_refs=evidence_refs,
        )

    canonical_coord: List[int] = []
    try:
        with open(canonical_path, "r", encoding="utf-8") as _f:
            canonical_doc = json.load(_f)
        canonical_coord = list(canonical_doc.get("coord") or [])
    except (OSError, json.JSONDecodeError) as exc:
        return ResolutionResult(
            canonical_ref=canonical_ref,
            resolved=False,
            notes=f"canonical_ref={canonical_ref!r} unreadable: {type(exc).__name__}: {exc}",
            evidence_refs=evidence_refs,
            queued_to=[],
            invalid_evidence_refs=evidence_refs,
        )

    # 2. Validate evidence refs
    valid_evidence: List[tuple] = []  # (ref, coord)
    for eref in evidence_refs:
        epath = path_for_id(eref)
        if not epath:
            invalid_evidence.append(eref)
            continue
        try:
            with open(epath, "r", encoding="utf-8") as _f:
                edoc = json.load(_f)
            valid_evidence.append((eref, list(edoc.get("coord") or [])))
        except (OSError, json.JSONDecodeError):
            invalid_evidence.append(eref)

    if not valid_evidence:
        notes_lines.append(f"no valid evidence (invalid: {invalid_evidence}); nothing queued")
        return ResolutionResult(
            canonical_ref=canonical_ref,
            resolved=False,
            notes=" | ".join(notes_lines),
            evidence_refs=evidence_refs,
            queued_to=[],
            invalid_evidence_refs=invalid_evidence,
        )

    # 3. Queue canonical + each valid evidence
    queue_reason = (
        f"contradiction:canonical={canonical_ref};evidence={','.join(e[0] for e in valid_evidence)}"
    )

    if enqueue_deepsleep_candidate(canonical_ref, canonical_coord, reason=queue_reason):
        queued_to.append(canonical_ref)
    else:
        notes_lines.append(f"queue append failed for canonical {canonical_ref!r}")

    for eref, ecoord in valid_evidence:
        if enqueue_deepsleep_candidate(eref, ecoord, reason=queue_reason):
            queued_to.append(eref)
        else:
            notes_lines.append(f"queue append failed for evidence {eref!r}")

    if invalid_evidence:
        notes_lines.append(f"invalid_evidence_skipped={invalid_evidence}")
    notes_lines.append(
        f"queued {len(queued_to)} ids to deepsleep_candidates; awaiting consolidation pass"
    )

    result = ResolutionResult(
        canonical_ref=canonical_ref,
        resolved=False,  # queued, not resolved — DeepSleep does that
        notes=" | ".join(notes_lines),
        evidence_refs=evidence_refs,
        queued_to=queued_to,
        invalid_evidence_refs=invalid_evidence,
    )
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "resolve_contradiction",
            summary=f"queued={len(queued_to)}",
            canonical_ref=canonical_ref,
            queued_count=len(queued_to),
            invalid_evidence_count=len(invalid_evidence),
            resolved=False,
        )
    except Exception:
        pass
    return result


# =====================================================================
# memory_health
# =====================================================================


def memory_health() -> MemoryHealth:
    """Lightweight diagnostic snapshot for the orchestrator + Omega."""
    _probe_embedding_substrate_if_unknown()
    substrate_status, substrate_detail = _observed_embedding_substrate()
    coord_index_path = OMEGA_ROOT / "memory" / "indexes" / "coord_index.json"
    si_state = _strength_index_state()
    staged = list_staged()
    health = MemoryHealth(
        snapshot_at=_now_iso(),
        coord_index_path=str(coord_index_path),
        coord_index_exists=coord_index_path.exists(),
        strength_records=si_state["records"],
        strength_dirty=si_state["dirty"],
        staging_count=len(staged),
        embedding_substrate=substrate_status,
        embedding_substrate_detail=substrate_detail,
    )
    if substrate_status == "degraded":
        health.issues.append(f"embedding substrate degraded: {substrate_detail}")
    # Cut 4: projection backlog / dirty state (derivative outbox; not truth)
    try:
        from memory.projection_delta import (
            native_citizenship_enabled,
            pending_projection_deltas,
            projection_dirty_state,
        )

        deltas, malformed = pending_projection_deltas()
        dirty = projection_dirty_state()
        health.projection_pending_count = len(deltas)
        health.projection_malformed_count = len(malformed)
        health.projection_dirty = bool(dirty.get("dirty"))
        health.projection_dirty_reason = str(dirty.get("reason") or "")
        health.native_citizenship_enabled = native_citizenship_enabled()
        if health.projection_dirty:
            health.issues.append(
                f"projection dirty — rebuild required: {health.projection_dirty_reason or 'unspecified'}"
            )
        if health.projection_pending_count:
            health.issues.append(
                f"projection backlog: {health.projection_pending_count} pending delta(s) "
                "(canonical valid; DME freshness lagging)"
            )
        if health.projection_malformed_count:
            health.issues.append(
                f"projection outbox has {health.projection_malformed_count} malformed line(s)"
            )
    except Exception as exc:  # noqa: BLE001
        health.issues.append(f"projection outbox unavailable: {exc!r}")
    # Cut 13: generation / coverage / lane state — never a "current" claim with lag or pending work
    try:
        from memory.projection.generation import projection_health

        ph = projection_health(_active_profile_root())
        health.projection_generation = ph.generation
        health.semantic_dme_lane = ph.semantic_dme_lane
        health.semantic_dme_lane_reason = ph.lane_reason or ""
        health.projection_current = ph.current
        health.projection_watermark_commit_seq = ph.watermark_commit_seq
        health.projection_latest_canonical_commit_seq = ph.latest_canonical_commit_seq
        health.projection_lag_commits = ph.lag_commits
        health.projection_head_clock = ph.head_clock
        health.projection_live_clock = ph.live_clock
        health.projection_reserved_seq = ph.reserved_seq
        health.projection_landed_seq = ph.landed_seq
        health.projection_coverage_seal = ph.coverage_seal
        health.projection_unsequenced_pending = ph.unsequenced_pending
        health.projection_unsequenced_posture = ph.unsequenced_posture
        health.projection_oldest_pending_age_s = ph.oldest_pending_age_s
        health.projection_blocked_on_obligations = ph.blocked_on_obligations
        health.projection_page0_utilization = dict(ph.page0_utilization_by_lobe)
        health.projection_live_intents = list(ph.live_intents)
        health.projection_program0 = ph.program0
        health.projection_pending_partial_bytes = ph.pending_partial_bytes
        if ph.pending_partial_bytes:
            health.issues.append(f"projection outbox: {ph.pending_partial_bytes} unterminated pending byte(s) beyond the settled cursor")
        if ph.program0 and ph.program0.get("cursor_offset") is not None:
            # Program 0 has settled a cursor: the honest backlog is the UNCONSUMED tail, not every row ever appended (Cut-4 count)
            health.issues = [i for i in health.issues if not i.startswith("projection backlog: ")]
            health.projection_pending_count = ph.pending_delta_count
            if ph.pending_delta_count:
                health.issues.append(f"projection backlog: {ph.pending_delta_count} pending delta(s) beyond the settled cursor (canonical valid; DME freshness lagging)")
        if ph.program0 and ph.program0.get("recovery_pending"):
            health.issues.append("projection outbox recovery in progress (rotate-first; rebuild pending)")
        if ph.program0 and ph.program0.get("malformed_receipts"):
            health.issues.append(f"projection outbox: {ph.program0['malformed_receipts']} malformed-row receipt(s) on record")
        if ph.semantic_dme_lane == "unavailable":
            health.issues.append(f"semantic DME lane unavailable: {ph.lane_reason or 'unspecified'}")
        elif ph.lane_reason:
            health.issues.append(f"projection lane: {ph.lane_reason}")
        if ph.lag_commits:
            health.issues.append(f"projection lag: {ph.lag_commits} commit(s) behind the live sequence (not current)")
    except Exception as exc:  # noqa: BLE001
        health.issues.append(f"projection generation health unavailable: {exc!r}")
    try:
        # Health is a cheap probe — reuse the interval-gated audit cache
        # (OMEGA_MEMORY_INTEGRITY_AUDIT_INTERVAL, default 300s) instead of
        # scanning the full corpus on every call. Explicit audit/repair
        # tools still force a fresh run.
        from memory.integrity.audit import run_integrity_audit_if_due

        integrity = run_integrity_audit_if_due()
        health.issues.extend(integrity.issues)
    except Exception as exc:
        health.issues.append(f"integrity audit unavailable: {exc!r}")
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "memory_health",
            summary=(f"strength={health.strength_records} staging={health.staging_count}"),
            strength_records=health.strength_records,
            strength_dirty=health.strength_dirty,
            staging_count=health.staging_count,
            coord_index_exists=health.coord_index_exists,
            embedding_substrate=health.embedding_substrate,
            projection_pending_count=health.projection_pending_count,
            projection_dirty=health.projection_dirty,
        )
    except Exception:
        pass
    return health


# Compatibility-named semantic-substrate health surface. The active substrate
# is the in-brain Ettin retina; no runtime dense-embedding worker participates.
_embedding_substrate_state: Dict[str, str] = {
    "status": "unknown",
    "detail": "boot has not reported L0 state yet",
}


def set_embedding_substrate_state(status: str, detail: str = "") -> None:
    """Boot/loader setter. status ∈ {'ok', 'degraded', 'failed', 'unknown'}."""
    if status not in ("ok", "degraded", "failed", "unknown"):
        raise ValueError(f"invalid status {status!r}")
    _embedding_substrate_state["status"] = status
    _embedding_substrate_state["detail"] = detail or ""


def _observed_embedding_substrate() -> tuple[str, str]:
    """Overlay the boot verdict with the live in-process Ettin seat."""
    status = _embedding_substrate_state["status"]
    detail = _embedding_substrate_state["detail"]
    if status != "ok":
        return status, detail
    try:
        from memory import retina

        stats = retina.stats()
        if stats.get("encoder_resident"):
            return status, detail
    except Exception as exc:  # noqa: BLE001 -- health probes never raise
        return status, f"{detail} (liveness probe unavailable: {exc!r})"
    return (
        "degraded",
        f"Ettin encoder not seated; loader verdict was: {detail}",
    )


def _probe_embedding_substrate_if_unknown() -> None:
    """Lazy-seat Ettin once so health is not stuck on unknown."""
    if _embedding_substrate_state["status"] != "unknown":
        return
    try:
        from memory import retina

        retina.encoder()
        ok = bool(retina.stats().get("encoder_resident"))
        if _embedding_substrate_state["status"] == "unknown":
            set_embedding_substrate_state(
                "ok" if ok else "failed",
                "probed via memory_health",
            )
    except Exception as exc:
        set_embedding_substrate_state(
            "failed",
            f"probe failed: {type(exc).__name__}: {exc}",
        )

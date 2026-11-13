"""Symphony §S11 — Idle Epistemic Substrate probes.

Bounded probes that emit lightweight `ProbeFinding` records when the
substrate notices idle signals (stale open curiosity, etc.) WITHOUT
invoking any executive movement. Per the v0.2 "idle is active"
principle: bounded probes, not loops; structured noticing, not
silent counting.

Framing law (binding for §S11):

  - Probes are CURIOSITY PRODUCERS, never movement initiators. This
    module MUST NOT import any executive surface (`core.symphony_
    runner`, `metacognition.*`); the §S11 audit's §2 asserts ZERO
    such imports + ZERO Name references to movement runners.
  - Findings are LIGHTWEIGHT — `ProbeFinding` carries `probe_name`,
    `reason`, `evidence`, `age_seconds`. Full `CuriosityEngineEntry`
    promotion (with scoring, recommended_path, etc.) is §S12's
    deliverable (evidence ladder).
  - At most ONE finding surfaces per tick across all probes. The
    bound is enforced at `run_idle_probes` — even with multiple
    probes firing, only the first non-None result is returned.

Public surface:

    ProbeFinding            — frozen dataclass with the operator-
                              locked field contract.
    run_idle_probes(ss, episode_store)
                            — list[ProbeFinding]; bounded to 0 or 1
                              per tick. Pure of executive surfaces;
                              never raises.
    _long_open_curiosity_probe(ss, episode_store)
                            — concrete probe: emits a finding when an
                              open curiosity entry's age exceeds the
                              threshold AND its parent's refractory
                              window has aged out.
    _reset_refractory_state() — test helper; clears module-level
                              refractory dict. Production code MUST
                              NOT call this.

Refractory mechanism: module-level `_REFRACTORY_STATE` tracks the
last-emit unix timestamp per parent `curiosity_id`. The same parent
cannot trigger a fresh finding within `refractory_seconds` (default
600s). Prevents flood when an open curiosity entry stays open
indefinitely.

Per [[feedback-s9-is-authority-not-function-pointer]] — the §S11
substrate-probing authority lives in its own module; CueDesk is the
attachment point (`curiosity_substrate_tick` calls `run_idle_probes`
and threads findings into its summary dict). Probes never reach for
executive surfaces; the dispatcher reads ignition, the substrate
reads curiosity.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from hud import curiosity_store

log = logging.getLogger("omega.pressure_loop.symphony_probes")


# ---------------------------------------------------------------------
# Defaults — tunable per-probe via kwargs; production callers use these
# ---------------------------------------------------------------------

_DEFAULT_OPEN_AGE_THRESHOLD_SECONDS: float = 600.0
_DEFAULT_REFRACTORY_SECONDS: float = 600.0


# ---------------------------------------------------------------------
# ProbeFinding — the lightweight structured signal §S11 surfaces
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeFinding:
    """A structured signal from an idle probe.

    Lightweight (NOT a full `CuriosityEngineEntry`) — surfaces on
    `CueDeskReport.idle_probe_findings` and the
    `curiosity_substrate_tick` summary dict.

    Phase 2 §P2-S15: extended with `weight` + `source_id` +
    `refractory_key` + `probe_class` (additive — `_long_open_curiosity_probe`
    keeps working via defaults). The full contract per PACKET_P2_S15.md
    RB#1 is: compact facts + weight + reason + source id + freshness
    + refractory key. The aggregating pressure contributor reads
    findings and sums `weight` into pressure; `probe_class` names
    the substrate class the probe surfaced (`stale_question`,
    `evidence_debt`, `learning_debt`, `stale_knowledge`,
    `guardian_debt`, `web_source_opportunity`, plus legacy
    `long_open_curiosity`).

    Fields:
        probe_name      — the probe function that emitted this finding
        probe_class     — the substrate class this finding belongs to
                          (P2-S15 contract; see PROBE_CLASSES tuple)
        reason          — stable machine-readable reason string
        evidence        — structured dict naming substrate slot(s)
        age_seconds     — freshness scalar (substrate-event age)
        weight          — pressure weight in [0, 1] this finding
                          contributes to the aggregate contributor
        source_id       — identifier of the substrate record (e.g.,
                          curiosity_id, question_id, intervention_id)
        refractory_key  — key the aggregator uses to filter already-
                          handled findings; defaults to source_id
                          when omitted
    """

    probe_name: str
    reason: str
    evidence: Dict[str, Any] = field(default_factory=dict)
    age_seconds: float = 0.0
    # Phase 2 §P2-S15 additive fields (defaults preserve back-compat):
    weight: float = 0.0
    source_id: str = ""
    refractory_key: str = ""
    probe_class: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """JSON-friendly view for tick summaries / trace surfaces."""
        return {
            "probe_name": self.probe_name,
            "probe_class": self.probe_class,
            "reason": self.reason,
            "evidence": dict(self.evidence),
            "age_seconds": float(self.age_seconds),
            "weight": float(self.weight),
            "source_id": self.source_id,
            "refractory_key": self.refractory_key or self.source_id,
        }


# Canonical probe class set per PACKET_P2_S15.md RB#2. The substrate
# tick surfaces probe_classes_seen as a subset of this tuple; the
# pressure aggregator filters findings using these names. Adding a
# new probe class requires updating this tuple AND the audit's §3
# fixtures (verifies all classes can fire under deterministic state).
PROBE_CLASSES: tuple[str, ...] = (
    "long_open_curiosity",  # legacy §S11
    "stale_question",  # open operator question past age
    "evidence_debt",  # unresolved curiosity count
    "learning_debt",  # P2-S13 learning_state debt flags
    "stale_knowledge",  # mastery freshness past threshold
    "guardian_debt",  # Guardian adversarial intervention (with handled cache)
    "web_source_opportunity",  # durable hint about external evidence (NO network)
)

# Probe classes whose findings are ACTION/HOLD-MEANINGFUL per packet
# RB#4 — they may override pure epistemic_emptiness routing. Low-
# severity probes (learning_debt, stale_knowledge, evidence_debt,
# web_source_opportunity) default to NO_ACTION_OBSERVE.
MEANINGFUL_PROBE_CLASSES: frozenset[str] = frozenset(
    {
        "guardian_debt",
        "stale_question",
    }
)


# ---------------------------------------------------------------------
# Refractory state — module-level dedup per parent curiosity_id
# ---------------------------------------------------------------------

# Maps parent_curiosity_id → last-emit unix timestamp. Cleared only
# by `_reset_refractory_state()` (test helper); production code never
# resets — the refractory is intentional flood-protection.
_REFRACTORY_STATE: Dict[str, float] = {}


def _reset_refractory_state() -> None:
    """Test helper — clears refractory state. Production code must
    NEVER call this; the refractory mechanism is the §S11 flood
    guard ("repeated idle cannot flood duplicate curiosities" per
    SWING §S11 acceptance)."""
    _REFRACTORY_STATE.clear()


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _now_unix() -> float:
    return time.time()


def _emit_probe_finding(finding: ProbeFinding) -> None:
    try:
        from guardian.terminal_feedback import emit_data

        probe_class = finding.probe_class or finding.probe_name
        emit_data(
            "pressure",
            f"probe_{probe_class}",
            probe_type=probe_class,
            probe_name=finding.probe_name,
            reason=finding.reason,
            weight=finding.weight,
            source_id=finding.source_id,
            age_seconds=finding.age_seconds,
        )
    except Exception:
        pass


def _parse_iso_age_seconds(iso_string: str, now_unix: float) -> float:
    """Parse ISO-8601 timestamp; return age in seconds vs now_unix.
    Returns 0.0 on parse failure (safe — won't trigger stale-age
    branch on garbage timestamps)."""
    if not iso_string:
        return 0.0
    try:
        dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
        return max(0.0, now_unix - dt.timestamp())
    except Exception:
        return 0.0


# ---------------------------------------------------------------------
# Concrete probe — _long_open_curiosity_probe
# ---------------------------------------------------------------------


def _long_open_curiosity_probe(
    shared_state,
    episode_store,
    *,
    open_age_threshold_seconds: float = _DEFAULT_OPEN_AGE_THRESHOLD_SECONDS,
    refractory_seconds: float = _DEFAULT_REFRACTORY_SECONDS,
) -> Optional[ProbeFinding]:
    """Detects an open curiosity entry whose age exceeds the threshold
    AND whose parent `curiosity_id` is not in the refractory window.

    Reads open entries via `curiosity_store.list_open()` — the §S7
    cure surface ("live curiosity store" per §S6 / §S7 union). Sorts
    eligible candidates by age (oldest first) for deterministic
    picking when multiple stale entries exist.

    Returns:
        Optional[ProbeFinding] — None when no stale-and-refresh-eligible
        entries exist; a finding (and refractory state updated) when
        one fires. The 0-or-1-per-tick bound is enforced at the
        `run_idle_probes` layer (this function returns at most one).

    Never raises. Degraded paths (store import / list_open failure /
    malformed timestamps) return None silently — the substrate is
    best-effort + the absence of a finding is indistinguishable from
    "nothing to surface this tick" by design.
    """
    try:
        open_entries = curiosity_store.list_open()
    except Exception as exc:  # noqa: BLE001
        log.debug("long_open_curiosity_probe: list_open failed: %s", exc)
        return None

    now = _now_unix()

    candidates: List[tuple] = []
    for entry in open_entries:
        try:
            age = _parse_iso_age_seconds(entry.created_at, now)
        except Exception:
            continue
        if age < open_age_threshold_seconds:
            continue
        last_emit = _REFRACTORY_STATE.get(entry.curiosity_id, 0.0)
        if (now - last_emit) < refractory_seconds:
            continue
        candidates.append((age, entry))

    if not candidates:
        return None

    candidates.sort(key=lambda kv: kv[0], reverse=True)
    age, entry = candidates[0]

    _REFRACTORY_STATE[entry.curiosity_id] = now

    return ProbeFinding(
        probe_name="long_open_curiosity",
        probe_class="long_open_curiosity",
        reason="open_curiosity_past_age_threshold",
        evidence={
            "parent_curiosity_id": entry.curiosity_id,
            "age_seconds": age,
            "open_age_threshold_seconds": open_age_threshold_seconds,
            "problem_statement_excerpt": ((entry.problem_statement or "")[:80]),
        },
        age_seconds=age,
        # P2-S15 contract additive fields. Long-open curiosity is
        # mid-severity; weight stays modest so legacy contributors
        # remain the dominant routing signal when they fire.
        weight=0.25,
        source_id=entry.curiosity_id,
        refractory_key=entry.curiosity_id,
    )


# ---------------------------------------------------------------------
# Phase 2 §P2-S15 — broader probe set
#
# Per-probe refractory caches at module scope. Each cache maps
# refractory_key → last-emit unix timestamp; the same key cannot
# emit again until refractory_seconds have passed. The
# `_GUARDIAN_INTERVENTION_HANDLED` cache for the guardian probe lives
# in `core.pressure_field` instead (alongside the existing handled-
# cache infrastructure + WAL export/import seam) — packet RB#3
# mandates restart safety for that one.
# ---------------------------------------------------------------------

_STALE_QUESTION_REFRACTORY: Dict[str, float] = {}
_EVIDENCE_DEBT_REFRACTORY: Dict[str, float] = {}
_LEARNING_DEBT_REFRACTORY: Dict[str, float] = {}
_STALE_KNOWLEDGE_REFRACTORY: Dict[str, float] = {}
_WEB_SOURCE_REFRACTORY: Dict[str, float] = {}

# Default per-probe age + refractory thresholds. All tunable for
# tests; production callers use these as the bounded floor.
_DEFAULT_STALE_QUESTION_AGE_SECONDS: float = 600.0
_DEFAULT_STALE_QUESTION_REFRACTORY_SECONDS: float = 600.0
_DEFAULT_EVIDENCE_DEBT_MIN_OPEN_COUNT: int = 3
_DEFAULT_EVIDENCE_DEBT_REFRACTORY_SECONDS: float = 300.0
_DEFAULT_LEARNING_DEBT_REFRACTORY_SECONDS: float = 300.0
_DEFAULT_STALE_KNOWLEDGE_REFRACTORY_SECONDS: float = 1800.0
_DEFAULT_WEB_SOURCE_REFRACTORY_SECONDS: float = 600.0


def _reset_all_broad_probe_refractory_state() -> None:
    """Clear every P2-S15 probe refractory cache. Test helper —
    production code never resets; the bounds are intentional flood
    guards."""
    _STALE_QUESTION_REFRACTORY.clear()
    _EVIDENCE_DEBT_REFRACTORY.clear()
    _LEARNING_DEBT_REFRACTORY.clear()
    _STALE_KNOWLEDGE_REFRACTORY.clear()
    _WEB_SOURCE_REFRACTORY.clear()


def _stale_open_question_probe(
    shared_state,
    episode_store,
    *,
    age_threshold_seconds: float = _DEFAULT_STALE_QUESTION_AGE_SECONDS,
    refractory_seconds: float = _DEFAULT_STALE_QUESTION_REFRACTORY_SECONDS,
) -> Optional[ProbeFinding]:
    """Probe: an open question whose age exceeds the threshold.

    Reads `pressure_loop.open_questions.list_open()` read-only. Picks the
    oldest open question past the age threshold not currently in
    refractory. Action/hold-meaningful per RB#4 — the operator is
    waiting on an answer, so this probe carries a higher weight and
    is in MEANINGFUL_PROBE_CLASSES.
    """
    try:
        from pressure_loop import open_questions

        open_qs = open_questions.list_open() or []
    except Exception as exc:  # noqa: BLE001 - never raise
        log.debug("stale_open_question_probe: list_open failed: %s", exc)
        return None
    if not open_qs:
        return None

    now = _now_unix()
    candidates: List[tuple] = []
    for q in open_qs:
        created_at = getattr(q, "created_at", "") or ""
        age = _parse_iso_age_seconds(created_at, now)
        if age < age_threshold_seconds:
            continue
        qid = str(getattr(q, "question_id", "") or "")
        if not qid:
            continue
        last_emit = _STALE_QUESTION_REFRACTORY.get(qid, 0.0)
        if (now - last_emit) < refractory_seconds:
            continue
        candidates.append((age, qid, q))
    if not candidates:
        return None
    candidates.sort(key=lambda kv: kv[0], reverse=True)
    age, qid, q = candidates[0]
    _STALE_QUESTION_REFRACTORY[qid] = now

    return ProbeFinding(
        probe_name="stale_open_question",
        probe_class="stale_question",
        reason="open_question_past_age_threshold",
        evidence={
            "question_id": qid,
            "age_seconds": age,
            "age_threshold_seconds": age_threshold_seconds,
            "act": str(getattr(q, "act", "") or ""),
            "question_text_excerpt": (str(getattr(q, "question_text", "") or "")[:80]),
        },
        age_seconds=age,
        # Stale operator question is HIGH-SEVERITY (operator is
        # waiting for cognition's answer) — meaningful for routing.
        weight=0.55,
        source_id=qid,
        refractory_key=qid,
    )


def _evidence_debt_probe(
    shared_state,
    episode_store,
    *,
    min_open_count: int = _DEFAULT_EVIDENCE_DEBT_MIN_OPEN_COUNT,
    refractory_seconds: float = _DEFAULT_EVIDENCE_DEBT_REFRACTORY_SECONDS,
) -> Optional[ProbeFinding]:
    """Probe: aggregate count of open curiosities past a minimum
    threshold. Surfaces "the system has accumulated unresolved
    evidence work" as a low-severity substrate fact.

    Reads `curiosity_store.list_open()` read-only. Refractory key is
    the open count itself, so a fresh debt level (different count)
    refires; same count within the window stays quiet.
    """
    try:
        open_entries = curiosity_store.list_open() or []
    except Exception as exc:  # noqa: BLE001
        log.debug("evidence_debt_probe: list_open failed: %s", exc)
        return None
    count = len(open_entries)
    if count < min_open_count:
        return None
    refractory_key = f"open_count:{count}"
    now = _now_unix()
    last_emit = _EVIDENCE_DEBT_REFRACTORY.get(refractory_key, 0.0)
    if (now - last_emit) < refractory_seconds:
        return None
    _EVIDENCE_DEBT_REFRACTORY[refractory_key] = now
    return ProbeFinding(
        probe_name="evidence_debt",
        probe_class="evidence_debt",
        reason="open_curiosity_count_past_threshold",
        evidence={
            "open_count": count,
            "min_open_count": min_open_count,
            # Compact id list capped so envelope stays small
            "sample_curiosity_ids": [
                str(getattr(e, "curiosity_id", "") or "") for e in open_entries[:5]
            ],
        },
        age_seconds=0.0,
        # Evidence debt is LOW-SEVERITY substrate state — informs
        # observe routing, doesn't demand action. Weight kept small.
        weight=0.15,
        source_id=refractory_key,
        refractory_key=refractory_key,
    )


def _learning_debt_probe(
    shared_state,
    episode_store,
    *,
    refractory_seconds: float = _DEFAULT_LEARNING_DEBT_REFRACTORY_SECONDS,
) -> Optional[ProbeFinding]:
    """Probe: P2-S13 learning-state envelope's debt flags.

    Composes a fresh `episode_learning_state` envelope (read-only
    over StrengthIndex + mastery + no recall_receipts at idle) and
    fires when ANY learning_debt_flag is True. Refractory key is
    the flag signature so a different debt pattern refires while
    the same pattern stays quiet.
    """
    try:
        from pressure_loop.symphony_learning_state import build_episode_learning_state

        envelope = build_episode_learning_state()
    except Exception as exc:  # noqa: BLE001
        log.debug("learning_debt_probe: build envelope failed: %s", exc)
        return None
    flags = envelope.get("learning_debt_flags") or {}
    if not isinstance(flags, dict):
        return None
    fired = {k: bool(v) for k, v in flags.items() if bool(v)}
    if not fired:
        return None
    # Refractory key is the sorted-flag-name signature
    sig = "|".join(sorted(fired.keys()))
    refractory_key = f"debt_sig:{sig}"
    now = _now_unix()
    last_emit = _LEARNING_DEBT_REFRACTORY.get(refractory_key, 0.0)
    if (now - last_emit) < refractory_seconds:
        return None
    _LEARNING_DEBT_REFRACTORY[refractory_key] = now
    return ProbeFinding(
        probe_name="learning_debt",
        probe_class="learning_debt",
        reason="learning_debt_flags_present",
        evidence={
            "flags_fired": sorted(fired.keys()),
            "envelope_source": str(envelope.get("source") or ""),
            "captured_at_iso": str(envelope.get("captured_at_iso") or ""),
        },
        age_seconds=0.0,
        # Learning debt is LOW-SEVERITY informational signal — observe
        # routing is the correct default per packet RB#4.
        weight=0.18,
        source_id=refractory_key,
        refractory_key=refractory_key,
    )


def _stale_knowledge_probe(
    shared_state,
    episode_store,
    *,
    refractory_seconds: float = _DEFAULT_STALE_KNOWLEDGE_REFRACTORY_SECONDS,
) -> Optional[ProbeFinding]:
    """Probe: mastery freshness anchor past stale threshold.

    Distinct from `learning_debt` (which combines multiple flags) —
    this one focuses ONLY on `mastery.oldest_updated_at` so consumers
    can branch on "knowledge surveys are old" separately from
    "knowledge surveys are empty."
    """
    try:
        from pressure_loop.symphony_learning_state import build_episode_learning_state

        envelope = build_episode_learning_state()
    except Exception as exc:  # noqa: BLE001
        log.debug("stale_knowledge_probe: build envelope failed: %s", exc)
        return None
    flags = envelope.get("learning_debt_flags") or {}
    if not isinstance(flags, dict) or not flags.get("stale_mastery"):
        return None
    mastery = envelope.get("mastery") or {}
    oldest_iso = str((mastery or {}).get("oldest_updated_at") or "")
    if not oldest_iso:
        return None
    refractory_key = f"stale_mastery_at:{oldest_iso}"
    now = _now_unix()
    last_emit = _STALE_KNOWLEDGE_REFRACTORY.get(refractory_key, 0.0)
    if (now - last_emit) < refractory_seconds:
        return None
    _STALE_KNOWLEDGE_REFRACTORY[refractory_key] = now
    age_seconds = _parse_iso_age_seconds(oldest_iso, now)
    return ProbeFinding(
        probe_name="stale_knowledge",
        probe_class="stale_knowledge",
        reason="mastery_oldest_updated_at_past_threshold",
        evidence={
            "oldest_updated_at": oldest_iso,
            "age_seconds": age_seconds,
            "domains_count": int((mastery or {}).get("domains_count") or 0),
        },
        age_seconds=age_seconds,
        weight=0.15,
        source_id=refractory_key,
        refractory_key=refractory_key,
    )


def _guardian_intervention_probe(
    shared_state,
    episode_store,
) -> Optional[ProbeFinding]:
    """Probe: surfaces a Guardian adversarial intervention as a probe
    class finding, applying the same-event handled cache so the same
    `intervention_id` doesn't refire forever (packet RB#3).

    The handled cache lives in `pressure_loop.pressure_field` — the
    same module that owns the `_GUARDIAN_INTERVENTION_HANDLED` /
    evidence_terminal handled caches.
    """
    try:
        from pressure_loop.cycle_cadence import _recent_jsonl_entries
        from pressure_loop.pressure_field import (
            _GUARDIAN_INTERVENTION_HANDLED,
            _GUARDIAN_ADVERSARIAL_KINDS,
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("guardian_intervention_probe: imports failed: %s", exc)
        return None
    try:
        for entry in _recent_jsonl_entries(("guardian", "logs", "guardian_interventions.jsonl")):
            kind = str(entry.get("kind", "")).strip().lower()
            if kind not in _GUARDIAN_ADVERSARIAL_KINDS:
                continue
            iid = str(entry.get("intervention_id", "") or "")
            if not iid:
                continue
            if iid in _GUARDIAN_INTERVENTION_HANDLED:
                continue
            ts = str(entry.get("ts") or entry.get("timestamp") or "")
            age_seconds = _parse_iso_age_seconds(ts, _now_unix())
            return ProbeFinding(
                probe_name="guardian_intervention",
                probe_class="guardian_debt",
                reason="guardian_adversarial_intervention_unhandled",
                evidence={
                    "intervention_id": iid,
                    "kind": kind,
                    "ts": ts,
                    "target_id": str(entry.get("target_id", "") or ""),
                    "reason": str(entry.get("reason", "") or "")[:120],
                },
                age_seconds=age_seconds,
                # Guardian block is HIGH-SEVERITY — meaningful for routing.
                weight=0.75,
                source_id=iid,
                refractory_key=iid,
            )
    except Exception as exc:  # noqa: BLE001
        log.debug("guardian_intervention_probe: ledger scan failed: %s", exc)
        return None
    return None


def _web_source_opportunity_probe(
    shared_state,
    episode_store,
    *,
    refractory_seconds: float = _DEFAULT_WEB_SOURCE_REFRACTORY_SECONDS,
) -> Optional[ProbeFinding]:
    """Probe: existing durable hints that external evidence may be
    useful. PURE READ — must NOT perform any network/web/HTTP access
    per packet Must-Not-Touch.

    Hint source for this slice: any open curiosity entry whose
    `trigger_source` matches a "web-source-able" pattern OR whose
    problem_statement names an external knowledge gap. The probe
    surfaces the existence of such hints; downstream routing handles
    them (later operator-directed slice may wire actual web tool
    invocation).
    """
    try:
        open_entries = curiosity_store.list_open() or []
    except Exception as exc:  # noqa: BLE001
        log.debug("web_source_opportunity_probe: list_open failed: %s", exc)
        return None
    web_hint_entries: List[Any] = []
    for entry in open_entries:
        trigger = str(getattr(entry, "trigger_source", "") or "").lower()
        statement = str(getattr(entry, "problem_statement", "") or "").lower()
        # Existing durable hint patterns — additive, no scraping
        if (
            "web" in trigger
            or "external" in trigger
            or "web source" in statement
            or "needs external" in statement
            or "no internal record" in statement
        ):
            web_hint_entries.append(entry)
    if not web_hint_entries:
        return None
    first = web_hint_entries[0]
    source_id = str(getattr(first, "curiosity_id", "") or "")
    if not source_id:
        return None
    now = _now_unix()
    last_emit = _WEB_SOURCE_REFRACTORY.get(source_id, 0.0)
    if (now - last_emit) < refractory_seconds:
        return None
    _WEB_SOURCE_REFRACTORY[source_id] = now
    return ProbeFinding(
        probe_name="web_source_opportunity",
        probe_class="web_source_opportunity",
        reason="existing_durable_hint_names_external_evidence",
        evidence={
            "curiosity_id": source_id,
            "trigger_source": str(getattr(first, "trigger_source", "") or ""),
            "problem_statement_excerpt": (str(getattr(first, "problem_statement", "") or "")[:80]),
            "candidate_count": len(web_hint_entries),
        },
        age_seconds=0.0,
        # Low-severity informational — observe routing per RB#4.
        weight=0.15,
        source_id=source_id,
        refractory_key=source_id,
    )


# ---------------------------------------------------------------------
# Probe registry + runner
# ---------------------------------------------------------------------

# Registered probes. Each probe registered here may emit AT MOST ONE
# finding per tick (its own refractory cache bounds repeat-fire); the
# runner aggregates up to N findings per tick where N = number of
# probes. The original §S11 "0 or 1 across all probes" bound was about
# CURIOSITY CREATION (full CuriosityEngineEntry promotion) — findings
# are NOT curiosities (per ProbeFinding docstring), so the broader
# probe set may safely surface multiple per tick. Per-probe refractory
# still prevents flood on the same substrate fact.
_PROBES: tuple[Callable[..., Optional[ProbeFinding]], ...] = (
    _long_open_curiosity_probe,
    _stale_open_question_probe,
    _evidence_debt_probe,
    _learning_debt_probe,
    _stale_knowledge_probe,
    _guardian_intervention_probe,
    _web_source_opportunity_probe,
)


def run_idle_probes(
    shared_state,
    episode_store,
) -> List[ProbeFinding]:
    """Run registered idle probes; return up to one ProbeFinding per
    registered probe (max N = len(_PROBES) per tick).

    Phase 2 §P2-S15 loosens the prior "0 or 1 across all probes" bound
    (which was a §S11-era over-tightening of "0 or 1 curiosity per
    tick"). Findings are lightweight evidence — not curiosity
    creations — so the broader probe set may safely surface multiple
    per tick. Per-probe refractory caches still bound repeat-fire on
    the same substrate fact.

    Pure of executive surfaces: this function NEVER invokes any
    movement runner. Per the framing law
    [[feedback-s9-is-authority-not-function-pointer]]: probe is a
    CURIOSITY PRODUCER, never a movement initiator.

    Never raises. A probe that raises lands in debug-level log; the
    runner skips to the next probe without aborting the tick.
    """
    findings: List[ProbeFinding] = []
    for probe in _PROBES:
        try:
            finding = probe(shared_state, episode_store)
        except Exception as exc:  # noqa: BLE001
            log.debug(
                "run_idle_probes: probe %s raised: %s",
                getattr(probe, "__name__", "<anonymous>"),
                exc,
            )
            continue
        if finding is not None:
            _emit_probe_finding(finding)
            findings.append(finding)
    return findings

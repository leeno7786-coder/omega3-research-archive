"""The window response: shared control, ten-field reflection, one trajectory proposal.

CONTRACTS.md section 6. Three rules matter most here.

**The shared control object is imported, never redesigned.** A second control vocabulary is
the thing the charter forbids outright, so this module validates control through
``schema.omega_control_decision`` and holds no decision names of its own.

**The optional cognitive delta survives.** ``decisions``, ``user_constraints`` and
``lane_injections`` are live cross-lane behaviour (``meta_omega/core.py``). The older draft's
"exactly three top-level fields" wording would have deleted them; CONTRACTS.md refines it and
this contract admits them beside the three required keys.

**One trajectory vocabulary, the producer's.** WO-01 measured three disagreeing sets: the
typed store's, the window prompt's, and the one the reseed branch reads. The store wins —
:data:`TRAJECTORY_OPERATIONS` is ``schema.omega_trajectory.TRAJECTORY_DECISION_VALUES``
imported, not restated — so the retired prompt words are refused rather than silently mapped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from schema.continuity_json import (
    CONTINUITY_SCHEMA_VERSION,
    CanonicalRef,
    ContinuityContractError,
    Observation,
    WindowOutputError,
    content_hash,
    freeze,
    require_enum,
    require_keys,
    require_text,
    unfreeze,
)
from schema.omega_control_decision import OmegaControlDecision
from schema.omega_trajectory import TRAJECTORY_DECISION_VALUES
from schema.window_evidence import EVIDENCE_GRADES, UNCERTAINTY_LEVELS

SCHEMA_VERSION = f"{CONTINUITY_SCHEMA_VERSION}.window_reflection"

WINDOW_RESPONSE_KEYS: tuple[str, ...] = ("control", "reflection", "trajectory_update")

#: The exact existing allowlist. Frozen here so a later window change cannot delete a
#: working cross-lane feature by tightening the response schema.
COGNITIVE_DELTA_KEYS: tuple[str, ...] = ("decisions", "user_constraints", "lane_injections")

REFLECTION_FIELDS: tuple[str, ...] = (
    "summary",
    "coverage",
    "episode_assessments",
    "temporal_findings",
    "patterns",
    "learning_followthrough",
    "thread_updates",
    "proactive_candidates",
    "limitations",
    "trajectory_assessment",
)

#: Output-contract limits. NOT evidence thresholds and NOT user-scope rules: they bound what
#: one response may say, nothing about what is true.
REFLECTION_LIMITS: Mapping[str, int] = freeze(
    {
        "temporal_findings": 8,
        "patterns": 8,
        "learning_followthrough": 16,
        "thread_updates": 16,
        "proactive_candidates": 4,
        "limitations": 16,
        "evidence_refs_per_finding": 16,
        "claim_chars": 2000,
        "summary_chars": 4000,
    }
)

#: The authoritative trajectory operations, imported from the typed store's own vocabulary.
TRAJECTORY_OPERATIONS: tuple[str, ...] = TRAJECTORY_DECISION_VALUES

#: Words an earlier prompt draft used that no applier ever supported.
_RETIRED_TRAJECTORY_WORDS = frozenset({"continue", "adjust", "complete_window", "establish_new"})

#: Host state a model may never return in place of the host's own.
_HOST_OWNED_KEYS = frozenset(
    {
        "generation_id",
        "coverage_clock",
        "commit_seq",
        "clock_epoch_id",
        "snapshot_id",
        "window_version",
        "configuration_fingerprint",
        "expected_trajectory_revision",
        "expected_grant_revision",
        "authority_snapshot_ref",
        "receipt",
        "content_sha256",
    }
)

_COVERAGE_REQUIRED = ("member_ids", "declared_limitations")

_ASSESSMENT_REQUIRED = (
    "episode_id",
    "assessment",
    "evidence_refs",
    "outcome",
    "uncertainty",
    "unresolved_condition",
)

_FINDING_REQUIRED = (
    "claim",
    "relation_kind",
    "episode_ids",
    "support_refs",
    "counterevidence_refs",
    "uncertainty",
)

_PATTERN_REQUIRED = (
    "claim",
    "applicability",
    "support_refs",
    "counterevidence_refs",
    "metric_refs",
    "evidence_grade",
    "falsifier",
)

_FOLLOWTHROUGH_STATES = (
    "committed",
    "exposed",
    "reported_use",
    "behaviourally_applied",
    "eligible_opportunity",
    "evaluated_result",
)
_FOLLOWTHROUGH_REQUIRED = ("intervention_ref",) + _FOLLOWTHROUGH_STATES + ("support_refs",)

_THREAD_EXISTING_REQUIRED = ("thread_ref", "proposed_state", "change", "evidence_refs")
_THREAD_NEW_REQUIRED = ("new_thread_request", "proposed_state", "change", "evidence_refs")

_PROACTIVE_REQUIRED = (
    "what",
    "why_now",
    "articulation",
    "delivery_conditions",
    "ownership_refs",
    "dedup_refs",
)

_TRAJECTORY_ASSESSMENT_REQUIRED = (
    "start_snapshot_ref",
    "cutoff_snapshot_ref",
    "assessment",
    "reason",
    "evidence_refs",
)

_TRAJECTORY_UPDATE_REQUIRED = ("operation", "reason")
_TRAJECTORY_UPDATE_OPTIONAL = ("target_domain", "target_capability", "confidence")


#: Subtrees whose content is a reference the HOST already gave the model. Quoting one back
#: is a citation, not an attempt to mint host state, so the host-key scan steps over them —
#: their shape is validated as a typed reference instead.
_CITATION_SUBTREES = frozenset({"thread_ref", "ref", "start_snapshot_ref", "cutoff_snapshot_ref"})


def _reject_host_keys(value: Any, *, where: str) -> None:
    """Refuse model-returned host state: receipts, grant versions, authoritative snapshot ids.

    Deliberately NOT a blanket ban on the words: a model citing an existing canonical thread
    must be able to quote that reference, hash included. Only keys the model would be
    *asserting* about host state are refused, and citation subtrees are skipped.
    """
    if isinstance(value, Mapping):
        offending = sorted(set(value) & _HOST_OWNED_KEYS)
        if offending:
            raise WindowOutputError(
                f"{where} returns host-owned state {offending}; the host supplies its own "
                "identities, receipts and revisions"
            )
        for key, item in value.items():
            if key in _CITATION_SUBTREES:
                continue
            _reject_host_keys(item, where=f"{where}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_host_keys(item, where=f"{where}[{index}]")


def _bounded_rows(value: Any, *, where: str, maximum: int) -> tuple[Any, ...]:
    if not isinstance(value, (list, tuple)):
        raise WindowOutputError(f"{where} must be a JSON array")
    rows = tuple(value)
    if len(rows) > maximum:
        raise WindowOutputError(f"{where} admits at most {maximum} rows, got {len(rows)}")
    return rows


def _claim(value: Any, *, where: str) -> str:
    try:
        return require_text(value, where=where, limit=int(REFLECTION_LIMITS["claim_chars"]))
    except ContinuityContractError as exc:
        raise WindowOutputError(str(exc)) from exc


def _refs(value: Any, *, where: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise WindowOutputError(f"{where} must be a JSON array")
    rows = tuple(value)
    if len(rows) > int(REFLECTION_LIMITS["evidence_refs_per_finding"]):
        raise WindowOutputError(
            f"{where} admits at most {REFLECTION_LIMITS['evidence_refs_per_finding']} references"
        )
    for index, item in enumerate(rows):
        if not isinstance(item, str) or not item.strip():
            raise WindowOutputError(f"{where}[{index}] must be a nonempty reference string")
    return tuple(str(item) for item in rows)


def _checked_coverage(value: Any, *, member_ids: Sequence[str]) -> dict[str, Any]:
    try:
        data = require_keys(
            value, required=_COVERAGE_REQUIRED, where="reflection.coverage", error=WindowOutputError
        )
    except ContinuityContractError as exc:
        raise WindowOutputError(str(exc)) from exc
    declared = data["member_ids"]
    if not isinstance(declared, (list, tuple)):
        raise WindowOutputError("reflection.coverage.member_ids must be a JSON array")
    if tuple(str(item) for item in declared) != tuple(member_ids):
        raise WindowOutputError(
            "reflection.coverage.member_ids does not equal the sealed manifest; the host "
            "recomputes coverage and a model may not certify its own input completeness"
        )
    if not isinstance(data["declared_limitations"], (list, tuple)):
        raise WindowOutputError("reflection.coverage.declared_limitations must be a JSON array")
    return data


def _checked_assessments(value: Any, *, member_ids: Sequence[str]) -> tuple[dict[str, Any], ...]:
    rows = _bounded_rows(value, where="reflection.episode_assessments", maximum=len(member_ids))
    if len(rows) != len(member_ids):
        raise WindowOutputError(
            f"reflection.episode_assessments must hold exactly one row per focal member "
            f"({len(member_ids)}), got {len(rows)}"
        )
    seen: list[str] = []
    out: list[dict[str, Any]] = []
    from schema.episode_record import TASK_OUTCOMES

    for index, row in enumerate(rows):
        try:
            data = require_keys(
                row,
                required=_ASSESSMENT_REQUIRED,
                where=f"reflection.episode_assessments[{index}]",
                error=WindowOutputError,
            )
        except ContinuityContractError as exc:
            raise WindowOutputError(str(exc)) from exc
        episode_id = str(data["episode_id"])
        if episode_id not in member_ids:
            raise WindowOutputError(
                f"reflection.episode_assessments[{index}] names {episode_id!r}, which is not a member"
            )
        if episode_id in seen:
            raise WindowOutputError(f"reflection.episode_assessments names {episode_id!r} twice")
        seen.append(episode_id)
        data["assessment"] = _claim(data["assessment"], where=f"reflection.episode_assessments[{index}].assessment")
        data["evidence_refs"] = _refs(
            data["evidence_refs"], where=f"reflection.episode_assessments[{index}].evidence_refs"
        )
        require_enum(
            data["outcome"],
            allowed=TASK_OUTCOMES,
            where=f"reflection.episode_assessments[{index}].outcome",
            error=WindowOutputError,
        )
        require_enum(
            data["uncertainty"],
            allowed=UNCERTAINTY_LEVELS,
            where=f"reflection.episode_assessments[{index}].uncertainty",
            error=WindowOutputError,
        )
        if not isinstance(data["unresolved_condition"], str):
            raise WindowOutputError(
                f"reflection.episode_assessments[{index}].unresolved_condition must be text"
            )
        out.append(data)
    if tuple(seen) != tuple(member_ids):
        raise WindowOutputError(
            "reflection.episode_assessments must account for every focal member exactly once"
        )
    return tuple(out)


def _checked_findings(value: Any) -> tuple[dict[str, Any], ...]:
    rows = _bounded_rows(
        value, where="reflection.temporal_findings", maximum=int(REFLECTION_LIMITS["temporal_findings"])
    )
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        where = f"reflection.temporal_findings[{index}]"
        try:
            data = require_keys(row, required=_FINDING_REQUIRED, optional=("metric_refs",), where=where, error=WindowOutputError)
        except ContinuityContractError as exc:
            raise WindowOutputError(str(exc)) from exc
        data["claim"] = _claim(data["claim"], where=f"{where}.claim")
        data["relation_kind"] = _claim(data["relation_kind"], where=f"{where}.relation_kind")
        if not isinstance(data["episode_ids"], (list, tuple)):
            raise WindowOutputError(f"{where}.episode_ids must be a JSON array")
        data["support_refs"] = _refs(data["support_refs"], where=f"{where}.support_refs")
        data["counterevidence_refs"] = _refs(data["counterevidence_refs"], where=f"{where}.counterevidence_refs")
        require_enum(data["uncertainty"], allowed=UNCERTAINTY_LEVELS, where=f"{where}.uncertainty", error=WindowOutputError)
        out.append(data)
    return tuple(out)


def _checked_patterns(value: Any) -> tuple[dict[str, Any], ...]:
    rows = _bounded_rows(value, where="reflection.patterns", maximum=int(REFLECTION_LIMITS["patterns"]))
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        where = f"reflection.patterns[{index}]"
        try:
            data = require_keys(row, required=_PATTERN_REQUIRED, where=where, error=WindowOutputError)
        except ContinuityContractError as exc:
            raise WindowOutputError(str(exc)) from exc
        for name in ("claim", "applicability", "falsifier"):
            data[name] = _claim(data[name], where=f"{where}.{name}")
        data["support_refs"] = _refs(data["support_refs"], where=f"{where}.support_refs")
        data["counterevidence_refs"] = _refs(data["counterevidence_refs"], where=f"{where}.counterevidence_refs")
        data["metric_refs"] = _refs(data["metric_refs"], where=f"{where}.metric_refs")
        require_enum(data["evidence_grade"], allowed=EVIDENCE_GRADES, where=f"{where}.evidence_grade", error=WindowOutputError)
        out.append(data)
    return tuple(out)


def _checked_followthrough(value: Any) -> tuple[dict[str, Any], ...]:
    rows = _bounded_rows(
        value,
        where="reflection.learning_followthrough",
        maximum=int(REFLECTION_LIMITS["learning_followthrough"]),
    )
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        where = f"reflection.learning_followthrough[{index}]"
        try:
            data = require_keys(row, required=_FOLLOWTHROUGH_REQUIRED, where=where, error=WindowOutputError)
        except ContinuityContractError as exc:
            raise WindowOutputError(str(exc)) from exc
        require_text(data["intervention_ref"], where=f"{where}.intervention_ref", limit=256)
        for state in _FOLLOWTHROUGH_STATES:
            try:
                data[state] = Observation.coerce(data[state], where=f"{where}.{state}")
            except ContinuityContractError as exc:
                raise WindowOutputError(str(exc)) from exc
        data["support_refs"] = _refs(data["support_refs"], where=f"{where}.support_refs")
        out.append(data)
    return tuple(out)


def _checked_thread_updates(value: Any) -> tuple[dict[str, Any], ...]:
    rows = _bounded_rows(
        value, where="reflection.thread_updates", maximum=int(REFLECTION_LIMITS["thread_updates"])
    )
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        where = f"reflection.thread_updates[{index}]"
        if not isinstance(row, Mapping):
            raise WindowOutputError(f"{where} must be a JSON object")
        if "new_thread_request" in row:
            try:
                data = require_keys(row, required=_THREAD_NEW_REQUIRED, where=where, error=WindowOutputError)
            except ContinuityContractError as exc:
                raise WindowOutputError(str(exc)) from exc
            request = data["new_thread_request"]
            if not isinstance(request, Mapping) or not str(request.get("topic") or "").strip():
                raise WindowOutputError(f"{where}.new_thread_request must name a topic")
            _reject_host_keys(request, where=f"{where}.new_thread_request")
        else:
            try:
                data = require_keys(row, required=_THREAD_EXISTING_REQUIRED, where=where, error=WindowOutputError)
            except ContinuityContractError as exc:
                raise WindowOutputError(str(exc)) from exc
            try:
                data["thread_ref"] = CanonicalRef.coerce(data["thread_ref"], where=f"{where}.thread_ref")
            except ContinuityContractError as exc:
                raise WindowOutputError(
                    f"{where}.thread_ref must cite an existing canonical thread; a model does "
                    f"not mint thread identities ({exc})"
                ) from exc
        data["change"] = _claim(data["change"], where=f"{where}.change")
        data["proposed_state"] = _claim(data["proposed_state"], where=f"{where}.proposed_state")
        data["evidence_refs"] = _refs(data["evidence_refs"], where=f"{where}.evidence_refs")
        out.append(data)
    return tuple(out)


def _checked_proactive(value: Any) -> tuple[dict[str, Any], ...]:
    rows = _bounded_rows(
        value,
        where="reflection.proactive_candidates",
        maximum=int(REFLECTION_LIMITS["proactive_candidates"]),
    )
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        where = f"reflection.proactive_candidates[{index}]"
        try:
            data = require_keys(row, required=_PROACTIVE_REQUIRED, where=where, error=WindowOutputError)
        except ContinuityContractError as exc:
            raise WindowOutputError(
                f"{where}: a proactive candidate proposes content and conditions, never a "
                f"delivery ({exc})"
            ) from exc
        for name in ("what", "why_now", "articulation"):
            data[name] = _claim(data[name], where=f"{where}.{name}")
        for name in ("delivery_conditions", "ownership_refs", "dedup_refs"):
            if not isinstance(data[name], (list, tuple)):
                raise WindowOutputError(f"{where}.{name} must be a JSON array")
        out.append(data)
    return tuple(out)


#: The frozen row shapes for the optional shared cognitive delta, mirroring the live
#: instruction block in ``organ/promptorgan/packs/meta_omega/core.py::COGNITIVE_DELTA``.
#:
#: WO-02A C6: an outer array type was all the predecessor checked, so any row content passed.
#: These rows must MEET their contracts. Note the difference from the live
#: ``omega_loop/episode/types.py::bounded_*`` helpers, which are deliberately validate-and-drop
#: for the other lanes: here a malformed row is refused, because a window response that
#: silently lost one would be indistinguishable from one that never proposed it.
_DELTA_ROW_CONTRACTS: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "decisions": (("statement", "rationale"), ("supersedes_id",)),
    "user_constraints": (("statement",), ("source_episode_id",)),
    "lane_injections": (
        ("target_lane", "objective", "constraints", "evidence_refs", "requirement"),
        (),
    ),
}

INJECTION_REQUIREMENTS: tuple[str, ...] = ("required", "preferred", "optional")


def _checked_delta_rows(name: str, rows: Sequence[Any]) -> tuple[dict[str, Any], ...]:
    from schema.omega_control_decision import OMEGA_LANES

    required, optional = _DELTA_ROW_CONTRACTS[name]
    out: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        where = f"{name}[{index}]"
        try:
            data = require_keys(row, required=required, optional=optional, where=where, error=WindowOutputError)
        except ContinuityContractError as exc:
            raise WindowOutputError(str(exc)) from exc
        for field_name in ("statement", "rationale", "supersedes_id", "source_episode_id", "objective"):
            if field_name in data:
                data[field_name] = _claim(data[field_name], where=f"{where}.{field_name}")
        if name == "lane_injections":
            require_enum(
                data["target_lane"], allowed=OMEGA_LANES, where=f"{where}.target_lane", error=WindowOutputError
            )
            require_enum(
                data["requirement"],
                allowed=INJECTION_REQUIREMENTS,
                where=f"{where}.requirement",
                error=WindowOutputError,
            )
            for field_name in ("constraints", "evidence_refs"):
                if not isinstance(data[field_name], (list, tuple)):
                    raise WindowOutputError(f"{where}.{field_name} must be a JSON array")
                for item_index, item in enumerate(data[field_name]):
                    if not isinstance(item, str) or not item.strip():
                        raise WindowOutputError(
                            f"{where}.{field_name}[{item_index}] must be a nonempty string"
                        )
        out.append(data)
    return tuple(out)


def _checked_trajectory_assessment(value: Any) -> dict[str, Any]:
    where = "reflection.trajectory_assessment"
    try:
        data = require_keys(value, required=_TRAJECTORY_ASSESSMENT_REQUIRED, where=where, error=WindowOutputError)
    except ContinuityContractError as exc:
        raise WindowOutputError(
            f"{where} assesses the trajectory; it is not a second update channel ({exc})"
        ) from exc
    for name in ("assessment", "reason"):
        data[name] = _claim(data[name], where=f"{where}.{name}")
    data["evidence_refs"] = _refs(data["evidence_refs"], where=f"{where}.evidence_refs")
    return data


def _checked_trajectory_update(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    where = "trajectory_update"
    if not isinstance(value, Mapping):
        raise WindowOutputError(f"{where} must be a JSON object or null")
    operation = value.get("operation")
    if isinstance(operation, str) and operation.strip().lower() in _RETIRED_TRAJECTORY_WORDS:
        raise WindowOutputError(
            f"{where}.operation {operation!r} is a retired prompt word that no trajectory "
            f"applier supports; the authoritative operations are {list(TRAJECTORY_OPERATIONS)}"
        )
    _reject_host_keys(value, where=where)
    try:
        data = require_keys(
            value,
            required=_TRAJECTORY_UPDATE_REQUIRED,
            optional=_TRAJECTORY_UPDATE_OPTIONAL,
            where=where,
            error=WindowOutputError,
        )
    except ContinuityContractError as exc:
        raise WindowOutputError(str(exc)) from exc
    require_enum(data["operation"], allowed=TRAJECTORY_OPERATIONS, where=f"{where}.operation", error=WindowOutputError)
    data["reason"] = _claim(data["reason"], where=f"{where}.reason")
    domain = str(data.get("target_domain") or "").strip()
    capability = str(data.get("target_capability") or "").strip()
    if capability and not domain:
        raise WindowOutputError(
            f"{where}.target_capability was given with no target_domain; the trajectory store "
            "keys on the domain and could never record it"
        )
    if "confidence" in data:
        confidence = data["confidence"]
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise WindowOutputError(f"{where}.confidence must be a number, never a boolean")
        if not 0.0 <= float(confidence) <= 1.0:
            raise WindowOutputError(f"{where}.confidence {confidence} is outside [0.0, 1.0]")
    return data


def validated_reflection(reflection: Any, *, member_ids: Sequence[str]) -> dict[str, Any]:
    """THE structural validator for a reflection body. Non-constructing, so it cannot recurse.

    WO-02A closure F3. Every public path into a :class:`ValidatedWindowResponse` runs this:
    live admission, stored reload, and direct construction (including
    ``dataclasses.replace``). Before this existed the rules lived only inside
    ``validate_window_response_payload``, so ``replace(accepted, reflection=changed)`` produced
    an object carrying zero episode assessments beside a ten-member coverage declaration — the
    hardened loader would have refused the very same content.

    It validates and NORMALIZES: the returned mapping is what the record stores. Idempotent, so
    re-running it over an already-validated body yields the same body, which is what lets
    construction and admission both call it without a second code path.
    """
    members = tuple(str(item) for item in member_ids)
    if not members:
        raise WindowOutputError("a reflection needs the cohort it is validated against")
    try:
        body = require_keys(
            reflection, required=REFLECTION_FIELDS, where="reflection", error=WindowOutputError
        )
    except ContinuityContractError as exc:
        raise WindowOutputError(str(exc)) from exc

    _reject_host_keys(body, where="reflection")

    try:
        summary = require_text(
            body["summary"], where="reflection.summary", limit=int(REFLECTION_LIMITS["summary_chars"])
        )
    except ContinuityContractError as exc:
        raise WindowOutputError(str(exc)) from exc

    return {
        "summary": summary,
        "coverage": _checked_coverage(body["coverage"], member_ids=members),
        "episode_assessments": _checked_assessments(body["episode_assessments"], member_ids=members),
        "temporal_findings": _checked_findings(body["temporal_findings"]),
        "patterns": _checked_patterns(body["patterns"]),
        "learning_followthrough": _checked_followthrough(body["learning_followthrough"]),
        "thread_updates": _checked_thread_updates(body["thread_updates"]),
        "proactive_candidates": _checked_proactive(body["proactive_candidates"]),
        "limitations": tuple(
            _claim(item, where=f"reflection.limitations[{index}]")
            for index, item in enumerate(
                _bounded_rows(
                    body["limitations"],
                    where="reflection.limitations",
                    maximum=int(REFLECTION_LIMITS["limitations"]),
                )
            )
        ),
        "trajectory_assessment": _checked_trajectory_assessment(body["trajectory_assessment"]),
    }


def declared_member_ids(reflection: Any) -> tuple[str, ...]:
    """The cohort a reflection declares for ITSELF, for the paths that have no host manifest.

    This is internal consistency only. It is never evidence of what the host sealed — see
    :func:`validate_window_response_payload`, which compares it against the real manifest, and
    WO-13, which owns authoritative rebinding to the pinned request.
    """
    if not isinstance(reflection, Mapping):
        raise WindowOutputError("window_response.reflection must be a JSON object")
    coverage = reflection.get("coverage")
    if not isinstance(coverage, Mapping) or not isinstance(coverage.get("member_ids"), (list, tuple)):
        raise WindowOutputError(
            "a reflection must carry coverage.member_ids to be structurally validated"
        )
    declared = tuple(str(item) for item in coverage["member_ids"])
    if not declared:
        raise WindowOutputError("a reflection declares no coverage to validate against")
    return declared


@dataclass(frozen=True)
class ValidatedWindowResponse:
    """One accepted window response, bound to the request it answered.

    Construction is a validating boundary, not a container: ``__post_init__`` applies the same
    structural rules as admission and reload, through the shared non-constructing helpers. There
    is deliberately no unvalidated constructor and no bypass flag — a second way in would be a
    second contract with weaker semantics.
    """

    control: OmegaControlDecision
    reflection: Mapping[str, Any]
    trajectory_update: Mapping[str, Any] | None
    decisions: tuple[Mapping[str, Any], ...] = ()
    user_constraints: tuple[Mapping[str, Any], ...] = ()
    lane_injections: tuple[Mapping[str, Any], ...] = ()
    packet_digest: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.control, OmegaControlDecision):
            raise WindowOutputError("control must be a validated shared control decision")
        if not isinstance(self.packet_digest, str):
            raise WindowOutputError("packet_digest must be text")

        # Structural validity, on every public construction path. Nothing here calls
        # validate_window_response_payload, so construction cannot recurse into admission.
        reflection = unfreeze(self.reflection) if not isinstance(self.reflection, dict) else self.reflection
        validated = validated_reflection(reflection, member_ids=declared_member_ids(reflection))
        object.__setattr__(self, "reflection", freeze(validated))

        object.__setattr__(
            self,
            "trajectory_update",
            None if self.trajectory_update is None
            else freeze(_checked_trajectory_update(unfreeze(self.trajectory_update))),
        )
        for name in COGNITIVE_DELTA_KEYS:
            rows = getattr(self, name)
            if rows is None:
                object.__setattr__(self, name, ())
                continue
            if not isinstance(rows, (list, tuple)):
                raise WindowOutputError(f"{name} must be a JSON array when present")
            object.__setattr__(self, name, freeze(_checked_delta_rows(name, unfreeze(rows))))

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "control": self.control.to_dict(),
            "reflection": unfreeze(self.reflection),
            "trajectory_update": None if self.trajectory_update is None else unfreeze(self.trajectory_update),
            "packet_digest": self.packet_digest,
        }
        for name in COGNITIVE_DELTA_KEYS:
            out[name] = unfreeze(getattr(self, name))
        return out

    def content_sha256(self) -> str:
        return content_hash(self.to_dict())

    @classmethod
    def from_dict(cls, value: Any) -> "ValidatedWindowResponse":
        """Reload a stored response through the SAME structural validators as admission.

        WO-02A C6. The predecessor re-checked only two nested families, so a stored response
        whose ``episode_assessments`` had been emptied reloaded as an apparently-validated
        object despite its own ten-member coverage declaration.

        Two guarantees, kept separate on purpose:

        * **structural validity against the envelope's recorded coverage** — this method. The
          member list comes from the stored ``reflection.coverage.member_ids``, so the object
          is at least internally consistent and every field rule still applies.
        * **authoritative binding to the host's pinned request** — NOT this method. A
          self-declared coverage list is not proof of what the host actually sealed. WO-13
          must rebind a reloaded response to its trusted ``BudgetedWindowRequest``; a
          permissive deserializer may never stand in for that.
        """
        data = require_keys(
            value,
            required=("schema_version", "control", "reflection", "trajectory_update", "packet_digest")
            + COGNITIVE_DELTA_KEYS,
            where="window_response",
            error=WindowOutputError,
        )
        if data.pop("schema_version") != SCHEMA_VERSION:
            raise WindowOutputError("window_response.schema_version does not match this contract")

        reflection = data["reflection"]
        declared = declared_member_ids(reflection)

        # The whole payload, back through the admission validator. `packet_digest` is carried
        # across unchanged: it is the input receipt, not something to re-derive.
        #
        # EVERY delta key is passed through by PRESENCE (WO-02A closure F2). This loop used to
        # read `if rows:` before copying, so a stored `decisions=False`, `0`, `""` or `{}` was
        # OMITTED from the rebuilt payload and reload returned an empty array — while live
        # admission refused the identical value. Presence, declared absence and an explicitly
        # empty array are three different states, and a malformed present value is none of them.
        payload = {
            "control": data["control"].to_dict()
            if isinstance(data["control"], OmegaControlDecision)
            else data["control"],
            "reflection": dict(reflection) if isinstance(reflection, Mapping) else reflection,
            "trajectory_update": data["trajectory_update"],
            **{name: data[name] for name in COGNITIVE_DELTA_KEYS},
        }
        return validate_window_response_payload(
            payload, member_ids=declared, packet_digest=str(data["packet_digest"] or "")
        )


def validate_window_response_payload(
    payload: Any,
    *,
    member_ids: Sequence[str],
    packet_digest: str = "",
) -> ValidatedWindowResponse:
    """Validate one raw window response against the cohort the host actually sealed.

    ``member_ids`` is the host's manifest, not the model's claim: coverage is recomputed
    here, so a response cannot certify its own input completeness. The optional shared
    cognitive delta is admitted; any other top-level key is refused.
    """
    if not isinstance(payload, Mapping):
        raise WindowOutputError(f"a window response must be a JSON object, got {type(payload).__name__}")
    members = tuple(str(item) for item in member_ids)
    if not members:
        raise WindowOutputError("a window response needs the sealed cohort to validate against")

    unknown = sorted(set(payload) - set(WINDOW_RESPONSE_KEYS) - set(COGNITIVE_DELTA_KEYS))
    if unknown:
        raise WindowOutputError(
            f"a window response admits {list(WINDOW_RESPONSE_KEYS)} plus the optional "
            f"{list(COGNITIVE_DELTA_KEYS)}; it carries unknown key(s) {unknown}"
        )
    missing = sorted(set(WINDOW_RESPONSE_KEYS) - set(payload))
    if missing:
        raise WindowOutputError(f"a window response is missing required key(s) {missing}")

    try:
        control = OmegaControlDecision.from_dict(payload["control"])
    except ValueError as exc:
        raise WindowOutputError(f"control: {exc}") from exc

    # The HOST's cohort is the authority here: validate against `members`, not against what the
    # response declares about itself. This is the one check construction cannot perform, because
    # construction has no manifest — see `declared_member_ids`.
    validated = validated_reflection(payload["reflection"], member_ids=members)

    # Construction re-applies every structural rule through the same shared helpers, so the
    # remaining fields are handed over raw rather than validated twice by different code.
    return ValidatedWindowResponse(
        control=control,
        reflection=validated,
        trajectory_update=payload["trajectory_update"],
        packet_digest=packet_digest,
        **{name: payload.get(name) for name in COGNITIVE_DELTA_KEYS},
    )


__all__ = [
    "COGNITIVE_DELTA_KEYS",
    "REFLECTION_FIELDS",
    "REFLECTION_LIMITS",
    "SCHEMA_VERSION",
    "TRAJECTORY_OPERATIONS",
    "WINDOW_RESPONSE_KEYS",
    "ValidatedWindowResponse",
    "validate_window_response_payload",
]

"""Skill foldback — fold a qwen skill result through Meta B + Global B (Phase 6K).

Meta B classifies the QwenSkillExecutionResult into exactly one skill_* outcome; Global B
integrates whether the original intent was satisfied and what continuation (if any) is owed.
A ledger enforces the invariant: every dispatch produces exactly one Meta B outcome — a
duplicate fails, a missing one fails, nothing is silently dropped. Additive + flag-gated;
the live USE_TOOL/APPLY Meta B/Global B path is untouched.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

from schema.global_b_skill_update import GlobalBSkillUpdate
from schema.meta_b_skill_outcome import (
    FAILURE_KINDS,
    MetaBSkillOutcome,
    STATUS_TO_META_B_KIND,
)


class SkillFoldbackError(RuntimeError):
    """Raised on a duplicate / missing / un-recordable Meta B skill outcome."""


_NORMALIZED_FACT_FIELDS = frozenset(
    {
        "diff_summary",
        "files_changed",
        "commands_run",
        "tests_run",
        "tool_calls",
        "artifacts",
    }
)
_OBSERVATION_TOOLS = frozenset(
    {
        "read_file",
        "read_files",
        "search_repo",
        "grep_search",
        "web_fetch",
        "search_web",
        "fetch_url",
        "browser_fetch",
        "browser_search",
        "inspect_file",
    }
)
_PLAN_ARTIFACT_KINDS = frozenset(
    {"plan", "design", "architecture", "roadmap", "implementation_plan"}
)
_FINDINGS_ARTIFACT_KINDS = frozenset(
    {"findings", "research", "research_report", "audit", "review", "report"}
)
_COMMAND_TOOLS = frozenset(
    {
        "execute_command",
        "run_command",
        "run_tests",
        "run_shell",
        "bash",
        "shell",
        "terminal",
    }
)
_TEST_COMMAND_MARKERS = (
    "pytest",
    "py.test",
    "unittest",
    "npm test",
    "npm run test",
    "yarn test",
    "pnpm test",
    "jest",
    "vitest",
    "mocha",
    "go test",
    "cargo test",
    "rspec",
    "phpunit",
    "dotnet test",
    "gradle test",
    "mvn test",
    "ctest",
    "tox",
)


def _has_payload(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_has_payload(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_has_payload(item) for item in value)
    return bool(str(value or "").strip())


def _has_error_payload(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(
        key in value and _has_payload(value.get(key)) for key in ("error", "errors", "exception")
    )


def _trusted_observation_fact(result: Mapping[str, Any]) -> bool:
    for call in result.get("tool_calls") or ():
        if not isinstance(call, Mapping):
            continue
        tool_name = str(call.get("tool_name") or "").strip().lower()
        tool_name = tool_name.removeprefix("cli.")
        if tool_name not in _OBSERVATION_TOOLS:
            continue
        status = str(call.get("status") or "").strip().lower()
        positive_status = status in {
            "success",
            "passed",
            "ok",
            "completed",
        }
        unknown_but_returned = (
            status in {"", "unknown"}
            and not _has_payload(call.get("stderr"))
            and not _has_error_payload(call.get("result"))
        )
        if not positive_status and not unknown_but_returned:
            continue
        if not str(call.get("tool_call_id") or "").strip():
            continue
        if _has_payload(call.get("stdout")) or _has_payload(call.get("result")):
            return True
    return False


def _trusted_artifact_fact(
    result: Mapping[str, Any],
    *,
    allowed_kinds: frozenset[str],
) -> bool:
    for artifact in result.get("artifacts") or ():
        if not isinstance(artifact, Mapping):
            continue
        kind = str(artifact.get("kind") or artifact.get("type") or "").strip().lower()
        if kind not in allowed_kinds:
            continue
        has_locator = bool(str(artifact.get("path") or artifact.get("id") or "").strip())
        has_digest = bool(
            str(
                artifact.get("sha256") or artifact.get("digest") or artifact.get("source_id") or ""
            ).strip()
        )
        if has_locator and has_digest:
            return True
    return False


def _trusted_test_fact(result: Mapping[str, Any]) -> bool:
    for call in result.get("tool_calls") or ():
        if not isinstance(call, Mapping):
            continue
        tool_name = str(call.get("tool_name") or "").strip().lower()
        tool_name = tool_name.removeprefix("cli.")
        if tool_name not in _COMMAND_TOOLS:
            continue
        args = call.get("args")
        command = str(args.get("command") if isinstance(args, Mapping) else "").strip()
        if not command or not any(marker in command.lower() for marker in _TEST_COMMAND_MARKERS):
            continue
        if not str(call.get("tool_call_id") or "").strip():
            continue
        if str(call.get("status") or "").strip().lower() not in {
            "success",
            "passed",
            "ok",
            "completed",
        }:
            continue
        call_result = call.get("result")
        exit_code = (
            call_result.get("exit_code", call_result.get("returncode"))
            if isinstance(call_result, Mapping)
            else None
        )
        if exit_code not in (None, 0, "0"):
            continue
        return True
    return False


def _trusted_summary_fact(result: Mapping[str, Any]) -> bool:
    return _trusted_observation_fact(result) or _trusted_artifact_fact(
        result,
        allowed_kinds=_PLAN_ARTIFACT_KINDS | _FINDINGS_ARTIFACT_KINDS,
    )


def _normalized_words(value: Any) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9]+", str(value or "").lower()))


def _trusted_artifact_requirement(result: Mapping[str, Any], requirement: str) -> bool:
    required = _normalized_words(requirement)
    if not required:
        return False
    for artifact in result.get("artifacts") or ():
        if not isinstance(artifact, Mapping):
            continue
        kind = artifact.get("kind") or artifact.get("type")
        if not required.issubset(_normalized_words(kind)):
            continue
        if _trusted_artifact_fact(result, allowed_kinds=frozenset({str(kind).lower()})):
            return True
    return False


def _trusted_source_fact(result: Mapping[str, Any]) -> bool:
    if any(str(value or "").strip() for value in result.get("sources_consulted") or ()):
        return True
    return any(
        isinstance(artifact, Mapping)
        and str(artifact.get("source_id") or "").strip()
        and str(artifact.get("path") or artifact.get("id") or "").strip()
        for artifact in result.get("artifacts") or ()
    )


def _trusted_verification_requirement(result: Mapping[str, Any], requirement: str) -> bool:
    wanted = " ".join(re.findall(r"[a-z0-9]+", requirement.lower()))
    for row in result.get("verification_results") or ():
        if not isinstance(row, Mapping):
            continue
        requested = " ".join(re.findall(r"[a-z0-9]+", str(row.get("requested") or "").lower()))
        if requested != wanted or str(row.get("status") or "").lower() != "passed":
            continue
        provenance = row.get("executor_provenance")
        if not isinstance(provenance, Mapping):
            continue
        if str(provenance.get("source") or "") not in {
            "qwen_tool_call",
            "direct_tool",
            "bridge_ledger",
            "workspace_diff",
            "independent_workspace_diff",
        }:
            continue
        if any(
            str(provenance.get(key) or "").strip()
            for key in ("tool_call_id", "result_digest", "ledger_digest", "diff_digest")
        ):
            return True
    return False


_NORMALIZED_EVIDENCE_ALIASES = {
    "summary": _trusted_summary_fact,
    "findings": lambda result: (
        _trusted_observation_fact(result)
        or _trusted_artifact_fact(result, allowed_kinds=_FINDINGS_ARTIFACT_KINDS)
    ),
    "plan": lambda result: _trusted_artifact_fact(result, allowed_kinds=_PLAN_ARTIFACT_KINDS),
}
_NORMALIZED_FACT_ALIASES = {
    "file_changed": lambda result: bool(result.get("files_changed")),
    "files_changed": lambda result: bool(result.get("files_changed")),
    "pytest": _trusted_test_fact,
    "test_run": _trusted_test_fact,
    "tests_run": _trusted_test_fact,
    "test_results": _trusted_test_fact,
    "tests_pass": _trusted_test_fact,
    "tests_passed": _trusted_test_fact,
}


def _required_evidence_present(result: Mapping[str, Any], key: Any) -> bool:
    """Check normalized executor facts; never trust model-authored evidence maps.

    The legacy direct helper shape remains accepted for non-normalized callers, but
    the live Qwen result contract is recognized by its type/schema and can satisfy
    a dispatch only through fields derived from tool calls, the bridge ledger, or
    the independently observed workspace diff.
    """
    name = str(key or "").strip()
    normalized_name = "_".join(re.findall(r"[a-z0-9]+", name.lower()))
    normalized = str(result.get("type") or "") == "qwen_skill_execution_result" or str(
        result.get("schema_version") or ""
    ).startswith("omega.qwen_skill_execution_result.")
    if normalized:
        fact_alias = _NORMALIZED_FACT_ALIASES.get(normalized_name)
        if fact_alias is not None:
            return bool(fact_alias(result))
        if normalized_name in _NORMALIZED_FACT_FIELDS:
            return bool(result.get(normalized_name))
        governed_alias = _NORMALIZED_EVIDENCE_ALIASES.get(normalized_name)
        if governed_alias is not None:
            return bool(governed_alias(result))
        if normalized_name in {"source", "source_id", "source_proof", "url"}:
            return _trusted_source_fact(result)
        if _trusted_artifact_requirement(result, name):
            return True
        return _trusted_verification_requirement(result, name)
    provided = result.get("evidence")
    return isinstance(provided, dict) and bool(provided.get(name))


# ── Meta B classification ─────────────────────────────────────────────────────
def classify_meta_b_skill_outcome(
    result: Mapping[str, Any], *, expected_partial_ok: bool = False, evidence_required: tuple = ()
) -> MetaBSkillOutcome:
    """Map a QwenSkillExecutionResult (dict form, 6J) -> exactly one MetaBSkillOutcome.
    Total + deterministic: an unknown status fails closed to skill_malformed_result."""
    status = str(result.get("status") or "")
    kind = STATUS_TO_META_B_KIND.get(status, "skill_malformed_result")
    failure_class = result.get("failure_class")
    summary = str(result.get("summary") or "")
    blockers = tuple(result.get("unresolved_blockers") or ())

    satisfied = kind == "skill_success"
    safety_blocker: Optional[str] = None
    requires_operator_input = False

    if kind == "skill_partial":
        satisfied = bool(expected_partial_ok)
    elif kind == "skill_blocked":
        # blocked records the safety/blocker (from guard_status or summary)
        gs = result.get("guard_status") or {}
        safety_blocker = str(gs.get("reason") or summary or "blocked")
    elif kind == "skill_requires_confirmation":
        requires_operator_input = True
    elif kind in FAILURE_KINDS:
        # failure -> failure_class required; default to the kind's name when unset
        failure_class = failure_class or kind.removeprefix("skill_")

    # HONEST SIGNALS (#19): a 'success' is only a VERIFIED success if the dispatch's required
    # evidence is actually present — qwen's self-reported status alone does not substantiate it.
    # Missing evidence keeps outcome_kind=skill_success (it ran) but records an evidence_unsatisfied
    # BLOCKER, so Global B owes verification and will NOT satisfy the intent / close the episode
    # (same path as any other unresolved blocker on a success).
    if kind == "skill_success" and evidence_required:
        missing = [k for k in evidence_required if not _required_evidence_present(result, k)]
        if missing:
            blockers = blockers + (f"evidence_unsatisfied:{','.join(missing)}",)

    return MetaBSkillOutcome(
        episode_id=str(result.get("episode_id") or ""),
        dispatch_id=str(result.get("dispatch_id") or ""),
        skill_name=str(result.get("skill_name") or ""),
        skill_id=str(result.get("skill_id") or ""),
        execution_kind=str(result.get("execution_kind") or ""),
        outcome_kind=kind,
        satisfied_dispatch=satisfied,
        failure_class=failure_class,
        safety_blocker=safety_blocker,
        requires_operator_input=requires_operator_input,
        unresolved_blockers=blockers,
        summary=summary,
        evidence=dict(result.get("evidence") or {}),
        guard_status=dict(result.get("guard_status") or {}),
        confidence=float(result.get("confidence") or 0.0),
    )


# ── Global B integration ──────────────────────────────────────────────────────
# Agentic-work progression: a successful step satisfies the OBJECTIVE only when it is the
# terminal verification — investigate -> implement -> verify -> done. A successful investigation
# still owes an implementation; a successful implementation still owes a verification. (The spec's
# 6O story: inspect -> "more work needed" -> patch -> "verification needed" -> test -> satisfied.)
_VERIFICATION_KINDS = frozenset({"TEST", "VERIFY"})
_IMPLEMENTATION_KINDS = frozenset(
    {"PATCH", "REPAIR", "WRITE", "REFACTOR", "MAINTAIN", "RUN", "APPLY"}
)


def _continuation_pressure(reason: str, objective: str, *, kind: str = "execution_repair") -> dict:
    """A RECORDED continuation-pressure intent (Global B does not enqueue/execute it)."""
    return {
        "kind": kind,
        "reason": reason,
        "objective": objective,
        "source": "global_b_skill_foldback",
    }


def integrate_global_b_skill_outcome(
    mbo: MetaBSkillOutcome, *, objective: str, result: Mapping[str, Any]
) -> GlobalBSkillUpdate:
    """Decide intent satisfaction + continuation from a Meta B skill outcome."""
    kind = mbo.outcome_kind
    blockers = tuple(result.get("unresolved_blockers") or mbo.unresolved_blockers or ())
    fc = (mbo.failure_class or "").lower()

    intent_satisfied = False
    objective_complete = False
    more_work_needed = True
    close_episode = False
    ask_operator = False
    pressure: Optional[dict] = None
    signals: list[str] = []

    if kind == "skill_success":
        if blockers:
            signals = ["verification_needed"]
            pressure = _continuation_pressure("unresolved_blockers", objective)
        else:
            # SUCCESS = STOP. The agentic executor does the WHOLE task in one pass — it
            # implements AND self-verifies — so a clean success (no unresolved blockers,
            # required evidence present) SATISFIES the objective and closes the cycle.
            # The legacy investigate->implement->verify continuation chain re-ran a
            # finished, verified build and hammered the runtime into a crash (operator
            # finding 2026-06-25: build #2 succeeded, #3 re-fired and the model endpoint
            # died). Evidence is still enforced — missing required evidence is recorded as
            # a blocker above, which takes the verification_needed branch instead.
            intent_satisfied = True
            objective_complete = True
            more_work_needed = False
            close_episode = True
    elif kind == "skill_partial":
        signals = ["partial_completion"]
        pressure = _continuation_pressure("partial_completion", objective)
    elif kind == "skill_yielded":
        # FOLD-BACK CHECKPOINT. The build hit its round/watchdog budget and stopped at a clean
        # boundary with progress intact. This is NEITHER success (objective unfinished) NOR a
        # failure (no error) — it owes MORE WORK, but the next move is the MIND's: Omega's episode
        # reasons over the build_in_progress context (recalled digest) and either raises pressure to
        # resume or idles. So: keep the episode OPEN, record (documentary only) a continuation
        # intent, and DO NOT close or auto-fire. (intent_satisfied / objective_complete stay False,
        # more_work_needed stays True, close_episode stays False — the defaults.)
        signals = ["build_in_progress"]
        pressure = _continuation_pressure(
            "build_in_progress", objective, kind="execution_continuation"
        )
    elif kind == "skill_requires_confirmation":
        # pending operator — neutral, await input; no repair pressure
        signals = ["requires_operator_input"]
        ask_operator = True
    elif kind == "skill_blocked":
        # safety block — operator decides; recorded as safety, not a repair
        signals = ["blocked_by_safety"]
        ask_operator = True
    elif kind == "skill_invalid_dispatch":
        signals = ["skill_gap", "missing_context"]
        pressure = _continuation_pressure("invalid_dispatch", objective)
    elif kind in ("skill_malformed_result", "skill_bridge_unavailable"):
        # bridge/protocol failure — repair the bridge path, context may be missing
        signals = ["repair_needed", "missing_context"]
        pressure = _continuation_pressure(kind.removeprefix("skill_"), objective)
    else:  # skill_failure, skill_timeout
        signals = ["repair_needed"]
        if "test" in fc:
            signals.append("test_failed")
        if "forbidden_tool" in fc:
            signals.append("skill_gap")
        pressure = _continuation_pressure(
            mbo.failure_class or kind.removeprefix("skill_"), objective
        )

    return GlobalBSkillUpdate(
        episode_id=mbo.episode_id,
        dispatch_id=mbo.dispatch_id,
        intent_satisfied=intent_satisfied,
        objective_complete=objective_complete,
        more_work_needed=more_work_needed,
        unresolved_blockers=blockers,
        continuation_signals=tuple(signals),
        continuation_pressure=pressure,
        close_episode=close_episode,
        ask_operator=ask_operator,
        summary=mbo.summary,
    )


# ── one-outcome-per-dispatch ledger ──────────────────────────────────────────
class MetaBSkillLedger:
    """Enforce exactly one Meta B outcome per dispatch — duplicate fails, missing fails."""

    def __init__(self) -> None:
        self._by_dispatch: dict[str, MetaBSkillOutcome] = {}

    def record(self, outcome: MetaBSkillOutcome) -> MetaBSkillOutcome:
        did = outcome.dispatch_id
        if not did:
            raise SkillFoldbackError("cannot record a Meta B outcome with no dispatch_id")
        if did in self._by_dispatch:
            raise SkillFoldbackError(f"duplicate Meta B outcome for dispatch {did}")
        self._by_dispatch[did] = outcome
        return outcome

    def has(self, dispatch_id: str) -> bool:
        return dispatch_id in self._by_dispatch

    def require(self, dispatch_id: str) -> MetaBSkillOutcome:
        if dispatch_id not in self._by_dispatch:
            raise SkillFoldbackError(f"missing Meta B outcome for dispatch {dispatch_id}")
        return self._by_dispatch[dispatch_id]


# ── orchestrator ──────────────────────────────────────────────────────────────
def fold_skill_result(
    result: Mapping[str, Any],
    *,
    objective: str,
    expected_partial_ok: bool = False,
    evidence_required: tuple = (),
    ledger: Optional[MetaBSkillLedger] = None,
) -> dict:
    """Classify (Meta B) then integrate (Global B). ALWAYS produces both — no silent drop.
    With a ledger, enforces one-outcome-per-dispatch. evidence_required gates 'success'
    satisfaction against the dispatch's declared evidence (honest signals)."""
    mbo = classify_meta_b_skill_outcome(
        result, expected_partial_ok=expected_partial_ok, evidence_required=evidence_required
    )
    errs = mbo.validate()
    if errs:
        raise SkillFoldbackError("invalid Meta B skill outcome: " + "; ".join(errs))
    if ledger is not None:
        ledger.record(mbo)
    gbu = integrate_global_b_skill_outcome(mbo, objective=objective, result=result)
    gerrs = gbu.validate()
    if gerrs:
        raise SkillFoldbackError("invalid Global B skill update: " + "; ".join(gerrs))
    return {"meta_b": mbo.to_dict(), "global_b": gbu.to_dict()}


def attach_skill_foldback(episode, folded: Mapping[str, Any]) -> None:
    """Write the skill foldback onto the episode ALONGSIDE the live reflection/outcome —
    episode.meta['skill_reflection'] + episode.global_['skill_outcome']."""
    meta = dict(getattr(episode, "meta", None) or {})
    meta["skill_reflection"] = dict(folded["meta_b"])
    episode.meta = meta
    glob = dict(getattr(episode, "global_", None) or {})
    glob["skill_outcome"] = dict(folded["global_b"])
    episode.global_ = glob

"""QwenSkillExecutionResult — the normalized result of a skill-mediated qwen run (6J).

Auto's qwen invoker returns a raw bridge dict; this normalizes it into a single contract
Meta B + Global B reason over. Fail-closed: a non-dict / unrecognizable bridge result
becomes malformed_result; a missing dispatch id becomes invalid_dispatch; a missing bridge
'done' (timeout / connection) becomes timeout / bridge_unavailable. qwen cannot smuggle a
new top-level intent or a continuation request through the result — those keys are ignored.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

QWEN_SKILL_RESULT_SCHEMA_VERSION = "omega.qwen_skill_execution_result.v1"

ALLOWED_STATUS = frozenset(
    {
        "success",
        "partial",
        "yielded",  # budget/watchdog checkpoint — progress intact, more work owed (NOT a failure)
        "failure",
        "blocked",
        "requires_confirmation",
        "timeout",
        "bridge_unavailable",
        "invalid_dispatch",
        "malformed_result",
    }
)

# The execution limb reports facts; it never owns Omega's goal/program contract. Strip
# attempted authority fields even from the retained diagnostic payload so downstream code
# cannot accidentally treat bridge-authored intent or planning data as canonical.
_BRIDGE_GOAL_SETTING_KEYS = frozenset(
    {
        "intent",
        "pressure",
        "pathway",
        "outcome",
        "acceptance_criteria",
        "constraints",
        "priorities",
        "objective",
        "outcome_contract",
        "execution_plan",
        "evidence_contract",
        "motor_program",
        "program_id",
        "parent_program_id",
        "outcome_id",
        "episode_id",
        "attempt_number",
        "created_at",
        "pathway_id",
        "revision",
        "name",
        "method",
        "operational_rationale",
        "failed_predecessor_id",
        "change_evidence",
        "capability_class",
        "operations",
        "permitted_local_discretion",
        "prohibited_deviations",
        "return_to_mind_conditions",
        "required_artifacts",
        "required_source_proof",
        "required_checks",
        "completion_signals",
        "disqualifying_failures",
        "executor_identity",
        "planning_authority",
        "replacement_authority",
        "replacement_objective",
        "replacement_motor_program",
    }
)
_RATE_LIMIT_MARKERS = (
    "429",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "throttl",
    "too many requests",
)


def normalize_executor_failure_kind(kind: Any, *details: Any) -> str:
    """Canonicalize provider bursts without reclassifying genuine failures."""
    text = " ".join(str(value or "") for value in (kind, *details)).lower()
    if any(marker in text for marker in _RATE_LIMIT_MARKERS):
        return "rate_limited"
    return str(kind or "")


def _diagnostic_bridge_result(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {"raw": repr(raw)}
    return {key: value for key, value in raw.items() if key not in _BRIDGE_GOAL_SETTING_KEYS}


@dataclass(frozen=True)
class QwenSkillExecutionResult:
    episode_id: str
    dispatch_id: str
    skill_name: str
    skill_id: str
    execution_kind: str
    status: str
    summary: str
    tool_calls: tuple = ()
    files_changed: tuple = ()
    commands_run: tuple = ()
    tests_run: tuple = ()
    diff_summary: dict = field(default_factory=dict)
    artifacts: tuple = ()
    evidence: dict = field(default_factory=dict)
    unresolved_blockers: tuple = ()
    failure_class: str | None = None
    confidence: float = 0.0
    guard_status: dict = field(default_factory=dict)
    duration_ms: int = 0
    # Fold-back build loop: a `yielded` result carries the brain-derived progress digest
    # (the context-compactor that re-grounds qwen on resume) and how many rounds it used.
    progress_digest: str = ""
    rounds_used: int = 0
    raw_bridge_result: dict = field(default_factory=dict)
    execution_route: str = "development"
    type: str = "qwen_skill_execution_result"
    schema_version: str = QWEN_SKILL_RESULT_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "episode_id": self.episode_id,
            "dispatch_id": self.dispatch_id,
            "skill_name": self.skill_name,
            "skill_id": self.skill_id,
            "execution_kind": self.execution_kind,
            "execution_route": self.execution_route,
            "status": self.status,
            "summary": self.summary,
            "tool_calls": [dict(t) for t in self.tool_calls],
            "files_changed": list(self.files_changed),
            "commands_run": list(self.commands_run),
            "tests_run": list(self.tests_run),
            "diff_summary": dict(self.diff_summary),
            "artifacts": list(self.artifacts),
            "evidence": dict(self.evidence),
            "unresolved_blockers": list(self.unresolved_blockers),
            "failure_class": self.failure_class,
            "confidence": self.confidence,
            "guard_status": dict(self.guard_status),
            "duration_ms": self.duration_ms,
            "progress_digest": self.progress_digest,
            "rounds_used": self.rounds_used,
            "raw_bridge_result": dict(self.raw_bridge_result),
            "schema_version": self.schema_version,
        }


def _derive_status(raw: dict) -> str:
    declared = str(raw.get("status") or "")
    if declared in ALLOWED_STATUS:
        return declared
    error = str(raw.get("error") or "").lower()
    if error:
        if "timeout" in error or "timed out" in error:
            return "timeout"
        if any(w in error for w in ("unavailable", "refused", "bridge", "connect", "no done")):
            return "bridge_unavailable"
        return "failure"
    if "ok" in raw:
        return "success" if bool(raw.get("ok")) else "failure"
    return "malformed_result"  # no recognizable success/error shape


def _norm_tool_call(tc: Any, idx: int) -> dict:
    tc = tc if isinstance(tc, dict) else {"tool_name": str(tc)}
    return {
        "tool_call_id": str(tc.get("tool_call_id") or f"tc_{idx}"),
        "tool_name": str(tc.get("tool_name") or tc.get("name") or ""),
        "args": dict(tc.get("args")) if isinstance(tc.get("args"), dict) else {},
        "status": str(tc.get("status") or "unknown"),
        "stdout": str(tc.get("stdout") or ""),
        "stderr": str(tc.get("stderr") or ""),
        "result": dict(tc.get("result") or {})
        if isinstance(tc.get("result"), dict)
        else {"value": tc.get("result")},
        "duration_ms": int(tc.get("duration_ms") or 0),
    }


_DIFF_FILE = re.compile(r"^\+\+\+ b/(.+)$", re.MULTILINE)


def _files_from_diff(raw: dict) -> list[str]:
    diff = str(raw.get("git_diff") or "")
    return _DIFF_FILE.findall(diff)


def _tool_calls_from_raw(raw: dict) -> list[Any]:
    """Extract tool call records from bridge output. Supports explicit tool_calls lists and
    dict-shaped ledgers (live bridge) where touched_files imply write/patch tool use."""
    tcs = raw.get("tool_calls")
    if isinstance(tcs, list):
        return tcs
    led = raw.get("ledger")
    if isinstance(led, list):
        return led
    if not isinstance(led, dict):
        return []
    out: list[Any] = []
    for tc in led.get("tool_calls") or []:
        if isinstance(tc, dict):
            out.append(tc)
    for f in led.get("touched_files") or []:
        if isinstance(f, dict) and f.get("path"):
            out.append(
                {
                    "tool_name": str(f.get("tool") or f.get("tool_name") or "patch_file"),
                    "args": {"path": f["path"]},
                    "status": str(f.get("status") or "success"),
                }
            )
        elif isinstance(f, str):
            out.append({"tool_name": "patch_file", "args": {"path": f}, "status": "success"})
    return out


def _files_from_ledger(raw: dict) -> list[str]:
    """The qwen bridge reports file changes via its keep/rollback ledger (touched_files), not a
    raw git_diff — read those paths so live skill writes surface as files_changed."""
    led = raw.get("ledger")
    if not isinstance(led, dict):
        return []
    out: list[str] = []
    seen: set[str] = set()
    # DEDUPE by path: the bridge records one touched_files entry per write/edit/patch CALL, so a
    # file edited N times appears N times. max_files_changed bounds distinct FILES, not operations,
    # so a 1-file build that re-edits must count as 1 (operator live finding 2026-06-28).
    for f in led.get("touched_files") or []:
        path = ""
        if isinstance(f, dict) and f.get("path"):
            path = str(f["path"])
        elif isinstance(f, str):
            path = f
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _artifacts_from_ledger(raw: dict) -> list[Any]:
    """Return only bridge-ledger artifact facts, never model top-level claims."""
    led = raw.get("ledger")
    if not isinstance(led, dict):
        return []
    artifacts = led.get("artifacts")
    if not isinstance(artifacts, list):
        return []
    return [item for item in artifacts if isinstance(item, (str, dict))]


# Tool names the executor uses to RUN shell commands. A tool_call to one of these IS evidence the
# command ran — the same trust files_changed places in write_file calls.
_COMMAND_TOOLS = frozenset(
    {
        "execute_command",
        "run_command",
        "run_tests",
        "run_shell",
        "bash",
        "shell",
        "terminal",
        "cli.execute_command",
        "cli.run_command",
    }
)

# Substrings that identify a TEST-runner invocation in a command string. Specific on purpose — a
# bare "test" would false-match "latest"/"fastest"; each marker names a real test runner/verb.
_TEST_COMMAND_MARKERS = (
    "pytest",
    "py.test",
    "-m pytest",
    "-m unittest",
    "unittest discover",
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


def _command_of(tc: Any) -> str:
    args = tc.get("args") if isinstance(tc, dict) else None
    if not isinstance(args, dict):
        return ""
    return str(args.get("command") or args.get("cmd") or "").strip()


def _commands_from_tool_calls(tool_calls: Iterable[Any]) -> list[str]:
    """Every shell command the executor actually ran, first-seen order, deduped."""
    out: list[str] = []
    seen: set[str] = set()
    for tc in tool_calls or ():
        if not isinstance(tc, dict) or str(tc.get("tool_name") or "") not in _COMMAND_TOOLS:
            continue
        cmd = _command_of(tc)
        if cmd and cmd not in seen:
            seen.add(cmd)
            out.append(cmd)
    return out


def _tests_from_tool_calls(tool_calls: Iterable[Any]) -> list[str]:
    """The executed commands that invoked a test runner — the tests_run evidence the evidence gate
    checks. Derived from the commands actually run (a run_command 'pytest ...' means the bridge ran
    pytest), so a TEST/APPLY skill reports its tests even when the bridge sends no explicit
    tests_run field (operator 2026-07-01: tests_run never propagated -> a real build that ran pytest
    was failed as skill_no_evidence)."""
    out: list[str] = []
    seen: set[str] = set()
    for cmd in _commands_from_tool_calls(tool_calls):
        low = cmd.lower()
        if any(m in low for m in _TEST_COMMAND_MARKERS) and cmd not in seen:
            seen.add(cmd)
            out.append(cmd)
    return out


def normalize_qwen_result(
    raw: Any, *, dispatch, guard_status: dict | None = None
) -> QwenSkillExecutionResult:
    base = dict(
        episode_id=dispatch.episode_id,
        dispatch_id=dispatch.dispatch_id,
        skill_name=dispatch.skill_name,
        skill_id=dispatch.skill_id,
        execution_kind=dispatch.execution_kind,
        execution_route=str(getattr(dispatch, "execution_route", "development")),
        guard_status=dict(guard_status or {}),
        raw_bridge_result=_diagnostic_bridge_result(raw),
    )

    if not dispatch.episode_id or not dispatch.dispatch_id:
        return QwenSkillExecutionResult(
            status="invalid_dispatch",
            summary="missing episode_id/dispatch_id",
            failure_class="invalid_dispatch",
            **base,
        )
    if not isinstance(raw, dict):
        return QwenSkillExecutionResult(
            status="malformed_result",
            summary="bridge result is not a mapping",
            failure_class="malformed_result",
            **base,
        )

    status = _derive_status(raw)
    tool_calls = tuple(_norm_tool_call(tc, i) for i, tc in enumerate(_tool_calls_from_raw(raw)))
    # Execution facts come only from the bridge's independently observed ledger/diff.
    # Top-level files_changed/diff_summary are executor prose claims and are diagnostic
    # only; admitting them here would let a model assert work that never happened.
    files_changed = tuple(dict.fromkeys(_files_from_ledger(raw) or _files_from_diff(raw)))
    if raw.get("git_diff"):
        diff_summary = {
            "source": "workspace_diff",
            "git_diff_present": True,
            "git_diff": str(raw.get("git_diff"))[:4000],
            "files_changed": len(files_changed),
            "paths": list(files_changed),
        }
    elif files_changed:
        diff_summary = {
            "source": "bridge_ledger",
            "files_changed": len(files_changed),
            "paths": list(files_changed),
        }
    else:
        diff_summary = {}

    failure_class = raw.get("failure_class")
    # forbidden tool use -> downgrade to failure (qwen escaped its allowed set)
    forbidden = set(dispatch.forbidden_tools)
    used_forbidden = sorted({tc["tool_name"] for tc in tool_calls if tc["tool_name"] in forbidden})
    if used_forbidden:
        # A forbidden-tool ESCAPE is a violation regardless of how the run ended — a yielded
        # checkpoint that broke its allowed-tool envelope is a failure, not clean progress.
        status = "failure" if status in ("success", "partial", "yielded") else status
        failure_class = failure_class or f"forbidden_tool_use:{','.join(used_forbidden)}"
    if (
        status in ("failure", "timeout", "bridge_unavailable", "malformed_result")
        and not failure_class
    ):
        failure_class = status
    if status in ("failure", "timeout", "bridge_unavailable"):
        failure_class = normalize_executor_failure_kind(
            failure_class,
            raw.get("error"),
            raw.get("tool_error"),
        )

    return QwenSkillExecutionResult(
        status=status,
        summary=str(raw.get("summary") or ""),
        tool_calls=tool_calls,
        files_changed=files_changed,
        # commands_run / tests_run: the bridge doesn't emit these fields, so derive them from the
        # command-runner tool calls it DOES emit (same pattern as files_changed <- write calls).
        # Without this, tests_run was always empty and a TEST-kind build that ran pytest failed the
        # evidence gate as skill_no_evidence (operator live finding 2026-07-01).
        commands_run=tuple(dict.fromkeys(_commands_from_tool_calls(tool_calls))),
        tests_run=tuple(dict.fromkeys(_tests_from_tool_calls(tool_calls))),
        diff_summary=diff_summary,
        artifacts=tuple(_artifacts_from_ledger(raw)),
        evidence={},
        unresolved_blockers=tuple(raw.get("unresolved_blockers") or ()),
        failure_class=failure_class,
        confidence=float(raw.get("confidence") or 0.0),
        duration_ms=int(raw.get("duration_ms") or 0),
        progress_digest=str(raw.get("progress_digest") or ""),
        rounds_used=int(raw.get("rounds_used") or 0),
        **base,
    )

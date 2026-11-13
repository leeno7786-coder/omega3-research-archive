"""Adapters that let AUTO execute an Omega-authored MotorProgram without owning it."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import Any

from schema.execution_evidence import ExecutionEvidence
from schema.motor_program import MotorProgram
from schema.qwen_skill_result import normalize_executor_failure_kind


_MOTOR_TRANSPORTS = frozenset({"USE_TOOL", "USE_SKILL"})
_EVIDENCE_STATUSES = {
    "success": "success",
    "partial": "partial",
    "yielded": "yielded",
    "blocked": "blocked",
    "requires_confirmation": "blocked",
    "failure": "failure",
    "timeout": "failure",
    "bridge_unavailable": "failure",
    "invalid_dispatch": "failure",
    "malformed_result": "failure",
    "failed": "failure",
}


def effective_motor_action(execution_pack: Mapping[str, Any]) -> str:
    """Resolve a motor transport without mutating the canonical execution pack."""
    action = str(execution_pack.get("action") or "IDLE")
    if action != "EXECUTE_MOTOR_PROGRAM":
        return action
    inputs = dict(execution_pack.get("execution_inputs") or {})
    transport = str(inputs.get("motor_transport") or "")
    if transport not in _MOTOR_TRANSPORTS:
        raise ValueError("motor_transport must be USE_TOOL or USE_SKILL")
    return transport


def motor_program_from_pack(execution_pack: Mapping[str, Any]) -> MotorProgram | None:
    """Parse the exact canonical program transported by an execution pack, if present."""
    inputs = execution_pack.get("execution_inputs") or {}
    if not isinstance(inputs, Mapping):
        return None
    value = inputs.get("motor_program")
    if value is None:
        return None
    if isinstance(value, MotorProgram):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("motor_program must be a mapping or MotorProgram")
    return MotorProgram.from_dict(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _episode_field(episode: Any, name: str) -> Any:
    if isinstance(episode, Mapping):
        return episode.get(name)
    return getattr(episode, name, None)


def _source_urls(value: Any) -> tuple[str, ...]:
    """Extract concrete URL/source-id facts from a normalized tool result."""
    urls: list[str] = []

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if key in {"url", "link", "source_url"}:
                    text = str(nested or "").strip()
                    if text.startswith(("http://", "https://")) and text not in urls:
                        urls.append(text)
                elif key == "source_id":
                    text = str(nested or "").strip()
                    if text and text not in urls:
                        urls.append(text)
                elif key in {"sources", "results", "items"}:
                    visit(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                visit(nested)

    visit(value)
    return tuple(urls)


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
_TEST_MARKERS = (
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
_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "edit_file",
        "patch_file",
        "apply_patch",
        "cli.write_file",
        "cli.edit_file",
        "cli.patch_file",
    }
)


def _normalized(value: Any) -> str:
    if isinstance(value, Mapping):
        raw = json.dumps(dict(value), sort_keys=True, ensure_ascii=False, default=str)
    else:
        raw = str(value or "")
    return " ".join(re.findall(r"[a-z0-9]+", raw.lower()))


def _digest(value: Any) -> str:
    if isinstance(value, str):
        raw = value
    else:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _command(tc: Mapping[str, Any]) -> str:
    args = tc.get("args")
    if not isinstance(args, Mapping):
        return ""
    return str(args.get("command") or args.get("cmd") or "").strip()


def _is_test_command(command: str) -> bool:
    low = command.lower()
    return any(marker in low for marker in _TEST_MARKERS)


def _matches_check(requested: str, tc: Mapping[str, Any]) -> bool:
    requirement = _normalized(requested)
    terms = set(requirement.split())
    command = _command(tc)
    command_terms = set(_normalized(command).split())
    if (
        "test-path" in requested.lower()
        or {"directory", "creation", "check"} <= terms
        or {"file", "existence", "check"} <= terms
    ):
        if "test-path" not in command.lower():
            return False
        scope_terms = terms - {
            "check",
            "creation",
            "directory",
            "existence",
            "file",
            "path",
            "test",
        }
        return not scope_terms or scope_terms <= command_terms
    if terms & {"pytest", "test", "tests", "unittest"}:
        if not _is_test_command(command):
            return False
        # A bare runner/check name is an alias for the final test state.  Once
        # the requirement names a suite/path, bind it to a call containing that
        # scope so separate unit/integration requirements cannot share one
        # unrelated GREEN receipt.
        scope_terms = terms - {
            "check",
            "checks",
            "m",
            "python",
            "pytest",
            "q",
            "run",
            "runs",
            "test",
            "tests",
            "unittest",
            "using",
            "via",
            "with",
        }
        return not scope_terms or scope_terms <= command_terms
    facts = _normalized(
        {
            "tool_name": tc.get("tool_name"),
            "args": tc.get("args"),
            "stdout": tc.get("stdout"),
            "result": tc.get("result"),
        }
    )
    return bool(requirement and requirement in facts)


def _exit_code(tc: Mapping[str, Any]) -> int | None:
    result = tc.get("result")
    if not isinstance(result, Mapping):
        return None
    raw = result.get("exit_code", result.get("returncode"))
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _tool_call_passed(tc: Mapping[str, Any]) -> bool:
    status = str(tc.get("status") or "").strip().lower()
    code = _exit_code(tc)
    return status in {"success", "passed", "ok", "completed"} and code in {None, 0}


def _check_call_passed(tc: Mapping[str, Any]) -> bool:
    if not _tool_call_passed(tc):
        return False
    if "test-path" not in _command(tc).lower():
        return True
    # PowerShell Test-Path exits zero even when the predicate is false.  Require
    # an explicit true result (and reject mixed True/False batches) instead of
    # treating process success as filesystem proof.
    candidates: list[Any] = [tc.get("stdout")]
    result = tc.get("result")
    if isinstance(result, Mapping):
        candidates.extend(result.get(key) for key in ("stdout", "output", "result", "value"))
    truth_values: list[bool] = []
    for candidate in candidates:
        if isinstance(candidate, bool):
            truth_values.append(candidate)
        elif isinstance(candidate, str):
            truth_values.extend(
                token.lower() == "true"
                for token in re.findall(r"\b(?:true|false)\b", candidate, re.IGNORECASE)
            )
    return bool(truth_values) and all(truth_values)


def _trusted_qwen_files(qwen: Mapping[str, Any], tool_calls: tuple[Any, ...]) -> tuple[str, ...]:
    files: list[str] = []
    raw = _mapping(qwen.get("raw_bridge_result"))
    ledger = _mapping(raw.get("ledger"))
    for item in ledger.get("touched_files") or ():
        path = str(item.get("path") if isinstance(item, Mapping) else item or "").strip()
        if path and path not in files:
            files.append(path)
    diff = str(raw.get("git_diff") or "")
    for path in re.findall(r"^\+\+\+ b/(.+)$", diff, flags=re.MULTILINE):
        if path not in files:
            files.append(path)
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        if str(call.get("tool_name") or "") not in _WRITE_TOOLS:
            continue
        args = call.get("args")
        path = str(args.get("path") if isinstance(args, Mapping) else "").strip()
        if path and _tool_call_passed(call) and path not in files:
            files.append(path)
    return tuple(files)


def _trusted_qwen_commands(tool_calls: tuple[Any, ...]) -> tuple[str, ...]:
    commands: list[str] = []
    for call in tool_calls:
        if not isinstance(call, Mapping):
            continue
        if str(call.get("tool_name") or "") not in _COMMAND_TOOLS:
            continue
        command = _command(call)
        if command and command not in commands:
            commands.append(command)
    return tuple(commands)


def _trusted_qwen_sources(tool_calls: tuple[Any, ...]) -> tuple[str, ...]:
    sources: list[str] = []
    for call in tool_calls:
        if not isinstance(call, Mapping) or not _tool_call_passed(call):
            continue
        for source in _source_urls(call.get("result")):
            if source not in sources:
                sources.append(source)
    return tuple(sources)


def _trusted_qwen_artifacts(qwen: Mapping[str, Any]) -> tuple[Any, ...]:
    raw = _mapping(qwen.get("raw_bridge_result"))
    ledger = _mapping(raw.get("ledger"))
    artifacts = ledger.get("artifacts")
    if not isinstance(artifacts, (list, tuple)):
        return ()
    return tuple(item for item in artifacts if isinstance(item, (str, Mapping)))


def _qwen_check_results(
    program: MotorProgram,
    tool_calls: tuple[Any, ...],
) -> tuple[dict[str, Any], ...]:
    results: list[dict[str, Any]] = []
    factual_calls = [tc for tc in tool_calls if isinstance(tc, Mapping)]
    for requested in program.evidence_contract.required_checks:
        matches = [tc for tc in factual_calls if _matches_check(requested, tc)]
        if not matches:
            continue
        # A check is a stateful motor action.  Test-first execution commonly emits
        # a deliberate RED receipt before implementation and a GREEN receipt after
        # it.  The last matching executor call is the resulting check state; the
        # complete red-to-green history remains preserved in tool_calls.
        selected = matches[-1]
        passed = _check_call_passed(selected)
        provenance = {
            "source": "qwen_tool_call",
            "tool_call_id": str(selected.get("tool_call_id") or ""),
            "tool_name": str(selected.get("tool_name") or ""),
            "command": _command(selected),
            "stdout_digest": _digest(str(selected.get("stdout") or "")),
            "result_digest": _digest(selected.get("result") or {}),
            "exit_code": _exit_code(selected),
        }
        results.append(
            {
                "requested": requested,
                "status": "passed" if passed else "failed",
                "executor_provenance": provenance,
                "failure_reason": "" if passed else "executor check failed",
            }
        )
    return tuple(results)


def _structured_content_present(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(_structured_content_present(nested) for nested in value.values())
    if isinstance(value, (list, tuple)):
        return any(_structured_content_present(nested) for nested in value)
    if isinstance(value, str):
        return bool(value.strip())
    return value is not None


def _direct_check_passed(
    requested: str,
    result: Any,
    sources: tuple[str, ...],
    *,
    tool_name: str,
) -> bool:
    requirement = _normalized(requested).replace(" ", "_")
    is_web_fetch = _normalized(tool_name).replace(" ", "_") == "web_fetch"
    if requirement == "fetch":
        return is_web_fetch and _structured_content_present(result)
    if requirement == "fetch_verified":
        return is_web_fetch and _structured_content_present(result) and bool(sources)
    if requirement in {"content_present", "structured_content", "results_present"}:
        return _structured_content_present(result)
    if requirement in {"source_present", "source_backed", "url_present"}:
        return bool(sources)
    if requirement in {"artifact_present", "artifacts_present"}:
        return _structured_content_present(result)
    return bool(
        _structured_content_present(result) and _normalized(requested) in _normalized(result)
    )


def _direct_check_results(
    program: MotorProgram,
    *,
    tool_name: str,
    tool_status: str,
    result: Any,
    sources: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    results = []
    factual_success = tool_status == "success"
    provenance = {
        "source": "direct_tool",
        "tool_name": tool_name,
        "tool_status": tool_status,
        "result_digest": _digest(result),
    }
    for requested in program.evidence_contract.required_checks:
        passed = factual_success and _direct_check_passed(
            requested,
            result,
            sources,
            tool_name=tool_name,
        )
        results.append(
            {
                "requested": requested,
                "status": "passed" if passed else "failed",
                "executor_provenance": dict(provenance),
                "failure_reason": "" if passed else "direct tool check failed",
            }
        )
    return tuple(results)


def execution_evidence_from_fold(episode: Any) -> ExecutionEvidence:
    """Build factual limb evidence from folded, normalized AUTO output only.

    The motor program supplies identity, never execution claims. Conversely, normalized
    tool/Qwen output supplies facts but cannot revise intent, pressure, pathway, outcome,
    acceptance criteria, or any other field owned by Omega's program.
    """
    episode_id = str(_episode_field(episode, "episode_id") or "")
    meta = _mapping(_episode_field(episode, "meta"))
    program_value = meta.get("motor_program")
    if isinstance(program_value, MotorProgram):
        program = program_value
    elif isinstance(program_value, Mapping):
        program = MotorProgram.from_dict(program_value)
    else:
        raise ValueError("folded episode is missing a canonical motor_program")
    if not episode_id or program.episode_id != episode_id:
        raise ValueError("motor program episode_id does not match the folded episode")

    auto = _mapping(_episode_field(episode, "auto"))
    outputs = _mapping(auto.get("outputs"))
    qwen = _mapping(outputs.get("qwen_skill_result") or outputs.get("skill_result"))
    if qwen:
        result_episode_id = str(qwen.get("episode_id") or "")
        if result_episode_id and result_episode_id != episode_id:
            raise ValueError("qwen result episode_id does not match the folded episode")
        status = _EVIDENCE_STATUSES.get(str(qwen.get("status") or ""), "failure")
        tool_calls = tuple(qwen.get("tool_calls") or ())
        commands_run = _trusted_qwen_commands(tool_calls)
        return ExecutionEvidence(
            program_id=program.program_id,
            episode_id=episode_id,
            status=status,
            tool_calls=tool_calls,
            commands_run=commands_run,
            sources_consulted=_trusted_qwen_sources(tool_calls),
            files_changed=_trusted_qwen_files(qwen, tool_calls),
            artifacts=_trusted_qwen_artifacts(qwen),
            tests_run=tuple(command for command in commands_run if _is_test_command(command)),
            verification_results=_qwen_check_results(program, tool_calls),
            blockers=qwen.get("unresolved_blockers") or (),
            progress_digest=str(qwen.get("progress_digest") or qwen.get("summary") or ""),
            # A successful normalized bridge result cannot simultaneously be a
            # provider failure.  Failure classification is derived upstream from
            # explicit error fields; scanning an entire successful raw payload can
            # mistake opaque IDs (for example a hex token containing "429") for a
            # rate-limit event.
            failure_kind=(
                ""
                if status == "success"
                else normalize_executor_failure_kind(
                    qwen.get("failure_class"), qwen.get("failure_reason")
                )
            ),
            failure_reason=str(qwen.get("failure_reason") or ""),
        )

    tool_status = str(outputs.get("tool_status") or auto.get("status") or "failure")
    status = _EVIDENCE_STATUSES.get(tool_status, "failure")
    execution_pack = _mapping(auto.get("execution_pack"))
    tool_name = str(execution_pack.get("tool") or "")
    tool_result = outputs.get("tool_result")
    sources = _source_urls(tool_result)
    has_artifact = _structured_content_present(tool_result)
    tool_call = {
        "tool_name": tool_name,
        "args": dict(outputs.get("tool_args") or {}),
        "status": tool_status,
    }
    failure_reason = str(auto.get("failure_reason") or outputs.get("tool_error") or "")
    return ExecutionEvidence(
        program_id=program.program_id,
        episode_id=episode_id,
        status=status,
        tool_calls=(tool_call,) if outputs.get("tool_args") is not None else (),
        sources_consulted=sources,
        artifacts=(tool_result,) if has_artifact else (),
        verification_results=_direct_check_results(
            program,
            tool_name=tool_name,
            tool_status=str(outputs.get("tool_status") or "failure"),
            result=tool_result,
            sources=sources,
        ),
        failure_kind=normalize_executor_failure_kind(
            auto.get("failure_kind"),
            failure_reason,
            outputs.get("tool_error"),
        ),
        failure_reason=failure_reason,
    )

"""Phase 6M — raw tool routing retirement: agentic work cannot bypass the skill layer."""

from __future__ import annotations

from pathlib import Path

import pytest

from schema.execution_skill import EXECUTION_KINDS
from schema.routing_policy import (
    AGENTIC_EXTERNAL_WORK,
    RoutingBypassError,
    assert_routing,
    check_routing,
    classify_work,
)

_REPO = Path(__file__).resolve().parents[2]


# ── acceptance: agentic external work routes through a skill ───────────────────
@pytest.mark.parametrize("work", ["inspection", "patch", "edit", "tests", "command"])
def test_agentic_work_routes_through_skill(work):
    assert check_routing(stage="global", work_kind=work, target="USE_SKILL").allowed is True
    bad = check_routing(stage="global", work_kind=work, target="USE_TOOL")
    assert bad.allowed is False and bad.bypass_kind == "agentic_bypass"


def test_every_execution_kind_is_agentic_external():
    for k in EXECUTION_KINDS:
        assert classify_work(k) == "agentic_external"
        assert check_routing(stage="global", work_kind=k, target="USE_TOOL").allowed is False


# ── acceptance: raw write/command bypass fails closed ─────────────────────────
@pytest.mark.parametrize(
    "tool", ["write_file", "execute_command", "run_command", "patch_file", "shell"]
)
def test_raw_world_mutating_tool_fails_closed_at_global(tool):
    v = check_routing(stage="global", tool=tool)
    assert v.allowed is False and v.bypass_kind == "raw_world_mutating"


def test_raw_tool_also_blocked_at_meta():
    assert check_routing(stage="meta", tool="write_file").allowed is False


def test_marked_internal_substrate_raw_tool_is_allowed():
    # "...unless explicitly marked internal deterministic substrate"
    assert check_routing(stage="meta", tool="write_file", marked_internal=True).allowed is True


# ── acceptance: internal memory/graph reads still work ────────────────────────
@pytest.mark.parametrize(
    "op",
    [
        "memory_recall",
        "recall",
        "graph_lookup",
        "policy_read",
        "episode_state_read",
        "status_check",
        "telemetry",
    ],
)
def test_internal_substrate_reads_allowed(op):
    assert classify_work(op) == "internal_substrate"
    assert check_routing(stage="meta", work_kind=op).allowed is True
    assert check_routing(stage="global", work_kind=op, target="direct").allowed is True


# ── structural forbidden bypasses fail closed ─────────────────────────────────
@pytest.mark.parametrize(
    "stage,target",
    [
        ("meta", "qwen_agentcore"),
        ("meta", "qwen_bridge_raw"),
        ("auto", "qwen_unguarded"),
        ("qwen", "canonical_memory_write"),
        ("qwen", "new_top_level_goal"),
        ("qwen", "continuation_outside_dispatch"),
    ],
)
def test_structural_bypasses_fail_closed(stage, target):
    v = check_routing(stage=stage, target=target)
    assert v.allowed is False and v.bypass_kind == target


def test_assert_routing_raises_on_bypass():
    with pytest.raises(RoutingBypassError):
        assert_routing(stage="qwen", target="canonical_memory_write")
    # a legal route does not raise
    assert assert_routing(stage="global", work_kind="patch", target="USE_SKILL").allowed is True


def test_sanctioned_skill_route_is_allowed():
    # the full sanctioned chain: Global USE_SKILL -> Meta skill -> Auto guarded -> qwen
    assert check_routing(stage="global", work_kind="repair", target="USE_SKILL").allowed
    assert check_routing(stage="meta", target="selected_skill").allowed
    assert check_routing(stage="auto", target="guarded_qwen_dispatch").allowed
    assert check_routing(stage="qwen", target="allowed_internal_tool").allowed


# ── spine audit: no direct qwen AgentCore / raw-bridge call in the cognition spine ──
_SPINE_DIRS = ["global_loop", "meta_loop", "omega_loop", "pressure_loop", "deepsleep_loop"]
_SPINE_FILES = ["cognition_chain.py"]
_FORBIDDEN_IN_SPINE = (
    "AgentCore",
    "_dispatch_ws",
    "default_apply_invoker",
    "default_qwen_skill_invoker",
    "websockets",
    "ws://127.0.0.1:8092",
)
# The Python qwen bridge is retired. Any raw bridge marker in Auto is forbidden.
_SANCTIONED_BRIDGE_FILES = set()


def _spine_py_files():
    files = []
    for d in _SPINE_DIRS:
        files += sorted((_REPO / d).rglob("*.py"))
    files += [_REPO / f for f in _SPINE_FILES]
    return files


def test_no_qwen_bridge_or_agentcore_in_cognition_spine():
    offenders = []
    for f in _spine_py_files():
        if not f.is_file():
            continue
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for pat in _FORBIDDEN_IN_SPINE:
            if pat in txt:
                offenders.append(f"{f.relative_to(_REPO).as_posix()} :: {pat}")
    assert offenders == [], f"cognition spine reaches qwen directly: {offenders}"


def test_auto_loop_bridge_access_is_confined_to_sanctioned_files():
    offenders = []
    for f in sorted((_REPO / "auto_loop").rglob("*.py")):
        rel = f.relative_to(_REPO).as_posix()
        if rel in _SANCTIONED_BRIDGE_FILES:
            continue
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for pat in ("_dispatch_ws", "default_apply_invoker", "websockets.connect", "AgentCore"):
            if pat in txt:
                offenders.append(f"{rel} :: {pat}")
    assert offenders == [], f"unsanctioned auto_loop file reaches the bridge: {offenders}"

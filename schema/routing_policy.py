"""Routing policy — keep agentic work inside the skill layer (Phase 6M).

Raw world-mutating tools (write_file, execute_command, patch, …) are INTERNAL to skills:
agentic external work must route Global USE_SKILL -> Meta selected skill -> Auto qwen dispatch
-> qwen's allowed internal tools. Omega's own deterministic substrate (memory recall, graph
lookup, policy read, episode-state read, status check, telemetry) is the allowed exception and
may be reached directly. This module is the codified policy + guard; the spine audit (no direct
qwen AgentCore / raw-bridge call outside the sanctioned dispatch) lives in the tests.

Additive + flag-gated: with OMEGA_USE_SKILL_ROUTING off, routing is byte-identical; this guard
is the safety net that proves agentic work cannot bypass the skill layer when it is on.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from schema.execution_skill import EXECUTION_KINDS

# Raw, world-mutating tools — must be INTERNAL to a skill, never first-class agentic execution.
WORLD_MUTATING_RAW_TOOLS = frozenset(
    {
        "write_file",
        "execute_command",
        "run_command",
        "patch_file",
        "edit_file",
        "apply_patch",
        "delete_file",
        "create_file",
        "move_file",
        "shell",
        "bash",
        "git_commit",
        "git_push",
    }
)

# Omega's own deterministic substrate — allowed to be reached directly (the spec exceptions).
INTERNAL_SUBSTRATE_OPS = frozenset(
    {
        "memory_recall",
        "recall",
        "graph_lookup",
        "policy_read",
        "episode_state_read",
        "status_check",
        "deterministic_status_check",
        "telemetry",
        "internal_telemetry",
    }
)

# Aliases for agentic external work kinds (mapped onto the skill execution_kinds).
_AGENTIC_ALIASES = frozenset(
    {
        "inspection",
        "repo_inspection",
        "patch",
        "edit",
        "tests",
        "test",
        "command",
        "refactor",
        "repair",
        "build",
        "migration",
        "review",
        "debug",
        "debugging",
    }
)
_EXECUTION_KINDS_LOWER = frozenset(k.lower() for k in EXECUTION_KINDS)
AGENTIC_EXTERNAL_WORK = _AGENTIC_ALIASES | _EXECUTION_KINDS_LOWER

# Structural forbidden bypasses keyed by (stage, target). The two Global->raw bypasses are
# enforced via the world-mutating tool check, so they are NOT duplicated here.
FORBIDDEN_BYPASSES = {
    ("meta", "qwen_agentcore"): "Meta must not call qwen AgentCore directly",
    ("meta", "qwen_bridge_raw"): "Meta must not call the raw qwen bridge",
    ("auto", "qwen_unguarded"): "Auto must not dispatch qwen unguarded (guard first)",
    ("qwen", "canonical_memory_write"): "qwen must not write canonical Omega memory",
    ("qwen", "new_top_level_goal"): "qwen must not create a new top-level goal",
    ("qwen", "continuation_outside_dispatch"): "qwen must not continue outside its dispatch",
}

_SKILL_TARGETS = frozenset({"use_skill", "skill"})


@dataclass(frozen=True)
class RoutingVerdict:
    allowed: bool
    reason: str = ""
    bypass_kind: Optional[str] = None

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reason": self.reason, "bypass_kind": self.bypass_kind}


def _norm(s: Optional[str]) -> str:
    return (s or "").strip().lower()


def is_world_mutating_raw(tool: Optional[str]) -> bool:
    return _norm(tool) in WORLD_MUTATING_RAW_TOOLS


def is_internal_substrate(op: Optional[str]) -> bool:
    return _norm(op) in INTERNAL_SUBSTRATE_OPS


def classify_work(work_kind: Optional[str]) -> str:
    """ "agentic_external" | "internal_substrate" | "unknown"."""
    wk = _norm(work_kind)
    if wk in INTERNAL_SUBSTRATE_OPS:
        return "internal_substrate"
    if wk in AGENTIC_EXTERNAL_WORK:
        return "agentic_external"
    return "unknown"


def check_routing(
    *,
    stage: str,
    work_kind: Optional[str] = None,
    tool: Optional[str] = None,
    target: Optional[str] = None,
    marked_internal: bool = False,
) -> RoutingVerdict:
    """Verdict on a single routing attempt. Fails CLOSED on any forbidden bypass."""
    st = _norm(stage)
    tgt = _norm(target)

    # 1. structural bypasses (Meta->AgentCore/bridge, Auto->unguarded, qwen->memory/goal/continuation)
    if tgt and (st, tgt) in FORBIDDEN_BYPASSES:
        return RoutingVerdict(False, FORBIDDEN_BYPASSES[(st, tgt)], bypass_kind=tgt)

    # 2. raw world-mutating tool as first-class execution at Global/Meta (the Global->raw bypasses)
    if tool and is_world_mutating_raw(tool) and st in ("global", "meta") and not marked_internal:
        return RoutingVerdict(
            False,
            f"{st} must not route to raw world-mutating tool {tool!r}; route agentic work via USE_SKILL",
            bypass_kind="raw_world_mutating",
        )

    # 3. agentic external work at Global must target USE_SKILL (never a raw tool / USE_TOOL)
    if work_kind is not None and classify_work(work_kind) == "agentic_external" and st == "global":
        if tgt not in _SKILL_TARGETS:
            return RoutingVerdict(
                False,
                f"agentic-external work {work_kind!r} must route via USE_SKILL, not {target!r}",
                bypass_kind="agentic_bypass",
            )

    # internal substrate + everything else is allowed direct
    return RoutingVerdict(True, "allowed")


def assert_routing(**kw) -> RoutingVerdict:
    """Like check_routing but raises on a forbidden bypass (for guard call-sites that must
    fail hard rather than branch)."""
    v = check_routing(**kw)
    if not v.allowed:
        raise RoutingBypassError(v.reason)
    return v


class RoutingBypassError(RuntimeError):
    """Raised when a routing attempt would bypass the skill layer."""

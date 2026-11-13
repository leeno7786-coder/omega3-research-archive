"""General skill-execution guard (Phase 6I) — generalize safety from APPLY-only to any
skill-mediated qwen dispatch.

This produces a structured GuardStatus BEFORE qwen is contacted: it validates the safety
class, rejects forbidden-in-allowed tool sets, denies self_modification, demands
confirmation + checkpoint per class, requires a command policy for command_safe, asserts
the workspace jail (qwen is jailed to the operator workspace; Omega source is invisible —
the sandbox invariant), and declares the timeout + fail-closed policy the dispatcher
enforces at runtime. Legacy APPLY guards were retired in Phase 6P — this module is the sole guard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from core.paths import OMEGA_ROOT
from schema.execution_skill import SAFETY_CLASSES


def _jail_root_disjoint_from_omega(workspace_root: str) -> bool:
    """True iff ``workspace_root`` resolves OUTSIDE (and is not an ancestor of) the Omega tree.

    Jailing qwen INTO ``OMEGA_ROOT`` (or a parent that contains it) would expose Omega's own
    source/state to agentic execution — the exact thing the sandbox invariant forbids. Fails
    SAFE: an unresolvable root is treated as not disjoint (blocks)."""
    try:
        root = Path(str(workspace_root)).resolve()
        omega = Path(OMEGA_ROOT).resolve()
    except (OSError, ValueError):
        return False
    if root == omega:
        return False
    if omega in root.parents:  # root lives inside the Omega tree
        return False
    if root in omega.parents:  # root is an ancestor that contains the Omega tree
        return False
    return True


# Per-safety-class policy (work-order 6I default policy).
_POLICY: dict[str, dict] = {
    "read_only": dict(
        checkpoint=False,
        confirmation=False,
        writes=False,
        network=False,
        command=False,
        command_policy=False,
    ),
    "network_read": dict(
        checkpoint=False,
        confirmation=False,
        writes=False,
        network=True,
        command=False,
        command_policy=False,
    ),
    "write_safe": dict(
        checkpoint=True,
        confirmation=True,
        writes=True,
        network=False,
        command=False,
        command_policy=False,
    ),
    "command_safe": dict(
        checkpoint=True,
        confirmation=False,
        writes=True,
        network=True,
        command=True,
        command_policy=True,
    ),
    "destructive": dict(
        checkpoint=True,
        confirmation=True,
        writes=True,
        network=True,
        command=True,
        command_policy=True,
        stricter_denylist=True,
    ),
    "desktop_control": dict(
        checkpoint=True,
        confirmation=True,
        writes=True,
        network=True,
        command=True,
        desktop=True,
        strict_timeout=True,
    ),
    "self_modification": dict(denied=True),
}
_WRITE_TOOLS = frozenset(
    {"write_file", "edit_file", "patch_file", "apply_patch", "restore_project_from_memory"}
)
_COMMAND_TOOLS = frozenset({"run_command", "run_tests", "execute_command", "shell"})


@dataclass(frozen=True)
class GuardStatus:
    allowed: bool
    status: str  # "allowed" | "blocked" | "requires_confirmation"
    reason: str = ""
    safety_class: str = ""
    checkpoint_required: bool = False
    confirmation_required: bool = False
    timeout_ms: int = 0
    fail_closed_on_missing_done: bool = True  # a missing bridge 'done' event is a failure
    workspace_jail: bool = (
        False  # True ONLY when a validated jail root (disjoint from OMEGA_ROOT) is in force
    )
    blocked_tools: tuple = ()
    policy: dict = field(default_factory=dict)

    @classmethod
    def allow(cls, **kw) -> "GuardStatus":
        return cls(allowed=True, status="allowed", **kw)

    @classmethod
    def block(cls, reason: str, **kw) -> "GuardStatus":
        return cls(allowed=False, status="blocked", reason=reason, **kw)

    @classmethod
    def needs_confirmation(cls, reason: str, **kw) -> "GuardStatus":
        return cls(allowed=False, status="requires_confirmation", reason=reason, **kw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "status": self.status,
            "reason": self.reason,
            "safety_class": self.safety_class,
            "checkpoint_required": self.checkpoint_required,
            "confirmation_required": self.confirmation_required,
            "timeout_ms": self.timeout_ms,
            "fail_closed_on_missing_done": self.fail_closed_on_missing_done,
            "workspace_jail": self.workspace_jail,
            "blocked_tools": list(self.blocked_tools),
            "policy": dict(self.policy),
        }


def skill_execution_guard(
    dispatch,
    *,
    confirmed: bool = False,
    allow_self_modification: bool = False,
    command_policy: Optional[Callable] = None,
    workspace_root: Optional[str] = None,
) -> GuardStatus:
    """Evaluate a QwenSkillExecutionDispatch against its safety class. Returns a structured
    GuardStatus; the caller must NOT contact qwen unless status == 'allowed'."""
    sc = dispatch.safety.get("safety_class", "")

    # 1. validate safety_class
    if sc not in SAFETY_CLASSES:
        return GuardStatus.block(f"unknown safety_class: {sc!r}", safety_class=sc)

    # 5. self-mod denied by default
    if sc == "self_modification" and not allow_self_modification:
        return GuardStatus.block(
            "self_modification denied (agentic-execution sandbox invariant)", safety_class=sc
        )

    policy = dict(_POLICY.get(sc, {}))

    # 2/3. validate allowed_tools / block forbidden_tools — no tool may be both
    overlap = tuple(sorted(set(dispatch.allowed_tools) & set(dispatch.forbidden_tools)))
    if overlap:
        return GuardStatus.block(
            f"forbidden tool(s) present in allowed set: {list(overlap)}",
            safety_class=sc,
            blocked_tools=overlap,
        )

    allowed = set(dispatch.allowed_tools)
    if sc in {"read_only", "network_read"} and allowed & (_WRITE_TOOLS | _COMMAND_TOOLS):
        blocked = tuple(sorted(allowed & (_WRITE_TOOLS | _COMMAND_TOOLS)))
        return GuardStatus.block(
            f"{sc} cannot expose mutating tools: {list(blocked)}",
            safety_class=sc,
            blocked_tools=blocked,
        )
    if sc == "write_safe" and allowed & (_COMMAND_TOOLS - {"run_tests"}):
        blocked = tuple(sorted(allowed & (_COMMAND_TOOLS - {"run_tests"})))
        return GuardStatus.block(
            f"write_safe cannot expose arbitrary command tools: {list(blocked)}",
            safety_class=sc,
            blocked_tools=blocked,
        )
    if sc == "command_safe" and "restore_project_from_memory" in allowed:
        return GuardStatus.block(
            "command_safe cannot expose restore_project_from_memory",
            safety_class=sc,
            blocked_tools=("restore_project_from_memory",),
        )

    # command_safe / destructive / desktop require a command policy
    if policy.get("command_policy") and command_policy is None:
        return GuardStatus.block(f"{sc} requires a command policy", safety_class=sc, policy=policy)

    # 7/8. limits must be present + positive (9 enforced at runtime via max_files_changed)
    limits = dispatch.limits or {}
    if not (int(limits.get("max_steps", 0)) > 0 and int(limits.get("max_duration_ms", 0)) > 0):
        return GuardStatus.block(
            "missing or non-positive limits (max_steps / max_duration_ms)",
            safety_class=sc,
            policy=policy,
        )
    timeout_ms = int(limits["max_duration_ms"])

    checkpoint_required = bool(policy.get("checkpoint"))
    confirmation_required = bool(policy.get("confirmation")) or bool(
        dispatch.safety.get("requires_confirmation")
    )

    common = dict(
        safety_class=sc,
        checkpoint_required=checkpoint_required,
        confirmation_required=confirmation_required,
        timeout_ms=timeout_ms,
        policy=policy,
    )

    # 6. confirmation gate
    if confirmation_required and not confirmed:
        return GuardStatus.needs_confirmation(f"{sc} requires operator confirmation", **common)

    # 7. workspace jail root REQUIRED for any write-capable class — fail closed if a write
    # skill is dispatched without a declared operator-workspace root to jail qwen into.
    if policy.get("writes") and not workspace_root:
        return GuardStatus.block(f"{sc} requires a workspace jail root (none provided)", **common)

    # 7b. A provided jail root MUST be disjoint from the Omega tree — jailing qwen INTO (or
    # above) OMEGA_ROOT would expose Omega's own source/state to agentic execution, the exact
    # thing invariant 2 forbids. A present-but-overlapping root is worse than none. workspace_jail
    # is DERIVED from this check (honest signal), never assumed: True only with a validated root.
    workspace_jail = False
    if workspace_root:
        if not _jail_root_disjoint_from_omega(workspace_root):
            return GuardStatus.block(
                f"workspace jail root must be OUTSIDE the Omega tree: {workspace_root!r}", **common
            )
        workspace_jail = True

    return GuardStatus.allow(workspace_jail=workspace_jail, **common)

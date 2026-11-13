"""
Execution Skill Frontmatter Schema
==================================

Canonical definition + validator for SKILL.md frontmatter so a bundled Omega skill
becomes an EXECUTABLE CONTRACT, not just prompt text. Phase 6B of the skill-mediated
qwen executor bridge.

A SKILL.md frontmatter declares the bounded box qwen executes inside: which task it
serves (skill_class / intents / execution_kinds / domains), which tools it may and may
not use, its safety class, its hard limits, the evidence it must return, its completion
criteria, and the schema of its structured output.

ARCHITECTURAL RULES:
    - This module is the sole AUTHORITY for the frontmatter contract. Do not redefine
      the enums elsewhere. (The config-side wire schema is schema/skill_execution.schema.yaml,
      validated for config/skill_execution.yaml; the unreferenced duplicate
      execution_skill.schema.yaml was removed 2026-07-04.)
    - Cross-field safety rules (read-only must not write, destructive must confirm,
      self_modification denied by default) live here — jsonschema cannot express them.
    - Per the agentic-execution sandbox invariant (Omega source is hidden from agentic
      execution), self_modification is DENIED unless an explicit policy flag is passed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

KIND = "execution_skill"

# 15 skill classes — the task family a skill belongs to.
SKILL_CLASSES = frozenset(
    {
        "inspection_skill",
        "repair_skill",
        "verification_skill",
        "research_skill",
        "artifact_skill",
        "browser_skill",
        "ocr_skill",
        "desktop_skill",
        "planning_skill",
        "review_skill",
        "maintenance_skill",
        "debugging_skill",
        "refactor_skill",
        "testing_skill",
        "devops_skill",
    }
)

# 21 execution kinds — the concrete operations a skill performs.
EXECUTION_KINDS = frozenset(
    {
        "INSPECT",
        "SEARCH",
        "FETCH",
        "READ",
        "PATCH",
        "WRITE",
        "RUN",
        "TEST",
        "VERIFY",
        "REPAIR",
        "REPORT",
        "ARTIFACT",
        "BROWSER",
        "OCR",
        "DESKTOP",
        "APPLY",
        "PLAN",
        "REVIEW",
        "DEBUG",
        "REFACTOR",
        "MAINTAIN",
    }
)

# 7 safety classes — increasing privilege.
SAFETY_CLASSES = frozenset(
    {
        "read_only",
        "network_read",
        "write_safe",
        "command_safe",
        "destructive",
        "desktop_control",
        "self_modification",
    }
)

REQUIRED_FIELDS = (
    "name",
    "kind",
    "version",
    "enabled",
    "description",
    "skill_class",
    "intents",
    "execution_kinds",
    "domains",
    "allowed_tools",
    "forbidden_tools",
    "safety_class",
    "requires_confirmation_for",
    "max_steps",
    "max_duration_ms",
    "max_files_changed",
    "evidence_required",
    "completion_criteria",
    "failure_classes",
    "output_schema",
)

# Heuristic capability sets (by tool name) for the cross-field safety rules. Both bare
# and cli.-prefixed names are covered so the rules hold regardless of naming convention.
_WRITE_TOOLS = frozenset(
    {
        "write_file",
        "patch_file",
        "apply_patch",
        "edit_file",
        "run_command",
        "cli.write_file",
        "cli.patch_file",
        "cli.run_command",
    }
)
_DESTRUCTIVE_TOOLS = frozenset(
    {"run_command", "execute_command", "delete_file", "cli.run_command", "shell"}
)
# Execution kinds that mutate the world — incompatible with a read_only skill.
_WORLD_MUTATING_KINDS = frozenset(
    {"PATCH", "WRITE", "RUN", "REPAIR", "REFACTOR", "MAINTAIN", "APPLY"}
)

# skill classes allowed to declare an empty allowed_tools (pure reasoning), if read_only.
_TOOLLESS_OK_CLASSES = frozenset({"planning_skill", "review_skill"})


class ExecutionSkillSchemaError(ValueError):
    """Raised when SKILL.md frontmatter fails schema validation."""


def _is_positive_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v > 0


def _is_nonneg_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def validate_execution_skill_frontmatter(
    data: Any,
    *,
    allow_self_modification: bool = False,
) -> list[str]:
    """Return a list of validation errors (empty == valid). Enforces the 14 rules.

    ``allow_self_modification`` is the only policy escape hatch (default DENY), gated by
    the sandbox invariant — never set it true except from an explicit self-mod policy.
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["frontmatter must be a mapping"]

    # rule 1 — required fields
    for f in REQUIRED_FIELDS:
        if f not in data:
            errors.append(f"missing required field: {f}")

    # rule 2 — kind
    if data.get("kind") != KIND:
        errors.append(f"unknown kind: {data.get('kind')!r} (must be {KIND!r})")

    # rule 3 — skill_class
    skill_class = data.get("skill_class")
    if skill_class not in SKILL_CLASSES:
        errors.append(f"unknown skill_class: {skill_class!r}")

    # rule 4 — execution_kinds
    execution_kinds = data.get("execution_kinds")
    if not isinstance(execution_kinds, list):
        errors.append("execution_kinds must be a list")
        execution_kinds = []
    for k in execution_kinds:
        if k not in EXECUTION_KINDS:
            errors.append(f"unknown execution_kind: {k!r}")

    # rule 5 — safety_class
    safety = data.get("safety_class")
    if safety not in SAFETY_CLASSES:
        errors.append(f"unknown safety_class: {safety!r}")

    allowed = data.get("allowed_tools")
    if not isinstance(allowed, list):
        errors.append("allowed_tools must be a list")
        allowed = []

    # rule 6 — empty allowed_tools fails unless planning/review skill that is read_only
    if not allowed:
        exempt = (skill_class in _TOOLLESS_OK_CLASSES) and (safety == "read_only")
        if not exempt:
            errors.append(
                "allowed_tools is empty (only permitted for a read_only planning_skill/review_skill)"
            )

    # rule 7 — write tools / write execution kinds with read_only safety
    if safety == "read_only":
        write_tools = sorted(t for t in allowed if t in _WRITE_TOOLS)
        if write_tools:
            errors.append(f"read_only skill must not declare write tools: {write_tools}")
        write_kinds = sorted(k for k in execution_kinds if k in _WORLD_MUTATING_KINDS)
        if write_kinds:
            errors.append(
                f"read_only skill must not declare world-mutating execution_kinds: {write_kinds}"
            )

    # rule 8 — destructive tools/safety without confirmation
    rcf = data.get("requires_confirmation_for")
    if not isinstance(rcf, list):
        rcf = []
    destructive_tools = [t for t in allowed if t in _DESTRUCTIVE_TOOLS]
    if (safety == "destructive" or destructive_tools) and not rcf:
        errors.append("destructive tools/safety require a non-empty requires_confirmation_for")

    # rule 9 — self_modification denied unless policy allows
    if safety == "self_modification" and not allow_self_modification:
        errors.append("self_modification safety_class is denied by policy")

    # rule 11 — malformed output_schema
    output_schema = data.get("output_schema")
    if not isinstance(output_schema, dict):
        errors.append("output_schema must be a mapping (a JSON Schema object)")
    else:
        try:
            from jsonschema import Draft202012Validator

            Draft202012Validator.check_schema(output_schema)
        except Exception as exc:  # noqa: BLE001 — any malformed schema is a failure
            errors.append(f"malformed output_schema: {exc}")

    # rule 12 — max_steps positive
    if not _is_positive_int(data.get("max_steps")):
        errors.append("max_steps must be a positive integer")

    # rule 13 — max_duration_ms positive
    if not _is_positive_int(data.get("max_duration_ms")):
        errors.append("max_duration_ms must be a positive integer")

    # rule 14 — read_only skills cannot change files
    mfc = data.get("max_files_changed")
    if safety == "read_only":
        if mfc != 0:
            errors.append("read_only skill must have max_files_changed == 0")
    elif not _is_nonneg_int(mfc):
        errors.append("max_files_changed must be a non-negative integer")

    return errors


def is_selectable(data: Any, *, allow_self_modification: bool = False) -> bool:
    """rule 10 — a disabled skill LOADS (validates) but is never selectable; a skill is
    selectable only if it is enabled AND passes validation."""
    if not isinstance(data, dict):
        return False
    if not bool(data.get("enabled", False)):
        return False
    return not validate_execution_skill_frontmatter(
        data, allow_self_modification=allow_self_modification
    )


def parse_execution_skill_frontmatter(
    data: Any,
    *,
    allow_self_modification: bool = False,
) -> dict:
    """Validate + return the frontmatter dict, or raise ExecutionSkillSchemaError."""
    errors = validate_execution_skill_frontmatter(
        data, allow_self_modification=allow_self_modification
    )
    if errors:
        raise ExecutionSkillSchemaError("; ".join(errors))
    return dict(data)


@dataclass(frozen=True)
class ExecutionSkill:
    """A validated SKILL.md as a structured execution contract (Phase 6C).

    Built by the loader from validated frontmatter + the preserved body + the
    deterministic ``skill_id`` (``capability_id_for_name(name, scope='dev')``). The
    loader owns id computation so this schema object stays free of memory-layer imports.
    """

    name: str
    skill_id: str
    kind: str
    version: int
    enabled: bool
    description: str
    skill_class: str
    intents: tuple
    execution_kinds: tuple
    domains: tuple
    allowed_tools: tuple
    forbidden_tools: tuple
    safety_class: str
    requires_confirmation_for: tuple
    limits: dict
    evidence_required: tuple
    completion_criteria: tuple
    failure_classes: tuple
    output_schema: dict
    body_markdown: str
    source_path: str

    @property
    def selectable(self) -> bool:
        """rule 10 — disabled skills load but are never selectable."""
        return bool(self.enabled)

    @classmethod
    def from_validated_frontmatter(
        cls,
        fm: dict,
        *,
        skill_id: str,
        body_markdown: str,
        source_path: str,
        enabled: bool | None = None,
    ) -> "ExecutionSkill":
        def _tup(key: str) -> tuple:
            v = fm.get(key, [])
            return tuple(str(x) for x in v) if isinstance(v, list) else ()

        return cls(
            name=str(fm["name"]),
            skill_id=str(skill_id),
            kind=str(fm["kind"]),
            version=int(fm["version"]),
            enabled=bool(fm.get("enabled", True) if enabled is None else enabled),
            description=str(fm.get("description", "")),
            skill_class=str(fm["skill_class"]),
            intents=_tup("intents"),
            execution_kinds=_tup("execution_kinds"),
            domains=_tup("domains"),
            allowed_tools=_tup("allowed_tools"),
            forbidden_tools=_tup("forbidden_tools"),
            safety_class=str(fm["safety_class"]),
            requires_confirmation_for=_tup("requires_confirmation_for"),
            limits={
                "max_steps": int(fm["max_steps"]),
                "max_duration_ms": int(fm["max_duration_ms"]),
                "max_files_changed": int(fm["max_files_changed"]),
            },
            evidence_required=_tup("evidence_required"),
            completion_criteria=_tup("completion_criteria"),
            failure_classes=_tup("failure_classes"),
            output_schema=dict(fm.get("output_schema") or {}),
            body_markdown=str(body_markdown),
            source_path=str(source_path),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "skill_id": self.skill_id,
            "kind": self.kind,
            "version": self.version,
            "enabled": self.enabled,
            "description": self.description,
            "skill_class": self.skill_class,
            "intents": list(self.intents),
            "execution_kinds": list(self.execution_kinds),
            "domains": list(self.domains),
            "allowed_tools": list(self.allowed_tools),
            "forbidden_tools": list(self.forbidden_tools),
            "safety_class": self.safety_class,
            "requires_confirmation_for": list(self.requires_confirmation_for),
            "limits": dict(self.limits),
            "evidence_required": list(self.evidence_required),
            "completion_criteria": list(self.completion_criteria),
            "failure_classes": list(self.failure_classes),
            "output_schema": dict(self.output_schema),
            "body_markdown": self.body_markdown,
            "source_path": self.source_path,
        }

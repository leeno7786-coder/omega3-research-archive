"""User-created execution skills — disabled-by-default foundation (Phase 6N).

Prepares for future user expansion WITHOUT enabling unsafe arbitrary skills yet. User skills
use the SAME ExecutionSkill schema + validation + deterministic skill_id as bundled skills, but
the trust policy differs: a bundled skill may be enabled as authored, while a user skill is
forced DISABLED unless it is explicitly trusted (by name, or the global trust flag). Malformed
or unsafe (self_modification) user skills are rejected by the existing validation and never load.
The trust flag is OFF by default — no unsafe user skill can execute out of the box.

Pure-guidance exception: a skill that CANNOT act (read_only, empty allowed_tools,
max_files_changed 0 — the adapted agent-format libraries) is prompt guidance, not
execution. The disabled-by-default gate exists to stop unsafe execution, so it does
not apply to skills with no execution surface; those stay enabled as authored.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Iterable, Optional

from runtime.execution_skills import (
    SkillLoadDiagnostic,
    load_execution_skills,
)
from schema.execution_skill import ExecutionSkill

# Global trust-all flag default — individual trust still requires OMEGA_TRUST_USER_SKILLS names.
OMEGA_TRUST_USER_SKILLS = False


def is_pure_guidance(skill: ExecutionSkill) -> bool:
    """True when the skill has no execution surface: read-only, toolless, zero writes.

    Such a skill can only contribute its body markdown as guidance — there is
    nothing for the trust gate to protect against."""
    return (
        skill.safety_class == "read_only"
        and not skill.allowed_tools
        and skill.limits.get("max_files_changed") == 0
    )


def apply_user_skill_policy(
    skill: ExecutionSkill,
    *,
    trust_approved: Iterable[str] = (),
    trust_all: bool = False,
) -> ExecutionSkill:
    """Disabled-by-default: a user skill stays enabled ONLY if globally trusted, named in
    trust_approved, or pure guidance (no execution surface — see is_pure_guidance);
    otherwise its enabled flag is forced False (so it is not selectable)."""
    trusted = trust_all or skill.name in set(trust_approved) or is_pure_guidance(skill)
    if trusted or not skill.enabled:
        return skill
    return replace(skill, enabled=False)


def load_user_execution_skills(
    roots: Optional[list] = None,
    *,
    trust_approved: Iterable[str] = (),
    trust_all: Optional[bool] = None,
    config: Optional[dict] = None,
) -> tuple[list[ExecutionSkill], list[SkillLoadDiagnostic]]:
    """Load user skills through the bundled loader (same schema/validation/skill_id), then apply
    the disabled-by-default trust policy. A self_modification or malformed user skill is rejected
    by validation upstream and surfaces only as a diagnostic — never as a loaded skill."""
    if trust_all is None:
        trust_all = OMEGA_TRUST_USER_SKILLS
    skills, diags = load_execution_skills(roots=roots, config=config or {})
    policed = [
        apply_user_skill_policy(s, trust_approved=trust_approved, trust_all=trust_all)
        for s in skills
    ]
    return policed, diags


def selectable_user_execution_skills(
    roots: Optional[list] = None,
    *,
    trust_approved: Iterable[str] = (),
    trust_all: Optional[bool] = None,
    config: Optional[dict] = None,
) -> list[ExecutionSkill]:
    """User skills that are BOTH trust-approved and schema-selectable. Empty by default."""
    skills, _ = load_user_execution_skills(
        roots, trust_approved=trust_approved, trust_all=trust_all, config=config
    )
    return [s for s in skills if s.selectable]

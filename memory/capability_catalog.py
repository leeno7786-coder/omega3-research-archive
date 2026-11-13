"""Seam 9 — register the local live-chat skills as Omega's APPLIED capabilities.

The local skill catalog ships SKILL.md skills, but the cognition cascade has
never had a representation of them — it could LEARN a domain yet had no inventory of
the capabilities it can APPLY. This module closes that: it reads the executor skill
catalog (``runtime.qwen_skills``) and ingests each enabled skill as a leaf in
lobe 5 (``500_tools``, dev scope) through the existing ``ToolsGate`` pipeline — the
dormant 'guardian writes registrations' slot left vacant when the old tool_registrar
was retired. No new lobe, no new store, no HUD dependency.

Each capability leaf carries a DETERMINISTIC id (``5.01.qwenskill-<slug>``) so a
boot re-sync overwrites in place — the catalog converges to the current enabled set
rather than accumulating duplicates. Failure-soft per skill: a blocked/failed ingest
is recorded, never raised, so boot continues.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from runtime.qwen_skills import QwenSkill

log = logging.getLogger("omega.memory.capability_catalog")

# lobe 5 (500_tools) subdomain Y per ToolsGate._SCOPE_TO_Y: dev->1, cognitive->0.
_SCOPE_TO_Y = {"dev": 1, "cognitive": 0}
_TOOLS_LOBE = 5

# STEP 4: write_strength credit applied to a capability leaf when an APPLY that
# named + used it succeeds (a rewarded choice). Small + bounded; the strength
# index clamps the leaf's write_strength to [0, 1].
CAPABILITY_REINFORCE_DELTA = 0.05

# Phase 6L: debit applied to a capability leaf on a PUNISHED use (e.g. a qwen
# forbidden-tool attempt). Same magnitude as the credit; the index clamps to [0, 1].
CAPABILITY_WEAKEN_DELTA = 0.05


def _normalize_skill_name(name: str) -> str:
    """Standalone copy of runtime.qwen_skills.normalize_skill_name (no runtime import)."""
    return re.sub(
        r"-+",
        "-",
        re.sub(r"[^a-z0-9-_]+", "-", str(name or "").strip().lower()),
    ).strip("-")


def capability_id_for_name(name: str, scope: str = "dev") -> str:
    """Deterministic capability leaf id for a skill NAME — ``{lobe}.{Y:02d}.
    qwenskill-<slug>``. The APPLY records only the skill name in execution_inputs,
    so STEP-4 reinforcement resolves the leaf id from the name; this is the SAME
    id ``sync_capabilities`` wrote (build_memory_dict honors ``metadata['id']``)."""
    y = _SCOPE_TO_Y.get(scope, 1)
    return f"{_TOOLS_LOBE}.{y:02d}.qwenskill-{_normalize_skill_name(name)}"


def capability_id(skill: QwenSkill, scope: str = "dev") -> str:
    """Deterministic leaf id for a QwenSkill (so a re-sync overwrites in place)."""
    return capability_id_for_name(skill.name, scope=scope)


def capability_text(skill: QwenSkill) -> str:
    """The leaf body — a concise statement of the capability (not the full prompt)."""
    desc = (skill.long_description or skill.description or "").strip()
    return f"{skill.name}: {desc}".strip().rstrip(":").strip() if desc else skill.name


def capability_metadata(skill: QwenSkill, scope: str = "dev") -> Dict[str, Any]:
    """Leaf metadata routing the skill into lobe 5 (dev) with capability tags.

    ``registry_scope`` drives ToolsGate's deterministic coord; ``id`` makes the
    write idempotent; ``source='qwen_skill'`` marks the provenance so the catalog
    is identifiable for re-sync / telemetry.
    """
    tags: List[str] = []
    for tag in ["qwen_skill", "capability", *skill.tags]:
        if tag and tag not in tags:
            tags.append(tag)
    return {
        "id": capability_id(skill, scope=scope),
        "registry_scope": scope,
        "type": "capability",
        "title": skill.name,
        "summary": (skill.description or skill.name)[:240],
        "tags": tags,
        "keywords": list(skill.keywords),
        "command": skill.command or f"skill:{skill.name}",
        "source": "qwen_skill",
        "confidence": 0.9,
    }


def _tokens(*parts: str) -> set[str]:
    out: set[str] = set()
    for part in parts:
        for tok in re.split(r"[-_\s]+", str(part or "").lower()):
            if tok:
                out.add(tok)
    return out


def select_capability_for_domain(
    domain: str,
    *,
    skills: Optional[Dict[str, QwenSkill]] = None,
) -> Optional[QwenSkill]:
    """Pick the bundled capability that best matches a learned-but-unapplied gap
    DOMAIN, so an autonomous APPLY can name the skill it is exercising.

    Deterministic token scoring: each domain token scores +2 if it appears in the
    skill's NAME and +1 if it appears in the skill's tags/keywords. The highest
    scorer (>=1) wins; ties break alphabetically by name. No match → None (the qwen
    executor still auto-selects skills runtime-side, so this only ADDS deliberate
    selection, never blocks). Disabled skills are never selected.
    """
    dom = (domain or "").strip()
    if not dom:
        return None
    if skills is None:
        from runtime.qwen_skills import BUNDLED_SKILL_DIRS, load_skills

        skills = load_skills(roots=list(BUNDLED_SKILL_DIRS))

    domain_tokens = _tokens(dom)
    best: Optional[QwenSkill] = None
    best_score = 0
    for name in sorted(skills):  # alphabetical → stable tie-break
        skill = skills[name]
        if not getattr(skill, "enabled", True):
            continue
        name_tokens = _tokens(skill.name)
        tag_tokens = _tokens(*skill.tags, *skill.keywords)
        score = 0
        for tok in domain_tokens:
            if tok in name_tokens:
                score += 2
            elif tok in tag_tokens:
                score += 1
        if score > best_score:
            best_score = score
            best = skill
    return best if best_score >= 1 else None


def reinforce_capability(
    skill_name: str,
    *,
    scope: str = "dev",
    delta: float = CAPABILITY_REINFORCE_DELTA,
    domain: str = "",
    index: Any = None,
) -> Optional[float]:
    """STEP 4 — credit a capability after a successful APPLY that USED it.

    A capability the executor successfully APPLIED is a rewarded choice: bump its
    leaf write_strength so the engram that drove the application gains durability
    and a usage track-record. Uses the strength index's RPE ``reinforce``, which
    is a NO-OP for an untracked/absent leaf — so an unsynced capability is
    harmless. Failure-soft: never raises into the cascade. Returns the new
    write_strength, or None if nothing was reinforced.
    """
    name = (skill_name or "").strip()
    if not name:
        return None
    try:
        if index is None:
            from memory.strength_index import get_strength_index

            index = get_strength_index()
        if index is None:
            return None
        mid = capability_id_for_name(name, scope=scope)
        new_ws = index.reinforce(mid, float(delta))
    except Exception as exc:  # strength index unavailable / malformed id
        log.debug("capability reinforce failed for %s: %s", name, exc)
        return None
    if new_ws is not None:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "capability_reinforced",
                summary=(
                    f"capability '{name}' reinforced +{delta:.3f} -> {new_ws:.3f}"
                    + (f" (domain={domain})" if domain else "")
                ),
                skill=name,
                domain=domain,
                write_strength=new_ws,
                delta=delta,
                ok=True,
            )
        except Exception:
            pass
    return new_ws


def weaken_capability(
    skill_name: str,
    *,
    scope: str = "dev",
    delta: float = CAPABILITY_WEAKEN_DELTA,
    domain: str = "",
    reason: str = "",
    index: Any = None,
) -> Optional[float]:
    """Phase 6L (mirror of reinforce_capability) — debit a capability after a PUNISHED use,
    e.g. a qwen forbidden-tool attempt. Applies a NEGATIVE RPE delta via the strength index
    ``reinforce`` (a no-op for an untracked/absent leaf, so it is harmless). Failure-soft:
    never raises into the cascade. Returns the new write_strength, or None.

    NOTE: weakening targets the SKILL's capability strength. A malformed/bridge failure must
    NOT call this (that is bridge reliability, not the skill) — see skill_consolidation.
    """
    name = (skill_name or "").strip()
    if not name:
        return None
    try:
        if index is None:
            from memory.strength_index import get_strength_index

            index = get_strength_index()
        if index is None:
            return None
        mid = capability_id_for_name(name, scope=scope)
        new_ws = index.reinforce(mid, -abs(float(delta)))
    except Exception as exc:  # strength index unavailable / malformed id
        log.debug("capability weaken failed for %s: %s", name, exc)
        return None
    if new_ws is not None:
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "capability_weakened",
                summary=(
                    f"capability '{name}' weakened -{abs(delta):.3f} -> {new_ws:.3f}"
                    + (f" ({reason})" if reason else "")
                ),
                skill=name,
                domain=domain,
                write_strength=new_ws,
                delta=-abs(delta),
                reason=reason,
                ok=True,
            )
        except Exception:
            pass
    return new_ws


def capability_strengths_for_catalog(
    catalog: list,
    *,
    scope: str = "dev",
    index: Any = None,
) -> dict[str, float]:
    """Map ``skill_id`` -> write_strength [0..1] for Meta A skill selection (Phase 6F).

    Failure-soft: returns {} when the strength index is unavailable. Untracked skills
    default to 0.5 via the selection scorer."""
    out: dict[str, float] = {}
    try:
        if index is None:
            from memory.strength_index import get_strength_index

            index = get_strength_index()
        if index is None:
            return out
        for skill in catalog or []:
            sid = str(
                getattr(skill, "skill_id", "")
                or capability_id_for_name(getattr(skill, "name", ""), scope=scope)
            )
            if not sid:
                continue
            mid = (
                sid
                if sid.startswith(f"{_TOOLS_LOBE}.")
                else capability_id_for_name(getattr(skill, "name", ""), scope=scope)
            )
            out[sid] = float(index.get_strength(mid))
    except Exception as exc:
        log.debug("capability strength lookup failed: %s", exc)
    return out


def sync_capabilities(
    *,
    gate: Any = None,
    skills: Optional[Dict[str, QwenSkill]] = None,
    scope: str = "dev",
) -> Dict[str, Any]:
    """Ingest the enabled executor skills into lobe 5 as Omega capabilities.

    Args:
      gate: a ToolsGate-like object with ``ingest(text, metadata) -> path|None``
        (tests inject a fake); defaults to a real ``ToolsGate``.
      skills: ``{name: QwenSkill}`` (tests inject); defaults to ``load_skills()``.
      scope: 'dev' (lobe 5,1) or 'cognitive' (5,0).

    Returns ``{registered, count, skipped_disabled, failed}``. Never raises.
    """
    if skills is None:
        from runtime.qwen_skills import BUNDLED_SKILL_DIRS, load_skills

        # Curated to Omega's SHIPPED executor skills, not the user's global library.
        skills = load_skills(roots=list(BUNDLED_SKILL_DIRS))
    if gate is None:
        from memory.gates.lobe_gates import ToolsGate

        gate = ToolsGate()

    registered: List[str] = []
    skipped: List[str] = []
    failed: List[str] = []
    for name in sorted(skills):
        skill = skills[name]
        if not getattr(skill, "enabled", True):
            skipped.append(name)
            continue
        try:
            path = gate.ingest(capability_text(skill), capability_metadata(skill, scope=scope))
        except Exception as exc:  # a single bad skill must not abort the catalog sync
            log.debug("capability ingest raised for %s: %s", name, exc)
            path = None
        (registered if path else failed).append(name)

    result = {
        "registered": registered,
        "count": len(registered),
        "skipped_disabled": skipped,
        "failed": failed,
    }
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "capabilities_synced",
            summary=f"{len(registered)} qwen skills registered as capabilities (lobe 5,{_SCOPE_TO_Y.get(scope, 1)})",
            registered=len(registered),
            skipped=len(skipped),
            failed=len(failed),
            ok=not failed,
        )
    except Exception:
        pass
    return result

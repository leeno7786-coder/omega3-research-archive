"""Seam 9 — the qwen executor's skills become Omega's APPLIED capabilities.

sync_capabilities ingests the (enabled) executor skill catalog into lobe 5
(500_tools, dev scope) through the existing-but-dormant ToolsGate pipeline, so the
cognition cascade has a first-class inventory of what it can DO. Each skill gets a
DETERMINISTIC id so a boot re-sync overwrites in place (idempotent) rather than
piling up duplicate leaves. Disabled skills are skipped; an ingest failure is
recorded, never raised (boot must stay failure-soft).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.capability_catalog import (  # noqa: E402
    capability_id,
    capability_metadata,
    select_capability_for_domain,
    sync_capabilities,
)
from runtime.qwen_skills import QwenSkill  # noqa: E402


class _FakeGate:
    """Records ingests; returns a stable leaf path unless the skill is in fail."""

    def __init__(self, fail: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail or set()

    def ingest(self, text, metadata=None):
        meta = metadata or {}
        self.calls.append((text, meta))
        return None if meta.get("title") in self.fail else f"/leaf/{meta.get('id')}"


def _skills(*items: QwenSkill) -> dict[str, QwenSkill]:
    return {s.name: s for s in items}


def test_sync_ingests_only_enabled_skills():
    gate = _FakeGate()
    skills = _skills(
        QwenSkill(name="pytest", description="run tests", enabled=True),
        QwenSkill(name="python", description="py expert", enabled=True),
        QwenSkill(name="azure-ai", description="azure", enabled=False),
    )
    out = sync_capabilities(gate=gate, skills=skills)
    assert out["count"] == 2
    assert out["registered"] == ["pytest", "python"]  # sorted enabled
    assert out["skipped_disabled"] == ["azure-ai"]
    assert len(gate.calls) == 2


def test_capabilities_route_to_tools_dev_scope():
    gate = _FakeGate()
    sync_capabilities(gate=gate, skills=_skills(QwenSkill(name="pytest", enabled=True)))
    _text, meta = gate.calls[0]
    assert meta["registry_scope"] == "dev"
    assert meta["type"] == "capability"


def test_capability_id_is_deterministic_and_coord_formatted():
    sk = QwenSkill(name="omega-refactor-surgeon", enabled=True)
    first = capability_id(sk)
    second = capability_id(sk)
    assert first == second  # stable -> idempotent overwrite
    assert first == "5.01.qwenskill-omega-refactor-surgeon"  # {lobe}.{Y:02d}.{slug}


def test_resync_reuses_the_same_ids():
    skills = _skills(QwenSkill(name="pytest", enabled=True), QwenSkill(name="python", enabled=True))
    g1, g2 = _FakeGate(), _FakeGate()
    sync_capabilities(gate=g1, skills=skills)
    sync_capabilities(gate=g2, skills=skills)
    ids1 = sorted(m["id"] for _t, m in g1.calls)
    ids2 = sorted(m["id"] for _t, m in g2.calls)
    assert ids1 == ids2  # a re-sync writes the SAME leaf paths


def test_metadata_carries_tags_command_and_source():
    sk = QwenSkill(
        name="pytest",
        description="run tests",
        command="skill:pytest",
        tags=["testing", "python"],
        enabled=True,
    )
    meta = capability_metadata(sk)
    assert "qwen_skill" in meta["tags"] and "capability" in meta["tags"]
    assert "testing" in meta["tags"]
    assert meta["command"] == "skill:pytest"
    assert meta["source"] == "qwen_skill"
    assert meta["title"] == "pytest"


def test_failed_ingest_is_recorded_not_raised():
    gate = _FakeGate(fail={"python"})
    skills = _skills(QwenSkill(name="pytest", enabled=True), QwenSkill(name="python", enabled=True))
    out = sync_capabilities(gate=gate, skills=skills)
    assert out["registered"] == ["pytest"]
    assert out["failed"] == ["python"]


# --------------------------------------------------------------------------
# select_capability_for_domain — the APPLY-time matcher (application seam)
# --------------------------------------------------------------------------


def test_select_capability_prefers_name_match_over_tag_match():
    skills = _skills(
        QwenSkill(name="python", description="py", tags=["python"], enabled=True),
        QwenSkill(name="pytest", description="tests", tags=["testing", "python"], enabled=True),
    )
    sk = select_capability_for_domain("python_async", skills=skills)
    assert sk is not None and sk.name == "python"  # name hit outweighs pytest's tag hit


def test_select_capability_matches_via_tags():
    skills = _skills(
        QwenSkill(name="omega-refactor-surgeon", tags=["refactoring", "code"], enabled=True),
    )
    sk = select_capability_for_domain("code_refactoring", skills=skills)
    assert sk is not None and sk.name == "omega-refactor-surgeon"


def test_select_capability_returns_none_when_no_match():
    skills = _skills(QwenSkill(name="python", tags=["python"], enabled=True))
    assert select_capability_for_domain("rust_systems", skills=skills) is None


def test_select_capability_skips_disabled():
    skills = _skills(QwenSkill(name="python", tags=["python"], enabled=False))
    assert select_capability_for_domain("python_async", skills=skills) is None


def test_select_capability_empty_domain_is_none():
    skills = _skills(QwenSkill(name="python", tags=["python"], enabled=True))
    assert select_capability_for_domain("", skills=skills) is None


# --------------------------------------------------------------------------
# STEP 4 — capability fold-back: a successful APPLY reinforces the capability
# leaf it used (rewarded choice -> +write_strength), so capabilities that work
# accrue durability + a usage track-record.
# --------------------------------------------------------------------------


class _FakeIndex:
    """Mirrors StrengthIndex.reinforce: no-op (None) for an untracked mid."""

    def __init__(self, tracked=None):
        self.tracked = dict(tracked or {})
        self.calls: list[tuple[str, float]] = []

    def reinforce(self, mid, delta):
        self.calls.append((mid, delta))
        if mid not in self.tracked:
            return None
        self.tracked[mid] = max(0.0, min(1.0, self.tracked[mid] + delta))
        return self.tracked[mid]

    def get_strength(self, mid):
        return float(self.tracked.get(mid, 0.5))


def test_capability_id_for_name_matches_capability_id():
    from memory.capability_catalog import capability_id_for_name

    sk = QwenSkill(name="omega-refactor-surgeon", enabled=True)
    assert capability_id_for_name("omega-refactor-surgeon") == capability_id(sk)
    assert (
        capability_id_for_name("omega-refactor-surgeon") == "5.01.qwenskill-omega-refactor-surgeon"
    )


def test_reinforce_capability_credits_the_used_capability():
    from memory.capability_catalog import capability_id_for_name, reinforce_capability

    mid = capability_id_for_name("pytest")
    idx = _FakeIndex({mid: 0.5})
    new_ws = reinforce_capability("pytest", delta=0.05, index=idx)
    assert idx.calls == [(mid, 0.05)]
    assert new_ws == 0.55


def test_reinforce_capability_is_noop_for_untracked_skill():
    from memory.capability_catalog import reinforce_capability

    idx = _FakeIndex({})  # capability never synced -> untracked
    assert reinforce_capability("never-synced", index=idx) is None


def test_reinforce_capability_is_failure_soft():
    from memory.capability_catalog import reinforce_capability

    assert reinforce_capability("", index=_FakeIndex()) is None  # blank name -> None

    class _Boom:
        def reinforce(self, *_a):
            raise RuntimeError("index down")

    assert reinforce_capability("pytest", index=_Boom()) is None  # never raises


def test_capability_strengths_for_catalog_reads_write_strength():
    from memory.capability_catalog import capability_id_for_name, capability_strengths_for_catalog
    from schema.execution_skill import ExecutionSkill

    skill = ExecutionSkill(
        name="pytest",
        skill_id=capability_id_for_name("pytest"),
        kind="execution_skill",
        version=1,
        enabled=True,
        description="t",
        skill_class="testing_skill",
        intents=("USE_SKILL",),
        execution_kinds=("TEST",),
        domains=("python",),
        allowed_tools=("run_tests",),
        forbidden_tools=(),
        safety_class="command_safe",
        requires_confirmation_for=(),
        limits={"max_steps": 1, "max_duration_ms": 1, "max_files_changed": 0},
        evidence_required=(),
        completion_criteria=(),
        failure_classes=(),
        output_schema={},
        body_markdown="b",
        source_path="p",
    )
    idx = _FakeIndex({skill.skill_id: 0.72})
    strengths = capability_strengths_for_catalog([skill], index=idx)
    assert strengths[skill.skill_id] == 0.72

"""Live validation — the memory taxonomy learning loop, through the REAL wired path.

Isolated cold root (never touches the real data/memory_taxonomy). Flag ON. Proves the loop
the audit said was the prerequisite for 7N to function live:

  1. a skill episode FAILS -> cognition_chain._capture_episode_to_taxonomy (the real
     episode-close hook) captures it to the ledger AND recompiles the graph
  2. the next episode's Meta-A recall (recall_skill_context) sees that failure as a penalty
  3. select_skill_for_intent deprioritizes the failed skill -> a different, equally-fit skill
     wins that would NOT have won without memory

i.e. the system LEARNED from a failure across episodes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OBJ = "fix the flaky bug"
FAILED_SKILL = "flaky-fixer"
FRESH_SKILL = "steady-fixer"


def _isolate_memory_root(root: Path) -> None:
    """Redirect canonical memory + taxonomy projections into this script's temp root."""
    memory_root = root / "memory"
    taxonomy_root = memory_root / "taxonomy_store"
    index_path = memory_root / "indexes" / "coord_index.json"
    (memory_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text("{}", encoding="utf-8")

    import core.paths as paths
    import memory.engine.reindex as reindex
    import memory.engine.validate_write as validate_write
    import memory.engine.walker as walker
    import memory.encoding_journal as encoding_journal
    import memory.promotion as promotion
    import memory.taxonomy.paths as taxonomy_paths

    paths.OMEGA_ROOT = root
    paths.MEMORY_ROOT = memory_root
    paths.MEMORY_TAXONOMY_ROOT = taxonomy_root
    taxonomy_paths.MEMORY_TAXONOMY_ROOT = taxonomy_root
    validate_write.MEMORY_ROOT = memory_root
    validate_write.TMP_DIR = memory_root / "tmp_writes"
    validate_write.INDEX_PATH = index_path
    walker.MEMORY_ROOT = memory_root
    walker.INDEX_PATH = index_path
    reindex.MEMORY_ROOT = memory_root
    reindex.INDEX_PATH = index_path
    promotion.STAGING_DIR = root / "staging"
    encoding_journal.JOURNAL_PATH = root / "encoding_journal.jsonl"
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0


def _failed_skill_episode():
    """A closed CognitiveEpisode-shaped object whose skill run FAILED."""
    return SimpleNamespace(
        episode_id="ep_fail_1",
        closed_at="2026-06-21T12:00:00Z",
        opened_at="2026-06-21T11:59:00Z",
        pressure={"aggregate": 1.0, "opened_by": "operator_command", "contributors": []},
        global_={
            "intent_decision": {
                "episode_id": "ep_fail_1",
                "intent": "USE_SKILL",
                "confidence": 0.9,
                "reason": OBJ,
            }
        },
        meta={
            "skill_execution_pack": {
                "skill_name": FAILED_SKILL,
                "skill_id": "5.01.qwenskill-flaky-fixer",
                "execution_kind": "REPAIR",
                "dispatch_id": "d1",
                "objective": OBJ,
                "allowed_tools": ["patch_file"],
            }
        },
        auto={
            "status": "failure",
            "action": "USE_SKILL",
            "failure_kind": "skill_failure",
            "outputs": {
                "skill_result": {
                    "episode_id": "ep_fail_1",
                    "dispatch_id": "d1",
                    "skill_name": FAILED_SKILL,
                    "skill_id": "5.01.qwenskill-flaky-fixer",
                    "execution_kind": "REPAIR",
                    "status": "failure",
                    "tool_calls": [{"tool_name": "patch_file"}],
                    "files_changed": [],
                    "commands_run": [],
                    "tests_run": [],
                    "failure_class": "deadlock",
                }
            },
        },
        omega={},
    )


def _mk(name):
    from schema.execution_skill import ExecutionSkill

    return ExecutionSkill(
        name=name,
        skill_id=f"5.01.qwenskill-{name}",
        kind="execution_skill",
        version=1,
        enabled=True,
        description=name,
        skill_class="repair_skill",
        intents=("USE_SKILL",),
        execution_kinds=("REPAIR",),
        domains=("python",),
        allowed_tools=("read_files", "patch_file"),
        forbidden_tools=(),
        safety_class="write_safe",
        requires_confirmation_for=(),
        limits={"max_steps": 20, "max_duration_ms": 600000, "max_files_changed": 10},
        evidence_required=("summary",),
        completion_criteria=("done",),
        failure_classes=("blocked",),
        output_schema={"type": "object"},
        body_markdown="body",
        source_path="syn",
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        # isolate canonical Dewey + taxonomy projection, then flip the flag ON
        # for this process. This validates v2's real canonical->graph path without
        # writing test artifacts into the operator's live memory tree.
        _isolate_memory_root(Path(d))

        import cognition_chain as cc
        from memory.engine.reindex import reindex_and_compile
        from memory.taxonomy.config import taxonomy_enabled
        from memory.taxonomy.meta_recall import recall_skill_context
        from meta_loop.skill.run import select_skill_for_intent

        print("=== flag ===")
        print("  taxonomy_enabled():", taxonomy_enabled())

        catalog = [_mk(FAILED_SKILL), _mk(FRESH_SKILL)]

        print("\n=== BEFORE memory (cold) — who wins the objective? ===")
        cold = select_skill_for_intent(objective=OBJ, catalog=catalog)
        print(f"  selected: {cold.skill_name}  (tie-break: name asc -> {FAILED_SKILL})")

        print("\n=== EPISODE 1 closes: the skill FAILS — real close-hook captures + compiles ===")
        cc._capture_episode_to_taxonomy(_failed_skill_episode())  # the wired path
        reindex_and_compile()
        ctx = recall_skill_context(OBJ)
        print(f"  recalled penalties: {ctx['skill_penalties']}")
        print(f"  recalled boosts   : {ctx['skill_boosts']}")
        print(
            f"  evidence cited    : {ctx['evidence_event_ids'][:3]}{' ...' if len(ctx['evidence_event_ids']) > 3 else ''}"
        )

        print("\n=== EPISODE 2 Meta-A: select WITH recalled memory ===")
        warm = select_skill_for_intent(objective=OBJ, catalog=catalog, recall_context=ctx)
        print(f"  selected: {warm.skill_name}")
        print(f"  reason  : {warm.reason}")

        ok = (
            cold.skill_name == FAILED_SKILL
            and ctx["skill_penalties"].get(FAILED_SKILL, 0.0) > 0.0
            and warm.skill_name == FRESH_SKILL
        )
        print("\n=== VERDICT ===")
        print(
            f"  cold pick was the (soon-to-fail) {FAILED_SKILL:<12}: {cold.skill_name == FAILED_SKILL}"
        )
        print(
            f"  failure captured + recalled as a penalty   : {ctx['skill_penalties'].get(FAILED_SKILL, 0.0) > 0.0}"
        )
        print(
            f"  warm pick avoided it -> {FRESH_SKILL:<12}       : {warm.skill_name == FRESH_SKILL}"
        )
        print(
            "\nRESULT:",
            "PASS - the system learned from the failure across episodes" if ok else "CHECK",
        )
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

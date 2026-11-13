"""Fold-back resume directive — the context-compactor that re-grounds qwen on resume.

When a prior build for THIS objective YIELDED, ``build_directive`` threads its progress digest
into the next dispatch so qwen continues from where it left off (does not redo finished files,
is not lost when the model reloads). Unavailable memory must retain its failure cause;
legitimate absence of prior work produces no resume section.
"""

from __future__ import annotations

import pytest

from schema.qwen_skill_dispatch import QwenSkillExecutionDispatch, build_directive


def _dispatch(objective="build the ai-agent framework"):
    return QwenSkillExecutionDispatch(
        episode_id="e",
        dispatch_id="d",
        skill_name="builder",
        skill_id="i",
        skill_class="execution_skill",
        execution_kind="WRITE",
        objective=objective,
        skill_markdown="# body",
        allowed_tools=("write_file", "restore_project_from_memory"),
        forbidden_tools=(),
        safety={},
        limits={"max_steps": 20},
        evidence_required=(),
        completion_criteria=(),
        expected_output_schema={},
        source_action_goal={},
        source_skill_execution_pack={},
    )


def test_graph_build_in_progress_does_not_create_a_second_resume_lane(monkeypatch):
    """Cold resume is injected from the current SRAM execution report only."""
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_enabled", lambda: True)
    monkeypatch.setattr(
        "memory.taxonomy.meta_recall.recall_skill_context",
        lambda objective, **k: {
            "build_in_progress": [
                {
                    "objective": objective,
                    "progress_digest": "legacy graph carrier",
                    "files_changed": ["legacy.py"],
                }
            ]
        },
    )
    text = build_directive(_dispatch())
    assert "legacy graph carrier" not in text
    assert "legacy.py" not in text


def test_no_resume_section_when_no_build_in_progress(monkeypatch):
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_enabled", lambda: True)
    monkeypatch.setattr(
        "memory.taxonomy.meta_recall.recall_skill_context",
        lambda objective, **k: {"build_in_progress": [], "prior_builds": []},
    )
    assert "Resume" not in build_directive(_dispatch())


def test_completed_project_recall_reaches_directive(monkeypatch):
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_enabled", lambda: True)
    monkeypatch.setattr(
        "memory.taxonomy.meta_recall.recall_skill_context",
        lambda objective, **k: {
            "build_in_progress": [],
            "prior_builds": [
                {
                    "objective": "build the csv_stats project",
                    "summary": "Created a csv_stats CLI with pytest coverage.",
                    "files_changed": ["csv_stats.py", "test_csv_stats.py", "README.md"],
                }
            ],
        },
    )
    text = build_directive(_dispatch("continue the previous completed project"))
    assert "Prior completed project" in text
    assert "csv_stats CLI" in text
    assert "csv_stats.py" in text
    assert "continue or update that completed project" in text
    assert "restore_project_from_memory" in text
    assert "latest verified dev-artifact snapshots" in text.lower()


def _project_record(
    tmp_path, objective, outcome="in_progress", files=("src/user.py", "tests/test_user.py")
):
    """Write a dev-lobe project record for ``objective`` under a tmp projects root."""
    import json

    from memory.dev_project_store import derive_project_name

    project_dir = tmp_path / derive_project_name((), objective)
    project_dir.mkdir(parents=True, exist_ok=True)
    (project_dir / "_omega_project.json").write_text(
        json.dumps(
            {
                "outcome": outcome,
                "file_manifest": [{"path": f, "sha256": "0" * 64} for f in files],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_stale_project_record_never_opens_a_resume_by_itself(monkeypatch, tmp_path):
    # A record from an abandoned build must not resurrect it: with no graph row and no stash,
    # there is no resume section even though the record exists on disk.
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_enabled", lambda: True)
    monkeypatch.setattr(
        "memory.taxonomy.meta_recall.recall_skill_context",
        lambda objective, **k: {"build_in_progress": [], "prior_builds": []},
    )
    monkeypatch.setattr("memory.dev_project_store.OMEGA_ROOT", tmp_path)
    _project_record(
        tmp_path / "memory" / "1100_dev" / "projects", "build the ai-agent framework",
    )
    assert "Resume" not in build_directive(_dispatch())


def test_completed_project_record_contributes_no_ledger(monkeypatch, tmp_path):
    # outcome != in_progress -> the completed-context path's business, not the resume ledger's.
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_enabled", lambda: True)
    monkeypatch.setattr(
        "memory.taxonomy.meta_recall.recall_skill_context",
        lambda objective, **k: {
            "build_in_progress": [
                {
                    "objective": objective,
                    "progress_digest": "one last check.",
                    "files_changed": [],
                    "rounds_used": 3,
                }
            ]
        },
    )
    monkeypatch.setattr("memory.dev_project_store.OMEGA_ROOT", tmp_path)
    _project_record(
        tmp_path / "memory" / "1100_dev" / "projects",
        "build the ai-agent framework", outcome="completed",
    )
    assert "project record — files" not in build_directive(_dispatch())


def test_resume_preserves_failure_when_recall_raises(monkeypatch):
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_enabled", lambda: True)

    def _boom(objective, **k):
        raise RuntimeError("graph missing")

    monkeypatch.setattr("memory.taxonomy.meta_recall.recall_skill_context", _boom)
    with pytest.raises(RuntimeError, match="graph missing"):
        build_directive(_dispatch())


def test_no_resume_section_when_taxonomy_disabled(monkeypatch):
    monkeypatch.setattr("memory.taxonomy.config.taxonomy_enabled", lambda: False)
    assert "Resume" not in build_directive(_dispatch())


def test_directive_states_turn_budget_and_progress_report(monkeypatch):
    # The executor is told its turn budget + to post a progress report before the limit so the
    # fold-back can resume it cleanly (operator 2026-07-01).
    monkeypatch.setenv("QWEN_FOLD_TURNS", "25")
    text = build_directive(_dispatch())
    assert "Turn budget" in text
    assert "25" in text
    assert "progress report" in text.lower()


def test_turn_budget_reads_env_override(monkeypatch):
    monkeypatch.setenv("QWEN_FOLD_TURNS", "20")
    assert "20" in build_directive(_dispatch())


def test_current_brain_budget_and_identity_override_legacy_labels(monkeypatch):
    monkeypatch.setenv("QWEN_FOLD_TURNS", "20")
    monkeypatch.setenv("OMEGA_BRAIN_TOOL_TURNS", "7")

    directive = build_directive(_dispatch())

    assert "about 7 agentic turns" in directive
    assert "current brain model on the existing chat seat" in directive
    assert "compatibility labels, not another model or lane" in directive

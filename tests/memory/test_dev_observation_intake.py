from __future__ import annotations

from pathlib import Path

from auto_loop.execute.brain_skill_executor import ExecutionContext
from auto_loop.mril_tools.remember import invoke_remember_tool
from memory import episode_sram
from memory.api import QuerySpec, recall_verified
from memory.dev_observation_intake import hold_dev_tool_observation


def _ctx(workspace):
    return ExecutionContext(
        workspace_root=workspace,
        trusted_roots=(workspace,),
        objective="Inspect the current directory",
        timeout_s=30,
        episode_id="ep-observe",
        workspace_available=True,
    )


def test_list_directory_observation_holds_exact_tree_and_authority(tmp_path):
    workspace = (tmp_path / "trusted").resolve()
    workspace.mkdir()
    episode_sram.open_bucket("ep-observe", pipeline_id=0)

    receipt = hold_dev_tool_observation(
        episode_id="ep-observe",
        objective="Inspect the current directory",
        record={
            "tool_name": "list_directory",
            "args": {"path": "."},
            "status": "success",
            "output": (
                f"DIRECTORY {workspace}\n"
                "DIR\talpha\nFILE\tnotes.txt\n"
            ),
            "error": "",
            "touched_files": [],
        },
        ctx=_ctx(workspace),
    )

    assert receipt["status"] == "held"
    env = episode_sram.pending("ep-observe")[-1].envelope
    assert env.coord == [11, 5, 0]
    assert env.type == "dev_tool_observation"
    assert env.payload["project_root"] == str(workspace)
    assert env.payload["observed_path"] == str(workspace)
    assert env.payload["tree"] == [
        {"kind": "DIR", "name": "alpha", "relative_path": "alpha"},
        {"kind": "FILE", "name": "notes.txt", "relative_path": "notes.txt"},
    ]
    assert env.payload["drive_root"]
    assert env.payload["verified_tool_evidence"] is True
    assert "alpha" in env.payload["text"]


def test_read_observation_keeps_project_root_and_bounded_strings(tmp_path):
    workspace = (tmp_path / "trusted").resolve()
    project = workspace / "demo"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")
    episode_sram.open_bucket("ep-observe", pipeline_id=0)

    receipt = hold_dev_tool_observation(
        episode_id="ep-observe",
        objective="Read the project configuration",
        record={
            "tool_name": "read_files",
            "args": {"paths": [str(project / "pyproject.toml")]},
            "status": "success",
            "output": f"{project / 'pyproject.toml'} (2 lines)\n     1\t[project]\n     2\tname='demo'",
            "error": "",
            "touched_files": [],
        },
        ctx=_ctx(workspace),
    )

    assert receipt["status"] == "held"
    env = episode_sram.pending("ep-observe")[-1].envelope
    assert env.coord == [11, 6, 0]
    assert env.payload["project"] == "demo"
    assert env.payload["project_root"] == str(project)
    assert any("name='demo'" in line for line in env.payload["strings"])


def test_observation_without_open_episode_is_explicit():
    receipt = hold_dev_tool_observation(
        episode_id="missing",
        objective="Inspect",
        record={"tool_name": "list_directory", "args": {"path": "."}, "status": "success"},
        ctx=type("Ctx", (), {"workspace_root": "C:\\trusted", "trusted_roots": ()})(),
    )
    assert receipt == {"status": "not_held", "tool": "list_directory", "reason": "no_open_sram_bucket"}


def test_observation_without_trusted_workspace_does_not_invent_a_path(tmp_path):
    episode_sram.open_bucket("ep-no-trust", pipeline_id=0)
    ctx = ExecutionContext(
        workspace_root=tmp_path / "sentinel",
        trusted_roots=(),
        objective="Inspect",
        timeout_s=30,
        episode_id="ep-no-trust",
        workspace_available=False,
        workspace_error="no active trusted folder",
    )

    receipt = hold_dev_tool_observation(
        episode_id="ep-no-trust",
        objective="Inspect",
        record={
            "tool_name": "list_directory",
            "args": {"path": "."},
            "status": "failure",
            "error": "list_directory: no active trusted folder",
        },
        ctx=ctx,
    )

    assert receipt["status"] == "not_held"
    assert receipt["reason"] == "no_authorized_path"
    assert episode_sram.pending("ep-no-trust") == []


def test_directory_observation_survives_deepsleep_and_typed_dev_recall(tmp_path):
    workspace = (tmp_path / "trusted").resolve()
    workspace.mkdir()
    episode_sram.open_bucket("ep-observe-recall", pipeline_id=0)

    receipt = hold_dev_tool_observation(
        episode_id="ep-observe-recall",
        objective="Inspect the current directory",
        record={
            "tool_name": "list_directory",
            "args": {"path": "."},
            "status": "success",
            "output": f"DIRECTORY {workspace}\nDIR\talpha\nFILE\tnotes.txt\n",
            "error": "",
            "touched_files": [],
        },
        ctx=_ctx(workspace),
    )
    assert receipt["status"] == "held"
    flushed = episode_sram.flush("ep-observe-recall")
    assert len(flushed["memories"]) == 2  # project identity + full tree

    verdict = recall_verified(
        QuerySpec(
            cycle_id="dev-observe-recall",
            text="what was in the directory?",
            allowed_lobes=[11],
            allowed_subdomains=[5],
            denied_lobes=[],
            limit=10,
        )
    )
    assert verdict.status == "verified", verdict.reason
    candidates = list((verdict.answer_payload or {}).get("candidates") or [])
    assert any("alpha" in str(candidate).lower() for candidate in candidates)

    recalled = invoke_remember_tool(
        {
            "tool": "remember_dev",
            "query": "what was in the directory?",
            "allowed_lobes": [11],
            "allowed_subdomains": [5],
            "memory_type": "episodic",
        },
        "dev-observe-recall-query",
    )
    assert recalled["grounding"] == "VERIFIED_RELEVANT"
    assert any("alpha" in str(item).lower() for item in recalled["evidence"])

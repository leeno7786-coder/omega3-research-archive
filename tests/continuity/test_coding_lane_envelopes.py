"""Coding evidence must distinguish unavailable state from an empty project."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def project(tmp_path):
    from auto_loop.execute import job_control as jobs

    workspace = tmp_path / "workspace"
    root = workspace / "sample-project"
    root.mkdir(parents=True)
    jobs.open_job("coding-envelope-job", "coding-envelope-cycle", "build a sample", workspace,
                  project_root=root)
    return root.resolve()


def _record(*, touched=()):
    return {
        "tool_call_id": "coding-envelope-write",
        "tool_name": "write_file",
        "args": {"path": "sample.py", "content": "value = 7\n"},
        "status": "success",
        "output": "wrote sample.py",
        "error": "",
        "touched_files": list(touched),
        "job_id": "coding-envelope-job",
    }


@pytest.mark.parametrize("boundary", ["record_touched_paths", "get_job"])
def test_agent_receipt_preserves_failed_job_evidence_instead_of_an_empty_task(
    monkeypatch, project, boundary,
):
    from auto_loop.execute import job_control as jobs
    from auto_loop.execute.run import _agent_run_receipt

    fault = OSError(f"coding-envelope-SENTINEL: {boundary} storage unavailable")

    def unavailable(*_args, **_kwargs):
        raise fault

    monkeypatch.setattr(jobs, boundary, unavailable)
    rows = [_record(touched=("sample.py",) if boundary == "record_touched_paths" else ())]
    with pytest.raises(OSError) as caught:
        _agent_run_receipt(rows, operator_route="agent", text="", stop_reason="")
    assert caught.value is fault


def _refuse_scan(monkeypatch, blocked):
    real_scandir = os.scandir
    fault = PermissionError(f"coding-envelope-SENTINEL: cannot enumerate {blocked.name}")

    def unavailable(path):
        if Path(path) == blocked:
            raise fault
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", unavailable)
    return fault


def test_agent_receipt_does_not_present_unreadable_project_as_empty(monkeypatch, project):
    from auto_loop.execute.run import _agent_run_receipt

    (project / "sample.py").write_text("value = 7\n", encoding="utf-8")
    fault = _refuse_scan(monkeypatch, project)
    with pytest.raises(PermissionError) as caught:
        _agent_run_receipt([_record()], operator_route="agent", text="", stop_reason="")
    assert caught.value is fault


def test_command_snapshot_does_not_treat_unreadable_subtree_as_deleted(monkeypatch, project):
    from auto_loop.execute.brain_skill_executor import _workspace_snapshot

    source = project / "src"
    source.mkdir()
    (source / "sample.py").write_text("value = 7\n", encoding="utf-8")
    before = _workspace_snapshot(project)
    assert "src/sample.py" in before
    fault = _refuse_scan(monkeypatch, source)
    with pytest.raises(PermissionError) as caught:
        _workspace_snapshot(project)
    assert caught.value is fault
    assert (source / "sample.py").is_file(), "an unread file was never deleted"


def test_empty_project_is_a_successful_empty_evidence_read(project):
    from auto_loop.execute.brain_skill_executor import _workspace_snapshot
    from auto_loop.execute.run import _agent_run_receipt

    assert _workspace_snapshot(project) == {}
    receipt = _agent_run_receipt([_record()], operator_route="agent", text="", stop_reason="")
    assert receipt["project_root"] == str(project)
    assert receipt["project_files"] == []
    assert receipt["task_touched_files"] == []


def test_coding_write_keeps_claimed_project_identity_through_deepsleep(monkeypatch, tmp_path):
    from memory import episode_commit_view, episode_sram
    from memory.engine import walker
    from schema.episode_record import EpisodeRecord
    from tests.helpers.blueprint_driver import BlueprintDriver, agent_decision, tool_call

    driver = BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)
    source = "VALUE = 'CODING-ENVELOPE-CANONICAL-SENTINEL'\n"
    driver.agent_batches.append({"text": "", "tool_calls": [
        tool_call("start_job", {"project_root": "sample-project"}, "claim"),
        tool_call("task_plan", {"steps": ["write the requested module"]}, "plan"),
        tool_call("write_file", {"path": "sample-project/src/sample.py", "content": source}, "write"),
        tool_call("finish_task", {"summary": "The requested module was written."}, "finished"),
    ]})
    driver.omega.append(agent_decision("DONE", synthesis="The requested module was written."))
    episode = driver.operator_turn(
        "Create the sample module in the sample project.", route="CODING_AGENT",
        cycle_id="coding-envelope-operator-cycle",
    )
    expected_root = (driver.harness.workspace_root / "sample-project").resolve()
    assert (expected_root / "src/sample.py").read_text(encoding="utf-8") == source
    assert driver.store.current() is None
    assert not episode_sram.is_open(episode.episode_id)

    ref = episode_commit_view.episode_record_ref(episode.episode_id)
    assert ref is not None
    walker.load_coord_index()
    document = json.loads(Path(walker.path_for_id(ref.record_id)).read_text(encoding="utf-8"))
    body = json.loads(document["text"])
    record = EpisodeRecord.from_dict(body["episode_record"])
    assert record.outcome.cascade_status == "completed"
    ledger = body["outputs"]["operator_tool_calls"]
    assert all(row["status"] == "success" for row in ledger)
    assert len({row["job_id"] for row in ledger}) == 1
    assert body["outputs"]["agent_run"]["job_id"] == ledger[0]["job_id"]
    assert body["outputs"]["agent_run"]["project_root"] == str(expected_root)

    memories = [
        json.loads(Path(walker.path_for_id(memory_id)).read_text(encoding="utf-8"))
        for memory_id, entry in walker.COORD_INDEX.items()
        if entry.get("type") in {"dev_artifact_snapshot", "dev_tool_observation"}
    ]
    snapshots = [item for item in memories if item.get("type") == "dev_artifact_snapshot"]
    assert any(item["text"] == source for item in snapshots), "the built bytes must reach canonical memory"
    writes = [item for item in memories if (item.get("enrichment") or {}).get("tool") == "write_file"]
    assert writes, "the tool observation must accompany the artifact bytes"
    assert {item["enrichment"]["project_root"] for item in writes} == {str(expected_root)}
    assert {item["enrichment"]["project"] for item in writes} == {"sample-project"}


def _native_dispatch(monkeypatch, tmp_path, *, before_final=None):
    from auto_loop.skill.run import default_brain_skill_invoker
    from core import trusted_folders
    from tests.core.test_brain_skill_executor import _dispatch
    from tests.helpers.blueprint_driver import tool_call

    trusted_folders.grant_trusted_folder(tmp_path)
    trusted_folders.set_active_trusted_folder(tmp_path)
    dispatch = _dispatch(episode_id="coding-native-episode")
    replies = iter([
        {"ok": True, "tool_calls": [tool_call("start_job", {"project_root": "."}, "claim")]},
        {"ok": True, "tool_calls": [tool_call("write_file", {
            "path": "sample.py", "content": "VALUE = 'NATIVE-CODING-SENTINEL'\n",
        }, "write")]},
        {"ok": True, "text": "The requested module was written."},
    ])

    def generate(_prompt, **_kwargs):
        response = next(replies)
        if response.get("text") and before_final is not None:
            before_final()
        return response

    return default_brain_skill_invoker(
        "Write the sample module.", dispatch,
        client=SimpleNamespace(generate_completion=generate), jail_root=str(tmp_path),
        cycle_id="coding-native-cycle", permission_mode="always_allow",
    )


def test_native_skill_holds_the_written_bytes_before_its_next_model_pass(monkeypatch, tmp_path):
    from memory import episode_sram
    from tests.helpers.episode_owner import owned

    seen = []

    def before_final():
        seen.extend(
            item.envelope.payload["content"]
            for item in episode_sram.pending("coding-native-episode")
            if item.kind == "memory" and item.envelope.type == "dev_artifact_snapshot"
        )

    with owned("coding-native-episode"):
        result = _native_dispatch(monkeypatch, tmp_path, before_final=before_final)
        assert result["ok"] is True
        assert seen == ["VALUE = 'NATIVE-CODING-SENTINEL'\n"], (
            "the real tool_result producer must feed artifact intake during the build"
        )


def test_native_skill_does_not_report_success_after_failed_artifact_intake(monkeypatch, tmp_path):
    from tests.helpers.episode_owner import owned

    fault = OSError("coding-envelope-SENTINEL: native artifact intake unavailable")

    def unavailable(**_kwargs):
        raise fault

    monkeypatch.setattr("memory.dev_artifact_intake.hold_dev_artifact_snapshots", unavailable)
    with owned("coding-native-episode"):
        with pytest.raises(OSError) as caught:
            _native_dispatch(monkeypatch, tmp_path)
        assert caught.value is fault


def test_native_history_failure_is_not_zero_recorded_changes(monkeypatch):
    from auto_loop.execute.run import _persist_dev_project_memory

    fault = OSError("coding-envelope-SENTINEL: canonical history unavailable")

    def unavailable(*_args, **_kwargs):
        raise fault

    monkeypatch.setattr("memory.dev_project_store.persist_project_files", unavailable)
    with pytest.raises(OSError) as caught:
        _persist_dev_project_memory(paths=["sample.py"], objective="build the sample",
                                    episode_id="coding-native-episode")
    assert caught.value is fault


def test_trusted_workspace_recall_fact_failure_retains_the_original_cause(monkeypatch):
    from memory.dev_observation_intake import hold_trusted_workspaces
    from tests.helpers.episode_owner import owned

    fault = OSError("coding-envelope-SENTINEL: trusted folder fact source unavailable")

    def unavailable():
        raise fault

    monkeypatch.setattr("memory.dev_observation_intake.trusted_workspace_facts", unavailable)
    with owned("coding-trusted-facts"):
        with pytest.raises(OSError) as caught:
            hold_trusted_workspaces(episode_id="coding-trusted-facts")
        assert caught.value is fault


def test_coding_exchange_does_not_discard_workspace_memory_failure(monkeypatch, tmp_path):
    from auto_loop.execute.run import _operator_tools_reply
    from core import trusted_folders
    from tests.helpers.blueprint_driver import tool_call
    from tests.helpers.episode_owner import owned

    trusted_folders.grant_trusted_folder(tmp_path)
    trusted_folders.set_active_trusted_folder(tmp_path)
    fault = OSError("coding-envelope-SENTINEL: workspace memory intake unavailable")

    def unavailable(**_kwargs):
        raise fault

    monkeypatch.setattr("memory.dev_observation_intake.hold_trusted_workspaces", unavailable)

    def generate(**_kwargs):
        return {"ok": True, "tool_calls": [tool_call("list_directory", {"path": "."}, "list")]}

    with owned("coding-workspace-memory"):
        with pytest.raises(OSError) as caught:
            _operator_tools_reply(
                generate, system="Inspect the sample workspace.", prompt="Inspect it.",
                objective="inspect", episode_id="coding-workspace-memory", max_tokens=100,
                enable_thinking=False, timeout=30, chat_cycle_id="coding-workspace-cycle",
                warnings=[], operator_route="agent", permission_mode="always_allow",
            )
        assert caught.value is fault


@pytest.mark.parametrize("starts_deleted", [False, True])
def test_empty_artifact_and_deleted_artifact_remain_distinct_episode_facts(
    monkeypatch, tmp_path, starts_deleted,
):
    from memory import episode_sram
    from memory.dev_artifact_intake import hold_dev_artifact_snapshots
    from tests.helpers.episode_owner import owned

    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: tmp_path)
    source = tmp_path / "empty.py"
    if not starts_deleted:
        source.write_bytes(b"")
    with owned("coding-empty-artifact"):
        before = hold_dev_artifact_snapshots(
            episode_id="coding-empty-artifact", objective="change the empty module", paths=[source],
        )
        if starts_deleted:
            source.write_bytes(b"")
        else:
            source.unlink()
        after = hold_dev_artifact_snapshots(
            episode_id="coding-empty-artifact", objective="change the empty module", paths=[source],
        )
        assert len(before) == len(after) == 1, "same bytes do not mean same existence state"
        artifacts = [
            item.envelope.payload for item in episode_sram.pending("coding-empty-artifact")
            if item.kind == "memory" and item.envelope.type == "dev_artifact_snapshot"
        ]
        assert [item["deleted"] for item in artifacts] == [starts_deleted, not starts_deleted]
        assert artifacts[0]["content_sha256"] == artifacts[1]["content_sha256"]


def test_artifact_reversion_to_earlier_bytes_is_a_new_latest_snapshot(monkeypatch, tmp_path):
    from memory import episode_sram
    from memory.dev_artifact_intake import hold_dev_artifact_snapshots
    from tests.helpers.episode_owner import owned

    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: tmp_path)
    source = tmp_path / "sample.py"
    with owned("coding-reverted-artifact"):
        for content in (b"VALUE = 1\n", b"VALUE = 2\n", b"VALUE = 1\n"):
            source.write_bytes(content)
            refs = hold_dev_artifact_snapshots(
                episode_id="coding-reverted-artifact", objective="revise the sample", paths=[source],
            )
            assert len(refs) == 1, "returning to older bytes changes the latest state"
        assert hold_dev_artifact_snapshots(
            episode_id="coding-reverted-artifact", objective="revise the sample", paths=[source],
        ) == (), "re-observing the current snapshot is idempotent"
        artifacts = [
            item.envelope.payload for item in episode_sram.pending("coding-reverted-artifact")
            if item.kind == "memory" and item.envelope.type == "dev_artifact_snapshot"
        ]
        assert [item["content"] for item in artifacts] == ["VALUE = 1\n", "VALUE = 2\n", "VALUE = 1\n"]


def _granted_artifact_roots(tmp_path):
    from core import trusted_folders

    roots = [(tmp_path / name).resolve() for name in ("b-first", "c-second")]
    for root in roots:
        root.mkdir()
        trusted_folders.grant_trusted_folder(root)
    trusted_folders.set_active_trusted_folder(roots[0])
    return roots


@pytest.mark.parametrize("same_content", [False, True])
def test_native_skill_keeps_same_named_artifacts_in_distinct_granted_roots(
    monkeypatch, tmp_path, same_content,
):
    from auto_loop.skill.run import default_brain_skill_invoker
    from memory import episode_sram
    from tests.core.test_brain_skill_executor import _dispatch
    from tests.helpers.blueprint_driver import tool_call
    from tests.helpers.episode_owner import owned

    roots = _granted_artifact_roots(tmp_path)
    contents = ["VALUE = 'first'\n", "VALUE = 'first'\n" if same_content else "VALUE = 'second'\n"]
    responses = iter([
        {"ok": True, "tool_calls": [tool_call("start_job", {"project_root": "."}, "claim")]},
        *[
            {"ok": True, "tool_calls": [tool_call("write_file", {
                "path": str(root / "shared.py"), "content": content,
            }, f"write-{index}")]}
            for index, (root, content) in enumerate(zip(roots, contents))
        ],
        {"ok": True, "text": "Wrote the two authorized modules."},
    ])
    with owned("coding-multi-root-native"):
        result = default_brain_skill_invoker(
            "Write both authorized modules.",
            _dispatch(episode_id="coding-multi-root-native", objective="Two authorized modules"),
            client=SimpleNamespace(generate_completion=lambda *_args, **_kwargs: next(responses)),
            jail_root=str(roots[0]), cycle_id="coding-multi-root-cycle",
            permission_mode="always_allow",
        )
        assert result["ok"] is True
        assert [(root / "shared.py").read_text(encoding="utf-8") for root in roots] == contents
        artifacts = [
            item.envelope.payload for item in episode_sram.pending("coding-multi-root-native")
            if item.kind == "memory" and item.envelope.type == "dev_artifact_snapshot"
        ]
        assert len(artifacts) == 2, "one job may touch two granted roots with equal relative paths"
        assert {item["workspace_root"]: item["content"] for item in artifacts} == {
            str(root): content for root, content in zip(roots, contents)
        }


def _capture_workspace_pair(episode_id, objective, roots, contents):
    from memory.dev_artifact_intake import hold_dev_artifact_snapshots

    for root, content in zip(roots, contents):
        (root / "shared.py").write_bytes(content.encode("utf-8"))
    hold_dev_artifact_snapshots(
        episode_id=episode_id, objective=objective,
        paths=[root / "shared.py" for root in roots],
    )


def test_artifact_history_pairs_each_granted_root_with_its_own_prior_bytes(
    tmp_path, deepsleep_sram_drain,
):
    from memory import episode_sram
    from memory.dev_project_store import persist_project_files
    from tests.helpers.episode_owner import owned

    roots = _granted_artifact_roots(tmp_path)
    objective = "Maintain both authorized modules"
    old = ["VALUE = 'first-old'\n", "VALUE = 'second-old'\n"]
    new = ["VALUE = 'first-new'\n", "VALUE = 'second-new'\n"]
    with owned("coding-multi-root-before"):
        _capture_workspace_pair("coding-multi-root-before", objective, roots, old)
        assert episode_sram.flush("coding-multi-root-before")["memories"]
    with owned("coding-multi-root-after"):
        _capture_workspace_pair("coding-multi-root-after", objective, roots, new)
        changes = persist_project_files(
            [str(root / "shared.py") for root in roots], objective=objective,
            episode_id="coding-multi-root-after",
        )
        assert {item.file_path: (item.before_content, item.after_content) for item in changes} == {
            str(root / "shared.py"): (before, after)
            for root, before, after in zip(roots, old, new)
        }


@pytest.mark.parametrize("legacy_route", [False, True])
def test_canonical_artifact_restore_uses_recorded_granted_roots(
    monkeypatch, tmp_path, deepsleep_sram_drain, legacy_route,
):
    from core import trusted_folders
    from memory import episode_sram
    from memory.dev_project_store import derive_project_name, restore_project_files
    from tests.helpers.episode_owner import owned

    roots = _granted_artifact_roots(tmp_path)
    objective = "Restore both authorized modules"
    contents = ["VALUE = 'first-saved'\n", "VALUE = 'second-saved'\n"]
    with owned("coding-multi-root-restore"):
        _capture_workspace_pair("coding-multi-root-restore", objective, roots, contents)
        assert episode_sram.flush("coding-multi-root-restore")["memories"]
    for root in roots:
        (root / "shared.py").unlink()
    # The current workspace and an old manifest both point somewhere else.
    other = (tmp_path / "a-other").resolve()
    other.mkdir()
    trusted_folders.grant_trusted_folder(other)
    trusted_folders.set_active_trusted_folder(other)
    isolated_omega = tmp_path / "omega"
    monkeypatch.setattr("memory.dev_project_store.OMEGA_ROOT", isolated_omega)
    projects = isolated_omega / "memory" / "1100_dev" / "projects"
    if legacy_route:
        project = projects / derive_project_name((), objective)
        project.mkdir(parents=True)
        (project / "_omega_project.json").write_text(json.dumps({
            "workspace_root": str(other), "file_manifest": [{"file_path": "shared.py"}],
        }), encoding="utf-8")
    restored = restore_project_files(objective)
    assert set(restored) == {("shared.py", str(root / "shared.py")) for root in roots}
    assert [(root / "shared.py").read_text(encoding="utf-8") for root in roots] == contents
    assert not (other / "shared.py").exists(), "a new grant cannot change historical identity"


@pytest.mark.parametrize("workspace_root", [None, "relative-workspace"])
def test_canonical_artifact_restore_rejects_unproven_workspace_identity(
    tmp_path, deepsleep_sram_drain, workspace_root,
):
    from memory import episode_sram
    from memory.dev_project_store import restore_project_files
    from tests.helpers.episode_owner import owned

    roots = _granted_artifact_roots(tmp_path)
    objective = "Reject unproven artifact location"
    with owned("coding-multi-root-unproven"):
        _capture_workspace_pair("coding-multi-root-unproven", objective, roots[:1], ["VALUE = 1\n"])
        artifact = next(
            item.envelope for item in episode_sram.pending("coding-multi-root-unproven")
            if item.kind == "memory" and item.envelope.type == "dev_artifact_snapshot"
        )
        for metadata in (artifact.payload, artifact.payload["enrichment"]):
            metadata.pop("workspace_root", None)
            if workspace_root is not None:
                metadata["workspace_root"] = workspace_root
        assert episode_sram.flush("coding-multi-root-unproven")["memories"]
    source = roots[0] / "shared.py"
    source.unlink()
    with pytest.raises(ValueError, match="workspace_root"):
        restore_project_files(objective)
    assert not source.exists()


def test_canonical_artifact_restore_rechecks_each_recorded_root_grant_before_writing(
    tmp_path, deepsleep_sram_drain,
):
    from core import trusted_folders
    from memory import episode_sram
    from memory.dev_project_store import restore_project_files
    from tests.helpers.episode_owner import owned

    roots = _granted_artifact_roots(tmp_path)
    objective = "Recheck both artifact grants"
    with owned("coding-multi-root-revoked"):
        _capture_workspace_pair("coding-multi-root-revoked", objective, roots, ["FIRST\n", "SECOND\n"])
        assert episode_sram.flush("coding-multi-root-revoked")["memories"]
    for root in roots:
        (root / "shared.py").unlink()
    assert trusted_folders.revoke_trusted_folder(roots[1])
    with pytest.raises(ValueError, match="outside repo or operator workspace"):
        restore_project_files(objective)
    assert all(not (root / "shared.py").exists() for root in roots)


def test_artifact_identity_survives_a_new_parent_folder_grant_in_the_same_episode(tmp_path):
    from core import trusted_folders
    from memory.dev_artifact_intake import hold_dev_artifact_snapshots
    from tests.helpers.episode_owner import owned

    parent = (tmp_path / "parent").resolve()
    child = parent / "child"
    child.mkdir(parents=True)
    trusted_folders.grant_trusted_folder(child)
    source = child / "shared.py"
    source.write_bytes(b"SAME_PHYSICAL_FILE = True\n")
    with owned("coding-nested-root-sram"):
        assert hold_dev_artifact_snapshots(
            episode_id="coding-nested-root-sram", objective="One physical module", paths=[source],
        )
        trusted_folders.grant_trusted_folder(parent)
        trusted_folders.set_active_trusted_folder(parent)
        assert hold_dev_artifact_snapshots(
            episode_id="coding-nested-root-sram", objective="One physical module", paths=[source],
        ) == (), "a broader grant changes authority representation, not artifact state"


def test_artifact_history_and_restore_survive_a_new_parent_folder_grant(
    tmp_path, deepsleep_sram_drain,
):
    from core import trusted_folders
    from memory import episode_sram
    from memory.dev_artifact_intake import hold_dev_artifact_snapshots
    from memory.dev_project_store import persist_project_files, restore_project_files
    from tests.helpers.episode_owner import owned

    parent = (tmp_path / "parent").resolve()
    child = parent / "child"
    child.mkdir(parents=True)
    trusted_folders.grant_trusted_folder(child)
    source = child / "shared.py"
    objective = "One physical module history"
    source.write_bytes(b"VERSION = 'old'\n")
    with owned("coding-nested-root-before"):
        hold_dev_artifact_snapshots(
            episode_id="coding-nested-root-before", objective=objective, paths=[source],
        )
        assert episode_sram.flush("coding-nested-root-before")["memories"]
    trusted_folders.grant_trusted_folder(parent)
    trusted_folders.set_active_trusted_folder(parent)
    source.write_bytes(b"VERSION = 'new'\n")
    with owned("coding-nested-root-after"):
        hold_dev_artifact_snapshots(
            episode_id="coding-nested-root-after", objective=objective, paths=[source],
        )
        changes = persist_project_files(
            [str(source)], objective=objective, episode_id="coding-nested-root-after",
        )
        assert len(changes) == 1
        assert changes[0].before_content == "VERSION = 'old'\n"
        assert changes[0].after_content == "VERSION = 'new'\n"
        assert episode_sram.flush("coding-nested-root-after")["memories"]
    source.unlink()
    restored = restore_project_files(objective)
    assert len(restored) == 1
    assert restored[0][1] == str(source)
    assert source.read_bytes() == b"VERSION = 'new'\n"


def test_artifact_restore_rejects_replaced_leaf_target(tmp_path, deepsleep_sram_drain):
    from core import trusted_folders
    from memory import episode_sram
    from memory.dev_artifact_intake import hold_dev_artifact_snapshots
    from memory.dev_project_store import restore_project_files
    from tests.helpers.episode_owner import owned

    workspace = (tmp_path / "workspace").resolve()
    original_parent = workspace / "saved"
    other_parent = workspace / "other"
    original_parent.mkdir(parents=True)
    other_parent.mkdir()
    trusted_folders.grant_trusted_folder(workspace)
    source = original_parent / "shared.py"
    other = other_parent / "shared.py"
    source.write_bytes(b"RECORDED_CONTENT = True\n")
    other.write_bytes(b"DO_NOT_OVERWRITE = True\n")
    objective = "Restore the original physical artifact"
    with owned("coding-replaced-artifact"):
        hold_dev_artifact_snapshots(
            episode_id="coding-replaced-artifact", objective=objective, paths=[source],
        )
        assert episode_sram.flush("coding-replaced-artifact")["memories"]
    source.unlink()
    if os.name == "nt":
        # Directory junctions exercise the real Windows resolver without
        # requiring the elevated privilege that file symlinks may need.
        original_parent.rmdir()
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(original_parent), str(other_parent)],
            capture_output=True, text=True,
        )
        if result.returncode:
            pytest.skip(f"directory junction unavailable: {result.stderr}")
    else:
        try:
            source.symlink_to(other)
        except OSError as exc:
            pytest.skip(f"file symlink unavailable: {exc}")
    assert source.resolve() == other.resolve(), "the recorded target now resolves to another file"
    with pytest.raises(ValueError, match="physical identity"):
        restore_project_files(objective)
    assert other.read_bytes() == b"DO_NOT_OVERWRITE = True\n"

"""Physical project claims narrow authority without classifying operator text."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
import subprocess
from threading import Barrier

import pytest

from auto_loop.execute import job_control as jobs
from auto_loop.execute import brain_skill_executor as executor
from tests.helpers.fs import link_or_copy


@pytest.fixture(autouse=True)
def _authorized_operator_tool_state(tmp_path, monkeypatch):
    """Keep writer/control tests independent of live chat permission state."""
    from auto_loop.execute import chat_permissions

    monkeypatch.setattr(chat_permissions, "CHAT_PERMISSION_PATH", tmp_path / "permission-mode.json")
    chat_permissions.set_chat_permission_mode("always-allow")


@pytest.mark.parametrize("scope", ["project", "PROJECT", "project/child", "."])
def test_overlapping_claim_fails_with_owner_and_without_publication(tmp_path, scope):
    jobs.open_job("first", "one", "build", tmp_path, project_root="project")
    with pytest.raises(ValueError, match="first.*stop that job"):
        jobs.open_job("second", "two", "build", tmp_path, project_root=scope)
    with pytest.raises(KeyError):
        jobs.get_job("second")


def test_independent_claims_and_atomic_same_scope_race(tmp_path):
    barrier = Barrier(2)
    def claim(cycle):
        barrier.wait(timeout=5)
        try:
            jobs.open_job(cycle, cycle, "build", tmp_path, project_root="project")
            return cycle
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, ("one", "two")))
    assert len([result for result in results if result]) == 1
    jobs.open_job("independent", "three", "build", tmp_path, project_root="other")
    assert len(jobs.active_jobs()) == 2


def test_actual_targets_and_command_cwd_stay_in_selected_project(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    jobs.open_job("job", "owner", "build", tmp_path, project_root=project)
    ctx = executor.ExecutionContext(tmp_path, "build", 10, job_id="job", cycle_id="owner")
    assert executor._write_file({"path": "project/result.txt", "content": "good"}, ctx).ok
    for path in ("other/result.txt", "project/../other/result.txt", str(tmp_path.parent / "escape.txt")):
        assert not executor._write_file({"path": path, "content": "bad"}, ctx).ok
    def forbidden_spawn(*args, **kwargs):
        raise AssertionError("out-of-project cwd must not spawn")
    monkeypatch.setattr(executor.subprocess, "Popen", forbidden_spawn)
    assert not executor._run_command({"command": "echo test", "cwd": str(tmp_path)}, ctx).ok
    assert not (tmp_path / "other").exists()


def test_scope_survives_restart_release_and_completed_revision_conflict(tmp_path):
    original = jobs.open_job("job", "owner", "build", tmp_path, project_root="project")
    jobs.reset_for_tests()
    jobs.reconcile_jobs_at_boot({"job": "owner"})
    assert jobs.get_job("job") == original
    jobs.finish_job("job", status="completed", summary="done")
    later = jobs.open_job("other", "other", "later work", tmp_path, project_root="project")
    assert later.project_root == original.project_root  # a finished job releases its project


def test_unbound_record_requires_genuine_owner_binding_and_preserves_controls(tmp_path):
    original = jobs.open_job("old", "owner", "build", tmp_path)
    row = jobs.request_stop("old", source_cycle_id="revision", arrival_seq=2)
    path = jobs.jobs_root() / "old.json"
    record = json.loads(path.read_text())
    record.pop("project_root")
    path.write_text(json.dumps(record), encoding="utf-8")
    before = path.read_bytes()
    assert jobs.get_job("old").project_root == ""
    assert path.read_bytes() == before  # decoding does not rewrite historical data
    with pytest.raises(ValueError, match="start_job"):
        jobs.owned_project("old", "owner")
    with pytest.raises(ValueError, match="genuine"):
        jobs.bind_project("old", cycle_id="intruder", project_root="project")
    bound = jobs.bind_project("old", cycle_id="owner", project_root="project", attempt_generation=0)
    assert bound.revisions == (row,)
    assert bound.cycle_id == original.cycle_id
    with pytest.raises(ValueError, match="immutable"):
        jobs.bind_project("old", cycle_id="owner", project_root="elsewhere")
    with pytest.raises(ValueError, match="stale"):
        jobs.bind_project("old", cycle_id="owner", project_root="project", attempt_generation=1)


def test_claim_scope_cannot_expand_native_authority(tmp_path):
    ceiling = jobs.JobExecutionCeiling(("read_files",), 3, 9999999999, None)
    job = jobs.open_job("native", "owner", "read only", tmp_path, execution_ceiling=ceiling)
    bound = jobs.bind_project("native", cycle_id="owner", project_root="project")
    assert bound.execution_ceiling == ceiling
    assert jobs.reserve_job_tool("native", "write_file", continuation=True)
    with pytest.raises(ValueError, match="escapes"):
        jobs.bind_project("native", cycle_id="owner", project_root=str(tmp_path.parent))
    assert jobs.get_job("native") == replace(job, project_root=str((tmp_path / "project").resolve()))


def test_directory_junction_alias_and_hardlinked_file_cannot_evade_owner(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    alias = tmp_path / "alias"
    if os.name == "nt":
        made = subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(project)], capture_output=True, text=True)
        assert made.returncode == 0, made.stderr
    else:
        alias.symlink_to(project, target_is_directory=True)
    jobs.open_job("owner", "owner", "build", tmp_path, project_root=project)
    with pytest.raises(ValueError, match="owned by job owner"):
        jobs.open_job("conflict", "conflict", "build", tmp_path, project_root=alias)
    other = tmp_path / "other"
    other.mkdir()
    victim = project / "victim.txt"
    victim.write_text("original")
    link_or_copy(victim, other / "alias.txt")
    jobs.open_job("other", "other", "build", tmp_path, project_root=other)
    ctx = executor.ExecutionContext(tmp_path, "build", 10, job_id="other", cycle_id="other")
    result = executor._write_file({"path": "other/alias.txt", "content": "bad"}, ctx)
    assert not result.ok and "hard-link" in result.error
    assert victim.read_text() == "original"
    result = executor._write_file({"path": "alias/victim.txt", "content": "bad"}, ctx)
    assert not result.ok and "outside selected project" in result.error


def test_restore_validates_all_destinations_before_any_write(tmp_path, monkeypatch):
    from core import trusted_folders
    from memory import dev_project_store

    workspace = tmp_path.resolve()
    trusted_folders.grant_trusted_folder(workspace)

    def snapshot(relative_path, content):
        return {
            "enrichment": {
                "scope": "operator_workspace",
                "workspace_root": str(workspace),
                "relative_path": relative_path,
                "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "deleted": False,
            },
            "_text": content,
        }

    monkeypatch.setattr(dev_project_store, "derive_project_name", lambda *args: "fixture")
    monkeypatch.setattr(
        dev_project_store,
        "_canonical_snapshot_records",
        lambda _project: {
            ("operator_workspace", os.path.normcase(str(workspace / "project/good.txt"))):
                snapshot("project/good.txt", "canonical good"),
            ("operator_workspace", os.path.normcase(str(workspace / "other/bad.txt"))):
                snapshot("other/bad.txt", "canonical bad"),
        },
    )
    monkeypatch.setattr(
        dev_project_store,
        "project_artifact_paths",
        lambda _objective: ["project/good.txt", "other/bad.txt"],
    )
    jobs.open_job("owner", "owner", "restore", tmp_path, project_root="project")
    ctx = executor.ExecutionContext(tmp_path, "restore", 10, job_id="owner", cycle_id="owner")
    result = executor._restore_project({}, ctx)
    assert not result.ok and "outside selected project" in result.error
    assert not (tmp_path / "project/good.txt").exists()


def test_selected_command_cwd_runs_with_existing_permissions(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    jobs.open_job("owner", "owner", "build", tmp_path, project_root=project)
    ctx = executor.ExecutionContext(tmp_path, "build", 10, job_id="owner", cycle_id="owner")
    seen = []
    class Process:
        returncode = 0
        def poll(self):
            return 0
        def communicate(self):
            return "checks passed", ""
    def spawn(argv, **kwargs):
        seen.append(kwargs["cwd"])
        return Process()
    monkeypatch.setattr(executor.subprocess, "Popen", spawn)
    result = executor._run_command({"command": "arbitrary-build-command", "cwd": "project"}, ctx)
    assert result.ok and "checks passed" in result.output
    assert seen == [str(project.resolve())]


def test_long_run_command_reports_elapsed_progress(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    jobs.open_job("progress-job", "progress-cycle", "build", tmp_path, project_root=project)
    progress = []
    ctx = executor.ExecutionContext(
        tmp_path, "build", 20, job_id="progress-job", cycle_id="progress-cycle",
        on_progress=progress.append,
    )

    class Process:
        returncode = 0

        def __init__(self):
            self.polls = 0

        def poll(self):
            self.polls += 1
            return 0 if self.polls > 5 else None

        def communicate(self):
            return "checks passed", ""

    clock = {"value": 0}

    def monotonic():
        clock["value"] += 1
        return clock["value"]

    monkeypatch.setattr(executor.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(executor.time, "monotonic", monotonic)
    monkeypatch.setattr(executor.time, "sleep", lambda _seconds: None)

    result = executor._run_command({"command": "arbitrary-build-command", "cwd": "project"}, ctx)

    assert result.ok
    assert progress == ["still running · 5s elapsed"]


def test_a_granted_workspace_has_no_project_containment(tmp_path):
    r"""Operator rule 2026-09-15: a folder set in the workspace settings panel has no restrictions.

    Live that day the agent claimed E:\todo_app, and then mkdir in E:\ was refused as outside
    the selected project, so the project it had just claimed could never be created.
    """
    project = tmp_path / "todo_app"
    jobs.open_job("granted", "owner", "build", tmp_path, project_root=project)
    ctx = executor.ExecutionContext(tmp_path, "build", 10, job_id="granted", cycle_id="owner",
                                    trusted_roots=(tmp_path,))

    assert executor._write_file({"path": "notes.md", "content": "beside the project"}, ctx).ok
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "beside the project"
    assert ctx.resolve(str(tmp_path / "elsewhere" / "x.txt")) == tmp_path / "elsewhere" / "x.txt"
    with pytest.raises(ValueError, match="outside trusted folders"):
        ctx.resolve(str(tmp_path.parent / "beyond.txt"))


def test_start_job_creates_the_project_folder_it_binds(tmp_path):
    """A bound project that does not exist on disk fails every call that follows it."""
    ctx = executor.ExecutionContext(tmp_path, "build a todo app", 10, cycle_id="owner",
                                    trusted_roots=(tmp_path,))

    result = executor._start_job({"project_root": "todo_app"}, ctx)

    assert result.ok, result.error
    assert (tmp_path / "todo_app").is_dir()
    assert result.meta["project_root"] == str((tmp_path / "todo_app").resolve())

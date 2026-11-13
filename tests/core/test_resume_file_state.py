"""The resume dispatch must hold the build's ACTUAL file contents in context and pass them
back to the executor with its next order — a 4B re-architects blindly when its own code is
out of context (operator 2026-07-22: it wrote a class-based test suite against an app.py it
never implemented, because the resume only ever carried filenames + a digest and forbade
re-reading the files)."""

from __future__ import annotations

import schema.qwen_skill_dispatch as qsd
import pytest
from memory import episode_sram
from memory.execution_report import ExecutionReport, hold_execution_report


def _ws(monkeypatch, root):
    monkeypatch.setattr("core.hud_path_guard._operator_workspace_root", lambda: root)


def test_file_state_injects_actual_contents(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def user_lookup(u):\n    return None\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text(
        "from app import user_lookup\n\n\ndef test_x():\n    assert user_lookup('a') is None\n",
        encoding="utf-8",
    )
    _ws(monkeypatch, tmp_path)
    state = qsd._workspace_file_state(["app.py", "test_app.py"])
    assert "── app.py ──" in state
    assert "def user_lookup" in state
    assert "── test_app.py ──" in state
    assert "from app import user_lookup" in state


def test_file_state_does_not_substitute_a_nested_file_by_basename(tmp_path, monkeypatch):
    d = tmp_path / "build-a-project-in-the-workspace"
    d.mkdir()
    (d / "app.py").write_text("SECRET = 42\n", encoding="utf-8")
    _ws(monkeypatch, tmp_path)
    # A bare name identifies the workspace file, not another project's same-named file.
    state = qsd._workspace_file_state(["app.py"])
    assert "SECRET = 42" not in state
    assert "file missing" in state


def test_file_state_skips_junk_trees(tmp_path, monkeypatch):
    cache = tmp_path / "__pycache__"
    cache.mkdir()
    (cache / "app.py").write_text("STALE = 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("REAL = 1\n", encoding="utf-8")
    _ws(monkeypatch, tmp_path)
    state = qsd._workspace_file_state(["app.py"])
    assert "REAL = 1" in state and "STALE" not in state


def test_file_state_bounds_per_file(tmp_path, monkeypatch):
    (tmp_path / "big.py").write_text("x" * 20000, encoding="utf-8")
    _ws(monkeypatch, tmp_path)
    state = qsd._workspace_file_state(["big.py"], max_per_file=500)
    assert "truncated" in state and len(state) < 2000


def test_file_state_requires_workspace_for_relative_paths(monkeypatch):
    _ws(monkeypatch, None)
    with pytest.raises(ValueError, match="workspace"):
        qsd._workspace_file_state(["app.py"])
    assert qsd._workspace_file_state([]) == ""


def test_resume_context_carries_contents_and_drops_the_forbiddance(tmp_path, monkeypatch):
    (tmp_path / "app.py").write_text("def user_lookup(u):\n    pass\n", encoding="utf-8")
    _ws(monkeypatch, tmp_path)
    episode_sram.open_bucket("ep-resume", pipeline_id=0)
    hold_execution_report(
        ExecutionReport(
            episode_id="ep-resume",
            build_task="build sqli demo",
            pass_gate=("focused exploit test passes",),
            did=("changed app.py",),
            did_not=("exploit test is still red",),
            succeeded=("app imports",),
            failed=("exploit test failed",),
            progress_digest="wrote app.py",
            files_changed=("app.py",),
            original_request="build a sqli demo",
            omega_intent="APPLY",
            omega_reason="Fix the parameter binding and rerun the exploit test.",
        )
    )
    block = qsd._resume_context_for("build sqli demo")
    # the complete report and actual code are in the fresh executor's context
    assert "PASS GATE: focused exploit test passes" in block
    assert "DID: changed app.py" in block
    assert "DID NOT: exploit test is still red" in block
    assert "SUCCEEDED: app imports" in block
    assert "FAILED: exploit test failed" in block
    assert "Omega's reason for re-execution" in block
    assert "Fix the parameter binding" in block
    assert "CURRENT STATE OF YOUR FILES" in block
    assert "def user_lookup" in block
    assert "Work from the CURRENT FILE CONTENTS" in block
    assert "re-read the files listed above" not in block


def test_file_state_never_reads_an_absolute_path_outside_the_trusted_folders(tmp_path, monkeypatch):
    """files_changed is persisted data. A stale or malformed entry naming a host file is
    reported unavailable, never read into the prompt."""
    workspace, outside = tmp_path / "workspace", tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    secret = outside / "secrets.txt"
    secret.write_text("HOST_SECRET_SENTINEL\n", encoding="utf-8")
    _ws(monkeypatch, workspace)

    state = qsd._workspace_file_state([str(secret)])

    assert "HOST_SECRET_SENTINEL" not in state
    assert "outside trusted folders" in state and "file missing" not in state


def test_file_state_refuses_a_relative_escape_from_the_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "escaped.txt").write_text("ESCAPED_SENTINEL\n", encoding="utf-8")
    _ws(monkeypatch, workspace)

    state = qsd._workspace_file_state(["../escaped.txt"])

    assert "ESCAPED_SENTINEL" not in state
    assert "outside trusted folders" in state

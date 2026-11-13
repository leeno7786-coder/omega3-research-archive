from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture()
def policy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from auto_loop.execute import chat_permissions

    path = tmp_path / "guardian" / "state" / "live-chat" / "permission-mode.json"
    monkeypatch.setattr(chat_permissions, "CHAT_PERMISSION_PATH", path)
    return path


def test_chat_permission_modes_round_trip_and_slash_commands(policy_path: Path) -> None:
    from auto_loop.execute.chat_permissions import (
        handle_chat_permission_command,
        load_chat_permission_mode,
    )

    assert not policy_path.exists()
    assert load_chat_permission_mode() == "read_only"
    changed = handle_chat_permission_command("/read-only")
    assert changed is not None
    assert changed["status"]["mode"] == "read_only"
    assert load_chat_permission_mode() == "read_only"

    status = handle_chat_permission_command("/mode")
    assert status is not None
    assert status["changed"] is False
    assert status["status"]["label"] == "read-only"

    changed = handle_chat_permission_command("/mode always-allow")
    assert changed is not None
    assert changed["status"]["mode"] == "always_allow"
    assert policy_path.is_file()


def test_unknown_slash_command_is_left_for_normal_chat(policy_path: Path) -> None:
    from auto_loop.execute.chat_permissions import handle_chat_permission_command

    assert handle_chat_permission_command("/not-a-real-command") is None


@pytest.mark.parametrize("contents", ["{", "[]", '{"mode":"future_mode"}'])
def test_corrupt_permission_state_surfaces_instead_of_escalating(policy_path: Path, contents: str) -> None:
    from auto_loop.execute.chat_permissions import (
        ChatPermissionModeUnreadable,
        load_chat_permission_mode,
    )

    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(contents, encoding="utf-8")

    with pytest.raises(ChatPermissionModeUnreadable):
        load_chat_permission_mode()


def test_operator_chat_permission_command_is_local_and_does_not_enter_queue(policy_path: Path) -> None:
    from pressure_loop.operator_chat import handle_operator_chat

    result = handle_operator_chat("/read-only", cycle_id="permission-command")
    assert result["ok"] is True
    assert result["queued"] is False
    assert result["chat_permission"]["mode"] == "read_only"
    assert "/read-only" not in str(result.get("error") or "")


@pytest.mark.parametrize(
    ("mode", "tool", "args", "blocked"),
    (
        ("read_only", "read_files", {"path": "README.md"}, False),
        ("read_only", "write_file", {"path": "x", "content": "x"}, True),
        ("read_only", "run_command", {"command": "pytest -q"}, True),
        ("allow_edits", "write_file", {"path": "x", "content": "x"}, False),
        ("allow_edits", "restore_project_from_memory", {}, True),
        ("allow_edits", "run_command", {"command": "git status"}, False),
        ("allow_edits", "run_command", {"command": "git commit -am x"}, True),
        ("always_allow", "run_command", {"command": "git commit -am x"}, False),
    ),
)
def test_operator_tool_permission_gate(mode: str, tool: str, args: dict, blocked: bool) -> None:
    from auto_loop.execute.chat_permissions import operator_tool_permission_denial

    assert bool(operator_tool_permission_denial(mode, tool, args)) is blocked


def test_skill_permission_gate_only_applies_to_chat_mode() -> None:
    from auto_loop.execute.chat_permissions import skill_permission_denial

    assert skill_permission_denial("read_only", "read_only") == ""
    assert skill_permission_denial("read_only", "write_safe")
    assert skill_permission_denial("allow_edits", "write_safe") == ""
    assert skill_permission_denial("allow_edits", "command_safe")
    assert skill_permission_denial("always_allow", "destructive") == ""


def test_unknown_skill_safety_class_fails_closed() -> None:
    from auto_loop.execute.chat_permissions import skill_permission_denial

    assert skill_permission_denial("allow_edits", "future_class")


def test_shell_commands_are_always_allow_unless_exactly_read_only_git() -> None:
    from auto_loop.execute.chat_permissions import command_requires_always_allow

    assert not command_requires_always_allow("run_command", {"command": "git status"})
    assert not command_requires_always_allow("run_command", {"command": "git diff --no-ext-diff --"})
    assert command_requires_always_allow("run_command", {"command": "python -c 'pass'"})
    assert not command_requires_always_allow("run_tests", {"command": "pytest -q"})
    assert command_requires_always_allow("run_tests", {"command": "pytest -q; echo bad"})

from __future__ import annotations

from pathlib import Path

import pytest

from core.trusted_folders import (
    active_trusted_folder,
    clear_trusted_folders,
    grant_trusted_folder,
    handle_trusted_folder_command,
    load_trusted_folders,
    resolve_trusted_path,
    revoke_trusted_folder,
    set_active_trusted_folder,
    trusted_active_path,
    trusted_folders_path,
)


def test_grant_persists_and_contains_descendants_but_not_siblings(tmp_path: Path) -> None:
    omega = tmp_path / "omega"
    trusted = tmp_path / "project"
    child = trusted / "src"
    sibling = tmp_path / "other"
    omega.mkdir()
    child.mkdir(parents=True)
    sibling.mkdir()

    record = grant_trusted_folder(trusted, omega_root=omega)

    assert record.path == trusted.resolve()
    assert load_trusted_folders(omega)[0].path == trusted.resolve()
    assert resolve_trusted_path(child / "main.py", omega_root=omega) == (child / "main.py").resolve()
    with pytest.raises(ValueError, match="outside trusted folders"):
        resolve_trusted_path(sibling / "secret.txt", omega_root=omega)


def test_grant_rejects_omega_and_drive_parent(tmp_path: Path) -> None:
    omega = tmp_path / "omega"
    child = omega / "memory"
    omega.mkdir()
    child.mkdir()

    with pytest.raises(ValueError, match="overlap"):
        grant_trusted_folder(child, omega_root=omega)
    with pytest.raises(ValueError, match="overlap"):
        grant_trusted_folder(tmp_path, omega_root=omega)


def test_chat_commands_grant_select_list_and_revoke_without_queue(tmp_path: Path) -> None:
    omega = tmp_path / "omega"
    trusted = tmp_path / "project"
    child = trusted / "src"
    omega.mkdir()
    child.mkdir(parents=True)

    added = handle_trusted_folder_command(f'/trust "{trusted}"', omega_root=omega)
    assert added and added["ok"] is True
    assert added["active"] == str(trusted.resolve())
    selected = handle_trusted_folder_command("/cd src", omega_root=omega)
    assert selected and selected["ok"] is True
    assert active_trusted_folder(omega) == child.resolve()
    listed = handle_trusted_folder_command("/trusted", omega_root=omega)
    assert listed and str(trusted.resolve()) in listed["message"]
    removed = handle_trusted_folder_command(f'/untrust "{trusted}"', omega_root=omega)
    assert removed and removed["removed"] is True
    assert active_trusted_folder(omega) is None


def test_reset_clears_grants_and_active_selection(tmp_path: Path) -> None:
    omega = tmp_path / "omega"
    trusted = tmp_path / "project"
    omega.mkdir()
    trusted.mkdir()
    grant_trusted_folder(trusted, omega_root=omega)
    set_active_trusted_folder(trusted, omega_root=omega)
    assert trusted_folders_path(omega).is_file()
    assert trusted_active_path(omega).is_file()

    clear_trusted_folders(omega)

    assert not trusted_folders_path(omega).exists()
    assert not trusted_active_path(omega).exists()
    assert load_trusted_folders(omega) == ()
    assert active_trusted_folder(omega) is None


def test_revoke_only_removes_exact_root(tmp_path: Path) -> None:
    omega = tmp_path / "omega"
    trusted = tmp_path / "project"
    child = trusted / "src"
    omega.mkdir()
    child.mkdir(parents=True)
    grant_trusted_folder(trusted, omega_root=omega)

    assert revoke_trusted_folder(child, omega_root=omega) is False
    assert revoke_trusted_folder(trusted, omega_root=omega) is True
    assert load_trusted_folders(omega) == ()

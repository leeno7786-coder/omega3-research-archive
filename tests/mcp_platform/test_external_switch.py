"""Contracts for the durable external-MCP operator switch."""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.external_switch import (
    ExternalMcpSwitch,
    ExternalMcpSwitchConflict,
    ExternalMcpSwitchUnreadable,
    mcp_eligible,
)
from mcp_platform.package_contracts import NetworkClass
import mcp_platform.external_switch as external_switch_module


def test_missing_switch_is_off_without_writing(tmp_path: Path) -> None:
    """Changing a missing-file default to enabled must fail this test."""
    switch = ExternalMcpSwitch(tmp_path / "external_switch.json")

    state = switch.load()

    assert state.enabled is False
    assert len(state.revision) == 64
    assert not switch.path.exists()
    assert not Path(f"{switch.path}.lock").exists()


def test_corrupt_switch_is_distinct_from_off(tmp_path: Path) -> None:
    """Treating malformed persisted state as the ordinary off default must fail."""
    path = tmp_path / "external_switch.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ExternalMcpSwitchUnreadable, match="^external_mcp_switch_unreadable$"):
        ExternalMcpSwitch(path).load()


@pytest.mark.parametrize(
    "document",
    [
        {"schema_version": True, "enabled": False, "revision": "0" * 64},
        {"schema_version": 1, "enabled": False, "revision": "0" * 64, "extra": 1},
        {"schema_version": 1, "enabled": False},
        {"schema_version": 1, "enabled": 0, "revision": "0" * 64},
        {"schema_version": 1, "enabled": False, "revision": "A" * 64},
    ],
)
def test_invalid_switch_schema_never_reads_as_off(tmp_path: Path, document: dict[str, object]) -> None:
    """Dropping a strict document validation branch must fail this test."""
    path = tmp_path / "external_switch.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ExternalMcpSwitchUnreadable, match="^external_mcp_switch_unreadable$"):
        ExternalMcpSwitch(path).load()


def test_duplicate_json_fields_are_unreadable(tmp_path: Path) -> None:
    """Accepting duplicate JSON keys must fail this test."""
    path = tmp_path / "external_switch.json"
    path.write_text(
        '{"schema_version":1,"enabled":false,"enabled":true,"revision":"' + "0" * 64 + '"}',
        encoding="utf-8",
    )

    with pytest.raises(ExternalMcpSwitchUnreadable, match="^external_mcp_switch_unreadable$"):
        ExternalMcpSwitch(path).load()


def test_revision_must_cover_exact_canonical_switch_state(tmp_path: Path) -> None:
    """Not comparing the stored revision with canonical state bytes must fail."""
    path = tmp_path / "external_switch.json"
    path.write_text(
        json.dumps({"schema_version": 1, "enabled": True, "revision": "0" * 64}),
        encoding="utf-8",
    )

    with pytest.raises(ExternalMcpSwitchUnreadable, match="^external_mcp_switch_unreadable$"):
        ExternalMcpSwitch(path).load()


def test_missing_state_has_the_explicit_off_revision(tmp_path: Path) -> None:
    """Using different revisions for absent and persisted off state must fail."""
    path = tmp_path / "external_switch.json"
    revision = sha256_hex(canonical_bytes({"schema_version": 1, "enabled": False}))
    path.write_bytes(
        canonical_bytes({"schema_version": 1, "enabled": False, "revision": revision})
    )

    assert ExternalMcpSwitch(path).load() == ExternalMcpSwitch(tmp_path / "missing.json").load()


def test_local_only_remains_eligible_when_external_is_off(tmp_path: Path) -> None:
    """Letting the external-off choice block local packages must fail this test."""
    state = ExternalMcpSwitch(tmp_path / "switch.json").load()

    assert mcp_eligible(NetworkClass.LOCAL_ONLY, state) is True
    assert mcp_eligible(NetworkClass.EXTERNAL, state) is False


def test_external_eligibility_follows_the_durable_enabled_bit(tmp_path: Path) -> None:
    """Ignoring the enabled bit for external packages must fail this test."""
    switch = ExternalMcpSwitch(tmp_path / "switch.json")
    off = switch.load()
    on = switch.set_enabled(True, expected_revision=off.revision)

    assert mcp_eligible(NetworkClass.EXTERNAL, on) is True


def test_unknown_network_class_has_no_eligibility_bypass(tmp_path: Path) -> None:
    """Treating an unknown network class as eligible must fail this test."""
    state = ExternalMcpSwitch(tmp_path / "switch.json").load()

    with pytest.raises(ValueError, match="^invalid_mcp_network_class$"):
        mcp_eligible("external" , state)  # type: ignore[arg-type]


def test_stale_revision_cannot_overwrite_operator_choice(tmp_path: Path) -> None:
    """Dropping compare-and-set protection must fail this test."""
    switch = ExternalMcpSwitch(tmp_path / "switch.json")
    off = switch.load()
    on = switch.set_enabled(True, expected_revision=off.revision)

    with pytest.raises(ExternalMcpSwitchConflict, match="^external_mcp_switch_conflict$"):
        switch.set_enabled(False, expected_revision=off.revision)

    assert switch.load() == on


def test_reparse_switch_path_fails_before_reading_outside_file(tmp_path: Path) -> None:
    """Following a state-file symlink must fail this test."""
    outside = tmp_path / "outside.json"
    outside.write_text("outside sentinel", encoding="utf-8")
    path = tmp_path / "external_switch.json"
    try:
        os.symlink(outside, path)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    with pytest.raises(ValueError, match="^external_mcp_switch_path_unsafe$"):
        ExternalMcpSwitch(path).load()

    assert outside.read_text(encoding="utf-8") == "outside sentinel"


def test_reparse_lock_sidecar_fails_before_entering_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Opening an existing lock symlink before rejecting it must fail this test."""
    path = tmp_path / "external_switch.json"
    outside_lock = tmp_path / "outside.lock"
    outside_lock.write_text("outside sentinel", encoding="utf-8")
    lock_path = Path(f"{path}.lock")
    try:
        os.symlink(outside_lock, lock_path)
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")

    def fail_if_lock_entered(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("file_lock entered before sidecar validation")

    monkeypatch.setattr("mcp_platform.external_switch.file_lock", fail_if_lock_entered)

    with pytest.raises(ValueError, match="^external_mcp_switch_path_unsafe$"):
        ExternalMcpSwitch(path).set_enabled(True, expected_revision="0" * 64)

    assert outside_lock.read_text(encoding="utf-8") == "outside sentinel"


def test_set_enabled_keeps_load_compare_state_and_write_inside_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Moving any compare-and-set step outside the lock context must fail this test."""
    path = tmp_path / "external_switch.json"
    off_revision = sha256_hex(canonical_bytes({"schema_version": 1, "enabled": False}))
    path.write_bytes(
        canonical_bytes({"schema_version": 1, "enabled": False, "revision": off_revision})
    )
    lock_active = False
    events: list[str] = []
    real_state = external_switch_module.ExternalMcpState
    real_atomic_write = external_switch_module.atomic_write_bytes

    class InstrumentedLock:
        def __enter__(self) -> None:
            nonlocal lock_active
            assert lock_active is False
            lock_active = True
            events.append("enter")

        def __exit__(self, *_args: object) -> None:
            nonlocal lock_active
            assert lock_active is True
            lock_active = False
            events.append("exit")

    class ObservedRevision(str):
        def __ne__(self, other: object) -> bool:
            assert lock_active is True
            events.append("compare")
            return super().__ne__(other)

    def state_while_locked(enabled: bool, revision: str):
        assert lock_active is True
        events.append("state")
        return real_state(enabled=enabled, revision=revision)

    def write_while_locked(target: Path, payload: bytes) -> Path:
        assert lock_active is True
        events.append("write")
        return real_atomic_write(target, payload)

    monkeypatch.setattr(external_switch_module, "file_lock", lambda _path: InstrumentedLock())
    monkeypatch.setattr(external_switch_module, "ExternalMcpState", state_while_locked)
    monkeypatch.setattr(external_switch_module, "atomic_write_bytes", write_while_locked)

    state = ExternalMcpSwitch(path).set_enabled(
        True,
        expected_revision=ObservedRevision(off_revision),
    )

    assert state.enabled is True
    assert events == ["enter", "state", "compare", "state", "write", "exit"]


def test_set_enabled_revalidates_state_path_after_lock_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Removing the in-lock reparse validation must fail this test."""
    path = tmp_path / "external_switch.json"
    off_revision = sha256_hex(canonical_bytes({"schema_version": 1, "enabled": False}))
    path.write_bytes(
        canonical_bytes({"schema_version": 1, "enabled": False, "revision": off_revision})
    )
    outside = tmp_path / "outside.json"
    outside.write_text("outside sentinel", encoding="utf-8")
    real_file_lock = external_switch_module.file_lock

    @contextmanager
    def swap_state_path_on_entry(target: Path):
        with real_file_lock(target):
            path.unlink()
            try:
                os.symlink(outside, path)
            except OSError as exc:
                pytest.skip(f"file symlinks unavailable: {exc}")
            yield

    monkeypatch.setattr(external_switch_module, "file_lock", swap_state_path_on_entry)

    with pytest.raises(ValueError, match="^external_mcp_switch_path_unsafe$"):
        ExternalMcpSwitch(path).set_enabled(True, expected_revision=off_revision)

    assert outside.read_text(encoding="utf-8") == "outside sentinel"

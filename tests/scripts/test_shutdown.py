"""Unit tests for scripts/shutdown.py — graceful substrate stop via boot.lock."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.shutdown as shutdown  # noqa: E402


@pytest.fixture
def lock_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    p = tmp_path / "boot.lock"
    monkeypatch.setattr(shutdown, "BOOT_LOCK_PATH", p)
    return p


def test_resolve_lock_returns_none_when_missing(lock_path: Path) -> None:
    assert shutdown.resolve_lock() is None


def test_resolve_lock_reads_pid_from_fake_lock(lock_path: Path) -> None:
    lock_path.write_text(
        json.dumps({"pid": 4242, "create_time": 1.0, "acquired_at": "x"}),
        encoding="utf-8",
    )
    rec = shutdown.resolve_lock()
    assert rec is not None
    assert rec["pid"] == 4242


def test_shutdown_nothing_running_when_no_lock(
    lock_path: Path,
) -> None:
    result = shutdown.shutdown_substrate(send_stop=lambda _pid: True)
    assert result.exit_code == 0
    assert result.status == "nothing_running"
    assert "nothing running" in result.message.lower()


def test_shutdown_clears_stale_lock_when_pid_dead(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock_path.write_text(
        json.dumps({"pid": 99999, "create_time": 1.0, "acquired_at": "x"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(shutdown, "_pid_alive", lambda pid, _ct=None: False)
    monkeypatch.setattr(
        shutdown,
        "_verify_boot_owner",
        lambda _pid: (_ for _ in ()).throw(AssertionError("must not verify dead pid")),
    )

    result = shutdown.shutdown_substrate(send_stop=lambda _pid: True)
    assert result.exit_code == 0
    assert result.status == "stale_lock_cleared"
    assert result.lock_cleared is True
    assert "stale" in result.message.lower() or "nothing running" in result.message.lower()
    assert not lock_path.exists()


def test_shutdown_sends_graceful_stop_for_live_boot(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock_path.write_text(
        json.dumps({"pid": 5555, "create_time": 2.0, "acquired_at": "x"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(shutdown, "_pid_alive", lambda pid, _ct=None: pid == 5555)
    monkeypatch.setattr(shutdown, "_verify_boot_owner", lambda pid: pid == 5555)
    stopped: list[int] = []

    result = shutdown.shutdown_substrate(
        send_stop=lambda pid: stopped.append(pid) or True,
    )
    assert result.exit_code == 0
    assert result.status == "stopped"
    assert stopped == [5555]
    assert "5555" in result.message
    assert not lock_path.exists()


def test_shutdown_refuses_unrelated_pid(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    lock_path.write_text(
        json.dumps({"pid": 7777, "create_time": 3.0, "acquired_at": "x"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(shutdown, "_pid_alive", lambda pid, _ct=None: pid == 7777)
    monkeypatch.setattr(shutdown, "_verify_boot_owner", lambda _pid: False)

    result = shutdown.shutdown_substrate(send_stop=lambda _pid: True)
    assert result.exit_code == 1
    assert result.status == "refused"
    assert "refus" in result.message.lower() or "not a boot" in result.message.lower()
    assert ".\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py" in result.message
    assert lock_path.exists()


def test_shutdown_dry_run_does_not_clear_stale_lock(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path.write_text(
        json.dumps({"pid": 99999, "create_time": 1.0, "acquired_at": "x"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(shutdown, "_pid_alive", lambda pid, _ct=None: False)

    result = shutdown.shutdown_substrate(dry_run=True, send_stop=lambda _pid: True)
    assert result.exit_code == 0
    assert result.dry_run is True
    assert result.lock_cleared is False
    assert "would clear stale" in result.message.lower()
    assert lock_path.exists()


def test_shutdown_dry_run_does_not_stop_live_boot(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lock_path.write_text(
        json.dumps({"pid": 5555, "create_time": 2.0, "acquired_at": "x"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(shutdown, "_pid_alive", lambda pid, _ct=None: pid == 5555)
    monkeypatch.setattr(shutdown, "_verify_boot_owner", lambda pid: pid == 5555)

    result = shutdown.shutdown_substrate(
        dry_run=True,
        send_stop=lambda pid: (_ for _ in ()).throw(AssertionError("must not stop")),
    )
    assert result.exit_code == 0
    assert result.dry_run is True
    assert "would stop" in result.message.lower()
    assert lock_path.exists()


def test_shutdown_json_output(
    lock_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    result = shutdown.shutdown_substrate(send_stop=lambda _pid: True)
    shutdown.emit_shutdown_result(result, as_json=True)
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["status"] == "nothing_running"
    assert payload["exit_code"] == 0
    assert payload["dry_run"] is False


def test_main_accepts_json_flag(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Stub the runtime sweep so the unit test never launches a real PowerShell kill.
    monkeypatch.setattr(
        shutdown, "_default_runtime_sweep",
        lambda: {"ok": False, "swept": False, "reason": "stop_script_missing"},
    )
    monkeypatch.setattr(
        shutdown,
        "_default_dev_bridge_sweep",
        lambda: {"ok": True, "swept": False, "reason": "not_running"},
        raising=False,
    )
    rc = shutdown.main(["--json"])
    out = capsys.readouterr().out
    assert rc == 1
    payload = json.loads(out)
    assert payload["status"] == "stop_failed"


def test_main_sweeps_runtime_after_stop(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # Even "nothing running" (a crashed boot leaves no lock) sweeps the runtime,
    # reaping the orphan on :11434 that the ownership path never saw.
    calls: list[int] = []
    monkeypatch.setattr(
        shutdown, "_default_runtime_sweep",
        lambda: (calls.append(1), {"ok": True, "swept": True})[1],
    )
    monkeypatch.setattr(
        shutdown,
        "_default_dev_bridge_sweep",
        lambda: {"ok": True, "swept": False, "reason": "not_running"},
        raising=False,
    )
    rc = shutdown.main([])
    assert rc == 0
    assert calls == [1]


def test_default_runtime_sweep_delegates_to_portable_bundle_sweeper(monkeypatch) -> None:
    from scripts import portable_runtime_shutdown

    calls: list[Path] = []
    monkeypatch.setattr(
        portable_runtime_shutdown,
        "sweep_portable_runtime_processes",
        lambda root: (calls.append(root), {"ok": True, "swept": True})[1],
    )

    assert shutdown._default_runtime_sweep() == {"ok": True, "swept": True}
    assert calls == [shutdown.OMEGA_ROOT]


def test_main_sweeps_dev_bridge_after_boot_process_stops(
    lock_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(
        shutdown,
        "_default_dev_bridge_sweep",
        lambda: (calls.append(1), {"ok": True, "swept": True})[1],
        raising=False,
    )
    monkeypatch.setattr(
        shutdown,
        "_default_runtime_sweep",
        lambda: {"ok": True, "swept": False},
    )

    assert shutdown.main([]) == 0
    capsys.readouterr()
    assert calls == [1]


def test_dev_bridge_sweep_failure_makes_shutdown_fail_honestly() -> None:
    sweep_after_stop = getattr(shutdown, "_sweep_dev_bridge_after_stop", None)
    assert callable(sweep_after_stop)
    result = shutdown.ShutdownResult(
        status="stopped",
        pid=5555,
        lock_cleared=True,
        dry_run=False,
        exit_code=0,
        message="stopped substrate boot pid=5555",
    )

    out = sweep_after_stop(
        result,
        dry_run=False,
        sweep=lambda: {"ok": False, "swept": False, "reason": "bridge_still_running"},
    )

    assert out.status == "stop_failed"
    assert out.exit_code == 1
    assert out.dev_bridge_swept is False


def test_default_dev_bridge_sweep_is_a_noop_after_live_chat_migration() -> None:
    sweep = getattr(shutdown, "_default_dev_bridge_sweep", None)
    assert callable(sweep)
    assert sweep() == {"ok": True, "swept": False, "reason": "not_running"}


def test_dry_run_skips_runtime_sweep() -> None:
    def _boom() -> dict:
        raise AssertionError("must not sweep on --dry-run")

    result = shutdown.ShutdownResult(
        status="stopped", pid=1, lock_cleared=True, dry_run=True,
        exit_code=0, message="would stop",
    )
    out = shutdown._sweep_runtime_after_stop(result, dry_run=True, sweep=_boom)
    assert out.runtime_swept is None
    assert out.model_runtime_note == "Portable runtime teardown is not performed in dry-run."


def test_refused_skips_runtime_sweep() -> None:
    def _boom() -> dict:
        raise AssertionError("must not sweep a refused foreign owner")

    result = shutdown.ShutdownResult(
        status="refused", pid=1, lock_cleared=False, dry_run=False,
        exit_code=1, message="refused",
    )
    out = shutdown._sweep_runtime_after_stop(result, dry_run=False, sweep=_boom)
    assert out.runtime_swept is None


def test_sweep_folds_local_services_stop_flag_and_note() -> None:
    result = shutdown.ShutdownResult(
        status="nothing_running", pid=None, lock_cleared=False, dry_run=False,
        exit_code=0, message="nothing running",
    )
    out = shutdown._sweep_runtime_after_stop(
        result, dry_run=False, sweep=lambda: {"ok": True, "swept": True},
    )
    assert out.runtime_swept is True
    assert out.model_runtime_note == "Portable Omega runtimes and model processes were stopped."

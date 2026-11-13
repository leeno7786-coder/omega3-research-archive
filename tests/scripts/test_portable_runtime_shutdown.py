from __future__ import annotations

import subprocess
from pathlib import Path

from scripts import portable_runtime_shutdown


class _Process:
    def __init__(self, pid: int, cwd: Path, cmdline: list[str], exe: Path) -> None:
        self.info = {
            "pid": pid,
            "ppid": 1,
            "cwd": str(cwd),
            "cmdline": cmdline,
            "exe": str(exe),
        }


def test_sweep_stops_only_portable_process_trees_without_console(tmp_path) -> None:
    product = tmp_path / "Omega3.0"
    windows_runtime = tmp_path / "LM Studio"
    linux_runtime = tmp_path / "LM-Studio-Linux"
    product.mkdir()
    windows_runtime.mkdir()
    linux_runtime.mkdir()
    processes = [
        _Process(
            101,
            product,
            ["python.exe", "-m", "runtime.local_services"],
            product / ".venv-brain" / "Scripts" / "python.exe",
        ),
        _Process(
            202,
            windows_runtime,
            [str(windows_runtime / "LM Studio.exe")],
            windows_runtime / "LM Studio.exe",
        ),
        _Process(303, product, ["python.exe", "-m", "pytest"], Path("C:/Python/python.exe")),
    ]
    calls: list[tuple[list[str], dict[str, object]]] = []

    result = portable_runtime_shutdown.sweep_portable_runtime_processes(
        product,
        process_iter=lambda _attrs: processes,
        run=lambda args, **kwargs: calls.append((args, kwargs)),
        which=lambda _name: None,
    )

    assert result == {"ok": True, "swept": True, "processes": 2, "linux": "not_available"}
    assert [call[0][2] for call in calls] == ["101", "202"]
    for _args, kwargs in calls:
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["check"] is False
        assert kwargs["creationflags"] & portable_runtime_shutdown.CREATE_NO_WINDOW


def test_sweep_kills_each_owned_process_tree_once(tmp_path) -> None:
    product = tmp_path / "Omega3.0"
    product.mkdir()
    processes = [
        _Process(
            101,
            product,
            ["python.exe", "-m", "runtime.local_services"],
            product / ".venv-brain" / "Scripts" / "python.exe",
        ),
        _Process(
            102,
            product,
            ["crispasr.exe", "--server"],
            product / "runtimes" / "bin" / "crispasr.exe",
        ),
    ]
    processes[1].info["ppid"] = 101
    calls: list[list[str]] = []

    result = portable_runtime_shutdown.sweep_portable_runtime_processes(
        product,
        process_iter=lambda _attrs: processes,
        run=lambda args, **_kwargs: calls.append(args),
        which=lambda _name: None,
    )

    assert result["ok"] is True
    assert result["processes"] == 1
    assert calls == [["taskkill.exe", "/PID", "101", "/T", "/F"]]


def test_sweep_stops_transferred_linux_runtime_from_dynamic_ssd_path(tmp_path) -> None:
    product = tmp_path / "Omega3.0"
    linux_runtime = tmp_path / "LM-Studio-Linux"
    product.mkdir()
    linux_runtime.mkdir()
    (linux_runtime / "run-ai.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    calls: list[tuple[list[str], dict[str, object]]] = []

    result = portable_runtime_shutdown.sweep_portable_runtime_processes(
        product,
        process_iter=lambda _attrs: [],
        run=lambda args, **kwargs: calls.append((args, kwargs)),
        which=lambda name: "wsl.exe" if name == "wsl.exe" else None,
    )

    assert result["ok"] is True
    assert result["processes"] == 0
    assert result["linux"] == "stop_requested"
    assert calls
    command, kwargs = calls[0]
    assert command[0:2] == ["wsl.exe", "--cd"]
    assert "LM-Studio-Linux" in command[2]
    assert "lm-studio.AppImage" in command[-1]
    assert "I:" not in " ".join(command)
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["creationflags"] & portable_runtime_shutdown.CREATE_NO_WINDOW

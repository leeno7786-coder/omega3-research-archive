"""Spawn-safe parent-death fixtures for round-one worker regressions."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def discovery_handler(
    body: dict[str, Any],
    _credential: dict[str, Any],
) -> dict[str, Any]:
    """Return an exact synthetic MCP receipt from the spawned worker PID."""

    marker = os.environ.get("OMEGA_TEST_PLUGIN_DISCOVERY_PID_PATH")
    if marker:
        Path(marker).write_text(str(os.getpid()), encoding="utf-8")
    descriptor = dict(body["descriptor"])
    signed = dict(descriptor["signed_capability_authority"])
    return {
        "discovery": {
            "app_id": descriptor["app_id"],
            "publisher_id": descriptor["publisher_id"],
            "deployment": descriptor["deployment"],
            "transport": descriptor["transport"],
            "protocol_version": descriptor["protocol_version"],
            "source": (
                "live_observed"
                if descriptor["deployment"] == "local"
                else "live_authenticated"
            ),
            "server_id": descriptor["app_id"],
            "server_version": "1.0.0",
            "tools": {name: {} for name in signed["tools"]},
            "prompts": {name: {} for name in signed["prompts"]},
            "resources": {name: {} for name in signed["resources"]},
            "resource_templates": {
                name: {} for name in signed["resource_templates"]
            },
            "extensions": {},
            "cache_metadata": {},
            "capability_revision": descriptor["capability_revision"],
            "signed_capability_authority": signed,
        }
    }


def blocked_handler(body: dict[str, Any], _credential: dict[str, Any]) -> dict[str, Any]:
    Path(body["worker_started_path"]).write_text(str(os.getpid()), encoding="utf-8")
    if body.get("provider_started_path") is not None:
        script = (
            "import os,pathlib,time; "
            f"pathlib.Path({body['provider_started_path']!r}).write_text(str(os.getpid()), encoding='utf-8'); "
            f"time.sleep({float(body['effect_delay_seconds'])!r}); "
            f"pathlib.Path({body['effect_path']!r}).write_text('late', encoding='utf-8'); "
            "time.sleep(60)"
        )
        subprocess.Popen(
            [sys.executable, "-c", script],
            start_new_session=os.name != "nt",
        )
    else:
        time.sleep(float(body["effect_delay_seconds"]))
        Path(body["effect_path"]).write_text("late", encoding="utf-8")
    while True:
        time.sleep(1.0)


def detach_and_kill_worker_handler(
    body: dict[str, Any],
    _credential: dict[str, Any],
) -> dict[str, Any]:
    """Launch one contained provider that immediately escapes and kills us."""

    from mcp_platform.plugin_execution_worker import _current_worker_provider_pin

    provider = (
        "import os,pathlib,signal,sys,time; "
        "gate=int(sys.argv[1]); token=os.read(gate,2); os.close(gate); "
        "assert token == b'\\x01'; "
        "parent=os.getppid(); "
        "os.setsid(); "
        f"pathlib.Path({body['provider_started_path']!r}).write_text(str(os.getpid()), encoding='utf-8'); "
        "os.kill(parent, signal.SIGKILL); "
        f"time.sleep({float(body['effect_delay_seconds'])!r}); "
        f"pathlib.Path({body['effect_path']!r}).write_text('late', encoding='utf-8'); "
        "time.sleep(60)"
    )
    gate_read, gate_write = os.pipe()
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", provider, str(gate_read)],
        close_fds=True,
        pass_fds=(gate_read,),
        start_new_session=False,
    )
    os.close(gate_read)
    try:
        _current_worker_provider_pin()(process.pid)
        os.write(gate_write, b"\x01")
    finally:
        os.close(gate_write)
    process.wait()
    raise AssertionError("escaped provider did not terminate its worker")


def double_fork_reparented_handler(
    body: dict[str, Any],
    _credential: dict[str, Any],
) -> dict[str, Any]:
    """Release a pinned provider that double-forks under the worker subreaper."""

    from mcp_platform.plugin_execution_worker import _current_worker_provider_pin

    provider = (
        "import os,pathlib,sys,time; "
        "gate=int(sys.argv[1]); token=os.read(gate,2); os.close(gate); "
        "assert token == b'\\x01'; "
        "first=os.fork(); "
        "\nif first == 0:\n"
        " os.setsid(); second=os.fork()\n"
        " if second == 0:\n"
        f"  pathlib.Path({body['provider_started_path']!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"  time.sleep({float(body['effect_delay_seconds'])!r})\n"
        f"  pathlib.Path({body['effect_path']!r}).write_text('late', encoding='utf-8')\n"
        "  os._exit(0)\n"
        " os._exit(0)\n"
        "os.waitpid(first, 0)"
    )
    gate_read, gate_write = os.pipe()
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", provider, str(gate_read)],
        close_fds=True,
        pass_fds=(gate_read,),
        start_new_session=False,
    )
    os.close(gate_read)
    try:
        _current_worker_provider_pin()(process.pid)
        os.write(gate_write, b"\x01")
    finally:
        os.close(gate_write)
    process.wait(timeout=5.0)
    deadline = time.monotonic() + 5.0
    while not Path(body["provider_started_path"]).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    return {"escaped": True}


def fork_escape_kill_worker_handler(
    body: dict[str, Any],
    _credential: dict[str, Any],
) -> dict[str, Any]:
    """Fork one escaped effect process, kill the worker, and exit immediately."""

    from mcp_platform.plugin_execution_worker import _current_worker_provider_pin

    provider = (
        "import os,pathlib,signal,sys,time; "
        "gate=int(sys.argv[1]); token=os.read(gate,2); os.close(gate); "
        "assert token == b'\\x01'; "
        "worker=os.getppid(); escaped=os.fork(); "
        "\nif escaped == 0:\n"
        " os.setsid()\n"
        f" pathlib.Path({body['provider_started_path']!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f" time.sleep({float(body['effect_delay_seconds'])!r})\n"
        f" pathlib.Path({body['effect_path']!r}).write_text('late', encoding='utf-8')\n"
        " os._exit(0)\n"
        f"marker=pathlib.Path({body['provider_started_path']!r}); deadline=time.monotonic()+5.0\n"
        "while not marker.exists() and time.monotonic() < deadline: time.sleep(0.001)\n"
        "os.kill(worker, signal.SIGKILL); os._exit(0)"
    )
    gate_read, gate_write = os.pipe()
    process = subprocess.Popen(
        [sys.executable, "-I", "-S", "-c", provider, str(gate_read)],
        close_fds=True,
        pass_fds=(gate_read,),
        start_new_session=False,
    )
    os.close(gate_read)
    try:
        _current_worker_provider_pin()(process.pid)
        os.write(gate_write, b"\x01")
    finally:
        os.close(gate_write)
    process.wait()
    raise AssertionError("escaped provider did not terminate its worker")


def kill_linux_guardian_handler(
    body: dict[str, Any],
    _credential: dict[str, Any],
) -> dict[str, Any]:
    """Kill the per-operation guardian and rely on the worker death signal."""

    Path(body["worker_started_path"]).write_text(
        f"{os.getpid()}:{os.getppid()}",
        encoding="utf-8",
    )
    os.kill(os.getppid(), signal.SIGKILL)
    time.sleep(float(body["effect_delay_seconds"]))
    Path(body["effect_path"]).write_text("late", encoding="utf-8")
    return {"late": True}


def run_desktop_parent(
    authority: object,
    body: dict[str, Any],
    *,
    disable_windows_job: bool,
) -> None:
    from mcp_platform import plugin_execution_worker as worker_module

    if disable_windows_job:
        worker_module._WindowsJob.create = staticmethod(lambda: None)
    worker = worker_module.PluginExecutionWorker(
        child_handler=blocked_handler,
        shutdown_timeout_seconds=1.0,
    )
    worker.run_sync(
        authority=authority,
        body=body,
        credential={"access_token": "memory-only-token"},
        timeout_seconds=120.0,
    )

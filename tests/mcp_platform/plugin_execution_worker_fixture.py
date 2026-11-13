"""Spawn-safe hostile handlers for the plugin execution worker tests."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def handle(body: dict[str, Any], credential: dict[str, Any]) -> dict[str, Any]:
    action = body["action"]
    if action == "return":
        return {"value": body["value"]}
    if action == "credential_location":
        token = credential["access_token"]
        return {
            "argv": token in "\0".join(sys.argv),
            "environment": token in "\0".join(
                f"{key}={value}" for key, value in os.environ.items()
            ),
        }
    if action == "late_marker":
        if body.get("started_at_path") is not None:
            Path(body["started_at_path"]).write_text(str(time.monotonic()), encoding="utf-8")
        if body.get("started_path") is not None:
            Path(body["started_path"]).write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(float(body["delay_seconds"]))
        Path(body["marker_path"]).write_text("late", encoding="utf-8")
        return {"late": True}
    if action == "spawn_tree":
        script = (
            "import pathlib,time; "
            f"time.sleep({float(body['delay_seconds'])!r}); "
            f"pathlib.Path({body['marker_path']!r}).write_text('grandchild', encoding='utf-8')"
        )
        child = subprocess.Popen([sys.executable, "-c", script])
        if body.get("started_path") is not None:
            Path(body["started_path"]).write_text(
                f"{os.getpid()}:{child.pid}",
                encoding="utf-8",
            )
        while True:
            time.sleep(1.0)
    if action == "spawn_then_return":
        script = (
            "import pathlib,time; "
            f"time.sleep({float(body['delay_seconds'])!r}); "
            f"pathlib.Path({body['marker_path']!r}).write_text('late', encoding='utf-8')"
        )
        child = subprocess.Popen([sys.executable, "-c", script])
        Path(body["started_path"]).write_text(str(child.pid), encoding="utf-8")
        return {"spawned": True}
    if action == "spawn_attached_tree_and_wait":
        script = (
            "import subprocess,sys; "
            "child=subprocess.Popen([sys.executable,'-c','pass']); "
            "child.wait()"
        )
        child = subprocess.Popen([sys.executable, "-c", script])
        Path(body["started_path"]).write_text(str(child.pid), encoding="utf-8")
        child.wait()
        return {"attached": True}
    if action == "setsid_escape_then_return":
        script = (
            "import os,pathlib,time; "
            "os.setsid(); "
            f"pathlib.Path({body['started_path']!r}).write_text(str(os.getpid()), encoding='utf-8'); "
            f"time.sleep({float(body['delay_seconds'])!r}); "
            f"pathlib.Path({body['marker_path']!r}).write_text('escaped', encoding='utf-8')"
        )
        subprocess.Popen([sys.executable, "-c", script])
        deadline = time.monotonic() + 5.0
        while not Path(body["started_path"]).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"escaped": True}
    if action == "setpgid_breakaway_then_return":
        script = (
            "import os,pathlib,time; "
            "os.setpgid(0,0); "
            f"pathlib.Path({body['started_path']!r}).write_text(str(os.getpid()), encoding='utf-8'); "
            f"time.sleep({float(body['delay_seconds'])!r}); "
            f"pathlib.Path({body['marker_path']!r}).write_text('breakaway', encoding='utf-8')"
        )
        subprocess.Popen([sys.executable, "-c", script])
        deadline = time.monotonic() + 5.0
        while not Path(body["started_path"]).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"breakaway": True}
    if action == "double_fork_then_return":
        first = os.fork()
        if first == 0:
            os.setsid()
            second = os.fork()
            if second != 0:
                os._exit(0)
            Path(body["started_path"]).write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(float(body["delay_seconds"]))
            Path(body["marker_path"]).write_text("daemon", encoding="utf-8")
            os._exit(0)
        os.waitpid(first, 0)
        deadline = time.monotonic() + 5.0
        while not Path(body["started_path"]).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"daemonized": True}
    if action == "inherit_pipes_then_return":
        read_fd, inherited_write_fd = os.pipe()
        try:
            os.set_inheritable(inherited_write_fd, True)
            script = (
                "import os,pathlib,time; "
                f"os.fstat({inherited_write_fd}); "
                f"pathlib.Path({body['started_path']!r}).write_text(str(os.getpid()), encoding='utf-8'); "
                f"time.sleep({float(body['delay_seconds'])!r}); "
                f"pathlib.Path({body['marker_path']!r}).write_text('inherited', encoding='utf-8')"
            )
            subprocess.Popen(
                [sys.executable, "-c", script],
                close_fds=False,
            )
        finally:
            os.close(inherited_write_fd)
            os.close(read_fd)
        deadline = time.monotonic() + 5.0
        while not Path(body["started_path"]).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        return {"inherited": True}
    if action == "attached_tree_timeout":
        script = (
            "import pathlib,time; "
            f"pathlib.Path({body['started_path']!r}).write_text(str(__import__('os').getpid()), encoding='utf-8'); "
            f"time.sleep({float(body['delay_seconds'])!r}); "
            f"pathlib.Path({body['marker_path']!r}).write_text('late', encoding='utf-8')"
        )
        subprocess.Popen([sys.executable, "-c", script])
        while True:
            time.sleep(1.0)
    if action == "crash":
        os._exit(23)
    raise ValueError("unknown_fixture_action")

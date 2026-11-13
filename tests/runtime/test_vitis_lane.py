"""The Vitis lane: a cp312 provider child behind a framed stdio protocol.

``onnxruntime-vitisai`` ships only for CPython 3.12, and a native provider that
does not match the chipset or driver can terminate the process that imports it.
So the NPU scorer lives in its own interpreter (``.venv-vitis`` on
``python-3.12-embed``) as a child of the local-services gateway.  These tests
pin the contract between the two sides: framing, error causes, crash and
timeout attribution, and exact overlap through a real child process.

The subprocess tests run the worker under the brain venv on the CPU provider:
the protocol is interpreter-independent, and correctness is provider-
independent by ORT contract.  Only the last test touches ``.venv-vitis``, and
it proves the lane *imports*, never that the NPU executed anything.
"""

from __future__ import annotations

import base64
import os
import sys
from pathlib import Path

import numpy as np
import pytest

from core.paths import OMEGA_ROOT, portable_python_path, vitis_python_path
from memory.dme import npu_backend
from runtime import vitis_lane, vitis_worker

RNG = np.random.default_rng(7)


def _brain_worker_argv() -> list[str]:
    return [str(portable_python_path()), "-I", "-m", "runtime.vitis_worker"]


def _script_argv(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake_worker.py"
    script.write_text(body, encoding="utf-8")
    return [str(portable_python_path()), "-I", str(script)]


def _client(tmp_path: Path, argv: list[str], **kwargs) -> vitis_lane.VitisWorkerClient:
    return vitis_lane.VitisWorkerClient(
        argv,
        cwd=tmp_path,
        log_path=tmp_path / "vitis_worker.log",
        **kwargs,
    )


def test_frames_round_trip_and_stray_output_is_not_a_frame() -> None:
    frame = vitis_worker.encode_frame({"id": 3, "ok": True, "result": {"n": [1, 2]}})

    assert frame.endswith(b"\n")
    assert vitis_lane.decode_frame(frame) == {"id": 3, "ok": True, "result": {"n": [1, 2]}}
    assert vitis_lane.decode_frame(b"[VitisAI] compiling partition 0\n") is None
    assert vitis_lane.decode_frame(b"\n") is None


def test_bits_round_trip_through_the_wire_encoding() -> None:
    bits = (RNG.random((3, 1000)) < 0.1).astype(np.uint8)

    payload = vitis_lane.encode_bits(bits)

    assert np.array_equal(vitis_worker.decode_bits(payload), bits)


def test_worker_reports_an_unknown_operation_with_its_cause() -> None:
    reply = vitis_worker.Worker().handle({"id": 9, "op": "reticulate"})

    assert reply["id"] == 9
    assert reply["ok"] is False
    assert reply["error"]["type"] == "ValueError"
    assert "reticulate" in reply["error"]["message"]


def test_child_environment_carries_no_host_python_state(tmp_path: Path) -> None:
    host = {
        "PATH": r"C:\host\Python312;C:\host\tools",
        "PYTHONPATH": r"C:\host\site",
        "PYTHONHOME": r"C:\host\Python312",
        "PYTHONSTARTUP": r"C:\host\startup.py",
        "SystemRoot": r"C:\Windows",
        "TEMP": r"C:\Temp",
    }

    env = vitis_lane.worker_environment(host, capi_dir=Path("capi"), state_root=tmp_path)

    assert not any(key.upper().startswith("PYTHON") for key in env)
    assert "host" not in env["PATH"].casefold()
    assert env["PATH"].split(os.pathsep)[0] == "capi"
    assert env["SystemRoot"] == r"C:\Windows"


# Every profile, temp, config and cache root a Windows process resolves from
# its environment.  Host values for any of them would let the child read or
# write the machine owner's profile.
_WRITABLE_KEYS = ("USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData", "TEMP", "TMP")


def test_child_writable_locations_live_under_the_worker_state_root(tmp_path: Path) -> None:
    host = {
        "userprofile": r"C:\Users\hostowner",
        "AppData": r"C:\Users\hostowner\AppData\Roaming",
        "LOCALAPPDATA": r"C:\Users\hostowner\AppData\Local",
        "programdata": r"C:\ProgramData",
        "temp": r"C:\Users\hostowner\AppData\Local\Temp",
        "Tmp": r"C:\Users\hostowner\AppData\Local\Temp",
        "HOME": r"C:\Users\hostowner",
        "HOMEDRIVE": "C:",
        "HOMEPATH": r"\Users\hostowner",
        "USERNAME": "hostowner",
        "SystemRoot": r"C:\Windows",
        "windir": r"C:\Windows",
    }
    state = tmp_path / "worker"

    env = vitis_lane.worker_environment(host, capi_dir=Path("capi"), state_root=state)

    folded = [key.casefold() for key in env]
    assert len(folded) == len(set(folded)), f"duplicate keys differing only in case: {env}"
    assert not any("hostowner" in value.casefold() for value in env.values()), env
    assert r"c:\programdata" not in {value.casefold() for value in env.values()}
    for key in _WRITABLE_KEYS:
        assert Path(env[key]).is_relative_to(state), (key, env[key])
    assert env["TEMP"] == env["TMP"]
    assert Path(env["APPDATA"]).is_relative_to(Path(env["USERPROFILE"]))
    assert Path(env["LOCALAPPDATA"]).is_relative_to(Path(env["USERPROFILE"]))
    assert env == vitis_lane.worker_environment(host, capi_dir=Path("capi"), state_root=state)


def test_start_creates_the_worker_state_directories_the_child_is_given(tmp_path: Path) -> None:
    state = tmp_path / "worker"
    env = vitis_lane.worker_environment(os.environ, capi_dir=tmp_path / "capi", state_root=state)
    argv = _script_argv(
        tmp_path,
        "import json, os, sys, tempfile\n"
        "request = json.loads(sys.stdin.readline())\n"
        f"keys = {list(_WRITABLE_KEYS)!r}\n"
        "result = {'dirs': {k: os.path.isdir(os.environ[k]) for k in keys},\n"
        "          'tempdir': tempfile.gettempdir(),\n"
        "          'home': os.path.expanduser('~')}\n"
        f"sys.stdout.write({vitis_worker.FRAME_PREFIX.decode()!r} + json.dumps("
        "{'id': request['id'], 'ok': True, 'result': result}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.readline()\n",
    )
    assert not state.exists()
    client = vitis_lane.VitisWorkerClient(
        argv, cwd=state, log_path=tmp_path / "vitis_worker.log", env=env,
    )
    try:
        hello = client.start()
    finally:
        client.close()

    assert hello["dirs"] == {key: True for key in _WRITABLE_KEYS}
    assert Path(hello["tempdir"]).resolve() == Path(env["TEMP"]).resolve()
    assert Path(hello["home"]).resolve() == Path(env["USERPROFILE"]).resolve()


def _fake_lane_root(tmp_path: Path) -> Path:
    root = tmp_path / "product"
    interpreter = vitis_python_path(root)
    interpreter.parent.mkdir(parents=True)
    interpreter.write_bytes(b"")
    return root


@pytest.mark.skipif(sys.platform != "win32", reason="the Vitis lane ships for Windows")
def test_default_client_keeps_state_and_log_inside_the_selected_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMEGA_GUARDIAN_LOGS_DIR", raising=False)
    root = _fake_lane_root(tmp_path)

    client = vitis_lane.default_client(root)

    assert client.log_path == root / "guardian" / "logs" / "vitis_worker.log"
    assert client.cwd == root / "guardian" / "state" / "dme" / "vitis_worker"
    assert client.env is not None
    for key in _WRITABLE_KEYS:
        assert Path(client.env[key]).is_relative_to(client.cwd), (key, client.env[key])


@pytest.mark.skipif(sys.platform != "win32", reason="the Vitis lane ships for Windows")
def test_default_client_honors_an_explicit_log_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    logs = tmp_path / "explicit-logs"
    monkeypatch.setenv("OMEGA_GUARDIAN_LOGS_DIR", str(logs))
    root = _fake_lane_root(tmp_path)

    client = vitis_lane.default_client(root)

    assert client.log_path == logs / "vitis_worker.log"


def test_real_child_serves_exact_overlap_from_parent_baked_tiles(tmp_path: Path) -> None:
    k, n = 1000, 96
    plane = (RNG.random((k, n)) < 0.1).astype(np.uint8)
    cache = tmp_path / "tiles"
    manifest = npu_backend.bake_tiles(
        plane, batch=1, shard_columns=48, k_chunk=256, cache_root=cache,
    )
    queries = np.zeros((2, k), dtype=np.uint8)
    queries[0, RNG.choice(k, 40, replace=False)] = 1
    queries[1, RNG.choice(k, 200, replace=False)] = 1

    client = _client(tmp_path, _brain_worker_argv())
    try:
        hello = client.start()
        opened = client.request(
            "open_tiles",
            {"manifest": manifest, "cache_root": str(cache), "verify_npu": False},
        )
        scored = client.request("overlap", {"bits": vitis_lane.encode_bits(queries)})
    finally:
        client.close()

    assert Path(hello["executable"]).resolve() == portable_python_path().resolve()
    assert "CPUExecutionProvider" in hello["providers"]
    assert opened["tiles"] == len(manifest["tiles"])
    assert np.array_equal(
        np.asarray(scored["overlaps"], dtype=np.int64),
        queries.astype(np.int64) @ plane.astype(np.int64),
    )
    assert client.returncode == 0


def test_child_error_reply_preserves_the_cause(tmp_path: Path) -> None:
    cache = tmp_path / "tiles"
    manifest = npu_backend.bake_tiles(
        (RNG.random((512, 48)) < 0.1).astype(np.uint8),
        batch=1, shard_columns=48, cache_root=cache,
    )
    (cache / f"{manifest['tiles'][0]['key']}.onnx").unlink()

    client = _client(tmp_path, _brain_worker_argv())
    try:
        client.start()
        with pytest.raises(vitis_lane.VitisWorkerError) as caught:
            client.request("open_tiles", {"manifest": manifest, "cache_root": str(cache)})
        # A refused request is an answer, not a dead worker.
        assert client.request("ping", {}) == {"pong": True}
    finally:
        client.close()

    assert caught.value.error_type == "FileNotFoundError"
    assert "not baked" in str(caught.value)


def test_child_stdout_noise_does_not_break_the_protocol(tmp_path: Path) -> None:
    argv = _script_argv(
        tmp_path,
        "import json, sys\n"
        "print('[VitisAI] native banner on stdout', flush=True)\n"
        "request = json.loads(sys.stdin.readline())\n"
        "print('more noise', flush=True)\n"
        f"sys.stdout.write({vitis_worker.FRAME_PREFIX.decode()!r} + json.dumps("
        "{'id': request['id'], 'ok': True, 'result': {'executable': sys.executable, "
        "'providers': ['CPUExecutionProvider']}}) + '\\n')\n"
        "sys.stdout.flush()\n"
        "sys.stdin.readline()\n",
    )
    client = _client(tmp_path, argv)
    try:
        hello = client.start()
    finally:
        client.close()

    assert hello["providers"] == ["CPUExecutionProvider"]
    assert "native banner on stdout" in client.stray_output()


def test_a_child_that_dies_is_reported_with_exit_code_and_log_tail(tmp_path: Path) -> None:
    argv = _script_argv(
        tmp_path,
        "import sys\n"
        "sys.stderr.write('ImportError: DLL load failed while importing onnxruntime\\n')\n"
        "sys.exit(3)\n",
    )
    client = _client(tmp_path, argv)

    with pytest.raises(vitis_lane.VitisWorkerExited) as caught:
        client.start()

    assert caught.value.returncode == 3
    assert "DLL load failed" in str(caught.value)
    assert client.alive is False


def test_a_stalled_child_times_out_and_is_killed(tmp_path: Path) -> None:
    argv = _script_argv(tmp_path, "import time\ntime.sleep(60)\n")
    client = _client(tmp_path, argv, start_timeout_s=0.5)

    with pytest.raises(TimeoutError, match="hello"):
        client.start()

    assert client.alive is False


def test_production_argv_runs_the_vitis_interpreter_isolated() -> None:
    argv = vitis_lane.worker_argv(OMEGA_ROOT)

    assert argv == [str(vitis_python_path(OMEGA_ROOT)), "-I", "-m", "runtime.vitis_worker"]


@pytest.mark.skipif(sys.platform != "win32", reason="the Vitis lane ships for Windows")
@pytest.mark.skipif(
    not vitis_python_path().is_file(), reason=".venv-vitis has not been bootstrapped"
)
def test_the_bootstrapped_lane_imports_the_vitis_provider(tmp_path: Path) -> None:
    """Import evidence only.  Registration proves nothing about placement."""
    client = _client(tmp_path, vitis_lane.worker_argv(OMEGA_ROOT))
    try:
        hello = client.start()
    finally:
        client.close()

    assert hello["python"].startswith("3.12.")
    assert Path(hello["executable"]).resolve() == vitis_python_path().resolve()
    # The Ryzen AI build reports its dev stamp: 1.23.2.dev20260117.
    assert hello["onnxruntime"].startswith("1.23.2")
    assert "VitisAIExecutionProvider" in hello["providers"]
    assert client.returncode == 0


def test_encoded_bits_are_base64_text() -> None:
    payload = vitis_lane.encode_bits(np.zeros((1, 16), dtype=np.uint8))

    assert set(payload) == {"rows", "k", "packed"}
    base64.b64decode(payload["packed"], validate=True)

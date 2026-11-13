"""Gateway side of the Vitis lane: owns the cp312 provider child.

The local-services gateway runs on the brain venv (CPython 3.13), where the
VitisAI provider cannot load.  ``VitisWorkerClient`` starts
``runtime.vitis_worker`` on ``.venv-vitis`` and speaks its framed stdio
protocol.  Every way the child can fail -- missing interpreter, import crash,
error reply, stall -- surfaces as a distinct exception that carries its cause;
none of them can be mistaken for an empty or successful result.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Optional

import numpy as np

from core.paths import OMEGA_ROOT, guardian_logs_root, vitis_python_path
from runtime.vitis_worker import FRAME_PREFIX

WORKER_MODULE = "runtime.vitis_worker"
_LOG_TAIL_BYTES = 2000
_EOF = object()

# Windows system identity the provider child needs to load its DLLs and
# describe the machine.  Everything else -- above all PYTHON* and the host
# PATH -- stays behind, so nothing machine-local can reach the lane's
# interpreter or its DLL search.
_CHILD_ENV_KEYS = frozenset(
    key.casefold()
    for key in (
        "SystemRoot", "SystemDrive", "windir", "ComSpec", "PATHEXT", "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE", "PROCESSOR_IDENTIFIER", "OS",
    )
)

# Where the child may write profile, config, cache and temp files: inside the
# worker state root, never the machine owner's profile.  Relative to that root.
_WORKER_STATE_DIRS = {
    "USERPROFILE": Path("home"),
    "APPDATA": Path("home") / "AppData" / "Roaming",
    "LOCALAPPDATA": Path("home") / "AppData" / "Local",
    "ProgramData": Path("programdata"),
    "TEMP": Path("tmp"),
    "TMP": Path("tmp"),
}


class VitisLaneMissing(FileNotFoundError):
    """The lane interpreter is not present in this product tree."""


class VitisWorkerExited(RuntimeError):
    """The child ended; ``returncode`` and the log tail say why."""

    def __init__(self, returncode: Optional[int], detail: str) -> None:
        super().__init__(f"Vitis worker exited with code {returncode}: {detail}".strip())
        self.returncode = returncode


class VitisWorkerError(RuntimeError):
    """The child answered with an error; the remote type and trace survive."""

    def __init__(self, error_type: str, message: str, remote_traceback: str = "") -> None:
        super().__init__(f"{error_type}: {message}")
        self.error_type = error_type
        self.remote_traceback = remote_traceback


def decode_frame(line: bytes) -> Optional[dict[str, Any]]:
    """Return the framed reply on ``line``, or ``None`` for any other output."""
    if not line.startswith(FRAME_PREFIX):
        return None
    message = json.loads(line[len(FRAME_PREFIX):])
    if not isinstance(message, dict):
        raise ValueError("Vitis worker frame is not a JSON object")
    return message


def encode_bits(bits: np.ndarray) -> dict[str, Any]:
    """Pack u8 {0,1} query rows [Q, K] for the wire (8x smaller than JSON ints)."""
    bits = np.ascontiguousarray(bits, dtype=np.uint8)
    if bits.ndim != 2:
        raise ValueError(f"query bits must be 2-D, got {bits.shape}")
    packed = np.packbits(bits, axis=1, bitorder="little")
    return {
        "rows": int(bits.shape[0]),
        "k": int(bits.shape[1]),
        "packed": base64.b64encode(packed.tobytes()).decode("ascii"),
    }


def worker_argv(omega_root: Path = OMEGA_ROOT) -> list[str]:
    """``-I``: no user site, no PYTHON* env; the venv ``._pth`` supplies the root."""
    return [str(vitis_python_path(omega_root)), "-I", "-m", WORKER_MODULE]


def worker_environment(
    base: Mapping[str, str], *, capi_dir: Path, state_root: Path
) -> dict[str, str]:
    """Build the child's environment: host system identity, worker-owned state.

    Profile, config, cache and temp locations point under ``state_root``; the
    client creates them before launch.  Host spellings of those keys, in any
    case, are dropped rather than overridden, so none can shadow ours.
    """
    env = {key: value for key, value in base.items() if key.casefold() in _CHILD_ENV_KEYS}
    for key, relative in _WORKER_STATE_DIRS.items():
        env[key] = str(Path(state_root) / relative)
    system_root = next(
        (value for key, value in env.items() if key.casefold() == "systemroot"), ""
    )
    search = [str(capi_dir)]
    if system_root:
        search += [str(Path(system_root) / "System32"), system_root]
    env["PATH"] = os.pathsep.join(search)
    return env


def default_client(omega_root: Path = OMEGA_ROOT) -> "VitisWorkerClient":
    """The production child for this product tree, or ``VitisLaneMissing``."""
    root = Path(omega_root)
    interpreter = vitis_python_path(root)
    if os.name != "nt":
        raise VitisLaneMissing("the Vitis lane ships for Windows only")
    if not interpreter.is_file():
        raise VitisLaneMissing(f"Vitis lane interpreter is missing: {interpreter}")
    capi = interpreter.parent.parent / "Lib" / "site-packages" / "onnxruntime" / "capi"
    # Derived from the tree being run -- never from a module-level state root,
    # which may point elsewhere -- so a relocated product keeps its own state:
    # ONNX profiles, provider caches and temp files land here.
    work = root / "guardian" / "state" / "dme" / "vitis_worker"
    return VitisWorkerClient(
        worker_argv(root),
        cwd=work,
        log_path=guardian_logs_root(root) / "vitis_worker.log",
        env=worker_environment(os.environ, capi_dir=capi, state_root=work),
    )


class VitisWorkerClient:
    """One resident provider child and its request/response channel.

    Requests are serialized: the gateway serves HTTP from a thread pool, and
    the child answers strictly in order.
    """

    def __init__(
        self,
        argv: list[str],
        *,
        cwd: Path,
        log_path: Path,
        env: Optional[Mapping[str, str]] = None,
        start_timeout_s: float = 120.0,
        request_timeout_s: float = 60.0,
    ) -> None:
        self.argv = [str(item) for item in argv]
        self.cwd = Path(cwd)
        self.log_path = Path(log_path)
        self.env = dict(env) if env is not None else None
        self.start_timeout_s = float(start_timeout_s)
        self.request_timeout_s = float(request_timeout_s)
        self._process: Optional[subprocess.Popen] = None
        self._log: Optional[BinaryIO] = None
        self._log_offset = 0
        self._frames: "queue.Queue[Any]" = queue.Queue()
        self._stray: deque[str] = deque(maxlen=50)
        self._lock = threading.Lock()
        self._next_id = 0

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def returncode(self) -> Optional[int]:
        return None if self._process is None else self._process.poll()

    def stray_output(self) -> str:
        """Unframed stdout the child produced (native banners), for diagnosis."""
        return "\n".join(self._stray)

    def start(self) -> dict[str, Any]:
        """Spawn the child and complete the ``hello`` handshake."""
        if self._process is not None:
            raise RuntimeError("Vitis worker already started")
        self.cwd.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        for key in _WORKER_STATE_DIRS:
            if self.env is not None and key in self.env:
                Path(self.env[key]).mkdir(parents=True, exist_ok=True)
        self._log = self.log_path.open("ab")
        self._log_offset = self._log.tell()
        kwargs: dict[str, Any] = {
            "cwd": str(self.cwd),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": self._log,
        }
        if self.env is not None:
            kwargs["env"] = self.env
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            self._process = subprocess.Popen(self.argv, **kwargs)
            threading.Thread(
                target=self._read_frames, name="vitis-worker-reader", daemon=True
            ).start()
            return self.request("hello", {}, timeout_s=self.start_timeout_s)
        except BaseException:
            self._reap()
            raise

    def request(
        self, op: str, params: Mapping[str, Any], *, timeout_s: Optional[float] = None
    ) -> dict[str, Any]:
        timeout = self.request_timeout_s if timeout_s is None else float(timeout_s)
        with self._lock:
            process = self._process
            if process is None:
                raise RuntimeError("Vitis worker is not started")
            if process.poll() is not None:
                raise VitisWorkerExited(process.returncode, self._log_tail())
            self._next_id += 1
            request_id = self._next_id
            line = json.dumps({"id": request_id, "op": op, "params": dict(params)}) + "\n"
            try:
                assert process.stdin is not None
                process.stdin.write(line.encode("utf-8"))
                process.stdin.flush()
            except OSError as exc:
                raise VitisWorkerExited(self._wait_exit(), self._log_tail()) from exc
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                try:
                    item = self._frames.get(timeout=max(remaining, 0.0))
                except queue.Empty:
                    self._reap(kill=True)
                    raise TimeoutError(
                        f"Vitis worker did not answer {op!r} within {timeout:g}s"
                    ) from None
                if item is _EOF:
                    raise VitisWorkerExited(self._wait_exit(), self._log_tail())
                if item.get("id") != request_id:
                    self._stray.append(f"unexpected reply id: {item.get('id')!r}")
                    continue
                if item.get("ok") is True:
                    return dict(item.get("result") or {})
                error = dict(item.get("error") or {})
                raise VitisWorkerError(
                    str(error.get("type") or "Error"),
                    str(error.get("message") or ""),
                    str(error.get("traceback") or ""),
                )

    def close(self) -> None:
        """Ask the child to release its sessions and exit; reap it regardless."""
        if self._process is None:
            return
        failure: Optional[BaseException] = None
        if self.alive:
            try:
                self.request("close", {}, timeout_s=30.0)
            except VitisWorkerError as exc:
                failure = exc
            except (VitisWorkerExited, TimeoutError):
                pass  # the child is gone either way; reaping below is the release
        self._reap()
        if failure is not None:
            raise failure

    def _read_frames(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    frame = decode_frame(line)
                except ValueError as exc:
                    self._stray.append(f"malformed frame: {exc}")
                    continue
                if frame is None:
                    self._stray.append(line.decode("utf-8", "replace").rstrip())
                else:
                    self._frames.put(frame)
        finally:
            # Closed here, by the only thread that reads it: closing a buffered
            # pipe from another thread blocks on the reader's lock.
            process.stdout.close()
            self._frames.put(_EOF)

    def _wait_exit(self) -> Optional[int]:
        process = self._process
        if process is None:
            return None
        try:
            return process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._reap(kill=True)
            return process.poll()

    def _reap(self, *, kill: bool = False) -> None:
        """End the child (gracefully via stdin EOF unless ``kill``) and release handles."""
        process = self._process
        if process is not None:
            if process.poll() is None:
                if kill:
                    process.kill()
                    process.wait(timeout=10)
                else:
                    try:
                        if process.stdin is not None:
                            process.stdin.close()
                        process.wait(timeout=10)
                    except (OSError, subprocess.TimeoutExpired):
                        process.kill()
                        process.wait(timeout=10)
            if process.stdin is not None and not process.stdin.closed:
                try:
                    process.stdin.close()
                except OSError:
                    pass  # the pipe is already broken; the handle is released
        if self._log is not None and not self._log.closed:
            self._log.close()

    def _log_tail(self) -> str:
        try:
            if self._log is not None and not self._log.closed:
                self._log.flush()
            with self.log_path.open("rb") as handle:
                handle.seek(self._log_offset)
                data = handle.read()
        except OSError as exc:
            return f"(worker log unreadable: {exc})"
        return data[-_LOG_TAIL_BYTES:].decode("utf-8", "replace").strip()

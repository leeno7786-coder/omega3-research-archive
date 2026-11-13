"""Qwen3-TTS GGML sidecar for the portable speech lane.

The Qwen implementation is deliberately a child process, not an in-process
Python/ONNX model. ``qwentts.cpp`` owns the GGML context and exposes an
OpenAI-compatible HTTP surface. Local mind seats select CPU; cloud mind seats
select the configured Vulkan GPU. The local-services gateway warms the voice
at boot and retains the child until shutdown.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Any, AsyncIterator, Callable, Mapping, Optional

import httpx

from core.paths import OMEGA_ROOT, RUNTIME_BIN_ROOT, RUNTIME_MODELS_ROOT, guardian_logs_root

log = logging.getLogger("omega.runtime.qwen_tts")

_DEFAULT_MODEL = RUNTIME_MODELS_ROOT / "qwen3-tts" / "qwen-talker-0.6b-customvoice-Q8_0.gguf"
_DEFAULT_CODEC = RUNTIME_MODELS_ROOT / "qwen3-tts" / "qwen-tokenizer-12hz-Q8_0.gguf"
_DEFAULT_BINARY = RUNTIME_BIN_ROOT / "qwentts" / ("tts-server.exe" if os.name == "nt" else "tts-server")
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 35802
_DEFAULT_START_TIMEOUT_S = 120.0


#: qwentts.cpp announces the backend it actually loaded on, as its fourth line:
#:     [Load] Talker backend: Vulkan0 (CPU threads: 8)
_BACKEND_BANNER = re.compile(r"^\[Load\] Talker backend:\s*(\S+)", re.M)


class BackendNotHonoured(RuntimeError):
    """The backend Omega asked for is not the backend the worker loaded on.

    The binary has no ``--n-gpu-layers`` flag (``tts-server.exe --help``): placement follows the
    ggml backend it selects, which Omega selects through ``GGML_BACKEND``. If the Vulkan device is
    absent, its driver fails or its DLL is missing, the worker loads on CPU and reports itself
    perfectly healthy — a voice an order of magnitude slower with nothing saying the request was
    refused. Silent degradation is not a mode this substrate has (AGENTS.md section 2).
    """


def loaded_backend(log_path: Path) -> Optional[str]:
    """The backend the worker's own banner reports, or ``None`` when it never said.

    ``None`` rather than a guess: an unreadable or silent log means the placement is UNKNOWN, and
    unknown must never be presentable as the backend that was requested. The newest banner wins —
    the log is appended across restarts, so a stale CPU run must not condemn a live Vulkan one.
    """
    try:
        text = Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    found = _BACKEND_BANNER.findall(text)
    return found[-1] if found else None


def require_backend(log_path: Path, *, requested: str) -> str:
    """The loaded backend, or :class:`BackendNotHonoured` when it is not what was asked for.

    ``auto`` accepts whatever loaded — but still reports it, because "we did not care which" and
    "we do not know which" are different statements.
    """
    actual = loaded_backend(log_path)
    wanted = str(requested or "auto").strip() or "auto"
    if actual is None:
        raise BackendNotHonoured(
            f"the qwen tts worker did not report a backend in {log_path}; the placement of the "
            f"model is unknown and {wanted!r} cannot be confirmed"
        )
    if wanted.casefold() == "auto":
        return actual
    if actual.casefold() != wanted.casefold():
        raise BackendNotHonoured(
            f"qwen tts was asked for {wanted!r} and loaded on {actual!r}: the model is not on the "
            f"device Omega selected, which would be a silently slower voice rather than a failure"
        )
    return actual


def _path(raw: object, default: Path) -> Path:
    """Resolve a config path against the portable Omega root."""
    value = str(raw or "").strip()
    candidate = Path(value) if value else default
    if not candidate.is_absolute():
        candidate = OMEGA_ROOT / candidate
    return candidate.resolve()


def _block(config: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if config is not None:
        return config
    try:
        from config.loader import load_subsystem_bundle

        raw = (load_subsystem_bundle("tts").get("config") or {}).get("tts") or {}
        return raw if isinstance(raw, Mapping) else {}
    except Exception:
        return {}


def paths(
    config: Optional[Mapping[str, Any]] = None, *, platform: Optional[str] = None
) -> dict[str, Path]:
    """Return the complete Qwen payload paths without touching the process.

    ``config/tts.yaml`` names the Windows sidecar (the release builder stages that
    file verbatim). Off Windows the same payload directory carries the ELF build
    of the sidecar under the same name without ``.exe``. An explicit
    ``OMEGA_QWEN_TTS_BIN`` is used exactly as given.
    """
    cfg = _block(config)
    qwen = cfg.get("qwen") if isinstance(cfg.get("qwen"), Mapping) else {}
    root_raw = str(os.environ.get("OMEGA_QWEN_TTS_ROOT") or qwen.get("root") or "").strip()
    root = _path(root_raw, RUNTIME_MODELS_ROOT / "qwen3-tts") if root_raw else None

    def rooted(name: str, fallback: Path) -> Path:
        raw = qwen.get(name)
        if raw:
            candidate = Path(str(raw))
            if candidate.is_absolute():
                return candidate.resolve()
            if root is not None:
                return (root / candidate).resolve()
            return (OMEGA_ROOT / candidate).resolve()
        if root is not None:
            return (root / fallback.name).resolve()
        return fallback.resolve()

    override = os.environ.get("OMEGA_QWEN_TTS_BIN")
    binary = _path(override or qwen.get("binary"), _DEFAULT_BINARY)
    if not override and (platform or os.name) != "nt" and binary.suffix.lower() == ".exe":
        binary = binary.with_suffix("")
    return {
        "binary": binary,
        "model": rooted("model", _DEFAULT_MODEL),
        "codec": rooted("codec", _DEFAULT_CODEC),
    }


def payload_available(config: Optional[Mapping[str, Any]] = None) -> bool:
    """True only when the executable and both GGUF files are present."""
    resolved = paths(config)
    return all(item.is_file() for item in resolved.values())


def _speaker(value: object, config: Mapping[str, Any]) -> str:
    """Preserve the user's Qwen speaker selection; only absent values default."""
    candidate = str(value or "").strip()
    if not candidate:
        configured = str(config.get("speaker") or "").strip()
        return configured or "Aiden"
    return candidate


def _free_port(host: str = _DEFAULT_HOST) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return int(probe.getsockname()[1])


class QwenTTSProcess:
    """Own one qwentts.cpp server and expose Omega's ``synthesise`` shape."""

    engine = "qwen3-tts"

    def __init__(
        self,
        config: Optional[Mapping[str, Any]] = None,
        *,
        popen: Optional[Callable[..., Any]] = None,
        client: Optional[httpx.Client] = None,
    ) -> None:
        cfg = _block(config)
        qwen = cfg.get("qwen") if isinstance(cfg.get("qwen"), Mapping) else {}
        self.config = dict(qwen)
        self.paths = paths(cfg)
        self.host = str(qwen.get("host") or _DEFAULT_HOST)
        self.port = int(qwen.get("port") or _free_port(self.host))
        self.base_url = ""
        self.process: Any = None
        self._popen = popen or subprocess.Popen
        self._client = client or httpx.Client(timeout=httpx.Timeout(180.0, connect=5.0))
        from runtime.tts_stream import pcm_client

        self._async_client_factory = lambda: pcm_client(url=self.base_url)
        self.device = str(qwen.get("device") or "auto")
        self.provider = str(qwen.get("provider") or "ggml")
        self.execution = {"device": self.device, "provider": self.provider}
        self._warmed = False
        self._warmup_lock = Lock()
        self._start_timeout_s = max(
            5.0, float(qwen.get("start_timeout_seconds") or _DEFAULT_START_TIMEOUT_S)
        )
        try:
            self._start()
        except Exception:
            self.close()
            raise

    def warmup(self) -> None:
        """Compile and exercise the talker and streaming codec once at boot."""
        with self._warmup_lock:
            if self._warmed:
                return
            payload = {
                "input": "Ready to talk.",
                "voice": _speaker("", self.config),
                "response_format": "pcm",
                "speed": 1.0,
            }
            size = 0
            with self._client.stream("POST", f"{self.base_url}/v1/audio/speech", json=payload) as response:
                response.raise_for_status()
                for chunk in response.iter_bytes():
                    size += len(chunk)
            if not size or size % 2:
                raise RuntimeError(f"Qwen warmup returned invalid PCM ({size} bytes)")
            self._warmed = True
            log.info("qwen tts warmed; talker and codec remain resident")

    def _command(self) -> list[str]:
        return [
            str(self.paths["binary"]),
            "--model",
            str(self.paths["model"]),
            "--codec",
            str(self.paths["codec"]),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--alias",
            str(self.config.get("alias") or "omega-qwen3-tts"),
        ]

    def _start(self) -> None:
        missing = [str(path) for path in self.paths.values() if not path.is_file()]
        if missing:
            raise FileNotFoundError("Qwen TTS payload missing: " + ", ".join(missing))

        log_path = guardian_logs_root() / "qwen-tts.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        sink = log_path.open("a", encoding="utf-8", buffering=1)
        kwargs: dict[str, Any] = {
            "cwd": str(self.paths["binary"].parent),
            "env": dict(os.environ),
            "stdout": sink,
            "stderr": subprocess.STDOUT,
        }
        if self.device != "auto":
            kwargs["env"]["GGML_BACKEND"] = self.device
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        try:
            log.info(
                "qwen tts starting model=%s codec=%s port=%s",
                self.paths["model"],
                self.paths["codec"],
                self.port,
            )
            self.process = self._popen(self._command(), **kwargs)
        except Exception:
            sink.close()
            raise
        finally:
            # The child owns the duplicated file handle on Windows/POSIX; the
            # parent must close its copy so shutdown can rotate the log.
            try:
                sink.close()
            except Exception:
                pass

        self.base_url = f"http://{self.host}:{self.port}"
        deadline = time.monotonic() + self._start_timeout_s
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise RuntimeError(f"Qwen TTS worker exited early: {self.process.returncode}")
            try:
                if self._client.get(f"{self.base_url}/health").status_code == 200:
                    # Healthy is not the same as loaded where we asked. The worker announces its
                    # own backend; hold it to the request rather than believing the config.
                    actual = require_backend(log_path, requested=self.device)
                    self.execution = {"device": self.device, "provider": self.provider,
                                      "loaded_backend": actual}
                    log.info("qwen tts healthy port=%s backend=%s (requested %s)",
                             self.port, actual, self.device)
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.25)
        self.close()
        raise TimeoutError("Qwen TTS worker did not become healthy")

    def health(self) -> dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        if not alive or not self.base_url:
            return {
                "ok": False,
                "engine": self.engine,
                "device": self.device,
                "provider": self.provider,
                "reason": "qwen_tts_unavailable",
            }
        try:
            response = self._client.get(f"{self.base_url}/health")
            ok = response.status_code == 200
            body = response.json() if ok else {}
            return {
                "ok": ok,
                "engine": self.engine,
                "device": self.device,
                "provider": self.provider,
                **(body if isinstance(body, dict) else {}),
                "resident": alive and ok,
                "warmed": self._warmed and ok,
            }
        except (httpx.HTTPError, ValueError) as exc:
            return {
                "ok": False,
                "engine": self.engine,
                "device": self.device,
                "provider": self.provider,
                "reason": "qwen_tts_health_failed",
                "detail": str(exc)[:240],
            }

    def synthesise(self, text: str, voice: str, speed: float) -> dict[str, Any]:
        if not str(text or "").strip():
            return {"ok": False, "reason": "missing_text"}
        payload = {
            "input": str(text),
            "voice": _speaker(voice, self.config),
            "response_format": "wav",
            "speed": float(speed or 1.0),
        }
        try:
            response = self._client.post(f"{self.base_url}/v1/audio/speech", json=payload)
            if response.status_code >= 400:
                detail = response.text[:240]
                try:
                    body = response.json()
                    detail = json.dumps(body, ensure_ascii=True)[:240]
                except ValueError:
                    pass
                return {
                    "ok": False,
                    "reason": "qwen_synthesis_failed",
                    "status": response.status_code,
                    "detail": detail,
                }
            audio = bytes(response.content or b"")
            if not audio:
                return {"ok": False, "reason": "qwen_empty_synthesis"}
            log.info("qwen tts result bytes=%s text_len=%s", len(audio), len(text))
            return {
                "ok": True,
                "audio_b64": base64.b64encode(audio).decode("ascii"),
                "engine": self.engine,
            }
        except httpx.HTTPError as exc:
            return {"ok": False, "reason": "qwen_tts_transport_failed", "detail": str(exc)[:240]}

    async def synthesise_stream(self, text: str, voice: str, speed: float) -> AsyncIterator[bytes]:
        """Yield s16le 24 kHz mono chunks as qwentts.cpp generates them."""
        if not str(text or "").strip():
            raise ValueError("TTS text required")
        payload = {
            "input": str(text),
            "voice": _speaker(voice, self.config),
            "response_format": "pcm",
            "speed": float(speed or 1.0),
        }
        from contextlib import aclosing
        from runtime.tts_stream import http_pcm

        async with aclosing(http_pcm(
            f"{self.base_url}/v1/audio/speech", payload,
            client_factory=self._async_client_factory,
        )) as stream:
            async for chunk in stream:
                yield chunk

    def close(self) -> None:
        process = self.process
        self.process = None
        self.base_url = ""
        self._warmed = False
        try:
            self._client.close()
        finally:
            if process is None:
                return
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            except Exception as exc:  # noqa: BLE001 - teardown is best effort
                log.warning("qwen tts process teardown failed: %s", exc)

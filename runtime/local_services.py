"""Omega-owned local sensory and DME service.

This service is deliberately independent of the selected mind seat.  LM Studio
and cloud providers answer through ``runtime.brain_cloud``; this process owns
the local Parakeet, TTS, and DME capabilities that both seats can use.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import secrets
import signal
import subprocess
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Lock, Thread
from typing import Any, Mapping, Optional

import httpx
import numpy as np
from fastapi import FastAPI
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool
from runtime import vitis_lane
from runtime.tts_stream import PCMResponse

from core.paths import (
    OMEGA_ROOT,
    RUNTIME_BIN_ROOT,
    RUNTIME_MODELS_ROOT,
    STAGING_ROOT,
)

log = logging.getLogger("omega.runtime.local_services")

_DME_BIT_COUNT = 61 * 64
_DME_PAGE_SIZE = 8192
_DME_MODEL = RUNTIME_MODELS_ROOT / "onnxs" / "dme" / "overlap_matmul.onnx"
_PARAKEET_MODEL = RUNTIME_MODELS_ROOT / "ggufs" / "crispasr" / "parakeet-tdt-0.6b-v3-q4_k.gguf"
_LOCAL_SERVICE_DEFAULT_PORT = 11434
_CPU_PROVIDER = "CPUExecutionProvider"
_GPU_PROVIDERS = (
    "DmlExecutionProvider",
    "CUDAExecutionProvider",
    "ROCMExecutionProvider",
)
_NPU_PROVIDERS = ("QNNExecutionProvider",)
_VITIS_PROVIDER = "VitisAIExecutionProvider"


def _native_onnx_requested() -> bool:
    """Return whether a native ONNX Runtime session was explicitly requested.

    ONNX Runtime is a native extension.  Importing it is enough to terminate a
    Python process on machines whose bundled DLLs do not match the chipset or
    graphics driver (the Windows crash log showed exactly that failure).  The
    portable service therefore uses its deterministic NumPy scorer by default;
    native execution is an opt-in acceleration, never a boot prerequisite.
    """
    return (os.environ.get("OMEGA_NATIVE_ORT") or "").strip().casefold() in {
        "1",
        "true",
        "yes",
        "on",
    }


def selected_mind_seat() -> str:
    """Resolve the operator's mind seat for local-service device policy."""
    configured = (os.environ.get("OMEGA_MIND_BACKEND") or "").strip().lower()
    if configured in {"local", "cloud"}:
        return configured
    from runtime import brain_cloud, brain_cloud_config

    backend = brain_cloud_config.backend()
    if backend in {"local", "cloud"}:
        return backend
    return "cloud" if brain_cloud.enabled() else "local"


def _onnx_providers() -> list[str]:
    """Probe native providers only when acceleration was explicitly enabled.

    The CPU provider is a truthful portable default: the DME and speech lanes
    remain usable without loading an architecture-sensitive native DLL during
    gateway construction.
    """
    if not _native_onnx_requested():
        return [_CPU_PROVIDER]
    try:
        import onnxruntime as ort

        return [str(item) for item in ort.get_available_providers()]
    except Exception as exc:  # noqa: BLE001 - native probe must not kill boot
        log.warning("native ONNX Runtime probe failed; using portable CPU path: %s", exc)
        return [_CPU_PROVIDER]


def select_speech_execution(mind_seat: str, available: list[str]) -> dict[str, str]:
    """Choose speech placement without allowing it to claim the DME NPU."""
    seat = str(mind_seat).strip().lower()
    if seat not in {"local", "cloud"}:
        raise ValueError(f"unsupported mind seat: {mind_seat}")
    providers = {str(item) for item in available}
    requested = "gpu" if seat == "cloud" else "cpu"
    if seat == "cloud":
        for provider in _GPU_PROVIDERS:
            if provider in providers:
                return {"device": "gpu", "provider": provider, "requested": requested}
    if _CPU_PROVIDER not in providers:
        raise RuntimeError("speech has no usable CPU execution provider")
    return {"device": "cpu", "provider": _CPU_PROVIDER, "requested": requested}


def select_qwen_execution(mind_seat: str, configured: Mapping[str, Any]) -> dict[str, str]:
    """Run Qwen on CPU with a local mind and on the configured Vulkan GPU with cloud."""
    seat = str(mind_seat).strip().lower()
    if seat == "local":
        return {"device": "CPU", "provider": "cpu"}
    if seat != "cloud":
        raise ValueError(f"unsupported mind seat: {mind_seat}")

    device = str(configured.get("device") or "").strip()
    provider = str(configured.get("provider") or "").strip().casefold()
    if not device or device.casefold() in {"cpu", "auto"} or provider != "vulkan":
        raise RuntimeError(
            "cloud mind seat requires Qwen's configured Vulkan GPU device; "
            f"received device={device!r}, provider={provider!r}"
        )
    return {"device": device, "provider": provider}


def select_parakeet_backend(mind_seat: str, binary: Path) -> str:
    """Recognition runs on the CPU. The GPU belongs to the voice (operator 2026-09-22).

    This device is a shared-memory iGPU — `ggml_vulkan` reports the AMD 860M with `uma: 1` and
    ~8 GB total — and qwen TTS holds roughly 1.2 GB of weights plus 912 MB of KV cache on it, all
    28 talker layers resident. Putting recognition on the same device buys little (parakeet is
    fast on a 16-thread Ryzen AI 7) and costs the one path that must never be slow: speech has to
    START quickly, and it cannot while ASR is contending for the same memory and queues.

    So placement no longer follows the mind seat, and no longer follows which folder the binary
    shipped in — the Vulkan payload is the only one we ship and it runs CPU perfectly well with
    ``--gpu-backend cpu``. One rule, both seats: the voice owns the GPU, recognition owns the CPU.
    """
    seat = str(mind_seat).strip().lower()
    if seat not in {"local", "cloud"}:
        raise ValueError(f"unsupported mind seat: {mind_seat}")
    return "cpu"


def _temporary_audio_path(suffix: str) -> Path:
    """Allocate a short-lived audio path under Omega's portable state tree."""
    root = STAGING_ROOT / "audio"
    root.mkdir(parents=True, exist_ok=True)
    clean_suffix = str(suffix or ".bin")
    if not clean_suffix.startswith("."):
        clean_suffix = f".{clean_suffix}"
    return root / f"parakeet-{secrets.token_hex(16)}{clean_suffix}"


class TranscribeRequest(BaseModel):
    audio_b64: str
    format: str = "wav"
    language: str = "auto"


class SynthesiseRequest(BaseModel):
    text: str
    voice: str = ""
    length_scale: float = 1.0


class DmeOverlapRequest(BaseModel):
    query: list[float]
    memory: list[list[float]] = Field(default_factory=list)

    @field_validator("query")
    @classmethod
    def query_is_present(cls, value: list[float]) -> list[float]:
        if not value:
            raise ValueError("query required")
        return value


class PackedDmePlaneRequest(BaseModel):
    path: str
    rows: int = Field(ge=1)


class PackedDmeOverlapRequest(BaseModel):
    queries_b64: str


class DmeEngine:
    """DME overlap engine with a portable CPU route and optional native QNN EP.

    The NumPy implementation is the canonical portable scorer.  It computes
    the same query-by-memory popcount matrix as ``overlap_matmul.onnx`` while
    avoiding a process-fatal import of ONNX Runtime on incompatible Windows
    machines.  VitisAI runs only in the dedicated CPython 3.12 child lane; this
    CPython 3.13 gateway never registers or selects that provider.  Set
    ``OMEGA_NATIVE_ORT=1`` only after validating the machine's other native
    providers.
    """

    def __init__(self, model_path: Path = _DME_MODEL) -> None:
        self.model_path = model_path.resolve()
        self._session: Any = None
        self.provider_probe_error = ""
        self.backend = "numpy"
        self.provider = self._select_provider()
        self.device = "npu" if self.provider in _NPU_PROVIDERS else "cpu"
        if _native_onnx_requested() and self.provider != _CPU_PROVIDER:
            self.backend = "onnxruntime"
        elif _native_onnx_requested() and not self.provider_probe_error:
            # Native CPU is still opt-in.  A provider probe failure leaves the
            # deterministic NumPy implementation active and records the cause.
            self.backend = "onnxruntime"

    def _available_providers(self) -> list[str]:
        if not _native_onnx_requested():
            return [_CPU_PROVIDER]
        try:
            available = [
                provider
                for provider in _onnx_providers()
                if provider != _VITIS_PROVIDER
            ]
        except Exception as exc:  # noqa: BLE001 - preserve portable boot
            self.provider_probe_error = f"native ONNX Runtime unavailable: {exc}"[:240]
            log.warning("DME native provider probe failed; using NumPy: %s", exc)
            return [_CPU_PROVIDER]
        return available

    def _select_provider(self) -> str:
        available = self._available_providers()
        requested = (os.environ.get("OMEGA_DME_PROVIDER") or "auto").strip()
        if requested.casefold() not in {"", "auto"}:
            if requested not in available:
                self.provider_probe_error = (
                    f"configured DME provider is unavailable: {requested}; "
                    f"available={','.join(available)}"
                )[:240]
                log.warning("%s; using portable CPU DME", self.provider_probe_error)
                return _CPU_PROVIDER
            return requested
        for provider in _NPU_PROVIDERS:
            if provider in available:
                return provider
        if "CPUExecutionProvider" in available:
            return "CPUExecutionProvider"
        raise RuntimeError("DME has no usable ONNX execution provider")

    def _session_or(self) -> Any:
        if self._session is not None:
            return self._session
        if self.backend != "onnxruntime":
            return None
        if not self.model_path.is_file():
            raise FileNotFoundError(f"DME model is missing: {self.model_path}")
        import onnxruntime as ort

        options = ort.SessionOptions()
        options.intra_op_num_threads = max(1, int(os.environ.get("OMEGA_DME_ORT_THREADS", "1")))
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(self.model_path),
            sess_options=options,
            providers=[self.provider],
        )
        return self._session

    def _portable_warmup(self) -> None:
        """Exercise the exact-width NumPy scorer used on portable boots."""
        query = np.zeros((1, _DME_BIT_COUNT), dtype=np.float32)
        memory = np.zeros((_DME_PAGE_SIZE, _DME_BIT_COUNT), dtype=np.float32)
        np.matmul(query, memory.T)

    def warmup(self) -> None:
        query = np.zeros((1, _DME_BIT_COUNT), dtype=np.float32)
        memory = np.zeros((_DME_PAGE_SIZE, _DME_BIT_COUNT), dtype=np.float32)
        if self.backend == "numpy":
            self._portable_warmup()
            return
        try:
            self._session_or().run(None, {"query": query, "memory": memory.T})
        except Exception as exc:  # noqa: BLE001 - retreat to the portable scorer
            self._disable_native(exc)
            self._portable_warmup()

    def _disable_native(self, exc: Exception) -> None:
        """Retreat from a broken native session without losing DME."""
        self.provider_probe_error = f"native DME disabled: {exc}"[:240]
        log.warning("native DME session failed; using NumPy scorer: %s", exc)
        self.backend = "numpy"
        self.provider = _CPU_PROVIDER
        self.device = "cpu"
        session = self._session
        self._session = None
        closer = getattr(session, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception as close_error:  # noqa: BLE001 - preserve root cause
                log.warning("native DME session cleanup failed: %s", close_error)

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "device": self.device,
            "provider": self.provider,
            "backend": self.backend,
            "loaded": self.backend == "numpy" or self._session is not None,
            "provider_probe_error": self.provider_probe_error,
        }

    def close(self) -> None:
        """Release the DME session."""
        resources = (self._session,)
        self._session = None
        failures: list[Exception] = []
        for resource in resources:
            closer = getattr(resource, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception as exc:  # noqa: BLE001 - report after all releases
                    failures.append(exc)
        if failures:
            raise ExceptionGroup("DME resource release failed", failures)

    def overlap(self, query: list[float], memory: list[list[float]]) -> dict[str, Any]:
        query_array = np.asarray(query, dtype=np.float32).reshape(1, -1)
        if query_array.shape[1] != _DME_BIT_COUNT:
            return {"ok": False, "reason": "invalid_query_dim", "expected": _DME_BIT_COUNT}
        if not memory:
            return {"ok": True, "overlaps": [], "backend": self.provider}
        memory_array = np.asarray(memory, dtype=np.float32)
        if memory_array.ndim != 2 or memory_array.shape[1] != _DME_BIT_COUNT:
            return {"ok": False, "reason": "invalid_memory_shape", "expected_bits": _DME_BIT_COUNT}
        actual_length = memory_array.shape[0]
        if actual_length > _DME_PAGE_SIZE:
            return {"ok": False, "reason": "page_size_exceeded", "max_slots": _DME_PAGE_SIZE}
        if actual_length < _DME_PAGE_SIZE:
            padding = np.zeros(
                (_DME_PAGE_SIZE - actual_length, _DME_BIT_COUNT), dtype=np.float32
            )
            memory_array = np.vstack([memory_array, padding])
        if self.backend == "numpy":
            result = np.matmul(query_array, memory_array.T)
        else:
            try:
                result = self._session_or().run(
                    None, {"query": query_array, "memory": memory_array.T}
                )[0]
            except Exception as exc:  # noqa: BLE001 - preserve DME capability
                self._disable_native(exc)
                result = np.matmul(query_array, memory_array.T)
        return {
            "ok": True,
            "overlaps": result.reshape(-1)[:actual_length].astype(int).tolist(),
            "backend": self.provider,
            "engine": self.backend,
        }


class _UnavailableLocalService:
    """Explicit capability record used when an optional native lane cannot boot.

    The gateway remains reachable so cognition and browser automation can still
    start, but every unavailable operation returns ``ok=False`` with its original
    startup cause.  This is deliberately not a fake success or an empty result.
    """

    def __init__(self, name: str, exc: Exception) -> None:
        self.name = str(name)
        self.provider = "unavailable"
        self.device = "unavailable"
        self.backend = "unavailable"
        self.execution = {"device": "unavailable", "provider": "unavailable"}
        self.engine = "unavailable"
        self.provider_probe_error = f"{type(exc).__name__}: {exc}"[:240]
        self.reason = f"{self.name}_unavailable"

    def health(self) -> dict[str, Any]:
        return {
            "ok": False,
            "engine": self.engine,
            "device": self.device,
            "provider": self.provider,
            "backend": self.backend,
            "reason": self.reason,
            "detail": self.provider_probe_error,
        }

    def warmup(self) -> None:
        raise RuntimeError(f"{self.reason}: {self.provider_probe_error}")

    def overlap(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "reason": self.reason,
            "detail": self.provider_probe_error,
        }

    def transcribe(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "reason": self.reason,
            "detail": self.provider_probe_error,
        }

    def synthesise(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "reason": self.reason,
            "detail": self.provider_probe_error,
        }

    def synthesise_stream(self, *_args: Any, **_kwargs: Any):
        raise RuntimeError(f"{self.reason}: {self.provider_probe_error}")

    def close(self) -> None:
        return None


def _construct_service(name: str, factory: Any) -> Any:
    """Construct a local capability without making it a gateway boot blocker."""
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001 - retain the cause in health/logs
        log.error(
            "local service %s is unavailable; gateway remains online: %s",
            name,
            exc,
            exc_info=True,
        )
        return _UnavailableLocalService(name, exc)


class PackedDmeEngine:
    """Runtime-owned scorer for the live packed DME plane, on the Vitis lane.

    The memory organ owns the packed SRAM bytes, but it does not load an ONNX
    session. This service bakes the payload bits into content-addressed tile
    models here (the brain venv owns ``onnx``) and hands their manifest to the
    cp312 provider child (``runtime.vitis_worker`` on ``.venv-vitis``), which
    opens them on VitisAIExecutionProvider. Readiness is reported only after
    the child's placement receipt proves every tile. If that proof is
    unavailable, the caller keeps the exact CPU scorer instead.
    """

    _PROVIDER = "VitisAIExecutionProvider"
    _BAKE_TIMEOUT_S = 1800.0

    def __init__(self, *, client_factory: Optional[Any] = None) -> None:
        factory = client_factory or (lambda: vitis_lane.default_client(OMEGA_ROOT))
        self.provider = self._PROVIDER
        self.device = "unavailable"
        self.reason = ""
        self.worker: dict[str, str] = {}
        self._client: Any = None
        self._loaded = False
        self._signature: tuple[int, int] | None = None
        self._rows = 0
        self._path = ""
        try:
            client = factory()
            hello = client.start()
        except Exception as exc:  # noqa: BLE001 - health carries the cause
            self.reason = f"{type(exc).__name__}: {exc}"[:600]
            log.warning("packed DME Vitis lane unavailable: %s", self.reason)
            return
        self.worker = {
            "python": str(hello.get("python", "")),
            "onnxruntime": str(hello.get("onnxruntime", "")),
        }
        providers = [str(item) for item in hello.get("providers") or []]
        if self._PROVIDER not in providers:
            self.reason = (
                f"the Vitis lane child has no {self._PROVIDER}; "
                f"providers={','.join(providers)}"
            )
            client.close()
            return
        self._client = client
        self.device = "npu"

    @property
    def available(self) -> bool:
        return self.device == "npu"

    def health(self) -> dict[str, Any]:
        return {
            "ok": self.available,
            "device": self.device,
            "provider": self.provider,
            "loaded": self._loaded,
            "reason": self.reason,
            "worker": dict(self.worker),
        }

    def _lane_failed(self, exc: Exception) -> None:
        """A dead or stalled child ends the lane for this gateway's lifetime."""
        if isinstance(exc, (vitis_lane.VitisWorkerExited, TimeoutError)):
            self.device = "unavailable"
            self._loaded = False
            self.reason = f"{type(exc).__name__}: {exc}"[:600]

    @staticmethod
    def _plane_bits(path: Path, rows: int) -> np.ndarray:
        from memory.dme.lobe_layout import ENTRY_WORDS
        from memory.dme.packer import HEADER_WORDS

        raw = np.fromfile(path, dtype=np.uint64)
        expected = rows * ENTRY_WORDS
        if raw.size != expected:
            raise ValueError(
                f"packed DME plane has {raw.size} words; expected {expected}"
            )
        entries = raw.reshape(rows, ENTRY_WORDS)
        bits = np.unpackbits(
            entries.view(np.uint8), bitorder="little"
        ).reshape(rows, ENTRY_WORDS * 64)
        return np.ascontiguousarray(bits[:, HEADER_WORDS * 64 :].T, dtype=np.uint8)

    def load_plane(self, path: str, rows: int) -> dict[str, Any]:
        if not self.available:
            raise RuntimeError(self.reason or "DME NPU provider is unavailable")
        candidate = Path(path).resolve()
        try:
            candidate = candidate.relative_to(OMEGA_ROOT.resolve())
        except ValueError as exc:
            raise ValueError(f"DME plane is outside the Omega tree: {path}") from exc
        resolved = (OMEGA_ROOT / candidate).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"DME plane is missing: {resolved}")
        stat = resolved.stat()
        signature = (stat.st_mtime_ns, stat.st_size)
        if self._loaded and self._signature == signature and self._rows == rows:
            return self.health()

        from memory.dme import npu_backend

        cache_root = npu_backend.default_cache_root()
        manifest = npu_backend.bake_tiles(
            self._plane_bits(resolved, rows),
            batch=1,
            shard_columns=8192,
            k_chunk=3904,
            cache_root=cache_root,
            cache_key="live_dme",
        )
        self._loaded = False
        try:
            opened = self._client.request(
                "open_tiles",
                {"manifest": manifest, "cache_root": str(cache_root), "verify_npu": True},
                timeout_s=self._BAKE_TIMEOUT_S,
            )
        except Exception as exc:
            self._lane_failed(exc)
            raise
        report = list(opened.get("engagement") or [])
        if len(report) != len(manifest["tiles"]) or not all(
            item.get("proven") is True for item in report
        ):
            self.reason = "DME NPU placement was not proven for every tile"
            raise RuntimeError(self.reason)
        self._loaded = True
        self._signature = signature
        self._rows = rows
        self._path = str(resolved)
        self.reason = ""
        return self.health()

    def overlap(self, queries_b64: str) -> dict[str, Any]:
        if not self._loaded:
            raise RuntimeError("packed DME plane is not loaded")
        from memory.dme.lobe_layout import ENTRY_WORDS
        from memory.dme.packer import HEADER_WORDS

        try:
            raw = base64.b64decode(queries_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise ValueError("queries_b64 is not valid base64") from exc
        words_per_query = ENTRY_WORDS * 8
        if not raw or len(raw) % words_per_query:
            raise ValueError("queries_b64 has an invalid packed query width")
        entries = np.frombuffer(raw, dtype=np.uint64).reshape(-1, ENTRY_WORDS)
        bits = np.unpackbits(
            entries.view(np.uint8), bitorder="little"
        ).reshape(entries.shape[0], ENTRY_WORDS * 64)
        queries = np.ascontiguousarray(bits[:, HEADER_WORDS * 64 :], dtype=np.uint8)
        try:
            scored = self._client.request("overlap", {"bits": vitis_lane.encode_bits(queries)})
        except Exception as exc:
            self._lane_failed(exc)
            raise
        return {
            "ok": True,
            "overlaps": scored["overlaps"],
            "device": self.device,
            "provider": self.provider,
        }

    def close(self) -> None:
        """Release the child's tile sessions and end the provider child."""
        client = self._client
        self._client = None
        self._loaded = False
        self._signature = None
        self._rows = 0
        self._path = ""
        self.device = "unavailable"
        self.reason = "packed DME Vitis lane is closed"
        if client is not None:
            client.close()


def create_tts_service() -> Any:
    """Construct Qwen on the CPU or Vulkan GPU selected by the mind seat."""
    try:
        from config.loader import load_subsystem_bundle

        cfg = (load_subsystem_bundle("tts").get("config") or {}).get("tts") or {}
    except Exception:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    engine = str(cfg.get("engine") or "qwen").strip().casefold()
    if engine not in {"auto", "qwen", "qwen3", "qwen3-tts"}:
        raise RuntimeError(
            f"Qwen TTS is the only supported speech engine; configured engine={engine!r}"
        )
    from runtime import qwen_tts

    if not qwen_tts.payload_available(cfg):
        raise FileNotFoundError(
            "Qwen TTS is selected; it is the only supported speech engine and its "
            "binary/model/codec payload is missing"
        )
    qwen_config = cfg.get("qwen") if isinstance(cfg.get("qwen"), Mapping) else {}
    cfg = dict(cfg)
    cfg["qwen"] = {
        **qwen_config,
        **select_qwen_execution(selected_mind_seat(), qwen_config),
    }
    return qwen_tts.QwenTTSProcess(cfg)


class ParakeetEngine:
    """Small CrispASR process adapter; the worker remains SSD-owned."""

    def __init__(self, model_path: Path = _PARAKEET_MODEL) -> None:
        self.model_path = model_path.resolve()
        self.process: Optional[subprocess.Popen] = None
        self.base_url = ""
        self.mind_seat = selected_mind_seat()
        self.backend = ""
        self.device = ""
        self._warmed = False
        self._warmup_lock = Lock()
        self._client = httpx.Client(timeout=httpx.Timeout(180.0, connect=5.0))
        self._start()

    def warmup(self) -> None:
        """Run the recognizer once; retain its native worker until shutdown."""
        with self._warmup_lock:
            if self._warmed:
                return
            audio = io.BytesIO()
            with wave.open(audio, "wb") as wav:
                wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                wav.writeframes(bytes(32000))
            result = self.transcribe({
                "audio_b64": base64.b64encode(audio.getvalue()).decode("ascii"),
                "format": "wav", "language": "en",
            })
            if result.get("ok") is not True:
                raise RuntimeError(f"Parakeet warmup failed: {result}")
            self._warmed = True
            log.info("parakeet warmed; recognizer remains resident")

    def health(self) -> dict[str, Any]:
        alive = self.process is not None and self.process.poll() is None
        return {
            "ok": alive, "engine": "parakeet", "device": self.device,
            "backend": self.backend, "resident": alive,
            "warmed": self._warmed and alive,
        }

    def _binary(self) -> Path:
        suffix = "crispasr.exe" if os.name == "nt" else "crispasr"
        candidates = sorted(RUNTIME_BIN_ROOT.glob(f"crispasr/**/{suffix}"))
        if not candidates:
            raise FileNotFoundError(f"Parakeet CrispASR binary is missing under {RUNTIME_BIN_ROOT}")
        preferred = [item for item in candidates if item.parent.name == "vulkan"]
        if self.mind_seat == "cloud" and not preferred:
            raise FileNotFoundError("cloud speech requires the portable Vulkan CrispASR payload")
        return (preferred or candidates)[0]

    def _start(self) -> None:
        try:
            if not self.model_path.is_file():
                raise FileNotFoundError(f"Parakeet model is missing: {self.model_path}")
            port = 35801
            binary = self._binary()
            self.backend = select_parakeet_backend(self.mind_seat, binary)
            self.device = "gpu" if self.backend == "vulkan" else "cpu"
            command = [
                str(binary),
                "--server",
                "--backend",
                "parakeet",
                "-m",
                str(self.model_path),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--gpu-backend",
                self.backend,
                "-l",
                "en",
            ]
            environment = dict(os.environ)
            environment["PATH"] = f"{binary.parent}{os.pathsep}{environment.get('PATH', '')}"
            kwargs: dict[str, Any] = {
                "cwd": str(binary.parent),
                "env": environment,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            self.process = subprocess.Popen(command, **kwargs)
            self.base_url = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"Parakeet worker exited early: {self.process.returncode}"
                    )
                try:
                    if self._client.get(f"{self.base_url}/health").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.25)
            raise TimeoutError("Parakeet worker did not become healthy")
        except Exception as exc:
            try:
                self.close()
            except Exception as cleanup_error:  # noqa: BLE001 - preserve startup cause
                exc.add_note(f"Parakeet startup cleanup also failed: {cleanup_error}")
            raise

    def transcribe(self, body: Mapping[str, Any]) -> dict[str, Any]:
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("Parakeet worker is not running")
        raw_audio = base64.b64decode(str(body["audio_b64"]))
        suffix = ".wav" if str(body.get("format") or "wav").casefold() == "wav" else ".bin"
        temporary_path = _temporary_audio_path(suffix)
        temporary_path.write_bytes(raw_audio)
        try:
            with temporary_path.open("rb") as audio_file:
                response = self._client.post(
                    f"{self.base_url}/inference",
                    files={"file": (f"audio{suffix}", audio_file, "application/octet-stream")},
                    data={"response_format": "json", "language": str(body.get("language") or "auto")},
                )
            response.raise_for_status()
            payload = response.json()
            text = str(payload.get("text") or payload.get("transcript") or "").strip()
            return {"ok": True, "text": text, "language": body.get("language", "auto"), "engine": "parakeet"}
        finally:
            temporary_path.unlink(missing_ok=True)

    def close(self) -> None:
        client = self._client
        process = self.process
        self.process = None
        self.base_url = ""
        self._warmed = False
        failures: list[Exception] = []
        try:
            client.close()
        except Exception as exc:  # noqa: BLE001 - report after worker teardown
            failures.append(exc)
        if process is not None:
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
            except Exception as exc:  # noqa: BLE001 - report after all cleanup
                failures.append(exc)
        if failures:
            raise ExceptionGroup("Parakeet resource release failed", failures)


def create_app(
    *,
    dme: Optional[DmeEngine] = None,
    packed_dme: Optional[PackedDmeEngine] = None,
    tts: Optional[Any] = None,
    stt: Optional[ParakeetEngine] = None,
    warmup: bool = True,
) -> FastAPI:
    dme_service = dme or _construct_service("dme", DmeEngine)
    services = {
        "dme": dme_service,
        "packed_dme": packed_dme or _construct_service("packed_dme", PackedDmeEngine),
        "tts": tts or _construct_service("tts", create_tts_service),
        "stt": stt or _construct_service("stt", ParakeetEngine),
    }

    def close_services() -> None:
        """Release every model seat even when one individual close fails."""
        failures: list[Exception] = []
        for name in ("stt", "tts", "packed_dme", "dme"):
            service = services[name]
            closer = getattr(service, "close", None)
            if not callable(closer):
                failures.append(TypeError(f"local service {name} has no close()"))
                continue
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 - report after all services
                failure = RuntimeError(f"local service {name} close failed")
                failure.__cause__ = exc
                failures.append(failure)
        if failures:
            raise ExceptionGroup("local service release failed", failures)

    if warmup:
        try:
            # Nothing serves /health until the inference paths have run. These
            # service objects retain their models for the gateway's lifetime.
            for name in ("dme", "tts", "stt"):
                services[name].warmup()
        except Exception as exc:
            try:
                close_services()
            except Exception as cleanup_error:  # noqa: BLE001 - preserve warmup cause
                exc.add_note(f"local service startup cleanup also failed: {cleanup_error}")
            raise

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            yield
        finally:
            close_services()

    app = FastAPI(title="Omega Local Services", version="1.0.0", lifespan=lifespan)
    app.state.services = services
    app.state.close_services = close_services

    @app.post("/admin/orchestration/shutdown")
    def orchestration_shutdown() -> dict[str, str | bool]:
        """Release all local models before asking the server to exit."""
        server = getattr(app.state, "server", None)
        try:
            close_services()
        finally:
            if server is not None:
                server.should_exit = True
        return {"ok": True, "shutdown": "services_closed"}

    @app.get("/health")
    def health() -> dict[str, Any]:
        dme = services["dme"]
        packed_dme = services["packed_dme"]
        tts = services["tts"]
        stt = services["stt"]
        dme_health = dme.health() if callable(getattr(dme, "health", None)) else {
            "ok": True,
            "provider": dme.provider,
            "device": dme.device,
        }
        tts_health = tts.health() if callable(getattr(tts, "health", None)) else {
            "ok": True,
            "device": tts.execution["device"],
            "provider": tts.execution["provider"],
        }
        stt_health = stt.health() if callable(getattr(stt, "health", None)) else {
            "ok": True,
            "device": getattr(stt, "device", "cpu"),
            "backend": getattr(stt, "backend", "cpu"),
        }
        return {
            "status": "ok",
            "service": "omega-local-services",
            "mind": selected_mind_seat(),
            "services": {
                "dme": dme.provider,
                "stt": "parakeet",
                "tts": str(getattr(tts, "engine", "unavailable")),
            },
            "devices": {
                "dme": {"device": dme.device, "provider": dme.provider},
                "stt": {
                    "device": getattr(stt, "device", "cpu"),
                    "backend": getattr(stt, "backend", "cpu"),
                },
                "tts": {
                    "device": tts.execution["device"],
                    "provider": tts.execution["provider"],
                },
            },
            "dme_provider_probe_error": dme.provider_probe_error,
            "packed_dme": packed_dme.health(),
            "capability_health": {
                "dme": dme_health,
                "tts": tts_health,
                "stt": stt_health,
            },
        }

    @app.post("/generate")
    def generate(body: dict[str, Any]) -> JSONResponse:
        """Proxy local cognition to the operator-selected LM Studio seat."""
        from runtime import brain_local

        result = brain_local.generate_completion(body)
        if result is None:
            result = {
                "ok": False,
                "reason": "mind_local_unavailable",
                "detail": "local mind seat did not return a response",
            }
        return JSONResponse(result)

    @app.post("/generate/stream")
    def generate_stream(body: dict[str, Any]) -> StreamingResponse:
        """Proxy the mind seat's SSE using the stable Omega stream contract.

        Tokens, the think trace and every tool-call fragment are forwarded as their own
        events; the client reassembles the calls, so a tool-carrying pass streams exactly
        as the blocking call would answer.
        """
        from runtime import brain_local

        def events():
            try:
                done: dict[str, Any] = {}
                for event, data in brain_local.generate_stream_events(body):
                    if event == "token":
                        yield f"event: token\ndata: {json.dumps({'token': data})}\n\n"
                    elif event == "reasoning":
                        yield f"event: reasoning\ndata: {json.dumps({'text': data})}\n\n"
                    elif event == "tool_call":
                        yield f"event: tool_call\ndata: {json.dumps(data)}\n\n"
                    elif event == "done":
                        done = dict(data or {})
                yield f"event: done\ndata: {json.dumps(done)}\n\n"
            except Exception as exc:  # noqa: BLE001 - stream clients need the cause
                yield (
                    "event: error\ndata: "
                    f"{json.dumps({'reason': type(exc).__name__, 'detail': str(exc)[:240]})}\n\n"
                )

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.post("/transcribe")
    def transcribe(body: TranscribeRequest) -> JSONResponse:
        return JSONResponse(services["stt"].transcribe(body.model_dump()))

    @app.post("/synthesise")
    def synthesise(body: SynthesiseRequest) -> JSONResponse:
        return JSONResponse(services["tts"].synthesise(body.text, body.voice, body.length_scale))

    @app.post("/synthesise/stream")
    async def synthesise_stream(body: SynthesiseRequest):
        """Stream raw s16le 24 kHz mono audio from the active TTS engine.

        The health gate runs before the response is created so a cold or
        unavailable model is reported as an honest 503 instead of a successful
        empty completion.  The iterator is intentionally owned by the service:
        qwentts.cpp can stop work when the client disconnects.
        """
        if not body.text.strip():
            return JSONResponse({"ok": False, "reason": "missing_text"}, status_code=400)
        service = services["tts"]
        health_fn = getattr(service, "health", None)
        health = await run_in_threadpool(health_fn) if callable(health_fn) else {
            "ok": True,
            "engine": getattr(service, "engine", "unknown"),
            "device": getattr(service, "device", "unknown"),
            "provider": getattr(service, "provider", "unknown"),
        }
        if not health.get("ok"):
            return JSONResponse(health, status_code=503)
        producer = getattr(service, "synthesise_stream", None)
        if not callable(producer):
            return JSONResponse(
                {
                    "ok": False,
                    "reason": "tts_stream_unsupported",
                    "engine": getattr(service, "engine", "unknown"),
                },
                status_code=501,
            )

        return PCMResponse(
            producer(body.text, body.voice, body.length_scale),
            headers={
                "Cache-Control": "no-store",
                "X-Omega-TTS-Sample-Rate": "24000",
                "X-Omega-TTS-Channels": "1",
                "X-Omega-TTS-Sample-Width": "2",
                "X-Omega-TTS-Engine": str(getattr(service, "engine", "unknown")),
            },
        )

    @app.post("/dme/overlap")
    def overlap(body: DmeOverlapRequest) -> JSONResponse:
        started = time.monotonic()
        result = services["dme"].overlap(body.query, body.memory)
        result["latency_ms"] = int((time.monotonic() - started) * 1000)
        return JSONResponse(result)

    @app.get("/dme/packed/health")
    def packed_health() -> JSONResponse:
        return JSONResponse(services["packed_dme"].health())

    @app.post("/dme/packed/plane")
    def packed_plane(body: PackedDmePlaneRequest) -> JSONResponse:
        return JSONResponse(services["packed_dme"].load_plane(body.path, body.rows))

    @app.post("/dme/packed/overlap")
    def packed_overlap(body: PackedDmeOverlapRequest) -> JSONResponse:
        return JSONResponse(services["packed_dme"].overlap(body.queries_b64))

    return app


def exit_with_parent(
    parent_pid: int,
    *,
    getppid: Any = os.getppid,
    getpgrp: Any = None,
    killpg: Any = None,
    sleep: Any = time.sleep,
    interval_s: float = 1.0,
) -> None:
    """POSIX counterpart of the Windows kill-on-close job.

    The gateway runs in its own session with its TTS and STT sidecars in its
    process group. Once it is re-parented, the brain that spawned it is gone, so
    the whole group is terminated instead of being left running unowned.
    """
    while getppid() == parent_pid:
        sleep(interval_s)
    log.warning(
        "owning brain pid=%s is gone — terminating the gateway and its sidecars", parent_pid
    )
    (killpg or os.killpg)((getpgrp or os.getpgrp)(), signal.SIGTERM)


def main() -> None:
    parser = argparse.ArgumentParser(prog="omega-local-services")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=_LOCAL_SERVICE_DEFAULT_PORT)
    args = parser.parse_args()
    owner = (os.environ.get("OMEGA_RUNTIME_PARENT_PID") or "").strip()
    if os.name != "nt" and owner.isdigit():
        Thread(
            target=exit_with_parent, args=(int(owner),), name="gateway-owner-watch", daemon=True
        ).start()
    import uvicorn

    app = create_app()
    server = uvicorn.Server(
        uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
    )
    app.state.server = server
    server.run()


if __name__ == "__main__":
    main()

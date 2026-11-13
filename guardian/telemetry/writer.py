"""Failure-soft JSONL telemetry writer — per-system files + firehose."""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from core.paths import guardian_logs_root

log = logging.getLogger("omega.guardian.telemetry")

# guardian/logs is the SINGLE log directory (operator directive 2026-07-13) —
# jsonl telemetry, per-component *.log files, and terminal/infra streams all
# land there. guardian/telemetry/ holds package code only.
# Resolved through core.paths so OMEGA_GUARDIAN_LOGS_DIR (tests, the plain-script matrix)
# redirects it; unset in a live boot, so this is the live guardian/logs.
TELEMETRY_DIR = guardian_logs_root()
TELEMETRY_SYSTEMS = (
    "pressure",
    "global",
    "meta",
    "auto",
    "omega",
    "deepsleep",
    "memory",
    "runtime",
    "guardian",
    "knowledge",
)

_RUN_ID: str = ""
# Phase 3 cut 2: per-thread episode context — lane-parallel cascades each stamp
# their own episode id on the telemetry they emit; a single process-global id
# would cross-tag concurrent lanes. Threads that never set one (observers: HUD,
# schedulers, guardian sweeps) honestly stamp "no-episode".
_EPISODE_TLS = threading.local()
_LOCKS: dict[str, threading.Lock] = {}
_GLOBAL_LOCK = threading.Lock()

_NO_EPISODE = "no-episode"

# Unified failure log — the bounded, every-system "what went down" record.
# write_telemetry forks any failure-status (or ok=False) event HERE in addition
# to the firehose/per-system files. Only failures write, so it stays small; a
# remote monitor pulls this one file to detect failures across any system.
FAILURE_LOG = guardian_logs_root() / "failures.jsonl"
_FAILURE_STATUSES = frozenset({"fail", "error", "degraded", "crash", "timeout", "declined"})
# Client-side validation / probe traffic — still in firehose, not the failure sink.
_NON_FAILURE_CLASSES = frozenset(
    {
        "bad_request",
    }
)
_PREBOOT_SUBSTRATE_CLASSES = frozenset(
    {
        "tts_model_missing",
        "stt_model_missing",
        "tts_status_failed",
        "stt_status_failed",
    }
)
_PREBOOT_INFERENCE_PHASES = frozenset(
    {
        "stt",
        "tts",
    }
)


def _is_preboot_noise(record: dict[str, Any]) -> bool:
    """HUD-only / plain-script runs without a registered boot ``run_id``.

      Substrate gaps (Whisper/TTS/face runtime down) and sensory inference
    misses are expected before ``boot.py`` registers PressureRuntime — they
      must not pollute the operator failure monitor.
    """
    run_id = str(record.get("run_id") or "").strip()
    if run_id not in ("", "no-run"):
        return False
    failure_class = str(record.get("failure_class") or "").strip().lower()
    if failure_class in _PREBOOT_SUBSTRATE_CLASSES:
        return True
    if failure_class == "path_traversal_attempt":
        return True
    phase = str(record.get("phase") or "").strip().lower()
    operation = str(record.get("operation") or phase).strip().lower()
    if (
        str(record.get("event") or "") == "inference"
        and operation in _PREBOOT_INFERENCE_PHASES
        and record.get("ok") is False
    ):
        return True
    blob = " ".join(str(record.get(key) or "") for key in ("error", "reason", "summary")).lower()
    if "no pressureruntime registered" in blob:
        return True
    if (
        str(record.get("system") or "") == "hud"
        and str(record.get("status") or "") == "degraded"
        and phase in {"stt_transcribe", "tts_speak", "stt_status", "tts_status"}
    ):
        return True
    return False


def _is_failure(record: dict[str, Any]) -> bool:
    failure_class = str(record.get("failure_class") or "").strip().lower()
    if failure_class in _NON_FAILURE_CLASSES:
        return False
    if _is_preboot_noise(record):
        return False
    if str(record.get("status") or "").strip().lower() in _FAILURE_STATUSES:
        return True
    return record.get("ok") is False


def _ensure_dir() -> None:
    try:
        TELEMETRY_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.debug("telemetry mkdir failed: %s", exc)


def _lock_for(path: Path) -> threading.Lock:
    key = str(path)
    if key not in _LOCKS:
        with _GLOBAL_LOCK:
            if key not in _LOCKS:
                _LOCKS[key] = threading.Lock()
    return _LOCKS[key]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_status(status: Any, ok: Any) -> str:
    """Keep the status and boolean outcome fields from contradicting each other."""
    normalized = str(status or "").strip().lower()
    if ok is False and normalized in {"", "ok", "success", "completed"}:
        return "fail"
    if ok is False and normalized in {"partial", "in_progress", "in-progress"}:
        return "degraded"
    return str(status or "")


def _normalize_system(system: str) -> str:
    raw = str(system or "").strip().lower()
    aliases = {
        "pressure_loop": "pressure",
        "global_loop": "global",
        "meta_loop": "meta",
        "auto_loop": "auto",
        "omega_loop": "omega",
        "deepsleep_loop": "deepsleep",
        "cognition_chain": "pressure",
    }
    mapped = aliases.get(raw, raw)
    if mapped in TELEMETRY_SYSTEMS:
        return mapped
    for candidate in TELEMETRY_SYSTEMS:
        if candidate in mapped:
            return candidate
    return mapped or "guardian"


def init_run_id(run_id: Optional[str] = None) -> str:
    """Assign a per-process boot id (stable until reset)."""
    global _RUN_ID
    _RUN_ID = str(run_id or uuid.uuid4())
    return _RUN_ID


def current_run_id() -> str:
    return _RUN_ID or "no-run"


def set_episode_id(episode_id: Optional[str]) -> None:
    """Bind the CALLING thread's telemetry to an episode (None to clear)."""
    _EPISODE_TLS.episode_id = str(episode_id or "").strip() or _NO_EPISODE


def current_episode_id() -> str:
    return getattr(_EPISODE_TLS, "episode_id", _NO_EPISODE)


def reset_for_tests() -> None:
    """Test isolation — clear run/episode context (calling thread)."""
    global _RUN_ID
    _RUN_ID = ""
    _EPISODE_TLS.episode_id = _NO_EPISODE


def _append_line(path: Path, record: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = _lock_for(path)
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with lock:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception as exc:
        log.warning("telemetry write failed (%s): %s", path.name, exc)


def write_telemetry(
    *,
    system: str,
    event: str,
    phase: str = "",
    status: str = "",
    summary: str = "",
    episode_id: Optional[str] = None,
    run_id: Optional[str] = None,
    **fields: Any,
) -> None:
    """Write one structured telemetry record (failure-soft)."""
    try:
        sys_name = _normalize_system(system)
        normalized_status = _canonical_status(status, fields.get("ok"))
        record: dict[str, Any] = {
            "ts": _iso_now(),
            "run_id": run_id or current_run_id(),
            "episode_id": episode_id if episode_id is not None else current_episode_id(),
            "system": sys_name,
            "phase": str(phase or ""),
            "event": str(event or ""),
            "status": normalized_status,
        }
        if summary:
            record["summary"] = summary
        for key, value in fields.items():
            if value is not None:
                record[key] = value
        if "ok" not in record and normalized_status.lower() in _FAILURE_STATUSES:
            record["ok"] = False

        _append_line(TELEMETRY_DIR / "firehose.jsonl", record)
        if sys_name in TELEMETRY_SYSTEMS:
            _append_line(TELEMETRY_DIR / f"{sys_name}.jsonl", record)
        # Failure sink — only failures land here, so it stays bounded.
        if _is_failure(record):
            _append_line(FAILURE_LOG, record)
    except Exception as exc:
        log.warning("telemetry write_telemetry failed: %s", exc)


def emit_boot_marker(*, build: str = "main") -> str:
    """Write one boot marker to firehose + each per-system file."""
    run_id = init_run_id()
    ts = _iso_now()
    for system in TELEMETRY_SYSTEMS:
        record = {
            "ts": ts,
            "run_id": run_id,
            "episode_id": _NO_EPISODE,
            "system": system,
            "phase": "",
            "event": "boot",
            "status": "ok",
            "build": build,
        }
        _append_line(TELEMETRY_DIR / "firehose.jsonl", record)
        _append_line(TELEMETRY_DIR / f"{system}.jsonl", record)
    return run_id

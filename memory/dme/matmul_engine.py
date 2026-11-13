"""Deterministic overlap engine — NPU packed plane preferred, numpy fallback.

The Omega local-services process owns the silicon: its packed-DME worker holds
the DME plane resident on the XDNA2 tiles and scores popcount(entry AND query)
exactly. This module dials that worker through the portable local service, with
an honest CPU route when the worker is unavailable.

Exactness guards (the NPU must never score a DIFFERENT plane than the numpy
path would):

- ``register_plane`` records the sram.bin path + rows + stat signature at
  the moment ``load_sram``/rebuild produced the in-memory array;
- every NPU call re-stats the file — if it moved on since registration, the
  caller's array is older than the file, so numpy (which scores the array)
  is the only honest engine for that call;
- the server bakes FROM the registered file and re-bakes lazily; any server
  hiccup drops the bake marker so the next call re-bakes from scratch.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Literal, Optional

import numpy as np

from memory.dme.geometry import validate_query_entry, validate_weight_entries
from memory.dme.lobe_layout import PAYLOAD_BITS
from memory.dme.packer import HEADER_WORDS

log = logging.getLogger("omega.memory.dme.matmul_engine")

_ACTIVE_BACKEND: Optional[Literal["numpy", "npu_packed"]] = None


def npu_service_base_url() -> str:
    """Return the portable local-services endpoint for packed DME."""
    return os.environ.get(
        "OMEGA_LOCAL_SERVICES_URL", "http://127.0.0.1:11434"
    ).rstrip("/")


_PROBE_TTL_S = 10.0
# The Vitis worker permits 1800 seconds for the first tile compile and NPU
# placement. Give the HTTP caller one extra minute to receive that result.
_NPU_PLANE_LOAD_TIMEOUT_S = 1860.0

_lock = threading.Lock()
_registered: dict = {"path": "", "rows": 0, "sig": None, "generation_id": None, "lease": None}
_baked = {"sig": None}
_probe = {"at": 0.0, "ok": False}


def _npu_enabled() -> bool:
    return os.environ.get("OMEGA_DME_NPU", "1").strip().lower() not in (
        "0",
        "false",
        "off",
    )


def _stat_sig(path: str):
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _generation_mode_active() -> bool:
    """A projection manifest exists for the live memory root (Cut 13 generation mode)."""
    try:
        from memory.projection.generation import manifest_state, projection_root

        return manifest_state(projection_root(None)).mode != "legacy"
    except Exception:  # noqa: BLE001 — probing must never break scoring
        return False


def register_plane(path, rows: int, *, generation_id: str | None = None) -> None:
    """Adapter hook: the sram file the current in-memory index came from.

    Cut 13 (D-13-24/37): under generation mode the registration is PINNED to a
    generation and holds that generation's shared OS lease until ``unregister_plane``
    (or a re-registration for another generation) — the resident plane's files can
    never be GC'd from under the scorer, and the scorer never scores one generation's
    view against another's registration. The un-pinned legacy signature is refused
    while a manifest exists."""
    from memory.projection.generation import GenerationIntegrityError

    if generation_id is None and _generation_mode_active():
        raise GenerationIntegrityError("register_plane requires generation_id under generation mode (D-13-24)")
    lease = None
    if generation_id is not None:
        from memory.projection.generation import acquire_shared_lease, projection_root

        lease = acquire_shared_lease(projection_root(None), generation_id)
    with _lock:
        previous = _registered.get("lease")
        _registered["path"] = str(path)
        _registered["rows"] = int(rows)
        _registered["sig"] = _stat_sig(str(path))
        _registered["generation_id"] = generation_id
        _registered["lease"] = lease
        _baked["sig"] = None  # force a re-bake against the fresh registration
    if previous is not None:
        previous.release()


def unregister_plane(generation_id: str | None = None) -> bool:
    """Release the scorer's generation lease (D-13-37). Returns True iff a registration
    for ``generation_id`` (or any, when None) was released."""
    with _lock:
        if generation_id is not None and _registered.get("generation_id") != generation_id:
            return False
        lease = _registered.get("lease")
        _registered.update({"path": "", "rows": 0, "sig": None, "generation_id": None, "lease": None})
        _baked["sig"] = None
    if lease is not None:
        lease.release()
    return True


def shutdown_runtime() -> None:
    """Release the packed-plane lease and reset runtime probe state."""
    global _ACTIVE_BACKEND
    unregister_plane()
    with _lock:
        _baked["sig"] = None
        _probe.update({"at": 0.0, "ok": False})
        _ACTIVE_BACKEND = None


def registered_generation_id() -> str | None:
    with _lock:
        return _registered.get("generation_id")


def _http(url: str, payload=None, *, timeout: float) -> Optional[dict]:
    try:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"} if data else {},
            method="POST" if data is not None else "GET",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None


def _npu_url(path: str) -> str:
    """Build a local-services DME URL from the current deployment setting."""
    return f"{npu_service_base_url()}/dme/{str(path).lstrip('/')}"


def _npu_healthy(*, force: bool = False) -> bool:
    now = time.monotonic()
    with _lock:
        if not force and now - _probe["at"] < _PROBE_TTL_S:
            return bool(_probe["ok"])
    health = _http(_npu_url("packed/health"), timeout=1.0)
    ok = bool(health and health.get("ok"))
    with _lock:
        _probe["at"] = now
        _probe["ok"] = ok
    return ok


def _npu_overlaps(sram_rows: int, query: np.ndarray) -> Optional[np.ndarray]:
    """Score on the resident NPU plane, or None so the caller falls back."""
    if not _npu_enabled():
        return None
    with _lock:
        reg = dict(_registered)
        baked_sig = _baked["sig"]
    if not reg["path"] or reg["sig"] is None or reg["rows"] != sram_rows:
        return None
    if _stat_sig(reg["path"]) != reg["sig"]:
        # The file moved on since this index loaded; the caller's array is
        # older than the file, so numpy (which scores the array) is truth.
        return None
    if not _npu_healthy():
        return None

    if baked_sig != reg["sig"]:
        baked = _http(
            _npu_url("packed/plane"),
            {"path": reg["path"], "rows": reg["rows"]},
            timeout=_NPU_PLANE_LOAD_TIMEOUT_S,
        )
        if not (baked and baked.get("ok")):
            return None
        with _lock:
            _baked["sig"] = reg["sig"]

    payload = {
        "queries_b64": base64.b64encode(
            np.ascontiguousarray(query, dtype=np.uint64).tobytes()
        ).decode("ascii")
    }
    resp = _http(_npu_url("packed/overlap"), payload, timeout=10.0)
    if not (resp and resp.get("ok")):
        with _lock:
            _baked["sig"] = None  # server restarted or lost the plane; re-bake next call
        return None
    rows = resp.get("overlaps") or []
    if len(rows) != 1 or len(rows[0]) != sram_rows:
        return None
    return np.asarray(rows[0], dtype=np.int32)


def _entries_to_float_matrix(sram: np.ndarray) -> np.ndarray:
    validate_weight_entries(sram)
    if sram.size == 0:
        return np.zeros((0, PAYLOAD_BITS), dtype=np.float32)
    bits = np.unpackbits(sram.view(np.uint8), bitorder="little").reshape(
        len(sram), sram.shape[1] * 64
    )
    return bits[:, HEADER_WORDS * 64 :].astype(np.float32)


def _query_to_float_vector(query: np.ndarray) -> np.ndarray:
    validate_query_entry(query)
    bits = np.unpackbits(query.view(np.uint8), bitorder="little")
    return bits[HEADER_WORDS * 64 :].astype(np.float32)


def active_backend(*, force_probe: bool = False) -> Literal["numpy", "npu_packed"]:
    """The engine that would score right now (probed, not aspirational)."""
    global _ACTIVE_BACKEND
    if _ACTIVE_BACKEND is None or force_probe:
        _ACTIVE_BACKEND = (
            "npu_packed"
            if (_npu_enabled() and _npu_healthy(force=force_probe))
            else "numpy"
        )
    return _ACTIVE_BACKEND


def compute_overlaps(sram: np.ndarray, query: np.ndarray, *, generation_id: str | None = None) -> np.ndarray:
    global _ACTIVE_BACKEND
    validate_weight_entries(sram)
    validate_query_entry(query)
    if len(sram) == 0:
        return np.zeros(0, dtype=np.int32)
    if generation_id is not None:
        # Cut 13 (D-13-24): a view's scoring is asserted against the pinned registration —
        # the resident plane must be THIS generation's, never another's.
        with _lock:
            registered = _registered.get("generation_id")
        if registered is not None and registered != generation_id:
            from memory.projection.generation import GenerationIntegrityError

            raise GenerationIntegrityError(
                f"scorer registration is pinned to generation {registered[:12]}…, view is {generation_id[:12]}…")
    counts = _npu_overlaps(len(sram), query)
    if counts is not None:
        _ACTIVE_BACKEND = "npu_packed"
        return counts
    _ACTIVE_BACKEND = "numpy"
    return _compute_overlaps_numpy(sram, query)


def _compute_overlaps_numpy(sram: np.ndarray, query: np.ndarray) -> np.ndarray:
    q = _query_to_float_vector(query)
    m = _entries_to_float_matrix(sram)
    overlaps = m @ q
    return overlaps.astype(np.int32)


def query_popcount(query: np.ndarray) -> int:
    q = _query_to_float_vector(query)
    return int(q.sum())

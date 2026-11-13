"""
state_persistence.py ΓÇö guardian's sole-writer save layer for SharedState
=========================================================================

Per `project_guardian_sole_writer_doctrine` (2026-05-08):

> "Make a policy, a schema and config for how shared state works, how
> the data should be structured for each level etc and then we make a
> save state file in guardian. That's why everything gets routed
> through it."

This module is the ONLY writer to `guardian/state/save_state.json`. It
takes a snapshot of `core.shared_state.SharedState`, embeds a manifest
header, and atomically replaces the on-disk file. Per-key validation is
delegated to `SharedState.write` / `write_canonical` at write time (not
re-validated here against a separate schema file).

Loops never touch this file. They write to SharedState; the orchestrator
calls `save_now()` here after each loop completes (per the policy
`persistence.cadence` setting). Every save emits a phase event to
`guardian/logs/state_persistence.log` so the audit trail is grep-able.

Boot calls `restore_at_boot()` which reads the file (if it exists,
schema_version matches, and the snapshot is fresher than
`hydrate_max_age_seconds`) and rehydrates SharedState from it.

Architecturally:

    SharedState (in-memory)        ΓåÉ writes from every loop
        Γöé
        Γû╝
    state_persistence.save_now()   ΓåÉ guardian, only writer
        Γöé
        Γû╝
    guardian/state/save_state.json ΓåÉ the canonical save file
        Γöé
        Γû╝
    state_persistence.restore_at_boot()  ΓåÉ guardian, only reader
        Γöé
        Γû╝
    SharedState (in-memory)        ΓåÉ rehydrated for next boot
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

import logging

from core.paths import GUARDIAN_STATE_ROOT, OMEGA_ROOT

log = logging.getLogger("omega.guardian.state_persistence")


# --- paths ---
SAVE_PATH = GUARDIAN_STATE_ROOT / "save_state.json"
# Optional policy override; inline defaults apply when absent.
POLICY_PATH = OMEGA_ROOT / "policy" / "shared_state.policy.yaml"


# ΓöÇΓöÇΓöÇ policy load (lazy + cached) ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
_POLICY: Optional[Dict[str, Any]] = None
_POLICY_LOCK = threading.RLock()


def _load_policy() -> Dict[str, Any]:
    """Read policy/shared_state.policy.yaml. Cached after first call."""
    global _POLICY
    if _POLICY is not None:
        return _POLICY
    with _POLICY_LOCK:
        if _POLICY is not None:
            return _POLICY
        try:
            import yaml  # type: ignore
        except ImportError:
            log.warning("state_persistence: PyYAML not installed; falling back to inline defaults")
            _emit_persist_signal(
                status="degraded",
                failure_class="pyyaml_missing_default_policy",
                operation="persist",
            )
            _POLICY = _default_policy()
            return _POLICY
        try:
            with open(POLICY_PATH, "r", encoding="utf-8") as f:
                _POLICY = yaml.safe_load(f) or {}
        except OSError as exc:
            log.warning("state_persistence: policy read failed (%s); using defaults", exc)
            _emit_persist_signal(
                status="degraded",
                failure_class="policy_read_failed_default_policy",
                operation="persist",
                error_type=type(exc).__name__,
                reason=str(exc)[:120],
            )
            _POLICY = _default_policy()
        return _POLICY


def _default_policy() -> Dict[str, Any]:
    """Inline fallback if policy/shared_state.policy.yaml is missing."""
    return {
        "version": 1,
        "persistence": {
            "atomic": True,
            "cadence": "post_loop",
            "min_interval_seconds": 0.5,
            "retention_snapshots": 1,
            "restore_at_boot": True,
            "hydrate_max_age_seconds": 21600,
        },
        "validation": {
            "on_canonical_shape_violation": "raise",
            "on_unknown_key_write": "allow",
        },
        "audit": {
            "emit_phase": True,
            "emit_restore_phase": True,
            "embed_manifest": True,
        },
        "keys": {
            "conversation_state": {"persist": False},
        },
    }


# ΓöÇΓöÇΓöÇ debounce: enforce min_interval_seconds ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
_last_save_ts: float = 0.0
_save_lock = threading.RLock()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _emit_persist_signal(*, status: str, failure_class: str, operation: str, **fields: Any) -> None:
    """Structured failure/degrade surface for the sole-writer save
    layer. Lazy import of terminal_feedback (this module already lazy-
    imports emit_phase in its audit blocks). Best-effort ΓÇö a failure
    to emit must never affect persistence behaviour."""
    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "guardian",
            operation,
            status=status,
            summary=f"state_persistence {operation} {status}: {failure_class}",
            component="state_persistence",
            operation=operation,
            failure_class=failure_class,
            **fields,
        )
    except Exception as exc:
        log.debug("state_persistence: structured emit failed: %s", exc)


def _should_save_now() -> bool:
    """Honor policy.persistence.min_interval_seconds ΓÇö coalesce
    rapid-fire save calls down to one write per debounce window."""
    import time

    policy = _load_policy()
    min_interval = float(policy.get("persistence", {}).get("min_interval_seconds", 0.5))
    if min_interval <= 0:
        return True
    with _save_lock:
        global _last_save_ts
        now = time.time()
        if now - _last_save_ts < min_interval:
            return False
        _last_save_ts = now
        return True


# ΓöÇΓöÇΓöÇ manifest header ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
def _build_manifest(cycle_id: str = "") -> Dict[str, Any]:
    """Capture writer fingerprint at save time so post-mortem can
    identify which boot wrote this snapshot."""
    import sys

    git_sha = ""
    try:
        # Prefer the cached fingerprint that boot.py captured at start
        from boot import _version_fingerprint  # type: ignore

        fp = _version_fingerprint()
        git_sha = str(fp.get("git", ""))
    except Exception:
        pass
    return {
        "git": git_sha or "unknown",
        "python": ".".join(map(str, sys.version_info[:3])),
        "cycle_id": cycle_id or "",
    }


# ΓöÇΓöÇΓöÇ filter: which keys to persist ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
def _persisted_keys(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Filter `snapshot` to the keys allowed on durable storage.

    Phase 2 ┬ºP2-S17 layered filter (executed in order, all-must-allow):

      1. **Classification filter** ΓÇö only keys classified as
         `DURABLE_PROJECTION` in `core.shared_state_keys` qualify.
         TICK_BOUNDARY_PROJECTION, EPHEMERAL_RUNTIME, and
         FORBIDDEN_TO_PERSIST keys are dropped here. Unknown / ad-hoc
         keys not in the canonical registry default to EPHEMERAL and
         therefore also drop ΓÇö this is the safe-by-default rule per
         PACKET_P2_S17.md RB#2 (no unclassified key reaches disk).

      2. **Forbidden-payload shape filter** ΓÇö even when a key is
         classified durable, its value is walked for forbidden
         payload tokens (raw_model_output / embeddings / image_bytes /
         audio_bytes / video_bytes / stack_trace / etc.). A match
         drops the value AND logs at WARN so a substrate observer
         sees the raw-payload-leak attempt.

      3. **Policy override** ΓÇö the legacy
         `policy/shared_state.policy.yaml` `keys[name].persist` switch
         still applies for callers that want to opt OUT of persistence
         on a specific durable key (defaults to True). Cannot opt IN
         a non-durable key ΓÇö classification is the authority.
    """
    from core.shared_state_keys import (
        is_persistable,
        value_contains_forbidden_payload,
    )

    policy = _load_policy()
    overrides = policy.get("keys", {}) or {}
    out: Dict[str, Any] = {}
    for k, v in snapshot.items():
        # (1) classification filter ΓÇö durable_projection only
        if not is_persistable(k):
            continue
        # (2) forbidden-payload shape filter
        offending = value_contains_forbidden_payload(v)
        if offending is not None:
            log.warning(
                "state_persistence: refused to persist key=%r ΓÇö value "
                "contains forbidden payload field %r (raw blob / "
                "embedding / stack trace / image-audio bytes). "
                "substrate raw-payload sensor is degraded",
                k,
                offending,
            )
            continue
        # (3) legacy policy override (durable-by-default unless
        #     explicitly opted out via policy/shared_state.policy.yaml)
        cfg = overrides.get(k, {})
        if not cfg.get("persist", True):
            continue
        out[k] = v
    return out


# ΓöÇΓöÇΓöÇ save ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
@dataclass(frozen=True)
class SaveResult:
    ok: bool
    keys_saved: int
    bytes_written: int
    elapsed_ms: float
    error_kind: str = ""


def save_now(
    shared_state: Any,
    *,
    cycle_id: str = "",
    force: bool = False,
) -> SaveResult:
    """Atomically snapshot SharedState to guardian/state/save_state.json.

    Args:
        shared_state: a `core.shared_state.SharedState` instance (duck-
                      typed to expose `.snapshot() -> dict`).
        cycle_id:     the cycle_id of the loop that just completed.
                      Embedded in the manifest for audit.
        force:        skip the debounce window. Use only on shutdown.

    Returns:
        SaveResult ΓÇö `.ok=False` on any failure (file system, schema,
        etc.). Does NOT raise ΓÇö guardian's job is to be the immune
        system; persistence failures are logged but don't crash loops.
    """
    import time

    t0 = time.time()

    if not force and not _should_save_now():
        return SaveResult(
            ok=True,
            keys_saved=0,
            bytes_written=0,
            elapsed_ms=0.0,
            error_kind="debounced",
        )

    try:
        snapshot = shared_state.snapshot() if shared_state is not None else {}
    except Exception as exc:
        log.warning("state_persistence: snapshot failed: %s", exc)
        _emit_persist_signal(
            status="fail",
            failure_class="snapshot_capture_failed",
            operation="persist",
            error_type=type(exc).__name__,
            cycle_id=cycle_id,
            reason=str(exc)[:120],
        )
        return SaveResult(
            ok=False,
            keys_saved=0,
            bytes_written=0,
            elapsed_ms=(time.time() - t0) * 1000.0,
            error_kind=type(exc).__name__,
        )

    persisted = _persisted_keys(snapshot)

    payload = {
        "schema_version": int(_load_policy().get("version", 1)),
        "saved_at": _now_iso(),
        "writer_fingerprint": _build_manifest(cycle_id),
        "data": persisted,
    }

    try:
        SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, indent=2, ensure_ascii=False)
        if _load_policy().get("persistence", {}).get("atomic", True):
            tmp = SAVE_PATH.with_suffix(".json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            tmp.replace(SAVE_PATH)
        else:
            with open(SAVE_PATH, "w", encoding="utf-8") as f:
                f.write(body)
        bytes_written = len(body.encode("utf-8"))
        elapsed_ms = (time.time() - t0) * 1000.0
    except OSError as exc:
        log.error("state_persistence: save failed: %s", exc)
        _emit_persist_signal(
            status="fail",
            failure_class="save_write_failed",
            operation="persist",
            error_type=type(exc).__name__,
            cycle_id=cycle_id,
            reason=str(exc)[:120],
        )
        return SaveResult(
            ok=False,
            keys_saved=0,
            bytes_written=0,
            elapsed_ms=(time.time() - t0) * 1000.0,
            error_kind=type(exc).__name__,
        )

    # Audit emit
    if _load_policy().get("audit", {}).get("emit_phase", True):
        try:
            from guardian.terminal_feedback import emit_phase

            emit_phase(
                "guardian",
                "state_persistence",
                status="ok",
                cycle_id=cycle_id,
                summary=(
                    f"keys={len(persisted)} bytes={bytes_written} elapsed_ms={elapsed_ms:.1f}"
                ),
            )
        except Exception:
            pass

    return SaveResult(
        ok=True,
        keys_saved=len(persisted),
        bytes_written=bytes_written,
        elapsed_ms=elapsed_ms,
    )


# ΓöÇΓöÇΓöÇ restore ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ


@dataclass(frozen=True)
class RestoreReport:
    """Phase 2 ┬ºP2-S17 ΓÇö structured restore result.

    Per PACKET_P2_S17.md RB#4: "Corrupt or schema-invalid bus
    snapshots must not crash boot. The restore result must report
    missing/corrupt keys and whether pressure evidence was
    degraded."

    Fields:
        keys_restored:      count of keys successfully written into
                            SharedState.
        keys_dropped_stale: count of keys that failed write
                            (corrupt shape / validator rejection).
        ok:                 True iff no fatal degradation; matches the
                            tick-state WAL `RestoreReport.ok` contract.
        cold_start:         True when no save file existed or the file
                            was rejected wholesale (e.g., schema-version
                            mismatch, age past max). Pressure evidence
                            IS degraded on cold start.
        degradation_reason: short string naming the cold-start cause
                            ("snapshot_unreadable" / "schema_version_mismatch"
                            / "max_age_exceeded" / "no_save_file" / ""
                            on clean restore).
        corrupt_keys:       list of canonical key names whose writes
                            failed during restore (degradation visible).
        pressure_evidence_degraded:  True if any durable-projection key
                            failed to restore OR cold-started. The
                            substrate's first post-restart pressure
                            evaluation reads this flag.
        saved_at_iso:       ISO timestamp of the snapshot read, or ""
                            on cold start.
    """

    keys_restored: int = 0
    keys_dropped_stale: int = 0
    ok: bool = True
    cold_start: bool = False
    degradation_reason: str = ""
    corrupt_keys: List[str] = field(default_factory=list)
    pressure_evidence_degraded: bool = False
    saved_at_iso: str = ""

    # Legacy callers that destructure as `(restored, dropped) =
    # restore_at_boot(...)` keep working ΓÇö iteration yields the same
    # two ints in the same order.
    def __iter__(self):
        yield self.keys_restored
        yield self.keys_dropped_stale

    def __getitem__(self, idx: int) -> int:
        if idx == 0:
            return self.keys_restored
        if idx == 1:
            return self.keys_dropped_stale
        raise IndexError(idx)


def restore_at_boot(shared_state: Any) -> "RestoreReport":
    """Hydrate SharedState from the save file at boot.

    Returns a `RestoreReport` (P2-S17). Legacy callers that
    destructured the return as `(restored, dropped)` continue to work
    via the report's `__iter__` / `__getitem__` shim ΓÇö the report
    yields its `keys_restored` and `keys_dropped_stale` ints in the
    same order the old tuple did.

    Never raises. Corrupt snapshots, schema mismatches, and key-level
    write failures all degrade soft and surface in the returned
    report's `degradation_reason` / `corrupt_keys` /
    `pressure_evidence_degraded` fields so the boot loop + the
    substrate's first pressure evaluation can see the degradation.
    """
    policy = _load_policy()
    if not policy.get("persistence", {}).get("restore_at_boot", True):
        return RestoreReport(
            cold_start=True,
            degradation_reason="restore_disabled_by_policy",
            pressure_evidence_degraded=True,
        )
    if not SAVE_PATH.exists():
        return RestoreReport(
            cold_start=True,
            degradation_reason="no_save_file",
            pressure_evidence_degraded=True,
        )

    try:
        with open(SAVE_PATH, "r", encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(
            "state_persistence: restore read failed (%s); cold start ΓÇö "
            "substrate restart-integrity sensor is degraded",
            exc,
        )
        _emit_persist_signal(
            status="degraded",
            failure_class="restore_read_failed_cold_start",
            operation="recover",
            error_type=type(exc).__name__,
            reason=str(exc)[:120],
        )
        return RestoreReport(
            cold_start=True,
            degradation_reason="snapshot_unreadable",
            pressure_evidence_degraded=True,
        )

    saved_version = int(payload.get("schema_version", 0))
    cur_version = int(policy.get("version", 1))
    if saved_version != cur_version:
        log.warning(
            "state_persistence: save schema_version=%d != current=%d; "
            "cold start ΓÇö substrate restart-integrity sensor is degraded",
            saved_version,
            cur_version,
        )
        _emit_persist_signal(
            status="degraded",
            failure_class="restore_schema_version_mismatch",
            operation="recover",
            saved_version=saved_version,
            current_version=cur_version,
            reason="schema_version_mismatch_cold_start",
        )
        return RestoreReport(
            cold_start=True,
            degradation_reason="schema_version_mismatch",
            pressure_evidence_degraded=True,
        )

    saved_at = str(payload.get("saved_at", "") or "")
    keys_restored = 0
    keys_dropped_stale = 0
    corrupt_keys: List[str] = []

    max_age = float(policy.get("persistence", {}).get("hydrate_max_age_seconds", 21600))
    if max_age > 0 and saved_at:
        try:
            saved_dt = datetime.fromisoformat(saved_at)
            age = (datetime.now(timezone.utc) - saved_dt).total_seconds()
            if age > max_age:
                log.info(
                    "state_persistence: snapshot is %.1fh old > max_age=%.1fh; cold start",
                    age / 3600.0,
                    max_age / 3600.0,
                )
                return RestoreReport(
                    cold_start=True,
                    degradation_reason="max_age_exceeded",
                    pressure_evidence_degraded=True,
                    saved_at_iso=saved_at,
                )
        except ValueError:
            pass

    data = payload.get("data") or {}
    if not isinstance(data, dict):
        log.warning(
            "state_persistence: restore payload.data is not a dict "
            "(%s); cold start ΓÇö substrate restart-integrity sensor is "
            "degraded",
            type(data).__name__,
        )
        return RestoreReport(
            cold_start=True,
            degradation_reason="payload_data_not_dict",
            pressure_evidence_degraded=True,
            saved_at_iso=saved_at,
        )
    from core.shared_state_keys import KEY_REGISTRY, validate_write

    for key, value in data.items():
        # Canonical-shape gate (P2-S17 RB#4). The save side only persists
        # canonical durable keys (see _persisted_keys), so a canonical key
        # whose on-disk value fails its registry shape validator is a
        # corrupt / forward-drifted snapshot (hand-edited save file, partial
        # write, schema downgrade). Drop it — never rehydrate a malformed
        # value into the inter-loop wire — and record the degradation so the
        # substrate's first post-restart pressure evaluation sees it. The
        # old path wrote everything through the permissive write, so shape
        # violations were silently accepted and corrupt_keys never populated.
        # Non-canonical / ad-hoc keys stay on the permissive write below.
        if key in KEY_REGISTRY:
            shape_err = validate_write(key, value)
            if shape_err is not None:
                log.warning(
                    "state_persistence: restore of %r dropped ΓÇö value fails "
                    "canonical shape (%s); substrate sees missing durable "
                    "evidence",
                    key,
                    shape_err,
                )
                _emit_persist_signal(
                    status="degraded",
                    failure_class="restore_key_shape_invalid",
                    operation="recover",
                    state_key=str(key)[:60],
                    reason=str(shape_err)[:120],
                )
                keys_dropped_stale += 1
                corrupt_keys.append(str(key)[:80])
                continue
        try:
            shared_state.write(key, value)
            keys_restored += 1
        except Exception as exc:
            log.warning(
                "state_persistence: restore-write of %r failed (%s) ΓÇö "
                "key dropped, substrate sees missing durable evidence",
                key,
                exc,
            )
            _emit_persist_signal(
                status="degraded",
                failure_class="restore_key_write_failed",
                operation="recover",
                error_type=type(exc).__name__,
                state_key=str(key)[:60],
                reason=str(exc)[:120],
            )
            keys_dropped_stale += 1
            corrupt_keys.append(str(key)[:80])

    if policy.get("audit", {}).get("emit_restore_phase", True):
        try:
            from guardian.terminal_feedback import emit_phase

            emit_phase(
                "guardian",
                "state_restore",
                status="ok",
                summary=(
                    f"restored={keys_restored} dropped={keys_dropped_stale} saved_at={saved_at}"
                ),
            )
        except Exception:
            pass

    return RestoreReport(
        keys_restored=keys_restored,
        keys_dropped_stale=keys_dropped_stale,
        ok=(keys_dropped_stale == 0),
        cold_start=False,
        degradation_reason=("key_write_failures" if keys_dropped_stale > 0 else ""),
        corrupt_keys=corrupt_keys,
        pressure_evidence_degraded=(keys_dropped_stale > 0),
        saved_at_iso=saved_at,
    )


# ΓöÇΓöÇΓöÇ reset (called by day_zero_reset) ΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇΓöÇ
def reset_save_state() -> bool:
    """Truncate the save state file. Called by day_zero_reset.

    Returns True if the file existed and was reset, False if it
    didn't exist.
    """
    if not SAVE_PATH.exists():
        SAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
        return False
    try:
        SAVE_PATH.unlink()
        return True
    except OSError as exc:
        log.warning("state_persistence: reset failed (%s)", exc)
        _emit_persist_signal(
            status="fail",
            failure_class="reset_unlink_failed",
            operation="recover",
            error_type=type(exc).__name__,
            reason=str(exc)[:120],
        )
        return False

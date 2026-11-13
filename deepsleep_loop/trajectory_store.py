"""DeepSleep trajectory store (Slice 6c) — file-backed JSON, fail-hard."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from deepsleep_loop._deepsleep_state import TrajectoryRecord
from deepsleep_loop.store_locks import TRAJECTORY, guarded

_LOG = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


def trajectory_key(target_domain: str, target_capability: str) -> str:
    return f"{target_domain}::{target_capability}"


@guarded(TRAJECTORY)
def load_trajectory(path: str | Path | None = None) -> dict[str, TrajectoryRecord]:
    """Load all TrajectoryRecords keyed by target.

    Fail-hard: the trajectory store is a continuity structure. A MISSING file
    (first boot) is absence, not failure → returns {}. A parse/decode/schema
    failure on an EXISTING file is corruption → it propagates and CRASHES."""
    from pressure_loop.policy import deepsleep_trajectory_path

    resolved = _resolve_path(path or deepsleep_trajectory_path())
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"trajectory store {resolved} must contain an object")

    out: dict[str, TrajectoryRecord] = {}
    for key, payload in raw.items():
        if not isinstance(payload, dict):
            raise ValueError(f"trajectory store {resolved} row {key!r} must be an object")
        record = TrajectoryRecord.from_dict(payload)
        out[str(key)] = record
    return out


@guarded(TRAJECTORY)
def save_trajectory(store: dict[str, TrajectoryRecord], path: str | Path | None = None) -> None:
    """Write the full trajectory store.

    Fail-hard: a trajectory write failure is a continuity write failure → crash.
    """
    from pressure_loop.policy import deepsleep_trajectory_path

    resolved = _resolve_path(path or deepsleep_trajectory_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = {k: v.to_dict() for k, v in store.items()}
    from pressure_loop.durable_write import write_text_atomic

    write_text_atomic(
        resolved,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


@guarded(TRAJECTORY)
def upsert_trajectory(record: TrajectoryRecord, path: str | Path | None = None) -> None:
    """Read-modify-write one TrajectoryRecord.

    Fail-hard: a continuity read-modify-write failure propagates and crashes.
    """
    from pressure_loop.policy import deepsleep_trajectory_path

    resolved = _resolve_path(path or deepsleep_trajectory_path())
    store = load_trajectory(resolved)
    key = trajectory_key(record.target_domain, record.target_capability)
    store[key] = record
    save_trajectory(store, path=resolved)

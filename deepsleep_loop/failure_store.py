"""DeepSleep failure pattern store (Slice 6c) — file-backed JSON, fail-hard."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from deepsleep_loop._deepsleep_state import FailurePattern
from deepsleep_loop.store_locks import FAILURE, guarded

_LOG = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


def failure_key(kind: str, context: str) -> str:
    return f"{kind}::{context}"


@guarded(FAILURE)
def load_failures(path: str | Path | None = None) -> dict[str, FailurePattern]:
    """Load all FailurePatterns keyed by kind+context.

    Fail-hard: a MISSING file (first boot) is absence → {}. A parse/decode/schema
    failure on an EXISTING file is corrupt consolidation data → CRASH."""
    from pressure_loop.policy import deepsleep_failure_path

    resolved = _resolve_path(path or deepsleep_failure_path())
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"failure store {resolved} must contain an object")

    out: dict[str, FailurePattern] = {}
    for key, payload in raw.items():
        if not isinstance(payload, dict):
            raise ValueError(f"failure store {resolved} row {key!r} must be an object")
        pattern = FailurePattern.from_dict(payload)
        out[str(key)] = pattern
    return out


@guarded(FAILURE)
def upsert_failure(pattern: FailurePattern, path: str | Path | None = None) -> None:
    """Read-modify-write one FailurePattern.

    Fail-hard: a failure-pattern write failure CRASHES — learned failure history
    must not be silently dropped."""
    from pressure_loop.policy import deepsleep_failure_path

    resolved = _resolve_path(path or deepsleep_failure_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)

    store = load_failures(resolved)
    key = failure_key(pattern.kind, pattern.context)
    store[key] = pattern

    payload = {k: v.to_dict() for k, v in store.items()}
    from pressure_loop.durable_write import write_text_atomic

    write_text_atomic(
        resolved,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

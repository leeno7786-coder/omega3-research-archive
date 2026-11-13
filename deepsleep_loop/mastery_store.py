"""DeepSleep mastery store (Slice 6b) — file-backed JSON, fail-hard."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from deepsleep_loop._deepsleep_state import MasteryRecord
from deepsleep_loop.store_locks import MASTERY, guarded

_LOG = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


def normalize_domain_id(target: str) -> str:
    """Normalize a human topic into a stable domain_id key (matches engine _slug)."""
    from knowledge_extractor.derive.run import _slug

    return _slug(str(target or ""))


def status_for(knowledge_mastery: float) -> str:
    """Map knowledge_mastery to learning / operational / mastered."""
    from pressure_loop.policy import deepsleep_mastery_status_thresholds

    thresholds = deepsleep_mastery_status_thresholds()
    km = float(knowledge_mastery)
    operational = float(thresholds.get("operational_max", 0.80))
    learning = float(thresholds.get("learning_max", 0.60))
    if km >= operational:
        return "mastered"
    if km >= learning:
        return "operational"
    return "learning"


@guarded(MASTERY)
def load_mastery(path: str | Path | None = None) -> dict[str, MasteryRecord]:
    """Load all MasteryRecords keyed by domain_id.

    Fail-hard: a MISSING file (first boot) is absence → {}. A parse/decode/schema
    failure on an EXISTING file is corrupt consolidation data → CRASH (mastery is
    the single source of truth)."""
    from pressure_loop.policy import deepsleep_mastery_path

    resolved = _resolve_path(path or deepsleep_mastery_path())
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"mastery store {resolved} must contain an object")

    out: dict[str, MasteryRecord] = {}
    for key, payload in raw.items():
        if not isinstance(payload, dict):
            raise ValueError(f"mastery store {resolved} row {key!r} must be an object")
        record = MasteryRecord.from_dict(payload)
        domain_id = str(record.domain_id or key)
        out[domain_id] = record
    return out


@guarded(MASTERY)
def upsert_mastery(record: MasteryRecord, path: str | Path | None = None) -> None:
    """Read-modify-write one MasteryRecord into the store.

    Fail-hard: a mastery write failure CRASHES — a success-credit update must not
    be silently lost (mastery is the single source of truth)."""
    from pressure_loop.policy import deepsleep_mastery_path

    resolved = _resolve_path(path or deepsleep_mastery_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)

    store = load_mastery(resolved)
    domain_id = str(record.domain_id)
    store[domain_id] = record

    payload = {k: v.to_dict() for k, v in store.items()}
    from pressure_loop.durable_write import write_text_atomic

    write_text_atomic(
        resolved,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

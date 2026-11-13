"""DeepSleep semantic store (Slice 6c) — file-backed JSON, fail-hard."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from deepsleep_loop._deepsleep_state import SemanticEntry
from deepsleep_loop.store_locks import SEMANTIC, guarded

_LOG = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


def semantic_key(domain: str, statement: str) -> str:
    return f"{domain}::{statement}"


@guarded(SEMANTIC)
def load_semantic(path: str | Path | None = None) -> dict[str, SemanticEntry]:
    """Load all SemanticEntries keyed by domain+statement.

    Fail-hard: a MISSING file (first boot) is absence → {}. A parse/decode/schema
    failure on an EXISTING file is corrupt consolidation data → CRASH."""
    from pressure_loop.policy import deepsleep_semantic_path

    resolved = _resolve_path(path or deepsleep_semantic_path())
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"semantic store {resolved} must contain an object")

    out: dict[str, SemanticEntry] = {}
    for key, payload in raw.items():
        if not isinstance(payload, dict):
            raise ValueError(f"semantic store {resolved} row {key!r} must be an object")
        entry = SemanticEntry.from_dict(payload)
        out[str(key)] = entry
    return out


@guarded(SEMANTIC)
def upsert_semantic(entry: SemanticEntry, path: str | Path | None = None) -> None:
    """Insert one SemanticEntry if not already present (idempotent).

    Fail-hard: a semantic write failure CRASHES — promoted knowledge must not be
    silently dropped into the void."""
    from pressure_loop.policy import deepsleep_semantic_path

    resolved = _resolve_path(path or deepsleep_semantic_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)

    store = load_semantic(resolved)
    key = semantic_key(entry.domain, entry.statement)
    if key in store:
        return

    store[key] = entry
    payload = {k: v.to_dict() for k, v in store.items()}
    from pressure_loop.durable_write import write_text_atomic

    write_text_atomic(
        resolved,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )

"""DeepSleep open query store (Slice 6d) — file-backed JSON, failure-soft."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from deepsleep_loop._deepsleep_state import OpenQuery
from deepsleep_loop.store_locks import QUERY, guarded

_LOG = logging.getLogger(__name__)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _resolve_path(path: str | Path) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = _repo_root() / p
    return p


def query_key(question: str, domain: str) -> str:
    """Stable dedup key for (question, domain)."""
    cleaned_q = re.sub(r"\s+", " ", str(question or "").strip().lower())
    cleaned_d = re.sub(r"\s+", " ", str(domain or "").strip().lower())
    return f"{cleaned_d}::{cleaned_q}"


@guarded(QUERY)
def load_queries(path: str | Path | None = None) -> dict[str, OpenQuery]:
    """Load all OpenQueries keyed by query_id.

    Fail-hard: a MISSING file (first boot) is absence → {}. A parse/decode/schema
    failure on an EXISTING file is corrupt continuity data → CRASH."""
    from pressure_loop.policy import deepsleep_query_path

    resolved = _resolve_path(path or deepsleep_query_path())
    if not resolved.is_file():
        return {}

    raw = json.loads(resolved.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return {}

    out: dict[str, OpenQuery] = {}
    for key, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        query = OpenQuery.from_dict(payload)
        out[str(query.query_id or key)] = query
    return out


@guarded(QUERY)
def upsert_query(query: OpenQuery, path: str | Path | None = None) -> None:
    """Read-modify-write one OpenQuery into the store.

    Fail-hard: an open-query write failure CRASHES — the mind's outstanding
    questions must not be silently dropped."""
    from pressure_loop.policy import deepsleep_query_path

    resolved = _resolve_path(path or deepsleep_query_path())
    resolved.parent.mkdir(parents=True, exist_ok=True)

    store = load_queries(resolved)
    store[str(query.query_id)] = query

    payload = {k: v.to_dict() for k, v in store.items()}
    from pressure_loop.durable_write import write_text_atomic

    write_text_atomic(
        resolved,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )


def open_queries(path: str | Path | None = None) -> list[OpenQuery]:
    """Return queries with status open (non-archived active queries)."""
    return [q for q in load_queries(path).values() if q.status == "open"]

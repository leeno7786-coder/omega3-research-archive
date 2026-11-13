"""DeepSleep Phase 2 — open query lifecycle from omega signals."""

from __future__ import annotations

from typing import Any

from deepsleep_loop._deepsleep_state import DeepSleepInput, OpenQuery
from deepsleep_loop.query_store import load_queries, query_key, upsert_query
from deepsleep_loop.store_locks import QUERY, store_transaction

_RESOLVED_STATUSES = frozenset({"resolved", "abandoned", "superseded"})
_STALE_OPEN_QUERY_DAYS = 30


def _now_iso() -> str:
    from core.symphony_time import now_iso

    return now_iso()


def _existing_open_by_key(store: dict[str, OpenQuery]) -> dict[str, OpenQuery]:
    out: dict[str, OpenQuery] = {}
    for query in store.values():
        if query.status != "open":
            continue
        out[query_key(query.question, query.domain)] = query
    return out


def _create_queries(
    ds_input: DeepSleepInput,
    store: dict[str, OpenQuery],
) -> list[OpenQuery]:
    omega = ds_input.omega_outcome or {}
    reflection = dict(omega.get("reflection") or {})
    open_by_key = _existing_open_by_key(store)
    created: list[OpenQuery] = []

    for item in reflection.get("open_questions") or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or "").strip()
        domain = str(item.get("domain") or "").strip()
        if not question:
            continue
        key = query_key(question, domain)
        if key in open_by_key:
            continue
        try:
            confidence = max(0.0, min(1.0, float(item.get("confidence", 0.5) or 0.5)))
        except (TypeError, ValueError):
            confidence = 0.5
        query = OpenQuery(
            query_id=key,
            question=question,
            domain=domain,
            created_episode=ds_input.episode_id,
            confidence=confidence,
            status="open",
            created_timestamp=_now_iso(),
        )
        store[query.query_id] = query
        open_by_key[key] = query
        created.append(query)

    return created


def _apply_resolutions(
    ds_input: DeepSleepInput,
    store: dict[str, OpenQuery],
) -> list[OpenQuery]:
    omega = ds_input.omega_outcome or {}
    resolutions = omega.get("query_resolutions")
    if not isinstance(resolutions, list):
        reflection = dict(omega.get("reflection") or {})
        resolutions = reflection.get("query_resolutions")
    if not isinstance(resolutions, list):
        return []

    updated: list[OpenQuery] = []
    for item in resolutions:
        if not isinstance(item, dict):
            continue
        query_id = str(item.get("query_id") or "").strip()
        if not query_id or query_id not in store:
            continue
        status = str(item.get("status") or "resolved").strip().lower()
        if status not in _RESOLVED_STATUSES:
            status = "resolved"
        prior = store[query_id]
        query = OpenQuery(
            query_id=prior.query_id,
            question=prior.question,
            domain=prior.domain,
            created_episode=prior.created_episode,
            confidence=prior.confidence,
            status=status,
            created_timestamp=prior.created_timestamp,
            resolution_episode=ds_input.episode_id,
            resolution_reason=str(item.get("reason") or item.get("resolution_reason") or ""),
        )
        store[query_id] = query
        updated.append(query)
    return updated


def _sync_pressure_resolutions(
    ds_input: DeepSleepInput,
    store: dict[str, OpenQuery],
) -> list[OpenQuery]:
    """Resolve deepsleep OpenQueries when the live pressure_loop journal closed them."""
    try:
        from pressure_loop.open_questions import (
            STATUS_RESOLVED,
            STATUS_SUPERSEDED,
            _latest_per_id,
            _load_all,
        )
    except Exception:
        return []

    updated: list[OpenQuery] = []
    latest = _latest_per_id(_load_all())
    for question in latest.values():
        if question.status not in (STATUS_RESOLVED, STATUS_SUPERSEDED):
            continue
        qtext = str(getattr(question, "question_text", "") or "").strip()
        if not qtext:
            continue
        domain = str((getattr(question, "memory", None) or {}).get("domain") or "")
        key = query_key(qtext, domain)
        prior = store.get(key)
        if prior is None or prior.status != "open":
            continue
        status = "resolved" if question.status == STATUS_RESOLVED else "superseded"
        resolved = OpenQuery(
            query_id=prior.query_id,
            question=prior.question,
            domain=prior.domain,
            created_episode=prior.created_episode,
            confidence=prior.confidence,
            status=status,
            created_timestamp=prior.created_timestamp,
            resolution_episode=ds_input.episode_id,
            resolution_reason=str(
                getattr(question, "resolved_note", "") or "pressure_open_questions"
            ),
        )
        store[key] = resolved
        updated.append(resolved)
    return updated


def _abandon_stale_open_queries(
    ds_input: DeepSleepInput,
    store: dict[str, OpenQuery],
) -> list[OpenQuery]:
    """Auto-abandon deepsleep OpenQueries left unresolved beyond the age window."""
    from datetime import datetime, timezone

    updated: list[OpenQuery] = []
    now = datetime.now(timezone.utc)
    for prior in list(store.values()):
        if prior.status != "open":
            continue
        ts = str(prior.created_timestamp or "").strip()
        if not ts:
            continue
        try:
            created = datetime.fromisoformat(ts)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - created).days < _STALE_OPEN_QUERY_DAYS:
            continue
        abandoned = OpenQuery(
            query_id=prior.query_id,
            question=prior.question,
            domain=prior.domain,
            created_episode=prior.created_episode,
            confidence=prior.confidence,
            status="abandoned",
            created_timestamp=prior.created_timestamp,
            resolution_episode=ds_input.episode_id,
            resolution_reason="auto_archive_stale",
        )
        store[prior.query_id] = abandoned
        updated.append(abandoned)
    return updated


def _archive_resolved(store: dict[str, OpenQuery]) -> list[OpenQuery]:
    archived: list[OpenQuery] = []
    for query_id, query in list(store.items()):
        if query.status not in _RESOLVED_STATUSES:
            continue
        archived_query = OpenQuery(
            query_id=query.query_id,
            question=query.question,
            domain=query.domain,
            created_episode=query.created_episode,
            confidence=query.confidence,
            status="archived",
            created_timestamp=query.created_timestamp,
            resolution_episode=query.resolution_episode,
            resolution_reason=query.resolution_reason,
        )
        store[query_id] = archived_query
        archived.append(archived_query)
    return archived


def query_effects_from_queries(
    queries: list[OpenQuery],
    *,
    prior: dict[str, OpenQuery] | None,
) -> list[dict[str, Any]]:
    """Build journal query_effects from query lifecycle changes."""
    prior = prior or {}
    effects: list[dict[str, Any]] = []
    seen: set[str] = set()
    for query in queries:
        if query.query_id in seen:
            continue
        seen.add(query.query_id)
        old = prior.get(query.query_id)
        if old is None:
            effects.append(
                {
                    "query_id": query.query_id,
                    "status": query.status,
                    "question": query.question,
                    "domain": query.domain,
                    "action": "created",
                }
            )
            continue
        if old.status != query.status:
            effects.append(
                {
                    "query_id": query.query_id,
                    "status": query.status,
                    "question": query.question,
                    "domain": query.domain,
                    "action": "updated",
                    "prior_status": old.status,
                }
            )
    return effects


def run_query_lifecycle(
    ds_input: DeepSleepInput,
    *,
    query_path: Any = None,
) -> list[OpenQuery]:
    """Create, resolve, and archive open queries.

    Fail-hard: the open-query store is continuity substrate. A load/write failure
    propagates and CRASHES — it is not swallowed to []."""
    from pressure_loop.policy import deepsleep_query_path

    resolved_path = query_path or deepsleep_query_path()
    # Phase 4 cut 13 review: ONE transaction. Every lifecycle step below
    # mutates the map loaded here and the loop at the bottom writes the whole
    # thing back key by key; with lane-local deepsleep (cut 13) a second lane
    # inside the same span reinstates the queries this one just archived.
    with store_transaction(QUERY):
        store = load_queries(resolved_path)

        created = _create_queries(ds_input, store)
        resolved = _apply_resolutions(ds_input, store)
        resolved.extend(_sync_pressure_resolutions(ds_input, store))
        resolved.extend(_abandon_stale_open_queries(ds_input, store))
        archived = _archive_resolved(store)

        changed: list[OpenQuery] = []
        seen: set[str] = set()
        for query in created + resolved + archived:
            if query.query_id in seen:
                continue
            seen.add(query.query_id)
            changed.append(query)

        for query in store.values():
            upsert_query(query, path=resolved_path)

    return changed

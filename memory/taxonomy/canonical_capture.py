"""Stage 1: canonical episodic capture.

Re-points the episode-close capture hook from the append-only taxonomy ledger into
canonical Dewey memory via direct, Guardian-free writes.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from memory.taxonomy import adapters
from memory.dewey_map import SITUATIONAL_FACETS
from memory.enrichment.situational import enrich_situational
from pressure_loop.temporal_store import (
    write_episode_to_temporal,
    write_consolidation_to_temporal,
    write_capability_to_temporal,
)


# Event kinds that are pipeline plumbing, not something Omega experienced:
# they stay in the pressure log / the lobe-9 episode record (which carries
# the full episode incl. its ignition) and never mint a situational record.
# Live 2026-09-05: every operator turn minted an "Event pressure_ignition"
# lobe-4 record (50 in a day) that no recall path consumed and that
# crowded real situational memories out of retina hits.
LOG_ONLY_EVENT_KINDS = frozenset({"pressure_ignition"})

EVENT_KIND_TO_FACET = {
    "operator_input": "who",
    "pressure_ignition": "when",
    "global_a_intent": "why",
    "meta_a_action_pack": "how",
    "meta_a_skill_selection": "how",
    "skill_execution_pack": "how",
    "qwen_skill_dispatch": "how",
    "qwen_skill_result": "how",
    "auto_outcome": "outcome",
    "meta_b_outcome": "outcome",
    "meta_b_skill_outcome": "outcome",
    "global_b_update": "outcome",
    "global_b_skill_update": "outcome",
    "deepsleep_skill_summary": "outcome",
    "tool_result": "outcome",
    "tool_failure": "outcome",
    "skill_failure": "outcome",
    "safety_block": "outcome",
    "bridge_failure": "outcome",
    "recall_query": "what",
    # A typed causal edge (spec section 9, T11): WHY something followed from something.
    "causal_edge": "why",
    "recall_result": "what",
    "correction": "what",
    "reflection": "why",
    "episode_failure": "outcome",
}


def _episode_id(episode: Mapping[str, Any]) -> str:
    return str(episode.get("episode_id") or "")


def _capability_signal_event(episode: Mapping[str, Any], episode_id: str, created_at: str):
    consolidation = (episode.get("meta") or {}).get("skill_consolidation") or {}
    signal = (consolidation.get("record") or {}).get("capability_signal")
    if not signal:
        return None
    return adapters.event_from_capability_signal(
        signal, episode_id=episode_id, created_at=created_at
    )


def _extract_success(event: Any) -> Optional[bool]:
    payload = event.payload or {}
    success = payload.get("success")
    if success is None and "status" in payload:
        status = str(payload["status"]).lower()
        if status in ("completed", "success", "success_reply", "passed", "green"):
            success = True
        elif status in ("failed", "failure", "blocked", "error", "red"):
            success = False
    if success is None:
        if event.event_kind in (
            "tool_failure",
            "skill_failure",
            "safety_block",
            "bridge_failure",
            "episode_failure",
        ):
            success = False
    return success


def _extract_failure_reason(event: Any) -> Optional[str]:
    payload = event.payload or {}
    return (
        payload.get("failure_reason")
        or payload.get("reason")
        or payload.get("error")
        or payload.get("message")
    )


def _extract_linked_persons(event: Any) -> Optional[list[str]]:
    payload = event.payload or {}
    val = payload.get("linked_persons") or payload.get("persons") or payload.get("operator")
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [str(item) for item in val]
    return None


def _extract_linked_coding(event: Any) -> Optional[list[str]]:
    payload = event.payload or {}
    val = (
        payload.get("linked_coding")
        or payload.get("coding")
        or payload.get("files")
        or payload.get("files_changed")
    )
    if isinstance(val, str):
        return [val]
    if isinstance(val, list):
        return [str(item) for item in val]
    return None


def _operator_source_citation(episode: Mapping[str, Any]) -> dict[str, Any]:
    pressure = episode.get("pressure") if isinstance(episode.get("pressure"), Mapping) else {}
    opened_by = str(pressure.get("opened_by") or "")
    operator_types = {
        "operator_query",
        "operator_command",
        "operator_response",
        "operator_message",
    }
    for row in pressure.get("contributors") or ():
        if not isinstance(row, Mapping):
            continue
        detail = row.get("detail") if isinstance(row.get("detail"), Mapping) else {}
        metadata = row.get("metadata") if isinstance(row.get("metadata"), Mapping) else {}
        merged = {**metadata, **detail}
        citation = merged.get("source_citation")
        if isinstance(citation, Mapping) and citation.get("source_id"):
            return dict(citation)
        if str(row.get("id") or "") in operator_types and isinstance(citation, Mapping):
            return dict(citation)
    if opened_by in operator_types:
        for row in pressure.get("contributors") or ():
            if not isinstance(row, Mapping):
                continue
            detail = row.get("detail") if isinstance(row.get("detail"), Mapping) else {}
            citation = detail.get("source_citation")
            if isinstance(citation, Mapping) and citation.get("source_id"):
                return dict(citation)
    return {}


def capture_episode_to_canonical(
    episode: Optional[Mapping[str, Any]],
    *,
    created_at: Optional[str] = None,
    cycle_id: Optional[str] = None,
) -> dict:
    """Capture a closed episode's full memory-event chain into canonical memory.

    Routes events to lobe 4 (situational) or lobe 9 (temporal).
    Returns a report matching the capture.py signature. Never raises.
    """
    report = {
        "episode_id": "",
        "events_written": 0,
        "events_skipped_duplicate": 0,
        "event_ids": [],
        "errors": [],
    }

    if not isinstance(episode, Mapping):
        report["errors"].append(f"episode is not a mapping: {type(episode).__name__}")
        return report

    ep_id = _episode_id(episode)
    report["episode_id"] = ep_id
    c_id = cycle_id or ep_id
    operator_citation = _operator_source_citation(episode)

    # 1) Build the event chain
    try:
        event_view = adapters.canonical_event_view(episode)
        resolved = adapters._resolve_created_at(event_view, created_at)
        events = list(adapters.events_from_episode(event_view, resolved))
        cap_event = _capability_signal_event(event_view, ep_id, resolved)
        if cap_event is not None:
            events.append(cap_event)
    except Exception as exc:
        report["errors"].append(f"event build failed: {exc!r}")
        return report

    # 2) Write full episode to Lobe 9 [9, 1, 0]
    try:
        receipt = write_episode_to_temporal(episode)
        report["events_written"] += 1
        report["event_ids"].append(f"episode-{ep_id}")
        # WO-07: the lobe-9 intake receipt is the recorded intent of this episode's canonical
        # write. The terminus binds the committed record through it after the drain.
        report["episode_record_receipt"] = {
            "journal_record_id": str(getattr(receipt, "journal_record_id", "") or ""),
            "event_id": str(getattr(receipt, "event_id", "") or ""),
            "validation_status": str(getattr(receipt, "validation_status", "") or ""),
            "path": str(getattr(receipt, "path", "") or ""),
            "replayed": bool(getattr(receipt, "replayed", False)),
        }
    except Exception as exc:
        report["errors"].append(f"episode_temporal_write_failed: {exc!r}")

    # 3) Route each event
    for event in events:
        try:
            if event.event_kind == "graph_snapshot":
                continue
            if event.event_kind in LOG_ONLY_EVENT_KINDS:
                report.setdefault("events_skipped", []).append(event.event_kind)
                continue

            # Route to temporal consolidation
            if event.event_kind in ("deepsleep_summary", "omega_consolidation"):
                write_consolidation_to_temporal(event.to_dict())
                report["events_written"] += 1
                report["event_ids"].append(event.event_id)

            # Route to temporal capability
            elif event.event_kind in (
                "capability_signal",
                "capability_reinforced",
                "capability_weakened",
            ):
                write_capability_to_temporal(event.to_dict())
                report["events_written"] += 1
                report["event_ids"].append(event.event_id)

            # Route to situational lobe 4
            else:
                from memory.api import CanonicalEnvelope, intake_memory

                facet = EVENT_KIND_TO_FACET.get(event.event_kind, "what")
                facet_y = SITUATIONAL_FACETS.index(facet)

                text = f"Event kind: {event.event_kind}\nSource: {event.source}\nEpisode: {event.episode_id}\nPayload: {json.dumps(event.payload, ensure_ascii=False)}"

                enrichment = (
                    enrich_situational(
                        text=text,
                        facet=facet,
                        success=_extract_success(event),
                        failure_reason=_extract_failure_reason(event),
                        linked_persons=_extract_linked_persons(event),
                        linked_coding=_extract_linked_coding(event),
                    )
                    or {}
                )

                # An event may supply its own readable title and summary. A
                # recall hit is read by the answer model, and
                # "Event tool_result in <uuid>" tells it nothing about what the
                # plugin actually returned.
                readable_title = str(event.payload.get("title") or "").strip()
                readable_summary = str(event.payload.get("summary") or "").strip()
                env = CanonicalEnvelope(
                    cycle_id=c_id,
                    coord=[4, facet_y, 0],
                    type="situational",
                    payload={
                        "title": readable_title
                        or f"Event {event.event_kind} in {event.episode_id}",
                        "summary": readable_summary
                        or f"Event {event.event_kind} in {event.episode_id}",
                        "text": text,
                        "subtopic": event.event_kind,
                        **operator_citation,
                        "enrichment": {
                            **enrichment,
                            "episode_id": event.episode_id,
                            "event_kind": event.event_kind,
                            "event_id": event.event_id,
                            "event_source": event.source,
                            "event_payload": event.payload,
                            "canonical": True,
                        },
                    },
                    source=event.source,
                )

                intake_memory(env, propose=False, bypass_guardian=True, skip_embed=False)
                report["events_written"] += 1
                report["event_ids"].append(event.event_id)

        except Exception as exc:
            report["errors"].append(f"{event.event_kind} write failed: {exc!r}")

    return report

"""Host-side evidence helpers used by the PromptOrgan answer path.

Prompt assembly belongs to ``organ.promptorgan.packs.chat.answer``. This module
keeps only recall/evidence shaping and answer-lane telemetry so evidence cannot
be confused with prompt ownership.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

def emit_answer_event(event_name: str, **payload: Any) -> None:
    """Persist one answer-lane datum to ``guardian/logs/scaffold.jsonl``.

    The HUD's live ring is in-memory per the live-feed doctrine — once it rolls,
    the evidence for a bad turn is gone. Answer quality is debugged after the
    fact, so this lane needs a durable record: the prompt the answer model
    actually received and the text it produced.

    The subsystem still begins ``omega.scaffold.`` so the record keeps landing in
    scaffold.jsonl alongside every turn logged before the composer was retired.
    A rename would split one lane's history across two files for no gain.

    Terminal and telemetry mirrors are suppressed. These payloads carry full
    prompts and record bodies; the HUD wants its one-line signal, not this.
    Failure-soft by contract — instrumentation must never break an answer."""
    try:
        from guardian.data_logger import emit_event

        emit_event(
            "omega.scaffold.answer",
            event_name,
            _skip_terminal_mirror=True,
            _skip_telemetry_mirror=True,
            **payload,
        )
    except Exception:
        pass


def verified_hits_from_results(
    results: Iterable[Mapping[str, Any]] | None,
) -> dict[str, str]:
    """Return the exact ordered id->text set admitted by host verification."""
    hits: dict[str, str] = {}
    for result in results or ():
        if not isinstance(result, Mapping):
            continue
        if str(result.get("verdict") or "").strip().lower() != "verified":
            continue
        for item in result.get("evidence") or ():
            if not isinstance(item, Mapping):
                continue
            record_id = str(item.get("memory_id") or item.get("id") or "").strip()
            body = str(
                item.get("remembered_as")
                or item.get("text")
                or item.get("summary")
                or item.get("title")
                or ""
            ).strip()
            title = str(item.get("title") or "").strip()
            text = (
                f"{title}: {body}"
                if title and body and title.lower() not in body.lower()
                else body or title
            )
            text = _date_episode_row(item, text)
            citation = _source_citation_text(item.get("source_citation"))
            if citation:
                text = f"{text}\n{citation}" if text else citation
            if record_id and text and record_id not in hits:
                hits[record_id] = text
    return hits


def _source_citation_text(value: Any) -> str:
    """Render only host-verified SourceLedger proof as bounded answer context."""
    if not isinstance(value, Mapping):
        return ""
    if str(value.get("evidence_strength") or "") != "source_ledger_crypto":
        return ""
    fields = (
        "source_id",
        "source_type",
        "source_session_id",
        "source_session_timestamp",
        "span_start",
        "span_end",
        "span_hash",
        "source_content_hash",
        "evidence_strength",
    )
    parts = []
    for key in fields:
        item = value.get(key)
        if item is None or item == "":
            continue
        if key in {"span_start", "span_end"}:
            if isinstance(item, bool) or not isinstance(item, int):
                continue
        else:
            item = str(item).strip()
            if not item:
                continue
        parts.append(f"{key}={item}")
    return "Source citation (verified): " + "; ".join(parts) if parts else ""


def _date_episode_row(item: Mapping[str, Any], text: str) -> str:
    """An episode transcript (lobe 9) carries its local date and time, so the
    model can order "earlier" / "the last thing I said" (live 2026-09-06)."""
    coord = item.get("coord") or []
    try:
        lobe = int(coord[0])
    except (TypeError, ValueError, IndexError):
        return text
    observed = str(item.get("observed_at") or "").strip()
    if lobe != 9 or not observed or not text:
        return text
    try:
        from datetime import datetime

        stamp = datetime.fromisoformat(observed.replace("Z", "+00:00"))
        label = stamp.astimezone().strftime("%Y-%m-%d %H:%M") if stamp.tzinfo else observed[:16].replace("T", " ")
    except ValueError:
        return text
    return f"[{label}] {text}"


def _local_day(observed_at: str) -> str:
    """The operator's calendar day of a UTC timestamp (an artifact written at
    20:40 CDT is stamped 01:40Z the next day; the label must say the evening)."""
    raw = str(observed_at or "").strip()
    if not raw:
        return ""
    try:
        from datetime import datetime

        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return raw[:10]
        return parsed.astimezone().date().isoformat()
    except ValueError:
        return raw[:10]


def temporal_hits_from_query(query: str) -> dict[str, str]:
    """Convert the temporal lane's dated host records into composer evidence."""
    try:
        from memory.temporal_recall import recall_temporal

        temporal = recall_temporal(str(query or ""))
    except Exception:
        return {}
    hits: dict[str, str] = {}
    for index, item in enumerate((temporal or {}).get("records") or ()):
        if not isinstance(item, Mapping):
            continue
        record_id = str(item.get("memory_id") or item.get("id") or "").strip()
        if not record_id:
            record_id = f"temporal.{index:02d}.record"
        observed = _local_day(str(item.get("observed_at") or ""))
        title = str(item.get("title") or "").strip()
        summary = str(item.get("summary") or item.get("text") or "").strip()
        body = f"{title}: {summary}" if title and summary and title.lower() not in summary.lower() else (summary or title)
        text = f"[{observed}] {body}" if observed else body
        if text and record_id not in hits:
            hits[record_id] = text
    return hits


def temporal_answer_hits(query: str, results: Any) -> dict[str, str]:
    """RESPONSE_TEMPORAL evidence: the span's dated records FIRST (the timeline),
    then the forced lanes' verified rows the dated block does not already carry.
    Live 2026-09-06, "do you remember what I built yesterday?": the dated block
    was the only source, so the builder artifact remember_dev had retained never
    reached the prompt and the answer was "I do not recall"."""
    hits = dict(temporal_hits_from_query(query))
    seen_text = {_transcript_key(text) for text in hits.values()}
    for record_id, text in verified_hits_from_results(results).items():
        text = _operator_side_of_transcript(text)
        key = _transcript_key(text)
        if record_id in hits or key in seen_text:
            continue
        hits[record_id] = text
        seen_text.add(key)
    return hits


def _transcript_key(text: str) -> str:
    """Dedupe key: the same operator words under either summary wording."""
    return text.replace("operator said: ", "operator: ").strip().lower()


# Episode summaries read "operator said: ... → omega replied: ..." (older
# records: "operator: ... → omega: ...").
_TRANSCRIPT_REPLY_TAILS = (" → omega replied: ", " → omega: ")


def _operator_side_of_transcript(text: str) -> str:
    """A RESPOND episode transcript enters a temporal answer as what the
    operator SAID, without Omega's reply. Live 2026-09-06: seven transcripts of
    Omega's own earlier wrong answers to the same question ("you built
    web-api-design ...") outweighed the dated artifact records, and the model
    repeated them as fact; the directive alone did not stop it. Work episodes
    (USE_SKILL, BUILD, ...) keep their reply — that reply is the work."""
    body = text.split("] ", 1)[1] if text.startswith("[") and "] " in text else text
    if not body.startswith("RESPOND: "):
        return text
    for tail in _TRANSCRIPT_REPLY_TAILS:
        if tail in text:
            return text.split(tail, 1)[0]
    return text


__all__ = [
    "emit_answer_event",
    "temporal_hits_from_query",
    "verified_hits_from_results",
]

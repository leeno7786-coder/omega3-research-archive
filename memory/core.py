"""Shared memory pipeline used by all gates."""

import logging
from typing import Dict, Any, Optional, List, Mapping

from core.symphony_time import now_iso
from memory.deterministic import stable_hash
from memory.temporal_authority import OBSERVED, PROVIDED


log = logging.getLogger("omega.memory.core")

_WORKSPACE_SOURCE_FIELDS = (
    "connection_id",
    "capability",
    "object_id",
    "revision_id",
    "content_digest",
    "source_ledger_receipt",
    "observed_at",
    "availability_state",
)
_HEX = frozenset("0123456789abcdef")


def safe_coord(coord, default_x: int = 2, default_y: int = 0, default_z: int = 0) -> List[int]:
    """Safely extract X, Y, Z from a coord list with bounds checking.

    Returns a 3-element list [X, Y, Z], padding with defaults if needed.
    """
    if not coord or not isinstance(coord, list):
        return [default_x, default_y, default_z]
    X = coord[0] if len(coord) > 0 else default_x
    Y = coord[1] if len(coord) > 1 else default_y
    Z = coord[2] if len(coord) > 2 else default_z
    return [X, Y, Z]


def build_memory_dict(
    text: str,
    coord: List[int],
    source_tag: str,
    enrichment: Optional[Dict] = None,
    metadata: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Build a schema-valid memory dict from raw inputs."""
    meta = dict(metadata or {})
    workspace_source = validate_workspace_source(meta.get("workspace_source"))
    # Ensure coord has 3 elements with bounds checking (schema requires minItems:3)
    coord = safe_coord(coord)
    mem_id = meta.get("id") or _memory_id_for(text, coord, source_tag, meta)
    try:
        from memory.source_ledger.store import ensure_source_citation

        citation = ensure_source_citation(
            {**meta, "text": text},
            source_type=str(source_tag or "memory_write"),
            origin_id=str(meta.get("origin_id") or source_tag),
        )
        meta.update(citation)
    except Exception as exc:
        log.debug("source ledger citation skipped (%s)", exc)
    from memory.sense_registry import attach_senses_to_enrichment, bare_polyseme_violation

    violation = bare_polyseme_violation(
        str(meta.get("title") or text[:80]),
        summary=str(meta.get("summary") or text[:240]),
        text=text,
        domain=str(meta.get("domain") or ""),
        subtopic=str(meta.get("subtopic") or meta.get("topic") or ""),
    )
    if violation:
        enriched = dict(enrichment or {})
        enriched["status"] = "quarantined"
        enriched["sense_violation"] = violation
        enrichment = enriched
        meta["quarantined"] = True
    else:
        enrichment = attach_senses_to_enrichment(
            text,
            coord=coord,
            metadata={**meta, "id": mem_id},
            enrichment=enrichment,
        )
    ingested_at = _ingested_at_for(text, coord, source_tag, meta)
    provenance = {
        "origin_id": meta.get("origin_id", source_tag),
        "origin_type": meta.get("origin_type", "ingest"),
        "author": source_tag,
        "ingested_at": ingested_at,
        "ingested_at_authority": PROVIDED if _explicit_ingested_at(meta) else OBSERVED,
        "confidence": meta.get("confidence", 0.8),
    }
    for key in (
        "source_id",
        "source_type",
        "span_start",
        "span_end",
        "span_hash",
        "source_content_hash",
        "source_session_id",
        "source_session_timestamp",
    ):
        if meta.get(key) is not None:
            provenance[key] = meta[key]

    flags = {
        "canonical": meta.get("canonical", False),
        "private": meta.get("private", False),
    }
    if meta.get("quarantined"):
        flags["quarantined"] = True
    # Ordinary memories keep the compact recall-preview ceiling. Canonical
    # temporal records are different: their text is the serialized episode
    # body that reflection hydration must parse losslessly. Truncating one at
    # an arbitrary character produces invalid JSON and bricks the pressure
    # loop as soon as its reflection window is restored.
    canonical_temporal_body = (
        coord[0] == 9
        and str(meta.get("type") or "") in {"episode", "consolidation", "capability"}
        and isinstance(enrichment, Mapping)
        and enrichment.get("canonical") is True
    )
    stored_text = text if canonical_temporal_body else text[:4000]
    mem = {
        "id": mem_id,
        "coord": coord,
        "type": meta.get("type", "concept"),
        "title": meta.get("title", text[:80]),
        "summary": meta.get("summary", text[:240]),
        "text": stored_text,
        "provenance": provenance,
        "flags": flags,
        "audit": [{"when": ingested_at, "what": "ingested", "by": source_tag}],
    }
    # Preserve HOW the text entered memory independently of WHAT/WHERE it is.
    # Explicit ingress classification wins; otherwise use the deterministic
    # fallback so every new record remains query/statement/command-aware even
    # when a caller omitted intent metadata.
    speech_act = str(meta.get("speech_act") or "").strip().lower()
    if speech_act not in {"question", "statement", "command", "unknown"}:
        from memory.speech_act import classify_speech_act

        speech_act = classify_speech_act(text)
    mem["speech_act"] = speech_act
    if meta.get("undecided"):
        mem["provenance"]["undecided"] = True
    if meta.get("tags"):
        mem["tags"] = meta["tags"]
    if meta.get("links"):
        mem["links"] = meta["links"]
    # Structured score block — used by autonomous cycle records to carry
    # the four neuromodulator signals (dopamine, plasticity, strength,
    # stress) plus cycle_kind so downstream layers can read structured
    # state without parsing the text body.
    if meta.get("score"):
        mem["score"] = meta["score"]
    # Knowledge domain fields (for three-tier routing + domain-aware recall)
    if meta.get("domain"):
        mem["domain"] = meta["domain"]
    # Encoding strength (FU-1): the autonomous fetch path computes a real
    # per-cycle write_strength (base(cycle_kind) x plasticity.strength).
    # Stamp it as a DISTINCT top-level field so the deep-sleep readers
    # (mastery_tracker / consolidation_engine / pruning_engine) read the
    # real value instead of their hardcoded 0.5 default. Kept separate
    # from provenance.confidence to avoid double-counting in the mastery
    # formula (conf*0.4 + write_strength*0.3).
    if meta.get("write_strength") is not None:
        mem["write_strength"] = meta["write_strength"]
    # Third axis: HOW the utterance entered memory (asked / told / commanded),
    # per omega_memory_mcp.speech_act.classify_speech_act. Stamped as a
    # distinct top-level field -- same pattern as write_strength -- so recall
    # can later prefer a user's stated assertion over the assistant restating
    # it, without parsing the text body to guess.
    if meta.get("speech_act"):
        mem["speech_act"] = meta["speech_act"]
    if meta.get("subtopic"):
        mem["subtopic"] = meta["subtopic"]
    elif meta.get("topic"):
        mem["subtopic"] = meta["topic"]
    if meta.get("tool_spec"):
        mem["tool_spec"] = meta["tool_spec"]
    if workspace_source is not None:
        mem["workspace_source"] = workspace_source
    # Promoted identity preference: preserve its structured payload through
    # the additionalProperties:false memory schema.
    if meta.get("trait"):
        mem["trait"] = meta["trait"]
    # Candidate identity evidence is deliberately separate from ``trait`` so
    # one foldback cannot enter the preference/self-model surface. It still
    # needs a structured, retry-safe record for cross-episode aggregation.
    if meta.get("trait_observation"):
        mem["trait_observation"] = meta["trait_observation"]
    # Tool registrations: keep catalog fields under enrichment (memory
    # schema is additionalProperties:false at the root).
    if meta.get("kind") == "tool_registration":
        tool_reg = {
            k: meta[k]
            for k in (
                "tool_name",
                "tool_id",
                "description",
                "schema_path",
                "registered_by",
                "registry_scope",
                "registered_at",
            )
            if meta.get(k) is not None
        }
        if tool_reg:
            base = dict(enrichment or {})
            base["tool_registration"] = tool_reg
            enrichment = base
    try:
        from memory.intake import build_intake_packet

        enriched = dict(enrichment or {})
        enriched["intake_packet"] = build_intake_packet(
            memory_id=mem_id,
            text=text,
            coord=coord,
            source_tag=source_tag,
            metadata=meta,
            enrichment=enriched,
        )
        enrichment = enriched
    except Exception as exc:
        log.debug("intake packet build skipped (%s)", exc)
    if enrichment:
        mem["enrichment"] = enrichment
    return mem


def validate_workspace_source(raw: Any) -> Optional[Dict[str, str]]:
    """Validate and copy the strict Workspace observation lineage object."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("workspace_source must be an object")
    unknown = sorted(set(raw) - set(_WORKSPACE_SOURCE_FIELDS))
    if unknown:
        raise ValueError(f"workspace_source unknown fields: {unknown}")
    missing = [field for field in _WORKSPACE_SOURCE_FIELDS if field not in raw]
    if missing:
        raise ValueError(f"workspace_source missing fields: {missing}")
    out: Dict[str, str] = {}
    for field in _WORKSPACE_SOURCE_FIELDS:
        value = raw[field]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"workspace_source.{field} must be a non-empty string")
        out[field] = value
    digest = out["content_digest"]
    if len(digest) != 64 or any(ch not in _HEX for ch in digest):
        raise ValueError("workspace_source.content_digest must be a 64-char lowercase hex digest")
    return out


def _memory_id_for(text: str, coord: List[int], source_tag: str, meta: Dict[str, Any]) -> str:
    material = {
        key: value
        for key, value in dict(meta or {}).items()
        if key
        not in {
            "id",
            "source_id",
            "source_type",
            "span_start",
            "span_end",
            "span_hash",
            "source_content_hash",
            "source_session_id",
            "source_session_timestamp",
        }
    }
    suffix = stable_hash(
        "memory.core.id",
        str(text or ""),
        list(coord or []),
        str(source_tag or ""),
        material,
        length=8,
    )
    return f"{coord[0]}.{coord[1]:02d}.{suffix}"


def _ingested_at_for(text: str, coord: List[int], source_tag: str, meta: Dict[str, Any]) -> str:
    explicit = _explicit_ingested_at(meta)
    if explicit:
        return str(explicit)
    return now_iso()


def _explicit_ingested_at(meta: Mapping[str, Any]) -> Any:
    return (
        meta.get("ingested_at")
        or meta.get("timestamp_created")
        or meta.get("created_at")
        or meta.get("created")
    )

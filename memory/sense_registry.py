"""File-backed polysemy registry for Memory Engine v2.

The registry assigns explicit sense IDs to ambiguous surface terms before the
graph/compiler/verifier layers consume a canonical memory. It is deterministic
and local: no model calls, no Guardian path, no second graph.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from core.symphony_time import now_iso
from memory.taxonomy.normalize import canonical_json

SENSE_SCHEMA_VERSION = "omega.sense.v1"
POLYSEMOUS_TERMS = frozenset({"neuron", "node", "model", "agent", "graph"})
_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "it",
        "of",
        "on",
        "or",
        "the",
        "to",
        "with",
        "through",
    }
)


@dataclass(frozen=True)
class SenseRecord:
    sense_id: str
    term: str
    label: str
    coord: tuple
    domain: str = ""
    subtopic: str = ""
    cues: tuple = ()
    memory_id: str = ""
    created_at: str = ""
    type: str = "sense_record"
    schema_version: str = SENSE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "sense_id": self.sense_id,
            "term": self.term,
            "label": self.label,
            "coord": list(self.coord),
            "domain": self.domain,
            "subtopic": self.subtopic,
            "cues": list(self.cues),
            "memory_id": self.memory_id,
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }


def register_senses_for_memory(
    *,
    memory_id: str,
    title: str = "",
    summary: str = "",
    text: str = "",
    coord: Iterable[int] = (),
    domain: str = "",
    subtopic: str = "",
    root: Path | str | None = None,
) -> tuple[str, ...]:
    title_terms = POLYSEMOUS_TERMS & _tokens(title)
    tokens = _tokens(" ".join((title, summary, text, domain, subtopic)))
    terms = sorted(title_terms)
    if not terms:
        return ()
    cues = _sense_cues(tokens, domain=domain, subtopic=subtopic)
    out: list[str] = []
    for term in terms:
        rec = register_sense(
            term,
            label=title or term,
            coord=tuple(coord or ()),
            domain=domain,
            subtopic=subtopic,
            cues=tuple(c for c in cues if c != term),
            memory_id=memory_id,
            root=root,
        )
        out.append(rec.sense_id)
    return tuple(out)


def register_sense(
    term: str,
    *,
    label: str,
    coord: Iterable[int],
    domain: str = "",
    subtopic: str = "",
    cues: Iterable[str] = (),
    memory_id: str = "",
    root: Path | str | None = None,
) -> SenseRecord:
    clean_term = _normalize_term(term)
    clean_cues = tuple(sorted({_normalize_term(c) for c in cues if _normalize_term(c)}))
    clean_coord = tuple(int(c) for c in tuple(coord or ())[:3])
    rec = SenseRecord(
        sense_id=_sense_id(
            clean_term, domain=domain, subtopic=subtopic, coord=clean_coord, cues=clean_cues
        ),
        term=clean_term,
        label=str(label or clean_term),
        coord=clean_coord,
        domain=str(domain or ""),
        subtopic=str(subtopic or ""),
        cues=clean_cues,
        memory_id=str(memory_id or ""),
        created_at=now_iso(),
    )
    path = sense_registry_path(root=root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = {row["sense_id"]: row for row in _read_rows(path)}
    if rec.sense_id in existing:
        return _record_from_dict(existing[rec.sense_id])
    with open(path, "a", encoding="utf-8") as f:
        f.write(canonical_json(rec.to_dict()) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return rec


def resolve_query_senses(query: str, *, root: Path | str | None = None) -> list[dict[str, Any]]:
    tokens = _tokens(query)
    terms = POLYSEMOUS_TERMS & tokens
    if not terms:
        return []
    extra = tokens - terms
    rows = [
        row
        for row in _read_rows(sense_registry_path(root=root))
        if str(row.get("term") or "") in terms
    ]
    if not rows:
        return []
    if not extra:
        return sorted(rows, key=lambda r: str(r.get("sense_id") or ""))

    scored: list[tuple[int, str, dict[str, Any]]] = []
    for row in rows:
        cue_tokens = _tokens(
            " ".join(
                (
                    str(row.get("domain") or ""),
                    str(row.get("subtopic") or ""),
                    str(row.get("label") or ""),
                    " ".join(str(c) for c in row.get("cues") or []),
                )
            )
        )
        score = len(extra & cue_tokens)
        if score > 0:
            scored.append((score, str(row.get("sense_id") or ""), row))
    if not scored:
        return sorted(rows, key=lambda r: str(r.get("sense_id") or ""))
    best = max(score for score, _, _ in scored)
    return [row for score, _, row in sorted(scored, key=lambda t: (-t[0], t[1])) if score == best]


def bare_polyseme_violation(
    title: str,
    *,
    summary: str = "",
    text: str = "",
    domain: str = "",
    subtopic: str = "",
) -> str | None:
    """Return a violation reason when a canonical title is a bare polyseme."""
    del summary, text  # disambiguation requires explicit domain/subtopic, not body cues
    title_tokens = _tokens(title)
    if len(title_tokens) != 1:
        return None
    term = next(iter(title_tokens))
    if term not in POLYSEMOUS_TERMS:
        return None
    if str(domain or "").strip() or str(subtopic or "").strip():
        return None
    return f"bare polyseme '{term}' without disambiguation"


def attach_senses_to_enrichment(
    text: str,
    *,
    coord: Iterable[int],
    metadata: Mapping[str, Any] | None = None,
    enrichment: Mapping[str, Any] | None = None,
    root: Path | str | None = None,
) -> dict[str, Any] | None:
    meta = dict(metadata or {})
    enriched = dict(enrichment or {})
    existing = tuple(enriched.get("sense_ids") or meta.get("sense_ids") or ())
    if existing:
        enriched["sense_ids"] = list(dict.fromkeys(str(s) for s in existing if s))
        return enriched
    sense_ids = register_senses_for_memory(
        memory_id=str(meta.get("id") or ""),
        title=str(meta.get("title") or ""),
        summary=str(meta.get("summary") or ""),
        text=text,
        coord=tuple(coord or ()),
        domain=str(meta.get("domain") or ""),
        subtopic=str(meta.get("subtopic") or meta.get("topic") or ""),
        root=root,
    )
    if not sense_ids:
        return enriched or None
    enriched["sense_ids"] = list(sense_ids)
    return enriched


def sense_registry_path(*, root: Path | str | None = None) -> Path:
    return _memory_root(root) / "sense_registry" / "senses.jsonl"


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _record_from_dict(row: Mapping[str, Any]) -> SenseRecord:
    return SenseRecord(
        sense_id=str(row.get("sense_id") or ""),
        term=str(row.get("term") or ""),
        label=str(row.get("label") or ""),
        coord=tuple(row.get("coord") or ()),
        domain=str(row.get("domain") or ""),
        subtopic=str(row.get("subtopic") or ""),
        cues=tuple(row.get("cues") or ()),
        memory_id=str(row.get("memory_id") or ""),
        created_at=str(row.get("created_at") or ""),
        schema_version=str(row.get("schema_version") or SENSE_SCHEMA_VERSION),
    )


def _memory_root(root: Path | str | None = None) -> Path:
    if root is not None:
        base = Path(root)
        return base if base.name == "memory" else base / "memory"
    try:
        from memory.engine import validate_write

        return Path(validate_write.MEMORY_ROOT)
    except Exception:
        from core.paths import OMEGA_ROOT

        return OMEGA_ROOT / "memory"


def _sense_id(term: str, *, domain: str, subtopic: str, coord: tuple, cues: tuple) -> str:
    raw = canonical_json(
        [term, str(domain or ""), str(subtopic or ""), list(coord[:2]), list(cues)]
    )
    return f"sense:{term}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def _sense_cues(tokens: set[str], *, domain: str, subtopic: str) -> tuple[str, ...]:
    cue_tokens = set(tokens)
    cue_tokens |= _tokens(domain)
    cue_tokens |= _tokens(subtopic)
    cue_tokens -= _STOP_WORDS
    return tuple(sorted(cue_tokens))[:24]


def _tokens(text: str) -> set[str]:
    return {
        tok
        for tok in re.findall(r"[a-z0-9_]+", str(text or "").lower())
        if len(tok) >= 2 and tok not in _STOP_WORDS
    }


def _normalize_term(term: str) -> str:
    tokens = _tokens(term)
    return sorted(tokens)[0] if tokens else ""

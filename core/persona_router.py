"""Persona router — per-person 3rd-tier paths under lobe 1.

Per `project_memory_lobe_restructure` (2026-05-06): when the system
detects a person's name, this router gives the system a stable
filesystem location to encode memories about that person. Layout
mirrors how the knowledge lobe handles its 3-tier domains:

    memory/100_personas/person_stub/<person_id>/<entry_id>.json

All NON-operator persona writes use canonical coord `[1, 1]` (lobe 1,
subdomain 1 = "person_stub" — the SetFit anchor "this text is about a
person other than the operator"). The 3rd-tier `<person_id>` directory
is set via the envelope's `domain` metadata field, same as knowledge
lobe's per-domain dirs. This keeps coord schema-compliant (all
integers) while still giving each person their own filesystem
namespace for recall + linking.

2026-05-14: subdomain (1, 0) "operator" is reserved for the operator
persona only. Operator records live directly at
`memory/100_personas/operator/<id>.json` (no 3rd-tier dir). This
router routes only OTHER people; operator info is written by
`core.first_contact` + related operator-identity surfaces.

The router maintains a JSON index at
`guardian/state/persona_index.json` mapping `person_id → {name,
first_seen}` so the catalog of known personas is durable across
process restarts.

Public API:
    detect_persons(text)               -> list[name]
    route_persona(name)                -> (1, 1, person_id)
    ensure_persona_profile(name)       -> canonical profile-stub ref
    add_memory_for_person(name, ...)   -> canonical_ref
    link_persons(a, b, relation)       -> None
    recall_for_person(name, query)     -> list[record]
    search_by_decimal(coord)           -> verified list[record]
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from core.paths import OMEGA_ROOT
from core.symphony_time import now_iso
from memory.deterministic import stable_hash

log = logging.getLogger("omega.core.persona_router")

# Durable catalog of known personas (person_id → first_seen metadata).
_INDEX_PATH = OMEGA_ROOT / "guardian" / "state" / "persona_index.json"
_LOBE = 1  # personas
# 2026-05-14: subdomain shifted from (1,0) "person_stub" to (1,1)
# "person_stub" because (1,0) is now reserved exclusively for the
# operator persona — see memory.dewey_map SUBDOMAIN_MAP. Operator
# records live at memory/100_personas/operator/; everyone else gets
# routed under memory/100_personas/person_stub/<person_id>/.
_SUBDOMAIN = 1  # person_stub (the SetFit anchor for non-operator persons)

# Lock for atomic index reads/writes.
_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────
# Name detection — pattern-based and conservative. A single-token name needs
# an introducer/relationship phrase; otherwise only a multi-token proper name
# is accepted. This prevents commands and technologies from becoming people.
# ─────────────────────────────────────────────────────────────────────

_NAME_COMPONENT = r"[A-Z][a-z]{1,30}(?:[-'’][A-Z][a-z]{1,30})*"
_INTRODUCER_PATTERN = re.compile(
    r"\b(?i:(?:met|spoke (?:with|to)|talked (?:with|to)|named|"
    r"(?:my|our)\s+(?:friend|colleague|sister|brother|wife|husband|"
    r"(?:business\s+)?partner|boss|mentor|teacher|student|son|daughter|"
    r"cousin|mother|father|mom|dad|uncle|aunt|neighbor)))\s+"
    rf"({_NAME_COMPONENT}(?:\s+{_NAME_COMPONENT}){{0,3}})\b",
)
_FULL_NAME_PATTERN = re.compile(rf"\b({_NAME_COMPONENT}(?:\s+{_NAME_COMPONENT}){{1,3}})\b")
_MEMORY_DIRECTIVE_NAME_PATTERN = re.compile(
    rf"^(?i:(?:please\s+)?(?:remember|note)(?:\s+that)?\s+)"
    rf"({_NAME_COMPONENT}(?:\s+{_NAME_COMPONENT}){{0,3}})\b"
)
_LEADING_MEMORY_DIRECTIVE = re.compile(
    r"^(?:please\s+)?(?:remember|note)(?:\s+that)?\s+",
    re.IGNORECASE,
)
_COMMON_CAPITALIZED = frozenset(
    {
        "I",
        "Omega",
        "Python",
        "TypeScript",
        "Rust",
        "Go",
        "Linux",
        "Windows",
        "Mac",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Monday",
        "Friday",
        "Saturday",
        "Sunday",
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
        "Hello",
        "Hi",
        "Hey",
        "Yes",
        "No",
        "OK",
        # Possessives + pronouns that look like name candidates after
        # introducer phrases ("spoke with My friend Bob" would otherwise
        # capture "My" instead of letting pass-2 catch "Bob").
        "My",
        "Your",
        "Our",
        "His",
        "Her",
        "Their",
        "Its",
        "The",
        "A",
        "An",
        "This",
        "That",
        "These",
        "Those",
        "Some",
        "Any",
        "All",
    }
)


def _slugify(name: str) -> str:
    """Lowercase, strip punctuation, replace spaces with underscores.
    Used as the Z coord for a person — short, filesystem-safe, stable."""
    s = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip().lower())
    return s.strip("_") or "anon"


def _storage_dirname(name: str) -> str:
    """Normalize a semantic memory type into a nested storage dirname."""
    s = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(name or "").strip().lower())
    return s.strip("._-") or "general"


def detect_persons(text: str) -> List[str]:
    """Return high-confidence whole person names from admitted text.

    Two passes:
      1. Names following an introducer phrase ("met X", "my friend Y",
         "spoke with Z"), including a single-token name.
      2. Multi-token proper names anywhere else.

    Returns a deduplicated list preserving first-occurrence order.
    Best-effort, no model inference. Capitalized words alone are intentionally
    excluded so Python/Windows/Monday never acquire persona trees.
    """
    if not text or not isinstance(text, str):
        return []
    source_text = text.strip()
    scan_text = _LEADING_MEMORY_DIRECTIVE.sub("", source_text, count=1)
    seen: Dict[str, None] = {}

    def _admit(raw: str) -> None:
        name = " ".join(str(raw or "").strip().split())
        if not name:
            return
        if any(part in _COMMON_CAPITALIZED for part in name.split()):
            return
        seen.setdefault(name, None)

    # Pass 0 — an explicit memory directive is itself a person introducer, so
    # ``Remember Abel`` creates the same profile root as ``my son Abel``.
    directive_match = _MEMORY_DIRECTIVE_NAME_PATTERN.search(source_text)
    if directive_match:
        _admit(directive_match.group(1))
    # Pass 1 — introducer-led (high confidence).
    for match in _INTRODUCER_PATTERN.finditer(scan_text):
        _admit(match.group(1))
    # Pass 2 — multi-token proper names only.
    for match in _FULL_NAME_PATTERN.finditer(scan_text):
        _admit(match.group(1))
    return list(seen.keys())


# ─────────────────────────────────────────────────────────────────────
# Index — durable catalog of known personas.
# ─────────────────────────────────────────────────────────────────────


def _read_index() -> Dict[str, Dict[str, Any]]:
    if not _INDEX_PATH.exists():
        return {}
    try:
        with open(_INDEX_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("persona_index read failed: %s", exc)
    return {}


def _write_index(idx: Dict[str, Dict[str, Any]]) -> None:
    _INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _INDEX_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2, ensure_ascii=False)
    tmp.replace(_INDEX_PATH)


def _ensure_persona_dir(person_id: str) -> Path:
    """Make sure the persona's 3rd-tier directory exists on disk so
    subsequent writes have a target. Layout mirrors knowledge lobe's
    3-tier convention: `<lobe_dir>/<subdomain_dir>/<person_id>/`."""
    from memory.dewey_map import LOBE_MAP, SUBDOMAIN_MAP

    slug = str(person_id or "").strip()
    if not slug:
        raise ValueError("person_id required")
    # The operator owns (1,0) and its own domain directory. Now that a person
    # occupies the SAME tier, a person slugged "operator" would land on the
    # operator's tree and silently merge two identities. Refuse instead.
    operator_dir = SUBDOMAIN_MAP.get((_LOBE, 0), "operator")
    if slug.lower() == str(operator_dir).lower():
        raise ValueError(
            f"person_id {slug!r} collides with the operator namespace at "
            f"memory/{LOBE_MAP.get(_LOBE)}/{operator_dir}"
        )

    lobe_dir = LOBE_MAP.get(_LOBE, f"{_LOBE}xx")
    # The person sits DIRECTLY under the personas lobe, beside "operator" --
    # `ls memory/100_personas` reads operator, abel, mitchell_ray. This used to
    # insert SUBDOMAIN_MAP[(1,1)] ("person_stub") above the person, putting the
    # record TYPE in the tier the person belongs in and giving the operator and
    # everyone else two different shapes. The Dewey coordinate is untouched:
    # route_persona still returns (1, 1, person_id), and person_id is a SEMANTIC
    # slug / the `domain` envelope field, never a coord component -- so the
    # on-disk tier moves independently of the coordinate.
    path = OMEGA_ROOT / "memory" / lobe_dir / slug
    path.mkdir(parents=True, exist_ok=True)
    return path


def route_persona(
    name: str,
    *,
    source_citation: Optional[Mapping[str, Any]] = None,
) -> Tuple[int, int, str]:
    """Return `(1, 1, person_id)` for `name`. First encounter:
      * creates the per-person 3rd-tier directory
      * persists `person_id` in persona_index.json

    A durable profile root is created explicitly by :func:`ensure_persona_profile`.

    The first two elements are the canonical coord (lobe + subdomain);
    the third is the SEMANTIC slug used as the 3rd-tier dir name and
    the `domain` envelope field for path resolution. It is NOT a
    schema coord component.
    """
    if not name or not isinstance(name, str):
        raise ValueError("route_persona requires a non-empty name string")
    person_id = _slugify(name)
    triple = (_LOBE, _SUBDOMAIN, person_id)
    with _lock:
        idx = _read_index()
        if person_id in idx:
            _ensure_persona_dir(person_id)
            return triple
        _ensure_persona_dir(person_id)
        idx[person_id] = {
            "name": name,
            "first_seen": now_iso(),
        }
        _write_index(idx)
    log.info("persona_router: registered %s → person_id=%s", name, person_id)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "persona_registered",
            status="ok",
            summary=f"person_id={person_id}",
            person_id=person_id,
            name=name,
        )
    except Exception:
        pass
    return triple


def _write_profile_root(
    person_id: str,
    name: str,
    *,
    source_citation: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Intake one factual, non-empty name record as the person's profile root."""
    from memory.api import CanonicalEnvelope, intake_memory

    payload: Dict[str, Any] = {
        "title": f"persona:{name}",
        "summary": f"First encounter with {name}.",
        "text": f"{name} is a known person.",
        "name": name,
        "person_id": person_id,
        "domain": person_id,
        "subtopic": "persona_name",
        "tags": ["persona", "first_encounter", "profile_root"],
        "enrichment": {
            "decomposer": "persona_router",
            "entities": [
                {
                    "name": name,
                    "facet": "name",
                    "subject": person_id,
                    "confidence": 0.95 if source_citation else 0.6,
                }
            ],
            "linked_persons": [name],
        },
    }
    if source_citation:
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
            if source_citation.get(key) is not None:
                payload[key] = source_citation[key]
    envelope = CanonicalEnvelope(
        cycle_id=f"persona-root-{stable_hash('persona_router.profile_root', person_id, name, length=8)}",
        coord=[_LOBE, _SUBDOMAIN],
        type="persona_name",
        payload=payload,
        source="memory.persona_ingest" if source_citation else "persona_router",
        confidence=0.95 if source_citation else 0.6,
        provenance=[
            "persona_router.route_persona",
            *(
                [f"source_id:{source_citation['source_id']}"]
                if source_citation and source_citation.get("source_id")
                else []
            ),
        ],
        id=f"1.01.{stable_hash('persona_router.profile_root.id', person_id, length=8)}",
    )
    receipt = intake_memory(envelope, propose=False, bypass_guardian=True)
    if receipt.validation_status == "rejected":
        return None
    ref = receipt.path or receipt.staging_ref
    return Path(str(ref)).stem if ref else None


# ─────────────────────────────────────────────────────────────────────
# Memory operations scoped to a single person.
# ─────────────────────────────────────────────────────────────────────


def add_memory_for_person(
    name: str,
    payload: Dict[str, Any],
    *,
    type: str = "persona_note",
    cycle_id: Optional[str] = None,
    confidence: float = 0.6,
    human_sourced: bool = False,
    source_citation: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Append a memory under the person's namespace.

    The envelope coord is `[1, 1]`; the per-person 3rd-tier directory
    is selected by the `domain` payload field. Returns the SRAM intake ref
    or canonical ref, and propagates any intake failure.

    ``human_sourced=True`` labels a derived relationship from an already-gated
    operator statement as ``memory.persona_ingest``.  It does not re-enter
    Guardian: cognitive self-writes are direct and preserve ``source_citation``
    from the original external-ingress decision.
    """
    _, _, person_id = route_persona(name)
    payload = dict(payload)
    payload.setdefault("person_id", person_id)
    payload.setdefault("domain", person_id)  # path resolver hint
    payload.setdefault("subtopic", type)
    if source_citation:
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
            if source_citation.get(key) is not None:
                payload[key] = source_citation[key]
    from memory.api import intake_memory

    envelope = _persona_memory_envelope(
        person_id=person_id,
        payload=payload,
        type=type,
        cycle_id=cycle_id,
        confidence=confidence,
        human_sourced=human_sourced,
    )
    receipt = intake_memory(envelope, propose=False, bypass_guardian=True)
    if receipt.validation_status == "rejected":
        return None
    return receipt.path or receipt.staging_ref


def _persona_memory_envelope(
    *,
    person_id: str,
    payload: Dict[str, Any],
    type: str,
    cycle_id: Optional[str],
    confidence: float,
    human_sourced: bool = False,
) -> Any:
    from memory.api import CanonicalEnvelope

    # Derived relationship facts keep a distinct source label while remaining on
    # the direct cognitive write path; the original ledger citation is in payload.
    source = "memory.persona_ingest" if human_sourced else "persona_router"
    return CanonicalEnvelope(
        cycle_id=cycle_id
        or f"persona-{stable_hash('persona_router.memory', person_id, type, payload, length=8)}",
        coord=[_LOBE, _SUBDOMAIN],
        type=type,
        payload=payload,
        source=source,
        confidence=confidence,
        provenance=["persona_router.add_memory_for_person", f"person_id:{person_id}"],
    )


def link_persons(name_a: str, name_b: str, relation: str) -> None:
    """Write a bidirectional relationship edge between two persons.
    Both sides get a record at their own coord pointing at the other,
    so `recall_for_person` on either side surfaces the relationship.
    """
    triple_a = route_persona(name_a)
    triple_b = route_persona(name_b)
    if triple_a == triple_b:
        log.debug("persona_router: link skipped (same person %s)", name_a)
        return
    # Use the canonical `links` field for relationship edges so they
    # survive the encoding chain (build_memory_dict drops unknown
    # payload fields but preserves links). Each link entry is a small
    # dict the consumer can introspect.
    add_memory_for_person(
        name_a,
        {
            "title": f"relationship:{relation}:{triple_b[2]}",
            "summary": f"{name_a} <-> {name_b} ({relation})",
            "tags": ["relationship", relation],
            "links": [
                {
                    "kind": "persona_relationship",
                    "relation": relation,
                    "other_id": triple_b[2],
                    "other_name": name_b,
                }
            ],
        },
        type="persona_relationship",
        confidence=0.7,
    )
    add_memory_for_person(
        name_b,
        {
            "title": f"relationship:{relation}:{triple_a[2]}",
            "summary": f"{name_b} <-> {name_a} ({relation})",
            "tags": ["relationship", relation],
            "links": [
                {
                    "kind": "persona_relationship",
                    "relation": relation,
                    "other_id": triple_a[2],
                    "other_name": name_a,
                }
            ],
        },
        type="persona_relationship",
        confidence=0.7,
    )


def recall_for_person(name: str, query: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all memories scoped to that person's namespace.

    When `query` is given, delegates to PersonasGate.recall with the
    query and post-filters to entries whose payload.person_id matches.
    When `query` is None, walks the on-disk directory and loads
    everything (the operator's "search against the decimal" surface).
    """
    triple = route_persona(name)
    person_id = triple[2]
    from memory.gates.lobe_gates import PersonasGate

    gate = PersonasGate()
    if query:
        try:
            hits = gate.recall(query, limit=50) or []
        except Exception as exc:
            log.warning("persona_router: gate.recall raised: %s", exc)
            hits = []
        return [
            h
            for h in hits
            if isinstance(h, dict)
            and (
                (h.get("payload") or {}).get("person_id") == person_id
                or (h.get("payload") or {}).get("domain") == person_id
            )
        ]
    return search_by_decimal(triple)


def search_by_decimal(
    coord: Tuple[int, int, str],
    *,
    include_unverified: bool = False,
) -> List[Dict[str, Any]]:
    """Return persona records at an exact (lobe, subdomain, person_id).

    Default behavior is verifier-backed and grounding-safe: only records that
    exist in coord_index and pass RecallVerifier.verify_leaf are returned.
    ``include_unverified=True`` is an explicit inspector/debug escape hatch for
    raw fallback files; those records are annotated and must not be used for
    prompt grounding.
    """
    if not isinstance(coord, tuple) or len(coord) < 3:
        return []
    person_id = str(coord[2])
    persona_dir = _ensure_persona_dir(person_id)
    out: List[Dict[str, Any]] = []

    try:
        from memory.engine.walker import COORD_INDEX, load_coord_index
        from memory.recall_verifier import verify_leaf

        load_coord_index()
        for memory_id, entry in sorted(COORD_INDEX.items(), key=lambda item: str(item[0])):
            path = _resolved_entry_path(entry.get("path"))
            if path is None or not _is_relative_to(path, persona_dir):
                continue
            verdict = verify_leaf(str(memory_id))
            if verdict.status != "verified":
                continue
            record = _read_persona_record(path)
            if record is None:
                continue
            record = dict(record)
            record["recall_verdict"] = verdict.to_dict()
            record["grounding_authority"] = "verified_persona_decimal"
            out.append(record)
    except Exception as exc:
        log.warning("persona_router: verified decimal search failed: %s", exc)

    if not include_unverified:
        return out

    verified_paths = {
        str(
            _resolved_entry_path(
                (rec.get("recall_verdict") or {}).get("evidence", [{}])[0].get("path")
            )
        )
        for rec in out
        if isinstance(rec.get("recall_verdict"), dict)
    }
    for path in sorted(persona_dir.rglob("*.json"), key=lambda p: p.as_posix()):
        if str(path.resolve()) in verified_paths:
            continue
        rec = _read_persona_record(path)
        if rec is None:
            continue
        rec = dict(rec)
        rec["grounding_authority"] = "unverified_inspection_only"
        rec["recall_verdict"] = {
            "status": "unknown",
            "reason": "raw persona file not admitted through RecallVerifier",
        }
        out.append(rec)
    return out


def ensure_persona_profile(
    name: str,
    *,
    source_citation: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Ensure one canonical persona-name profile root exists for ``name``.

    The canonical index is checked before a new SRAM intake is accepted, so an
    already-committed root is idempotent without using recall as authority.
    """
    triple = route_persona(name, source_citation=source_citation)
    existing = _canonical_profile_root_id(triple[2])
    if existing:
        return existing

    # Repair a prior failed/legacy registration without minting a second profile
    # when the canonical root already exists (the scan above is authoritative).
    written_id = _write_profile_root(
        triple[2],
        name,
        source_citation=source_citation,
    )
    if not written_id:
        return None
    return _canonical_profile_root_id(triple[2]) or written_id


def _canonical_profile_root_id(person_id: str) -> Optional[str]:
    """Find the committed profile root by semantic domain.

    The coord index plus exact canonical record shape is the idempotency
    authority. RecallVerifier remains the authority for later prompt grounding;
    it is intentionally not used to decide whether to repeat an immutable write.
    This also keeps recovery valid under isolated/profile-root test mounts where
    ``persona_router.OMEGA_ROOT`` and the active canonical root differ.
    """
    try:
        from memory.engine.walker import (
            coord_index_snapshot,
            load_coord_index,
            load_meta_by_path,
        )

        load_coord_index()
        for memory_id, entry in sorted(
            coord_index_snapshot().items(),
            key=lambda item: str(item[0]),
        ):
            coord = list((entry or {}).get("coord") or [])
            if len(coord) < 2 or coord[:2] != [_LOBE, _SUBDOMAIN]:
                continue
            record = load_meta_by_path((entry or {}).get("path"))
            if not isinstance(record, dict):
                continue
            if str(record.get("domain") or "") != person_id:
                continue
            if (
                str(record.get("type") or "") != "persona_name"
                or str(record.get("subtopic") or "") != "persona_name"
                or not str(record.get("text") or "").strip()
            ):
                continue
            return str(memory_id)
    except Exception:
        log.exception("persona_router: profile-root lookup failed for %s", person_id)
        raise
    return None


def _read_persona_record(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            rec = json.load(f)
        return rec if isinstance(rec, dict) else None
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("persona_router: skipping unreadable %s (%s)", path, exc)
        return None


def _resolved_entry_path(raw_path: Any) -> Optional[Path]:
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = OMEGA_ROOT / path
    try:
        return path.resolve()
    except OSError:
        return None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False

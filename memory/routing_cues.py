"""Bounded symbolic routing cues — the CueReceipt (memory-organ Cut 2).

A cue is a *routing* hint: a normalized term that may resolve to an address the
CoordinateRegistry already holds. It is never a claim, never a memory id, and it
never mints. Three sources are kept separate and precedence-ordered:

    deterministic  — restated/expanded RecallPlan structure (exact terms,
                     alias expansions, task tags); active in production because
                     it only re-expresses the declared query prior
    entity         — RecallPlan.entities
    generated      — optional language-organ terms behind an injectable
                     generator boundary; SHADOW-ONLY by default (Cut 3 gives
                     nonlexical paths a verifier-recognized RecallBasis)

Contracts frozen here (12 §2, 14, Cut 2 plan):
  * CUE != CLAIM — no truth/claim field exists on the receipt;
  * the builder calls only ``registry.lookup`` / ``registry.near_matches``
    (near-matches for deterministic terms only); registration APIs are never
    touched — query-time minting is impossible through this module;
  * addresses resolve through the SAME token grammar the seats were encoded
    with (``memory.dme.adapter._tokens``: normalize, stopword-drop, stem), so a
    cue meets the registry vocabulary rather than a prettified copy of it;
  * generated output is bounded and sanitized; the generator receives only
    ``(query_text, max_terms)``;
  * ``receipt_id`` is deterministic over the stable fields — no wall clock.

The receipt is a runtime routing receipt, not canonical memory. No file I/O.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

from memory.taxonomy.normalize import canonical_json

log = logging.getLogger("omega.memory.routing_cues")

ROUTING_CUE_SCHEMA_VERSION = "omega.routing_cues.v1"

# Initial experimental ceilings (RQ-MO-002) — safe bounds, not a measured optimum.
MAX_DETERMINISTIC_TERMS = 24
MAX_ENTITY_TERMS = 8
MAX_GENERATED_TERMS = 8
MAX_TOTAL_TERMS = 32
MAX_TERM_CHARS = 80
MAX_GENERATED_WORDS = 5

SOURCE_DETERMINISTIC = "deterministic"
SOURCE_ENTITY = "entity"
SOURCE_GENERATED = "generated"

Address = tuple[int, int, int]


def cue_limits() -> dict[str, int]:
    return {
        "deterministic": MAX_DETERMINISTIC_TERMS,
        "entity": MAX_ENTITY_TERMS,
        "generated": MAX_GENERATED_TERMS,
        "total": MAX_TOTAL_TERMS,
        "term_chars": MAX_TERM_CHARS,
    }


# ── feature flags ────────────────────────────────────────────────────────────


def _flag(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "on", "yes"}


def language_cues_enabled() -> bool:
    """Invoke the optional language-organ generator at all? Default OFF."""
    return _flag("OMEGA_DME_LANGUAGE_CUES")


def generated_cues_apply_enabled() -> bool:
    """Let generated terms affect DME query coordinates? Default OFF until
    Cut 3 gives nonlexical paths a verifier-recognized RecallBasis."""
    return _flag("OMEGA_DME_LANGUAGE_CUES_APPLY")


# ── normalization ────────────────────────────────────────────────────────────


def normalize_cue_term(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    # keep ordinary lexical/symbol terms; drop control chars and overly long prose
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:MAX_TERM_CHARS]


def _bounded_unique(terms: Iterable[Any], limit: int, seen: set[str]) -> tuple[str, ...]:
    """Normalize, drop empties, dedupe against ``seen`` (which is updated), cap."""
    out: list[str] = []
    if limit <= 0:
        return ()
    for raw in terms or ():
        term = normalize_cue_term(raw)
        if not term or term in seen:
            continue
        seen.add(term)
        out.append(term)
        if len(out) >= limit:
            break
    return tuple(out)


def _query_hash(query_text: str) -> str:
    return hashlib.sha256(str(query_text or "").encode("utf-8")).hexdigest()


# ── receipt ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CueReceipt:
    receipt_id: str
    query_hash: str
    deterministic_terms: tuple[str, ...]
    generated_terms: tuple[str, ...]
    entity_terms: tuple[str, ...]
    resolved_addresses: tuple[Address, ...]
    unresolved_terms: tuple[str, ...]
    generator_used: bool
    generator_version: str
    schema_version: str = ROUTING_CUE_SCHEMA_VERSION
    # Additive source map (term, source, address) so generated-origin addresses
    # stay independently switchable at application time (Cut 2 plan Task 5).
    # Optional/additive → schema stays v1.
    resolved_term_addresses: tuple[tuple[str, str, Address], ...] = ()

    def addresses_for(self, *sources: str) -> tuple[Address, ...]:
        wanted = set(sources)
        out: list[Address] = []
        seen: set[Address] = set()
        for _term, source, addr in self.resolved_term_addresses:
            if source in wanted and addr not in seen:
                seen.add(addr)
                out.append(addr)
        return tuple(out)

    def deterministic_addresses(self) -> tuple[Address, ...]:
        return self.addresses_for(SOURCE_DETERMINISTIC)

    def entity_addresses(self) -> tuple[Address, ...]:
        return self.addresses_for(SOURCE_ENTITY)

    def generated_addresses(self) -> tuple[Address, ...]:
        return self.addresses_for(SOURCE_GENERATED)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": "cue_receipt",
            "receipt_id": self.receipt_id,
            "query_hash": self.query_hash,
            "deterministic_terms": list(self.deterministic_terms),
            "entity_terms": list(self.entity_terms),
            "generated_terms": list(self.generated_terms),
            "resolved_addresses": [list(a) for a in self.resolved_addresses],
            "unresolved_terms": list(self.unresolved_terms),
            "generator_used": bool(self.generator_used),
            "generator_version": self.generator_version,
            "schema_version": self.schema_version,
            "resolved_term_addresses": [
                [term, source, list(addr)] for term, source, addr in self.resolved_term_addresses
            ],
        }


# ── optional language-organ generator boundary ───────────────────────────────
# The generator receives EXACTLY (query_text, max_terms) and returns a sequence
# of short terms. It never sees canonical records, candidate ids, DME scores,
# retrieval results, graph neighbours or the operator corpus — the language
# organ may suggest vocabulary, never rank or name memories (14 §12).

CueGenerator = Callable[[str, int], Sequence[str]]

# Memory IDs ARE seat tokens: memory/dme/adapter._coordinates_for_seat registers
# ``seat.memory_id`` in the coordinate vocabulary, so a generated cue equal to a
# canonical id would resolve straight to that memory's own row — the language
# organ naming a memory (forbidden, 14 §12; CUT2_REVIEW_REWORK.md blocking 1).
# Every live id family is rejected by SHAPE before any registry lookup:
#   Dewey        <lobe>.<subdomain>.<suffix>   e.g. 1.00.3b7d0dbf, 0.00.omega_creator,
#                                               11.127.deferred_note
#   user_profile user_profile.<section>.<hex>
#   uuid / hash  any run of 8+ hex chars containing a digit (uuids, mp_<uuid>_01,
#                pathway_<hex>_...) — pure-letter runs ("cafebabe") are words
#   scheme/path  memory:…, coord:…, /memory/…, m_…, structured payloads
#   short ids    r1 / k3 / mem_1 style (letter(s)+digits, word_digits)
_REJECT_PATTERNS = (
    re.compile(r"^\d{1,2}\.\d{2,3}\.\S+$"),          # Dewey memory id
    re.compile(r"^user_profile\."),                  # user_profile.* id family
    re.compile(r"^memory:"),
    re.compile(r"^coord:"),
    re.compile(r"^m_[0-9a-z]+$"),
    re.compile(r"^mp_"),
    re.compile(r"^pathway_"),
    re.compile(r"/memory/"),
    re.compile(r"[{}\[\]\"\\]"),
    re.compile(r"^[a-z]+:[0-9a-z_.:-]*$"),           # scheme-like "id:xyz", "coord:2:0:7"
    re.compile(r"^[a-z]{1,3}\d{1,4}$"),               # r1, k3, x9 (fixture / short ids)
    re.compile(r"^[a-z]+_\d+$"),                      # mem_1
)


_HEX_RUN = re.compile(r"[0-9a-f]{8,}")


def _has_hash_like_run(term: str) -> bool:
    return any(any(ch.isdigit() for ch in run) for run in _HEX_RUN.findall(term))


def _looks_like_identifier_or_prose(term: str) -> bool:
    if not term:
        return True
    if len(term.split()) > MAX_GENERATED_WORDS:
        return True
    if any(p.search(term) for p in _REJECT_PATTERNS) or _has_hash_like_run(term):
        return True
    # dotted terms: allow plain word.word ("node.js", "e.g."); reject when any
    # dot-segment carries a digit — that is an id/version shape, not vocabulary
    if "." in term:
        for seg in term.split("."):
            if any(ch.isdigit() for ch in seg):
                return True
    return False


def _normalized_untruncated(value: str) -> str:
    """Whitespace/printability normalization WITHOUT the MAX_TERM_CHARS cut, so
    length can be judged before anything is trimmed into a plausible key."""
    text = " ".join(str(value or "").strip().lower().split())
    return "".join(ch for ch in text if ch.isprintable())


def sanitize_generated_terms(raw: Any) -> tuple[str, ...]:
    """Bound and clean generator output. Rejects non-strings, empties /
    control-only strings, anything whose NORMALIZED length exceeds
    MAX_TERM_CHARS (rejected, never truncated — CUT2_REVIEW_REWORK.md
    blocking 2), memory-id / path / address / JSON shapes, phrases longer than
    MAX_GENERATED_WORDS, and duplicates; caps at MAX_GENERATED_TERMS. A
    rejected output is dropped — never inferred into a term or candidate."""
    if not isinstance(raw, (list, tuple)):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        cleaned = _normalized_untruncated(item)
        if not cleaned or len(cleaned) > MAX_TERM_CHARS:
            continue
        term = normalize_cue_term(cleaned)  # identical for accepted lengths
        if _looks_like_identifier_or_prose(term) or term in seen:
            continue
        seen.add(term)
        out.append(term)
        if len(out) >= MAX_GENERATED_TERMS:
            break
    return tuple(out)


def generate_terms(
    query_text: str,
    *,
    generator: CueGenerator | None,
    max_terms: int = MAX_GENERATED_TERMS,
    version: str | None = None,
) -> tuple[tuple[str, ...], str]:
    """Invoke the optional generator failure-soft.

    Returns ``(terms, generator_version)``. With no generator: ``((), "")``.
    Exception / timeout / malformed output: ``((), version or "unknown")`` —
    deterministic cues continue regardless."""
    if generator is None:
        return (), ""
    label = str(version or "unknown")
    try:
        raw = generator(str(query_text or ""), int(max_terms))
    except Exception as exc:  # noqa: BLE001 — generation must never break recall
        log.warning("routing_cues: generator failed (%s: %s)", type(exc).__name__, exc)
        return (), label
    return sanitize_generated_terms(raw)[: max(0, int(max_terms))], label


_DEFAULT_GENERATOR: CueGenerator | None = None
_DEFAULT_GENERATOR_VERSION: str = ""


def set_default_cue_generator(generator: CueGenerator | None, *, version: str = "") -> None:
    """Register the process-wide optional generator (e.g. a runtime-client
    adapter). Nothing is registered by default: even with
    OMEGA_DME_LANGUAGE_CUES on, production has no generator until an owner
    wires one behind this boundary."""
    global _DEFAULT_GENERATOR, _DEFAULT_GENERATOR_VERSION
    _DEFAULT_GENERATOR = generator
    _DEFAULT_GENERATOR_VERSION = str(version or "")


def default_cue_generator() -> CueGenerator | None:
    return _DEFAULT_GENERATOR


def default_cue_generator_version() -> str:
    return _DEFAULT_GENERATOR_VERSION


# ── cues from the compiled RecallPlan ────────────────────────────────────────
# The cue layer CONSUMES the declared plan (memory.recall_plan.RecallPlan); it
# does not fork posture/alias/entity logic from recall_compiler. Only semantic
# term lists become cues — never lobes, evidence roles, weights or budgets.


def _plan_list(plan: Any, name: str) -> list[str]:
    values = getattr(plan, name, None) if plan is not None else None
    if not values:
        return []
    return [str(v) for v in values if str(v or "").strip()]


def _carries_content(term: str) -> bool:
    """A cue must carry at least one content token under the seat grammar.
    RecallPlan.exact_terms is the raw query.split(): stopwords ('which',
    'did') can never resolve — stopwords never mint — and would crowd the
    deterministic cap ahead of real alias expansions on a long question."""
    return bool(_term_tokens(term))


def deterministic_terms_from_plan(plan: Any) -> tuple[str, ...]:
    """exact_terms → expanded_terms → task_tags, order preserved, normalized,
    deduped, content-bearing only. These restate/expand structure the
    compiler already declared, which is why deterministic cues may be active
    in production."""
    seen: set[str] = set()
    out: list[str] = []
    for name in ("exact_terms", "expanded_terms", "task_tags"):
        for raw in _plan_list(plan, name):
            term = normalize_cue_term(raw)
            if term and term not in seen and _carries_content(term):
                seen.add(term)
                out.append(term)
    return tuple(out)


def entity_terms_from_plan(plan: Any) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in _plan_list(plan, "entities"):
        term = normalize_cue_term(raw)
        if term and term not in seen and _carries_content(term):
            seen.add(term)
            out.append(term)
    return tuple(out)


# ── resolution (read-only) ───────────────────────────────────────────────────


def _term_tokens(term: str) -> list[str]:
    """Same grammar the seats were encoded with (adapter._tokens): normalize,
    stopword-drop, stem. A multi-word cue yields several registry keys."""
    from memory.dme.adapter import _tokens

    return sorted(_tokens(term))


def _lobe_ok(addr: Address, allowed: set[int] | None, denied: set[int]) -> bool:
    lobe = int(addr[0])
    if lobe in denied:
        return False
    if allowed is not None and lobe not in allowed:
        return False
    return True


def _resolve_terms(
    terms: Sequence[str],
    source: str,
    registry: Any,
    *,
    allowed: set[int] | None,
    denied: set[int],
    fuzzy: bool,
    resolved: list[tuple[str, str, Address]],
    seen_addr: set[Address],
    unresolved: list[str],
) -> None:
    for term in terms:
        hit_any = False
        for token in _term_tokens(term) or [term]:
            candidates: list[Address | None] = [registry.lookup(token)]
            if fuzzy:
                # deterministic / original-query terms only (plan Task 1 Step 4)
                candidates.extend(registry.near_matches(token))
            for addr in candidates:
                if addr is None:
                    continue
                addr = (int(addr[0]), int(addr[1]), int(addr[2]))
                if not _lobe_ok(addr, allowed, denied):
                    continue
                hit_any = True
                if addr in seen_addr:
                    continue
                seen_addr.add(addr)
                resolved.append((term, source, addr))
        if not hit_any:
            unresolved.append(term)


def build_cue_receipt(
    *,
    query_text: str,
    deterministic_terms: Iterable[str],
    entity_terms: Iterable[str],
    generated_terms: Iterable[str],
    registry: Any,
    allowed_lobes: Iterable[int] | None,
    denied_lobes: Iterable[int] | None,
    generator_used: bool = False,
    generator_version: str = "",
) -> CueReceipt:
    """Build one receipt. Read-only against ``registry`` (lookup/near_matches
    only). Terms are normalized, source-deduped in precedence order
    deterministic → entity → generated, and capped per source and in total."""
    seen_terms: set[str] = set()
    det = _bounded_unique(deterministic_terms, MAX_DETERMINISTIC_TERMS, seen_terms)
    remaining = max(0, MAX_TOTAL_TERMS - len(det))
    ent = _bounded_unique(entity_terms, min(MAX_ENTITY_TERMS, remaining), seen_terms)
    remaining = max(0, MAX_TOTAL_TERMS - len(det) - len(ent))
    # generated source is sanitized HERE too, so the CUE != CLAIM boundary holds
    # structurally even for a caller that bypassed generate_terms()
    gen = _bounded_unique(
        sanitize_generated_terms(list(generated_terms or ())),
        min(MAX_GENERATED_TERMS, remaining),
        seen_terms,
    )

    allowed = set(int(x) for x in allowed_lobes) if allowed_lobes is not None else None
    denied = set(int(x) for x in (denied_lobes or ()))

    resolved: list[tuple[str, str, Address]] = []
    seen_addr: set[Address] = set()
    unresolved: list[str] = []
    _resolve_terms(det, SOURCE_DETERMINISTIC, registry, allowed=allowed, denied=denied, fuzzy=True,
                   resolved=resolved, seen_addr=seen_addr, unresolved=unresolved)
    _resolve_terms(ent, SOURCE_ENTITY, registry, allowed=allowed, denied=denied, fuzzy=False,
                   resolved=resolved, seen_addr=seen_addr, unresolved=unresolved)
    _resolve_terms(gen, SOURCE_GENERATED, registry, allowed=allowed, denied=denied, fuzzy=False,
                   resolved=resolved, seen_addr=seen_addr, unresolved=unresolved)

    qh = _query_hash(query_text)
    resolved_addresses = tuple(addr for _t, _s, addr in resolved)
    identity = {
        "schema_version": ROUTING_CUE_SCHEMA_VERSION,
        "query_hash": qh,
        "deterministic_terms": list(det),
        "entity_terms": list(ent),
        "generated_terms": list(gen),
        "resolved_term_addresses": [[t, s, list(a)] for t, s, a in resolved],
        "unresolved_terms": list(unresolved),
        "generator_used": bool(generator_used),
        "generator_version": str(generator_version or ""),
    }
    receipt_id = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return CueReceipt(
        receipt_id=receipt_id,
        query_hash=qh,
        deterministic_terms=det,
        generated_terms=gen,
        entity_terms=ent,
        resolved_addresses=resolved_addresses,
        unresolved_terms=tuple(unresolved),
        generator_used=bool(generator_used),
        generator_version=str(generator_version or ""),
        resolved_term_addresses=tuple(resolved),
    )

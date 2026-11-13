"""Admit a record that shares NO word with the query.

Measured 2026-07-27: of 21 records naming a dermatologist / ENT / primary care
doctor, the recall lane returned 2 for "How many different doctors did I visit?"
-- and both were surface-token coincidences ("I'm not a doctor", "get some fresh
air"). Every actual visit record was missed. The cause is not a gate: a row
carried by the recall lane already bypasses both lexical gates
(filesystem_recall.py:1237, :1240). The cause is that nothing in the recall path
measured MEANING -- DME scores Dewey coordinate bitmaps
(memory/dme/geometry.py:36, uses_embeddings = False) and the filesystem lane is
BM25.

The live cure is the resident Ettin retina: packed binary codes are searched by
Hamming distance, while the taxonomy compiler's retired dense edge class is not
read here.

Simulated at the eval's operating point: 2/21 -> 19/21 at k=24, budget 128.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SemanticAdmission:
    """What the lane admitted, and enough counters to prove it ran.

    RecallCandidate is frozen with extra="forbid" and is built from a hardcoded
    field list, so no per-row provenance marker can reach evidence. These
    aggregate counters are therefore the ONLY available firing proof -- and a
    null result has to stay distinguishable from a dead mechanism, which is why
    `status` is carried separately from the counts.
    """

    ids: frozenset[str]
    seed_count: int
    closure_count: int
    query_dim: int
    query_norm: float
    plane_edges: int
    status: str
    # Same membership as `ids`, in SEMANTIC PRIORITY ORDER: seeds first (by
    # descending cosine), then closure rows (by descending edge strength).
    # `ids` is a set and cannot express that order, but reservation -- picking
    # which admitted rows are worth a scarce truncation-surviving slot -- needs
    # it. Defaults to `()` so existing manual constructions (error paths, older
    # tests) do not have to name a priority order they have none of.
    ranked_ids: tuple[str, ...] = ()
    # Retina similarity of the QUERY to every present record (id -> [0, 1]),
    # not just the admitted seeds. Membership is the top decile; VALUE is read
    # off this map by the canonical scorer (canonical_recall._fuse_semantic_value).
    # Empty when the retina is cold.
    similarity: Mapping[str, float] = field(default_factory=dict)


def semantic_seed_ids(
    records: Iterable[Mapping[str, Any]],
    query: str,
    *,
    k: int,
) -> tuple[list[str], int, float, str, dict[str, float]]:
    """Top-k record ids by MEANING, seeded from the semantic retina -- plus the
    similarity of every present record, for the scorer's value term.

    Selection is by code, never by token — that is the entire point, and the
    reason a "dermatologist" row can be admitted for a "doctors" query, or an
    "estuary survey" row for a question about a "report".

    The retired dense 768-d engine is not consulted and no dense vector is
    durable. This seeds from the retina's packed D=16384 binary codes by
    popcount - the same candidates, from bits the DME already consumes.

    The ids it returns are marked ``semantic_recall`` by the caller, which
    exempts them from the verifier's LEXICAL topicality heuristic. That
    exemption is why this lane has to exist: the verifier says so in terms — a
    semantically recalled row "routinely shares no content token with the
    query". It is an exemption from a relevance heuristic, not from
    verification; authenticity is still the verifier's to decide, so the retina
    gains no authority here (ADR-MO-018/020).

    Returns ``([], 0, 0.0, "retina_cold", {})`` when no retina is mapped — a
    distinct status from "ran and found nothing", because a dead mechanism and
    a null result must stay tellable apart.
    """
    try:
        from memory import retina
    except Exception:  # noqa: BLE001
        return [], 0, 0.0, "retina_unavailable", {}

    if not retina.is_loaded():
        return [], 0, 0.0, "retina_cold", {}

    present: set[str] = set()
    for record in records:
        rid = str(record.get("memory_id") or record.get("id") or "")
        if rid:
            present.add(rid)
    if not present:
        return [], 0, 0.0, "no_records", {}

    # Over-fetch: the retina scans the whole generation, but only rows in THIS
    # candidate set may be seeded, and the nearest codes overall are not
    # necessarily inside it.
    try:
        # The whole neighbourhood, not k*8: the scorer reads VALUE off every
        # record's similarity, so one popcount pass over the corpus serves both
        # membership (the top seeds) and value. Nearest first.
        window = retina.window(query, max(k * 8, len(present)))
    except Exception as exc:  # noqa: BLE001
        return [], 0, 0.0, f"error:{type(exc).__name__}", {}
    if not window:
        return [], retina.DIMENSION, 0.0, "no_query_signal", {}

    seeds: list[str] = []
    similarity: dict[str, float] = {}
    similarity_sum = 0.0
    for hit in window:
        if hit.record_id not in present:
            continue
        similarity[hit.record_id] = float(hit.similarity)
        if len(seeds) < k:
            seeds.append(hit.record_id)
            similarity_sum += hit.similarity

    mean_similarity = (similarity_sum / len(seeds)) if seeds else 0.0
    return seeds, retina.DIMENSION, round(mean_similarity, 6), "ok", similarity


def semantic_admission(
    records: Iterable[Mapping[str, Any]],
    query: str,
    *,
    k: int,
) -> SemanticAdmission:
    """Seed candidate ids by Ettin-retina meaning. Never walks graph edges."""
    try:
        materialised = list(records)
        seeds, dim, norm, seed_status, similarity = semantic_seed_ids(materialised, query, k=k)
        return SemanticAdmission(
            ids=frozenset(seeds),
            seed_count=len(seeds),
            closure_count=0,
            query_dim=dim,
            query_norm=norm,
            plane_edges=0,
            status=seed_status,
            ranked_ids=tuple(seeds),
            similarity=similarity,
        )
    except Exception as exc:
        return SemanticAdmission(
            ids=frozenset(),
            seed_count=0,
            closure_count=0,
            query_dim=0,
            query_norm=0.0,
            plane_edges=0,
            status=f"error:{type(exc).__name__}",
            ranked_ids=(),
        )

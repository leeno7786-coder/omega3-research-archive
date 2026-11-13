"""Cut 5A Gate-D1 fixture corpus builder (plan Task 1a).

Deterministic — no RNG, no wall clock, no model. Regenerating must reproduce
the committed files byte-for-byte (a test asserts it). Written and first run
in the PRE-CUT worktree at 468c5771; the fixture is pure data and identical in
every tree.

What the corpus exercises (plan Task 1a):
  semantic (v6 SEMANTIC_SIMILARITY producer):
    S1,S2,S3  identical NORMALIZED TRUNCATED (first-256) vectors, DIFFERENT tails
              (dims 257-768) -> pairwise cosine 1.0 edges; proves MRL-256 truncation
    S6        cosine exactly 0.8 to the S-group -> edges with non-1.0 confidence
    S4        cosine 0.5 to the S-group and -0.12 to S6 -> BELOW SIM_THRESHOLD, no edge at all
    N1        vector shorter than MRL_DIM (128 dims) -> ignored by v6
    N2        zero-norm 768-vector -> ignored by v6
    N3,N4     no `embeddings` key at all (Cut-4 native records)
    J1        lobe 6 (journal, NON_RECALLABLE) with the S vector -> never projected
  structural (survive Cut 5A; every class this compiler produces on this fixture):
    CO_LOCATED     N3 + N4 share coord [4,0,0]
    SHARES_DOMAIN  N3 + N4 + S1 domain "dev"
    SHARES_TAG     N3 + N4 share tags ["python","test"] (>= MIN_SHARED_TAGS)
    MENTIONS       N4 links -> N3 (link edge); "Bob Smith" / "Omega AI" named entities in titles
    SUPERSEDES     K2 supersedes K1 (K1 superseded_by K2)
    CONTRADICTS    K3 enrichment.contradiction_state -> K4
    SAME_AS / FAMILY_OF / OWNS / WORKS_AS / LIVES_IN   P1 persona enrichment.entities facets
    event-derived nodes  N3 (lobe 4, operator_input) + S3 (lobe 9, auto_outcome), canonical=true
"""
from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / "root"
MEM = ROOT / "memory"

D = 768
MRL = 256


def _unit(v):
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


# two exactly-orthogonal unit vectors in the first-256 subspace (tail = 0)
_A = _unit([1.0] * MRL + [0.0] * (D - MRL))                                   # all-ones head
_B = _unit([1.0 if i % 2 == 0 else -1.0 for i in range(MRL)] + [0.0] * (D - MRL))  # alternating head
assert abs(sum(a * b for a, b in zip(_A, _B))) < 1e-12


def _head_plus_tail(head, tail_value):
    """same first-256 head, a DIFFERENT constant tail (v6 truncates -> identical)."""
    return [round(x, 12) for x in head[:MRL]] + [tail_value] * (D - MRL)


def _mix(ca, cb):
    return [round(ca * a + cb * b, 12) for a, b in zip(_A, _B)]


VEC_S1 = _head_plus_tail(_A, 0.0)
VEC_S2 = _head_plus_tail(_A, 0.25)
VEC_S3 = _head_plus_tail(_A, -0.5)
VEC_S6 = _mix(0.8, 0.6)   # cosine to _A head = 0.8 exactly (unit)
VEC_S4 = _mix(0.5, -0.8660254037844386)  # cosine 0.5 to the S-group and -0.12 to S6 -> below SIM_THRESHOLD everywhere
VEC_N1 = [round(x, 12) for x in _A[:128]]   # too short for MRL_DIM
VEC_N2 = [0.0] * D                           # zero norm

RECORDS = {
    # ── semantic group (cross-lobe so ONLY similarity links them) ──
    "2.00.s1": {"id": "2.00.s1", "coord": [2, 0, 0], "type": "concept", "title": "Telescope collimation notes",
                "summary": "Aligning a Dobsonian telescope mirror.", "domain": "dev", "tags": ["optics"],
                "embeddings": {"text_768": VEC_S1, "engine": "gemma_gguf", "dim": 768}},
    "4.02.s2": {"id": "4.02.s2", "coord": [4, 2, 0], "type": "situational", "title": "Mirror alignment session",
                "summary": "Collimated the primary mirror last night.", "tags": ["optics", "night"],
                "embeddings": {"text_768": VEC_S2, "engine": "gemma_gguf", "dim": 768},
                "enrichment": {"canonical": True, "event_kind": "operator_input", "closed_at": "2026-06-29T10:05:00Z"}},
    "9.01.s3": {"id": "9.01.s3", "coord": [9, 1, 0], "type": "episode", "title": "Collimation episode",
                "text": json.dumps({"status": "completed", "intent": "LEARN"}, sort_keys=True),
                "embeddings": {"text_768": VEC_S3, "engine": "gemma_gguf", "dim": 768},
                "enrichment": {"canonical": True, "event_kind": "auto_outcome", "closed_at": "2026-06-29T10:06:00Z"}},
    "2.01.s6": {"id": "2.01.s6", "coord": [2, 1, 0], "type": "concept", "title": "Eyepiece focal ratios",
                "summary": "Focal ratio arithmetic for eyepieces.", "tags": ["optics"],
                "embeddings": {"text_768": VEC_S6, "engine": "gemma_gguf", "dim": 768}},
    "2.02.s4": {"id": "2.02.s4", "coord": [2, 2, 0], "type": "concept", "title": "Bread proofing schedule",
                "summary": "Cold proof overnight, bake in the morning.", "tags": ["baking"],
                "embeddings": {"text_768": VEC_S4, "engine": "gemma_gguf", "dim": 768}},
    # ── vectors v6 must ignore ──
    "2.03.n1": {"id": "2.03.n1", "coord": [2, 3, 0], "type": "concept", "title": "Short vector record",
                "summary": "Legacy record whose stored vector is 128-dim.", "tags": ["legacy"],
                "embeddings": {"text_768": VEC_N1, "engine": "gemma_gguf", "dim": 128}},
    "2.04.n2": {"id": "2.04.n2", "coord": [2, 4, 0], "type": "concept", "title": "Zero vector record",
                "summary": "Legacy record whose stored vector is all zeros.", "tags": ["legacy"],
                "embeddings": {"text_768": VEC_N2, "engine": "gemma_gguf", "dim": 768}},
    # ── Cut-4 native records: NO embeddings key; structural wiring ──
    "4.00.n3": {"id": "4.00.n3", "coord": [4, 0, 0], "type": "situational", "title": "Bob Smith reviewed the Omega AI plan",
                "summary": "Review notes on the plan.", "domain": "dev", "tags": ["python", "test", "review"],
                "enrichment": {"canonical": True, "event_kind": "operator_input", "closed_at": "2026-06-29T10:00:00Z"}},
    "4.00.n4": {"id": "4.00.n4", "coord": [4, 0, 0], "type": "situational", "title": "Follow-up with Bob Smith",
                "summary": "Second review pass.", "domain": "dev", "tags": ["python", "test"], "links": ["4.00.n3"],
                "enrichment": {"canonical": True, "event_kind": "recall_query", "closed_at": "2026-06-29T10:01:00Z"}},
    # ── supersedes chain ──
    "2.05.k1": {"id": "2.05.k1", "coord": [2, 5, 0], "type": "concept", "title": "Boot takes 40 seconds",
                "summary": "Measured cold boot at 40 s.", "tags": ["boot"], "version": 1, "superseded_by": "2.05.k2"},
    "2.05.k2": {"id": "2.05.k2", "coord": [2, 5, 0], "type": "concept", "title": "Boot takes 25 seconds",
                "summary": "Re-measured cold boot at 25 s after the cache fix.", "tags": ["boot"], "version": 2,
                "supersedes": "2.05.k1"},
    # ── contradiction ──
    "2.06.k3": {"id": "2.06.k3", "coord": [2, 6, 0], "type": "concept", "title": "claim X is true",
                "summary": "X holds.", "tags": ["claims"],
                "enrichment": {"canonical": True, "contradiction_state": "2.06.k4", "closed_at": "2026-06-29T10:02:00Z"}},
    "2.06.k4": {"id": "2.06.k4", "coord": [2, 6, 1], "type": "concept", "title": "claim X is false",
                "summary": "X does not hold.", "tags": ["claims"],
                "enrichment": {"canonical": True, "closed_at": "2026-06-29T10:03:00Z"}},
    # ── persona relations (extract_persona facets) ──
    "1.00.p1": {"id": "1.00.p1", "coord": [1, 0, 0], "type": "concept", "title": "About Noah",
                "summary": "Persona record with parsed relations.", "domain": "noah",
                "enrichment": {"canonical": True, "closed_at": "2026-06-29T10:04:00Z", "entities": [
                    {"facet": "name", "name": "Noah", "subject": "operator"},
                    {"facet": "relationship", "name": "son", "linked_name": "Abel", "subject": "operator"},
                    {"facet": "business", "name": "Omega AI", "subject": "operator"},
                    {"facet": "occupation", "name": "engineer", "subject": "operator"},
                    {"facet": "location", "name": "Texas", "subject": "operator"},
                ]}},
    # ── non-recallable lobe with the S vector: must never be projected ──
    "6.00.j1": {"id": "6.00.j1", "coord": [6, 0, 0], "type": "journal", "title": "Journal with the same vector",
                "summary": "Reflection.", "embeddings": {"text_768": VEC_S1, "engine": "gemma_gguf", "dim": 768},
                "enrichment": {"canonical": True, "event_kind": "reflection", "closed_at": "2026-06-29T10:07:00Z"}},
}

LOBE_DIR = {1: "100_personas", 2: "200_knowledge", 4: "400_situational", 6: "600_journal", 9: "900_temporal"}


def build() -> dict:
    coord_index = {}
    for rid in sorted(RECORDS):
        rec = RECORDS[rid]
        rel = f"memory/{LOBE_DIR[rec['coord'][0]]}/{rid}.json"
        p = ROOT / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(rec, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
        coord_index[rid] = {"id": rid, "coord": rec["coord"], "path": rel, "title": rec["title"],
                            "type": rec["type"], "domain": rec.get("domain", ""), "subtopic": "",
                            "canonical": True, "summary": rec.get("summary", "")}
    (MEM / "indexes").mkdir(parents=True, exist_ok=True)
    (MEM / "indexes" / "coord_index.json").write_text(
        json.dumps(coord_index, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return coord_index


if __name__ == "__main__":
    idx = build()
    print(f"wrote {len(idx)} records under {ROOT}")

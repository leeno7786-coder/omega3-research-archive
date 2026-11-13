from __future__ import annotations

from collections import Counter

from benchmarks.memory_organ.program1.allocation import allocate_sources


_FIXTURE_TOPICS = ('scorer planes', 'lease namespace', 'coverage seal', 'publication lineage', 'hop engine', 'coordinate registry', 'outbox cursor', 'generation manifest', 'reservation block', 'retina codes', 'landed frontier', 'quarantine tree', 'dirty marker')


def _sources() -> list[dict]:
    rows = []
    for source_class, count in (("omega", 3000), ("public", 1550), ("synthetic", 2950)):
        for index in range(count):
            provenance = {"ordinal": index}
            if source_class == "public" and index < 198:
                provenance.update({
                    "kind": "longmemeval_message",
                    "question_type": "temporal-reasoning",
                    "question_id": f"temporal-{index // 2:03d}",
                    "session_id": f"session-{index // 2:03d}",
                    "session_date": f"2026-01-{(index % 28) + 1:02d}",
                    "turn_index": index % 2,
                    "role": "user",
                    "has_answer": True,
                })
            if source_class == "omega" and index < 52:
                provenance.update({
                    "kind": "omega_research_paragraph",
                    "logical_path": f"research/near-{index // 2:03d}.md",
                    "paragraph_index": index % 2,
                })
            if source_class == "omega" and index < 2:
                provenance.update({
                    "logical_path": "architecture/currentness.md",
                    "paragraph_index": index,
                })
            rows.append({
                "source_id": f"{source_class}:{index:04d}",
                "source_class": source_class,
                "record": {
                    "title": "",
                    "summary": "",
                    "text": (
                        "The legacy path is retired and superseded by the newer architecture."
                        if source_class == "omega" and index == 0
                        else "The current active replacement is now the authoritative architecture."
                        if source_class == "omega" and index == 1
                        else f"{source_class} source {index} documents the {_FIXTURE_TOPICS[index % len(_FIXTURE_TOPICS)]} behaviour"
                        if source_class == "omega" and index < 52
                        else f"{source_class} source {index}"
                    ),
                },
                "provenance": provenance,
            })
    return rows


def test_allocation_is_exact_deterministic_and_uses_every_source_once() -> None:
    rows = allocate_sources(_sources())
    assert len(rows) == 7500
    assert len({row["source_id"] for row in rows}) == 7500
    assert len({row["candidate_id"] for row in rows}) == 7500
    assert rows == allocate_sources(list(reversed(_sources())))

    assert Counter(row["partition"] for row in rows) == {
        "development": 4500, "validation": 1500, "locked_test": 1500,
    }
    assert Counter(row["source_class"] for row in rows) == {
        "omega": 3000, "public": 1550, "synthetic": 2950,
    }


def test_allocation_hits_every_stratum_and_partition_quota() -> None:
    rows = allocate_sources(_sources())
    expected = {"development": 300, "validation": 100, "locked_test": 100}
    strata = Counter((row["partition"], row["stratum"]) for row in rows)
    for stratum in {
        "exact_lexical_restatement", "morphology_variation", "ordinary_synonyms",
        "zero_token_overlap_paraphrase", "domain_term_vs_lay_term", "role_reversal",
        "negation", "temporal_update", "polysemy_context_shift", "underspecified_cue",
        "multi_hop_cue", "noisy_distractor", "adversarial_semantic_near_neighbor",
        "unrelated_negative", "no_signal",
    }:
        for partition, count in expected.items():
            assert strata[(partition, stratum)] == count


def test_controlled_hard_strata_use_the_preregistered_synthetic_budget() -> None:
    rows = allocate_sources(_sources())
    synthetic = Counter(row["stratum"] for row in rows if row["source_class"] == "synthetic")
    assert synthetic == {
        "role_reversal": 500,
        "negation": 500,
        "polysemy_context_shift": 500,
        "adversarial_semantic_near_neighbor": 450,
        "temporal_update": 500,
        "zero_token_overlap_paraphrase": 500,
    }
    # operator decision 2026-08-18: temporal_update and zero_token_overlap are fully controlled
    assert sum(row["source_class"] != "synthetic" and row["stratum"] == "temporal_update" for row in rows) == 0
    assert sum(row["source_class"] != "synthetic" and row["stratum"] == "zero_token_overlap_paraphrase" for row in rows) == 0
    assert sum(row["source_class"] != "synthetic" and row["stratum"] == "adversarial_semantic_near_neighbor" for row in rows) == 50
    assert sum(row["source_class"] != "synthetic" and row["stratum"] == "no_signal" for row in rows) == 500


def test_hard_strata_have_partition_local_contrast_pairs() -> None:
    rows = allocate_sources(_sources())
    hard = {
        "role_reversal", "negation", "temporal_update", "polysemy_context_shift",
        "adversarial_semantic_near_neighbor",
    }
    by_pair: dict[str, list[dict]] = {}
    for row in rows:
        if row["stratum"] not in hard:
            assert row["contrast_pair_id"] is None
            continue
        assert row["contrast_pair_id"]
        by_pair.setdefault(row["contrast_pair_id"], []).append(row)

    assert sum(len(group) for group in by_pair.values()) == 2500
    assert all(len(group) == 2 for group in by_pair.values())
    for group in by_pair.values():
        assert group[0]["partition"] == group[1]["partition"]
        assert group[0]["stratum"] == group[1]["stratum"]
        assert group[0]["candidate_id"] != group[1]["candidate_id"]


def test_natural_hard_cases_are_grounded_in_real_near_neighbor_groups() -> None:
    """Natural hard cases must come from REAL adjacency, not from metadata that merely mentions a
    relation. The temporal half of this test was retired on 2026-08-18: LongMemEval's
    'temporal-reasoning' question_type labels the QUESTION, not a fact->updated-fact relation between
    two turns (only 5 of 100 such pairs expressed a real update), so temporal_update is generated on
    the controlled synthetic path instead."""
    rows = allocate_sources(_sources())
    assert not [row for row in rows if row["stratum"] == "temporal_update" and row["source_class"] != "synthetic"]
    natural_near = [
        row for row in rows
        if row["stratum"] == "adversarial_semantic_near_neighbor" and row["source_class"] != "synthetic"
    ]
    assert len(natural_near) == 50
    assert all(row["source_class"] == "omega" for row in natural_near)
    assert all(row["source_provenance"]["kind"] == "omega_research_paragraph" for row in natural_near)
    pair_ids = {row["contrast_pair_id"] for row in natural_near}
    assert len(pair_ids) == 25

def test_near_neighbor_selector_prefers_lexically_overlapping_adjacent_pair() -> None:
    from benchmarks.memory_organ.program1.allocation import _select_omega_near_pairs

    def source(source_id: str, path: str, index: int, text: str) -> dict:
        return {
            "source_id": source_id,
            "source_class": "omega",
            "record": {"title": "", "summary": "", "text": text},
            "provenance": {
                "kind": "omega_research_paragraph",
                "logical_path": path,
                "paragraph_index": index,
            },
        }

    rows = [
        source("low-a", "a.md", 0, "alpha engines compute orbital data"),
        source("low-b", "a.md", 1, "banana garden paints wooden tables"),
        source("high-a", "b.md", 0, "runtime client generates buffered completion tokens"),
        source("high-b", "b.md", 1, "runtime client generates streaming completion tokens"),
        # a STRICT SUPERSET pair overlaps even more, but its target keeps nothing the contrast lacks
        # (operator decision 2026-08-17) — it must never outrank an eligible pair
        source("superset-a", "c.md", 0, "planner emits deterministic routing cues"),
        source("superset-b", "c.md", 1, "planner emits deterministic routing cues every cycle"),
    ]
    pair = _select_omega_near_pairs(rows, excluded_ids=set(), count=1)[0]
    assert {pair[0]["source_id"], pair[1]["source_id"]} == {"high-a", "high-b"}


# ── P1-A Task 4a: a morphology-twin-only contrast can never enter the adversarial stratum ──
# Operator decision 2026-08-17 (REPLACE, not skip): the near-neighbor selector must skip a pair
# whose target retains no GENUINE distinguishing content token — a token that is not merely a
# morphological twin (stem edit-distance <= 1) of a token in its contrast — and take the next
# deterministic eligible pair under the existing ranking. The validator rule is NOT relaxed.

from benchmarks.memory_organ.program1.allocation import (  # noqa: E402
    distinguishing_tokens,
    is_morphological_twin,
    pair_has_two_way_distinction,
)
from benchmarks.memory_organ.program1.authoring import build_authoring_jobs  # noqa: E402
from benchmarks.memory_organ.program1.query_author import content_tokens  # noqa: E402


def test_morphological_twin_detects_inflection_but_not_distinct_concepts() -> None:
    assert is_morphological_twin("carri", "carry")            # the p1c:2b56… defect: carried vs carry
    assert is_morphological_twin("plane", "planes")
    assert not is_morphological_twin("boot", "bootstrap")      # different concepts, shared prefix
    assert not is_morphological_twin("wal", "wall")            # stems under 4 chars are never twins
    assert not is_morphological_twin("batch", "cache")


def test_distinguishing_tokens_drops_twin_only_differences() -> None:
    target = "WAL carried compact_temporal_summary"
    contrast = "WAL carry compact_temporal_summary"
    assert content_tokens(target) - content_tokens(contrast)   # a naive diff still sees a token
    assert distinguishing_tokens(target, contrast) == set()    # …but it is only a morphological twin
    assert distinguishing_tokens("WAL carried the retina batch", contrast) == {"batch", "retina"}
    assert not pair_has_two_way_distinction(target, contrast)
    assert pair_has_two_way_distinction("alpha beta gamma", "alpha delta epsilon")


def test_selector_skips_a_twin_only_pair_and_takes_the_next_eligible_pair() -> None:
    from benchmarks.memory_organ.program1.allocation import _select_omega_near_pairs

    def source(index: int, path: str, para: int, text: str) -> dict:
        return {
            "source_id": f"omega:{index:04d}", "source_class": "omega",
            "record": {"title": "", "summary": "", "text": text},
            "provenance": {"kind": "omega_research_paragraph", "logical_path": path, "paragraph_index": para},
        }

    # the highest-overlap adjacent pair is twin-only (carried/carry); the next one is genuinely distinct
    sources = [
        source(0, "research/twin.md", 0, "The WAL carried the compact temporal summary onto episode open"),
        source(1, "research/twin.md", 1, "The WAL carry the compact temporal summary onto episode open"),
        source(2, "research/real.md", 0, "The scorer registers the strong hop plane for the active generation"),
        source(3, "research/real.md", 1, "The scorer registers the weak hop plane for the retired generation"),
    ]
    selected = _select_omega_near_pairs(sources, excluded_ids=set(), count=1)
    assert len(selected) == 1
    chosen = {str(selected[0][0]["source_id"]), str(selected[0][1]["source_id"])}
    assert chosen == {"omega:0002", "omega:0003"}              # the twin-only pair was skipped, not selected


def test_every_adversarial_job_keeps_a_genuine_distinguishing_detail() -> None:
    jobs = build_authoring_jobs(allocate_sources(_sources()))
    adversarial = [job for job in jobs if job["stratum"] == "adversarial_semantic_near_neighbor"]
    assert adversarial
    for job in adversarial:
        hard = str(job.get("hard_negative_text") or "")
        assert hard, job["candidate_id"]
        assert distinguishing_tokens(str(job["candidate_text"]), hard), job["candidate_id"]

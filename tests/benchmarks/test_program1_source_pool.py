from __future__ import annotations

import json
from pathlib import Path

from benchmarks.memory_organ.program1.source_pool import (
    build_source_pool,
    extract_longmemeval_sources,
    extract_omega_sources,
    generate_synthetic_sources,
)


def test_extract_omega_sources_is_deterministic_and_text_only(tmp_path: Path) -> None:
    root = tmp_path / "omega"
    root.mkdir()
    (root / "a.md").write_text(
        "# Heading\n\nOmega stores canonical truth in the filesystem and treats projections as rebuildable derived state. "
        "This paragraph is intentionally long enough to qualify as research material.\n\n"
        "DeepSleep closes episodes and owns memory maintenance after cognition completes. "
        "This second paragraph is also deliberately long enough for the source filter.\n"
    )
    rows = extract_omega_sources([root], count=2)
    assert len(rows) == 2
    assert all(row["source_class"] == "omega" for row in rows)
    assert all(row["record"]["title"] == "" and row["record"]["summary"] == "" for row in rows)
    assert rows == extract_omega_sources([root], count=2)


def test_longmemeval_extractor_uses_local_dataset_messages(tmp_path: Path) -> None:
    dataset = tmp_path / "longmemeval.json"
    dataset.write_text(json.dumps([
        {"question_id": "q1", "question_type": "single-session-user", "haystack_dates": ["d1"],
         "haystack_session_ids": ["s1"], "haystack_sessions": [[
             {"role": "user", "content": "I graduated with a degree in business administration and later moved into operations management."},
             {"role": "assistant", "content": "That background combines business training with practical operations experience in a useful way."},
         ]]}
    ]))
    rows = extract_longmemeval_sources(dataset, count=2)
    assert len(rows) == 2
    assert all(row["source_class"] == "public" for row in rows)
    assert {row["provenance"]["session_id"] for row in rows} == {"s1"}


def test_synthetic_sources_are_unique_structured_and_repeatable() -> None:
    rows = generate_synthetic_sources(count=120)
    assert len(rows) == 120
    assert len({row["source_id"] for row in rows}) == 120
    assert len({row["record"]["text"] for row in rows}) == 120
    assert all(row["source_class"] == "synthetic" for row in rows)
    assert all(row["structured_fact"] for row in rows)
    assert rows == generate_synthetic_sources(count=120)


def test_build_source_pool_enforces_exact_requested_mix(tmp_path: Path) -> None:
    omega = tmp_path / "omega"
    omega.mkdir()
    for index in range(4):
        (omega / f"{index}.md").write_text(
            f"Research statement {index} explains a deterministic memory-system property with enough detail "
            f"to remain a meaningful standalone semantic source for the qualification corpus and its later queries.\n"
        )
    dataset = tmp_path / "lm.json"
    sessions = []
    session_ids = []
    dates = []
    for index in range(4):
        sessions.append([{"role": "user", "content": f"Public memory statement {index} contains a sufficiently descriptive fact for semantic retrieval evaluation and model-blind corpus construction."}])
        session_ids.append(f"s{index}")
        dates.append(f"d{index}")
    dataset.write_text(json.dumps([{"question_id": "q", "question_type": "multi-session",
                                    "haystack_dates": dates, "haystack_session_ids": session_ids,
                                    "haystack_sessions": sessions}]))
    rows = build_source_pool(omega_roots=[omega], longmemeval_path=dataset,
                             omega_count=3, public_count=3, synthetic_count=3,
                             public_temporal_pair_count=0)
    assert [sum(row["source_class"] == cls for row in rows) for cls in ("omega", "public", "synthetic")] == [3, 3, 3]
    assert len({row["source_id"] for row in rows}) == 9


def test_full_synthetic_quota_has_no_text_collisions() -> None:
    rows = generate_synthetic_sources(count=2250)
    assert len(rows) == 2250
    assert len({row["source_id"] for row in rows}) == 2250
    assert len({row["record"]["text"] for row in rows}) == 2250


def test_omega_extractor_merges_list_introduction_with_following_block(tmp_path: Path) -> None:
    root = tmp_path / "omega-merge"
    root.mkdir()
    (root / "plan.md").write_text(
        "The memory system repeatedly performs these consolidation actions:\n\n"
        "- replays eligible traces\n- strengthens useful paths\n- weakens misleading paths\n"
    )
    rows = extract_omega_sources([root], count=1)
    text = rows[0]["record"]["text"]
    assert text.startswith("The memory system repeatedly performs these consolidation actions:")
    assert "replays eligible traces" in text
    assert "weakens misleading paths" in text


def test_longmemeval_source_selection_prioritizes_answer_evidence_then_assistant(tmp_path: Path) -> None:
    dataset = tmp_path / "priority.json"
    dataset.write_text(json.dumps([{
        "question_id": "q1", "question_type": "single-session-user", "haystack_dates": ["d1"],
        "haystack_session_ids": ["s1"], "haystack_sessions": [[
            {"role": "user", "content": "This ordinary user request is long enough to be eligible, but it is not evidence selected as an answer and should rank after better source material."},
            {"role": "assistant", "content": "This assistant response is a substantive public memory statement with enough descriptive content to qualify ahead of an ordinary user request."},
            {"role": "user", "has_answer": True, "content": "The answer-bearing user memory states that the workshop inspection occurs every Thursday morning before the equipment is moved."},
        ]]
    }]))
    rows = extract_longmemeval_sources(dataset, count=2)
    texts = {row["record"]["text"] for row in rows}
    assert any("answer-bearing user memory" in text for text in texts)
    assert any("assistant response" in text for text in texts)


def test_source_extractor_rejects_orphan_list_introductions(tmp_path: Path) -> None:
    root = tmp_path / "omega-orphan"
    root.mkdir()
    (root / "orphan.md").write_text(
        "This isolated review fragment ends by announcing evidence that is not present in the source block and therefore must not become a semantic memory candidate:\n"
    )
    try:
        extract_omega_sources([root], count=1)
    except ValueError as exc:
        assert "eligible unique rows" in str(exc)
    else:
        raise AssertionError("orphan colon fragment was incorrectly admitted")


def test_longmemeval_source_selection_can_reserve_temporal_answer_pairs(tmp_path: Path) -> None:
    dataset = tmp_path / "temporal-pairs.json"
    dataset.write_text(json.dumps([
        {
            "question_id": "t1", "question_type": "temporal-reasoning",
            "haystack_dates": ["d1"], "haystack_session_ids": ["s1"],
            "haystack_sessions": [[
                {"role": "user", "has_answer": True, "content": "The project schedule originally placed the field inspection on Tuesday morning before the equipment transfer was approved."},
                {"role": "user", "has_answer": True, "content": "After the transfer approval, the field inspection moved to Thursday afternoon and the Tuesday appointment was cancelled."},
            ]],
        },
        {
            "question_id": "ordinary", "question_type": "single-session-user",
            "haystack_dates": ["d2"], "haystack_session_ids": ["s2"],
            "haystack_sessions": [[
                {"role": "assistant", "content": "This unrelated assistant message is substantive enough to qualify but must not displace the reserved temporal answer pair from the tiny pool."},
                {"role": "assistant", "content": "A second unrelated assistant message is also substantive enough to qualify and acts only as filler after temporal reservation."},
            ]],
        },
    ]))
    rows = extract_longmemeval_sources(dataset, count=3, temporal_pair_count=1)
    temporal = [row for row in rows if row["provenance"]["question_type"] == "temporal-reasoning"]
    assert len(temporal) == 2
    assert {row["provenance"]["question_id"] for row in temporal} == {"t1"}

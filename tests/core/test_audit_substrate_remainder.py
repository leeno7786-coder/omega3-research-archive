"""Audit substrate — MEDIUM/LOW remainder regression tests (2026-06-23)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_verify_promotion_surfaces_purity_flag_when_allowed(tmp_path):
    """MEDIUM-10: soft FLAG findings must not pass as canonical verification."""
    from memory import promotion as promo

    staging_path = tmp_path / "staging.json"
    staging_path.write_text(
        json.dumps(
            {
                "envelope": {"payload": {"text": "I love you"}, "coord": [0, 0, 0]},
                "memory_dict": {"provenance": {"confidence": 0.9}},
            }
        ),
        encoding="utf-8",
    )

    def _fake_guard(_env):
        return True, [
            {
                "stage": "memory_purity",
                "reason": "emotional_content",
                "category": "purity_flag",
                "detail": "rule_x",
            }
        ]

    with (
        patch.object(promo, "_staging_path", return_value=staging_path),
        patch(
            "memory.promotion.simulate_promotion",
            return_value={"simulation": {"target_path": "/x"}, "rollback": {"staging_ref": "s1"}},
        ),
        patch("memory.write_guard.guard_envelope", side_effect=_fake_guard),
        patch(
            "core.memory_config.staging_meets_observation_gate",
            return_value=(True, "ok", 0.9),
        ),
    ):
        result = promo.verify_promotion("s1")

    assert result["status"] == "failed"
    assert any(c.get("category") == "purity_flag" for c in result.get("checks") or [])


def test_recall_plan_enables_strength_lane_when_bias_set():
    """MEDIUM-8: strength_bias must activate the strength lane weight."""
    from memory.recall_compiler import compile_recall_plan

    class _QS:
        text = "explain photosynthesis"
        cycle_id = "t"
        domain = None
        task_scope = None

    plan = compile_recall_plan(_QS())
    assert plan.posture.name == "KNOWLEDGE_EXPLANATION"
    assert plan.lane_weights.get("strength", 0) > 0


def test_query_lifecycle_syncs_pressure_resolved_questions(tmp_path, monkeypatch):
    """MEDIUM-3: deepsleep queries resolve when pressure open_questions closes them."""
    from deepsleep_loop._deepsleep_state import DeepSleepInput, OpenQuery
    from deepsleep_loop.query_lifecycle import run as ql
    from deepsleep_loop.query_store import query_key

    qpath = tmp_path / "deepsleep_open_queries.json"
    monkeypatch.setattr("pressure_loop.policy.deepsleep_query_path", lambda: qpath)

    key = query_key("What is the plan?", "ai")
    store = {
        key: OpenQuery(
            query_id=key,
            question="What is the plan?",
            domain="ai",
            created_episode="ep-old",
            confidence=0.5,
            status="open",
            created_timestamp="2026-01-01T00:00:00+00:00",
        )
    }
    qpath.write_text(
        json.dumps({k: v.to_dict() for k, v in store.items()}),
        encoding="utf-8",
    )

    class _PQ:
        question_text = "What is the plan?"
        memory = {"domain": "ai"}
        status = "resolved"
        resolved_note = "answered"

    monkeypatch.setattr("pressure_loop.open_questions._load_all", lambda: [_PQ()])
    monkeypatch.setattr(
        "pressure_loop.open_questions._latest_per_id",
        lambda records: {"q1": records[0]},
    )

    ds_input = DeepSleepInput(
        window_id="ep-win",
        episode_window=[],
        window_reflection={},
        trajectory_snapshot={},
    )
    changed = ql.run_query_lifecycle(ds_input)
    assert any(c.status in ("resolved", "archived") for c in changed)


def test_select_model_profile_ask_operator_query():
    from auto_loop.route.run import select_model_profile

    assert select_model_profile("ASK_OPERATOR_QUERY", memory_enriched=False) == "TEXT_FAST"


def test_reseed_emits_inquiry_pressure_contributor(monkeypatch):
    # trajectory->pressure reseed is DECOUPLED by default (2026-06-30); this tests the legacy
    # inquiry-pressure mechanism, opt-in via the flag.
    monkeypatch.setenv("OMEGA_TRAJECTORY_PRESSURE", "1")
    from deepsleep_loop._deepsleep_state import DeepSleepInput
    from deepsleep_loop.reseed.run import generate_reseed

    ds_input = DeepSleepInput(
        window_id="w1",
        episode_window=[],
        window_reflection={
            "reflection": {
                "trajectory": {
                    "next_learning_pressure": 0.0,
                    "next_inquiry_pressure": 0.6,
                },
                "pressure_feedback": {},
            }
        },
        trajectory_snapshot={"target_domain": "ai"},
    )
    contributors = generate_reseed(ds_input)
    assert any(c.type == "curiosity_gap" for c in contributors)


def test_compact_reflection_eligible_trims_tail(tmp_path, monkeypatch):
    """LOW-11: reflection queue compaction keeps the newest lines."""
    import memory.queues as queues

    path = tmp_path / "reflection.jsonl"
    monkeypatch.setattr(queues, "REFLECTION_QUEUE_PATH", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps({"i": i}) for i in range(120)) + "\n", encoding="utf-8")

    dropped = queues.compact_reflection_eligible(keep_max_lines=100)
    assert dropped == 20
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 100
    assert json.loads(lines[0])["i"] == 20


def test_query_lifecycle_applies_reflection_query_resolutions(tmp_path, monkeypatch):
    """MEDIUM-3: resolutions emitted in Omega reflection close deepsleep queries."""
    from deepsleep_loop._deepsleep_state import DeepSleepInput, OpenQuery
    from deepsleep_loop.query_lifecycle import run as ql
    from deepsleep_loop.query_store import query_key

    qpath = tmp_path / "deepsleep_open_queries.json"
    monkeypatch.setattr("pressure_loop.policy.deepsleep_query_path", lambda: qpath)

    key = query_key("What is spin?", "quantum")
    store = {
        key: OpenQuery(
            query_id=key,
            question="What is spin?",
            domain="quantum",
            created_episode="w-old",
            confidence=0.6,
            status="open",
            created_timestamp="2026-01-01T00:00:00+00:00",
        )
    }
    qpath.write_text(
        json.dumps({k: v.to_dict() for k, v in store.items()}),
        encoding="utf-8",
    )

    ds_input = DeepSleepInput(
        window_id="w-new",
        episode_window=[],
        window_reflection={
            "reflection": {
                "query_resolutions": [
                    {
                        "query_id": key,
                        "status": "resolved",
                        "reason": "answered across window",
                    }
                ]
            },
            "query_resolutions": [
                {
                    "query_id": key,
                    "status": "resolved",
                    "reason": "answered across window",
                }
            ],
        },
        trajectory_snapshot={},
    )
    changed = ql.run_query_lifecycle(ds_input)
    assert any(c.status in ("resolved", "archived") for c in changed)
    from deepsleep_loop.query_store import load_queries

    assert load_queries(path=qpath)[key].status == "archived"


def test_abandon_stale_open_queries(tmp_path, monkeypatch):
    """MEDIUM-3: stale open queries auto-abandon instead of living forever."""
    from datetime import datetime, timedelta, timezone

    from deepsleep_loop._deepsleep_state import DeepSleepInput, OpenQuery
    from deepsleep_loop.query_lifecycle import run as ql
    from deepsleep_loop.query_store import load_queries, query_key

    qpath = tmp_path / "deepsleep_open_queries.json"
    monkeypatch.setattr("pressure_loop.policy.deepsleep_query_path", lambda: qpath)

    key = query_key("Old question?", "ai")
    old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    store = {
        key: OpenQuery(
            query_id=key,
            question="Old question?",
            domain="ai",
            created_episode="w-old",
            confidence=0.4,
            status="open",
            created_timestamp=old_ts,
        )
    }
    qpath.write_text(
        json.dumps({k: v.to_dict() for k, v in store.items()}),
        encoding="utf-8",
    )

    ds_input = DeepSleepInput(
        window_id="w-new",
        episode_window=[],
        window_reflection={},
        trajectory_snapshot={},
    )
    ql.run_query_lifecycle(ds_input)
    stored = load_queries(path=qpath)[key]
    assert stored.status == "archived"
    assert stored.resolution_reason == "auto_archive_stale"


def test_operator_intent_gather_loads_directive_metadata(monkeypatch):
    """LOW-1: operator_intent contributor carries directive text when active."""
    from core.shared_state import SharedState
    from pressure_loop import pressure_field as pf
    from pressure_loop.pressure import gather_contributors

    monkeypatch.setattr(
        pf,
        "build_registry",
        lambda: {pf.CONTRIBUTOR_OPERATOR_INTENT: lambda ss, **k: 0.8},
    )
    monkeypatch.setattr(
        "pressure_loop.operator_pressure.read_operator_directive",
        lambda ss, **k: {
            "text": "learn rust",
            "domain": "coding",
            "cycle_id": "cycle-1",
        },
    )
    out = gather_contributors(SharedState())
    intent = next(c for c in out if c.type == "operator_command")
    assert intent.metadata.get("message") == "learn rust"
    assert intent.metadata.get("domain") == "coding"


@pytest.fixture
def isolated_memory(tmp_path, monkeypatch):
    import memory.engine.validate_write as vw
    import memory.engine.walker as walker
    import memory.encoding_journal as ej
    import memory.promotion as prom

    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(parents=True, exist_ok=True)
    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)
    (mem_root / "tmp_writes").mkdir(parents=True, exist_ok=True)
    (mem_root / "indexes").mkdir(parents=True, exist_ok=True)
    index_path = mem_root / "indexes" / "coord_index.json"
    index_path.write_text("{}", encoding="utf-8")
    quarantine_dir = tmp_path / "quarantine"
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(prom, "STAGING_DIR", staging_dir)
    monkeypatch.setattr(prom, "QUARANTINE_DIR", quarantine_dir)
    monkeypatch.setattr(prom, "QUARANTINE_INDEX", quarantine_dir / "quarantine_index.json")
    monkeypatch.setattr(vw, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(vw, "TMP_DIR", mem_root / "tmp_writes")
    monkeypatch.setattr(vw, "INDEX_PATH", index_path)
    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr(walker, "INDEX_PATH", index_path)
    ej.JOURNAL_PATH = tmp_path / "encoding_journal.jsonl"
    with walker._COORD_INDEX_LOCK:
        walker.COORD_INDEX.clear()
    walker._coord_index_mtime = 0.0
    return tmp_path


def test_commit_flagged_restores_prior_on_encoding_failure(isolated_memory, monkeypatch):
    """UNCERTAIN: failed flagged re-write must restore prior canonical bytes."""
    import uuid
    from types import SimpleNamespace

    from memory.engine.validate_write import final_path_for_meta
    from memory.promotion import commit_flagged, create_staging

    suffix = uuid.uuid4().hex[:8]
    mem_id = f"9.01.{suffix}"
    prior = {
        "id": mem_id,
        "coord": [9, 1, 0],
        "type": "episode",
        "title": "prior-good",
        "summary": "keep me",
        "text": '{"episode_id":"ep-prior"}',
        "source": "lobe:temporal",
        "created": "2026-06-07T12:00:00+00:00",
        "modified": "2026-06-07T12:00:00+00:00",
        "tags": [],
        "provenance": {
            "origin_id": "cycle-prior",
            "origin_type": "test",
            "author": "test",
            "ingested_at": "2026-06-07T12:00:00+00:00",
            "confidence": 0.5,
        },
        "flags": {"canonical": False, "private": False},
        "audit": [],
    }
    prior_path = final_path_for_meta(prior)
    prior_path.parent.mkdir(parents=True, exist_ok=True)
    prior_path.write_text(json.dumps(prior), encoding="utf-8")

    envelope = {
        "cycle_id": f"cycle-{suffix}",
        "id": mem_id,
        "coord": [9, 1, 0],
        "type": "episode",
        "payload": {
            "id": mem_id,
            "title": "new-flagged",
            "summary": "overwrite attempt",
            "text": json.dumps({"episode_id": f"ep-{suffix}", "intent": "LEARN"}),
        },
        "source": "lobe:temporal",
        "confidence": 0.5,
        "provenance": [],
        "timestamp_created": None,
        "decay_policy": None,
        "contradiction_state": None,
        "permissions": {},
        "version": 1,
        "strength_index": 0.5,
    }
    staging_ref = create_staging(envelope)
    monkeypatch.setattr("memory.rollback.OMEGA_ROOT", isolated_memory)
    monkeypatch.setattr(
        "memory.encoding.encode_committed_memory",
        lambda *a, **k: SimpleNamespace(
            ok=False,
            failed_step="embed",
            failure_reason="induced",
            steps_completed=["canonical_commit"],
            # Match the real EncodingResult shape — commit_flagged reads
            # enc.is_identity_write; the stub omitting it raised AttributeError.
            is_identity_write=False,
        ),
    )
    ok, ref, _ = commit_flagged(
        staging_ref,
        "test-approver",
        flag_reason="ambiguous",
        response_mode="hedge",
    )
    assert ok is False
    assert ref is None
    assert json.loads(prior_path.read_text(encoding="utf-8"))["title"] == "prior-good"


def test_reflection_queue_failure_is_best_effort(isolated_memory, monkeypatch):
    """LOW-11: reflection queue append failure must not abort encoding."""
    import uuid
    from types import SimpleNamespace

    from memory.encoding import encode_committed_memory
    from memory.engine.validate_write import atomic_write
    from memory.source_ledger.store import record_source

    suffix = uuid.uuid4().hex[:8]
    mem_id = f"9.01.{suffix}"
    text = json.dumps({"episode_id": f"ep-{suffix}", "intent": "LEARN"})
    artifact = record_source(
        text,
        source_type="lobe:temporal",
        origin_id=f"cycle-{suffix}",
        metadata={
            "source_session_id": f"cycle-{suffix}",
            "source_session_timestamp": "2026-06-07T12:00:00+00:00",
            "created_at": "2026-06-07T12:00:00+00:00",
        },
        root=isolated_memory,
    )
    mem = {
        "id": mem_id,
        "coord": [9, 1, 0],
        "type": "episode",
        "title": f"ep-{suffix}",
        "summary": f"episode {suffix}",
        "text": text,
        "source": "lobe:temporal",
        "created": "2026-06-07T12:00:00+00:00",
        "modified": "2026-06-07T12:00:00+00:00",
        "tags": [],
        "provenance": {
            "origin_id": f"cycle-{suffix}",
            "origin_type": "test",
            "author": "test",
            "ingested_at": "2026-06-07T12:00:00+00:00",
            "confidence": 0.5,
            **artifact.citation(),
        },
        "flags": {"canonical": False, "private": False},
        "audit": [{"when": "2026-06-07T12:00:00+00:00", "what": "ingested", "by": "test"}],
    }
    final_path = atomic_write(mem, update_index=True)
    monkeypatch.setattr("memory.queues.enqueue_reflection_eligible", lambda *a, **k: False)
    monkeypatch.setattr("memory.queues.enqueue_deepsleep_candidate", lambda *a, **k: True)
    monkeypatch.setattr(
        "memory.strength_index.ensure_strength_index",
        lambda: SimpleNamespace(register=lambda *a, **k: None),
    )
    result = encode_committed_memory(mem, final_path, skip_embed=True)
    assert result.ok
    assert "reflection_eligibility" not in result.steps_completed

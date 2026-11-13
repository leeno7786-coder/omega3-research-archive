"""The per-episode learning-map foldback — the DELTA substrate Omega diffs against to
self-direct. We hand it the data (knowledge gap + application/execution gap per topic,
which topics are still EMPTY, the rollup) and let IT decide. No prescription, no menu.
See feedback_minimal_scaffolding_omega_self_directs."""

from __future__ import annotations

from deepsleep_loop._deepsleep_state import MasteryRecord
from deepsleep_loop import mastery_store


def _seed(tmp_path, monkeypatch, records):
    p = tmp_path / "mastery.json"
    monkeypatch.setattr("pressure_loop.policy.deepsleep_mastery_path", lambda policy=None: p)
    for r in records:
        mastery_store.upsert_mastery(r, p)
    return p


def test_learning_map_exposes_both_deltas_and_flags_empties(tmp_path, monkeypatch):
    from omega_loop.episode.packet import build_learning_map

    _seed(
        tmp_path,
        monkeypatch,
        [
            MasteryRecord(
                domain_id="python",
                domain_name="Python",
                knowledge_mastery=0.60,
                application_mastery=0.20,
                target_mastery=0.70,
                status="operational",
            ),
            MasteryRecord(
                domain_id="decorators",
                domain_name="Decorators",
                knowledge_mastery=0.0,
                application_mastery=0.0,
                target_mastery=0.70,
                status="learning",
                parent="python",
            ),
        ],
    )

    m = build_learning_map()
    by = {r["domain"]: r for r in m["topics"]}

    # Both deltas surfaced (knowledge = "learned", application = "execution").
    assert by["Python"]["knowledge_delta"] == 0.10
    assert by["Python"]["application_delta"] == 0.50
    assert by["Python"]["empty"] is False
    # An untouched topic Omega created is flagged EMPTY with full deltas.
    assert by["Decorators"]["empty"] is True
    assert by["Decorators"]["knowledge_delta"] == 0.70
    assert by["Decorators"]["application_delta"] == 0.70
    # Rollup so Omega sees the whole picture.
    assert m["total"] == 2
    assert m["learned_count"] == 1
    assert m["empty_count"] == 1


def test_learning_map_orders_weakest_first(tmp_path, monkeypatch):
    from omega_loop.episode.packet import build_learning_map

    _seed(
        tmp_path,
        monkeypatch,
        [
            MasteryRecord(
                domain_id="a",
                domain_name="A",
                knowledge_mastery=0.5,
                application_mastery=0.5,
                target_mastery=0.7,
            ),
            MasteryRecord(
                domain_id="b",
                domain_name="B",
                knowledge_mastery=0.0,
                application_mastery=0.0,
                target_mastery=0.7,
            ),
            MasteryRecord(
                domain_id="c",
                domain_name="C",
                knowledge_mastery=0.2,
                application_mastery=0.1,
                target_mastery=0.7,
            ),
        ],
    )

    topics = build_learning_map()["topics"]
    assert [t["domain"] for t in topics] == ["B", "C", "A"]  # least-known first


def test_learning_map_excludes_already_mastered(tmp_path, monkeypatch):
    from omega_loop.episode.packet import build_learning_map

    _seed(
        tmp_path,
        monkeypatch,
        [
            MasteryRecord(
                domain_id="done",
                domain_name="Done",
                knowledge_mastery=0.95,
                application_mastery=0.95,
                target_mastery=0.70,
                status="mastered",
            ),
            MasteryRecord(
                domain_id="open",
                domain_name="Open",
                knowledge_mastery=0.1,
                application_mastery=0.0,
                target_mastery=0.70,
            ),
        ],
    )

    m = build_learning_map()
    domains = {t["domain"] for t in m["topics"]}
    assert "Open" in domains
    assert "Done" not in domains  # both deltas closed -> not an open gap
    assert m["mastered_count"] == 1


def test_learning_map_failure_soft_when_no_store(tmp_path, monkeypatch):
    from omega_loop.episode.packet import build_learning_map

    monkeypatch.setattr(
        "pressure_loop.policy.deepsleep_mastery_path",
        lambda policy=None: tmp_path / "missing.json",
    )
    m = build_learning_map()
    assert m["topics"] == []
    assert m["total"] == 0

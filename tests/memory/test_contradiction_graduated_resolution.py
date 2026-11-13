"""Graduated conflict resolution — decisive losers tombstoned, near-ties demoted.

DeepSleep's conflict reconciliation now weighs each contradiction by lobe-wide corroboration and
resolves it graduated:
  - a DECISIVELY out-corroborated NON-canonical loser is TOMBSTONED (reversibly removed),
  - a near-tie / non-decisive loser takes the softer penalty+demote path (never a coin-flip delete),
  - a CANONICAL loser is IMMUNE (never auto-removed),
  - a subject-mismatch false positive (the frozen title-only partitioner over-flagging) is CLEARED
    with nothing removed,
DeepSleep runs this pass before its one synchronous graph rebuild.
"""

from __future__ import annotations

import json

import pytest

import memory.contradiction_resolution as cr
from memory.queues import enqueue_deepsleep_candidate


@pytest.fixture
def memory_tree(tmp_path, monkeypatch):
    import memory.engine.walker as walker
    import memory.queues as queues

    mem_root = tmp_path / "memory"
    mem_root.mkdir(parents=True, exist_ok=True)
    queue_dir = tmp_path / "queues"
    queue_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(walker, "MEMORY_ROOT", mem_root)
    monkeypatch.setattr("memory.engine.validate_write.MEMORY_ROOT", mem_root, raising=False)
    monkeypatch.setattr(queues, "DEEPSLEEP_QUEUE_PATH", queue_dir / "deepsleep_candidates.jsonl")
    state = {"archive": tmp_path / "tombstones", "root": mem_root}

    # Tombstone archives under guardian/state by default; redirect it into tmp for isolation by
    # patching the module constant the primitive reads.
    monkeypatch.setattr("memory.tombstone.TOMBSTONE_DIR", state["archive"], raising=False)

    def _write(
        mid, *, subject=None, facet="name", value="", confidence=0.5, canonical=False, entities=None
    ):
        path = mem_root / f"{mid}.json"
        doc = {
            "id": mid,
            "coord": [1, 0, 0],
            "provenance": {"confidence": confidence},
            "flags": {"canonical": canonical},
            "title": "Operator",
            "summary": f"operator {facet} {value}",
        }
        ents = entities
        if ents is None and subject is not None:
            ents = [(subject, facet, value)]
        if ents:
            doc["enrichment"] = {
                "entities": [{"subject": s, "facet": f, "name": v} for (s, f, v) in ents]
            }
        path.write_text(json.dumps(doc), encoding="utf-8")
        with walker._COORD_INDEX_LOCK:
            walker.COORD_INDEX[mid] = {
                "id": mid,
                "coord": [1, 0, 0],
                "path": str(path),
                "title": "Operator",
                "type": "persona",
            }
        return path

    state["write"] = _write
    return state


def _enqueue_pair(canonical_id, loser_id):
    reason = f"contradiction:canonical={canonical_id};evidence={loser_id}"
    enqueue_deepsleep_candidate(canonical_id, [1, 0, 0], reason=reason)
    enqueue_deepsleep_candidate(loser_id, [1, 0, 0], reason=reason)


def test_decisive_loser_is_tombstoned_before_deepsleep_reindex(memory_tree):
    write = memory_tree["write"]
    # "Noah" corroborated by 3 records; "Kim" only once -> decisive -> tombstone.
    noah = write("1.00.noah", subject="operator", value="noah", confidence=0.6)
    write("1.00.noah_a", subject="operator", value="noah", confidence=0.6)
    write("1.00.noah_b", subject="operator", value="noah", confidence=0.6)
    kim_path = write("1.00.kim", subject="operator", value="kim", confidence=0.5)
    _enqueue_pair("1.00.noah", "1.00.kim")

    report = cr.process_contradiction_candidates()

    row = report["adjudications"][0]
    assert row["winner_id"] == "1.00.noah"
    assert row["loser_id"] == "1.00.kim"
    assert row["persist"]["resolution"] == "tombstoned"
    assert report["tombstoned_count"] == 1
    assert not kim_path.exists()  # loser reversibly removed
    assert (memory_tree["archive"] / "1.00.kim.json").exists()  # archived
    assert noah.exists()  # winner untouched


def test_near_tie_is_demoted_not_tombstoned(memory_tree):
    write = memory_tree["write"]
    # both values asserted once -> non-decisive -> soft demote, NOT a delete.
    write("1.00.alpha", subject="operator", value="alpha", confidence=0.6)
    beta_path = write("1.00.beta", subject="operator", value="beta", confidence=0.5)
    beta_doc = json.loads(beta_path.read_text(encoding="utf-8"))
    beta_doc["type"] = "semantic_fact"  # give demotion a visible target
    beta_path.write_text(json.dumps(beta_doc), encoding="utf-8")
    _enqueue_pair("1.00.alpha", "1.00.beta")

    report = cr.process_contradiction_candidates()

    row = report["adjudications"][0]
    assert row["decisive"] is False
    assert row["persist"]["resolution"] in ("demoted", "penalized")
    assert report["tombstoned_count"] == 0
    assert beta_path.exists()  # NOT removed on a near-tie
    # HONESTY (review finding 2): this flat-index fixture record is not schema-valid, so persistence
    # routes through the raw-path fallback, not the production atomic_write branch (prod records ARE
    # schema-valid). Assert the path explicitly so the coverage boundary is visible, not masked.
    assert row["persist"].get("fallback_write") is True


def test_canonical_loser_is_immune(memory_tree):
    write = memory_tree["write"]
    # A canonical record can never win the rank tuple over a higher signal; force the canonical to
    # be the loser by making the challenger corroborated + higher confidence, and assert immunity.
    canon_path = write(
        "1.00.canon", subject="operator", value="old", confidence=0.4, canonical=True
    )
    write("1.00.new", subject="operator", value="new", confidence=0.9)
    write("1.00.new_b", subject="operator", value="new", confidence=0.9)
    # reason pivot is arbitrary; adjudicator ranks regardless of position.
    _enqueue_pair("1.00.new", "1.00.canon")

    report = cr.process_contradiction_candidates()

    row = report["adjudications"][0]
    assert row["loser_id"] == "1.00.canon"
    # canonical loser is never tombstoned nor penalized.
    assert report["tombstoned_count"] == 0
    assert canon_path.exists()
    persist = row["persist"]
    assert persist.get("resolution") != "tombstoned"
    assert persist.get("applied", False) is False or persist.get("reason") == "canonical_immune"


def test_subject_mismatch_is_cleared_not_removed(memory_tree):
    write = memory_tree["write"]
    # Different subjects sharing a title -> the frozen partitioner over-flagged; adjudicate clears.
    abel = write("1.01.abel", subject="abel", value="abel", confidence=0.6)
    op = write("1.00.op", subject="operator", value="noah", confidence=0.6)
    _enqueue_pair("1.01.abel", "1.00.op")

    report = cr.process_contradiction_candidates()

    row = report["adjudications"][0]
    assert row["conflict"] is False
    assert row["persist"]["reason"] == "not_a_conflict"
    assert report["tombstoned_count"] == 0
    assert abel.exists() and op.exists()  # nothing removed


def test_overflow_groups_are_re_enqueued_not_dropped(memory_tree):
    # Review finding 1: take-all drain + max_groups cap must NOT silently drop overflow groups.
    write = memory_tree["write"]
    for i, (w, lo) in enumerate([("a1", "a2"), ("b1", "b2"), ("c1", "c2")]):
        write(f"1.00.{w}", subject="operator", facet=f"f{i}", value="win", confidence=0.6)
        write(f"1.00.{lo}", subject="operator", facet=f"f{i}", value="lose", confidence=0.5)
        _enqueue_pair(f"1.00.{w}", f"1.00.{lo}")

    report = cr.process_contradiction_candidates(max_groups=2)
    assert report["groups_processed"] == 2

    from memory.queues import drain_deepsleep_candidates

    remaining = drain_deepsleep_candidates()
    reasons = {e.get("reason") for e in remaining}
    assert len(reasons) == 1  # exactly the one un-processed group survived in the queue
    assert all("contradiction:" in r for r in reasons)


def test_ghost_winner_prevented_by_cache_invalidation(memory_tree):
    # Review finding 5: a record tombstoned in an earlier group must NOT serve from a stale cache
    # as the winning anchor of a later group and cause a second (wrong) tombstone.
    write = memory_tree["write"]
    # A loses the CITY axis to B (london 2 vs paris 1) -> A tombstoned. A would then WIN the EMPLOYER
    # axis over C (acme 2 vs globex 1) from stale cache -> C wrongly tombstoned. The fix skips it.
    a = write(
        "1.00.aaa", entities=[("operator", "city", "paris"), ("operator", "employer", "acme")]
    )
    write("1.00.bbb", entities=[("operator", "city", "london")], confidence=0.5)
    write("1.00.lon2", entities=[("operator", "city", "london")])
    write("1.00.acme2", entities=[("operator", "employer", "acme")])
    c_path = write("1.00.ccc", entities=[("operator", "employer", "globex")], confidence=0.5)
    _enqueue_pair("1.00.aaa", "1.00.bbb")  # group 1 (processed first): A loses -> tombstoned
    _enqueue_pair("1.00.aaa", "1.00.ccc")  # group 2: A is a ghost winner unless cache invalidated

    report = cr.process_contradiction_candidates()

    assert not a.exists()  # A genuinely tombstoned in group 1
    assert c_path.exists()  # C spared: group 2 skipped because A's cache entry was invalidated
    assert report["tombstoned_count"] == 1  # only A, never C
    evidence_ids = {row.get("evidence_id") for row in report["adjudications"]}
    assert "1.00.ccc" not in evidence_ids  # group 2 never adjudicated (ghost anchor gone)


def test_decisive_tombstone_failure_falls_back_to_demotion(memory_tree, monkeypatch):
    # Review finding 3: if a decisive loser's tombstone FAILS, it must still be weakened
    # (penalized/demoted), never left fully intact.
    write = memory_tree["write"]
    write("1.00.noah", subject="operator", value="noah", confidence=0.6)
    write("1.00.noah_a", subject="operator", value="noah", confidence=0.6)
    write("1.00.noah_b", subject="operator", value="noah", confidence=0.6)
    kim_path = write("1.00.kim", subject="operator", value="kim", confidence=0.5)
    _enqueue_pair("1.00.noah", "1.00.kim")

    monkeypatch.setattr(
        "memory.tombstone.tombstone_memory_record",
        lambda *a, **k: {"tombstoned": False, "reason": "archive_failed"},
    )

    report = cr.process_contradiction_candidates()

    row = report["adjudications"][0]
    assert row["loser_id"] == "1.00.kim"
    assert report["tombstoned_count"] == 0  # tombstone failed
    assert row["persist"]["resolution"] in ("demoted", "penalized")  # but still weakened
    assert kim_path.exists()  # not lost — the soft fallback keeps the record
    updated = json.loads(kim_path.read_text(encoding="utf-8"))
    assert updated["provenance"]["confidence"] < 0.5  # penalty applied

"""Cut 4 — embedding-free (native) canonical citizenship for NON-identity writes.

Governing: 14, 16 (INV-MO-018), 17 §6 Stage C / Gate C, the Cut 4 plan.

    canonical durability != projection freshness

A non-identity canonical write is fully valid without a vector; it emits
deterministic projection work for the later projector. Identity stays on its
legacy embedding path until Cut 12 — deliberate containment.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

# ── Task 2: citizenship v2 receipt ──────────────────────────────────────────


def test_citizenship_does_not_require_current_dme_projection():
    from memory.citizenship import MemoryCitizenshipReceipt

    receipt = MemoryCitizenshipReceipt(
        memory_id="m1",
        canonical_path="memory/200_knowledge/a.json",
        canonical_committed=True,
        indexed=True,
        strength_registered=True,
        reflection_eligible=True,
        projection_eligible=True,
        projection_pending=True,
        projection_delta_id="d1",
        identity_continuity_status="not_applicable",
        steps_completed=("canonical", "index", "strength", "projection_outbox"),
    )
    assert receipt.active_canonical is True
    assert receipt.projection_pending is True
    assert receipt.schema_version == "omega.memory_citizenship_receipt.v2"
    payload = receipt.to_dict()
    assert "dme_addressable" not in payload  # v1 requirement superseded
    assert "projection_current" not in payload
    assert receipt.validate() == []


def test_active_canonical_requires_canonical_steps_but_not_projection():
    from memory.citizenship import MemoryCitizenshipReceipt

    def r(**kw):
        base = dict(memory_id="m1", canonical_path="p", canonical_committed=True, indexed=True,
                    strength_registered=True, reflection_eligible=True, projection_eligible=True,
                    projection_pending=True, projection_delta_id=None, identity_continuity_status="not_applicable",
                    steps_completed=())
        base.update(kw)
        return MemoryCitizenshipReceipt(**base)

    assert r().active_canonical is True
    assert r(projection_pending=False, projection_delta_id="d").active_canonical is True  # projection freshness irrelevant
    assert r(canonical_committed=False).active_canonical is False
    assert r(indexed=False).active_canonical is False
    assert r(strength_registered=False).active_canonical is False
    assert r(reflection_eligible=False).active_canonical is False
    assert r(projection_eligible=False).active_canonical is False
    assert r(identity_continuity_status="pass").active_canonical is True
    for bad in ("abstain", "conflict"):
        assert r(identity_continuity_status=bad).active_canonical is False, bad


def test_identity_continuity_statuses_are_frozen():
    from memory.citizenship import IDENTITY_CONTINUITY_STATUSES, MemoryCitizenshipReceipt

    assert IDENTITY_CONTINUITY_STATUSES == {"not_applicable", "pass", "abstain", "conflict"}
    bad = MemoryCitizenshipReceipt(memory_id="m1", canonical_path="p", canonical_committed=True, indexed=True,
                                   strength_registered=True, reflection_eligible=True, projection_eligible=True,
                                   projection_pending=False, projection_delta_id=None,
                                   identity_continuity_status="vibes", steps_completed=())
    assert any("identity_continuity_status" in e for e in bad.validate())
    assert bad.active_canonical is False
    empty = MemoryCitizenshipReceipt(memory_id="", canonical_path="p", canonical_committed=True, indexed=True,
                                     strength_registered=True, reflection_eligible=True, projection_eligible=True,
                                     projection_pending=False, projection_delta_id=None,
                                     identity_continuity_status="not_applicable", steps_completed=())
    assert any("memory_id" in e for e in empty.validate())


# ── Task 3: native non-identity encoding path ───────────────────────────────

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")

from tests.memory.native_fixtures import (  # noqa: E402
    _identity_memory_dict,
    _lobe9_memory_dict,
    _committed_path,
    _env_for,
)
from tests.memory.native_fixtures import isolated_memory  # noqa: E402,F401,F811


def _forbid_embed(monkeypatch):
    """Dense embedding modules are absent, so there is no call to patch."""
    return lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("retired dense embedding path was called")
    )


def test_native_nonidentity_encoding_never_calls_embed(isolated_memory, monkeypatch):  # noqa: F811
    from memory.encoding import encode_committed_memory
    from memory.engine.validate_write import atomic_write
    from memory.strength_index import ensure_strength_index

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    final_path = atomic_write(mem, update_index=True)
    result = encode_committed_memory(mem, final_path)
    assert result.ok is True, (result.failed_step, result.failure_reason)
    assert "embed" not in result.steps_completed
    assert "embed_persist" not in result.steps_completed
    assert "embed_skipped" not in result.steps_completed  # native is its own explicit step
    assert "native_projection_eligible" in result.steps_completed
    assert not mem.get("embeddings")
    # existing bookkeeping still runs, exactly as before
    assert "persistence_anchor" in result.steps_completed
    assert "reflection_eligibility" in result.steps_completed
    assert "deepsleep_candidacy" in result.steps_completed
    assert "projection_receipt" in result.steps_completed  # SourceLedger binding (not DME projection)
    assert ensure_strength_index().get_record(mem["id"]) is not None


def test_identity_is_native_and_never_enters_legacy_embedding_path(isolated_memory, monkeypatch):  # noqa: F811
    from memory.encoding import encode_committed_memory
    from memory.engine.validate_write import atomic_write

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    mem = _identity_memory_dict()
    final_path = atomic_write(mem, update_index=True)
    result = encode_committed_memory(mem, final_path)
    assert result.ok is True
    assert result.is_identity_write is True
    assert "native_projection_eligible" in result.steps_completed
    assert not mem.get("embeddings")


def test_native_mode_keeps_nonsemantic_trait_bypass_filesystem_only(isolated_memory, monkeypatch):  # noqa: F811
    from memory.encoding import encode_committed_memory
    from memory.engine.validate_write import atomic_write

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    mem["type"] = "trait_observation"
    final_path = atomic_write(mem, update_index=True)
    result = encode_committed_memory(mem, final_path)
    assert result.ok is True
    assert result.steps_completed == ["non_semantic_bypass", "projection_receipt"]
    assert "native_projection_eligible" not in result.steps_completed
    from memory.projection_receipt import memory_root_for_canonical_path, verify_projection_receipt

    assert verify_projection_receipt(mem, root=memory_root_for_canonical_path(final_path)).ok


def test_native_flag_off_cannot_restore_retired_dense_path(isolated_memory, monkeypatch):  # noqa: F811
    from memory.encoding import encode_committed_memory
    from memory.engine.validate_write import atomic_write

    monkeypatch.delenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", raising=False)
    mem = _lobe9_memory_dict()
    final_path = atomic_write(mem, update_index=True)
    r = encode_committed_memory(mem, final_path)
    assert r.ok is True
    assert "native_projection_eligible" in r.steps_completed
    assert not mem.get("embeddings")


# ── Task 4: promotion native branch → ProjectionDelta; outbox-failure posture ─

def _outbox_for(final_path):
    from memory.projection_delta import pending_projection_deltas, projection_dirty_state
    from memory.projection_receipt import memory_root_for_canonical_path

    root = memory_root_for_canonical_path(final_path)
    deltas, malformed = pending_projection_deltas(root=root)
    return root, deltas, malformed, projection_dirty_state(root=root)


def test_native_promotion_commits_without_vector_and_queues_projection_work(isolated_memory, monkeypatch):  # noqa: F811
    from memory.promotion import commit_promotion, create_staging
    from memory.strength_index import ensure_strength_index

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    ref = create_staging(_env_for(mem))
    committed, canonical_ref, info = commit_promotion(ref, "test:native")
    assert committed is True and canonical_ref == mem["id"], info
    final_path = _committed_path(mem["id"])
    assert final_path
    on_disk = json.loads(Path(final_path).read_text(encoding="utf-8"))
    assert not on_disk.get("embeddings")  # no dense block created
    assert ensure_strength_index().get_record(mem["id"]) is not None
    # exactly one ProjectionDelta for this memory, in the outbox beside the canonical tree
    root, deltas, malformed, dirty = _outbox_for(final_path)
    mine = [d for d in deltas if d.memory_id == mem["id"]]
    assert len(mine) == 1 and malformed == ()
    d = mine[0]
    assert d.operation == "upsert" and d.projection_class == "semantic"
    assert d.canonical_path == str(final_path) or Path(d.canonical_path).resolve() == Path(final_path).resolve()
    assert d.canonical_hash == hashlib.sha256(Path(final_path).read_bytes()).hexdigest()
    assert tuple(d.coord) == tuple(mem["coord"])
    assert dirty == {"dirty": False}
    # additive receipt/rollback metadata: honest projection_pending; legacy defer flag off
    assert info["projection_pending"] is True
    assert info["projection_delta_id"] == d.delta_id
    assert info["projection_dirty"] is False
    assert "native_projection_eligible" in info["encoding_steps"]


def test_native_projection_delta_records_current_commit_seq(isolated_memory, monkeypatch):  # noqa: F811
    from core.timekeeper import last_commit_seq
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    committed, _ref, info = commit_promotion(create_staging(_env_for(mem)), "test:seq")
    assert committed
    _root, deltas, _m, _d = _outbox_for(_committed_path(mem["id"]))
    d = next(x for x in deltas if x.memory_id == mem["id"])
    assert d.commit_seq == last_commit_seq()


def test_outbox_failure_never_rolls_back_canonical_and_marks_projection_dirty(isolated_memory, monkeypatch):  # noqa: F811
    """LAW-EO-004: canonical write succeeds, projection enqueue fails →
    canonical remains valid; loud durable dirty marker; degraded receipt;
    NO rollback, NO false 'projection current' claim."""
    import memory.projection_delta as pd
    from memory.promotion import commit_promotion, create_staging
    from memory.strength_index import ensure_strength_index

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)

    def _disk_full(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(pd, "_append_line_fsync", _disk_full)
    mem = _lobe9_memory_dict()
    ref = create_staging(_env_for(mem))
    committed, canonical_ref, info = commit_promotion(ref, "test:outbox-fail")
    assert committed is True and canonical_ref == mem["id"]  # canonical survives
    final_path = _committed_path(mem["id"])
    assert final_path and Path(final_path).exists()
    assert ensure_strength_index().get_record(mem["id"]) is not None
    root, deltas, _m, dirty = _outbox_for(final_path)
    assert not [d for d in deltas if d.memory_id == mem["id"]]  # nothing was appended
    assert dirty["dirty"] is True and dirty["memory_id"] == mem["id"]
    assert "outbox" in dirty["reason"] or "append" in dirty["reason"]
    assert info["projection_pending"] is True
    assert info["projection_delta_id"] is None
    assert info["projection_dirty"] is True
    # staging consumed, journal closed: the canonical commit is FINAL
    from memory.promotion import _staging_path

    assert not _staging_path(ref).exists()


def test_native_write_memory_direct_commit_receipt_reports_projection_pending(isolated_memory, monkeypatch):  # noqa: F811
    from memory.api import CanonicalEnvelope, write_memory

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    env = CanonicalEnvelope(
        cycle_id=f"native-write-{mem['id']}", coord=mem["coord"], type=mem["type"],
        payload={"id": mem["id"], "title": mem["title"], "summary": mem["summary"], "text": mem["text"]},
        source=mem["source"], id=mem["id"],
    )
    receipt = write_memory(env, propose=False, bypass_guardian=True)
    assert receipt.validation_status == "committed"
    assert receipt.projection_pending is True
    assert receipt.projection_delta_id


def test_native_flag_off_promotion_still_uses_ettin_projection(isolated_memory, monkeypatch):  # noqa: F811
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.delenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", raising=False)
    mem = _lobe9_memory_dict()
    committed, _ref, info = commit_promotion(create_staging(_env_for(mem)), "test:legacy")
    assert committed is True
    assert info.get("projection_pending") is True
    assert info.get("projection_delta_id")
    _root, deltas, _m, _d = _outbox_for(_committed_path(mem["id"]))
    assert [d for d in deltas if d.memory_id == mem["id"]]


def test_native_commit_flagged_is_embedding_free_and_queues_projection_work(isolated_memory, monkeypatch):  # noqa: F811
    from memory.promotion import commit_flagged, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    committed, canonical_ref, info = commit_flagged(
        create_staging(_env_for(mem)), "test:flagged", flag_reason="sensitive_persona", response_mode="hedge"
    )
    assert committed is True and canonical_ref == mem["id"], info
    assert info["flagged"] is True and info["projection_pending"] is True and info["projection_delta_id"]
    _root, deltas, malformed, dirty = _outbox_for(_committed_path(mem["id"]))
    assert len([d for d in deltas if d.memory_id == mem["id"]]) == 1 and malformed == () and dirty == {"dirty": False}


# ── Cut 4 REWORK (CUT4_REVIEW_REWORK.md): durability / projection contracts ─
# 1) the latest delta must hash the FINAL post-hook canonical bytes; peer
#    mutations must never be silently omitted (represented or rebuild-required)
# 2) boot recovery must not delete a FINALIZED canonical write; pre-final
#    crashes still roll back / restore; gaps leave rebuild-required evidence
# 3) Guardian ALLOW/FLAG receipts report the actual projection posture
# 5.1) commit_seq posture is explicit (sequenced only inside the commit region)
# 5.2) dirty marker lands in the canonical record's own profile


def _final_hash(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _latest_delta_for(root, mid):
    from memory.projection_delta import pending_projection_deltas

    deltas, _m = pending_projection_deltas(root=root)
    mine = [d for d in deltas if d.memory_id == mid]
    return mine[-1] if mine else None


def _persona_env(text: str, mid: str, *, title: str):
    """A lobe-1 persona record (ENTITY_BEARING lobe → supersedes + entity
    hooks run). Built the way _lobe9_memory_dict does, but in lobe 1."""
    from memory.engine import validate_write as vw
    from memory.source_ledger.store import record_source

    art = record_source(text, source_type="lobe:persona", origin_id=f"cycle-{mid}", root=vw.MEMORY_ROOT)
    return {
        "cycle_id": f"rw-{mid}", "id": mid, "coord": [1, 0, 0], "type": "concept",
        "payload": {"id": mid, "title": title, "summary": text, "text": text},
        "source": "lobe:persona", "confidence": 0.6, "provenance": [], "timestamp_created": None,
        "decay_policy": None, "contradiction_state": None, "permissions": {}, "version": 1, "strength_index": 0.5,
    }


def test_rework_latest_delta_hashes_final_post_supersedes_bytes_and_prior_is_represented(isolated_memory, monkeypatch):  # noqa: F811
    """Reviewer repro: two successive 'Bob favorite food' records through the
    real native write_memory path. The supersedes hook rewrites BOTH files
    (new: supersedes/version; prior: superseded_by) after commit. The latest
    delta for each must hash the final bytes; the prior's mutation must be
    represented by its own projection work (its path/hash are known)."""
    from memory.api import CanonicalEnvelope, write_memory
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    ids = ["1.00.bobfood1", "1.00.bobfood2"]
    paths = {}
    for i, mid in enumerate(ids):
        e = _persona_env(f"Bob's favorite food is {'pizza' if i == 0 else 'sushi'}", mid, title="Bob favorite food")
        env = CanonicalEnvelope(cycle_id=e["cycle_id"], coord=e["coord"], type=e["type"], payload=e["payload"],
                                source=e["source"], id=mid, confidence=0.6)
        receipt = write_memory(env, propose=False, bypass_guardian=True)
        assert receipt.validation_status == "committed"
        paths[mid] = _committed_path(mid)
        assert paths[mid]
    root = memory_root_for_canonical_path(paths[ids[1]])
    new_file = json.loads(Path(paths[ids[1]]).read_text(encoding="utf-8"))
    prior_file = json.loads(Path(paths[ids[0]]).read_text(encoding="utf-8"))
    assert new_file.get("supersedes") == ids[0], "fixture must exercise the supersedes hook"
    assert prior_file.get("superseded_by") == ids[1]
    # latest delta == final bytes for the NEW record, and it names the supersession
    latest_new = _latest_delta_for(root, ids[1])
    assert latest_new is not None
    assert latest_new.canonical_hash == _final_hash(paths[ids[1]])
    assert latest_new.operation == "supersede"
    # the PRIOR record's canonical mutation is represented by its own latest delta
    latest_prior = _latest_delta_for(root, ids[0])
    assert latest_prior is not None
    assert latest_prior.canonical_hash == _final_hash(paths[ids[0]])
    # write receipt for the second write reports the FINAL delta id, not the pre-hook one
    assert receipt.projection_delta_id == latest_new.delta_id


def test_rework_entity_link_peer_mutation_forces_rebuild_required_or_is_represented(isolated_memory, monkeypatch):  # noqa: F811
    """Entity-link hook writes back-links onto PEER files whose set the hook
    does not report. Correctness-first posture: the new record's final delta
    is emitted AND projection is marked dirty (rebuild-required) whenever a
    hook mutated additional canonical files it cannot enumerate."""
    from memory.api import CanonicalEnvelope, write_memory
    from memory.projection_delta import projection_dirty_state
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    # force the entity linker to report that it mutated peers
    monkeypatch.setattr("memory.enrichment.entity_linker.link_personal_memories", lambda *_a, **_k: 2)
    e = _persona_env("Alice met Carol at the observatory", "1.00.alicecarol", title="Alice and Carol")
    env = CanonicalEnvelope(cycle_id=e["cycle_id"], coord=e["coord"], type=e["type"], payload=e["payload"],
                            source=e["source"], id="1.00.alicecarol", confidence=0.6)
    receipt = write_memory(env, propose=False, bypass_guardian=True)
    assert receipt.validation_status == "committed"
    path = _committed_path("1.00.alicecarol")
    root = memory_root_for_canonical_path(path)
    latest = _latest_delta_for(root, "1.00.alicecarol")
    assert latest is not None and latest.canonical_hash == _final_hash(path)
    dirty = projection_dirty_state(root=root)
    assert dirty["dirty"] is True and "peer" in dirty["reason"]
    assert receipt.projection_pending is True


def test_rework_gate_write_final_delta_after_hooks(monkeypatch):
    """Gate writes run their hooks inline; the delta must be the post-hook bytes."""
    from tests.memory.test_gate_write_embed_backfill import _native_gate
    from memory.projection_receipt import memory_root_for_canonical_path

    gate = _native_gate(monkeypatch)
    monkeypatch.setattr(gate, "_get_strength_index", lambda: None)

    def _mutating_hooks(memory_id, *, coord=None, source=""):
        # emulate a canonical-mutating hook: rewrite the new record
        from memory.engine.walker import COORD_INDEX, load_coord_index, resolve_coord_index_path
        load_coord_index()
        p = resolve_coord_index_path(COORD_INDEX[memory_id]["path"])
        rec = json.loads(Path(p).read_text(encoding="utf-8")); rec["links"] = ["1.00.peer"]
        Path(p).write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return {"memory_id": memory_id, "ran": True, "supersedes": None, "entity_links": 0, "errors": []}

    monkeypatch.setattr("memory.post_commit.run_post_commit_memory_hooks", _mutating_hooks)
    ids: list = []
    path = gate._write("gate write whose hook rewrites the record", [2, 5, 0], None, None, id_out=ids)
    assert path
    latest = _latest_delta_for(memory_root_for_canonical_path(path), ids[0])
    assert latest is not None and latest.canonical_hash == _final_hash(path)


# ── crash-window matrix (boot recovery must respect canonical finalization) ──


def _recover():
    from memory.encoding_journal import recover_unclosed
    from memory.rollback import rollback_canonical_write

    return recover_unclosed(rollback_canonical_write)


@pytest.fixture
def isolated_rollback(isolated_memory, monkeypatch):  # noqa: F811
    """memory.rollback binds OMEGA_ROOT at import; point its path guard at the
    isolated tree so boot-recovery rollback/restore can act on test files."""
    import memory.rollback as rb

    monkeypatch.setattr(rb, "OMEGA_ROOT", isolated_memory)
    return isolated_memory


def _unclosed():
    from memory.encoding_journal import find_unclosed_entries

    return find_unclosed_entries()


def test_rework_crash_after_finalization_before_journal_close_preserves_canonical(isolated_rollback, monkeypatch):  # noqa: F811
    """Reviewer repro: suppress journal_close after a successful native write.
    Boot recovery must NOT delete the canonical file; it must reconcile the
    entry (delta present → close clean; no dirty needed)."""
    from memory.projection_delta import projection_dirty_state
    from memory.projection_receipt import memory_root_for_canonical_path
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    import memory.encoding_journal as ej
    mem = _lobe9_memory_dict()
    with pytest.MonkeyPatch.context() as crash:  # the process dies before journal_close lands
        crash.setattr(ej, "journal_close", lambda *_a, **_k: True)
        committed, _ref, info = commit_promotion(create_staging(_env_for(mem)), "test:crash-a")
    assert committed and info["projection_delta_id"]
    path = _committed_path(mem["id"])
    assert path and Path(path).exists()
    assert len(_unclosed()) == 1
    root = memory_root_for_canonical_path(path)
    before_delta = _latest_delta_for(root, mem["id"])
    results = _recover()
    assert Path(path).exists(), "boot recovery deleted a finalized canonical write"
    assert _unclosed() == []
    assert results and results[0].get("preserved") is True
    assert _latest_delta_for(root, mem["id"]).delta_id == before_delta.delta_id
    assert projection_dirty_state(root=root) == {"dirty": False}  # delta was durable → no rebuild needed


def test_rework_crash_after_finalization_before_delta_append_marks_rebuild_required(isolated_rollback, monkeypatch):  # noqa: F811
    """post-encoding, pre-outbox crash: canonical preserved; no delta exists →
    durable rebuild-required evidence; journal reconciled."""
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)

    class _Crash(BaseException):
        pass

    mem = _lobe9_memory_dict()
    ref = create_staging(_env_for(mem))
    with pytest.MonkeyPatch.context() as crash:  # the process dies inside the outbox append
        crash.setattr(pd, "emit_delta_for_canonical", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            commit_promotion(ref, "test:crash-b")
    path = _committed_path(mem["id"])
    assert path and Path(path).exists()
    root = memory_root_for_canonical_path(path)
    assert _latest_delta_for(root, mem["id"]) is None
    results = _recover()
    assert Path(path).exists()
    assert results and results[0].get("preserved") is True
    assert pd.projection_dirty_state(root=root)["dirty"] is True
    assert _unclosed() == []


def test_rework_crash_before_finalization_still_rolls_back(isolated_rollback, monkeypatch):  # noqa: F811
    """after atomic_write but before native encoding completes: pre-final →
    rollback as before (file + index entry removed)."""
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")

    class _Crash(BaseException):
        pass

    mem = _lobe9_memory_dict()
    ref = create_staging(_env_for(mem))
    with pytest.MonkeyPatch.context() as crash:  # the process dies mid-encoding
        import memory.encoding as enc
        crash.setattr(enc, "encode_committed_memory", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            commit_promotion(ref, "test:crash-c")
    from memory.engine.validate_write import final_path_for_meta
    path = final_path_for_meta(mem)
    assert path.exists() and len(_unclosed()) == 1
    results = _recover()
    assert not path.exists()
    assert results and results[0].get("preserved") is not True and "rollback" in results[0]
    assert _committed_path(mem["id"]) is None
    assert _unclosed() == []


def test_rework_crash_before_finalization_on_overwrite_restores_prior_record(isolated_rollback, monkeypatch):  # noqa: F811
    """prior-record overwrite/restore case: a re-write that dies before
    finalization must not destroy the previously committed record."""
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    committed, _r, _i = commit_promotion(create_staging(_env_for(mem)), "test:first")
    assert committed
    path = Path(_committed_path(mem["id"]))
    prior_bytes = path.read_bytes()

    class _Crash(BaseException):
        pass

    # a genuinely different re-write of the same id (avoids idempotent reuse)
    mem2 = dict(mem); mem2["summary"] = "rewritten summary"; mem2["text"] = json.dumps({"episode_id": "ep-rewrite", "intent": "LEARN"})
    ref = create_staging(_env_for(mem2))
    with pytest.MonkeyPatch.context() as crash:  # dies mid-encoding of the re-write
        import memory.encoding as enc
        crash.setattr(enc, "encode_committed_memory", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            commit_promotion(ref, "test:rewrite-crash")
    assert path.read_bytes() != prior_bytes  # overwritten in place before the crash
    results = _recover()
    assert path.exists(), "restore case must not delete the prior committed record"
    assert json.loads(path.read_text(encoding="utf-8"))["summary"] == mem["summary"]
    assert results and results[0].get("restored") is True
    assert _committed_path(mem["id"]) is not None


def test_rework_outbox_failure_plus_crash_leaves_rebuild_required_evidence(isolated_rollback, monkeypatch):  # noqa: F811
    import memory.encoding_journal as ej
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    monkeypatch.setattr(pd, "_append_line_fsync", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
    mem = _lobe9_memory_dict()
    with pytest.MonkeyPatch.context() as crash:  # and then the process dies before journal_close
        crash.setattr(ej, "journal_close", lambda *_a, **_k: True)
        committed, _r, info = commit_promotion(create_staging(_env_for(mem)), "test:outbox+crash")
    assert committed and info["projection_dirty"] is True
    path = _committed_path(mem["id"])
    root = memory_root_for_canonical_path(path)
    assert pd.projection_dirty_state(root=root)["dirty"] is True  # durable evidence already
    results = _recover()
    assert Path(path).exists() and results and results[0].get("preserved") is True
    assert pd.projection_dirty_state(root=root)["dirty"] is True


# ── Guardian receipt posture ────────────────────────────────────────────────


def test_rework_guardian_allow_and_quarantine_receipts_report_native_projection_posture(isolated_memory, monkeypatch):  # noqa: F811
    """Guardian seam with the decision faked: ALLOW carries the real native
    posture; a non-committed QUARANTINE never pretends to be projection work.
    (Rework 2 §7: the REAL ALLOW and REAL FLAG dispatch is exercised by
    test_rw2_real_guardian_allow_and_flag_receipts_report_final_projection_posture.)"""
    from types import SimpleNamespace

    from memory.api import CanonicalEnvelope, write_memory
    import memory.api as api

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    from memory.api import _USER_CHAT_SOURCE_PREFIXES

    chat_source = sorted(_USER_CHAT_SOURCE_PREFIXES)[0].rstrip(":") + ":test"
    env = CanonicalEnvelope(cycle_id="g-allow", coord=mem["coord"], type=mem["type"],
                            payload={"id": mem["id"], "title": mem["title"], "summary": mem["summary"], "text": mem["text"]},
                            source=chat_source, id=mem["id"])
    calls = {}

    def fake_decide(staging_ref, *, approver_id):
        # a real native commit behind the Guardian decision
        from memory.promotion import commit_promotion
        committed, cref, info = commit_promotion(staging_ref, approver_id)
        calls["info"] = info
        return SimpleNamespace(committed=committed, canonical_ref=cref, kind="ALLOW", rollback_plan=dict(info or {}))

    monkeypatch.setattr("guardian.decision.decide_request", fake_decide)
    receipt = write_memory(env, propose=True, bypass_guardian=False)
    assert receipt.validation_status == "committed"
    assert receipt.projection_pending is True
    assert receipt.projection_delta_id
    assert receipt.projection_delta_id == _latest_delta_for(
        __import__("memory.projection_receipt", fromlist=["x"]).memory_root_for_canonical_path(_committed_path(mem["id"])), mem["id"]
    ).delta_id
    # quarantine / non-committed decisions never pretend to be projection work
    monkeypatch.setattr("guardian.decision.decide_request", lambda *_a, **_k: SimpleNamespace(
        committed=False, canonical_ref=None, kind="QUARANTINE", rollback_plan={"projection_pending": True, "projection_delta_id": "x"}))
    mem2 = _lobe9_memory_dict()
    env2 = CanonicalEnvelope(cycle_id="g-q", coord=mem2["coord"], type=mem2["type"],
                             payload={"id": mem2["id"], "title": mem2["title"], "summary": mem2["summary"], "text": mem2["text"]},
                             source=chat_source, id=mem2["id"])
    r2 = write_memory(env2, propose=True, bypass_guardian=False)
    assert r2.validation_status != "committed" and r2.projection_pending is False and r2.projection_delta_id is None


# ── commit_seq posture + dirty-marker root ──────────────────────────────────


def test_rework_commit_seq_posture_is_explicit(isolated_memory, monkeypatch):  # noqa: F811
    from memory.projection_delta import ProjectionDelta, append_delta_for_canonical
    from memory.promotion import commit_promotion, create_staging
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    committed, _r, _i = commit_promotion(create_staging(_env_for(mem)), "test:seq")
    assert committed
    path = _committed_path(mem["id"])
    d = _latest_delta_for(memory_root_for_canonical_path(path), mem["id"])
    # a direct write outside the serialized commit region is NOT sequenced
    assert d.sequenced is False
    assert "sequenced" in d.to_dict()
    # inside the serialized commit region the same emission is sequenced and equals the epoch stamp
    from core.cognition_lock import commit_lock
    from core.timekeeper import last_commit_seq
    with commit_lock():
        d2 = append_delta_for_canonical(json.loads(Path(path).read_text(encoding="utf-8")), path)
    assert d2 is not None and d2.sequenced is True and d2.commit_seq == last_commit_seq()
    # sequenced participates in identity
    assert ProjectionDelta.build(operation="upsert", memory_id="m", canonical_path="p", canonical_hash="h", coord=(2, 0, 1),
                                 commit_seq=1, projection_class="semantic", sequenced=True).delta_id != \
        ProjectionDelta.build(operation="upsert", memory_id="m", canonical_path="p", canonical_hash="h", coord=(2, 0, 1),
                              commit_seq=1, projection_class="semantic", sequenced=False).delta_id


def test_rework_dirty_marker_lands_in_the_canonical_records_profile(tmp_path, monkeypatch):
    """5.2: delta-build failure with root=None must mark dirty in the profile
    that owns the canonical path — never the process-default tree."""
    from memory.projection_delta import append_delta_for_canonical, projection_dirty_state

    profile = tmp_path / "profileX"; (profile / "memory" / "200_knowledge").mkdir(parents=True)
    canonical = profile / "memory" / "200_knowledge" / "2.00.ghost.json"  # does NOT exist → read fails
    default_root = tmp_path / "default"; (default_root / "memory").mkdir(parents=True)
    import core.paths as core_paths
    monkeypatch.setattr(core_paths, "MEMORY_ROOT", default_root / "memory")
    out = append_delta_for_canonical({"id": "2.00.ghost", "coord": [2, 0, 0], "type": "concept"}, str(canonical))
    assert out is None
    assert projection_dirty_state(root=profile)["dirty"] is True
    assert projection_dirty_state(root=default_root) == {"dirty": False}


# ═════════════════════════════════════════════════════════════════════════════
# Rework 2 (CUT4_REVIEW_REWORK_2.md): WHERE THE DURABLE TRANSACTION ENDS
#   durable WAL open / prior snapshot → canonical write + encoding →
#   canonical_finalized → canonical-mutating hooks → final ProjectionDelta(s)
#   OR durable rebuild-required marker → ONLY NOW close the WAL.
# ═════════════════════════════════════════════════════════════════════════════


class _Crash(BaseException):
    """process-death stand-in: not an Exception, so no handler swallows it"""


def _obligations():
    return [e for e in _unclosed() if e.get("kind") == "projection_obligation"]


def _write_native(monkeypatch, env_dict, *, propose=False):
    from memory.api import CanonicalEnvelope, write_memory

    env = CanonicalEnvelope(cycle_id=env_dict["cycle_id"], coord=env_dict["coord"], type=env_dict["type"],
                            payload=env_dict["payload"], source=env_dict["source"], id=env_dict["id"], confidence=0.6)
    return write_memory(env, propose=propose, bypass_guardian=not propose)


def test_rw2_crash_after_supersedes_hook_before_final_publish_is_recoverable(isolated_rollback, monkeypatch):  # noqa: F811
    """§2 / test 1: the supersedes hook rewrites the new AND the prior record,
    then the process dies before publish_final_projection_state. The stale
    commit-time delta must NOT be the last word: the owner's projection
    obligation is still open (durable WAL) and boot marks projection dirty."""
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    ids = ["1.00.rw2food1", "1.00.rw2food2"]
    r1 = _write_native(monkeypatch, _persona_env("Bob's favorite food is pizza", ids[0], title="Bob favorite food"))
    assert r1.validation_status == "committed"
    assert _obligations() == []  # normal completion settles the obligation
    with pytest.MonkeyPatch.context() as crash:
        crash.setattr(pd, "publish_final_projection_state", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            _write_native(monkeypatch, _persona_env("Bob's favorite food is sushi", ids[1], title="Bob favorite food"))
    paths = {m: _committed_path(m) for m in ids}
    root = memory_root_for_canonical_path(paths[ids[1]])
    new_file = json.loads(Path(paths[ids[1]]).read_text(encoding="utf-8"))
    assert new_file.get("supersedes") == ids[0], "fixture must have run the supersedes hook before the crash"
    # the reviewer's observation: latest deltas are stale for BOTH records ...
    assert _latest_delta_for(root, ids[1]).canonical_hash != _final_hash(paths[ids[1]])
    assert _latest_delta_for(root, ids[0]).canonical_hash != _final_hash(paths[ids[0]])
    # ... but this time durable evidence exists: the owner's obligation is open
    obl = _obligations()
    assert len(obl) == 1 and obl[0]["memory_id"] == ids[1]
    assert pd.projection_dirty_state(root=root)["dirty"] is False  # not yet — WAL is the evidence
    results = _recover()
    assert results and results[0].get("obligation") is True and results[0].get("projection_dirty") is True
    assert pd.projection_dirty_state(root=root)["dirty"] is True
    assert Path(paths[ids[0]]).exists() and Path(paths[ids[1]]).exists()  # canonical never touched
    assert _unclosed() == []


def test_rw2_crash_after_entity_link_peer_mutation_before_publish_no_silent_drift(isolated_rollback, monkeypatch):  # noqa: F811
    """§2 / test 2: the entity-link hook mutates a peer canonical file, then
    the owner dies before publication. No delta represents the peer; the
    open obligation must force rebuild-required at boot."""
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    peer = _write_native(monkeypatch, _persona_env("Carol works at the observatory", "1.00.rw2peer", title="Carol"))
    assert peer.validation_status == "committed"
    peer_path = Path(_committed_path("1.00.rw2peer"))
    peer_hash_before = _final_hash(peer_path)

    def _linker_mutates_peer(*_a, **_k):
        rec = json.loads(peer_path.read_text(encoding="utf-8")); rec["links"] = ["1.00.rw2new"]
        peer_path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return 1

    monkeypatch.setattr("memory.enrichment.entity_linker.link_personal_memories", _linker_mutates_peer)
    with pytest.MonkeyPatch.context() as crash:
        crash.setattr(pd, "publish_final_projection_state", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            _write_native(monkeypatch, _persona_env("Alice met Carol at the observatory", "1.00.rw2new", title="Alice"))
    root = memory_root_for_canonical_path(peer_path)
    assert _final_hash(peer_path) != peer_hash_before  # peer really mutated
    assert _latest_delta_for(root, "1.00.rw2peer").canonical_hash == peer_hash_before  # stale, no peer delta
    assert len(_obligations()) == 1
    results = _recover()
    assert results and results[0].get("projection_dirty") is True
    assert pd.projection_dirty_state(root=root)["dirty"] is True
    assert _unclosed() == []


def test_rw2_commit_flagged_overwrite_pre_final_crash_restores_prior(isolated_rollback, monkeypatch):  # noqa: F811
    """§3 / test 3: commit_flagged takes + journals the prior snapshot BEFORE
    the overwrite; a crash during encoding restores prior bytes AND index."""
    from memory.promotion import commit_flagged, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    committed, _r, _i = commit_flagged(create_staging(_env_for(mem)), "test:flag1", flag_reason="ambiguous", response_mode="acknowledge_flagged")
    assert committed
    path = Path(_committed_path(mem["id"]))
    prior_bytes = path.read_bytes()
    prior_summary = json.loads(prior_bytes)["summary"]
    mem2 = dict(mem); mem2["summary"] = "flag rewrite"; mem2["text"] = json.dumps({"episode_id": "ep-flag-rw", "intent": "LEARN"})
    ref = create_staging(_env_for(mem2))
    with pytest.MonkeyPatch.context() as crash:
        import memory.encoding as enc
        crash.setattr(enc, "encode_committed_memory", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            commit_flagged(ref, "test:flag2", flag_reason="ambiguous", response_mode="acknowledge_flagged")
    assert path.read_bytes() != prior_bytes  # overwritten in place before the crash
    entry = _unclosed()
    from memory import encoding_journal

    snapshot_path = encoding_journal._resolve_snapshot_path(entry[0]["prior_snapshot"])
    assert len(entry) == 1 and entry[0]["prior_snapshot"] and snapshot_path.exists()
    results = _recover()
    assert results and results[0].get("restored") is True
    assert path.exists() and json.loads(path.read_text(encoding="utf-8"))["summary"] == prior_summary
    assert _committed_path(mem["id"]) is not None  # index entry present
    assert _unclosed() == []


def test_rw2_journal_finalize_failure_is_not_advisory(isolated_rollback, monkeypatch):  # noqa: F811
    """§4 / test 4: finalize marker not durable → the write does NOT return
    committed; canonical is rolled back synchronously (new) / restored (overwrite);
    and if the process dies during that cleanup, boot completes the same
    rollback/restore from the still-pre-final entry."""
    import memory.encoding_journal as ej
    import memory.promotion as prom
    from memory.promotion import commit_flagged, commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    from memory.engine.validate_write import final_path_for_meta

    # (a) new record, synchronous: not committed, no file, no index entry, journal aborted
    mem = _lobe9_memory_dict()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ej, "journal_finalize", lambda *_a, **_k: False)
        committed, ref, info = commit_promotion(create_staging(_env_for(mem)), "test:fin-a")
    assert committed is False and ref is None and info is None
    assert not final_path_for_meta(mem).exists() and _committed_path(mem["id"]) is None
    assert _unclosed() == []
    # (b) overwrite + crash during the cleanup: boot restores prior
    memb = _lobe9_memory_dict()
    ok, _r, _i = commit_promotion(create_staging(_env_for(memb)), "test:fin-b0")
    assert ok
    pathb = Path(_committed_path(memb["id"])); prior = pathb.read_bytes()
    memb2 = dict(memb); memb2["summary"] = "fin rewrite"; memb2["text"] = json.dumps({"episode_id": "ep-fin", "intent": "LEARN"})
    refb = create_staging(_env_for(memb2))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ej, "journal_finalize", lambda *_a, **_k: False)
        import memory.rollback as rb
        mp.setattr(rb, "restore_canonical_write", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
        mp.setattr(prom, "restore_canonical_write", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()), raising=False)
        with pytest.raises(_Crash):
            commit_promotion(refb, "test:fin-b1")
    assert pathb.read_bytes() != prior
    assert len(_unclosed()) == 1 and not str(_unclosed()[0]["last_step_completed"] or "").startswith("canonical_finalized")
    results = _recover()
    assert results and results[0].get("restored") is True and pathb.read_bytes() == prior
    assert _unclosed() == []
    # (c) commit_flagged honours the same rule
    memc = _lobe9_memory_dict()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(ej, "journal_finalize", lambda *_a, **_k: False)
        committed, ref, info = commit_flagged(create_staging(_env_for(memc)), "test:fin-c", flag_reason="x", response_mode="acknowledge_flagged")
    assert committed is False and _committed_path(memc["id"]) is None and _unclosed() == []


def test_rw2_prior_snapshot_persistence_failure_prevents_overwrite(isolated_rollback, monkeypatch):  # noqa: F811
    """§5 / test 5: for an overwrite the old bytes are truth until the new
    write finalizes; if the recovery copy cannot be persisted the overwrite
    never begins (both promotion and flagged paths)."""
    import memory.encoding_journal as ej
    from memory.promotion import commit_flagged, commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    ok, _r, _i = commit_promotion(create_staging(_env_for(mem)), "test:snap0")
    assert ok
    path = Path(_committed_path(mem["id"])); prior = path.read_bytes()
    blocker = ej.JOURNAL_PATH.parent / "not_a_dir"; blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ej, "_prior_snapshot_dir", lambda: blocker / "encoding_journal_prior")
    for fn, kw in ((commit_promotion, {}), (commit_flagged, {"flag_reason": "x", "response_mode": "acknowledge_flagged"})):
        mem2 = dict(mem); mem2["summary"] = f"rewrite via {fn.__name__}"; mem2["text"] = json.dumps({"episode_id": f"ep-{fn.__name__}", "intent": "LEARN"})
        ref = create_staging(_env_for(mem2))
        committed, cref, info = fn(ref, "test:snap1", **kw)
        assert committed is False and cref is None
        assert path.read_bytes() == prior, "overwrite must never begin without a durable prior snapshot"
        assert _unclosed() == []
        from memory.promotion import _staging_path
        assert _staging_path(ref).exists()  # staging intact for retry


def test_rw2_journal_open_failure_prevents_write(isolated_rollback, monkeypatch):  # noqa: F811
    """§5 / test 6: WAL open entry not durable → the crash-vulnerable
    atomic-write/encoding window is never entered (new record, both paths)."""
    import memory.encoding_journal as ej
    from memory.engine.validate_write import final_path_for_meta
    from memory.promotion import commit_flagged, commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    written = []
    import memory.promotion as prom
    real_atomic = prom.atomic_write
    monkeypatch.setattr(prom, "atomic_write", lambda *a, **k: (written.append(1), real_atomic(*a, **k))[1])
    monkeypatch.setattr(ej, "_append", lambda *_a, **_k: False)
    for fn, kw in ((commit_promotion, {}), (commit_flagged, {"flag_reason": "x", "response_mode": "acknowledge_flagged"})):
        mem = _lobe9_memory_dict()
        committed, cref, info = fn(create_staging(_env_for(mem)), "test:open", **kw)
        assert committed is False and cref is None and info is None
        assert not final_path_for_meta(mem).exists() and _committed_path(mem["id"]) is None
    assert written == [], "atomic_write must not run when the WAL could not open"


def test_rw2_outbox_and_dirty_marker_double_failure_keeps_finalized_entry_open(isolated_rollback, monkeypatch):  # noqa: F811
    """§6 / test 7: delta append fails AND the dirty marker fails → the write is
    still committed (canonical is truth) but the finalized WAL entry stays
    OPEN as the durable evidence; the next boot (marker writable again)
    marks dirty and closes it."""
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    mem = _lobe9_memory_dict()
    with pytest.MonkeyPatch.context() as broken:
        broken.setattr(pd, "_append_line_fsync", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk full")))
        real_mark = pd.mark_projection_dirty
        broken.setattr(pd, "mark_projection_dirty", lambda **_k: False)
        committed, _r, info = commit_promotion(create_staging(_env_for(mem)), "test:double")
    assert committed and info["projection_dirty"] is True and info["projection_evidence_durable"] is False
    assert info["journal_state"] == "open_unreconciled"
    path = _committed_path(mem["id"]); root = memory_root_for_canonical_path(path)
    assert Path(path).exists()
    assert _latest_delta_for(root, mem["id"]) is None
    assert pd.projection_dirty_state(root=root)["dirty"] is False  # nothing durable in the profile...
    entries = _unclosed()
    assert len(entries) == 1 and str(entries[0]["last_step_completed"]).startswith("canonical_finalized")  # ...but the WAL is
    results = _recover()  # next boot: marker writable
    assert results and results[0].get("preserved") is True and results[0].get("projection_dirty") is True
    assert pd.projection_dirty_state(root=root)["dirty"] is True and Path(path).exists()
    assert _unclosed() == []


def test_rw2_recovery_that_cannot_write_dirty_marker_leaves_entry_open_for_next_boot(isolated_rollback, monkeypatch):  # noqa: F811
    """§6 / test 8: recovery itself fails to persist the dirty marker →
    finalized entry AND obligation entry both remain open; a later boot with a
    working marker finishes the job."""
    import memory.encoding_journal as ej
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    # finalized commit entry left open with NO matching delta (crash before delta append)
    mem = _lobe9_memory_dict()
    ref = create_staging(_env_for(mem))
    with pytest.MonkeyPatch.context() as crash:
        crash.setattr(pd, "emit_delta_for_canonical", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            commit_promotion(ref, "test:rec8")
    path = _committed_path(mem["id"]); root = memory_root_for_canonical_path(path)
    # plus an owner obligation left open
    obl = ej.journal_open_obligation(mem["id"], path, "test:owner")
    assert obl and len(_unclosed()) == 2
    with pytest.MonkeyPatch.context() as broken:
        broken.setattr(pd, "mark_projection_dirty", lambda **_k: False)
        results = _recover()
    assert all(r.get("retry") is True for r in results) and len(_unclosed()) == 2
    assert Path(path).exists() and pd.projection_dirty_state(root=root)["dirty"] is False
    results = _recover()  # next boot
    assert all(r.get("projection_dirty") is True for r in results)
    assert pd.projection_dirty_state(root=root)["dirty"] is True and _unclosed() == []


def test_rw2_rejected_claim_overwrite_restores_prior_and_discards_snapshot_sidecar(isolated_rollback, monkeypatch):  # noqa: F811
    """A claim overwrite is rejected atomically; its rollback snapshot is closed."""
    import memory.encoding_journal as ej
    from memory.promotion import commit_promotion, create_staging

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    # Trait claim content now has the same immutable proof as other records.
    # Revisions require a new identity; exercise the real rejected overwrite.
    mem = _lobe9_memory_dict(); mem["type"] = "trait_observation"
    ok, _r, _i = commit_promotion(create_staging(_env_for(mem)), "test:sc0")
    assert ok
    prior_bytes = Path(_committed_path(mem["id"])).read_bytes()
    mem2 = dict(mem); mem2["summary"] = "sidecar rewrite"; mem2["text"] = json.dumps({"episode_id": "ep-sc", "intent": "LEARN"})
    opened = {}
    real_open = ej.journal_open

    def _spy_open(*a, **k):
        opened["prior_snapshot"] = k.get("prior_snapshot")
        jid = real_open(*a, **k)
        opened["sidecar"] = ej._prior_snapshot_dir() / f"{jid}.bin"
        opened["sidecar_existed"] = opened["sidecar"].exists()
        return jid

    monkeypatch.setattr(ej, "journal_open", _spy_open)
    ok2, _r2, info2 = commit_promotion(create_staging(_env_for(mem2)), "test:sc1")
    assert not ok2 and info2 is None
    assert Path(_committed_path(mem["id"])).read_bytes() == prior_bytes
    assert opened["prior_snapshot"] is not None and opened["sidecar_existed"] is True  # a real overwrite with a snapshot
    assert not opened["sidecar"].exists()  # ...discarded on close
    d = ej._prior_snapshot_dir()
    assert not d.exists() or list(d.iterdir()) == []
    assert _unclosed() == []


def _real_guardian_evaluation(kind: str):
    """Only Guardian's classifier is stubbed; decide_request's REAL dispatch
    runs commit_promotion (ALLOW) / commit_flagged (FLAG)."""
    def _evaluate(staging_record, *, evidence_refs=None):
        from guardian.decision import RESPONSE_MODE_ACCEPT_NORMAL
        return {"kind": kind, "subtype": "rw2", "escalation": False, "reason": f"rw2 {kind}",
                "response_mode": RESPONSE_MODE_ACCEPT_NORMAL if kind == "ALLOW" else "acknowledge_flagged",
                "persistence_policy": "canonical_commit" if kind == "ALLOW" else "flagged_memory",
                "verification": {"verification_id": f"vid-{kind}", "checks": []}}
    return _evaluate


@pytest.mark.parametrize("kind", ["ALLOW", "FLAG"])
def test_rw2_real_guardian_allow_and_flag_receipts_report_final_projection_posture(isolated_rollback, monkeypatch, kind):  # noqa: F811
    """§7 / test 10: REAL decide_request dispatch (only _evaluate stubbed) for
    ALLOW and for FLAG: committed receipt, projection_pending, delta id ==
    the latest outbox delta whose hash == the final canonical bytes; the
    flagged record really is flagged; obligation settled."""
    from memory.api import _USER_CHAT_SOURCE_PREFIXES
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    monkeypatch.setattr("guardian.decision._evaluate", _real_guardian_evaluation(kind))
    mem = _lobe9_memory_dict()
    src = sorted(_USER_CHAT_SOURCE_PREFIXES)[0].rstrip(":") + ":rw2"
    e = {"cycle_id": f"g-{kind}", "coord": mem["coord"], "type": mem["type"], "source": src, "id": mem["id"],
         "payload": {"id": mem["id"], "title": mem["title"], "summary": mem["summary"], "text": mem["text"]}}
    receipt = _write_native(monkeypatch, e, propose=True)
    assert receipt.validation_status == "committed" and receipt.message.endswith(kind)
    path = _committed_path(mem["id"]); root = memory_root_for_canonical_path(path)
    latest = _latest_delta_for(root, mem["id"])
    assert receipt.projection_pending is True
    assert receipt.projection_delta_id == latest.delta_id and latest.canonical_hash == _final_hash(path)
    rec = json.loads(Path(path).read_text(encoding="utf-8"))
    assert bool((rec.get("flags") or {}).get("flagged")) is (kind == "FLAG")
    assert _unclosed() == []


def test_rw2_gate_write_crash_before_publish_leaves_open_obligation(isolated_rollback, monkeypatch):  # noqa: F811
    """The gate is a write owner too: obligation journaled before atomic_write,
    settled after the final publication; a crash in between → boot marks
    projection dirty for that profile."""
    from tests.memory.test_gate_write_embed_backfill import _native_gate
    import memory.encoding_journal as ej
    import memory.projection_delta as pd

    gate = _native_gate(monkeypatch)
    monkeypatch.setattr(gate, "_get_strength_index", lambda: None)
    monkeypatch.setattr("memory.post_commit.run_post_commit_memory_hooks",
                        lambda memory_id, *, coord=None, source="": {"memory_id": memory_id, "ran": True, "supersedes": None, "entity_links": 0, "errors": []})
    ids: list = []
    # normal write: obligation opened and settled
    path = gate._write("gate write settles its obligation", [2, 5, 0], None, None, id_out=ids)
    assert path and _unclosed() == []
    # crash between hooks and publication
    ids2: list = []
    with pytest.MonkeyPatch.context() as crash:
        crash.setattr(pd, "publish_final_projection_state", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
        with pytest.raises(_Crash):
            gate._write("gate write that dies before publish", [2, 5, 0], None, None, id_out=ids2)
    obl = _obligations()
    assert len(obl) == 1 and obl[0]["owner"].startswith("gate:")
    results = _recover()
    assert results and results[0].get("projection_dirty") is True and _unclosed() == []
    from memory.projection_receipt import memory_root_for_canonical_path
    # The journal stores the path relative to its profile; resolve it the way recovery does.
    # Raw, it resolved against the working directory, i.e. the live memory tree.
    root = memory_root_for_canonical_path(ej._resolve_canonical_path(obl[0]["final_path"]))
    assert pd.projection_dirty_state(root=root)["dirty"] is True


# ═════════════════════════════════════════════════════════════════════════════
# Rework 3 (CUT4_REVIEW_REWORK_3.md): the recovery channel itself may fail;
# unknown mutation set != zero mutation set.
# ═════════════════════════════════════════════════════════════════════════════


def _break_recovery_channels(mp):
    """journal obligation append fails AND the dirty marker cannot be written."""
    import memory.encoding_journal as ej
    import memory.projection_delta as pd

    mp.setattr(ej, "journal_open_obligation", lambda *_a, **_k: None)
    mp.setattr(pd, "mark_projection_dirty", lambda **_k: False)


def test_rw3_api_owner_with_no_durable_recovery_channel_does_not_enter_hook_window(isolated_rollback, monkeypatch):  # noqa: F811
    """§3 / test 1: obligation journal fails + fallback dirty marker fails. The
    canonical write is already valid and is preserved; the canonical-mutating
    hooks are NOT run (the commit-time delta stays truthful); a crash where
    the final publish would be leaves NOTHING stale and NOTHING silent."""
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    ids = ["1.00.rw3food1", "1.00.rw3food2"]
    r1 = _write_native(monkeypatch, _persona_env("Bob's favorite food is pizza", ids[0], title="Bob favorite food"))
    assert r1.validation_status == "committed"
    p0 = Path(_committed_path(ids[0])); h0 = _final_hash(p0)
    hooks_ran = []
    import memory.post_commit as pc
    real_hooks = pc.run_post_commit_memory_hooks
    monkeypatch.setattr(pc, "run_post_commit_memory_hooks", lambda *a, **k: (hooks_ran.append(1), real_hooks(*a, **k))[1])
    with pytest.MonkeyPatch.context() as broken:
        _break_recovery_channels(broken)
        with pytest.MonkeyPatch.context() as crash:  # and the process dies where the final publish would run
            crash.setattr(pd, "publish_final_projection_state", lambda *_a, **_k: (_ for _ in ()).throw(_Crash()))
            with pytest.raises(_Crash):
                _write_native(monkeypatch, _persona_env("Bob's favorite food is sushi", ids[1], title="Bob favorite food"))
    p1 = Path(_committed_path(ids[1]))
    root = memory_root_for_canonical_path(p1)
    assert hooks_ran == [], "mutating hooks must not run inside an unprotected window"
    assert json.loads(p1.read_text(encoding="utf-8")).get("supersedes") is None  # nothing was linked
    assert _final_hash(p0) == h0  # prior untouched
    # NOTHING stale: latest deltas hash the current bytes of both records
    assert _latest_delta_for(root, ids[1]).canonical_hash == _final_hash(p1)
    assert _latest_delta_for(root, ids[0]).canonical_hash == h0
    assert p0.exists() and p1.exists()
    assert _unclosed() == []
    # once the channels are back, a normal write links + publishes again
    r3 = _write_native(monkeypatch, _persona_env("Bob's favorite food is ramen", "1.00.rw3food3", title="Bob favorite food"))
    assert r3.validation_status == "committed" and hooks_ran == [1]


def test_rw3_api_owner_with_dirty_fallback_channel_runs_hooks_under_durable_evidence(isolated_rollback, monkeypatch):  # noqa: F811
    """§3: journal fails but the dirty marker is durable → hooks may run (the
    profile is already rebuild-required); posture is 'dirty'."""
    import memory.encoding_journal as ej
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    ids = ["1.00.rw3d1", "1.00.rw3d2"]
    _write_native(monkeypatch, _persona_env("Bob's favorite food is pizza", ids[0], title="Bob favorite food"))
    monkeypatch.setattr(ej, "journal_open_obligation", lambda *_a, **_k: None)
    r = _write_native(monkeypatch, _persona_env("Bob's favorite food is sushi", ids[1], title="Bob favorite food"))
    assert r.validation_status == "committed"
    p1 = Path(_committed_path(ids[1])); root = memory_root_for_canonical_path(p1)
    assert json.loads(p1.read_text(encoding="utf-8")).get("supersedes") == ids[0]  # hooks ran
    assert pd.projection_dirty_state(root=root)["dirty"] is True  # under durable evidence
    assert _unclosed() == []


def test_rw3_gate_owner_with_no_durable_recovery_channel_refuses_before_write(isolated_rollback, monkeypatch):  # noqa: F811
    """§3 / test 2: gate obligation journal fails + dirty marker fails → the
    native semantic gate write is refused BEFORE atomic_write (no canonical
    record, no hooks, no false rebuild requirement); a filesystem-only
    (non-semantic) gate write is unaffected."""
    from tests.memory.test_gate_write_embed_backfill import _native_gate
    import memory.gates.base as gb

    gate = _native_gate(monkeypatch)
    monkeypatch.setattr(gate, "_get_strength_index", lambda: None)
    hooks = []
    monkeypatch.setattr("memory.post_commit.run_post_commit_memory_hooks",
                        lambda memory_id, *, coord=None, source="": (hooks.append(memory_id), {"memory_id": memory_id, "ran": True, "supersedes": None, "entity_links": 0, "errors": []})[1])
    writes = []
    real_atomic = gb.atomic_write
    monkeypatch.setattr(gb, "atomic_write", lambda *a, **k: (writes.append(1), real_atomic(*a, **k))[1])
    ids: list = []
    with pytest.MonkeyPatch.context() as broken:
        _break_recovery_channels(broken)
        path = gate._write("gate write with no recovery channel", [2, 5, 0], None, None, id_out=ids)
    assert path is None and writes == [] and hooks == [] and ids == []
    assert _unclosed() == []
    # channels back: the same write lands normally
    path2 = gate._write("gate write with recovery channel", [2, 5, 0], None, None, id_out=ids)
    assert path2 and writes == [1] and _unclosed() == []


def test_rw3_entity_link_peer_mutation_then_exception_is_uncertain_not_zero(isolated_rollback, monkeypatch):  # noqa: F811
    """§4 / test 3: the linker durably mutates a peer, then raises. The hook
    report must say uncertain (not entity_links=0), and the write owner must
    hold a durable dirty marker before settling — the peer's stale delta is
    covered by rebuild-required evidence."""
    import memory.projection_delta as pd
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    peer = _write_native(monkeypatch, _persona_env("Carol works at the observatory", "1.00.rw3peer", title="Carol"))
    assert peer.validation_status == "committed"
    peer_path = Path(_committed_path("1.00.rw3peer")); peer_hash_before = _final_hash(peer_path)
    root = memory_root_for_canonical_path(peer_path)
    assert pd.projection_dirty_state(root=root)["dirty"] is False

    def _mutate_then_raise(*_a, **_k):
        rec = json.loads(peer_path.read_text(encoding="utf-8")); rec["links"] = ["1.00.rw3new"]
        peer_path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        raise RuntimeError("linker died after writing a peer")

    monkeypatch.setattr("memory.enrichment.entity_linker.link_personal_memories", _mutate_then_raise)
    reports = []
    import memory.post_commit as pc
    real_hooks = pc.run_post_commit_memory_hooks
    monkeypatch.setattr(pc, "run_post_commit_memory_hooks", lambda *a, **k: (reports.append(real_hooks(*a, **k)), reports[-1])[1])
    r = _write_native(monkeypatch, _persona_env("Alice met Carol at the observatory", "1.00.rw3new", title="Alice"))
    assert r.validation_status == "committed"
    rep = reports[-1]
    assert rep["entity_links"] == 0 and rep["entity_linker"]["mutation_uncertain"] is True and rep["mutation_uncertain"] is True
    assert any("entity_linker" in e for e in rep["errors"])
    assert _final_hash(peer_path) != peer_hash_before  # peer really changed
    assert _latest_delta_for(root, "1.00.rw3peer").canonical_hash == peer_hash_before  # stale, but ...
    dirty = pd.projection_dirty_state(root=root)
    assert dirty["dirty"] is True and "uncertain" in dirty["reason"]  # ... covered by durable rebuild-required evidence
    assert r.projection_dirty is True
    assert _unclosed() == []  # settled ONLY because the marker is durable
    # and if that marker could NOT be written, the obligation must stay open
    ids2 = "1.00.rw3new2"
    with pytest.MonkeyPatch.context() as broken:
        broken.setattr(pd, "mark_projection_dirty", lambda **_k: False)
        r2 = _write_native(monkeypatch, _persona_env("Dave met Carol at the observatory", ids2, title="Dave"))
    assert r2.validation_status == "committed"
    obl = _obligations()
    assert len(obl) == 1 and obl[0]["memory_id"] == ids2


def test_rw3_hook_error_with_zero_mutation_is_still_conservative(isolated_rollback, monkeypatch):  # noqa: F811
    """§4 / test 4: a linker that raises BEFORE touching anything is reported
    as uncertain too — the contract never claims a known clean set unless
    mechanically proven; a clean zero (no exception) stays clean."""
    import memory.projection_delta as pd
    from memory.post_commit import run_post_commit_memory_hooks
    from memory.projection_receipt import memory_root_for_canonical_path

    monkeypatch.setenv("OMEGA_MEMORY_NATIVE_CITIZENSHIP", "1")
    _forbid_embed(monkeypatch)
    # clean zero: real linker, no peers → not uncertain, no dirty
    r0 = _write_native(monkeypatch, _persona_env("Erin likes telescopes", "1.00.rw3z0", title="Erin"))
    p0 = _committed_path("1.00.rw3z0"); root = memory_root_for_canonical_path(p0)
    rep0 = run_post_commit_memory_hooks("1.00.rw3z0", coord=[1, 0, 0], source="lobe:persona")
    assert rep0["ran"] is True and rep0["mutation_uncertain"] is False and rep0["entity_linker"]["ok"] is True
    assert pd.projection_dirty_state(root=root)["dirty"] is False and r0.projection_dirty is False
    # error with zero mutation → uncertain → durable dirty before settling
    monkeypatch.setattr("memory.enrichment.entity_linker.link_personal_memories",
                        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("linker refused before any write")))
    r1 = _write_native(monkeypatch, _persona_env("Frank likes telescopes", "1.00.rw3z1", title="Frank"))
    assert r1.validation_status == "committed" and r1.projection_dirty is True
    assert pd.projection_dirty_state(root=root)["dirty"] is True and _unclosed() == []
    # supersedes uncertainty follows the same rule (a write phase failed)
    rep = {"ran": True, "supersedes": {"ok": False, "phase": "write_prior", "revert": "revert_failed", "mutation_uncertain": True},
           "entity_links": 0, "entity_linker": {"ok": True, "links_made": 0, "mutation_uncertain": False, "error": ""},
           "mutation_uncertain": True, "errors": ["supersedes: boom"]}
    assert pd._hook_uncertainty(rep)
    assert pd._hook_uncertainty({"ran": True, "supersedes": {"ok": False, "reason": "prior_path_missing", "mutation_uncertain": False},
                                 "entity_links": 0, "mutation_uncertain": False, "errors": []}) == []


def test_rw3_gate_atomic_write_exception_releases_obligation(isolated_rollback, monkeypatch):  # noqa: F811
    """§5 / test 5: obligation opened, atomic_write raises → the obligation is
    released; boot must not create a false rebuild requirement."""
    from tests.memory.test_gate_write_embed_backfill import _native_gate
    import memory.gates.base as gb
    import memory.projection_delta as pd

    gate = _native_gate(monkeypatch)
    monkeypatch.setattr(gate, "_get_strength_index", lambda: None)
    opened = []
    real_open = pd.open_projection_obligation
    monkeypatch.setattr(pd, "open_projection_obligation", lambda *a, **k: (opened.append(1), real_open(*a, **k))[1])
    monkeypatch.setattr(gb, "atomic_write", lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk gone")))
    ids: list = []
    path = gate._write("gate write whose atomic_write raises", [2, 5, 0], None, None, id_out=ids)
    assert path is None and opened == [1] and ids == []
    assert _obligations() == [] and _unclosed() == []
    results = _recover()
    assert results == []


# ═════════════════════════════════════════════════════════════════════════════
# Rework 4 (CUT4_REVIEW_REWORK_4.md): the canonical mutators themselves are
# crash-safe — failure before the atomic replace leaves prior bytes intact.
# ═════════════════════════════════════════════════════════════════════════════


def _persona_file(dir_: Path, mid: str, *, coord, domain="bob", extra=None) -> Path:
    rec = {"id": mid, "coord": list(coord), "type": "concept", "title": f"about {domain} {mid}",
           "summary": f"{domain} record {mid}", "text": f"{domain} record {mid}", "domain": domain,
           "links": [], "version": 1, "provenance": {"confidence": 0.6}}
    rec.update(extra or {})
    path = dir_ / f"{mid}.json"
    path.write_text(json.dumps(rec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")  # distinctive bytes
    return path


def _fail_replace_for(monkeypatch, target: Path, *, boundary: str):
    """Inject an I/O failure on the rewrite of ``target`` at the chosen boundary:
    'temp' (during the temp-file write/fsync, before replace) or 'replace'
    (at the atomic replace itself)."""
    import memory.atomic_replace as ar

    if boundary == "temp":
        real = ar._fsync_file

        def _fsync(fh):
            if Path(fh.name).parent == target.parent and Path(fh.name).name.startswith(f".{target.name}."):
                raise OSError(f"temp write failure injected for {target.name}")
            return real(fh)

        monkeypatch.setattr(ar, "_fsync_file", _fsync)
    else:
        real = ar._replace

        def _replace(tmp, path):
            if Path(path).resolve() == target.resolve():
                raise OSError(f"replace failure injected for {target.name}")
            return real(tmp, path)

        monkeypatch.setattr(ar, "_replace", _replace)


def _no_temp_left(dir_: Path):
    return [p.name for p in dir_.iterdir() if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("boundary", ["temp", "replace"])
def test_rw4_entity_link_back_link_failure_leaves_peer_bytes_intact(tmp_path, monkeypatch, boundary):
    """§6.1: real link_personal_memories; the peer back-link rewrite fails →
    peer canonical bytes byte-identical; no temp left; the linker reports the
    failure (report out-param), links_created == 0 (no forward edge either)."""
    from memory.enrichment.entity_linker import link_personal_memories
    import memory.engine.walker as walker

    monkeypatch.setattr(walker, "MEMORY_ROOT", tmp_path)
    peer = _persona_file(tmp_path, "1.00.bobpeer", coord=[1, 0, 1])
    new = _persona_file(tmp_path, "1.00.bobnew", coord=[1, 0, 2])
    peer_bytes, new_bytes = peer.read_bytes(), new.read_bytes()
    idx = {"1.00.bobpeer": {"id": "1.00.bobpeer", "coord": [1, 0, 1], "path": str(peer)},
           "1.00.bobnew": {"id": "1.00.bobnew", "coord": [1, 0, 2], "path": str(new)}}
    _fail_replace_for(monkeypatch, peer, boundary=boundary)
    rep: dict = {}
    made = link_personal_memories(str(new), idx, report=rep)
    assert made == 0
    assert peer.read_bytes() == peer_bytes and new.read_bytes() == new_bytes
    assert rep["back_link_failures"] == 1 and rep["errors"] and _no_temp_left(tmp_path)


@pytest.mark.parametrize("boundary", ["temp", "replace"])
def test_rw4_entity_link_forward_link_failure_leaves_new_bytes_intact_and_peers_covered(isolated_rollback, monkeypatch, boundary):  # noqa: F811
    """§6.2: the new-record forward-link rewrite fails after peers took their
    back-links → new-record bytes byte-identical; links_made counts the peers
    and the linker ENUMERATES each peer it rewrote → the write owner emits the
    peer's own final delta (peers covered as projection work, no rebuild)."""
    from memory.enrichment.entity_linker import link_personal_memories
    from memory.post_commit import _run_entity_linker
    import memory.engine.walker as walker

    monkeypatch.setattr(walker, "MEMORY_ROOT", isolated_rollback)
    d = isolated_rollback / "rw4fwd"; d.mkdir()
    peer = _persona_file(d, "1.00.fwdpeer", coord=[1, 0, 1])
    new = _persona_file(d, "1.00.fwdnew", coord=[1, 0, 2])
    new_bytes = new.read_bytes()
    idx = {"1.00.fwdpeer": {"id": "1.00.fwdpeer", "coord": [1, 0, 1], "path": str(peer)},
           "1.00.fwdnew": {"id": "1.00.fwdnew", "coord": [1, 0, 2], "path": str(new)}}
    _fail_replace_for(monkeypatch, new, boundary=boundary)
    rep: dict = {}
    made = link_personal_memories(str(new), idx, report=rep)
    assert made == 1 and rep["forward_link_failed"] is True
    assert new.read_bytes() == new_bytes  # intact (still valid JSON, no forward edge)
    assert json.loads(peer.read_text(encoding="utf-8"))["links"] == ["1.00.fwdnew"]  # peer mutated (atomically)
    assert _no_temp_left(d)
    assert len(rep["peer_rewrites"]) == 1
    rewrite = rep["peer_rewrites"][0]
    assert rewrite["memory_id"] == "1.00.fwdpeer"
    assert Path(rewrite["path"]) == peer
    # the wrapper: known outcome (not uncertain). Re-running the linker finds the
    # peer's back-link already on disk: the link still counts, but no peer is
    # rewritten this time, so the wrapper enumerates NO peer rewrites.
    res = _run_entity_linker("1.00.fwdnew", str(new), idx, 1)
    assert res["ok"] is False and res["mutation_uncertain"] is False and res["forward_link_failed"] is True
    assert res["links_made"] == 1 and res["peer_rewrites"] == []
    # publish the FIRST run's result: the rewritten peer is enumerated
    linker_result = {**res, "peer_rewrites": rep["peer_rewrites"]}
    from memory.projection_delta import _hook_uncertainty, publish_final_projection_state
    import memory.projection_delta as pd
    report = {"ran": True, "supersedes": None, "entity_links": linker_result["links_made"], "entity_linker": linker_result,
              "mutation_uncertain": False, "errors": [], "write_failures": [f"entity_linker: {res['error']}"]}
    assert _hook_uncertainty(report) == []
    root = isolated_rollback / "memory"; root.mkdir(exist_ok=True)
    final = publish_final_projection_state("1.00.fwdnew", new, hook_report=report, root=root)
    assert final["projection_dirty"] is False and final["obligation_satisfied"] is True
    assert final["delta_id"] and len(final["peer_delta_ids"]) == 1
    assert pd.projection_dirty_state(root=root)["dirty"] is False
    # the peer's final delta hashes the MUTATED peer file (back-link on disk)
    assert _latest_delta_for(root, "1.00.fwdpeer").canonical_hash == _final_hash(peer)
    assert _latest_delta_for(root, "1.00.fwdnew").canonical_hash == _final_hash(new)


def test_rw4_entity_link_peers_not_enumerated_still_force_rebuild(isolated_rollback, monkeypatch):  # noqa: F811
    """A linker result that counts links but does NOT enumerate the peers it
    rewrote (legacy / patched linker) is an un-enumerable mutation set: the
    write owner must still mark projection dirty (rebuild-required)."""
    import memory.projection_delta as pd
    from memory.projection_delta import publish_final_projection_state

    d = isolated_rollback / "rw4noenum"; d.mkdir()
    new = _persona_file(d, "1.00.noenum", coord=[1, 0, 2])
    res = {"ok": True, "links_made": 1, "mutation_uncertain": False, "error": ""}  # no peer_rewrites
    report = {"ran": True, "supersedes": None, "entity_links": 1, "entity_linker": res,
              "mutation_uncertain": False, "errors": [], "write_failures": []}
    root = isolated_rollback / "memory"; root.mkdir(exist_ok=True)
    final = publish_final_projection_state("1.00.noenum", new, hook_report=report, root=root)
    assert final["projection_dirty"] is True and final["projection_dirty_durable"] is True
    assert final["obligation_satisfied"] is True and final["peer_delta_ids"] == []
    assert pd.projection_dirty_state(root=root)["dirty"] is True


@pytest.mark.parametrize("boundary", ["temp", "replace"])
def test_rw4_supersedes_write_new_failure_leaves_new_bytes_intact(tmp_path, monkeypatch, boundary):
    """§6.3: real link_supersedes; the new-record forward-edge write fails →
    new canonical bytes byte-identical, prior untouched, result ok=False
    phase=write_new, in-memory dict restored, no temp left."""
    from memory.supersedes import link_supersedes

    prior = _persona_file(tmp_path, "1.00.supprior", coord=[1, 0, 1])
    new = _persona_file(tmp_path, "1.00.supnew", coord=[1, 0, 1])
    pb, nb = prior.read_bytes(), new.read_bytes()
    new_meta = json.loads(nb); prior_meta = json.loads(pb)
    _fail_replace_for(monkeypatch, new, boundary=boundary)
    res = link_supersedes(new_meta_path=str(new), new_meta=new_meta, prior_meta=prior_meta, prior_path=str(prior))
    assert res["ok"] is False and res["phase"] == "write_new"
    assert new.read_bytes() == nb and prior.read_bytes() == pb
    assert new_meta.get("supersedes") is None and new_meta.get("version") == 1
    assert _no_temp_left(tmp_path)


@pytest.mark.parametrize("boundary", ["temp", "replace"])
def test_rw4_supersedes_write_prior_failure_keeps_prior_intact_and_reverts_new_exactly(tmp_path, monkeypatch, boundary):
    """§6.4: the prior back-edge write fails after the new record took its
    forward edge → prior bytes byte-identical; the new-record revert is
    crash-safe and restores the EXACT pre-hook bytes; result phase=write_prior
    revert=reverted."""
    from memory.supersedes import link_supersedes

    prior = _persona_file(tmp_path, "1.00.wpprior", coord=[1, 0, 1])
    new = _persona_file(tmp_path, "1.00.wpnew", coord=[1, 0, 1])
    pb, nb = prior.read_bytes(), new.read_bytes()
    new_meta = json.loads(nb); prior_meta = json.loads(pb)
    _fail_replace_for(monkeypatch, prior, boundary=boundary)
    res = link_supersedes(new_meta_path=str(new), new_meta=new_meta, prior_meta=prior_meta, prior_path=str(prior))
    assert res["ok"] is False and res["phase"] == "write_prior" and res["revert"] == "reverted"
    assert prior.read_bytes() == pb
    assert new.read_bytes() == nb  # exact pre-hook bytes, not a re-serialisation
    assert prior_meta.get("superseded_by") is None
    assert _no_temp_left(tmp_path)


def test_rw4_supersedes_revert_failure_cannot_corrupt_new_record(tmp_path, monkeypatch):
    """§6.5: prior write fails AND the revert fails → the new canonical file is
    still valid JSON carrying the forward edge (never truncated); result
    revert=revert_failed; no temp left."""
    import memory.atomic_replace as ar
    from memory.supersedes import link_supersedes

    prior = _persona_file(tmp_path, "1.00.rvprior", coord=[1, 0, 1])
    new = _persona_file(tmp_path, "1.00.rvnew", coord=[1, 0, 1])
    pb, nb = prior.read_bytes(), new.read_bytes()
    new_meta = json.loads(nb); prior_meta = json.loads(pb)
    calls = {"new": 0}
    real = ar._replace

    def _replace(tmp, path):
        p = Path(path).resolve()
        if p == prior.resolve():
            raise OSError("prior write injected")
        if p == new.resolve():
            calls["new"] += 1
            if calls["new"] >= 2:  # 1st = forward edge (ok), 2nd = revert (fails)
                raise OSError("revert injected")
        return real(tmp, path)

    monkeypatch.setattr(ar, "_replace", _replace)
    res = link_supersedes(new_meta_path=str(new), new_meta=new_meta, prior_meta=prior_meta, prior_path=str(prior))
    assert res["ok"] is False and res["phase"] == "write_prior" and res["revert"] == "revert_failed"
    assert prior.read_bytes() == pb
    after = json.loads(new.read_text(encoding="utf-8"))  # valid JSON — never truncated
    assert after["supersedes"] == "1.00.rvprior" and after["version"] == 2
    assert _no_temp_left(tmp_path)


def test_rw4_successful_links_still_produce_bidirectional_structure(tmp_path, monkeypatch):
    """§6.6: with no faults, entity-link and supersedes writes land through the
    atomic path and produce the expected bidirectional structure."""
    from memory.enrichment.entity_linker import link_personal_memories
    from memory.supersedes import link_supersedes
    import memory.engine.walker as walker

    monkeypatch.setattr(walker, "MEMORY_ROOT", tmp_path)

    peer = _persona_file(tmp_path, "1.00.okpeer", coord=[1, 0, 1])
    new = _persona_file(tmp_path, "1.00.oknew", coord=[1, 0, 2])
    idx = {"1.00.okpeer": {"id": "1.00.okpeer", "coord": [1, 0, 1], "path": str(peer)},
           "1.00.oknew": {"id": "1.00.oknew", "coord": [1, 0, 2], "path": str(new)}}
    rep: dict = {}
    assert link_personal_memories(str(new), idx, report=rep) == 1 and rep["errors"] == []
    assert json.loads(new.read_text(encoding="utf-8"))["links"] == ["1.00.okpeer"]
    assert json.loads(peer.read_text(encoding="utf-8"))["links"] == ["1.00.oknew"]
    prior = _persona_file(tmp_path, "1.00.okprior", coord=[1, 0, 3])
    new2 = _persona_file(tmp_path, "1.00.oknew2", coord=[1, 0, 3])
    nm = json.loads(new2.read_text(encoding="utf-8")); pm = json.loads(prior.read_text(encoding="utf-8"))
    res = link_supersedes(new_meta_path=str(new2), new_meta=nm, prior_meta=pm, prior_path=str(prior))
    assert res["ok"] is True
    assert json.loads(new2.read_text(encoding="utf-8"))["supersedes"] == "1.00.okprior"
    assert json.loads(prior.read_text(encoding="utf-8"))["superseded_by"] == "1.00.oknew2"
    assert _no_temp_left(tmp_path)


def test_rw4_atomic_replace_helper_contract(tmp_path, monkeypatch):
    """The helper itself: same-directory temp, fsync, atomic replace, temp
    cleanup on failure at either boundary; unserialisable doc touches nothing."""
    import memory.atomic_replace as ar

    target = tmp_path / "rec.json"; target.write_bytes(b'{"a": 1}\n'); before = target.read_bytes()
    ar.atomic_replace_json(target, {"a": 2})
    assert json.loads(target.read_text()) == {"a": 2} and _no_temp_left(tmp_path)
    mid = target.read_bytes()
    with pytest.raises(TypeError):
        ar.atomic_replace_json(target, {"a": object()})
    assert target.read_bytes() == mid and _no_temp_left(tmp_path)
    monkeypatch.setattr(ar, "_fsync_file", lambda fh: (_ for _ in ()).throw(OSError("fsync")))
    with pytest.raises(OSError):
        ar.atomic_replace_bytes(target, b"x")
    assert target.read_bytes() == mid and _no_temp_left(tmp_path)
    monkeypatch.undo()
    monkeypatch.setattr(ar, "_replace", lambda t, p: (_ for _ in ()).throw(OSError("replace")))
    with pytest.raises(OSError):
        ar.atomic_replace_bytes(target, b"x")
    assert target.read_bytes() == mid and _no_temp_left(tmp_path)
    assert before != mid

"""Trait revisions cannot be acknowledged with an incomplete canonical chain."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from deepsleep_loop import identity_store
from deepsleep_loop._deepsleep_state import Trait
from memory import post_commit
from memory.engine import walker
from tests.memory.projection._fixtures import redirect_timekeeper


@pytest.fixture(autouse=True)
def _clock(tmp_path):
    from memory import episode_sram

    redirect_timekeeper(tmp_path)
    episode_sram.reset()
    yield
    episode_sram.reset()


def _seed_traits():
    from core.paths import MEMORY_ROOT

    records = []
    for cycle, strength, count in (("first", .2, 2), ("second", .3, 3)):
        envelope = identity_store._trait_envelope(Trait("synthetic_trait", strength, count), cycle_id=cycle)
        record = {**envelope.payload, "id": envelope.id, "coord": list(envelope.coord), "type": envelope.type}
        path = MEMORY_ROOT / "000_identity" / "traits" / f"{envelope.id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        records.append((record, path))
    walker.INDEX_PATH.write_text(json.dumps({
        record["id"]: {"coord": record["coord"], "path": str(path)}
        for record, path in records
    }), encoding="utf-8")
    walker.load_coord_index()
    return records


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_competing_trait_heads_are_explicitly_unavailable(tmp_path):
    _seed_traits()
    with pytest.raises(ValueError, match="competing.*trait"):
        identity_store.load_identity(path=tmp_path / "absent.json")


def test_required_trait_link_failure_propagates_and_retry_publishes_both_records(monkeypatch, tmp_path):
    from memory import atomic_replace, projection_delta

    (old, old_path), (new, new_path) = _seed_traits()
    replace_json = atomic_replace.atomic_replace_json
    failure = OSError("synthetic prior-link failure")

    def fail_old(path, payload, *args, **kwargs):
        if Path(path).resolve() == old_path.resolve():
            raise failure
        return replace_json(path, payload, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(atomic_replace, "atomic_replace_json", fail_old)
        with pytest.raises(RuntimeError, match="synthetic prior-link failure"):
            post_commit.ensure_trait_revision(new["id"])
    assert not _read(new_path).get("supersedes")
    assert not _read(old_path).get("superseded_by")

    report = post_commit.ensure_trait_revision(new["id"])
    assert report["ok"] is True
    assert _read(new_path)["supersedes"] == old["id"]
    assert _read(old_path)["superseded_by"] == new["id"]
    assert identity_store.load_identity(path=tmp_path / "absent.json")["synthetic_trait"].strength == .3
    deltas = [json.loads(line) for line in projection_delta.outbox_path().read_text(encoding="utf-8").splitlines()]
    assert {old["id"], new["id"]} <= {row["memory_id"] for row in deltas}


@pytest.mark.parametrize("missing_edge", ["forward", "backward"])
def test_interrupted_trait_link_repairs_the_missing_edge(missing_edge):
    (old, old_path), (new, new_path) = _seed_traits()
    if missing_edge == "backward":
        new.update(supersedes=old["id"], version=2)
        new_path.write_text(json.dumps(new), encoding="utf-8")
    else:
        old["superseded_by"] = new["id"]
        old_path.write_text(json.dumps(old), encoding="utf-8")

    assert post_commit.ensure_trait_revision(new["id"])["ok"] is True
    assert _read(new_path)["supersedes"] == old["id"]
    assert _read(old_path)["superseded_by"] == new["id"]


def test_retrying_historical_trait_does_not_create_a_cycle():
    (old, old_path), (new, new_path) = _seed_traits()
    post_commit.ensure_trait_revision(new["id"])
    before = (old_path.read_bytes(), new_path.read_bytes())
    post_commit.ensure_trait_revision(old["id"])
    post_commit.ensure_trait_revision(new["id"])
    assert (old_path.read_bytes(), new_path.read_bytes()) == before


def test_trait_link_is_not_mutated_without_durable_projection_obligation(monkeypatch):
    (old, old_path), (new, new_path) = _seed_traits()
    before = (old_path.read_bytes(), new_path.read_bytes())
    monkeypatch.setattr("memory.projection_delta.open_projection_obligation", lambda *args, **kwargs: {
        "journal_id": None, "channel": "none", "durable": False,
    })
    with pytest.raises(RuntimeError, match="durable.*projection"):
        post_commit.ensure_trait_revision(new["id"])
    assert (old_path.read_bytes(), new_path.read_bytes()) == before


def test_trait_retry_republishes_after_final_projection_failure(monkeypatch):
    from memory.encoding_journal import journal_status

    (old, old_path), (new, new_path) = _seed_traits()
    with monkeypatch.context() as fault:
        fault.setattr("memory.projection_delta.publish_final_projection_state", lambda *args, **kwargs: {
            "obligation_satisfied": False, "reason": "synthetic outbox unavailable",
        })
        with pytest.raises(RuntimeError, match="final projection is not durable"):
            post_commit.ensure_trait_revision(new["id"])
    linked_bytes = (old_path.read_bytes(), new_path.read_bytes())
    assert _read(old_path)["superseded_by"] == new["id"]
    assert _read(new_path)["supersedes"] == old["id"]
    assert post_commit.ensure_trait_revision(new["id"])["ok"] is True
    assert (old_path.read_bytes(), new_path.read_bytes()) == linked_bytes
    assert not [row for row in journal_status()["unclosed"]
                if row.get("owner") == "memory.trait_revision"]


def test_trait_retry_settles_only_its_exact_owner_and_canonical_file():
    from memory.encoding_journal import journal_status
    from memory.projection_delta import open_projection_obligation

    (_old, old_path), (new, new_path) = _seed_traits()
    other_owner = open_projection_obligation(new["id"], new_path, "another.owner")
    other_path = open_projection_obligation(new["id"], old_path, "memory.trait_revision")
    prior_attempt = open_projection_obligation(new["id"], new_path, "memory.trait_revision")
    post_commit.ensure_trait_revision(new["id"])
    unclosed = {row["journal_id"] for row in journal_status()["unclosed"]}
    assert {other_owner["journal_id"], other_path["journal_id"]} <= unclosed
    assert prior_attempt["journal_id"] not in unclosed


def test_trait_retry_cannot_hide_unreadable_obligation_journal(monkeypatch):
    (_old, _old_path), (new, _new_path) = _seed_traits()
    monkeypatch.setattr("memory.encoding_journal.journal_status", lambda: {
        "unclosed": [], "error": "synthetic WAL read failure",
    })
    with pytest.raises(post_commit.TraitRevisionError, match="synthetic WAL read failure"):
        post_commit.ensure_trait_revision(new["id"])


def test_trait_link_reports_an_unrelated_active_head():
    from core.paths import MEMORY_ROOT

    (old, old_path), (new, _new_path) = _seed_traits()
    rogue = {**old, "id": "trait.synthetic_trait.rogue"}
    rogue_path = MEMORY_ROOT / "000_identity" / "traits" / "rogue.json"
    rogue_path.write_text(json.dumps(rogue), encoding="utf-8")
    index = json.loads(walker.INDEX_PATH.read_text(encoding="utf-8"))
    index[rogue["id"]] = {"coord": rogue["coord"], "path": str(rogue_path)}
    walker.INDEX_PATH.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(RuntimeError, match="competing revision heads"):
        post_commit.ensure_trait_revision(new["id"])
    assert not _read(old_path).get("superseded_by")


def test_intake_does_not_ack_failed_trait_link_and_retry_finishes_without_rewrite(monkeypatch, tmp_path):
    from memory import api, atomic_replace, episode_sram, intake_journal

    episode_sram.open_bucket("trait-first", pipeline_id=0)
    identity_store.upsert_trait(Trait("synthetic_trait", .2, 2),
                                path=tmp_path / "absent.json", cycle_id="trait-first")
    old_intake = intake_journal.pending_records(owner_episode_id="trait-first")[0]
    episode_sram.close_bucket("trait-first")
    old = intake_journal.record_by_id(old_intake.record_id)
    old_path = Path(walker.path_for_id(old.memory_id)).resolve()
    replace_json = atomic_replace.atomic_replace_json

    def fail_old(path, payload, *args, **kwargs):
        if Path(path).resolve() == old_path:
            raise OSError("synthetic prior-link failure")
        return replace_json(path, payload, *args, **kwargs)

    episode_sram.open_bucket("trait-update", pipeline_id=0)
    identity_store.upsert_trait(Trait("synthetic_trait", .3, 3),
                                path=tmp_path / "absent.json", cycle_id="trait-update")
    accepted = intake_journal.pending_records(owner_episode_id="trait-update")[0]
    with monkeypatch.context() as fault:
        fault.setattr(atomic_replace, "atomic_replace_json", fail_old)
        with pytest.raises(RuntimeError, match="synthetic prior-link failure"):
            episode_sram.close_bucket("trait-update")
    assert intake_journal.record_by_id(accepted.record_id).status == intake_journal.PENDING
    assert episode_sram.pending("trait-update")
    with pytest.raises(ValueError, match="competing.*trait"):
        identity_store.load_identity(path=tmp_path / "absent.json")

    monkeypatch.setattr(api, "write_memory", lambda *args, **kwargs: pytest.fail("durable canonical claim was rewritten"))
    episode_sram.close_bucket("trait-update")
    assert intake_journal.record_by_id(accepted.record_id).status == intake_journal.COMMITTED
    current = identity_store.load_identity(path=tmp_path / "absent.json")["synthetic_trait"]
    assert (current.strength, current.evidence_count) == (.3, 3)


def test_full_deepsleep_retry_repairs_prior_trait_write_before_identity_read(monkeypatch, tmp_path):
    from deepsleep_loop import loop
    from memory import atomic_replace, episode_sram, intake_journal
    from tests.core.deepsleep_window_fixtures import (
        active_store_from_episode_body, episode_entry, patch_window_deepsleep_paths,
    )
    from tests.helpers.blueprint_driver import _ModelFreeProjectionEncoder

    patch_window_deepsleep_paths(monkeypatch, tmp_path)
    monkeypatch.setattr("memory.retina.encoder", lambda: _ModelFreeProjectionEncoder())
    episode_sram.open_bucket("trait-prior", pipeline_id=0)
    identity_store.upsert_trait(Trait("synthetic_trait", .2, 2),
                                path=tmp_path / "absent.json", cycle_id="trait-prior")
    old_intake = intake_journal.pending_records(owner_episode_id="trait-prior")[0]
    episode_sram.close_bucket("trait-prior")
    old = intake_journal.record_by_id(old_intake.record_id)
    old_path = Path(walker.path_for_id(old.memory_id)).resolve()
    replace_json = atomic_replace.atomic_replace_json

    def fail_old(path, payload, *args, **kwargs):
        if Path(path).resolve() == old_path:
            raise OSError("synthetic prior-link failure")
        return replace_json(path, payload, *args, **kwargs)

    store, episode = active_store_from_episode_body(episode_entry("trait-full-retry", target="Parsing"))
    episode_sram.open_bucket(episode.episode_id, pipeline_id=episode.pipeline_id)
    identity_store.upsert_trait(Trait("synthetic_trait", .3, 3),
                                path=tmp_path / "absent.json", cycle_id=episode.episode_id)
    accepted = intake_journal.pending_records(owner_episode_id=episode.episode_id)[0]
    with monkeypatch.context() as fault:
        fault.setattr(atomic_replace, "atomic_replace_json", fail_old)
        with pytest.raises(RuntimeError, match="synthetic prior-link failure"):
            loop.run_deepsleep_for_episode(store)
    assert intake_journal.record_by_id(accepted.record_id).status == intake_journal.PENDING
    assert store.current() is episode

    outcome = loop.run_deepsleep_for_episode(store)
    assert outcome is not None
    assert store.current() is None
    assert intake_journal.record_by_id(accepted.record_id).status == intake_journal.COMMITTED
    assert identity_store.load_identity(path=tmp_path / "absent.json")["synthetic_trait"].strength == .3


def test_trait_recovery_deltas_are_unsequenced_even_under_commit_exclusion():
    from core.cognition_lock import commit_lock
    from memory import projection_delta

    (old, _old_path), (new, _new_path) = _seed_traits()
    with commit_lock():
        post_commit.ensure_trait_revision(new["id"], sequenced=False)
    rows = [json.loads(line) for line in projection_delta.outbox_path().read_text(encoding="utf-8").splitlines()]
    assert {old["id"], new["id"]} <= {row["memory_id"] for row in rows}
    assert all(row["sequenced"] is False for row in rows)


def test_unsequenced_final_publication_covers_entity_link_peers():
    from core.cognition_lock import commit_lock
    from memory import projection_delta

    (old, old_path), (new, new_path) = _seed_traits()
    with commit_lock():
        report = projection_delta.publish_final_projection_state(
            new["id"], new_path, sequenced=False,
            hook_report={
                "entity_links": 1,
                "entity_linker": {"peer_rewrites": [{"id": old["id"], "path": str(old_path)}]},
            },
        )
    assert report["obligation_satisfied"] is True
    rows = [json.loads(line) for line in projection_delta.outbox_path().read_text(encoding="utf-8").splitlines()]
    assert {old["id"], new["id"]} <= {row["memory_id"] for row in rows}
    assert all(row["sequenced"] is False for row in rows)


def test_deepsleep_entry_reconciliation_does_not_write_or_ack_fresh_trait(monkeypatch, tmp_path):
    from deepsleep_loop import loop
    from memory import api, episode_sram, intake_journal

    episode_sram.open_bucket("fresh-trait", pipeline_id=0)
    identity_store.upsert_trait(Trait("synthetic_trait", .2, 2),
                                path=tmp_path / "absent.json", cycle_id="fresh-trait")
    accepted = intake_journal.pending_records(owner_episode_id="fresh-trait")[0]
    monkeypatch.setattr(api, "write_memory", lambda *args, **kwargs: pytest.fail("premature fresh canonical write"))
    monkeypatch.setattr(post_commit, "ensure_trait_revision", lambda *args, **kwargs: pytest.fail("fresh record has no durable revision"))
    loop._reconcile_pending_trait_commits("fresh-trait")
    assert intake_journal.record_by_id(accepted.record_id).status == intake_journal.PENDING
    assert len(episode_sram.pending("fresh-trait")) == 1
    assert walker.path_for_id(accepted.payload["id"]) is None

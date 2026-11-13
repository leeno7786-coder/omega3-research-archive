"""The semantic retina is a generation artifact (audit 2026-09-28 M01/M05/M08).

Real generation build → publication → read-transaction contracts; only the Ettin
forward pass is replaced by a deterministic counting encoder, so no weights are
needed and every encode is observable.

* M01 — each read transaction perceives through ITS captured generation's codes.
  A lane still holding G1 keeps G1's codes while a lane opened after G2 was
  published perceives G2, including the record G2 added. No global reload.
* M05 — the retina's codes and index are pinned inside the generation content with
  the encoder/render contract. Missing, corrupt or contract-mismatched artifacts
  fail validation, reads and boot health; an empty corpus is declared, not guessed.
* M08 — the next publication reuses the verified prior generation's codes by
  rendered-text hash under the same contract; only new or changed texts encode.
"""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import numpy as np
import pytest

import core.paths as core_paths
from core import timekeeper as TK
from memory import retina
from memory.projection import build as B
from memory.projection import generation as G
from memory.semantic_plane import LATENT_DIM
from tests.memory.projection._fixtures import (
    build_and_publish,
    redirect_timekeeper,
    seed_records_only,
    write_record,
)

NEW_TITLE = "Glia"
NEW_SUMMARY = "Glial cells support neurons and insulate axons."
SEEDED = {"k1", "k2", "k3", "k4"}


class CountingEncoder:
    """Deterministic unit latents from the text hash; records every text encoded."""

    def __init__(self) -> None:
        self.texts: list[str] = []

    def encode(self, texts):
        vectors = []
        for text in texts:
            self.texts.append(str(text))
            seed = int.from_bytes(hashlib.sha256(str(text).encode()).digest()[:8], "big")
            vector = np.random.Generator(np.random.PCG64(seed)).normal(size=LATENT_DIM).astype(np.float32)
            vectors.append(vector / np.linalg.norm(vector))
        return np.stack(vectors)


@pytest.fixture
def encoder(tmp_path, monkeypatch):
    redirect_timekeeper(tmp_path)
    retina.unload()
    enc = CountingEncoder()
    monkeypatch.setattr(retina, "encoder", lambda: enc)
    yield enc
    retina.unload()
    TK.reset_for_tests()


@pytest.fixture
def profile() -> Path:
    # The isolated profile that owns core.paths.MEMORY_ROOT, so boot's default-root
    # read transaction and these builds address the same temporary generation.
    return Path(core_paths.MEMORY_ROOT).parent


def _active_dir(profile: Path, publication: G.PublicationResult) -> Path:
    return G.projection_root(profile) / publication.active_manifest.generation_dir


def _window_ids(query: str, k: int = 10) -> list[str]:
    return [hit.record_id for hit in retina.window(query, k)]


def _record_text(title: str, summary: str) -> str:
    return retina.render_record({"title": title, "summary": summary})


# ── M01: generation-bound perception, overlapping lanes ──────────────────────

def test_overlapping_lanes_each_perceive_through_their_own_generation(encoder, profile):
    seed_records_only(profile)
    _, first = build_and_publish(profile, reason="retina-g1")
    assert first is not None

    with G.projection_read_transaction(profile) as old_lane:
        assert old_lane.generation_id == first.generation_id
        # No boot mapping and no global load: the transaction's generation binds it.
        assert retina.is_loaded() is True
        assert set(_window_ids(NEW_SUMMARY)) == SEEDED

        write_record(profile, "k5", title=NEW_TITLE, summary=NEW_SUMMARY)
        _, second = build_and_publish(profile, reason="retina-g2")
        assert second is not None and second.generation_id != first.generation_id

        seen: dict[str, object] = {}

        def new_lane() -> None:
            # A separate thread starts with its own context: another cognition lane.
            with G.projection_read_transaction(profile) as ctx:
                seen["generation_id"] = ctx.generation_id
                seen["ids"] = set(_window_ids(NEW_SUMMARY))
                seen["nearest"] = _window_ids(NEW_SUMMARY, 1)
                seen["stats"] = retina.stats()

        lane = threading.Thread(target=new_lane)
        lane.start()
        lane.join(timeout=60)
        assert not lane.is_alive()

        assert seen["generation_id"] == second.generation_id
        assert seen["ids"] == SEEDED | {"k5"}
        assert seen["nearest"] == ["k5"]
        assert seen["stats"]["generation_id"] == second.generation_id and seen["stats"]["codes"] == 5

        # The older lane still holds G1's lease and still perceives only G1.
        assert set(_window_ids(NEW_SUMMARY)) == SEEDED
        assert retina.stats()["generation_id"] == first.generation_id


def test_recall_retina_lane_proposes_from_its_transactions_generation(encoder, profile, monkeypatch):
    from types import SimpleNamespace

    from memory import recall_lanes

    monkeypatch.setattr(recall_lanes, "_rerank_window", lambda _query, _window: None)
    seed_records_only(profile)
    build_and_publish(profile, reason="retina-lane-g1")
    write_record(profile, "k5", title=NEW_TITLE, summary=NEW_SUMMARY)
    _, second = build_and_publish(profile, reason="retina-lane-g2")
    snapshot = {mid: {"coord": [2, 0, int(mid[-1])], "title": mid} for mid in SEEDED | {"k5"}}
    plan = SimpleNamespace(original_query=NEW_SUMMARY, limit_per_lane=1)
    receipts: list[dict] = []

    with G.projection_read_transaction(profile) as ctx:
        assert ctx.generation_id == second.generation_id
        hits = recall_lanes._run_retina_lane(plan, snapshot, lambda _mid: None, set(), truncation_receipts=receipts)

    assert [hit.memory_id for hit in hits] == ["k5"]
    assert receipts and receipts[0]["input_count"] == 5 and receipts[0]["output_count"] == 1


def test_stats_outside_a_transaction_reports_the_active_generation(encoder, profile):
    seed_records_only(profile)
    _, publication = build_and_publish(profile, reason="retina-stats")

    stats = retina.stats()

    assert stats["loaded"] is True and stats["state"] == "mapped"
    assert stats["generation_id"] == publication.generation_id and stats["codes"] == 4


# ── M05: the retina is generation-hashed with its encoder/render contract ────

def test_published_generation_pins_retina_artifacts_and_encoder_contract(encoder, profile):
    seed_records_only(profile)
    _, publication = build_and_publish(profile, reason="retina-pin")
    component = publication.active_manifest.content.components["semantic_retina"]

    assert isinstance(component, dict), f"semantic_retina must be pinned, got {component!r}"
    assert component["status"] == "built" and component["records"] == 4
    assert component["contract_sha256"] == retina.contract_sha256()
    gen_dir = _active_dir(profile, publication)
    assert component["codes_sha256"] == G.sha256_file(gen_dir / retina.CODES_FILE)
    assert component["index_sha256"] == G.sha256_file(gen_dir / retina.INDEX_FILE)


def _damage(gen_dir: Path, case: str) -> None:
    codes = gen_dir / retina.CODES_FILE
    index = gen_dir / retina.INDEX_FILE
    if case == "missing_codes":
        codes.unlink()
    elif case == "corrupt_codes":
        codes.write_bytes(b"broken")
    elif case == "missing_index":
        index.unlink()
    elif case == "rewritten_index":
        index.write_bytes(index.read_bytes().replace(b"k1", b"kX"))
    else:  # pragma: no cover
        raise AssertionError(case)


DAMAGE = ("missing_codes", "corrupt_codes", "missing_index", "rewritten_index")


@pytest.mark.parametrize("case", DAMAGE)
def test_damaged_retina_fails_generation_validation(encoder, profile, case):
    seed_records_only(profile)
    _, publication = build_and_publish(profile, reason=f"retina-{case}")
    gen_dir = _active_dir(profile, publication)
    _damage(gen_dir, case)

    with pytest.raises(G.GenerationIntegrityError):
        G.validate_generation_directory(gen_dir, G.read_receipt(gen_dir))


@pytest.mark.parametrize("case", DAMAGE)
def test_damaged_retina_is_unavailable_to_the_reading_lane(encoder, profile, case):
    seed_records_only(profile)
    _, publication = build_and_publish(profile, reason=f"retina-read-{case}")
    _damage(_active_dir(profile, publication), case)

    with G.projection_read_transaction(profile) as ctx:
        assert ctx.generation_id == publication.generation_id
        with pytest.raises(retina.RetinaUnavailable):
            retina.is_loaded()
        with pytest.raises(retina.RetinaUnavailable):
            retina.window(NEW_SUMMARY, 3)


@pytest.mark.parametrize("case", DAMAGE)
def test_boot_refuses_to_report_a_damaged_retina_healthy(encoder, profile, monkeypatch, case):
    import boot
    from core.required_substrate import RequiredSubstrateError
    from memory import api

    seed_records_only(profile)
    _, publication = build_and_publish(profile, reason=f"retina-boot-{case}")
    _damage(_active_dir(profile, publication), case)
    health: list[tuple[str, str]] = []
    monkeypatch.setattr(api, "set_embedding_substrate_state", lambda state, detail="": health.append((state, detail)))

    report: dict = {}
    with pytest.raises(RequiredSubstrateError) as raised:
        boot._boot_semantic_retina(report)

    assert raised.value.component == "memory_retina" and raised.value.operation == "map"
    assert isinstance(raised.value.__cause__, retina.RetinaUnavailable)
    assert report["retina"] == "down"
    assert health == []


def test_boot_reports_the_bound_generation_codes(encoder, profile, monkeypatch):
    import boot
    from memory import api

    seed_records_only(profile)
    _, publication = build_and_publish(profile, reason="retina-boot-ok")
    health: list[tuple[str, str]] = []
    monkeypatch.setattr(api, "set_embedding_substrate_state", lambda state, detail="": health.append((state, detail)))

    report: dict = {}
    boot._boot_semantic_retina(report)

    assert report["retina"] == "up"
    assert len(health) == 1 and health[0][0] == "ok"
    assert "codes=4" in health[0][1] and publication.generation_id[:12] in health[0][1]


def test_empty_corpus_publishes_an_explicit_empty_semantic_plane(encoder, profile, monkeypatch):
    import boot
    from memory import api

    _, publication = build_and_publish(profile, reason="retina-empty")
    assert publication is not None
    component = publication.active_manifest.content.components["semantic_retina"]
    assert isinstance(component, dict) and component["status"] == "empty" and component["records"] == 0
    gen_dir = _active_dir(profile, publication)
    assert not (gen_dir / retina.CODES_FILE).exists() and not (gen_dir / retina.INDEX_FILE).exists()
    G.validate_generation_directory(gen_dir, G.read_receipt(gen_dir))

    with G.projection_read_transaction(profile):
        assert retina.is_loaded() is False            # nothing to perceive — not a fault
        assert retina.window(NEW_SUMMARY, 3) == []
        assert retina.stats()["state"] == "empty"

    health: list[tuple[str, str]] = []
    monkeypatch.setattr(api, "set_embedding_substrate_state", lambda state, detail="": health.append((state, detail)))
    report: dict = {}
    boot._boot_semantic_retina(report)
    assert report["retina"] == "up"
    assert health and health[0][0] == "ok" and "codes=0" in health[0][1] and "empty" in health[0][1]

    # An empty plane carries no retina artifacts; a stray one is a mixed generation.
    (gen_dir / retina.CODES_FILE).write_bytes(b"\x00" * (retina.DIMENSION // 8))
    with pytest.raises(G.GenerationIntegrityError):
        G.validate_generation_directory(gen_dir, G.read_receipt(gen_dir))


def test_retina_build_failure_blocks_the_candidate(encoder, profile, monkeypatch):
    from memory.ettin_encoder import EttinExecutionError

    class BrokenEncoder:
        def encode(self, texts):
            raise EttinExecutionError("forward pass failed")

    monkeypatch.setattr(retina, "encoder", lambda: BrokenEncoder())
    seed_records_only(profile)

    with G.projection_publication_authority(profile) as authority:
        with pytest.raises(retina.RetinaUnavailable) as raised:
            B.build_generation(profile, reason="retina-broken", authority=authority)

    assert isinstance(raised.value.__cause__, EttinExecutionError)
    proj = G.projection_root(profile)
    assert not any(p.name.startswith(G.GENERATION_DIR_PREFIX) for p in proj.iterdir())
    assert G.read_active_manifest(proj) is None


def test_contract_change_makes_the_generation_unavailable_and_the_projector_rebuilds(encoder, profile, monkeypatch):
    from memory.projection import projector

    seed_records_only(profile)
    first = projector.drain_once(profile, reason="retina-contract-g1")
    assert first.publication is not None
    old = first.publication.active_manifest.content.components["semantic_retina"]
    assert isinstance(old, dict) and old["contract_sha256"] == retina.contract_sha256()

    pinned = retina.encoder_contract()
    monkeypatch.setattr(retina, "encoder_contract", lambda: {**pinned, "render_schema": "omega.semantic_retina.render.v2"})
    assert retina.contract_sha256() != old["contract_sha256"]

    with G.projection_read_transaction(profile):
        with pytest.raises(retina.RetinaUnavailable, match="contract"):
            retina.is_loaded()

    encoder.texts.clear()
    second = projector.drain_once(profile, reason="retina-contract-g2")
    assert second.publication is not None and second.generation_id != first.generation_id
    new = second.publication.active_manifest.content.components["semantic_retina"]
    assert new["contract_sha256"] == retina.contract_sha256()
    assert len(encoder.texts) == 4, "codes built under another contract are never reused"
    with G.projection_read_transaction(profile):
        assert retina.is_loaded() is True


# ── M08: reuse the verified prior generation's codes ─────────────────────────

def test_next_publication_encodes_only_new_texts(encoder, profile):
    seed_records_only(profile)
    _, first = build_and_publish(profile, reason="retina-reuse-g1")
    assert first is not None and len(encoder.texts) == 4
    old = np.fromfile(_active_dir(profile, first) / retina.CODES_FILE, dtype=np.uint8).reshape(4, -1)

    encoder.texts.clear()
    write_record(profile, "k5", title=NEW_TITLE, summary=NEW_SUMMARY)
    _, second = build_and_publish(profile, reason="retina-reuse-g2")
    assert second is not None and second.generation_id != first.generation_id

    assert encoder.texts == [_record_text(NEW_TITLE, NEW_SUMMARY)]
    # Reused codes are the prior generation's verified bytes, row for row.
    new = np.fromfile(_active_dir(profile, second) / retina.CODES_FILE, dtype=np.uint8).reshape(5, -1)
    assert np.array_equal(new[:4], old)


def test_unverified_prior_codes_are_never_reused(encoder, profile):
    seed_records_only(profile)
    _, first = build_and_publish(profile, reason="retina-unverified-g1")
    (_active_dir(profile, first) / retina.CODES_FILE).write_bytes(b"\x00" * (4 * retina.DIMENSION // 8))

    encoder.texts.clear()
    write_record(profile, "k5", title=NEW_TITLE, summary=NEW_SUMMARY)
    _, second = build_and_publish(profile, reason="retina-unverified-g2")

    assert second is not None
    assert len(encoder.texts) == 5, "a prior whose bytes fail the manifest hash is not reused"
    gen_dir = _active_dir(profile, second)
    G.validate_generation_directory(gen_dir, G.read_receipt(gen_dir))

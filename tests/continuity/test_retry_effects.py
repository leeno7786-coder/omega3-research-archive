"""WO-07 carry-over: a retried terminus reports the semantic and identity effects its first pass made.

``EpisodeStore.close`` keeps an episode open when its terminus raises and runs the whole terminus
again. WO-07 made every per-episode effect apply once and report its real delta through receipts —
mastery, failure, journal, pathway, trajectory — and left two: semantic promotion and identity
traits applied once on a retry, but the retry's journal row reported them EMPTY, because each
recomputed "what changed" from a store the first pass had already moved.

For semantic promotion that is not only a reporting gap: ``semantic_effects`` are what
``consolidate_to_memory`` turns into canonical knowledge, so an empty retry effect loses the
knowledge write the first pass never reached.
"""

from __future__ import annotations

import pytest

from pressure_loop.episode import EpisodeStore
from tests.continuity.test_effect_idempotency import _fail_once
from tests.helpers import continuity_terminus as ct

DOMAIN = "quantum_physics"
STATEMENT = "A qubit holds a superposition of basis states until it is measured."
TRAIT = "explains_with_examples"


@pytest.fixture(autouse=True)
def _terminus(tmp_path, monkeypatch):
    from memory import episode_sram

    paths = ct.isolate(monkeypatch, tmp_path)
    yield paths
    episode_sram.reset()


def _learn_with(store, *, knowledge: bool = False, identity: bool = False):
    carrier = ct.dress_learn(ct.open_carrier(store), domain=DOMAIN)
    omega = carrier.global_["omega_result"]
    if knowledge:
        omega["knowledge_to_ingest"] = [{"domain": DOMAIN, "statement": STATEMENT,
                                         "confidence": 0.9}]
    if identity:
        omega["identity_patterns"] = [{"trait": TRAIT, "pattern": "Explains with worked examples.",
                                       "evidence": "used two worked examples", "status": "new"}]
    return carrier


def test_a_retried_terminus_reports_and_writes_the_knowledge_its_first_pass_promoted(monkeypatch):
    import deepsleep_loop.consolidate_memory as consolidate_memory
    import deepsleep_loop.loop as loop
    from deepsleep_loop.semantic_store import load_semantic, semantic_key

    captured: list[dict] = []
    real = consolidate_memory.consolidate_to_memory

    def spy(*args, **kwargs):
        captured.append(dict(kwargs.get("semantic_effects") or {}))
        return real(*args, **kwargs)

    monkeypatch.setattr(consolidate_memory, "consolidate_to_memory", spy)
    store = EpisodeStore()
    _learn_with(store, knowledge=True)
    # Promotion lands in the semantic store; the NEXT phase raises before any acknowledgement.
    _fail_once(monkeypatch, loop.identity_phase, "update_identity", "injected after semantic")
    with pytest.raises(RuntimeError, match="injected after semantic"):
        ct.close(store)

    key = semantic_key("quantum-physics", STATEMENT)
    assert key in load_semantic(), "the first pass did not promote the statement"

    pack = ct.close(store)
    assert key in pack.journal_entry["semantic_effects"], (
        "the retry recomputed promotion from the already-updated store and reported nothing"
    )
    assert captured and key in captured[-1], (
        "the retry never handed the promoted statement to the canonical knowledge write"
    )


def test_a_retried_terminus_reports_the_identity_trait_its_first_pass_promoted(monkeypatch):
    import deepsleep_loop.loop as loop
    from deepsleep_loop.identity_store import load_identity

    # One earlier episode observed the trait; this episode's observation promotes it.
    earlier = EpisodeStore()
    _learn_with(earlier, identity=True)
    ct.close(earlier)
    assert TRAIT not in load_identity(), "one observation must not promote a trait"

    store = EpisodeStore()
    _learn_with(store, identity=True)
    # The trait is promoted, then the NEXT phase raises before any acknowledgement.
    _fail_once(monkeypatch, loop.trajectory_phase, "maintain_trajectory", "injected after identity")
    with pytest.raises(RuntimeError, match="injected after identity"):
        ct.close(store)

    pack = ct.close(store)
    trait = load_identity()[TRAIT]
    assert trait.evidence_count == 2, "the retry promoted the same observation twice"
    effect = pack.journal_entry["identity_effects"].get(TRAIT)
    assert effect is not None, (
        "the retry recomputed the trait from the already-updated store and reported nothing"
    )
    assert effect["evidence_delta"] == 2 and effect["strength"] == pytest.approx(trait.strength)

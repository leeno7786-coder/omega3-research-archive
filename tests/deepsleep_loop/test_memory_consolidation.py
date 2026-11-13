"""Slice 7a — DeepSleep Phase 7 memory consolidation (policy + phase)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deepsleep_loop.consolidate_memory.run import (
    _identity_envelopes,
    _journal_envelope,
    _knowledge_coord,
    _mastery_envelopes,
    _resolved_query_envelopes,
    _semantic_envelopes,
    consolidate_to_memory,
)
from pressure_loop.policy import (
    clear_policy_cache,
    deepsleep_memory_consolidation_enabled,
)


def test_memory_consolidation_flag_reads_live_policy():
    clear_policy_cache()
    # config/deepsleep.yaml — enabled when Phase 7 consolidation is live.
    assert deepsleep_memory_consolidation_enabled() is True


def test_memory_consolidation_flag_off_when_disabled():
    clear_policy_cache()
    policy = {"deepsleep": {"memory_consolidation": {"enabled": False}}}
    assert deepsleep_memory_consolidation_enabled(policy) is False


def test_knowledge_coord_resolves_known_domain():
    assert _knowledge_coord("quantum_physics") == [2, 2, 0]


def test_knowledge_coord_falls_back_to_ai():
    assert _knowledge_coord("unknown_domain_xyz") == [2, 24, 0]


# ---------------------------------------------------------------------------
# Loop-closure slice #3 — flag-aware _knowledge_coord. When lobe 2 is in the
# dynamic-Y feature flag, DeepSleep consolidation stores learned knowledge
# under the SAME dynamically-allocated subdomain the gate write path + recall
# resolve to (storage -> recall alignment). Flag-OFF stays byte-for-byte
# static, and a flagged-lobe allocation failure fails HARD (no silent fallback
# into the retiring static taxonomy).
# ---------------------------------------------------------------------------


def _dyn_setup(tmp_path, monkeypatch, flag):
    root = tmp_path / "omega_root"
    (root / "memory" / "200_knowledge").mkdir(parents=True)
    (root / "guardian" / "state").mkdir(parents=True)
    import core.paths as paths

    monkeypatch.setattr(paths, "OMEGA_ROOT", root)
    import memory.subdomain_flag as flag_mod

    monkeypatch.setattr(flag_mod, "_FLAG_CACHE", frozenset(flag))
    return root


def test_knowledge_coord_flag_on_uses_dynamic_allocator(tmp_path, monkeypatch):
    _dyn_setup(tmp_path, monkeypatch, {2})
    from memory.knowledge_router import _derive_name
    from memory.subdomain_allocator import _load_index

    # a KNOWLEDGE topic (no code markers) routes to lobe 2 via the dynamic allocator.
    coord = _knowledge_coord("ancient rome")
    # First dynamic allocation -> Y=0 (NOT the static Y=24 default for an
    # unmapped domain) — proves consolidation routes through the allocator.
    assert coord == [2, 0, 0], f"flag-ON must allocate dynamic Y=0, got {coord}"
    # Stored under the SAME subdomain the allocator/recall use.
    dirname = _derive_name("ancient rome", "ancient rome")
    subs = _load_index().get("lobes", {}).get("2", {}).get("subdomains", {})
    assert dirname in subs, f"allocator must have created {dirname!r}; got {list(subs)}"
    # Idempotent: same domain -> same coord, no second subdomain.
    assert _knowledge_coord("ancient rome") == [2, 0, 0]
    assert len(_load_index().get("lobes", {}).get("2", {}).get("subdomains", {})) == 1


def test_knowledge_coord_routes_coding_topic_to_lobe_8(tmp_path, monkeypatch):
    # a learned CODING topic (code markers) routes to the CODING lobe (8), not knowledge —
    # so it lands beside the operator's coding world + becomes a coding-lobe mastery topic.
    _dyn_setup(tmp_path, monkeypatch, {2, 8})  # onboarding flags both lobes
    coord = _knowledge_coord("python async")
    assert coord == [8, 0, 0], f"coding topic must route to lobe 8, got {coord}"


def test_knowledge_coord_flag_off_static_byte_for_byte(tmp_path, monkeypatch):
    root = _dyn_setup(tmp_path, monkeypatch, set())  # flag OFF
    assert _knowledge_coord("quantum_physics") == [2, 2, 0]
    assert _knowledge_coord("unknown_domain_xyz") == [2, 24, 0]
    # Static path must never touch the allocator.
    assert not (root / "guardian" / "state" / "dynamic_y_index.json").exists()


def test_knowledge_coord_flag_on_allocator_failure_fails_hard(tmp_path, monkeypatch):
    _dyn_setup(tmp_path, monkeypatch, {2})
    import memory.subdomain_allocator as alloc_mod

    def _boom(**kwargs):
        raise RuntimeError("allocator down")

    monkeypatch.setattr(alloc_mod, "get_or_create_subdomain", _boom)
    # Fail-HARD: a flagged-lobe allocation failure must propagate, not silently
    # fall back to the retiring static taxonomy (which would mis-route recall).
    with pytest.raises(RuntimeError):
        _knowledge_coord("ancient rome")  # knowledge topic -> lobe 2 (flagged) -> boom


def test_journal_envelope_coords_and_cycle_id():
    entry = {
        "intent": "LEARN",
        "action": "web_fetch",
        "outcome": {"goal_state": "completed"},
        "omega_reasoning": {"what": "learned basics"},
    }
    env = _journal_envelope("ep-7a", entry)
    assert env is not None
    assert env.cycle_id == "ep-7a"
    assert env.coord == [6, 0, 0]
    assert env.type == "consolidation_journal"
    assert "LEARN" in env.payload["text"]


def test_journal_envelope_empty_returns_none():
    assert _journal_envelope("ep-7a", {}) is None


def test_identity_envelopes_per_trait():
    effects = {
        "curious": {
            "name": "curious",
            "strength_delta": 0.1,
            "evidence_delta": 1,
            "strength": 0.5,
            "evidence_count": 3,
        }
    }
    envs = _identity_envelopes("ep-7a", effects)
    assert envs == []


def test_mastery_envelopes_knowledge_and_application():
    effects = {
        "quantum_physics": {
            "domain_name": "quantum_physics",
            "knowledge_delta": 0.1,
            "application_delta": 0.1,
            "knowledge_mastery": 0.5,
            "application_mastery": 0.4,
            "status": "learning",
        }
    }
    envs = _mastery_envelopes("ep-7a", effects)
    coords = [e.coord for e in envs]
    assert [2, 2, 0] in coords
    assert [5, 0, 0] in coords
    assert all(e.type == "mastery_progress" for e in envs)
    assert all(e.cycle_id == "ep-7a" for e in envs)


def test_mastery_envelopes_knowledge_only_when_no_application_delta():
    effects = {
        "python": {
            "domain_name": "python",
            "knowledge_delta": 0.1,
            "application_delta": 0.0,
            "knowledge_mastery": 0.3,
            "application_mastery": 0.0,
            "status": "learning",
        }
    }
    envs = _mastery_envelopes("ep-7a", effects)
    assert len(envs) == 1
    assert envs[0].coord == [2, 24, 0]  # python not in SUBDOMAIN_MAP — ai fallback


def test_semantic_envelopes_per_fact():
    effects = {
        "quantum_physics": {
            "domain": "quantum_physics",
            "statement": "foundational understanding of quantum_physics",
            "reinforcement_count": 3,
            "promoted_episode": "ep-7a",
        }
    }
    envs = _semantic_envelopes("ep-7a", effects)
    assert len(envs) == 1
    assert envs[0].coord == [2, 2, 0]
    assert envs[0].type == "semantic_fact"
    assert envs[0].payload["text"] == effects["quantum_physics"]["statement"]
    assert envs[0].payload["domain"] == "quantum_physics"


def test_resolved_query_envelopes_only_resolution_flips():
    effects = [
        {
            "query_id": "q1",
            "status": "open",
            "question": "What is spin?",
            "domain": "quantum_physics",
            "action": "created",
        },
        {
            "query_id": "q2",
            "status": "resolved",
            "question": "What is entanglement?",
            "domain": "quantum_physics",
            "action": "updated",
            "prior_status": "open",
        },
        {
            "query_id": "q3",
            "status": "open",
            "question": "What is decoherence?",
            "domain": "quantum_physics",
            "action": "updated",
            "prior_status": "open",
        },
    ]
    envs = _resolved_query_envelopes("ep-7a", effects)
    assert len(envs) == 1
    assert envs[0].coord == [3, 1, 0]
    assert envs[0].type == "resolved_inquiry"
    assert "entanglement" in envs[0].payload["text"]


def test_consolidate_to_memory_off_no_writes(monkeypatch):
    calls: list = []

    def _spy(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(validation_status="held")

    monkeypatch.setattr("deepsleep_loop.consolidate_memory.run.intake_memory", _spy)

    result = consolidate_to_memory(
        "ep-7a",
        {"intent": "LEARN"},
        mastery_effects={
            "d": {
                "domain_name": "python",
                "knowledge_delta": 0.1,
                "application_delta": 0.0,
                "knowledge_mastery": 0.1,
                "application_mastery": 0.0,
                "status": "learning",
            }
        },
        enabled=False,
    )
    assert result == {"written": [], "failed": [], "enabled": False}
    assert calls == []


def test_consolidate_to_memory_on_writes_all_envelopes(monkeypatch):
    written_coords: list = []

    def _spy(envelope, propose=False, bypass_guardian=False, **kwargs):
        written_coords.append(list(envelope.coord))
        return SimpleNamespace(validation_status="held")

    monkeypatch.setattr("deepsleep_loop.consolidate_memory.run.intake_memory", _spy)

    journal = {"intent": "LEARN", "action": "web_fetch", "omega_reasoning": {"what": "ok"}}
    mastery = {
        "qp": {
            "domain_name": "quantum_physics",
            "knowledge_delta": 0.1,
            "application_delta": 0.1,
            "knowledge_mastery": 0.5,
            "application_mastery": 0.4,
            "status": "learning",
        }
    }
    identity = {
        "curious": {
            "name": "curious",
            "strength_delta": 0.1,
            "evidence_delta": 1,
            "strength": 0.5,
            "evidence_count": 1,
        }
    }
    semantic = {
        "quantum_physics": {
            "domain": "quantum_physics",
            "statement": "foundational understanding of quantum_physics",
            "reinforcement_count": 3,
            "promoted_episode": "ep-7a",
        }
    }
    queries = [
        {
            "query_id": "q1",
            "status": "resolved",
            "question": "What is spin?",
            "domain": "quantum_physics",
            "action": "updated",
        }
    ]

    result = consolidate_to_memory(
        "ep-7a",
        journal,
        mastery_effects=mastery,
        identity_effects=identity,
        semantic_effects=semantic,
        query_effects=queries,
        enabled=True,
    )

    assert result["enabled"] is True
    assert len(result["written"]) == 5  # journal + 2 mastery + semantic + query
    assert [6, 0, 0] in written_coords
    assert [0, 3, 0] not in written_coords
    assert [2, 2, 0] in written_coords
    assert [5, 0, 0] in written_coords
    assert [3, 1, 0] in written_coords


def test_consolidate_to_memory_exception_halts(monkeypatch):
    """Fail-hard: an intake exception is a continuity write failure — it
    propagates and CRASHES, it is NOT swallowed and counted as 'failed'. (A
    returned rejected receipt is a soft verdict; see the next test.)"""
    call_count = 0

    def _spy(envelope, propose=False, bypass_guardian=False, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("write failed")
        return SimpleNamespace(validation_status="held")

    monkeypatch.setattr("deepsleep_loop.consolidate_memory.run.intake_memory", _spy)

    with pytest.raises(RuntimeError, match="write failed"):
        consolidate_to_memory(
            "ep-7a",
            {"intent": "LEARN", "action": "x"},
            mastery_effects={
                "python": {
                    "domain_name": "python",
                    "knowledge_delta": 0.1,
                    "application_delta": 0.0,
                    "knowledge_mastery": 0.3,
                    "application_mastery": 0.0,
                    "status": "learning",
                },
            },
            enabled=True,
        )


def test_consolidate_to_memory_rejected_receipt_counts_as_failed(monkeypatch):
    def _spy(envelope, propose=False, bypass_guardian=False, **kwargs):
        return SimpleNamespace(validation_status="rejected")

    monkeypatch.setattr("deepsleep_loop.consolidate_memory.run.intake_memory", _spy)

    result = consolidate_to_memory(
        "ep-7a",
        {"intent": "LEARN"},
        enabled=True,
    )
    assert len(result["failed"]) == 1
    assert result["written"] == []


def test_consolidate_to_memory_empty_episode_id_no_op(monkeypatch):
    calls: list = []

    def _spy(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(validation_status="held")

    monkeypatch.setattr("deepsleep_loop.consolidate_memory.run.intake_memory", _spy)

    result = consolidate_to_memory("", {"intent": "LEARN"}, enabled=True)
    assert result == {"written": [], "failed": [], "enabled": True}
    assert calls == []

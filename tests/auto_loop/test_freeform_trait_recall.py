"""Freeform trait recall — END-TO-END recall seam.

Promoted traits are written to canonical memory at [0, 1, *] by DeepSleep's identity_store;
one-off observations remain outside preference recall at [0, 3, *]. This guards that the FREEFORM
path surfaces a promoted trait through its real recall execution so the dynamic
answer engineer receives it — i.e. the trait WRITE move is matched by a working READ.

The existing freeform tests stub the recall result (rk) directly; this drives the real freeform
routing (run -> _apply_identity_recall) and the real recall executor (run_forced_memory_tools ->
invoke_remember_tool evidence shaping). Only the lowest-level store read is faked, returning the
canonical trait candidate exactly as memory.api.read_memory would.
"""

from __future__ import annotations

from types import SimpleNamespace

from auto_loop.mril_tools import run_forced_memory_tools
from meta_loop._meta_a_state import MetaAState
from meta_loop.memory.run import run


def _fake_recall_verified(spec):
    """Stand in for memory.api.recall_verified: return verified candidates per lobe."""
    lobes = list(getattr(spec, "allowed_lobes", []) or [])
    if lobes == [0]:
        candidates = [
            {
                "memory_id": "0.constitution.0001",
                "coord": [0, 0, 0],
                "title": "Creator — Noah (Leeno)",
                "summary": "Built Omega 3.0 by hand",
                "score": 0.9,
            },
            {
                "memory_id": "trait.curious.deadbeefcafe",
                "coord": [0, 1, 0],
                "title": "Identity Trait - curious",
                "summary": "curious emergent trait",
                "score": 0.9,
            },
        ]
    elif lobes == [1]:
        candidates = [
            {
                "memory_id": "1.persona.0001",
                "coord": [1, 0, 0],
                "title": "Omega persona",
                "summary": "warm and direct",
                "score": 0.9,
            },
        ]
    else:
        candidates = []
    if not candidates:
        return SimpleNamespace(status="unknown", reason="none", answer_payload=None)
    return SimpleNamespace(
        status="verified",
        reason="verified",
        answer_payload={"candidates": candidates},
    )


def _freeform_pack():
    state = MetaAState(
        mi=None,
        episode_id="ep-ff-trait",
        intent="RESPOND",
        action="RESPONSE",
        source={},
        text="hey there",
        target_str="hey there",
        constraints={},
        meta={"chat_intent": "freeform"},
    )
    run(state)  # exercises the real freeform branch -> _apply_identity_recall
    assert [t["tool"] for t in state.memory_tools] == [
        "remember_identity",
        "remember_persona",
        "remember_knowledge",
        "remember_code",
        "remember_time",
    ]
    return {
        "memory_required": True,
        "memory_tools": state.memory_tools,
        "memory_query": state.memory_query,
        "memory_reason": state.memory_reason,
        "memory_scope": state.memory_scope,
    }


def test_freeform_recall_surfaces_canonical_trait():
    pack = _freeform_pack()
    results, _env = run_forced_memory_tools(pack, "ep-ff-trait", memory=_fake_recall_verified)
    identity_evidence = next(
        result["evidence"] for result in results if result.get("tool") == "remember_identity"
    )
    assert any(item.get("memory_id") == "trait.curious.deadbeefcafe" for item in identity_evidence)

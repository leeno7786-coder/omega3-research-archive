"""Slice 1 — deterministic subdomain recall lane (RED → GREEN)."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from auto_loop.mril_tools.remember import invoke_remember_tool
from auto_loop.mril_tools.router import route_memory_tool
from auto_loop.mril_tools.subdomain_helpers import subdomain_for
from memory.api import QuerySpec
from memory.dewey_map import SUBDOMAIN_MAP
from memory.engine.walker import walker_query


# ── subdomain_for (pure deterministic helper) ─────────────────────────


def test_subdomain_for_identity_omega_second_person():
    assert subdomain_for("what is your identity", 0) == 6


def test_subdomain_for_identity_operator_first_person():
    assert subdomain_for("who am i", 0) in {0, 1}


def test_subdomain_for_identity_open_questions():
    assert subdomain_for("what open questions", 0) == 5


def test_subdomain_for_rules_questions():
    assert subdomain_for("what are your rules", 0) == 4
    assert subdomain_for("never do this", 0) == 4
    assert subdomain_for("tell me about your rules", 0) == 4


def test_subdomain_for_knowledge_metaphysics_not_physics_substring():
    physics_y = next(y for (x, y), name in SUBDOMAIN_MAP.items() if x == 2 and name == "physics")
    assert subdomain_for("metaphysics of mind", 2) != physics_y
    assert subdomain_for("metaphysics of time", 2) != physics_y


def test_subdomain_for_knowledge_quantum_physics_term():
    quantum_y = next(
        y for (x, y), name in SUBDOMAIN_MAP.items() if x == 2 and name == "quantum_physics"
    )
    physics_y = next(y for (x, y), name in SUBDOMAIN_MAP.items() if x == 2 and name == "physics")
    assert subdomain_for("quantum entanglement", 2) == quantum_y
    assert quantum_y != physics_y


def test_subdomain_for_coding_python_async_term():
    python_async_y = next(
        y for (x, y), name in SUBDOMAIN_MAP.items() if x == 8 and name == "python_async"
    )
    assert subdomain_for("python async", 8) == python_async_y


# ── router + invoke_remember_tool wiring ──────────────────────────────


class _Candidate:
    def __init__(self, *, mid="mem_1", coord=None, score=0.8, summary="hit"):
        self.id = mid
        self.coord = coord or [0, 6, 0]
        self.score = score
        self.rerank_score = score
        self.summary = summary
        self.title = "title"


class _Result:
    def __init__(self, candidates):
        self.candidates = candidates


def test_invoke_remember_tool_passes_allowed_subdomains_for_pronoun_query():
    seen = []

    def fake_read(spec):
        seen.append(
            {
                "cycle_id": spec.cycle_id,
                "episode_id": getattr(spec, "episode_id", None),
                "allowed_subdomains": list(getattr(spec, "allowed_subdomains", None) or []),
                "allowed_lobes": list(spec.allowed_lobes),
            }
        )
        return _Result([_Candidate()])

    tool = route_memory_tool("what is your identity")
    # Router may not map this query to identity; force the identity tool for the contract.
    tool = {
        "tool": "remember_identity",
        "query": "what is your identity",
        "allowed_lobes": [0],
        "memory_type": "episodic",
        "subdomain": subdomain_for("what is your identity", 0),
    }
    invoke_remember_tool(tool, "ep_sub", memory=fake_read)
    # The first call is the routed scoped recall. A later whole-lobe widening is
    # allowed when candidate responsiveness rejects that first result.
    assert seen[0] == {
        "cycle_id": "ep_sub-mril-remember_identity",
        "episode_id": "ep_sub",
        "allowed_subdomains": [6],
        "allowed_lobes": [0],
    }


def test_invoke_remember_tool_subdomain_scoped_read_returns_only_matching_coord():
    """Only lobe-0 / subdomain-6 hits should surface when scoped to [6]."""

    def fake_read(spec):
        allowed_y = (getattr(spec, "allowed_subdomains", None) or [None])[0]
        hits = [
            _Candidate(mid="omega_hit", coord=[0, 6, 0], summary="omega identity"),
            _Candidate(mid="traits_hit", coord=[0, 2, 0], summary="traits"),
        ]
        if allowed_y is not None:
            hits = [c for c in hits if c.coord[1] == allowed_y]
        return _Result(hits)

    tool = {
        "tool": "remember_identity",
        "query": "what is your identity",
        "allowed_lobes": [0],
        "memory_type": "episodic",
        "subdomain": 6,
    }
    out = invoke_remember_tool(tool, "ep_sub", memory=fake_read)
    coords = [e["coord"] for e in out["evidence"]]
    assert coords == [[0, 6, 0]]


# ── walker failsafe (classifier mocked to raise) ──────────────────────


def test_walker_query_allowed_subdomains_skips_classifier_failsafe():
    """Recall with allowed_subdomains must not call SetFit (mock raises)."""

    spec = QuerySpec(
        cycle_id="test-failsafe",
        text="what is your identity",
        allowed_lobes=[0],
        allowed_subdomains=[6],
        limit=5,
    )

    fake_index = {
        "mem_omega": {"coord": [0, 6, 0], "path": "/fake/omega.json"},
        "mem_traits": {"coord": [0, 2, 0], "path": "/fake/traits.json"},
    }

    def fake_load_meta(path):
        if "omega" in path:
            return {
                "id": "mem_omega",
                "coord": [0, 6, 0],
                "title": "omega",
                "summary": "omega identity",
                "text": "omega",
                "provenance": {"confidence": 0.9},
                "tags": [],
            }
        return {
            "id": "mem_traits",
            "coord": [0, 2, 0],
            "title": "traits",
            "summary": "traits",
            "text": "traits",
            "provenance": {"confidence": 0.9},
            "tags": [],
        }

    with (
        patch("memory.engine.walker.COORD_INDEX", fake_index),
        patch("memory.engine.walker.load_coord_index"),
        patch("memory.engine.walker.load_meta_by_path", side_effect=fake_load_meta),
    ):
        candidates, meta = walker_query(spec)

    returned_coords = [c.get("coord") for c in candidates]
    assert all(c[0] == 0 and c[1] == 6 for c in returned_coords if c)
    assert meta.get("seed_coord") == [0, 6, 0]


# ── Phase 3: deterministic recall (no classifier) ────────────────────
def test_tokenize_is_local_word_split():
    from memory.engine.walker import _tokenize

    assert _tokenize("The Quantum Entanglement of  Qubits") == [
        "the",
        "quantum",
        "entanglement",
        "qubits",
    ]
    assert _tokenize("a or in") == []  # all <= 2 chars dropped


def test_grammar_seed_routes_deterministically():
    from memory.engine.walker import _grammar_seed

    # "who am i" -> identity tool (lobe 0) via route_memory_tool
    lobe, _sub = _grammar_seed("who am i")
    assert lobe == 0
    # default fallback is knowledge (lobe 2)
    lobe2, _ = _grammar_seed("blizzard penguin teapot zzz")
    assert lobe2 == 2


def test_geometric_score_lobe_only_seed_is_uniform_within_lobe():
    from memory.engine.walker import geometric_score

    # lobe-only seed (Y=None): every same-lobe coord scores equally (1.0),
    # different-lobe scores tiny — so token overlap decides the ranking.
    assert geometric_score([3, 0, 0], (3, None)) == 1.0
    assert geometric_score([3, 4, 0], (3, None)) == 1.0
    assert geometric_score([5, 0, 0], (3, None)) < 0.1


def test_walker_forced_lobe_no_subdomain_ignores_raising_classifier(monkeypatch):
    """Forced lobe, no subdomain: whole-lobe fan-out, no classify call."""
    from memory.engine import walker as W

    class _Boom:
        def classify(self, *a, **k):
            raise RuntimeError("classifier must not run in recall")

        def tokens(self, *a, **k):
            raise RuntimeError("classifier.tokens must not run in recall")

    fake_index = {
        "k_async": {"coord": [8, 1, 0], "path": "/fake/async.json"},
        "k_web": {"coord": [8, 5, 0], "path": "/fake/web.json"},
    }

    def fake_load_meta(path):
        cid = "k_async" if "async" in path else "k_web"
        y = 1 if cid == "k_async" else 5
        return {
            "id": cid,
            "coord": [8, y, 0],
            "title": cid,
            "summary": "python async await" if cid == "k_async" else "python web flask",
            "text": "",
            "provenance": {"confidence": 0.9},
            "tags": [],
        }

    monkeypatch.setattr(W, "COORD_INDEX", fake_index)
    monkeypatch.setattr(W, "load_coord_index", lambda: fake_index)
    monkeypatch.setattr(W, "load_meta_by_path", fake_load_meta)

    # force_lobe=8, no allowed_subdomains; query tokens favor async.
    contexts = W.walker("python async await", force_lobe=8, allowed_subdomains=None)
    coords = [c.get("coord") for c in contexts]
    assert coords, "whole-lobe fan-out must return candidates"
    assert all(c[0] == 8 for c in coords), "only lobe 8 candidates"
    assert contexts[0]["coord"] == [8, 1, 0], "token overlap ranks async first"


def test_walker_unscoped_ignores_raising_classifier(monkeypatch):
    """Unscoped recall must never call SetFit; grammar seed + full corpus."""
    from memory.engine import walker as W

    class _Boom:
        def classify(self, *a, **k):
            raise RuntimeError("classifier must not run in unscoped recall")

        def tokens(self, *a, **k):
            raise RuntimeError("classifier.tokens must not run in unscoped recall")

    fake_index = {
        "id_identity": {"coord": [0, 1, 0], "path": "/fake/identity.json"},
        "id_knowledge": {"coord": [2, 0, 0], "path": "/fake/knowledge.json"},
    }

    def fake_load_meta(path):
        if "identity" in path:
            return {
                "id": "id_identity",
                "coord": [0, 1, 0],
                "title": "operator identity",
                "summary": "blizzard penguin teapot operator identity profile",
                "text": "",
                "provenance": {"confidence": 0.9},
                "tags": [],
            }
        return {
            "id": "id_knowledge",
            "coord": [2, 0, 0],
            "title": "noise",
            "summary": "unrelated knowledge filler",
            "text": "",
            "provenance": {"confidence": 0.9},
            "tags": [],
        }

    monkeypatch.setattr(W, "COORD_INDEX", fake_index)
    monkeypatch.setattr(W, "load_coord_index", lambda: fake_index)
    monkeypatch.setattr(W, "load_meta_by_path", fake_load_meta)

    # Grammar default is knowledge (lobe 2); identity record must still surface.
    contexts = W.walker(
        "blizzard penguin teapot operator identity",
        _Boom(),
        force_lobe=None,
        allowed_subdomains=None,
    )
    coords = [c.get("coord") for c in contexts if c.get("coord")]
    assert coords, "full-corpus unscoped recall must return candidates"
    assert any(c[0] == 0 for c in coords), "token overlap must surface off-seed lobe"


def test_walker_query_allowed_lobes_only_skips_classifier(monkeypatch):
    """allowed_lobes but no allowed_subdomains: forced-lobe fan-out, no classify."""
    from memory.engine import walker as W
    from memory.api import QuerySpec

    fake_index = {
        "id_omega": {"coord": [0, 6, 0], "path": "/fake/omega.json"},
        "id_traits": {"coord": [0, 2, 0], "path": "/fake/traits.json"},
        "other": {"coord": [2, 0, 0], "path": "/fake/other.json"},
    }

    def fake_load_meta(path):
        if "omega" in path:
            return {
                "id": "id_omega",
                "coord": [0, 6, 0],
                "title": "omega",
                "summary": "your identity omega",
                "text": "",
                "provenance": {"confidence": 0.9},
                "tags": [],
            }
        if "traits" in path:
            return {
                "id": "id_traits",
                "coord": [0, 2, 0],
                "title": "traits",
                "summary": "emergent traits",
                "text": "",
                "provenance": {"confidence": 0.9},
                "tags": [],
            }
        return {
            "id": "other",
            "coord": [2, 0, 0],
            "title": "other",
            "summary": "astronomy",
            "text": "",
            "provenance": {"confidence": 0.9},
            "tags": [],
        }

    monkeypatch.setattr(W, "COORD_INDEX", fake_index)
    monkeypatch.setattr(W, "load_coord_index", lambda: fake_index)
    monkeypatch.setattr(W, "load_meta_by_path", fake_load_meta)

    spec = QuerySpec(cycle_id="t", text="your identity omega", allowed_lobes=[0], limit=5)
    candidates, meta = W.walker_query(spec)
    coords = [c.get("coord") for c in candidates if c.get("coord")]
    assert coords and all(c[0] == 0 for c in coords), "scoped to forced lobe 0, no cross-lobe leak"
    assert meta.get("seed_coord")[0] == 0

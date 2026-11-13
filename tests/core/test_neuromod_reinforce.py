"""Phase 1 — dopamine RPE credit-assigns to the recalled engrams that drove a cycle.

``compute_episode_neuromod`` produces a dopamine RPE every cycle, but until now nothing fed it
back into memory. This wires the central designed link: a clearly-rewarding cycle STRENGTHENS the
write_strength of the memories the recall surfaced, a clearly-punished cycle WEAKENS them, each
scaled by its recall relevance so the memories that mattered most move most. Flag-gated by
OMEGA_DOPAMINE_REINFORCE (default OFF), failure-soft — a reinforce hiccup never breaks the cascade.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from autonomic import neuromod_config as nc
from autonomic.neuromod_reinforce import reinforce_recalled_memories


class _FakeStrengthIndex:
    """Records reinforce(mid, delta) calls so tests assert credit-assignment exactly."""

    def __init__(self):
        self.calls = []  # list of (mid, delta)

    def reinforce(self, mid, delta):
        self.calls.append((mid, delta))
        return 0.5  # arbitrary resulting write_strength


def _item(mid, relevance=1.0):
    return {
        "memory_id": mid,
        "source": "lobe:1",
        "timestamp": None,
        "relevance": relevance,
        "content": "x",
        "tool": "remember_journal",
    }


def _episode(items):
    return SimpleNamespace(
        episode_id="ep-reinforce",
        auto={
            "cycle_kind": "success",
            "memory": {"memory_status": "success", "items": items},
        },
        meta=None,
    )


# ----- logic ---------------------------------------------------------------


def test_flag_off_no_reinforce(monkeypatch):
    monkeypatch.delenv("OMEGA_DOPAMINE_REINFORCE", raising=False)
    si = _FakeStrengthIndex()
    reinforce_recalled_memories(_episode([_item("m1")]), {"dopamine": 1.0}, strength_index=si)
    assert si.calls == []


def test_strong_positive_dopamine_strengthens_recalled_memories(monkeypatch):
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    si = _FakeStrengthIndex()
    reinforce_recalled_memories(
        _episode([_item("m1"), _item("m2")]), {"dopamine": 1.0}, strength_index=si
    )
    assert {mid for mid, _ in si.calls} == {"m1", "m2"}
    assert all(delta > 0 for _, delta in si.calls)


def test_strong_negative_dopamine_weakens_recalled_memories(monkeypatch):
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    si = _FakeStrengthIndex()
    reinforce_recalled_memories(_episode([_item("m1")]), {"dopamine": -1.0}, strength_index=si)
    assert len(si.calls) == 1
    assert si.calls[0][0] == "m1"
    assert si.calls[0][1] < 0


def test_neutral_dopamine_leaves_engrams_untouched(monkeypatch):
    # Below |thresholds| the RPE is "predicted" — no surprise, so no credit reassignment.
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    pos, _neg = nc.dopamine_thresholds()
    si = _FakeStrengthIndex()
    reinforce_recalled_memories(_episode([_item("m1")]), {"dopamine": pos * 0.5}, strength_index=si)
    assert si.calls == []


def test_credit_scaled_by_relevance(monkeypatch):
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    si = _FakeStrengthIndex()
    reinforce_recalled_memories(
        _episode([_item("hi", relevance=1.0), _item("lo", relevance=0.5)]),
        {"dopamine": 1.0},
        strength_index=si,
    )
    deltas = dict(si.calls)
    assert deltas["hi"] > deltas["lo"] > 0


def test_exact_delta_is_dopamine_times_relevance_times_scale(monkeypatch):
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    si = _FakeStrengthIndex()
    reinforce_recalled_memories(
        _episode([_item("m1", relevance=1.0)]), {"dopamine": 1.0}, strength_index=si
    )
    expected = 1.0 * 1.0 * nc.dopamine_reinforce_scale()
    assert si.calls[0][1] == pytest.approx(expected)


def test_skips_missing_memory_ids(monkeypatch):
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    si = _FakeStrengthIndex()
    reinforce_recalled_memories(
        _episode([_item(None), _item(""), _item("good")]),
        {"dopamine": 1.0},
        strength_index=si,
    )
    assert [mid for mid, _ in si.calls] == ["good"]


def test_missing_relevance_defaults_to_full_participation(monkeypatch):
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    si = _FakeStrengthIndex()
    item = _item("m1")
    item["relevance"] = None  # remember tool returned no confidence
    reinforce_recalled_memories(_episode([item]), {"dopamine": 1.0}, strength_index=si)
    expected = 1.0 * 1.0 * nc.dopamine_reinforce_scale()
    assert si.calls[0][1] == pytest.approx(expected)


def test_no_recalled_items_is_a_noop(monkeypatch):
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    si = _FakeStrengthIndex()
    out = reinforce_recalled_memories(_episode([]), {"dopamine": 1.0}, strength_index=si)
    assert si.calls == []
    assert out["reinforced"] == 0


def test_failure_soft_when_no_strength_index(monkeypatch):
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    monkeypatch.setattr("memory.strength_index.get_strength_index", lambda: None)
    out = reinforce_recalled_memories(_episode([_item("m1")]), {"dopamine": 1.0})
    assert out["reinforced"] == 0


def test_failure_soft_on_reinforce_exception(monkeypatch):
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")

    class _Boom:
        def reinforce(self, mid, delta):
            raise RuntimeError("strength index exploded")

    out = reinforce_recalled_memories(
        _episode([_item("m1")]), {"dopamine": 1.0}, strength_index=_Boom()
    )
    assert out["reinforced"] == 0


# ----- wiring through the live cascade seam --------------------------------


def _wire_start_auto(monkeypatch, si, *, baseline=0.0):
    """Shared setup: stub AUTO to set a recalled-memory outcome, route the strength
    index, disable the downstream slice. Mirrors test_salience_latch_live."""
    import cognition_chain as cl
    from pressure_loop.episode import EpisodeStore

    monkeypatch.setattr("memory.strength_index.get_strength_index", lambda: si)

    def _auto(ep):
        ep.auto = {
            "cycle_kind": "success",
            "memory": {"memory_status": "success", "items": [_item("m1")]},
        }
        return ep.auto

    monkeypatch.setattr("auto_loop.auto.run_auto_front", _auto)
    monkeypatch.setattr(cl, "_start_meta_b_global_b", lambda *_a, **_k: None)
    monkeypatch.setattr(EpisodeStore, "recent_outcome_population",
                        lambda self, n=10: _measured(baseline))
    return cl, EpisodeStore


def test_start_auto_invokes_reinforce_when_enabled(monkeypatch):
    """The wiring guard: the central link must actually fire from the cascade, not just
    exist as an importable function (the 'built but never called' failure mode)."""
    monkeypatch.setenv("OMEGA_DOPAMINE_REINFORCE", "1")
    si = _FakeStrengthIndex()
    cl, EpisodeStore = _wire_start_auto(monkeypatch, si)

    episode = SimpleNamespace(
        episode_id="ep-wire", pipeline_id=0, work_kind="autonomous", auto=None, meta=None
    )
    cl._start_auto(EpisodeStore(), episode)

    assert [mid for mid, _ in si.calls] == ["m1"]
    assert si.calls[0][1] > 0  # success at baseline 0 -> positive RPE -> strengthen


def test_start_auto_no_reinforce_when_disabled(monkeypatch):
    monkeypatch.delenv("OMEGA_DOPAMINE_REINFORCE", raising=False)
    si = _FakeStrengthIndex()
    cl, EpisodeStore = _wire_start_auto(monkeypatch, si)

    episode = SimpleNamespace(
        episode_id="ep-wire2", pipeline_id=0, work_kind="autonomous", auto=None, meta=None
    )
    cl._start_auto(EpisodeStore(), episode)

    assert si.calls == []


def _measured(rate):
    """A recent-outcome population whose measured task success rate is ``rate`` (WO-06)."""
    from pressure_loop.episode import OutcomePopulation

    successes = round(rate * 10)
    return OutcomePopulation(total=10, task_successes=successes, task_failures=10 - successes)

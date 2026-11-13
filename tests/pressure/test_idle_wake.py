"""Idle-wake governor — rest is the default; autonomy is the wake.

Operator design 2026-07-08 (the final autonomy piece): DeepSleep must
actually SLEEP before the process starts again. The idle countdown runs
only when NOTHING is active (no episode and no operator
interaction); operator input resets it; on expiry the mind receives a
self-initiated prompt to run Omega — review knowledge vs application
mastery and autonomously decide what to learn or apply. That decision
enters pressure exactly like Omega's own PRESSURE: one validated successor
(plan Task 13). While resting, autonomous ignition is suppressed; operator
input always passes instantly.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pressure_loop.idle_wake as iw  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    import core.paths as paths
    from pressure_loop import pressure

    monkeypatch.setattr(paths, "GUARDIAN_STATE_ROOT", tmp_path)
    monkeypatch.setattr(pressure, "_RESEED_STAGING", {})
    iw._reset_for_tests()
    monkeypatch.setenv("OMEGA_IDLE_WAKE_ENABLED", "1")
    yield
    iw._reset_for_tests()


def _staged():
    from pressure_loop import pressure

    return [c for c in pressure.staged_signals() if c.type == "omega_successor"]


def _control_pressure(row):
    return row.metadata["omega_control"]["pressure"]


def test_fresh_activity_means_resting():
    iw.record_activity("operator")
    assert iw.is_resting(now=iw._last_activity_ts + 10) is True


def test_expired_idle_means_wake_due():
    iw.record_activity("operator")
    t = iw._last_activity_ts + iw.idle_wake_seconds() + 1
    assert iw.is_resting(now=t) is False
    assert iw.wake_due(now=t) is True


def test_activity_resets_the_countdown():
    iw.record_activity("operator")
    t1 = iw._last_activity_ts + iw.idle_wake_seconds() - 5
    assert iw.wake_due(now=t1) is False
    iw.record_activity("operator", now=t1)  # reset near the end
    t2 = t1 + 10
    assert iw.wake_due(now=t2) is False, "reset must restart the countdown"


def _stub_awaken(
    monkeypatch,
    out='{"reasoning":"gap","action":"LEARN","domain":"caching","topic":"cache eviction policies","level":1.0}',
):
    """Wire a fake mastery store + model output."""
    from deepsleep_loop._deepsleep_state import MasteryRecord

    recs = {
        "caching": MasteryRecord(
            domain_id="caching",
            domain_name="caching",
            knowledge_mastery=0.1,
            application_mastery=0.0,
            target_mastery=0.7,
        )
    }
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", lambda *a, **k: recs)

    class _Client:
        def generate(self, *a, **k):
            return out

    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: _Client())


def test_awakening_stages_one_validated_learn_successor(monkeypatch):
    from pressure_loop import learn_topic_choice

    learn_topic_choice._reset_for_tests()
    _stub_awaken(monkeypatch)
    iw.record_activity("operator")
    t = iw._last_activity_ts + iw.idle_wake_seconds() + 1
    assert iw.fire_awakening(now=t) is True

    (row,) = _staged()
    assert (row.source, row.metadata["directive_intent"], row.metadata["omega_lane"]) == ("omega", "LEARN", "agent")
    assert row.metadata["original_cycle_id"] == ""  # no operator lineage: retired if idle learning pauses
    pressure = _control_pressure(row)
    assert (pressure["domain"], pressure["topic"]) == ("caching", "cache eviction policies")
    assert pressure["setpoints"] == {"mastery_debt_delta": 1.0}
    # Omega's SPECIFIC chosen topic is stashed for the LEARN it ignites
    assert learn_topic_choice.take_chosen_topic() == {"domain": "caching", "topic": "cache eviction policies"}
    # Operator 2026-07-09: firing does NOT reset the idle clock (only user input
    # does) — the woken mind stays free to reignite continually. The model KICK is
    # throttled instead, so it can't machine-gun while the mind is persistently idle.
    assert iw.wake_due(now=t + 1) is True  # timer NOT reset by firing
    assert iw.wake_attempt_allowed(now=t + 1) is False  # kick throttled one window
    assert iw.wake_attempt_allowed(now=t + iw.idle_wake_seconds() + 1) is True


def _make_apply_ready(monkeypatch, *, knowledge=0.6):
    """Upgrade the stubbed 'caching' record to apply-ready: part-learned past the
    mind-driven bar, unapplied, own-warm substrate."""
    from deepsleep_loop._deepsleep_state import MasteryRecord

    recs = {
        "caching": MasteryRecord(
            domain_id="caching",
            domain_name="caching",
            knowledge_mastery=knowledge,
            application_mastery=0.0,
            target_mastery=0.7,
        )
    }
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", lambda *a, **k: recs)
    monkeypatch.setattr("memory.symphony_strength.hot_count_for_domain", lambda *_a: 999)


def test_awakening_execute_on_ready_domain_stages_a_use_skill_successor(monkeypatch):
    import pressure_loop.practice_mint as pm

    _stub_awaken(
        monkeypatch,
        out='{"reasoning":"learned not built","action":"EXECUTE","domain":"caching",'
        '"topic":"cache eviction policies","level":1.0}',
    )
    _make_apply_ready(monkeypatch)
    pm._pending_apply = None
    iw.record_activity("operator")
    t = iw._last_activity_ts + iw.idle_wake_seconds() + 1
    assert iw.fire_awakening(now=t) is True
    (row,) = _staged()
    assert row.metadata["directive_intent"] == "USE_SKILL"
    assert _control_pressure(row)["setpoints"] == {"execution_debt_delta": 1.0}
    # the mind's specific EXECUTE choice is stashed for the mint to back
    assert pm.take_apply_choice() == {"domain": "caching", "topic": "cache eviction policies"}


def test_awakening_execute_below_bar_downgrades_to_learn(monkeypatch):
    """Live root-cause #2 (2026-07-10): EXECUTE on an under-bar domain used to
    stage work USE_SKILL could never back (mint declines below
    MIND_DRIVEN_KNOWLEDGE_BAR) -> use_skill_undispatchable -> IDLE every window.
    It must DOWNGRADE to LEARN on the same domain+topic instead."""
    from pressure_loop import learn_topic_choice

    learn_topic_choice._reset_for_tests()
    _stub_awaken(
        monkeypatch,
        out='{"reasoning":"learned not built","action":"EXECUTE","domain":"caching",'
        '"topic":"cache eviction policies","level":1.0}',
    )
    # stub record has knowledge=0.1 < 0.5 bar -> not apply-ready
    iw.record_activity("operator")
    t = iw._last_activity_ts + iw.idle_wake_seconds() + 1
    assert iw.fire_awakening(now=t) is True
    (row,) = _staged()
    assert row.metadata["directive_intent"] == "LEARN"
    assert _control_pressure(row)["setpoints"] == {"mastery_debt_delta": 1.0}
    # the specific topic follows the downgrade into the LEARN resolver
    assert learn_topic_choice.take_chosen_topic() == {
        "domain": "caching",
        "topic": "cache eviction policies",
    }


def test_awakening_domain_strips_category_suffix(monkeypatch):
    """Live 2026-07-10: the model copied the whole map row label — domain +
    '[category]' — producing a phantom slugged domain id that fragments
    mastery crediting. The parser strips the trailing [category]."""
    from knowledge_extractor.derive.run import _slug

    _stub_awaken(
        monkeypatch,
        out='{"reasoning":"gap","action":"LEARN",'
        '"domain":"Data Structures in Python (Lists, Dict) [python]",'
        '"topic":"time complexity of dict operations","level":1.0}',
    )
    iw.record_activity("operator")
    t = iw._last_activity_ts + iw.idle_wake_seconds() + 1
    assert iw.fire_awakening(now=t) is True
    (row,) = _staged()
    assert _control_pressure(row)["domain"] == _slug("Data Structures in Python (Lists, Dict)")
    assert not _control_pressure(row)["domain"].endswith("-python")


def test_awakening_empty_map_rests(monkeypatch):
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", lambda *a, **k: {})
    iw.record_activity("operator")
    t = iw._last_activity_ts + iw.idle_wake_seconds() + 1
    assert iw.fire_awakening(now=t) is False  # cold start: nothing to learn yet
    assert _staged() == []


def test_awakening_empty_generate_falls_back_to_nothink(monkeypatch):
    """Live root-cause 2026-07-10: thinking ate the whole token budget -> the
    runtime returned ok=true with EMPTY text -> the wake silently rested every
    window. The fallback retries with thinking OFF and must still fire."""
    _stub_awaken(monkeypatch)

    calls = []

    class _FlakyClient:
        def generate(self, *a, **k):
            calls.append(dict(k))
            if len(calls) == 1:
                return ""  # think-runaway: all tokens burned inside <think>
            return (
                '{"reasoning":"gap","action":"LEARN","domain":"caching",'
                '"topic":"cache eviction policies","level":1.0}'
            )

    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: _FlakyClient())
    iw.record_activity("operator")
    t = iw._last_activity_ts + iw.idle_wake_seconds() + 1
    assert iw.fire_awakening(now=t) is True
    (row,) = _staged()
    assert row.metadata["directive_intent"] == "LEARN"
    assert len(calls) == 2
    assert calls[0].get("enable_thinking") is True  # primary: deliberation
    assert calls[1].get("enable_thinking") is False  # fallback: structured mode


def test_awakening_no_decision_is_loud_not_silent(monkeypatch):
    """A wake that decides NOTHING must emit failure telemetry — the silent rest
    is what hid the empty-generate bug live."""
    _stub_awaken(monkeypatch)

    class _DeadClient:
        def generate(self, *a, **k):
            return ""

    monkeypatch.setattr("runtime.client.get_runtime_client", lambda: _DeadClient())

    emitted = []
    monkeypatch.setattr(
        "guardian.terminal_feedback.emit_data",
        lambda *a, **k: emitted.append((a, k)),
    )
    iw.record_activity("operator")
    t = iw._last_activity_ts + iw.idle_wake_seconds() + 1
    assert iw.fire_awakening(now=t) is False
    assert _staged() == []
    fails = [
        (a, k)
        for (a, k) in emitted
        if len(a) >= 2 and a[1] == "idle_awakening" and k.get("ok") is False
    ]
    assert fails, "no-decision wake must emit idle_awakening status=fail"


def test_no_awakening_while_resting(monkeypatch):
    _stub_awaken(monkeypatch)
    iw.record_activity("operator")
    assert iw.fire_awakening(now=iw._last_activity_ts + 10) is False


def test_connector_contract_does_not_own_the_idle_governor_window():
    assert iw.idle_wake_seconds() == iw.DEFAULT_IDLE_WAKE_SECONDS


def test_firing_does_not_reset_clock_so_mind_can_reignite(monkeypatch):
    """The point of the redesign (operator 2026-07-09): once woken, the mind stays 'due'
    and free to keep reigniting on its own — ONLY user input resets the idle clock."""
    _stub_awaken(monkeypatch)
    iw.record_activity("operator")
    base = iw._last_activity_ts
    t = base + iw.idle_wake_seconds() + 1
    assert iw.fire_awakening(now=t) is True
    # Long after the first kick, still no user input -> STILL due, and (after the
    # throttle window) free to kick again: the mind can run itself indefinitely.
    later = t + 10 * iw.idle_wake_seconds()
    assert iw.wake_due(now=later) is True
    assert iw.wake_attempt_allowed(now=later) is True
    # A user turn is the ONE thing that resets it -> not due until the window elapses.
    iw.record_activity("operator", now=later)
    assert iw.wake_due(now=later + 5) is False


def test_seconds_until_wake_attempt_counts_down():
    iw.record_activity("operator")
    base = iw._last_activity_ts
    rem = iw.seconds_until_wake_attempt(now=base + iw.idle_wake_seconds() / 2)
    assert 0 < rem <= iw.idle_wake_seconds()
    # past the window, no wake fired yet -> fire now (0)
    assert iw.seconds_until_wake_attempt(now=base + iw.idle_wake_seconds() + 1) == 0.0

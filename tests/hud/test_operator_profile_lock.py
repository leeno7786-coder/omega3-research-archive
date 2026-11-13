"""Onboarding is one-shot: once an operator profile exists it is LOCKED, so a re-save
(a double-click, an API replay) can't overwrite the profile OR re-run the subtopic
decomposition. Releasing the lock = a day-zero reset (wipes the profile)."""

from __future__ import annotations

import threading

import pytest

import hud.operator_identity as oi
import hud.settings as hud_settings


@pytest.fixture(autouse=True)
def _allow_profile_ingress(monkeypatch):
    monkeypatch.setattr(
        hud_settings,
        "_gate_operator_profile_ingress",
        lambda payload: {
            "decision": "allow",
            "payload": dict(payload),
            "citation": {"source_id": "src-test", "source_type": "operator_onboarding"},
            "guardian": {"screened_fields": 1, "decisions": {}},
        },
        raising=False,
    )


def test_is_onboarded_reflects_profile_presence(monkeypatch):
    monkeypatch.setattr(oi, "operator_context", lambda: {})
    assert oi.is_onboarded() is False

    monkeypatch.setattr(oi, "operator_context", lambda: {"name": "Ada Lovelace"})
    assert oi.is_onboarded() is True

    # a context with no name is NOT onboarded (a profile must have a name to lock).
    monkeypatch.setattr(oi, "operator_context", lambda: {"name": "", "relationship": "creator"})
    assert oi.is_onboarded() is False


def test_save_rejected_and_nothing_runs_when_onboarded(monkeypatch):
    monkeypatch.setattr(oi, "is_onboarded", lambda: True)
    queued = []
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.enqueue_input",
        lambda contributor, **kwargs: queued.append(contributor) or True,
    )

    result = hud_settings.save_operator_profile(
        {"name": "Imposter", "occupation": "software_engineer"}
    )

    assert result["ok"] is False
    assert result["locked"] is True
    assert not queued


def test_save_proceeds_when_not_onboarded(monkeypatch):
    monkeypatch.setattr(oi, "is_onboarded", lambda: False)
    captured = []
    monkeypatch.setattr(
        "pressure_loop.cognition_queue.enqueue_input",
        lambda contributor, **kwargs: captured.append(contributor) or True,
    )

    result = hud_settings.save_operator_profile(
        {"name": "Ada Lovelace", "occupation": "software_engineer"}
    )

    assert result["ok"] is True
    assert result.get("locked") is not True
    assert len(captured) == 1
    assert result["memory"]["status"] == "queued"


def test_save_holds_lock_during_queue_persistence(monkeypatch):
    """The one-shot guarantee must be a real mutex, not just a check: the
    is_onboarded() test AND the durable write must run inside
    _OPERATOR_PROFILE_LOCK so a double-submit can't interleave two writes."""
    monkeypatch.setattr(oi, "is_onboarded", lambda: False)
    observed = {}

    def fake_enqueue(_contributor, **kwargs):
        # The lock MUST be held at the moment of durable queue persistence. Probe from
        # ANOTHER thread (RLock is re-entrant on the owning thread, so a
        # same-thread acquire would spuriously succeed): a non-blocking acquire
        # fails iff the lock is currently held by the saving thread.
        probe = {}

        def _probe():
            acquired = hud_settings._OPERATOR_PROFILE_LOCK.acquire(blocking=False)
            probe["acquired"] = acquired
            if acquired:
                hud_settings._OPERATOR_PROFILE_LOCK.release()

        thread = threading.Thread(target=_probe)
        thread.start()
        thread.join(timeout=5)
        observed["locked_during_write"] = probe.get("acquired") is False
        return True

    monkeypatch.setattr("pressure_loop.cognition_queue.enqueue_input", fake_enqueue)

    result = hud_settings.save_operator_profile({"name": "Noah", "occupation": "x"})

    assert result["ok"] is True
    assert observed.get("locked_during_write") is True


def test_concurrent_double_submit_queues_once(monkeypatch):
    """A genuine TOCTOU defense: two simultaneous saves (double-click / API
    replay) must serialize on the lock so the SECOND observes the first's
    persisted profile and is rejected — exactly one durable write occurs."""
    onboarded = {"value": False}
    monkeypatch.setattr(oi, "is_onboarded", lambda: onboarded["value"])
    entered = threading.Event()
    release = threading.Event()
    queued = []

    def fake_enqueue(contributor, **kwargs):
        queued.append(contributor)
        entered.set()  # signal: first save is inside the critical section
        release.wait(timeout=5)
        onboarded["value"] = True  # the profile is now persisted
        return True

    monkeypatch.setattr("pressure_loop.cognition_queue.enqueue_input", fake_enqueue)

    results = {}

    def _save(tag):
        results[tag] = hud_settings.save_operator_profile({"name": "Noah", "occupation": "x"})

    first = threading.Thread(target=_save, args=("first",))
    first.start()
    assert entered.wait(timeout=5), "first save never reached the write"

    # First save holds the lock inside fake_write. The second must block on it.
    second = threading.Thread(target=_save, args=("second",))
    second.start()
    second.join(timeout=0.3)
    assert second.is_alive(), (
        "second save entered the critical section while the first held the lock"
    )

    release.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert len(queued) == 1, f"expected exactly one durable enqueue, got {len(queued)}"
    assert results["first"]["ok"] is True
    assert results["second"].get("locked") is True  # rejected: already onboarded

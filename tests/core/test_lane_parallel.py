"""Physical worker contention, lane quarantine, and fatal relay contracts."""
import threading
from types import SimpleNamespace

import pytest

from pressure_loop import pipeline_dispatch as dispatch


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    dispatch.shutdown_for_tests()
    monkeypatch.setattr(dispatch, "mark_ready", lambda *_: None)
    monkeypatch.setattr(dispatch, "mark_failed", lambda *_: None)
    yield
    dispatch.shutdown_for_tests()


def _send(pid, release=lambda: None):
    # A real contributor carries metadata: the failure path releases this turn's durable
    # continuation from it, and a stand-in without it would fail there instead of where
    # the test is looking.
    from core.pipelines import lane_for_pipeline

    contributor = SimpleNamespace(metadata={"lane": lane_for_pipeline(pid)})
    ignition = SimpleNamespace(pipeline_id=pid, work_kind="operator_turn", dominant_contributor=contributor)
    dispatch.dispatch_cascade(None, ignition, contributor, pipeline_id=pid,
                              work_kind="operator_turn", on_not_admitted=release)


def test_second_dispatch_cannot_overwrite_busy_pipeline(monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def ignite(*_):
        entered.set()
        assert release.wait(3)
        return object()
    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    try:
        _send(0)
        assert entered.wait(2)
        with pytest.raises(RuntimeError, match="unavailable"):
            _send(0)
        assert dispatch.free_pipeline_ids() == (1, 2, 3)
    finally:
        release.set()


def test_worker_failure_reaches_pressure_and_releases_before_wake(monkeypatch):
    wake = threading.Event()
    failure = RuntimeError("cascade fatal")
    calls = []
    def ignite(*_):
        raise failure
    monkeypatch.setattr("cognition_chain.ignite_episode", ignite)
    monkeypatch.setattr(dispatch, "mark_failed", lambda pid, exc: calls.append((pid, exc)))
    monkeypatch.setattr(dispatch, "notify_work", wake.set)
    _send(3, lambda: calls.append("release"))
    assert wake.wait(2)
    assert calls == [(3, failure), "release"]
    # The failure still reaches the FAILED-envelope writer and remains readable through
    # the fault map, but it closes ONE pipeline, not the process.
    assert dispatch.pipeline_faults() == {3: failure}
    dispatch.raise_pending_fatal()
    dispatch.raise_pending_fatal()
    assert dispatch.free_pipeline_ids() == (0, 1, 2)

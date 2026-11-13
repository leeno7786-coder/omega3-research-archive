"""The retired GPU gate remains inert diagnostic bookkeeping."""

from __future__ import annotations

import threading
import time

import pytest

from runtime import gpu_lease


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "1")
    monkeypatch.setenv("OMEGA_LANE_PARALLEL", "0")
    gpu_lease._reset_for_tests()
    yield
    gpu_lease._reset_for_tests()


def test_default_is_unheld():
    assert gpu_lease.gpu_held_by_build() is False
    assert gpu_lease.current_holder() is None


def test_acquire_then_release():
    gpu_lease.acquire_gpu_for_build("build")
    assert gpu_lease.gpu_held_by_build() is False
    assert gpu_lease.current_holder() == "build"
    gpu_lease.release_gpu_for_build()
    assert gpu_lease.gpu_held_by_build() is False
    assert gpu_lease.current_holder() is None


def test_release_is_idempotent():
    gpu_lease.release_gpu_for_build()  # no holder -> harmless
    assert gpu_lease.gpu_held_by_build() is False


def test_holder_label_defaults_to_build():
    gpu_lease.acquire_gpu_for_build()
    assert gpu_lease.current_holder() == "build"


def test_flag_off_means_never_held(monkeypatch):
    # Flag OFF is the escape hatch — the lease records the holder but the query reports
    # unheld, so generate() never defers (original behavior restored).
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "0")
    gpu_lease.acquire_gpu_for_build("build")
    assert gpu_lease.gpu_held_by_build() is False


def test_reentrant_acquire_needs_matching_releases():
    # Nested historical call sites still balance their diagnostic counters.
    gpu_lease.acquire_gpu_for_build("build")
    gpu_lease.acquire_gpu_for_build("browser_agent")
    assert gpu_lease.gpu_held_by_build() is False
    assert gpu_lease.current_holder() == "browser_agent"
    gpu_lease.release_gpu_for_build()
    assert gpu_lease.gpu_held_by_build() is False
    assert gpu_lease.current_holder() == "browser_agent"
    gpu_lease.release_gpu_for_build()
    assert gpu_lease.gpu_held_by_build() is False


def test_thread_visibility():
    # Diagnostic holder state is process-global, while admission is never gated.
    gpu_lease.acquire_gpu_for_build("build")
    seen = {}

    def _check():
        seen["held"] = gpu_lease.gpu_held_by_build()
        seen["holder"] = gpu_lease.current_holder()

    t = threading.Thread(target=_check)
    t.start()
    t.join()
    assert seen == {"held": False, "holder": "build"}


def test_wait_returns_true_immediately_when_free():
    # Nothing holds the GPU -> a metacog call proceeds at once.
    assert gpu_lease.wait_for_gpu(timeout=0.05) is True


def test_wait_is_immediate_while_diagnostic_holder_exists():
    gpu_lease.acquire_gpu_for_build("build")
    started = time.monotonic()
    assert gpu_lease.wait_for_gpu(timeout=5.0) is True
    assert time.monotonic() - started < 0.5


def test_wait_does_not_depend_on_release():
    gpu_lease.acquire_gpu_for_build("build")

    def _release_soon():
        import time

        time.sleep(0.1)
        gpu_lease.release_gpu_for_build()

    t = threading.Thread(target=_release_soon)
    t.start()
    started = time.monotonic()
    assert gpu_lease.wait_for_gpu(timeout=5.0) is True
    assert time.monotonic() - started < 0.5
    t.join()


def test_wait_is_noop_when_flag_off(monkeypatch):
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "0")
    gpu_lease.acquire_gpu_for_build("build")
    assert gpu_lease.wait_for_gpu(timeout=0.05) is True


# ── Phase 4: the lease is inert while the lanes run concurrently ──────────────
# Operator 2026-08-11 ("i dont think the concurrent prediction lanes are working
# yet"): three chat turns sent during a build were answered by the canned
# build-busy ack instead of by cognition. The cause was this lease — written for
# a world where gemma and jackrong could not share the one GPU seat. Phase 4
# retired that world: one resident model serves all four lanes on four slots of
# one unified KV pool, so a build and a chat run the SAME weights and there is
# nothing to evict.


def test_the_lease_is_inert_while_lanes_run_concurrently(monkeypatch):
    """A build may hold it, but nothing is gated: the lanes are the concurrency."""
    monkeypatch.setenv("OMEGA_LANE_PARALLEL", "1")
    gpu_lease.acquire_gpu_for_build("agent-build")

    assert gpu_lease.gpu_held_by_build() is False  # nothing to guard against
    assert gpu_lease.wait_for_gpu(timeout=0.0) is True  # no generate ever waits


def test_a_chat_generate_never_waits_on_a_build_under_lane_parallelism(monkeypatch):
    """The regression the operator hit: wait_for_gpu must not block the chat lane.

    Under the serial switch the same call blocks (proven below), so this is the
    lane behaviour specifically, not the flag being globally off.
    """
    monkeypatch.setenv("OMEGA_LANE_PARALLEL", "1")
    gpu_lease.acquire_gpu_for_build("agent-build")

    started = time.monotonic()
    assert gpu_lease.wait_for_gpu(timeout=5.0) is True
    assert time.monotonic() - started < 0.5  # returned immediately, did not queue


def test_the_retired_serial_switch_cannot_restore_gating(monkeypatch):
    """Historical environment switches cannot become a second scheduler."""
    monkeypatch.setenv("OMEGA_LANE_PARALLEL", "0")
    gpu_lease.acquire_gpu_for_build("build")

    assert gpu_lease.gpu_held_by_build() is False
    assert gpu_lease.wait_for_gpu(timeout=0.05) is True

    gpu_lease.release_gpu_for_build()
    assert gpu_lease.wait_for_gpu(timeout=0.0) is True


def test_the_operator_switch_still_wins_over_the_lane_switch(monkeypatch):
    """OMEGA_GPU_BUILD_LEASE=0 disables gating in either world."""
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "0")
    monkeypatch.setenv("OMEGA_LANE_PARALLEL", "0")
    gpu_lease.acquire_gpu_for_build("build")

    assert gpu_lease.gpu_held_by_build() is False

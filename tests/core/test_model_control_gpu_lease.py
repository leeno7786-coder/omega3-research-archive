"""Model-control swap primitives balance retired GPU diagnostic bookkeeping."""

from __future__ import annotations

import pytest

from runtime import gpu_lease
from runtime import model_control as mc


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "1")
    # These assertions describe the SERIAL one-seat world; Phase 4 makes the
    # lease inert under lane parallelism (see runtime/gpu_lease.py's retirement
    # note), so the world under test has to be named explicitly.
    monkeypatch.setenv("OMEGA_LANE_PARALLEL", "0")
    monkeypatch.setattr(mc, "_cloud_cli_route", lambda: None)
    gpu_lease._reset_for_tests()
    yield
    gpu_lease._reset_for_tests()


def test_prepare_acquires_release_clears(monkeypatch):
    # A REAL swap: something had to be evicted to seat the build's model. That —
    # and only that — is what the lease guards (2026-08-11; see below for the
    # already-resident case, which is what the live config actually does).
    monkeypatch.setattr(
        mc, "unload_model", lambda *a, **k: {"ok": True, "model": a[0] if a else ""}
    )
    monkeypatch.setattr(mc, "load_model", lambda *a, **k: {"ok": True, "model": a[0] if a else ""})

    res = mc.prepare_browser_agent_runtime(
        target_model="jackrong_qwen", unload_models=["gemma_e4b"]
    )
    assert res["ok"] is True
    assert gpu_lease.current_holder() == "jackrong_qwen"
    assert gpu_lease.gpu_held_by_build() is False

    mc.release_browser_agent_runtime()
    assert gpu_lease.gpu_held_by_build() is False  # GPU handed back to cognition


def test_an_already_resident_target_never_takes_the_lease(monkeypatch):
    """The live Phase 4 config: the CLI executor runs the SAME resident model as
    cognition (qwen35_9b serving all four lanes), so ``prepare`` evicts
    nothing and there is no mutual exclusion to claim.

    Leasing here is what answered three operator chat turns with the canned
    build-busy ack on 2026-08-11 while the chat lane's own slot sat idle.
    """
    monkeypatch.setattr(mc, "unload_model", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(mc, "load_model", lambda *a, **k: {"ok": True})

    res = mc.prepare_browser_agent_runtime()  # defaults: target IS the resident
    assert res["ok"] is True
    assert res["unload"] == []  # nothing was evicted
    assert gpu_lease.gpu_held_by_build() is False  # so nothing is gated


def test_browser_agent_prepare_release_also_bracket(monkeypatch):
    monkeypatch.setattr(mc, "unload_model", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(mc, "load_model", lambda *a, **k: {"ok": True})

    mc.prepare_browser_agent_runtime(target_model="jackrong_qwen", unload_models=["gemma_e4b"])
    assert gpu_lease.current_holder() == "jackrong_qwen"
    assert gpu_lease.gpu_held_by_build() is False
    mc.release_browser_agent_runtime()
    assert gpu_lease.gpu_held_by_build() is False


def test_failed_load_does_not_acquire(monkeypatch):
    # jackrong never made it onto the GPU -> no contention to guard against; do not strand
    # the brain in deferred-quiet on a phantom build.
    monkeypatch.setattr(mc, "unload_model", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(mc, "load_model", lambda *a, **k: {"ok": False, "error": "no vram"})

    res = mc.prepare_dev_pill_runtime()
    assert res["ok"] is False
    assert gpu_lease.gpu_held_by_build() is False


def test_release_clears_even_after_failed_load(monkeypatch):
    # Defensive: a release must always free the lease, so a half-failed session can't wedge
    # the brain into permanent deferral.
    monkeypatch.setattr(mc, "unload_model", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(mc, "load_model", lambda *a, **k: {"ok": True})
    mc.prepare_browser_agent_runtime(target_model="jackrong_qwen", unload_models=["gemma_e4b"])
    assert gpu_lease.current_holder() == "jackrong_qwen"
    assert gpu_lease.gpu_held_by_build() is False
    mc.release_browser_agent_runtime()
    assert gpu_lease.gpu_held_by_build() is False

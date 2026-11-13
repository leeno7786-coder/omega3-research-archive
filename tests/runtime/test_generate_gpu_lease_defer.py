"""RuntimeClient.generate* is never gated by retired GPU bookkeeping."""

from __future__ import annotations

import threading
import time

import pytest

from runtime import gpu_lease
from runtime.client import RuntimeClient


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "1")
    # Serial one-seat world: under lane parallelism the lease is inert by design
    # (Phase 4 — one resident model, four slots, nothing to evict), so a generate
    # correctly does NOT defer there. See runtime/gpu_lease.py.
    monkeypatch.setenv("OMEGA_LANE_PARALLEL", "0")
    monkeypatch.setenv("OMEGA_GPU_WAIT_S", "5")  # generous cap; threaded tests release fast
    gpu_lease._reset_for_tests()
    yield
    gpu_lease._reset_for_tests()


def _client(monkeypatch):
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    posts: list = []

    def _fake_post(path, json_body, *, op, timeout=None, **kw):
        posts.append((path, op))
        return {"ok": True, "text": "RESULT", "tool_calls": None}

    monkeypatch.setattr(client, "_post", _fake_post)
    return client, posts


def test_generate_waits_then_runs_after_release(monkeypatch):
    client, posts = _client(monkeypatch)
    gpu_lease.acquire_gpu_for_build("build")

    def _handoff():
        time.sleep(0.15)
        gpu_lease.release_gpu_for_build()  # GPU freed at the handoff

    t = threading.Thread(target=_handoff)
    t.start()
    out = client.generate("reflect")  # blocks, then proceeds when freed
    t.join()
    assert out == "RESULT"
    assert len(posts) == 1  # ran AFTER the build released — never mid-run


def test_generate_runs_while_diagnostic_holder_remains(monkeypatch):
    monkeypatch.setenv("OMEGA_GPU_WAIT_S", "0.1")
    client, posts = _client(monkeypatch)
    gpu_lease.acquire_gpu_for_build("build")
    assert client.generate("reflect") == "RESULT"
    assert len(posts) == 1


def test_generate_completion_waits_its_turn(monkeypatch):
    client, posts = _client(monkeypatch)
    gpu_lease.acquire_gpu_for_build("build")

    def _handoff():
        time.sleep(0.15)
        gpu_lease.release_gpu_for_build()

    t = threading.Thread(target=_handoff)
    t.start()
    body = client.generate_completion("decide")
    t.join()
    assert body and body.get("text") == "RESULT"
    assert len(posts) == 1


def test_generate_stream_opens_while_diagnostic_holder_exists(monkeypatch):
    monkeypatch.setenv("OMEGA_GPU_WAIT_S", "0.1")
    client = RuntimeClient(base_url="http://127.0.0.1:11434")
    opened = {"n": 0}

    def _stream_events(*a, **k):
        opened["n"] += 1
        return iter((("token", {"token": "ready"}), ("done", {})))

    monkeypatch.setattr(client, "_stream_events", _stream_events)
    gpu_lease.acquire_gpu_for_build("build")
    assert list(client.generate_stream("narrate")) == ["ready"]
    assert opened["n"] == 1


def test_no_build_no_wait(monkeypatch):
    client, posts = _client(monkeypatch)
    assert client.generate("x") == "RESULT"
    assert len(posts) == 1


def test_flag_off_disables_gating(monkeypatch):
    monkeypatch.setenv("OMEGA_GPU_BUILD_LEASE", "0")
    client, posts = _client(monkeypatch)
    gpu_lease.acquire_gpu_for_build("build")
    assert client.generate("x") == "RESULT"  # flag off -> original behavior
    assert len(posts) == 1

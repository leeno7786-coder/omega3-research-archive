"""Boot-time cognition runtime gate.

Cognition cannot run without a resident model at the configured context. The
gate loads at 64k and HARD-FAILS when that exact contract cannot be satisfied;
silently shrinking the context would change cognition semantics.
"""

from __future__ import annotations

import pytest

import runtime.model_control as mc


def test_configured_context_is_64k_with_no_smaller_fallback():
    assert mc.COGNITION_CTX_PREFERRED == 65536
    assert not hasattr(mc, "COGNITION_CTX_FALLBACK")


def test_load_model_threads_context_size_as_num_ctx(monkeypatch):
    captured: list[dict] = []

    def _fake_post(base, path, body, *, timeout=0.0):
        captured.append(body)
        # First route "succeeds" so load_model returns ok on the first attempt.
        return {"ok": True, "route": path, "status": 200, "payload": {}}

    monkeypatch.setattr(mc, "_post_json", _fake_post)
    mc.load_model("gemma_e2b", base_url="http://x", context_size=65536)

    # num_ctx must appear in the request the runtime receives.
    assert any(
        b.get("num_ctx") == 65536
        or (isinstance(b.get("options"), dict) and b["options"].get("num_ctx") == 65536)
        for b in captured
    )


def test_ensure_cognition_runtime_loads_at_preferred(monkeypatch):
    calls: list[int] = []

    def _fake_load(model, *, base_url=None, context_size=None):
        calls.append(context_size)
        return {"ok": True, "model": model}

    monkeypatch.setattr(mc, "load_model", _fake_load)
    monkeypatch.setattr(mc, "_probe_runtime_reachable", lambda _base: True)
    result = mc.ensure_cognition_runtime(model="gemma_e2b", base_url="http://x")

    assert result["ok"] is True
    assert result["context_size"] == mc.COGNITION_CTX_PREFERRED
    assert calls == [mc.COGNITION_CTX_PREFERRED]  # no degrade needed


def test_ensure_cognition_runtime_never_retries_at_smaller_context(monkeypatch):
    calls: list[int] = []

    def _fake_load(model, *, base_url=None, context_size=None):
        calls.append(context_size)
        return {"ok": False, "model": model, "error": "no slot / OOM"}

    monkeypatch.setattr(mc, "load_model", _fake_load)
    monkeypatch.setattr(mc, "_probe_runtime_reachable", lambda _base: True)
    with pytest.raises(mc.RuntimeCognitionUnavailable, match="65536"):
        mc.ensure_cognition_runtime(model="gemma_e2b", base_url="http://x")

    assert calls == [mc.COGNITION_CTX_PREFERRED]


def test_ensure_cognition_runtime_crashes_when_configured_context_fails(monkeypatch):
    calls: list[int] = []

    def _fake_load(model, *, base_url=None, context_size=None):
        calls.append(context_size)
        return {"ok": False, "model": model, "error": "no slot / OOM"}

    monkeypatch.setattr(mc, "load_model", _fake_load)
    monkeypatch.setattr(mc, "_probe_runtime_reachable", lambda _base: True)
    with pytest.raises(mc.RuntimeCognitionUnavailable, match="no slot / OOM"):
        mc.ensure_cognition_runtime(model="gemma_e2b", base_url="http://x")
    assert calls == [mc.COGNITION_CTX_PREFERRED]


def test_ensure_cognition_runtime_crashes_when_runtime_down(monkeypatch):
    # Runtime down must fail through the reachability gate before any model load.
    calls: list[int] = []

    def _fake_load(model, *, base_url=None, context_size=None):
        calls.append(context_size)
        return {"ok": False, "model": model, "error": "ConnectionError"}

    monkeypatch.setattr(mc, "load_model", _fake_load)
    monkeypatch.setattr(mc, "_probe_runtime_reachable", lambda _base: False)
    with pytest.raises(mc.RuntimeCognitionUnavailable, match="not reachable"):
        mc.ensure_cognition_runtime(model="gemma_e2b", base_url="http://x")
    assert calls == []

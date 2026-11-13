from __future__ import annotations

import pytest

from runtime import model_control


def test_dev_pill_runtime_keeps_resident_and_warms_cli(monkeypatch):
    # 2026-09-05: the single Mistral AUTO interpreter is remote and the local
    # CLI driver is only an execution tool. Preparing that tool evicts no brain
    # model, and the local driver is already resident.
    # Only the warm-load runs.
    cli = model_control.DEFAULT_BROWSER_MODEL
    assert cli == model_control.LOCAL_RESIDENT_MODEL
    assert model_control.DEFAULT_COGNITION_MODEL in model_control.CLOUD_COGNITION_SEATS
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(model_control, "_cloud_cli_route", lambda: None)

    monkeypatch.setattr(
        model_control,
        "resolve_runtime_base_url",
        lambda: "http://127.0.0.1:11434",
    )

    # THE route omega-runtime actually serves: routes_orchestration is mounted
    # under /admin/orchestration. The ladder used to lead with /admin/models/…,
    # which 404s here — harmless for load, but on the unload side the same
    # guessing walked all the way to an Ollama keep_alive:0 generate and SEATED
    # the model it was asked to evict (2026-08-15). Asserting the real route is
    # first keeps the brain pointed at the door that exists.
    real_route = f"/admin/orchestration/models/{cli}/load"

    def fake_post_json(base_url, path, body, *, timeout=model_control._CONTROL_TIMEOUT):
        calls.append((path, body))
        if path == real_route:
            return {"ok": True, "route": path, "payload": {"status": "loaded"}}
        return {"ok": False, "route": path, "unsupported": True}

    monkeypatch.setattr(model_control, "_post_json", fake_post_json)

    result = model_control.prepare_dev_pill_runtime()

    assert result["ok"] is True
    assert result["target_model"] == cli
    assert result["unload"] == []  # nothing evicted — same-model handoff
    assert result["load"]["model"] == cli
    assert calls == [(real_route, {})]  # first try, no fallback walk


def test_browser_agent_runtime_unified_seat_evicts_nothing(monkeypatch):
    # Browser-agent start used to hot-swap a cognition model off the GPU. The
    # current local driver is a tool under AUTO, so preparation evicts nothing
    # and does not change physical pipeline ownership.
    unloaded: list[str] = []
    monkeypatch.setattr(model_control, "_cloud_cli_route", lambda: None)

    monkeypatch.setattr(
        model_control,
        "resolve_runtime_base_url",
        lambda: "http://127.0.0.1:11434",
    )
    monkeypatch.setattr(
        model_control,
        "unload_model",
        lambda model, *, base_url=None: unloaded.append(model) or {"ok": True, "model": model},
    )
    monkeypatch.setattr(
        model_control,
        "load_model",
        lambda model, *, base_url=None: {"ok": True, "model": model},
    )

    result = model_control.prepare_browser_agent_runtime()

    assert result["ok"] is True
    assert result["target_model"] == model_control.DEFAULT_BROWSER_MODEL
    assert unloaded == []  # same-model handoff: the resident stays seated


def test_release_dev_pill_unloads_cli_and_defers_cognition_to_next_request(monkeypatch):
    """Session release unloads the local tool driver without loading a second brain."""
    cli = model_control.DEFAULT_BROWSER_MODEL
    unloaded: list[str] = []
    monkeypatch.setattr(model_control, "_cloud_cli_route", lambda: None)

    monkeypatch.setattr(
        model_control,
        "resolve_runtime_base_url",
        lambda: "http://127.0.0.1:11434",
    )
    monkeypatch.setattr(
        model_control,
        "unload_model",
        lambda model, *, base_url=None: unloaded.append(model) or {"ok": True, "model": model},
    )
    monkeypatch.setattr(
        model_control,
        "ensure_cognition_runtime",
        lambda **kw: (_ for _ in ()).throw(AssertionError("release warmed cognition")),
    )

    result = model_control.release_dev_pill_runtime()

    assert result["ok"] is True
    assert result["target_model"] == cli
    assert unloaded == [cli]
    assert result["cognition_restore"]["ok"] is True
    assert result["cognition_restore"]["deferred"] is True


def test_release_browser_agent_does_not_probe_or_load_cognition(monkeypatch):
    monkeypatch.setattr(model_control, "_cloud_cli_route", lambda: None)
    monkeypatch.setattr(model_control, "resolve_runtime_base_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(
        model_control,
        "unload_model",
        lambda model, *, base_url=None: {"ok": True, "model": model},
    )

    monkeypatch.setattr(
        model_control,
        "ensure_cognition_runtime",
        lambda **kw: (_ for _ in ()).throw(AssertionError("release loaded cognition")),
    )

    result = model_control.release_browser_agent_runtime()
    assert result["cognition_restore"] == {"ok": True, "deferred": True}


def test_retired_openrouter_force_cannot_create_an_agent_route(monkeypatch, tmp_path):
    """Agent execution follows the unified mind seat, never the retired env route."""
    monkeypatch.setattr("core.paths.OMEGA_ROOT", tmp_path)
    monkeypatch.delenv("OMEGA_MIND_BACKEND", raising=False)
    monkeypatch.delenv("OMEGA_MIND_MODEL", raising=False)
    monkeypatch.delenv("OMEGA_MIND_BASE_URL", raising=False)
    monkeypatch.delenv("OMEGA_MIND_API_KEY", raising=False)
    monkeypatch.setenv("OMEGA_USE_OPENROUTER_CLI", "1")

    assert model_control._cloud_cli_route() is None


# I8 — model-control attaches admin auth when OMEGA_ADMIN_TOKEN is set, so
# load/unload don't 401 on a non-loopback bind (which would strand jackrong and
# defeat the dev-pill hotswap reclaim).


class _FakeResp:
    status_code = 200

    def json(self):
        return {"ok": True}


def test_post_json_attaches_bearer_when_admin_token_set(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["headers"] = headers
        return _FakeResp()

    monkeypatch.setattr(model_control.requests, "post", fake_post)
    monkeypatch.setenv("OMEGA_ADMIN_TOKEN", "secret-123")
    model_control._post_json("http://127.0.0.1:11434", "/models/unload", {"model": "x"})
    assert (captured["headers"] or {}).get("Authorization") == "Bearer secret-123"


def test_post_json_no_auth_header_when_token_absent(monkeypatch):
    captured = {}

    def fake_post(url, json=None, timeout=None, headers=None):
        captured["headers"] = headers
        return _FakeResp()

    monkeypatch.setattr(model_control.requests, "post", fake_post)
    monkeypatch.delenv("OMEGA_ADMIN_TOKEN", raising=False)
    model_control._post_json("http://127.0.0.1:11434", "/models/unload", {"model": "x"})
    assert not (captured["headers"] or {}).get("Authorization")


def test_ensure_cognition_runtime_is_noop_on_cloud_brain(monkeypatch):
    """Remote Mistral interpretation has no local brain model for boot to load.

    The boot gate must SKIP (ok, no load, no reachability probe) rather than
    hard-fail, touch the local runtime, or install a second interpreter.
    """
    from runtime import brain_cloud

    monkeypatch.setattr(brain_cloud, "enabled", lambda: True)
    monkeypatch.setattr(model_control, "resolve_runtime_base_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(
        model_control,
        "_probe_runtime_reachable",
        lambda base: (_ for _ in ()).throw(AssertionError("probed runtime under cloud brain")),
    )
    monkeypatch.setattr(
        model_control,
        "load_model",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("loaded local model under cloud brain")
        ),
    )

    result = model_control.ensure_cognition_runtime()

    assert result["ok"] is True
    assert result["skipped"] == "brain_cloud"


def test_ensure_cognition_runtime_loads_locally_when_cloud_brain_off(monkeypatch):
    """The remote skip is scoped: a configured local-brain test still loads its model."""
    from runtime import brain_cloud

    monkeypatch.setattr(brain_cloud, "enabled", lambda: False)
    monkeypatch.setattr(model_control, "resolve_runtime_base_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(model_control, "_probe_runtime_reachable", lambda base: True)
    loaded: list[tuple[str, int]] = []
    monkeypatch.setattr(
        model_control,
        "load_model",
        lambda model, *, base_url=None, context_size=None: (
            loaded.append((model, context_size)) or {"ok": True, "model": model}
        ),
    )

    result = model_control.ensure_cognition_runtime()

    assert result["ok"] is True
    assert result["model"] == model_control.DEFAULT_COGNITION_MODEL
    assert loaded and loaded[0][0] == model_control.DEFAULT_COGNITION_MODEL


# ── Endpoint-addressed control (operator ruling 2026-08-15) ─────────────────
# "the omega system itself should only resolve to endpoints, let the runtime
# handle the models, omega just calls them."


def test_unload_never_falls_back_to_a_generate(monkeypatch):
    """THE 2026-08-15 regression, nailed to the wall.

    Ollama unloads by POSTing /api/generate with keep_alive: 0. On omega-runtime
    /api/generate is the GENERATE route and _ensure_model_worker loads on
    demand, unguarded — so that fallback seats the model it was told to evict.
    It did: 23s of spawn, a 200 back, boot logging "Evicted stale local
    resident" about a model it had just loaded, and 9862 MiB held for a seat
    with no callers.

    Every route is refused here, which is the dangerous case: a ladder with a
    generate at the bottom reaches it precisely when nothing else works.
    """
    paths: list[str] = []
    monkeypatch.setattr(model_control, "resolve_runtime_base_url", lambda: "http://127.0.0.1:11434")

    def fake_post_json(base_url, path, body, *, timeout=model_control._CONTROL_TIMEOUT):
        paths.append(path)
        return {"ok": False, "route": path, "unsupported": True}

    monkeypatch.setattr(model_control, "_post_json", fake_post_json)

    result = model_control.unload_model("qwen35_9b")

    assert result["ok"] is False  # honest failure beats a lie
    assert paths, "unload attempted nothing at all"
    assert not any("generate" in p for p in paths)
    assert not any("completions" in p for p in paths)


def test_unload_endpoint_addresses_the_capability_and_reports_refusals(monkeypatch):
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(model_control, "resolve_runtime_base_url", lambda: "http://127.0.0.1:11434")

    def fake_post_json(base_url, path, body, *, timeout=model_control._CONTROL_TIMEOUT):
        calls.append((path, body))
        # A pinned seat: the runtime says no, and that no must survive intact.
        return {"ok": False, "route": path, "error": "pinned_model:qwen35_9b"}

    monkeypatch.setattr(model_control, "_post_json", fake_post_json)

    result = model_control.unload_endpoint("generate")

    # ONE call, to the endpoint door — no model id anywhere, no route walking
    # in search of a yes.
    assert calls == [("/admin/orchestration/endpoints/generate/unload", {})]
    assert result["ok"] is False
    assert result["endpoint"] == "generate"
    assert "pinned_model" in result["error"]


def test_unload_endpoint_marks_an_unsupported_route_as_a_skip(monkeypatch):
    events: list[dict[str, object]] = []
    monkeypatch.setattr(model_control, "resolve_runtime_base_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(
        model_control,
        "_post_json",
        lambda *args, **kwargs: {
            "ok": False,
            "route": "/admin/orchestration/endpoints/generate/unload",
            "status": 404,
            "unsupported": True,
        },
    )
    monkeypatch.setattr(
        "guardian.terminal_feedback.emit_guardian_event",
        lambda _subsystem, _event, **fields: events.append(fields),
    )

    result = model_control.unload_endpoint("generate")

    assert result["ok"] is False
    assert result["unsupported"] is True
    assert events and events[-1]["status"] == "skip"
    assert events[-1].get("ok") is None


def test_load_endpoint_addresses_the_capability(monkeypatch):
    calls: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(model_control, "resolve_runtime_base_url", lambda: "http://127.0.0.1:11434")

    def fake_post_json(base_url, path, body, *, timeout=model_control._CONTROL_TIMEOUT):
        calls.append((path, body))
        return {"ok": True, "route": path, "payload": {"models": ["qwen35_9b"]}}

    monkeypatch.setattr(model_control, "_post_json", fake_post_json)

    result = model_control.load_endpoint("generate", context_size=262144)

    assert result["ok"] is True
    assert calls[0][0] == "/admin/orchestration/endpoints/generate/load"
    assert calls[0][1]["num_ctx"] == 262144


def test_resolve_endpoint_model_asks_the_runtime(monkeypatch):
    """The few places that must name a model on the wire ASK rather than know."""

    class _Res:
        status_code = 200

        @staticmethod
        def json():
            return {
                "ok": True,
                "endpoints": {
                    "resolved": {"generate": "some_frontier_seat", "face_identify": ["det", "emb"]}
                },
            }

    monkeypatch.setattr(model_control, "resolve_runtime_base_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(model_control.requests, "get", lambda *a, **k: _Res())

    assert model_control.resolve_endpoint_model("generate") == "some_frontier_seat"
    # A pipeline answers with its first, addressable stage.
    assert model_control.resolve_endpoint_model("face_identify") == "det"
    assert model_control.resolve_endpoint_model("undeclared") == ""


def test_resolve_endpoint_model_degrades_silently_when_runtime_is_down(monkeypatch):
    import requests as _requests

    monkeypatch.setattr(model_control, "resolve_runtime_base_url", lambda: "http://127.0.0.1:11434")

    def _boom(*a, **k):
        raise _requests.ConnectionError("runtime down")

    monkeypatch.setattr(model_control.requests, "get", _boom)
    # "" lets the caller fall back to its local label rather than fail.
    assert model_control.resolve_endpoint_model("generate") == ""


# ── One configured brain descriptor drives interpretation ───────────────────
# "the whole system needs to be set for the single model that the user has
# loaded." The tests below exercise generic descriptor propagation with an
# isolated fake provider; production remains configured for Mistral.


@pytest.fixture
def _brain_descriptor(monkeypatch, tmp_path):
    from runtime import brain_cloud_config

    # The operator's own seat (guardian/state/mind_backend.json) must not leak in:
    # a local LM Studio selection there is not a cloud provider.
    monkeypatch.setattr(brain_cloud_config, "_config_path", lambda: tmp_path / "mind_backend.json")
    monkeypatch.setenv("OMEGA_MIND_BACKEND", "cloud")
    monkeypatch.setenv("OMEGA_MIND_MODEL", "zai-glm-5-2")
    monkeypatch.setenv("OMEGA_MIND_BASE_URL", "https://api.mistral.ai/v1")
    monkeypatch.setenv("OMEGA_MIND_API_KEY", "test-key")


def test_the_brain_descriptor_owns_the_cli_route(_brain_descriptor):
    """The configured brain descriptor wins over optional cloud tool routes."""
    route = model_control._cloud_cli_route()
    assert route == {"provider": "brain_cloud", "model": "zai-glm-5-2"}


def test_the_agent_handoff_seats_no_local_model_under_a_brain_descriptor(
    _brain_descriptor, monkeypatch
):
    """Regression, live 2026-08-15 08:25:04.

    A build's motor execution loaded qwen35_9b through
    prepare_browser_agent_runtime while cognition ran on GLM and
    OMEGA_SKIP_RESIDENT_LLM was set -- the resolver found no cloud route and
    took the local path. The release afterwards failed too, so 9.6 GB stayed
    held for hands nothing was using.
    """
    touched: list[tuple[str, str]] = []
    monkeypatch.setattr(
        model_control, "load_model", lambda m, **k: touched.append(("load", m))
    )
    monkeypatch.setattr(
        model_control, "unload_model", lambda m, **k: touched.append(("unload", m))
    )

    for prepare in (
        model_control.prepare_browser_agent_runtime,
        model_control.prepare_dev_pill_runtime,
    ):
        result = prepare()
        assert result["ok"] is True
        assert result["skipped"] == "brain_cloud"

    assert touched == []  # nothing local seated, nothing evicted

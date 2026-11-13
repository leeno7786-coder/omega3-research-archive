from __future__ import annotations

import browser_agent.lifecycle as lifecycle


def test_prepare_hud_browser_ensures_debugger_without_starting_driver(monkeypatch, tmp_path):
    seen: dict[str, object] = {}

    class _Bootstrap:
        def __init__(self, **kwargs):
            seen["root"] = kwargs["omega_root"]

        def ensure_debugger(self, **kwargs):
            seen["ensure"] = kwargs
            return {"debugger_address": kwargs["debugger_address"], "edge_started": True}

        def open_new_window(self, url):
            seen["url"] = url
            return object()

    monkeypatch.setattr(lifecycle, "_HUD_BOOTSTRAP", None)
    receipt = lifecycle.prepare_hud_browser(
        omega_root=tmp_path,
        bootstrap_factory=_Bootstrap,
    )

    assert receipt["edge_started"] is True
    assert seen["root"] == tmp_path
    assert seen["ensure"] == {
        "debugger_address": lifecycle.HUD_DEBUGGER_ADDRESS,
        "restart_existing_edge": False,
    }

    lifecycle.open_hud_window("http://127.0.0.1:13310")
    assert seen["url"] == "http://127.0.0.1:13310"


def test_prepare_hud_browser_forwards_initial_url_for_single_window_boot(monkeypatch, tmp_path):
    seen: dict[str, object] = {}

    class _Bootstrap:
        def __init__(self, **_kwargs):
            pass

        def ensure_debugger(self, **kwargs):
            seen["ensure"] = kwargs
            return {
                "debugger_address": kwargs["debugger_address"],
                "edge_started": True,
                "initial_url_opened": True,
            }

    monkeypatch.setattr(lifecycle, "_HUD_BOOTSTRAP", None)
    receipt = lifecycle.prepare_hud_browser(
        omega_root=tmp_path,
        initial_url="http://127.0.0.1:13310",
        bootstrap_factory=_Bootstrap,
    )

    assert receipt["initial_url_opened"] is True
    assert seen["ensure"] == {
        "debugger_address": lifecycle.HUD_DEBUGGER_ADDRESS,
        "restart_existing_edge": False,
        "initial_url": "http://127.0.0.1:13310",
    }


def test_hud_window_requires_prepared_edge(monkeypatch):
    monkeypatch.setattr(lifecycle, "_HUD_BOOTSTRAP", None)

    try:
        lifecycle.open_hud_window("http://127.0.0.1:13310")
    except lifecycle.BrowserLifecycleError as exc:
        assert str(exc) == "hud_edge_not_prepared"
    else:
        raise AssertionError("HUD window must not open through an unprepared Edge process")

from __future__ import annotations

import argparse

import browser_agent.lifecycle as lifecycle
from hud import standalone


def test_standalone_browser_flag_defaults_to_server_only(monkeypatch):
    monkeypatch.delenv("OMEGA_HUD_OPEN_BROWSER", raising=False)
    args = argparse.Namespace(open_browser=False, no_browser=False)

    assert standalone._browser_open_requested(args) is False


def test_standalone_browser_flag_honors_launcher_marker_and_override(monkeypatch):
    monkeypatch.setenv("OMEGA_HUD_OPEN_BROWSER", "1")
    args = argparse.Namespace(open_browser=False, no_browser=False)
    assert standalone._browser_open_requested(args) is True

    args.no_browser = True
    assert standalone._browser_open_requested(args) is False


def test_standalone_opens_hud_only_after_server_binds(monkeypatch):
    seen: list[tuple[str, object]] = []
    probes = iter((False, True))
    monkeypatch.setattr(standalone, "_port_in_use", lambda _host, _port: next(probes))
    monkeypatch.setattr(standalone.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        lifecycle,
        "prepare_hud_browser",
        lambda **kwargs: seen.append(("prepare", kwargs))
        or {"initial_url_opened": True},
    )
    monkeypatch.setattr(lifecycle, "open_hud_window", lambda url: seen.append(("open", url)))

    standalone._open_browser_when_ready(
        "http://127.0.0.1:13310",
        host="127.0.0.1",
        port=13310,
    )

    assert seen == [
        (
            "prepare",
            {
                "omega_root": standalone.ROOT.resolve(),
                "initial_url": "http://127.0.0.1:13310",
            },
        )
    ]

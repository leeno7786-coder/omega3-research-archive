from __future__ import annotations

from types import SimpleNamespace

import cognition_chain as chain
from omega_loop.episode import packet as packet_module


def test_terminal_response_runs_the_normal_omega_episode_once(monkeypatch) -> None:
    """AUTO replying completes the action, not the episode's Omega cognition."""
    events: list[str] = []
    episode = SimpleNamespace(
        episode_id="ep1",
        meta={},
        auto={"prompt_pack": {"prompt_type": "RESPONSE_QUESTION"}},
        global_={"omega_packet": {"intent": "RESPOND"}},
        pressure={},
    )
    store = SimpleNamespace(is_tenth_episode_window_reflection=lambda: False)

    def _meta_b(ep):
        events.append("meta_b")
        ep.meta["reflection"] = {}
        return {}

    monkeypatch.setattr("meta_loop.meta_b.run_meta_b_front", _meta_b)
    monkeypatch.setattr(
        "global_loop.loop.run_global_b",
        lambda *_a, **_k: events.append("global_b"),
    )
    monkeypatch.setattr(
        chain,
        "_run_skill_foldback",
        lambda *_a, **_k: events.append("skill_foldback"),
    )
    monkeypatch.setattr(
        chain,
        "_refresh_packet_wip_from_execution_report",
        lambda *_a, **_k: events.append("report_refresh"),
    )

    def _request_text(*_a, **_k):
        events.append("request_capture")
        return "request"

    monkeypatch.setattr(chain, "_operator_request_text", _request_text)
    monkeypatch.setattr(
        packet_module,
        "refresh_omega_execution_foldback",
        lambda *_a, **_k: events.append("execution_refresh"),
    )
    monkeypatch.setattr(
        chain,
        "_run_omega_episode_cognition",
        lambda *_a, **_k: events.append("omega"),
    )
    monkeypatch.setattr(chain, "_execute_pending_workspace_port", lambda *_a, **_k: None)
    chain._start_meta_b_global_b(store, episode)

    assert events == [
        "meta_b",
        "global_b",
        "request_capture",
        "skill_foldback",
        "report_refresh",
        "execution_refresh",
        "omega",
    ]

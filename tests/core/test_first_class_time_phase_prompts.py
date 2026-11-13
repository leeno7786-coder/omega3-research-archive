"""FIRST-CLASS TIME (task #17) — every cognitive model pass surfaces the wall clock.

The mind must know *when it is* in EVERY lane that reaches a model:
  - AUTO promptpack + connector decode (pre-existing pins:
    test_auto_promptpack.py / test_connector_tool.py)
  - omega per-episode cognition   (pinned here)
  - omega post-chat reflection    (pinned here)
  - omega window reflection/fold  (pinned here)
  - operator fast reply           (pinned here)

Per-episode passes render the EPISODE's fixed ignition envelope
(deterministic — never a fresh clock read); the window fold and the fast
reply render "now" through core.symphony_time (the temporal authority —
pinned here via set_fake_time). deepsleep/meta/global run no model passes
(verified 2026-07-18: no invoke_runtime_generate anywhere under them), and the
execution contract between Meta A and AUTO is a deterministic projection with
no model pass (2026-09-14); the fetched-knowledge comprehension pass is
mechanical classification and stays lean by design.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from core import symphony_time

_TIME_PREFIX = "Current date & time: "


@pytest.fixture
def fake_clock():
    """Pin symphony_time to a fixed 2026 moment; clear on teardown."""
    wall = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc).timestamp()
    symphony_time.set_fake_time(iso="2026-07-18T12:00:00+00:00", wall=wall)
    yield wall
    symphony_time.clear_fake_time()


def _assert_time_first_line(system: str) -> None:
    assert system.startswith(_TIME_PREFIX), system[:120]
    assert "2026" in system.splitlines()[0], system.splitlines()[0]


def _done_control() -> dict:
    """The strict Omega control envelope every per-episode fold returns."""
    assessment = dict.fromkeys(
        ("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
        "episode evidence",
    )
    return {
        "assessment": {**assessment, "gap": "", "evidence": ["episode receipt"]},
        "decision": "DONE",
        "pressure": None,
        "synthesis": "grounded summary",
    }


# ── shared helper ─────────────────────────────────────────────────────────


def test_prepend_time_line_renders_envelope_and_keeps_prompt(fake_clock):
    from core.episode_temporal import prepend_time_line

    out = prepend_time_line("BASE PROMPT", {"wall_clock_seconds": fake_clock})
    _assert_time_first_line(out)
    assert out.endswith("BASE PROMPT")


def test_prepend_time_line_never_breaks_the_prompt(fake_clock):
    # Failure-soft contract: garbage envelope still yields a usable prompt
    # (temporal_prompt_line falls back to a fresh authority read).
    from core.episode_temporal import prepend_time_line

    out = prepend_time_line("BASE PROMPT", "not a dict")
    assert out.endswith("BASE PROMPT")
    _assert_time_first_line(out)


# ── omega per-episode cognition ───────────────────────────────────────────


def _episode_with_packet(fake_wall, **overrides):
    packet = {
        "episode_id": "ep-time-1",
        "intent": "LEARN",
        "action": "USE_TOOL",
        "omega_lane": "agent",
        "tool": "web_fetch",
        "domain": "quantum_physics",
        "operator_text": None,
        "outcome": "success",
        "failure_reason": None,
        "raw_tool_result": {"results": [{"title": "Qubits", "snippet": "superposition"}]},
        "recall": [],
        "recall_error": None,
        "prior_mastery": {
            "domain": "quantum_physics",
            "knowledge_mastery": 0.2,
            "application_mastery": 0.1,
            "target_mastery": 0.7,
        },
    }
    packet.update(overrides)
    return SimpleNamespace(
        episode_id=packet["episode_id"],
        temporal={"wall_clock_seconds": fake_wall, "captured_at_iso": "2026-07-18T12:00:00+00:00"},
        global_={"omega_packet": packet},
    )


def test_omega_episode_cognition_sees_the_episode_wall_clock(fake_clock):
    from omega_loop.episode.run import run_omega_for_episode

    payload = {
        "control": _done_control(),
        "knowledge_to_ingest": [
            {
                "domain": "quantum_physics",
                "statement": "A qubit holds superposition.",
                "confidence": 0.9,
            }
        ],
        "mastery": {
            "knowledge_mastery": 0.45,
            "application_mastery": 0.1,
            "rationale": "covered superposition basics",
        },
    }
    captured = {}

    def fake(system, user):
        captured["system"] = system
        return json.dumps(payload)

    res = run_omega_for_episode(_episode_with_packet(fake_clock), reason_fn=fake)
    assert res.cognized is True
    _assert_time_first_line(captured["system"])


# ── omega post-chat reflection ────────────────────────────────────────────


def test_omega_response_reflection_sees_the_episode_wall_clock(fake_clock):
    from omega_loop.episode.run import run_omega_for_episode

    captured = {}

    def fake(system, user):
        captured["system"] = system
        return json.dumps({"control": _done_control(), "identity_patterns": []})

    ep = _episode_with_packet(
        fake_clock,
        intent="RESPOND",
        action="RESPONSE",
        omega_lane="chat",
        operator_text="hey omega",
        tool=None,
        raw_tool_result=None,
    )
    run_omega_for_episode(ep, reason_fn=fake)
    _assert_time_first_line(captured["system"])

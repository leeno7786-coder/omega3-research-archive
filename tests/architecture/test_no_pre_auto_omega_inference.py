"""Meta A hands its validated pack straight to AUTO: no model inference sits between them."""

from types import SimpleNamespace

import cognition_chain


def test_meta_a_hands_directly_to_auto_without_omega_motor_inference(monkeypatch):
    calls = []
    monkeypatch.setattr("meta_loop.loop.run_meta_a", lambda _episode: {"action": "USE_SKILL"})
    # Configuration alone must not decide the topology: the live call itself is gone.
    monkeypatch.setattr(cognition_chain, "motor_programs_enabled", lambda *_a: True, raising=False)
    monkeypatch.setattr("omega_loop.motor.run.run_motor_planning",
                        lambda *_a, **_k: calls.append("motor"), raising=False)
    monkeypatch.setattr(cognition_chain, "_start_motor_planning",
                        lambda *_a, **_k: calls.append("motor"), raising=False)
    monkeypatch.setattr(cognition_chain, "_start_auto", lambda *_a, **_k: calls.append("auto"))

    # WO-04: the acceptance-criteria capture needs the owner the admission seam always opens.
    from tests.helpers.episode_owner import owned

    with owned("agent-episode"):
        cognition_chain._start_meta_a(
            object(),
            SimpleNamespace(episode_id="agent-episode", meta={}, global_={}),
        )

    assert calls == ["auto"]

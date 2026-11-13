"""RISK 4(a): the boot probe waits out a cold start instead of crashing on it.

Measured 2026-08-11. The machine came up at 12:55:48, the unit started at
12:55:59, the gateway was spawned at 12:56:00 — and had still not answered
/health when ``probe_cognition_runtime`` fired at 13:00:40, so the brain crashed
by law and systemd restarted it five seconds later into a boot that worked. Self
healing, but wrong: a cold start is a KNOWN slow path, not a failure.

What these pin is the distinction the old single-shot probe could not make:
a runtime that is LATE is waited for; a runtime that is ABSENT still crashes
the boot. Hermetic — the clock, the sleep and the health probe are all fakes,
so nothing here waits on wall time or on a live gateway.
"""

from __future__ import annotations

import pytest

import runtime.model_control as mc


@pytest.fixture
def fake_clock(monkeypatch: pytest.MonkeyPatch):
    """A monotonic clock that only advances when the code under test sleeps.

    That is what makes a 300-second deadline testable in milliseconds, and it
    also proves the wait is driven by the deadline rather than by a fixed
    attempt count: nothing moves time except the backoff itself.
    """
    state = {"now": 0.0, "sleeps": []}

    def _monotonic() -> float:
        return state["now"]

    def _sleep(seconds: float) -> None:
        state["sleeps"].append(seconds)
        state["now"] += seconds

    monkeypatch.setattr(mc.time, "monotonic", _monotonic)
    monkeypatch.setattr(mc.time, "sleep", _sleep)
    return state


def _arm_probe(monkeypatch: pytest.MonkeyPatch, answers) -> list[str]:
    """Health probe returning the next value of ``answers`` (last value repeats)."""
    seen: list[str] = []
    pending = list(answers)

    def _probe(base: str) -> bool:
        seen.append(base)
        return pending.pop(0) if len(pending) > 1 else pending[0]

    monkeypatch.setattr(mc, "_probe_runtime_reachable", _probe)
    return seen


def test_probe_succeeds_when_the_runtime_appears_late(
    monkeypatch: pytest.MonkeyPatch, fake_clock
) -> None:
    """The cold-boot case: down for the first probes, then up. No crash."""
    _arm_probe(monkeypatch, [False, False, False, False, True])

    result = mc.probe_cognition_runtime(model="m", base_url="http://x")

    assert result["ok"] is True
    assert result["probes"] == 5
    # It genuinely waited rather than returning on a stale first answer.
    assert result["waited_s"] > 0
    assert fake_clock["sleeps"], "a late runtime must be polled, not probed once"


def test_probe_fails_hard_past_the_deadline(
    monkeypatch: pytest.MonkeyPatch, fake_clock
) -> None:
    """Waiting is not tolerating: a runtime that never arrives still crashes boot."""
    _arm_probe(monkeypatch, [False])

    with pytest.raises(mc.RuntimeCognitionUnavailable, match="not reachable after waiting"):
        mc.probe_cognition_runtime(model="m", base_url="http://x", wait_s=30.0)

    # And it stopped AT the deadline rather than polling forever.
    assert sum(fake_clock["sleeps"]) <= 30.0


def test_probe_backs_off_but_starts_fast(
    monkeypatch: pytest.MonkeyPatch, fake_clock
) -> None:
    """Frequent at first, then capped.

    A warm restart answers in well under a second and must not pay a fixed
    penalty for the cold case; a five-minute cold wait must not be five minutes
    of loopback requests either.
    """
    _arm_probe(monkeypatch, [False])

    with pytest.raises(mc.RuntimeCognitionUnavailable):
        mc.probe_cognition_runtime(model="m", base_url="http://x", wait_s=60.0)

    sleeps = fake_clock["sleeps"]
    assert sleeps[0] == mc._PROBE_POLL_START_S
    # The FINAL sleep is clipped to whatever the deadline had left, so the
    # monotonic-growth claim is about everything before it.
    body = sleeps[:-1]
    assert body == sorted(body)
    assert max(sleeps) <= mc._PROBE_POLL_CEILING_S
    assert mc._PROBE_POLL_CEILING_S in sleeps, "the backoff must actually reach its ceiling"


def test_warm_runtime_is_not_delayed(
    monkeypatch: pytest.MonkeyPatch, fake_clock
) -> None:
    """The ordinary boot pays nothing for the cold-boot machinery."""
    _arm_probe(monkeypatch, [True])

    result = mc.probe_cognition_runtime(model="m", base_url="http://x")

    assert result["ok"] is True
    assert result["probes"] == 1
    assert fake_clock["sleeps"] == []


def test_wait_can_be_disabled_back_to_the_single_shot_probe(
    monkeypatch: pytest.MonkeyPatch, fake_clock
) -> None:
    """``OMEGA_COGNITION_PROBE_WAIT_S=0`` is the lever back to the old shape."""
    monkeypatch.setenv("OMEGA_COGNITION_PROBE_WAIT_S", "0")
    _arm_probe(monkeypatch, [False])

    with pytest.raises(mc.RuntimeCognitionUnavailable):
        mc.probe_cognition_runtime(model="m", base_url="http://x")

    assert fake_clock["sleeps"] == []


def test_default_wait_covers_the_measured_cold_boot() -> None:
    """The cold boot we measured exceeded 121s; the deadline must clear it.

    Tied to the supervisor's spawn ceiling on purpose — a spawn ceiling shorter
    than this wait would have the probe waiting out a deadline for a gateway the
    supervisor had already tree-killed.
    """
    from runtime.supervisor import _DEFAULT_BOOT_TIMEOUT_S

    assert mc.COGNITION_PROBE_WAIT_S >= 300.0
    assert _DEFAULT_BOOT_TIMEOUT_S >= mc.COGNITION_PROBE_WAIT_S


def test_brain_cloud_route_still_skips_the_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cognition on the cloud seat has no local runtime to wait for."""
    monkeypatch.setattr("runtime.brain_cloud.enabled", lambda: True)

    def _never(_base: str) -> bool:  # pragma: no cover - must not be called
        raise AssertionError("the cloud route must not probe the local runtime")

    monkeypatch.setattr(mc, "_probe_runtime_reachable", _never)
    result = mc.probe_cognition_runtime(model="m", base_url="http://x")

    assert result["skipped"] == "brain_cloud"

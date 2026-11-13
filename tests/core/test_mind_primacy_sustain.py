"""Mastery state informs Omega but cannot autonomously wake cognition."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pressure_loop import pressure as pm  # noqa: E402
import pressure_loop.pressure_field as pf  # noqa: E402
from pressure_loop.pressure import gather_contributors, clear_pending_contributors  # noqa: E402
from deepsleep_loop._deepsleep_state import MasteryRecord  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate():
    clear_pending_contributors()
    pm._RESEED_STAGING.clear()
    pm._RESEED_SUPPRESS.clear()
    yield
    clear_pending_contributors()
    pm._RESEED_STAGING.clear()
    pm._RESEED_SUPPRESS.clear()


def _mastery(monkeypatch, records):
    import deepsleep_loop.mastery_store as ms

    monkeypatch.setattr(ms, "load_mastery", lambda *a, **k: records)


def _on(monkeypatch):
    monkeypatch.setattr(pf, "build_registry", lambda: {})  # no deterministic readers


_UNMASTERED = {
    "ai": MasteryRecord(
        domain_id="ai", domain_name="ai", knowledge_mastery=0.4, target_mastery=0.7
    ),
    "py": MasteryRecord(
        domain_id="py", domain_name="python", knowledge_mastery=0.1, target_mastery=0.7
    ),
    "done": MasteryRecord(
        domain_id="done", domain_name="done", knowledge_mastery=0.9, target_mastery=0.7
    ),
}


def test_unmastered_domains_do_not_wake_when_omega_is_silent(monkeypatch):
    _on(monkeypatch)  # PRIMACY on, no deterministic readers
    _mastery(monkeypatch, _UNMASTERED)
    out = gather_contributors(None)
    assert not any(c.type == "mastery_gap" for c in out)


def test_sustain_silent_when_all_mastered(monkeypatch):
    _on(monkeypatch)
    _mastery(
        monkeypatch,
        {
            "done": MasteryRecord(
                domain_id="done", domain_name="done", knowledge_mastery=0.9, target_mastery=0.7
            )
        },
    )
    out = gather_contributors(None)
    assert not any(c.type == "mastery_gap" for c in out)  # quiet-when-done


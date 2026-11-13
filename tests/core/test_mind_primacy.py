"""Mind-primacy — north-star #1 "no deterministic aggregate auto-fills".

The deterministic per-tick pressure-field readers never own ignition:
``gather_contributors`` reads only operator ingress and Omega-authored successors, so the
system is QUIET (cold start included) until the MIND drives it. The BOOTSTRAP is OPERATOR INTENT (evaluate()'s operator
path ignites onboarding/messages regardless of this floor); the mind then self-continues
via the mind-derived sources (reseed staging + the Omega overlay), and the deterministic
agentic loop sustains its learning agenda. Operator contributors are kept verbatim.

There is NO bootstrap latch (the earlier cold-start/re-arm latch was removed when the
bootstrap moved to operator-intent), and no flag: the floor is never read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pressure_loop import pressure as pm  # noqa: E402
from pressure_loop.contributors import make_contributor  # noqa: E402
from pressure_loop.pressure import gather_contributors, evaluate, clear_pending_contributors  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate():
    clear_pending_contributors()
    pm._RESEED_STAGING.clear()
    pm._RESEED_SUPPRESS.clear()
    pm.reset_mind_primacy()
    yield
    clear_pending_contributors()
    pm._RESEED_STAGING.clear()
    pm._RESEED_SUPPRESS.clear()
    pm.reset_mind_primacy()


# ── the reset is a no-op ──────────────────
def test_reset_mind_primacy_is_noop():
    # the bootstrap is operator-intent, not a latch — reset is a no-op stub kept only
    # for import-surface stability (boot.py + conftest call it).
    assert pm.reset_mind_primacy() is None


# ── suppression is unconditional (cold = quiet, no latch) ──────────
def test_flag_on_suppresses_floor_even_cold_start(monkeypatch):
    import pressure_loop.pressure_field as pf

    # cold start: no mind source, no operator — the deterministic floor must be suppressed
    # (no _bootstrap_active gate) -> empty -> quiet (the spec: cold start = quiet).
    monkeypatch.setattr(pf, "build_registry", lambda: {"learning_debt": lambda ss, **k: 0.29})
    out = gather_contributors(None)
    assert all(c.type != "learning_debt" for c in out)  # suppressed on cold start


# ── end-to-end gather: operator + mind-derived survive, det floor suppressed ─
def test_gather_suppresses_floor_keeps_mind_and_operator(monkeypatch):
    import pressure_loop.pressure_field as pf

    monkeypatch.setattr(pf, "build_registry", lambda: {"learning_debt": lambda ss, **k: 0.29})
    # Omega's prior-fold successor survives; anonymous deterministic reseeds do not.
    pm.stage_reseed([make_contributor("omega", "application_gap", 0.5, {"domain": "ai"})])
    out = gather_contributors(None)
    types = {c.type for c in out}
    assert "learning_debt" not in types  # deterministic floor SUPPRESSED
    assert "application_gap" in types  # mind-derived reseed PRESERVED


def test_operator_contributor_survives_suppression(monkeypatch):
    import pressure_loop.pressure_field as pf

    # an operator field-reader contributor (source=='operator') is NEVER suppressed,
    # even though it rides the gather floor — operator supremacy is inviolable.
    monkeypatch.setattr(
        pf,
        "build_registry",
        lambda: {"learning_debt": lambda ss, **k: 0.29},
    )
    # inject an operator contributor as if a reader produced one
    pm.stage_reseed(
        [make_contributor("operator", "operator_command", 0.9, {"message": "go"})]
    )
    out = gather_contributors(None)
    types_by_source = {(c.source, c.type) for c in out}
    assert ("operator", "operator_command") in types_by_source  # kept
    assert all(c.type != "learning_debt" for c in out)  # det floor suppressed


def test_idle_quiet_evaluate_returns_none(monkeypatch):
    import pressure_loop.pressure_field as pf

    # only a deterministic reader would fire; suppressed -> empty -> no ignition (quiet)
    monkeypatch.setattr(pf, "build_registry", lambda: {"learning_debt": lambda ss, **k: 0.29})
    out = gather_contributors(None)
    assert all(c.type != "learning_debt" for c in out)
    ign = evaluate(out, active_episode=False, open_threshold=0.1, neuromod={})
    assert ign is None  # idle mind stays QUIET — no auto-ignition

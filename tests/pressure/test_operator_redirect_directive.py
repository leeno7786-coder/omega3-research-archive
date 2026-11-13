"""Operator REDIRECT/pivot directives — flag-gated classification + immediate trajectory seed.

A pivot directive ("Change of plan — focus on X") must (1) classify as ``operator_command`` so
it rides operator supremacy + the enqueue path and reaches Omega as ``operator_command``, and
(2) immediately seed ``LAST_OMEGA_TRAJECTORY`` (approach A: the directive TEXT as free-text
guidance META_A reads next cycle; Omega decomposes the real subcategory and supersedes at
window close).

Flag-gated by ``OMEGA_OPERATOR_REDIRECT`` (default OFF) -> byte-identical: a redirect stays
``phatic`` and seeds nothing, exactly as today.
"""

from __future__ import annotations

from core.shared_state import SharedState
from core.shared_state_keys import LAST_OMEGA_TRAJECTORY
from pressure_loop import operator_pressure as OP
from pressure_loop.episode import EpisodeStore
from pressure_loop.operator_chat import _chat_ingress_class, handle_operator_chat
from pressure_loop.runtime import PressureRuntime

_REDIRECTS = [
    "Change of plan — pause the CSS layout work and focus on WCAG accessibility compliance.",
    "switch to time-series forecasting",
    "pivot to the data pipeline now",
    "Actually, focus on the auth bug instead.",
    "drop the CSS work and start on accessibility",
    "new priority: get the deploy pipeline green",
]
_NON_REDIRECTS = [
    "What is Python?",
    "how's it going?",
    "learn python",
    "thanks, that's all for now",
    "run the test suite",
    "hello",
]


def _read_traj(shared_state):
    try:
        return shared_state.read(LAST_OMEGA_TRAJECTORY)
    except Exception:
        return None


# ── detection (pure) ──────────────────────────────────────────────────────────


def test_detect_redirect_directive_true_for_pivots():
    for t in _REDIRECTS:
        assert OP.detect_redirect_directive(t) is True, t


def test_detect_redirect_directive_false_for_non_pivots():
    for t in _NON_REDIRECTS:
        assert OP.detect_redirect_directive(t) is False, t


# ── classification gate ───────────────────────────────────────────────────────


def test_redirect_classifies_as_operator_command_when_enabled(monkeypatch):
    monkeypatch.setattr(OP, "OMEGA_OPERATOR_REDIRECT", True)
    assert _chat_ingress_class("Change of plan — focus on WCAG accessibility") == "operator_command"


def test_redirect_stays_phatic_when_disabled(monkeypatch):
    # Phase-D flipped the default ON; pin OFF to test the disabled (phatic) branch.
    monkeypatch.setattr(OP, "OMEGA_OPERATOR_REDIRECT", False)
    assert _chat_ingress_class("Change of plan — focus on WCAG accessibility") == "phatic"


# ── immediate trajectory seed (approach A) ────────────────────────────────────


def test_redirect_seeds_trajectory_immediately_when_enabled(monkeypatch):
    monkeypatch.setattr(OP, "OMEGA_OPERATOR_REDIRECT", True)
    runtime = PressureRuntime(shared_state=SharedState(), episode_store=EpisodeStore())
    text = "Change of plan — pause CSS layout and focus on WCAG accessibility compliance."
    result = handle_operator_chat(text, runtime=runtime)

    # classified as a command -> enqueued under supremacy, scripted ack, no model call
    assert result["queued"] is True
    assert result["route"]["opened_by"] == "operator_command"

    # approach A: the directive TEXT seeded the trajectory immediately as guidance
    traj = _read_traj(runtime.shared_state)
    assert isinstance(traj, dict)
    assert "WCAG" in (traj.get("target_capability") or "")
    assert "WCAG" in (traj.get("influencer_text") or "")


def test_non_redirect_command_does_not_seed(monkeypatch):
    # a plain learn command is operator_command but NOT a redirect -> no trajectory seed
    monkeypatch.setattr(OP, "OMEGA_OPERATOR_REDIRECT", True)
    runtime = PressureRuntime(shared_state=SharedState(), episode_store=EpisodeStore())
    result = handle_operator_chat("learn python", runtime=runtime)
    assert result["route"]["opened_by"] == "operator_command"
    assert not _read_traj(runtime.shared_state)

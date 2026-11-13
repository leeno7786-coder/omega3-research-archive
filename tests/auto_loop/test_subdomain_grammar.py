"""Phase 1 — deterministic subdomain grammar for all 12 lobes (RED → GREEN)."""

from __future__ import annotations

from auto_loop.mril_tools.subdomain_helpers import subdomain_for
from memory.dewey_map import SUBDOMAIN_MAP


def _y(lobe: int, name: str) -> int:
    return next(y for (x, y), n in SUBDOMAIN_MAP.items() if x == lobe and n == name)


# ── Lobe 3: reasoning ────────────────────────────────────────────────
def test_reasoning_goals():
    assert subdomain_for("what is my goal here", 3) == _y(3, "goals")


def test_reasoning_decisions():
    assert subdomain_for("why did we decide that", 3) == _y(3, "decisions")


def test_reasoning_constraints():
    assert subdomain_for("what are the constraints", 3) == _y(3, "constraints")


def test_reasoning_heuristics():
    assert subdomain_for("which heuristic applies", 3) == _y(3, "heuristics")


def test_reasoning_alignment():
    assert subdomain_for("is this aligned with our values", 3) == _y(3, "alignment")


def test_reasoning_no_signal_widens():
    assert subdomain_for("blizzard penguin teapot", 3) is None


# ── Lobe 4: situational (5W1H + outcome + sentiment) ─────────────────
def test_situational_who():
    assert subdomain_for("who was involved", 4) == _y(4, "who")


def test_situational_when():
    assert subdomain_for("when did that happen", 4) == _y(4, "when")


def test_situational_why():
    assert subdomain_for("why did it fail", 4) == _y(4, "why")


def test_situational_outcome_beats_interrogative():
    # outcome/sentiment are checked before bare interrogatives
    assert subdomain_for("what was the outcome", 4) == _y(4, "outcome")


def test_situational_sentiment():
    assert subdomain_for("how did it feel", 4) == _y(4, "sentiment")


def test_situational_no_signal_widens():
    assert subdomain_for("blizzard penguin teapot", 4) is None


# ── Lobe 5: tools (cognitive vs dev) ─────────────────────────────────
def test_tools_dev():
    assert subdomain_for("the git build pipeline tool", 5) == _y(5, "dev")


def test_tools_cognitive():
    assert subdomain_for("the web fetch tool", 5) == _y(5, "cognitive")


def test_tools_no_signal_widens():
    assert subdomain_for("blizzard penguin teapot", 5) is None


# ── Lobe 6: journal (single subdomain) ───────────────────────────────
def test_journal_always_deepsleep():
    assert subdomain_for("any journal text at all", 6) == _y(6, "deepsleep")
    assert subdomain_for("consolidation cascade", 6) == _y(6, "deepsleep")


def test_journal_empty_widens():
    assert subdomain_for("", 6) is None


# ── Lobe 7: vision ───────────────────────────────────────────────────
def test_vision_face():
    assert subdomain_for("recognize this face", 7) == _y(7, "face_embeddings")


def test_vision_scene():
    assert subdomain_for("describe the scene", 7) == _y(7, "scene_descriptions")


def test_vision_pattern():
    assert subdomain_for("a recurring visual pattern", 7) == _y(7, "visual_patterns")


def test_vision_image():
    assert subdomain_for("the uploaded image", 7) == _y(7, "images")


def test_vision_no_signal_widens():
    assert subdomain_for("blizzard penguin teapot", 7) is None


# ── Lobe 9: temporal ─────────────────────────────────────────────────
def test_temporal_session():
    assert subdomain_for("our last session", 9) == _y(9, "sessions")


def test_temporal_event():
    assert subdomain_for("what happened in that event", 9) == _y(9, "events")


def test_temporal_milestone():
    assert subdomain_for("a project milestone", 9) == _y(9, "milestones")


def test_temporal_history():
    assert subdomain_for("the history of this", 9) == _y(9, "history")


def test_temporal_no_signal_widens():
    assert subdomain_for("blizzard penguin teapot", 9) is None


# ── Lobe 11: dev ─────────────────────────────────────────────────────
def test_dev_artifacts():
    assert subdomain_for("the build artifact", 11) == _y(11, "artifacts")


def test_dev_progress():
    assert subdomain_for("progress update on the build", 11) == _y(11, "progress")


def test_dev_context():
    assert subdomain_for("dev context for this module", 11) == _y(11, "context")


def test_dev_vision():
    assert subdomain_for("the dev roadmap vision", 11) == _y(11, "vision")


def test_dev_full_tree():
    assert subdomain_for("what was in the directory", 11) == _y(11, "full_tree")


def test_dev_strings():
    assert subdomain_for("what strings did we read", 11) == _y(11, "strings")


def test_dev_operations():
    assert subdomain_for("which files did we touch", 11) == _y(11, "operations")


def test_dev_projects():
    assert subdomain_for("which project drive did we inspect", 11) == _y(11, "projects")


def test_dev_no_signal_widens():
    assert subdomain_for("blizzard penguin teapot", 11) is None


# ── Regex-boundary fixes (independent-review findings) ───────────────
def test_vision_pattern_requires_word_boundary():
    # \bpatterns?\b must NOT match a substring (patternsX / xpatterns) — widen instead
    assert subdomain_for("patternsx blob", 7) != _y(7, "visual_patterns")
    assert subdomain_for("xpatterns blob", 7) != _y(7, "visual_patterns")
    # a genuine pattern phrase still routes
    assert subdomain_for("a recurring visual pattern", 7) == _y(7, "visual_patterns")


def test_tools_bare_cd_does_not_route_to_dev():
    # bare "cd" must not false-positive into the dev subdomain
    assert subdomain_for("cd into the directory", 5) != _y(5, "dev")
    # genuine ci/cd + pipeline language still routes to dev
    assert subdomain_for("the ci/cd pipeline", 5) == _y(5, "dev")
    assert subdomain_for("a cicd job", 5) == _y(5, "dev")


# ── 12-lobe coverage: a representative query decides in every lobe ────
import pytest


@pytest.mark.parametrize(
    "lobe,text",
    [
        (0, "what is your identity"),
        (1, "what do you remember about me"),
        (2, "quantum entanglement"),
        (3, "what are the constraints"),
        (4, "what was the outcome"),
        (5, "the git build pipeline tool"),
        (6, "consolidation cascade"),
        (7, "recognize this face"),
        (8, "python async"),
        (9, "our last session"),
        (10, "code review workflow"),
        (11, "the build artifact"),
    ],
)
def test_every_lobe_has_a_deterministic_decision(lobe, text):
    result = subdomain_for(text, lobe)
    assert result is not None, f"lobe {lobe} failed to decide for {text!r}"
    assert (lobe, result) in SUBDOMAIN_MAP, f"({lobe},{result}) not a real subdomain"

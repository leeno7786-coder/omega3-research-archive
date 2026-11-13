"""Tests for `memory.enrichment.situational` (lobe 4 — 5W1H + outcome
+ sentiment, system self-awareness).

Covers the full event-recollection schema:
    facet ∈ SITUATIONAL_FACETS
    event_summary, success ∈ {True, False, None}, failure_reason,
    sentiment, intensity, linked_persons, linked_coding
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.dewey_map import SITUATIONAL_FACETS  # noqa: E402
from memory.enrichment.situational import enrich  # noqa: E402
import logging

log = logging.getLogger("omega.tests.memory.test_situational_enrichment")


def _ok(label: str) -> None:
    print(f"  [OK]   {label}")


def _fail(label: str) -> None:
    print(f"  [FAIL] {label}")
    sys.exit(1)


def test_failure_event():
    print("\n[1] failed event with explicit reason and frustration")
    out = enrich("the cycle failed because voice_box was unreachable, system felt frustrated")
    if out is None:
        _fail("enrich returned None - expected a record")
    if out["facet"] not in SITUATIONAL_FACETS:
        _fail(f"facet not in canonical set: {out['facet']!r}")
    _ok(f"facet picked: {out['facet']}")
    if out["success"] is not False:
        _fail(f"success should be False, got {out['success']!r}")
    _ok("success=False")
    if not out["failure_reason"]:
        _fail("failure_reason missing despite 'because' clause")
    _ok(f"failure_reason captured: {out['failure_reason'][:60]!r}")
    if out["sentiment"] != "frustrated":
        _fail(f"sentiment should be 'frustrated', got {out['sentiment']!r}")
    _ok("sentiment=frustrated")


def test_success_event():
    print("\n[2] successful event with confident sentiment")
    out = enrich("the meta dispatch succeeded and the system felt confident")
    if out is None:
        _fail("enrich returned None - expected a record")
    if out["success"] is not True:
        _fail(f"success should be True, got {out['success']!r}")
    _ok("success=True")
    if out["sentiment"] != "confident":
        _fail(f"sentiment should be 'confident', got {out['sentiment']!r}")
    _ok("sentiment=confident")
    if out["failure_reason"] is not None:
        _fail(f"failure_reason should be None on success, got {out['failure_reason']!r}")
    _ok("failure_reason=None on success")


def test_neutral_event():
    print("\n[3] facet picked, no explicit success/sentiment cues")
    out = enrich("the operator did this task during epoch 5")
    if out is None:
        _fail("enrich returned None")
    if out["facet"] not in SITUATIONAL_FACETS:
        _fail(f"facet invalid: {out['facet']!r}")
    _ok(f"facet picked: {out['facet']}")
    if out["success"] is not None:
        _fail(f"success should be None when undetermined, got {out['success']!r}")
    _ok("success=None when neither success nor failure cues present")
    if out["sentiment"] != "neutral":
        _fail(f"sentiment should default to 'neutral', got {out['sentiment']!r}")
    _ok("sentiment=neutral default")


def test_caller_overrides():
    print("\n[4] caller hints win over auto-detection")
    out = enrich(
        "anything",
        facet="outcome",
        success=True,
        sentiment="curious",
        intensity=0.9,
        linked_persons=["alice"],
        linked_coding=["python_web"],
    )
    if out is None:
        _fail("enrich returned None despite caller hint")
    if out["facet"] != "outcome":
        _fail(f"caller facet hint ignored: {out['facet']!r}")
    if out["success"] is not True:
        _fail(f"caller success hint ignored: {out['success']!r}")
    if out["sentiment"] != "curious":
        _fail(f"caller sentiment hint ignored: {out['sentiment']!r}")
    if out["intensity"] != 0.9:
        _fail(f"caller intensity hint ignored: {out['intensity']!r}")
    if out["linked_persons"] != ["alice"]:
        _fail(f"linked_persons not threaded: {out['linked_persons']!r}")
    if out["linked_coding"] != ["python_web"]:
        _fail(f"linked_coding not threaded: {out['linked_coding']!r}")
    _ok("all caller hints honored + linked_* arrays carried through")


def test_no_signal_returns_none():
    print("\n[5] no facet signal -> graceful None return")
    out = enrich("blah blah blah")
    if out is not None:
        _fail(f"expected None for no-signal text, got {out}")
    _ok("None on truly empty text - graceful degradation")


def main():
    print("=" * 60)
    print(" situational enrichment test")
    print("=" * 60)
    test_failure_event()
    test_success_event()
    test_neutral_event()
    test_caller_overrides()
    test_no_signal_returns_none()
    print("\n" + "=" * 60)
    print(" ALL PASS - situational enrichment schema verified")
    print("=" * 60)


if __name__ == "__main__":
    main()

"""_dominant_text must not repeat identical part texts (live 2026-07-02: operator build
objective was the request twice — target.message and the operator contributor carry
the same text)."""

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from meta_loop.intake.run import _dominant_text  # noqa: E402

REQ = "Rebuild the csv_stats project. Run the tests and confirm they all pass."


def _mi(target, contributors):
    return SimpleNamespace(
        action_goal={"target": target},
        pressure={"contributors": contributors},
    )


def test_identical_target_and_contributor_text_appears_once():
    mi = _mi({"message": REQ}, [{"metadata": {"message": REQ}}])
    assert _dominant_text(mi) == REQ


def test_distinct_parts_still_join_in_order():
    mi = _mi({"message": "alpha"}, [{"metadata": {"message": "beta"}}])
    assert _dominant_text(mi) == "alpha beta"


def test_duplicate_contributors_collapse():
    mi = _mi(None, [{"metadata": {"message": REQ}}, {"detail": {"message": REQ}}])
    assert _dominant_text(mi) == REQ

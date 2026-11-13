"""Phase 5b — per-domain memory hotness join (`hot_count_for_domain`).

The execution_gap practice bar must gate each learned-but-unapplied domain on
ITS OWN warmth, not a global aggregate. The join is coord_index (mid -> domain)
× StrengthIndex (mid -> is_hot). This module pins that join's behavior in
isolation: a fake StrengthIndex + a monkeypatched walker lookup, so no real
coord index / engram files are needed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import memory.engine.walker as walker  # noqa: E402
from memory import symphony_strength  # noqa: E402


class _FakeSI:
    """Minimal StrengthIndex stub: knows which mids are hot."""

    def __init__(self, hot_mids: set[str]) -> None:
        self._hot = set(hot_mids)
        self.seen_age: float | None = None

    def is_hot(self, mid: str, hot_age_seconds: float) -> bool:
        self.seen_age = hot_age_seconds
        return mid in self._hot


def _patch_domain_ids(monkeypatch, by_domain: dict[str, list[str]]) -> None:
    monkeypatch.setattr(walker, "load_coord_index", lambda: {})
    monkeypatch.setattr(
        walker,
        "list_ids_by_domain",
        lambda domain: list(by_domain.get(domain, [])),
    )


def test_counts_only_hot_memories_in_that_domain(monkeypatch):
    _patch_domain_ids(monkeypatch, {"rust": ["r1", "r2", "r3"]})
    si = _FakeSI(hot_mids={"r1", "r2"})  # r3 is cold
    assert symphony_strength.hot_count_for_domain("rust", strength_index=si) == 2


def test_zero_when_domain_is_cold(monkeypatch):
    _patch_domain_ids(monkeypatch, {"rust": ["r1", "r2"]})
    si = _FakeSI(hot_mids=set())  # nothing hot
    assert symphony_strength.hot_count_for_domain("rust", strength_index=si) == 0


def test_does_not_count_hot_memories_of_another_domain(monkeypatch):
    # The whole point of 5b: a hot python substrate must not make rust look warm.
    _patch_domain_ids(monkeypatch, {"python": ["p1", "p2"], "rust": ["r1"]})
    si = _FakeSI(hot_mids={"p1", "p2"})  # only python is hot
    assert symphony_strength.hot_count_for_domain("rust", strength_index=si) == 0
    assert symphony_strength.hot_count_for_domain("python", strength_index=si) == 2


def test_empty_domain_returns_zero_without_lookup(monkeypatch):
    called = []
    monkeypatch.setattr(walker, "load_coord_index", lambda: {})
    monkeypatch.setattr(
        walker,
        "list_ids_by_domain",
        lambda domain: called.append(domain) or [],
    )
    assert symphony_strength.hot_count_for_domain("", strength_index=_FakeSI(set())) == 0
    assert called == []  # no join attempted for an empty domain


def test_zero_when_strength_index_unwired(monkeypatch):
    _patch_domain_ids(monkeypatch, {"rust": ["r1"]})
    monkeypatch.setattr("memory.strength_index.ensure_strength_index", lambda: None)
    # No injected index → resolves the singleton, which is unwired → 0.
    assert symphony_strength.hot_count_for_domain("rust") == 0


def test_failure_soft_returns_zero(monkeypatch):
    monkeypatch.setattr(walker, "load_coord_index", lambda: {})

    def _boom(_domain):
        raise RuntimeError("coord index exploded")

    monkeypatch.setattr(walker, "list_ids_by_domain", _boom)
    assert symphony_strength.hot_count_for_domain("rust", strength_index=_FakeSI(set())) == 0


def test_uses_configured_hot_age_window(monkeypatch):
    _patch_domain_ids(monkeypatch, {"rust": ["r1"]})
    si = _FakeSI(hot_mids={"r1"})
    symphony_strength.hot_count_for_domain("rust", hot_age_seconds=42.0, strength_index=si)
    assert si.seen_age == 42.0

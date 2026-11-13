"""Mastery-as-subcat parent->child tree: a mind-created child subtopic ("async python")
is recorded UNDER its parent capability ("python coding") — child.parent set + child
linked into the parent's subdomains (navigable downward tree). Default-safe.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import deepsleep_loop.mastery_store as ms  # noqa: E402
from deepsleep_loop._deepsleep_state import MasteryRecord  # noqa: E402
import memory.subdomain_allocator as sa  # noqa: E402


@pytest.fixture
def store(monkeypatch):
    db: dict = {}
    monkeypatch.setattr(ms, "load_mastery", lambda *a, **k: dict(db))
    monkeypatch.setattr(ms, "upsert_mastery", lambda rec, **k: db.__setitem__(rec.domain_id, rec))
    return db


# ── the schema field ─────────────────────────────────────────────────────────
def test_parent_field_default_safe():
    assert MasteryRecord(domain_id="x", domain_name="x").parent == ""
    r = MasteryRecord(domain_id="async-python", domain_name="async python", parent="python-coding")
    assert MasteryRecord.from_dict(r.to_dict()).parent == "python-coding"
    # old record (no parent key) loads default-safe
    assert MasteryRecord.from_dict({"domain_id": "x", "domain_name": "x"}).parent == ""


# ── the allocator builds the tree ────────────────────────────────────────────
def test_seed_links_child_under_parent(store):
    sa._seed_mastery_gap("python coding")  # parent (top-level)
    sa._seed_mastery_gap("async python", parent="python coding")  # child under parent
    child = store.get("async-python")
    parent = store.get("python-coding")
    assert child is not None and child.parent == "python-coding"  # child -> parent
    assert "async-python" in (parent.subdomains or {})  # parent -> child (downward)
    assert parent.parent == ""  # parent is top-level


def test_seed_top_level_has_no_parent(store):
    sa._seed_mastery_gap("statistics")
    assert store["statistics"].parent == ""


def test_seed_missing_parent_is_failsoft(store):
    # naming a parent that doesn't exist yet: the child still records its parent slug,
    # the (absent) parent link is simply skipped — never blocks the child.
    sa._seed_mastery_gap("async python", parent="python coding")
    assert store["async-python"].parent == "python-coding"
    assert "python-coding" not in store  # parent not auto-created here

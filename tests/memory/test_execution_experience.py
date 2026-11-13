"""Execution-experience ledger — the mind remembers what it DID, and adapts.

Operator design 2026-07-08 ("teach the system how to remember what it does
and how to adapt and evolve from it"): every build outcome — verified
success or stopped-with-red-checks — is recorded per domain. On the NEXT
practice attempt the mint folds the past failures into a better execution
prompt (learn-from-failure), and the Omega packet surfaces the track record
during metacognition so the mind reasons over its own doing. This is
episodic memory of ACTION, sibling to the mastery store's memory of learning.
Day-zero clears it — a fresh mind has done nothing yet.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import memory.execution_experience as ee  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(ee, "_LEDGER_PATH", tmp_path / "execution_experience.json")
    ee._reset_cache_for_tests()
    yield
    ee._reset_cache_for_tests()


def test_record_and_recall_by_domain():
    ee.record_execution(
        domain="asyncio-basics",
        objective="build a rate limiter",
        outcome="stopped",
        red_reasons=["test_rate_limit: AssertionError 5 != 3"],
        rounds=3,
    )
    rows = ee.recent_for_domain("asyncio-basics")
    assert len(rows) == 1
    assert rows[0]["outcome"] == "stopped"
    assert "AssertionError" in rows[0]["red_reasons"][0]
    assert rows[0]["rounds"] == 3


def test_ledger_is_durable_across_reload():
    ee.record_execution(
        domain="caching", objective="LRU cache", outcome="success", red_reasons=[], rounds=2
    )
    ee._reset_cache_for_tests()  # simulate process restart (re-read from disk)
    rows = ee.recent_for_domain("caching")
    assert rows and rows[0]["outcome"] == "success"


def test_recall_is_bounded_and_newest_first():
    for i in range(ee.MAX_PER_DOMAIN + 5):
        ee.record_execution(
            domain="d",
            objective=f"attempt {i}",
            outcome="stopped",
            red_reasons=[f"fail {i}"],
            rounds=1,
        )
    rows = ee.recent_for_domain("d")
    assert len(rows) == ee.MAX_PER_DOMAIN
    assert rows[0]["objective"] == f"attempt {ee.MAX_PER_DOMAIN + 4}", "newest first"


def test_unknown_domain_recalls_empty():
    assert ee.recent_for_domain("never-built") == []
    assert ee.failure_guidance_for_domain("never-built") == ""


def test_failure_guidance_summarizes_recent_fails():
    ee.record_execution(
        domain="d", objective="v1", outcome="stopped", red_reasons=["no test file found"], rounds=2
    )
    ee.record_execution(
        domain="d",
        objective="v2",
        outcome="stopped",
        red_reasons=["test_x failed: timeout"],
        rounds=3,
    )
    guidance = ee.failure_guidance_for_domain("d")
    assert guidance, "past failures must produce guidance"
    assert "no test file found" in guidance
    assert "test_x failed: timeout" in guidance


def test_failure_guidance_empty_after_success():
    ee.record_execution(
        domain="d", objective="v1", outcome="stopped", red_reasons=["broke"], rounds=1
    )
    ee.record_execution(domain="d", objective="v2", outcome="success", red_reasons=[], rounds=1)
    # the domain was JUST conquered — do not nag the next build with old fails
    assert ee.failure_guidance_for_domain("d") == ""


def test_experience_summary_for_metacognition():
    ee.record_execution(
        domain="asyncio", objective="a", outcome="success", red_reasons=[], rounds=1
    )
    ee.record_execution(
        domain="asyncio", objective="b", outcome="stopped", red_reasons=["x"], rounds=2
    )
    ee.record_execution(
        domain="caching", objective="c", outcome="success", red_reasons=[], rounds=1
    )
    summary = ee.experience_summary()
    by_domain = {row["domain"]: row for row in summary}
    assert by_domain["asyncio"]["attempts"] == 2
    assert by_domain["asyncio"]["successes"] == 1
    assert by_domain["caching"]["successes"] == 1


def test_record_is_failure_soft_on_bad_input():
    # empty domain -> no-op, never raises
    ee.record_execution(domain="", objective="x", outcome="success", red_reasons=[], rounds=0)
    assert ee.experience_summary() == []

"""Step 8 (final) of the metacognitive pressure evolution — the SignalEvent audit ledger.

Record-only substrate (north-star #1): a structured §7.1 SignalEvent appended to
guardian/logs/signals.jsonl, behind SIGNALS_RECORD (default OFF = nothing written =
byte-identical). RECORD-ONLY (no consumer), failure-soft, off the hot tick. Two
producers wired (Omega result + mastery update); the rest deferred.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pressure_loop import signal_ledger as sl  # noqa: E402
from pressure_loop.signal_ledger import SignalEvent  # noqa: E402


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    p = tmp_path / "signals.jsonl"
    monkeypatch.setattr(sl, "_LEDGER_PATH", p)
    return p


def _on(monkeypatch):
    monkeypatch.setattr(sl, "SIGNALS_RECORD", True)


def _lines(p):
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── flag gate ────────────────────────────────────────────────────────────────
def test_flag_defaults_off():
    assert sl.SIGNALS_RECORD is True  # Phase-D flip: SIGNALS_RECORD ships ON


def test_flag_off_writes_nothing(ledger, monkeypatch):
    monkeypatch.setattr(sl, "SIGNALS_RECORD", False)  # Phase-D flip: pin OFF to test the off-branch
    sl.record(SignalEvent(source="x", kind="y", domain="d", valence=0.5))
    assert not ledger.exists()  # zero I/O, byte-identical


def test_flag_on_appends_one_jsonl_line(ledger, monkeypatch):
    _on(monkeypatch)
    sl.record(SignalEvent(source="omega_result", kind="outcome", episode_id="e1", domain="ai"))
    rows = _lines(ledger)
    assert len(rows) == 1
    r = rows[0]
    assert r["source"] == "omega_result" and r["kind"] == "outcome"
    assert r["episode_id"] == "e1" and r["domain"] == "ai"


# ── auto-fill + honest None-omission ─────────────────────────────────────────
def test_autofill_id_ts_episode(ledger, monkeypatch):
    _on(monkeypatch)
    # Phase 3 cut 2: the episode stamp is per-thread (lane-parallel cascades).
    from guardian.telemetry import writer as tw

    monkeypatch.setattr(tw._EPISODE_TLS, "episode_id", "ep-current", raising=False)
    sl.record(SignalEvent(source="s", kind="k"))  # no id/ts/episode
    r = _lines(ledger)[0]
    assert len(r["id"]) == 32 and int(r["id"], 16) >= 0  # uuid4 hex
    assert "T" in r["ts"]  # ISO8601
    assert r["episode_id"]  # filled from current_episode_id()


def test_optional_fields_omitted_when_none_not_fabricated(ledger, monkeypatch):
    _on(monkeypatch)
    sl.record(SignalEvent(source="s", kind="k"))  # all measured-quantity fields None
    r = _lines(ledger)[0]
    for k in ("domain", "valence", "confidence", "salience", "uncertainty", "cost"):
        assert k not in r  # honest: a null field is OMITTED, never written as 0.0


def test_measured_fields_are_written_when_present(ledger, monkeypatch):
    _on(monkeypatch)
    sl.record(SignalEvent(source="s", kind="k", valence=-1.0, salience=0.3, confidence=0.9))
    r = _lines(ledger)[0]
    assert r["valence"] == -1.0 and r["salience"] == 0.3 and r["confidence"] == 0.9


# ── failure-soft + record-only ───────────────────────────────────────────────
def test_failure_soft_never_raises(monkeypatch, tmp_path):
    _on(monkeypatch)
    # point the ledger at a DIRECTORY so open() fails -> must swallow, return None
    monkeypatch.setattr(sl, "_LEDGER_PATH", tmp_path)  # a dir, not a file
    assert sl.record(SignalEvent(source="s", kind="k")) is None  # no raise


def test_no_consumer_api():
    # record-only: the module exposes record + SignalEvent, NEVER a reader/decision API
    for forbidden in ("read", "read_signals", "resolve", "compose", "load_signals", "query"):
        assert not hasattr(sl, forbidden), f"signal_ledger must not expose {forbidden}"


# ── Producer 1: Omega result @ _apply_omega_episode_result ───────────────────
def test_omega_producer_emits_outcome(ledger, monkeypatch):
    _on(monkeypatch)
    from cognition_chain import _apply_omega_episode_result
    from omega_loop.episode.types import OmegaEpisodeResult

    ep = SimpleNamespace(global_={"outcome": {"artifacts": {}}, "omega_packet": {"domain": "ai"}})
    res = OmegaEpisodeResult(
        episode_id="e9",
        cognized=True,
        knowledge_to_ingest=(),
        mastery_effects=(),
        pressure_params={},
        response_text=None,
        synthesis=None,
        reflection=None,
        intent_decision={"intent": "APPLY", "confidence": 0.8},
    )
    _apply_omega_episode_result(ep, res)
    omega_rows = [r for r in _lines(ledger) if r["source"] == "omega_result"]
    assert len(omega_rows) == 1
    r = omega_rows[0]
    assert r["kind"] == "outcome" and r["episode_id"] == "e9" and r["domain"] == "ai"
    assert r["confidence"] == 0.8 and r["meta"]["prepared_intent"] == "APPLY"


def test_omega_producer_silent_flag_off(ledger, monkeypatch):
    monkeypatch.setattr(sl, "SIGNALS_RECORD", False)  # Phase-D flip: pin OFF to test the off-branch
    from cognition_chain import _apply_omega_episode_result
    from omega_loop.episode.types import OmegaEpisodeResult

    ep = SimpleNamespace(global_={"outcome": {"artifacts": {}}})
    res = OmegaEpisodeResult(
        episode_id="e",
        cognized=True,
        knowledge_to_ingest=(),
        mastery_effects=(),
        pressure_params={},
        response_text=None,
        synthesis=None,
        reflection=None,
    )
    _apply_omega_episode_result(ep, res)  # flag OFF
    assert not ledger.exists()


def test_omega_producer_failure_never_breaks_episode(ledger, monkeypatch):
    _on(monkeypatch)
    from cognition_chain import _apply_omega_episode_result
    from omega_loop.episode.types import OmegaEpisodeResult

    monkeypatch.setattr(sl, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    ep = SimpleNamespace(global_={"outcome": {"artifacts": {}}})
    res = OmegaEpisodeResult(
        episode_id="e",
        cognized=True,
        knowledge_to_ingest=(),
        mastery_effects=(),
        pressure_params={},
        response_text=None,
        synthesis=None,
        reflection=None,
    )
    _apply_omega_episode_result(ep, res)  # must not raise
    assert set(ep.global_.keys()) == {"outcome", "omega_result"}  # episode closed clean


# ── Producer 2: mastery update @ _apply_grounded_mastery ─────────────────────
def test_mastery_producer_emits_delta(ledger, monkeypatch):
    _on(monkeypatch)
    from deepsleep_loop import mastery as _m  # noqa: F401
    from deepsleep_loop.mastery import run as mrun
    from deepsleep_loop._deepsleep_state import MasteryRecord

    prior = MasteryRecord(domain_id="ai", domain_name="ai", knowledge_mastery=0.4)
    monkeypatch.setattr(mrun, "load_mastery", lambda *a, **k: {"ai": prior})
    monkeypatch.setattr(mrun, "upsert_mastery", lambda *a, **k: None)
    mrun._apply_grounded_mastery(
        domain_name="ai",
        effect={"knowledge_mastery": 0.6, "application_mastery": 0.2, "episode_id": "e1"},
        action_goal={"intent": "LEARN"},
        resolved_path="/tmp/x",
    )
    rows = [r for r in _lines(ledger) if r["source"] == "mastery_update"]
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "mastery_delta" and r["domain"] == "ai"
    assert r["valence"] == 1.0  # 0.6 > 0.4 -> positive
    assert r["salience"] == pytest.approx(0.2)  # |0.6-0.4|
    assert r["meta"]["delta"] == pytest.approx(0.2)


def test_mastery_upsert_still_fail_hard_on_signal_error(monkeypatch, tmp_path):
    # a signal-record failure must NOT break the (fail-hard) mastery upsert
    _on(monkeypatch)
    monkeypatch.setattr(sl, "_LEDGER_PATH", tmp_path)  # dir -> record() swallows internally
    from deepsleep_loop.mastery import run as mrun
    from deepsleep_loop._deepsleep_state import MasteryRecord

    prior = MasteryRecord(domain_id="ai", domain_name="ai", knowledge_mastery=0.4)
    monkeypatch.setattr(mrun, "load_mastery", lambda *a, **k: {"ai": prior})
    upserted = {}
    monkeypatch.setattr(mrun, "upsert_mastery", lambda rec, **k: upserted.update({"r": rec}))
    out = mrun._apply_grounded_mastery(
        domain_name="ai",
        effect={"knowledge_mastery": 0.6, "application_mastery": 0.2},
        action_goal={},
        resolved_path="/tmp/x",
    )
    assert (
        out.knowledge_mastery == 0.6 and upserted["r"].knowledge_mastery == 0.6
    )  # upsert happened

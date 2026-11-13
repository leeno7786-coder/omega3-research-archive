"""Terminal console subscriber tests."""

from __future__ import annotations

import logging
from unittest.mock import patch

import pytest

from guardian import terminal_console as tc
from guardian import terminal_feedback as tf


@pytest.fixture(autouse=True)
def _isolate():
    tf._reset_recent_events_ring_for_tests()
    tc.reset_for_tests()
    yield
    tf._reset_recent_events_ring_for_tests()
    tc.reset_for_tests()


def test_format_phase_event():
    line = tc.format_event_line(
        {
            "kind": "phase",
            "loop": "pressure",
            "phase": "evaluate",
            "status": "ok",
            "latency_ms": 42,
            "episode_id": "episode_abc123",
            "summary": "threshold crossed",
        }
    )
    assert line == "pressure/evaluate PHASE ok (42ms) ep=episode_ | threshold crossed"


def test_format_inference_event():
    line = tc.format_event_line(
        {
            "kind": "inference",
            "loop": "auto",
            "operation": "generate",
            "status": "ok",
            "latency_ms": 1200,
            "tokens": 88,
            "ok": True,
            "episode_id": "episode_xyz",
        }
    )
    assert "auto/generate INFERENCE ok (1200ms) ep=episode_" in line
    assert "op=generate" in line
    assert "tokens=88" in line


def test_install_subscribes_and_logs(caplog):
    caplog.set_level(logging.INFO, logger="omega.telemetry.terminal")
    with patch.dict("os.environ", {"OMEGA_TERMINAL_TELEMETRY": "1"}, clear=False):
        tc.install()
        tf.emit_phase("pressure", "evaluate", status="ok", summary="done")
    assert any("pressure/evaluate PHASE ok" in rec.message for rec in caplog.records)


def test_disabled_by_env(caplog):
    caplog.set_level(logging.INFO, logger="omega.telemetry.terminal")
    with patch.dict("os.environ", {"OMEGA_TERMINAL_TELEMETRY": "0"}, clear=False):
        tc.install()
        tf.emit_phase("loop", "phase", status="ok")
    assert not any("PHASE" in rec.message for rec in caplog.records)


def test_no_enter_filter(caplog):
    caplog.set_level(logging.INFO, logger="omega.telemetry.terminal")
    env = {
        "OMEGA_TERMINAL_TELEMETRY": "1",
        "OMEGA_TERMINAL_TELEMETRY_FILTER": "no_enter",
    }
    with patch.dict("os.environ", env, clear=False):
        tc.install()
        tf.emit_phase("loop", "phase", status="enter")
        tf.emit_phase("loop", "phase", status="ok", summary="done")
    messages = [rec.message for rec in caplog.records]
    assert not any("PHASE enter" in msg for msg in messages)
    assert any("PHASE ok" in msg for msg in messages)


def test_install_idempotent():
    with patch.dict("os.environ", {"OMEGA_TERMINAL_TELEMETRY": "1"}, clear=False):
        tc.install()
        tc.install()
        assert tc._INSTALLED is True


def test_format_phase_renders_cognition_fields():
    """Curated cognition fields (weights/dominant/agg/margin/...) reach the line."""
    line = tc.format_event_line(
        {
            "kind": "phase",
            "loop": "pressure",
            "phase": "evaluate",
            "status": "ok",
            "latency_ms": 1,
            "episode_id": "ep1",
            "dominant": "learning_debt",
            "agg": 0.6088,
            "margin": "12:1",
            "weights": {"learning_debt": 0.52, "curiosity_gap": 0.18},
        }
    )
    assert "dominant=learning_debt" in line
    assert "agg=0.609" in line  # compact float formatting
    assert "margin=12:1" in line
    assert "weights: learning_debt=0.52 curiosity_gap=0.18" in line


def test_format_data_renders_cognition_fields():
    line = tc.format_event_line(
        {
            "kind": "data",
            "system": "global",
            "phase": "action_goal",
            "status": "ok",
            "intent": "IDLE",
            "opened_by": "idle_probe_aggregate",
        }
    )
    assert "intent=IDLE" in line
    assert "opened_by=idle_probe_aggregate" in line


def test_format_phase_without_cognition_unchanged():
    """A plain phase (summary only, no cognition keys) stays byte-identical."""
    line = tc.format_event_line(
        {
            "kind": "phase",
            "loop": "pressure",
            "phase": "evaluate",
            "status": "ok",
            "latency_ms": 42,
            "episode_id": "episode_abc123",
            "summary": "threshold crossed",
        }
    )
    assert line == "pressure/evaluate PHASE ok (42ms) ep=episode_ | threshold crossed"


def test_install_persists_terminal_stream_to_file(tmp_path):
    """Rendered terminal telemetry is appended to a single file for later review."""
    log_path = tmp_path / "terminal.log"
    prev = getattr(tc, "TERMINAL_LOG_PATH", None)
    tc.TERMINAL_LOG_PATH = log_path
    try:
        with patch.dict("os.environ", {"OMEGA_TERMINAL_TELEMETRY": "1"}, clear=False):
            tc.install()
            tf.emit_phase("global", "intent", status="ok", episode_id="ep-file-1", intent="LEARN")
            tf.emit_phase("auto", "execute", status="ok", episode_id="ep-file-1", summary="done")
            for h in list(tc.log.handlers):
                h.flush()
        assert log_path.exists(), "terminal log file must be created on install"
        text = log_path.read_text(encoding="utf-8")
        assert "global/intent PHASE ok" in text
        assert "intent=LEARN" in text
        assert "auto/execute PHASE ok" in text
    finally:
        tc.TERMINAL_LOG_PATH = prev


def test_capability_evaluate_routes_to_infra_not_terminal(caplog, tmp_path):
    """Bulk runtime probes belong in infra.log, not the cognition terminal."""
    caplog.set_level(logging.INFO)
    term_path = tmp_path / "terminal.log"
    infra_path = tmp_path / "infra.log"
    prev_term = tc.TERMINAL_LOG_PATH
    prev_infra = tc.INFRA_LOG_PATH
    tc.TERMINAL_LOG_PATH = term_path
    tc.INFRA_LOG_PATH = infra_path
    try:
        with patch.dict("os.environ", {"OMEGA_TERMINAL_TELEMETRY": "1"}, clear=False):
            tc.install()
            tf.emit_data(
                "runtime",
                "capability_evaluate",
                capability="embedding",
                state="available",
                available=True,
            )
            tf.emit_phase(
                "pressure",
                "evaluate",
                status="ok",
                episode_id="ep-1",
                dominant="curiosity_gap",
                agg=0.72,
            )
            for h in list(tc.log.handlers):
                h.flush()
            for h in list(tc.infra_log.handlers):
                h.flush()
        term_text = term_path.read_text(encoding="utf-8") if term_path.exists() else ""
        infra_text = infra_path.read_text(encoding="utf-8") if infra_path.exists() else ""
        assert "capability_evaluate" not in term_text
        assert "pressure/evaluate" in term_text
        assert "capability_evaluate" in infra_text
    finally:
        tc.TERMINAL_LOG_PATH = prev_term
        tc.INFRA_LOG_PATH = prev_infra


def test_pressure_bookkeeping_heartbeat_routes_to_infra_not_terminal(tmp_path):
    """Operator 2026-07-21: the routine pressure bookkeeping rows (the `tick`
    wrapper phase + the `pressure_tick_summary` data row) carry no decision
    content and must NOT cycle on the cognition terminal. They go to infra.log
    (durable trail) instead. The decision-bearing pressure phases (gather /
    evaluate / ignite_episode) and any FAILURE still surface on the terminal."""
    term_path = tmp_path / "terminal.log"
    infra_path = tmp_path / "infra.log"
    prev_term = tc.TERMINAL_LOG_PATH
    prev_infra = tc.INFRA_LOG_PATH
    tc.TERMINAL_LOG_PATH = term_path
    tc.INFRA_LOG_PATH = infra_path
    try:
        with patch.dict("os.environ", {"OMEGA_TERMINAL_TELEMETRY": "1"}, clear=False):
            tc.install()
            # Bookkeeping heartbeat — infra only.
            tf.emit_phase("pressure", "tick", status="ok", tick=7)
            tf.emit_data("pressure", "pressure_tick_summary", tick=7, ignited=False)
            # Operator 2026-09-14: the per-tick gather/gate/neuromod rows print whether or
            # not anything ignites, so they are heartbeat too.
            tf.emit_data("pressure", "ignition_gate", summary="top=-:0.000 -> below_ceiling")
            tf.emit_data("pressure", "reseed_staging", summary="staged=0 suppressed=none")
            tf.emit_guardian_event("omega.pressure.loop", "gather_contributors_snapshot", contributors=[])
            tf.emit_guardian_event("omega.pressure.loop", "neuromod_tonic_transition", reason="idle_tick_decay")
            # Decision-bearing + ignition — terminal.
            tf.emit_phase(
                "pressure",
                "evaluate",
                status="ok",
                episode_id="ep-1",
                dominant="curiosity_gap",
                agg=0.72,
            )
            tf.emit_phase(
                "pressure",
                "ignite_episode",
                status="ok",
                episode_id="ep-1",
                opened_by="operator_command",
            )
            # A FAILED tick is not routine bookkeeping — it must surface.
            tf.emit_phase("pressure", "tick", status="fail", tick=8, error="cascade exploded")
            for h in list(tc.log.handlers):
                h.flush()
            for h in list(tc.infra_log.handlers):
                h.flush()
        term_text = term_path.read_text(encoding="utf-8") if term_path.exists() else ""
        infra_text = infra_path.read_text(encoding="utf-8") if infra_path.exists() else ""
        # heartbeat OFF the terminal, ON in infra
        assert "pressure/tick PHASE ok" not in term_text
        assert "pressure_tick_summary" not in term_text
        assert "pressure/tick PHASE ok" in infra_text
        assert "pressure_tick_summary" in infra_text
        for phase in ("ignition_gate", "reseed_staging", "gather_contributors_snapshot", "neuromod_tonic_transition"):
            assert phase not in term_text
            assert phase in infra_text
        # decision + ignition + failure stay on the terminal
        assert "pressure/evaluate" in term_text
        assert "pressure/ignite_episode" in term_text
        assert "pressure/tick PHASE fail" in term_text
    finally:
        tc.TERMINAL_LOG_PATH = prev_term
        tc.INFRA_LOG_PATH = prev_infra


def test_legacy_all_filter_prints_everything(caplog):
    caplog.set_level(logging.INFO, logger="omega.telemetry.terminal")
    env = {
        "OMEGA_TERMINAL_TELEMETRY": "1",
        "OMEGA_TERMINAL_TELEMETRY_FILTER": "all",
    }
    with patch.dict("os.environ", env, clear=False):
        tc.install()
        tf.emit_data("runtime", "capability_evaluate", capability="face")
    messages = [rec.message for rec in caplog.records]
    assert any("capability_evaluate" in msg for msg in messages)


def test_terminal_file_appends_across_installs(tmp_path):
    """The file is append-mode — a later run's lines join the earlier ones."""
    log_path = tmp_path / "terminal.log"
    prev = getattr(tc, "TERMINAL_LOG_PATH", None)
    tc.TERMINAL_LOG_PATH = log_path
    try:
        with patch.dict("os.environ", {"OMEGA_TERMINAL_TELEMETRY": "1"}, clear=False):
            tc.install()
            tf.emit_phase("pressure", "evaluate", status="ok", summary="first")
            for h in list(tc.log.handlers):
                h.flush()
            tc.reset_for_tests()
            tc.install()
            tf.emit_phase("pressure", "evaluate", status="ok", summary="second")
            for h in list(tc.log.handlers):
                h.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "first" in text and "second" in text
    finally:
        tc.TERMINAL_LOG_PATH = prev

"""Loop phase data logger — guardian/logs JSONL persistence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guardian import loop_phase_logger as lpl
from guardian import terminal_feedback as tf
from guardian.telemetry import writer


@pytest.fixture
def log_dir(tmp_path, monkeypatch):
    import guardian.data_logger as dl

    monkeypatch.setattr(dl, "_LOG_DIR", tmp_path)
    dl._file_locks.clear()
    return tmp_path


def test_log_loop_phase_writes_before_and_after(log_dir):
    lpl.log_loop_phase(
        hook="before",
        loop="meta",
        phase="intake",
        status="enter",
        episode_id="ep-1",
    )
    lpl.log_loop_phase(
        hook="after",
        loop="meta",
        phase="intake",
        status="ok",
        episode_id="ep-1",
        latency_ms=12,
    )
    path = log_dir / "meta.jsonl"
    assert path.is_file()
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(rows) == 2
    assert rows[0]["event"] == "phase_before"
    assert rows[0]["data"]["phase"] == "intake"
    assert rows[1]["event"] == "phase_after"
    assert rows[1]["data"]["latency_ms"] == 12


def test_phase_span_mirrors_to_guardian_logs(log_dir, monkeypatch):
    events = []
    monkeypatch.setattr(tf, "_publish_event", lambda e: events.append(e))
    with tf.phase_span("global", "intent", episode_id="ep-2") as span:
        span.set(intent="USE_SKILL")
    path = log_dir / "global.jsonl"
    assert path.is_file()
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert rows[0]["event"] == "phase_before"
    assert rows[1]["event"] == "phase_after"
    assert rows[1]["data"]["intent"] == "USE_SKILL"


def test_emit_data_mirrors_loop_payload_to_guardian_logs(log_dir, monkeypatch):
    monkeypatch.setattr(tf, "_publish_event", lambda _e: None)
    tf.emit_data(
        "auto",
        "auto_outcome_pack",
        episode_id="ep-3",
        summary="status=success",
        status="ok",
    )
    path = log_dir / "auto.jsonl"
    rows = [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["event"] == "data_auto_outcome_pack"
    assert rows[0]["data"]["episode_id"] == "ep-3"


def test_emit_data_writes_one_telemetry_and_one_failure_record(log_dir, monkeypatch, tmp_path):
    monkeypatch.setattr(writer, "TELEMETRY_DIR", tmp_path / "telemetry")
    monkeypatch.setattr(writer, "FAILURE_LOG", tmp_path / "failures.jsonl")
    writer._LOCKS.clear()

    tf.emit_data(
        "auto",
        "auto_outcome_pack",
        episode_id="ep-4",
        summary="status=failed",
        status="fail",
        ok=False,
        failure_kind="skill_files_changed_exceeded",
    )

    telemetry = [
        json.loads(line)
        for line in (tmp_path / "telemetry" / "auto.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    failures = [
        json.loads(line)
        for line in (tmp_path / "failures.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(telemetry) == 1
    assert len(failures) == 1
    assert telemetry[0]["ts"].endswith("Z")
    assert failures[0]["status"] == "fail"
    assert failures[0]["ok"] is False


def test_emit_guardian_event_failure_has_consistent_status(monkeypatch, tmp_path):
    monkeypatch.setattr(writer, "TELEMETRY_DIR", tmp_path / "telemetry")
    monkeypatch.setattr(writer, "FAILURE_LOG", tmp_path / "failures.jsonl")
    writer._LOCKS.clear()

    tf.emit_guardian_event(
        "omega.runtime.model_control",
        "unload_model",
        status="fail",
        ok=False,
        model="gemma_e4b",
    )

    rows = [
        json.loads(line)
        for line in (tmp_path / "telemetry" / "runtime.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    assert len(rows) == 1
    assert rows[0]["status"] == "fail"
    assert rows[0]["ok"] is False

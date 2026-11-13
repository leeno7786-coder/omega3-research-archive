"""T12: every stop path reports whether the operator's stop was made durable.

Cross-lane cognitive integration spec, section 11. The unambiguous stop is a host-owned halt,
and it lands whatever the ledger does — but a stop whose durable record failed must not be
reported as a plain success: after a restart, work continuing from that turn could resume.
Telemetry is not the operator's answer (review 2026-09-18), so the HUD's own stop routes carry
the outcome. RED-first against the live FastAPI routes.
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hud.server import create_app  # noqa: E402

JOB = "build"
JOB_CYCLE = "bubble-build"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    from auto_loop.execute import job_control as jobs
    from pressure_loop import cognition_queue as cq
    from pressure_loop import operator_chat_responses as replies
    from pressure_loop import pressure

    monkeypatch.setattr(cq, "_queue_path", lambda: tmp_path / "queue.json")
    cq._reset_cache_for_tests()
    monkeypatch.setattr(pressure, "_successor_path", lambda: tmp_path / "successors.json")
    monkeypatch.setattr(pressure, "_RESEED_STAGING", {})
    pressure._UNRESOLVED_STOPS.clear()
    project = tmp_path / "projects" / JOB
    project.mkdir(parents=True, exist_ok=True)
    jobs.open_job(JOB, JOB_CYCLE, "build a todo app", tmp_path, project_root=project)
    yield
    pressure._UNRESOLVED_STOPS.clear()
    replies.clear_work_halt(JOB_CYCLE)
    cq.clear_inputs()


def _unwritable_ledger(monkeypatch):
    from pressure_loop import pressure

    monkeypatch.setattr(pressure, "_write_successors",
                        lambda _data: (_ for _ in ()).throw(OSError("ledger unwritable")))


@pytest.mark.parametrize("route, payload", [
    ("/api/chat/interrupt", {"cycle_id": JOB_CYCLE}),
    ("/api/agent/halt", {"cycle_ids": [JOB_CYCLE]}),
], ids=["stop-control", "esc"])
def test_a_stop_route_reports_a_durable_record(route, payload):
    from auto_loop.execute import job_control as jobs

    response = TestClient(create_app()).post(route, json=payload)

    body = response.json()
    assert response.status_code == 200 and body["ok"] is True
    assert body["stopped_jobs"] == [JOB]
    assert body["stop_recorded"] is True and body["unresolved_stops"] == []
    assert [row.kind for row in jobs.get_job(JOB).revisions] == ["stop"]


@pytest.mark.parametrize("route, payload", [
    ("/api/chat/interrupt", {"cycle_id": JOB_CYCLE}),
    ("/api/agent/halt", {"cycle_ids": [JOB_CYCLE]}),
], ids=["stop-control", "esc"])
def test_a_stop_route_reports_a_record_that_could_not_be_written(route, payload, monkeypatch):
    from auto_loop.execute import job_control as jobs
    from pressure_loop import pressure

    _unwritable_ledger(monkeypatch)

    response = TestClient(create_app()).post(route, json=payload)

    body = response.json()
    assert response.status_code == 200, "the halt itself still lands"
    assert body["stopped_jobs"] == [JOB]
    assert [row.kind for row in jobs.get_job(JOB).revisions] == ["stop"]
    assert body["stop_recorded"] is False
    assert JOB_CYCLE in body["unresolved_stops"]
    assert pressure.unresolved_stops(), "retained in memory and retried"

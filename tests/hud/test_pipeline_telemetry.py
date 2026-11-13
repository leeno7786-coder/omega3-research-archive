"""The HUD reports the four physical execution pipelines."""

from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from core import pipeline_state
from hud import telemetry
import hud.server as hud_server
from pressure_loop import cognition_queue as cq, pressure
from pressure_loop.contributors import make_contributor


class _Lifecycle:
    plugin_registry = None

    def close(self) -> None:
        pass


def test_queued_work_has_no_fabricated_physical_assignment():
    cq.enqueue_input(
        make_contributor("operator", "operator_query", 1, {"cycle_id": "queued"}),
        operator_activity=False,
    )
    snapshot = telemetry.pipeline_telemetry()
    assert snapshot["queue_depth"] == 1
    assert set(snapshot["pipelines"]) == {"0", "1", "2", "3"}
    assert all(row["state"] == "free" for row in snapshot["pipelines"].values())
    assert all(row["pipeline_id"] in range(4) for row in snapshot["pipelines"].values())
    assert not ({"chat", "agent", "meta", "window"} & set(snapshot["pipelines"]))


def test_running_pipeline_exposes_its_physical_correlation():
    cq.enqueue_input(
        make_contributor(
            "omega", "lane_work_dispatch", 1,
            {"lane": "agent", "cycle_id": "cycle-a", "job_id": "job-a"},
        ),
        operator_activity=False,
    )
    contributor = cq.lease_next_input(2)
    ignition = replace(
        pressure.evaluate([contributor], active_episode=False),
        pipeline_id=2,
        work_kind="operator_lane_work",
    )
    pipeline_state.mark_ready(2, ignition)
    snapshot = telemetry.pipeline_telemetry()
    row = snapshot["pipelines"]["2"]
    assert row["state"] == "running"
    assert row["pipeline_id"] == 2
    assert row["work_kind"] == "operator_lane_work"
    assert row["cycle_id"] == "cycle-a"
    assert row["job_id"] == "job-a"
    assert {"queue_id", "phase", "active_episode_id", "elapsed_s"} <= row.keys()


def test_pipeline_route_replaces_lane_route(monkeypatch):
    # TestClient runs real lifespan hooks. Keep warmups inside this test; they
    # must never contact a live inference/speech seat or rebuild live anchors.
    monkeypatch.setattr("runtime.tts.warmup", lambda: None)
    monkeypatch.setattr("core.face_recall_warmup.warm_anchor_store", lambda: None)
    monkeypatch.setattr("httpx.post", lambda *args, **kwargs: SimpleNamespace(status_code=200))
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: _Lifecycle()
    )
    with TestClient(hud_server.create_app()) as client:
        assert client.get("/api/telemetry/pipelines").status_code == 200
        assert client.get("/api/telemetry/lanes").status_code == 404

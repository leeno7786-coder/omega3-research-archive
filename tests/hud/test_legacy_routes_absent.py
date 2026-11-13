from __future__ import annotations

from fastapi.testclient import TestClient

from hud.server import create_app


def test_native_connector_and_workspace_routes_are_not_registered() -> None:
    client = TestClient(create_app())
    for method, path in (
        ("get", "/api/connectors/status"),
        ("post", "/api/workspace/onboarding/start"),
        ("get", "/api/workspace/alignment"),
        ("post", "/api/connectors/google/app_config"),
        ("post", "/api/connectors/google_workspace/connect"),
    ):
        response = getattr(client, method)(path, **({"json": {}} if method == "post" else {}))
        assert response.status_code == 404, path


def test_retired_plugin_routes_are_not_registered() -> None:
    response = TestClient(create_app()).get("/api/plugins/featured")
    assert response.status_code == 404

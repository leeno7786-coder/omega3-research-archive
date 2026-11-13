from __future__ import annotations

import json

from fastapi.testclient import TestClient


def test_mind_connector_api_persists_generic_cloud_config_without_plaintext_key(
    tmp_path, monkeypatch
) -> None:
    from hud import server
    from runtime import brain_cloud_config

    monkeypatch.setattr(brain_cloud_config, "_config_path", lambda: tmp_path / "mind.json")
    monkeypatch.setattr(
        brain_cloud_config,
        "_connector_store_root",
        lambda: tmp_path / "connect",
    )
    monkeypatch.delenv("OMEGA_MIND_BACKEND", raising=False)
    monkeypatch.setenv("OMEGA_CONNECT_SECRET", "test-connector-secret")
    client = TestClient(server.create_app())

    response = client.post(
        "/api/settings/mind-backend",
        json={
            "backend": "cloud",
            "provider": "custom",
            "model": "operator-model",
            "base_url": "https://mind.example.test/v1",
            "api_key": "do-not-store-plaintext",
        },
    )

    assert response.status_code == 200
    connector = response.json()["connector"]
    assert connector["backend"] == "cloud"
    assert connector["provider"] == "custom"
    assert connector["has_key"] is True
    assert "do-not-store-plaintext" not in (tmp_path / "mind.json").read_text(encoding="utf-8")
    secret_path = tmp_path / "connect" / "omega-connect.secrets.json"
    assert secret_path.is_file()
    assert "do-not-store-plaintext" not in secret_path.read_text(encoding="utf-8")

    loaded = json.loads((tmp_path / "mind.json").read_text(encoding="utf-8"))
    assert loaded["backend"] == "cloud"
    assert brain_cloud_config.descriptor()["api_key"] == "do-not-store-plaintext"


def test_mind_connector_api_reports_local_selection_without_a_model_default(tmp_path, monkeypatch):
    from hud import server
    from runtime import brain_cloud_config

    monkeypatch.setattr(brain_cloud_config, "_config_path", lambda: tmp_path / "mind.json")
    client = TestClient(server.create_app())

    response = client.post(
        "/api/settings/mind-backend",
        json={"backend": "local", "local_model": "operator-supplied-model"},
    )

    assert response.status_code == 200
    connector = response.json()["connector"]
    assert connector["backend"] == "local"
    assert connector["local_model"] == "operator-supplied-model"
    assert connector["has_key"] is False
    assert brain_cloud_config.descriptor() is None


def test_cloud_provider_catalog_route_excludes_local_seats() -> None:
    from hud import server

    client = TestClient(server.create_app())
    response = client.get("/api/settings/cloud-providers")

    assert response.status_code == 200
    providers = response.json()["providers"]
    assert len(providers) >= 20
    assert all(provider["id"] not in {"lmstudio", "ollama"} for provider in providers)
    assert all('"api_key":' not in str(provider) for provider in providers)


def test_cloud_provider_test_route_discovers_models_without_persisting_key(
    tmp_path, monkeypatch
) -> None:
    from hud import server
    from runtime import brain_cloud_config, cloud_providers

    monkeypatch.setattr(brain_cloud_config, "_config_path", lambda: tmp_path / "mind.json")
    monkeypatch.setattr(
        cloud_providers,
        "discover_cloud_models",
        lambda provider, api_key, custom_base_url=None: [
            {"id": "operator-model", "name": "Operator Model"}
        ],
    )
    client = TestClient(server.create_app())

    response = client.post(
        "/api/settings/mind-backend/test",
        json={
            "provider": "mistral",
            "model": "operator-model",
            "api_key": "do-not-persist",
        },
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "provider": "mistral",
        "models": [{"id": "operator-model", "name": "Operator Model"}],
    }
    assert not (tmp_path / "mind.json").exists()
    assert "do-not-persist" not in response.text


def test_cloud_api_key_save_route_persists_provider_scoped_key_without_exposing_it(
    tmp_path, monkeypatch
) -> None:
    from hud import server
    from runtime import brain_cloud_config

    monkeypatch.setattr(brain_cloud_config, "_config_path", lambda: tmp_path / "mind.json")
    monkeypatch.setattr(
        brain_cloud_config,
        "_connector_store_root",
        lambda: tmp_path / "connect",
    )
    monkeypatch.setenv("OMEGA_CONNECT_SECRET", "test-connector-secret")
    client = TestClient(server.create_app())

    selected = client.post(
        "/api/settings/mind-backend",
        json={
            "backend": "cloud",
            "provider": "mistral",
            "model": "mistral-small-latest",
        },
    )
    assert selected.status_code == 200

    response = client.post(
        "/api/settings/mind-backend/key",
        json={"provider": "mistral", "api_key": "mistral-save-only-key"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "provider": "mistral",
        "saved": True,
        "saved_key_providers": ["mistral"],
    }
    assert "mistral-save-only-key" not in response.text

    response = client.get("/api/settings/mind-backend")
    assert response.status_code == 200
    connector = response.json()["connector"]
    assert connector["has_key"] is True
    assert connector["saved_key_providers"] == ["mistral"]


def test_cloud_provider_test_route_reuses_saved_provider_key_when_key_is_omitted(
    tmp_path, monkeypatch
) -> None:
    from hud import server
    from runtime import brain_cloud_config, cloud_providers

    monkeypatch.setattr(brain_cloud_config, "_config_path", lambda: tmp_path / "mind.json")
    monkeypatch.setattr(
        brain_cloud_config,
        "_connector_store_root",
        lambda: tmp_path / "connect",
    )
    monkeypatch.setenv("OMEGA_CONNECT_SECRET", "test-connector-secret")
    client = TestClient(server.create_app())
    saved = client.post(
        "/api/settings/mind-backend/key",
        json={"provider": "mistral", "api_key": "mistral-test-key"},
    )
    assert saved.status_code == 200

    captured = {}

    def discover(provider, api_key, custom_base_url=None):
        captured.update(
            provider=provider,
            api_key=api_key,
            custom_base_url=custom_base_url,
        )
        return [{"id": "mistral-small-latest", "name": "Mistral Small"}]

    monkeypatch.setattr(cloud_providers, "discover_cloud_models", discover)
    response = client.post(
        "/api/settings/mind-backend/test",
        json={
            "provider": "mistral",
            "base_url": "https://api.mistral.ai/v1",
            "model": "mistral-small-latest",
        },
    )

    assert response.status_code == 200
    assert captured == {
        "provider": "mistral",
        "api_key": "mistral-test-key",
        "custom_base_url": "https://api.mistral.ai/v1",
    }
    assert "mistral-test-key" not in response.text

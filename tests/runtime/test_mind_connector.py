from __future__ import annotations

from runtime import brain_cloud_config
from runtime.mind_connector import MindConnector


def test_connector_contract_accepts_an_unauthenticated_openai_compatible_seat() -> None:
    connector = MindConnector.from_mapping(
        {
            "backend": "cloud",
            "provider": "local-openai-server",
            "model": "operator-model",
            "base_url": "http://127.0.0.1:9000/v1",
        }
    )

    assert connector.public_dict()["provider"] == "local-openai-server"
    assert connector.with_api_key("")["api_key"] == ""


def test_connector_contract_rejects_an_incomplete_cloud_seat() -> None:
    try:
        MindConnector.from_mapping(
            {"backend": "cloud", "provider": "operator", "base_url": "not-a-url"}
        )
    except ValueError as exc:
        assert str(exc) in {"mind_connector_base_url_invalid", "mind_connector_identity_missing"}
    else:
        raise AssertionError("incomplete cloud connector was accepted")


def test_mind_backend_can_explicitly_select_local_without_a_cloud_descriptor(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(brain_cloud_config, "_config_path", lambda: tmp_path / "mind.json")
    monkeypatch.delenv("OMEGA_MIND_BACKEND", raising=False)
    monkeypatch.setenv("OMEGA_MIND_BACKEND", "local")
    monkeypatch.delenv("OMEGA_MIND_MODEL", raising=False)
    monkeypatch.delenv("OMEGA_MIND_BASE_URL", raising=False)
    monkeypatch.delenv("OMEGA_MIND_API_KEY", raising=False)

    assert brain_cloud_config.backend() == "local"
    assert brain_cloud_config.descriptor() is None

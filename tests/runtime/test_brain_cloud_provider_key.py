"""The cloud brain reads bridge-sealed provider keys without persisting plaintext."""

from __future__ import annotations

import base64
import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def test_dropdown_key_decrypts_bridge_sealed_secret(tmp_path, monkeypatch):
    """Round-trip against the TS bridge's AES-256-GCM format (iv/tag/data b64)."""
    from runtime import brain_cloud_config

    key = AESGCM.generate_key(bit_length=256)
    aes = AESGCM(key)
    iv = b"\x01" * 12
    sealed = aes.encrypt(iv, b"sk-or-secret", None)
    data, tag = sealed[:-16], sealed[-16:]
    (tmp_path / "omega-connect.key").write_text(base64.b64encode(key).decode(), encoding="utf-8")
    (tmp_path / "omega-connect.secrets.json").write_text(
        json.dumps(
            {
                "api_keys": {
                    "openrouter": {
                        "v": 1,
                        "alg": "aes-256-gcm",
                        "iv": base64.b64encode(iv).decode(),
                        "tag": base64.b64encode(tag).decode(),
                        "data": base64.b64encode(data).decode(),
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        brain_cloud_config,
        "_connector_store_root",
        lambda: tmp_path,
    )
    monkeypatch.delenv("OMEGA_CONNECT_SECRET", raising=False)

    assert brain_cloud_config._dropdown_api_key("openrouter") == "sk-or-secret"
    assert brain_cloud_config._dropdown_api_key("missing") is None


def test_corrupt_connector_secret_store_is_not_treated_as_empty(tmp_path, monkeypatch):
    from runtime import brain_cloud_config

    (tmp_path / "omega-connect.key").write_text(
        base64.b64encode(AESGCM.generate_key(bit_length=256)).decode(), encoding="utf-8"
    )
    (tmp_path / "omega-connect.secrets.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(brain_cloud_config, "_connector_store_root", lambda: tmp_path)
    monkeypatch.delenv("OMEGA_CONNECT_SECRET", raising=False)

    with pytest.raises(brain_cloud_config.MindBackendConfigError, match="secret_store_unreadable"):
        brain_cloud_config._dropdown_api_key("openrouter")

    with pytest.raises(brain_cloud_config.MindBackendConfigError, match="secret_store_unreadable"):
        brain_cloud_config._save_provider_api_key("openrouter", "new-secret")

    with pytest.raises(brain_cloud_config.MindBackendConfigError, match="secret_store_unreadable"):
        brain_cloud_config._clear_provider_api_key("openrouter")


def test_corrupt_connector_key_is_not_treated_as_missing(tmp_path, monkeypatch):
    from runtime import brain_cloud_config

    (tmp_path / "omega-connect.key").write_text("not-a-key", encoding="utf-8")
    monkeypatch.setattr(brain_cloud_config, "_connector_store_root", lambda: tmp_path)
    monkeypatch.delenv("OMEGA_CONNECT_SECRET", raising=False)

    with pytest.raises(brain_cloud_config.MindBackendConfigError, match="connector_key_unreadable"):
        brain_cloud_config._dropdown_api_key("openrouter")

"""Contract tests for the portable cloud-seat catalog."""

from __future__ import annotations

import json

import pytest

from runtime.cloud_providers import (
    CloudProviderError,
    cloud_providers,
    discover_cloud_models,
    get_cloud_provider,
    public_cloud_providers,
    provider_api_key_envs,
    provider_endpoint,
)


def test_catalog_contains_only_cloud_seats_and_no_local_runtime() -> None:
    providers = cloud_providers()

    assert len(providers) >= 20
    assert all(provider.id not in {"lmstudio", "ollama", "llamacpp"} for provider in providers)
    assert len({provider.id for provider in providers}) == len(providers)
    assert all(provider.name and provider.description for provider in providers)


def test_catalog_has_the_nanoagent_cloud_seats() -> None:
    ids = {provider.id for provider in cloud_providers()}

    assert {
        "openai",
        "openrouter",
        "mistral",
        "google",
        "deepseek",
        "groq",
        "xai",
        "together",
        "fireworks",
        "cerebras",
        "minimax",
        "nvidia",
        "huggingface",
        "moonshot",
        "moonshot-cn",
        "kimi-code",
        "dashscope",
        "dashscope-cn",
        "dashscope-coding",
        "dashscope-coding-cn",
        "nebius",
        "zhipu",
        "ollama-cloud",
        "foundry",
        "custom",
    } <= ids


def test_public_catalog_is_json_safe_and_contains_no_secret_material() -> None:
    public = public_cloud_providers()

    encoded = json.dumps(public)
    assert '"api_key":' not in encoded
    assert all("api_key_env" in item for item in public)
    assert all("base_url" in item and "docs_url" in item for item in public)


def test_provider_endpoint_uses_catalog_or_requires_custom_endpoint() -> None:
    assert provider_endpoint("mistral") == "https://api.mistral.ai/v1"
    assert provider_endpoint("nebius") == "https://api.nebius.com/ai/v1"
    assert provider_endpoint("google").endswith("/openai")
    assert provider_endpoint("foundry", "https://resource.openai.azure.com/openai/v1") == (
        "https://resource.openai.azure.com/openai/v1"
    )
    assert provider_endpoint("custom", "https://example.test/v1") == "https://example.test/v1"

    with pytest.raises(CloudProviderError, match="custom_endpoint_required"):
        provider_endpoint("foundry")
    with pytest.raises(CloudProviderError, match="custom_endpoint_required"):
        provider_endpoint("custom")


def test_provider_keys_include_nanoagent_aliases() -> None:
    assert provider_api_key_envs("google") == ("GOOGLE_API_KEY", "GEMINI_API_KEY")
    assert provider_api_key_envs("huggingface") == ("HUGGINGFACE_API_KEY", "HF_TOKEN")
    assert provider_api_key_envs("mistral") == ("MISTRAL_API_KEY",)


def test_unknown_provider_fails_explicitly() -> None:
    with pytest.raises(CloudProviderError, match="cloud_provider_unknown"):
        get_cloud_provider("not-a-provider")


def test_model_discovery_uses_provider_endpoint_and_bearer_key(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class _Response:
        status_code = 200
        text = "ok"

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, list[dict[str, str]]]:
            return {"data": [{"id": "cloud-model", "name": "Cloud Model"}]}

    def fake_get(url, *, headers, timeout):
        calls.update(url=url, headers=headers, timeout=timeout)
        return _Response()

    monkeypatch.setattr("requests.get", fake_get)

    assert discover_cloud_models("mistral", "secret") == [
        {"id": "cloud-model", "name": "Cloud Model"}
    ]
    assert calls == {
        "url": "https://api.mistral.ai/v1/models",
        "headers": {"Accept": "application/json", "Authorization": "Bearer secret"},
        "timeout": 10.0,
    }

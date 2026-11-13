"""Portable catalog of cloud model seats exposed by the Omega HUD.

The catalog is deliberately separate from the local LM Studio choice.  Every
entry uses the OpenAI-compatible request contract implemented by
``runtime.brain_cloud``; native SDKs and local runtimes do not belong in this
button.  A provider's model list is a suggestion only.  The operator still
chooses the model id that is sent to the selected seat.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final
from urllib.parse import urlsplit


class CloudProviderError(ValueError):
    """A provider selection or endpoint cannot be used by the cloud seat."""


@dataclass(frozen=True, slots=True)
class CloudProvider:
    """Non-secret connection metadata for one cloud provider."""

    id: str
    name: str
    base_url: str
    api_key_env: str
    description: str
    docs_url: str
    icon: str
    suggested_models: tuple[str, ...] = ()
    api_key_aliases: tuple[str, ...] = ()
    dynamic_models: bool = False
    custom_base_url: bool = False
    dialect: str = "openai"

    @property
    def api_key_envs(self) -> tuple[str, ...]:
        return (self.api_key_env, *self.api_key_aliases) if self.api_key_env else ()

    def public_dict(self) -> dict[str, object]:
        """Return the HUD-safe shape; this never contains a credential."""

        data = asdict(self)
        data.pop("api_key_aliases")
        data["api_key_envs"] = list(self.api_key_envs)
        data["models"] = list(self.suggested_models)
        data.pop("suggested_models")
        return data


def _provider(
    provider_id: str,
    name: str,
    base_url: str,
    api_key_env: str,
    description: str,
    docs_url: str,
    icon: str,
    models: tuple[str, ...] = (),
    *,
    aliases: tuple[str, ...] = (),
    dynamic: bool = False,
    custom_base_url: bool = False,
) -> CloudProvider:
    return CloudProvider(
        id=provider_id,
        name=name,
        base_url=base_url,
        api_key_env=api_key_env,
        description=description,
        docs_url=docs_url,
        icon=icon,
        suggested_models=models,
        api_key_aliases=aliases,
        dynamic_models=dynamic,
        custom_base_url=custom_base_url,
    )


# Adapted from nanoagent's remote provider catalog.  The local providers in
# that project are intentionally absent: Omega owns the local voice/runtime
# stack and launches LM Studio through the SSD-relative launcher instead.
_CLOUD_PROVIDERS: Final[tuple[CloudProvider, ...]] = (
    _provider(
        "openai",
        "OpenAI",
        "https://api.openai.com/v1",
        "OPENAI_API_KEY",
        "OpenAI's official API",
        "https://platform.openai.com",
        "🤖",
        ("gpt-4.1", "gpt-4.1-mini", "gpt-4o", "o4-mini"),
    ),
    _provider(
        "openrouter",
        "OpenRouter",
        "https://openrouter.ai/api/v1",
        "OPENROUTER_API_KEY",
        "One API for models from many providers",
        "https://openrouter.ai",
        "🌐",
        dynamic=True,
    ),
    _provider(
        "mistral",
        "Mistral AI",
        "https://api.mistral.ai/v1",
        "MISTRAL_API_KEY",
        "Mistral's hosted models",
        "https://mistral.ai",
        "🌪️",
        ("mistral-large-latest", "mistral-small-latest", "codestral-latest"),
    ),
    _provider(
        "google",
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "GOOGLE_API_KEY",
        "Gemini through Google's OpenAI-compatible endpoint",
        "https://ai.google.dev",
        "🔍",
        ("gemini-2.5-flash", "gemini-2.5-pro", "gemini-2.0-flash"),
        aliases=("GEMINI_API_KEY",),
    ),
    _provider(
        "deepseek",
        "DeepSeek",
        "https://api.deepseek.com",
        "DEEPSEEK_API_KEY",
        "DeepSeek's OpenAI-compatible API",
        "https://api-docs.deepseek.com",
        "🐋",
        ("deepseek-chat", "deepseek-reasoner"),
    ),
    _provider(
        "groq",
        "Groq",
        "https://api.groq.com/openai/v1",
        "GROQ_API_KEY",
        "Fast hosted inference on Groq LPUs",
        "https://console.groq.com",
        "⚡",
        ("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3-32b"),
    ),
    _provider(
        "xai",
        "xAI",
        "https://api.x.ai/v1",
        "XAI_API_KEY",
        "xAI Grok models",
        "https://docs.x.ai",
        "𝕏",
        ("grok-4", "grok-3", "grok-3-mini"),
    ),
    _provider(
        "together",
        "Together AI",
        "https://api.together.xyz/v1",
        "TOGETHER_API_KEY",
        "Together hosted open models",
        "https://docs.together.ai",
        "🤝",
        ("meta-llama/Llama-3.3-70B-Instruct-Turbo", "Qwen/Qwen2.5-Coder-32B-Instruct"),
    ),
    _provider(
        "fireworks",
        "Fireworks AI",
        "https://api.fireworks.ai/inference/v1",
        "FIREWORKS_API_KEY",
        "Fireworks hosted inference",
        "https://docs.fireworks.ai",
        "🎆",
        (
            "accounts/fireworks/models/llama-v3p3-70b-instruct",
            "accounts/fireworks/models/qwen2p5-coder-32b-instruct",
        ),
    ),
    _provider(
        "cerebras",
        "Cerebras",
        "https://api.cerebras.ai/v1",
        "CEREBRAS_API_KEY",
        "Cerebras hosted inference",
        "https://inference-docs.cerebras.ai",
        "🧠",
        ("llama3.1-8b", "llama3.3-70b", "qwen-3-32b"),
    ),
    _provider(
        "minimax",
        "MiniMax",
        "https://api.minimax.io/v1",
        "MINIMAX_API_KEY",
        "MiniMax's OpenAI-compatible API",
        "https://platform.minimax.io/docs/api-reference/text-openai-api",
        "🅰️",
        ("MiniMax-M2.7", "MiniMax-M2.5", "MiniMax-M3"),
    ),
    _provider(
        "nvidia",
        "NVIDIA NIM",
        "https://integrate.api.nvidia.com/v1",
        "NVIDIA_API_KEY",
        "NVIDIA hosted NIM models",
        "https://build.nvidia.com",
        "🟢",
        ("meta/llama-3.1-8b-instruct", "meta/llama-3.1-70b-instruct"),
        dynamic=True,
    ),
    _provider(
        "huggingface",
        "Hugging Face",
        "https://router.huggingface.co/v1",
        "HUGGINGFACE_API_KEY",
        "Hugging Face Inference Providers router",
        "https://huggingface.co/docs/inference-providers",
        "🤗",
        aliases=("HF_TOKEN",),
        dynamic=True,
    ),
    _provider(
        "moonshot",
        "Moonshot / Kimi",
        "https://api.moonshot.ai/v1",
        "MOONSHOT_API_KEY",
        "Moonshot international platform",
        "https://platform.moonshot.ai",
        "🌙",
        ("kimi-k2.5", "moonshot-v1-auto", "kimi-latest"),
    ),
    _provider(
        "moonshot-cn",
        "Moonshot / Kimi (China)",
        "https://api.moonshot.cn/v1",
        "MOONSHOT_CN_API_KEY",
        "Moonshot China platform",
        "https://platform.moonshot.cn",
        "🌙",
        ("kimi-k2.5", "moonshot-v1-auto", "kimi-latest"),
    ),
    _provider(
        "kimi-code",
        "Kimi Code",
        "https://api.kimi.com/coding/v1",
        "KIMI_API_KEY",
        "Kimi Code OpenAI-compatible coding API",
        "https://www.kimi.com/code/docs/en/",
        "🌙",
        ("kimi-for-coding", "kimi-for-coding-highspeed", "k3"),
    ),
    _provider(
        "dashscope",
        "Alibaba Model Studio",
        "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_API_KEY",
        "Alibaba Model Studio international endpoint",
        "https://help.aliyun.com/en/model-studio/base-url",
        "☁️",
        ("qwen-plus", "qwen-turbo", "qwen-max"),
    ),
    _provider(
        "dashscope-cn",
        "Alibaba Model Studio (China)",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "DASHSCOPE_CN_API_KEY",
        "Alibaba Model Studio China endpoint",
        "https://help.aliyun.com/en/model-studio/base-url",
        "☁️",
        ("qwen-plus", "qwen-turbo", "qwen-max"),
    ),
    _provider(
        "dashscope-coding",
        "Alibaba Coding Plan",
        "https://coding-intl.dashscope.aliyuncs.com/v1",
        "DASHSCOPE_CODING_API_KEY",
        "Alibaba Coding Plan international endpoint",
        "https://www.alibabacloud.com/help/en/model-studio/coding-plan",
        "💎",
        ("qwen3-coder-plus", "qwen3-coder-flash"),
    ),
    _provider(
        "dashscope-coding-cn",
        "Alibaba Coding Plan (China)",
        "https://coding.dashscope.aliyuncs.com/v1",
        "DASHSCOPE_CODING_CN_API_KEY",
        "Alibaba Coding Plan China endpoint",
        "https://help.aliyun.com/en/model-studio/coding-plan",
        "💎",
        ("qwen3-coder-plus", "qwen3-coder-flash"),
    ),
    _provider(
        "nebius",
        "Nebius Token Factory",
        "https://api.nebius.com/ai/v1",
        "NEBIUS_API_KEY",
        "Nebius hosted model inference",
        "https://docs.nebius.com",
        "⭐",
        ("yandexgpt", "yandexgpt-lite"),
    ),
    _provider(
        "zhipu",
        "Zhipu AI",
        "https://open.bigmodel.cn",
        "ZHIPU_API_KEY",
        "Zhipu GLM OpenAI-compatible endpoint",
        "https://bigmodel.cn",
        "🧩",
        ("glm-4-flash", "glm-4", "glm-4-air"),
    ),
    _provider(
        "ollama-cloud",
        "Ollama Cloud",
        "https://cloud.ollama.ai",
        "OLLAMA_CLOUD_API_KEY",
        "Ollama hosted open models",
        "https://ollama.com",
        "☁️",
        ("llama3.1:70b", "llama3.1:8b", "qwen2.5-coder:7b"),
    ),
    _provider(
        "foundry",
        "Azure AI Foundry",
        "",
        "AZURE_OPENAI_API_KEY",
        "Azure per-resource OpenAI-compatible endpoint",
        "https://learn.microsoft.com/azure/ai-foundry",
        "☁️",
        ("deployment-name",),
        dynamic=True,
        custom_base_url=True,
    ),
    _provider(
        "custom",
        "Custom OpenAI-compatible cloud",
        "",
        "",
        "Any cloud endpoint that implements /chat/completions",
        "",
        "🔗",
        custom_base_url=True,
    ),
)

_BY_ID: Final[dict[str, CloudProvider]] = {provider.id: provider for provider in _CLOUD_PROVIDERS}


def cloud_providers() -> tuple[CloudProvider, ...]:
    """Return the immutable provider catalog in connection-screen order."""

    return _CLOUD_PROVIDERS


def get_cloud_provider(provider_id: str) -> CloudProvider:
    """Resolve one provider or raise an explicit selection error."""

    key = str(provider_id or "").strip().lower()
    provider = _BY_ID.get(key)
    if provider is None:
        raise CloudProviderError("cloud_provider_unknown")
    return provider


def public_cloud_providers() -> list[dict[str, object]]:
    """Return provider cards safe to send to the HUD."""

    return [provider.public_dict() for provider in _CLOUD_PROVIDERS]


def provider_api_key_envs(provider_id: str) -> tuple[str, ...]:
    """Return the provider's optional environment aliases in priority order."""

    return get_cloud_provider(provider_id).api_key_envs


def provider_endpoint(provider_id: str, custom_base_url: str | None = None) -> str:
    """Resolve and validate a provider endpoint without inventing a default."""

    provider = get_cloud_provider(provider_id)
    endpoint = str(custom_base_url or provider.base_url).strip().rstrip("/")
    if provider.custom_base_url and not endpoint:
        raise CloudProviderError("custom_endpoint_required")
    parsed = urlsplit(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CloudProviderError("cloud_provider_endpoint_invalid")
    return endpoint


def discover_cloud_models(
    provider_id: str,
    api_key: str,
    custom_base_url: str | None = None,
    *,
    timeout: float = 10.0,
) -> list[dict[str, str]]:
    """Fetch a provider's OpenAI-compatible model catalog for the HUD.

    Network and response failures are raised as explicit provider errors.  A
    successful response with no usable model ids is also a failure, so the UI
    cannot mistake an unavailable catalog for a legitimate empty choice.
    """

    provider = get_cloud_provider(provider_id)
    endpoint = provider_endpoint(provider.id, custom_base_url)
    key = str(api_key or "").strip()
    if provider.api_key_env and not key:
        raise CloudProviderError("cloud_provider_api_key_required")
    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        import importlib

        requests = importlib.import_module("requests")
        response = requests.get(f"{endpoint}/models", headers=headers, timeout=float(timeout))
    except ImportError as exc:
        raise CloudProviderError("cloud_provider_http_client_unavailable") from exc
    except requests.RequestException as exc:
        raise CloudProviderError("cloud_provider_transport_failed") from exc
    if response.status_code >= 400:
        raise CloudProviderError(f"cloud_provider_http_{response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise CloudProviderError("cloud_provider_response_invalid") from exc
    rows = body.get("data") if isinstance(body, dict) else body
    if not isinstance(rows, list):
        raise CloudProviderError("cloud_provider_models_invalid")
    models: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("id") or row.get("name") or "").strip()
        if not model_id:
            continue
        name = str(row.get("name") or model_id).strip()
        models.append({"id": model_id, "name": name or model_id})
    if not models:
        raise CloudProviderError("cloud_provider_models_empty")
    return models


__all__ = [
    "CloudProvider",
    "CloudProviderError",
    "cloud_providers",
    "discover_cloud_models",
    "get_cloud_provider",
    "public_cloud_providers",
    "provider_api_key_envs",
    "provider_endpoint",
]

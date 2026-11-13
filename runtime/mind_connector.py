"""Provider-neutral connector contract for the single Omega mind route."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlsplit

_BACKENDS = frozenset({"local", "cloud"})


@dataclass(frozen=True, slots=True)
class MindConnector:
    """The non-secret connection material for one configured mind backend."""

    backend: str
    provider: str = ""
    model: str = ""
    base_url: str = ""
    api_key_env: str = ""
    dialect: str = "openai"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "MindConnector":
        backend = str(value.get("backend") or "").strip().lower()
        provider = str(value.get("provider") or "").strip().lower()
        model = str(value.get("model") or "").strip()
        base_url = str(value.get("base_url") or "").strip().rstrip("/")
        api_key_env = str(value.get("api_key_env") or "").strip()
        dialect = str(value.get("dialect") or "openai").strip().lower()
        if backend not in _BACKENDS:
            raise ValueError("mind_connector_backend_invalid")
        if backend == "cloud":
            parsed = urlsplit(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("mind_connector_base_url_invalid")
            if not provider or not model:
                raise ValueError("mind_connector_identity_missing")
        return cls(backend, provider, model, base_url, api_key_env, dialect)

    def public_dict(self) -> dict[str, str]:
        """Return configuration safe for HUD/status responses; never includes a key."""
        return {
            "backend": self.backend,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "dialect": self.dialect,
        }

    def with_api_key(self, api_key: str) -> dict[str, str]:
        """Return the request descriptor consumed by the cloud adapter."""
        if self.backend != "cloud":
            raise ValueError("mind_connector_key_missing")
        return {**self.public_dict(), "api_key": str(api_key).strip()}

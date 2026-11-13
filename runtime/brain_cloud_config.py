"""Backend-neutral mind route config.

The mind has one route. It may be backed by a configured OpenAI-compatible cloud seat or by
the local LM Studio seat; downstream cognition does not know which backend answered. The
operator selects the mode with ``OMEGA_MIND_BACKEND=auto|local|cloud`` or the persisted
``mind_backend.json`` state. ``auto`` uses cloud only when a complete cloud descriptor is
configured, otherwise it leaves the local runtime in charge.

Non-secret selection lives in a gitignored guardian-state JSON (``guardian/state/mind_backend.json``).
Keys never enter the file: a key saved in the portable encrypted provider store is authoritative;
environment variables are used only when that store has no key. Missing configuration is the only empty state;
an unreadable or malformed saved selection is an explicit startup/configuration failure.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import os
import secrets
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from runtime.cloud_providers import cloud_providers, get_cloud_provider, provider_endpoint

log = logging.getLogger("omega.runtime.brain_cloud_config")

# No provider or model is a product default. A deployment supplies these through state or
# environment when it selects the cloud backend.
BASE_URL = ""
MODEL = ""
LOCAL_BASE_URL = "http://127.0.0.1:1234/v1"

_BACKEND_ENV = "OMEGA_MIND_BACKEND"
_PROVIDER_ENV = "OMEGA_MIND_PROVIDER"
_MODEL_ENV = "OMEGA_MIND_MODEL"
_BASE_URL_ENV = "OMEGA_MIND_BASE_URL"
_KEY_ENV = "OMEGA_MIND_API_KEY"
_LOCAL_MODEL_ENV = "OMEGA_MIND_LOCAL_MODEL"


_PROVIDER_KEY_ENVS = {
    provider.id: provider.api_key_env
    for provider in cloud_providers()
    if provider.api_key_env
}
_BACKENDS = {"auto", "local", "cloud"}


class MindBackendConfigError(RuntimeError):
    """The persisted mind-seat configuration exists but cannot be trusted."""


def _secret_key(base_dir) -> Optional[bytes]:
    env_secret = os.environ.get("OMEGA_CONNECT_SECRET", "").strip()
    if env_secret:
        return hashlib.sha256(env_secret.encode("utf-8")).digest()
    key_path = base_dir / "omega-connect.key"
    if not key_path.is_file():
        return None
    try:
        raw = key_path.read_text(encoding="utf-8").strip()
        key = base64.b64decode(raw, validate=True)
    except (OSError, UnicodeError, binascii.Error, ValueError) as exc:
        raise MindBackendConfigError(f"connector_key_unreadable: {key_path}") from exc
    if len(key) != 32:
        raise MindBackendConfigError(f"connector_key_unreadable: {key_path}")
    return key


def _ensure_secret_key(base_dir) -> bytes:
    """Create the local connector key on first credential entry."""
    existing = _secret_key(base_dir)
    if existing is not None:
        return existing
    base_dir.mkdir(parents=True, exist_ok=True)
    key = secrets.token_bytes(32)
    (base_dir / "omega-connect.key").write_text(
        base64.b64encode(key).decode("ascii"), encoding="utf-8"
    )
    return key


def _dropdown_api_key(provider_id: str) -> Optional[str]:
    """Read the connector-sealed provider key without logging plaintext."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        base_dir = _connector_store_root()
        key = _secret_key(base_dir)
        if key is None:
            return None
        secrets = _load_secret_store(base_dir)
        payload = (secrets.get("api_keys") or {}).get(provider_id)
        if not isinstance(payload, dict):
            return None
        iv = base64.b64decode(str(payload.get("iv") or ""))
        tag = base64.b64decode(str(payload.get("tag") or ""))
        data = base64.b64decode(str(payload.get("data") or ""))
        plaintext = AESGCM(key).decrypt(iv, data + tag, None)
        return plaintext.decode("utf-8").strip() or None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise MindBackendConfigError(
            f"connector_secret_store_unreadable: {_connector_store_root()}"
        ) from exc


def _save_provider_api_key(provider_id: str, value: str) -> None:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    base_dir = _connector_store_root()
    key = _ensure_secret_key(base_dir)
    secret_path = base_dir / "omega-connect.secrets.json"
    blob = _load_secret_store(base_dir)
    api_keys = dict(blob.get("api_keys") or {})
    iv = secrets.token_bytes(12)
    sealed = AESGCM(key).encrypt(iv, value.encode("utf-8"), None)
    api_keys[provider_id.strip().lower() or "custom"] = {
        "v": 1,
        "alg": "aes-256-gcm",
        "iv": base64.b64encode(iv).decode("ascii"),
        "tag": base64.b64encode(sealed[-16:]).decode("ascii"),
        "data": base64.b64encode(sealed[:-16]).decode("ascii"),
    }
    base_dir.mkdir(parents=True, exist_ok=True)
    secret_path.write_text(json.dumps({"api_keys": api_keys}, indent=2), encoding="utf-8")


def _clear_provider_api_key(provider_id: str) -> None:
    base_dir = _connector_store_root()
    secret_path = base_dir / "omega-connect.secrets.json"
    blob = _load_secret_store(base_dir)
    api_keys = dict(blob.get("api_keys") or {})
    api_keys.pop(provider_id.strip().lower() or "custom", None)
    if api_keys:
        secret_path.write_text(json.dumps({"api_keys": api_keys}, indent=2), encoding="utf-8")
    else:
        secret_path.unlink(missing_ok=True)


def saved_api_key_providers() -> list[str]:
    """Return providers with decryptable keys, never the keys themselves."""
    base_dir = _connector_store_root()
    records = _load_secret_store(base_dir).get("api_keys") or {}
    saved: list[str] = []
    for raw_provider, payload in sorted(records.items()):
        provider = str(raw_provider or "").strip().lower()
        if not provider or not isinstance(payload, dict):
            raise MindBackendConfigError(
                f"connector_secret_record_invalid: {base_dir}"
            )
        if not _dropdown_api_key(provider):
            raise MindBackendConfigError(
                f"connector_secret_record_invalid: {base_dir}"
            )
        saved.append(provider)
    return saved


def save_provider_api_key(provider_id: str, value: str) -> Dict[str, Any]:
    """Encrypt one provider key and return only non-secret save status."""
    provider = str(provider_id or "").strip().lower()
    secret = str(value or "").strip()
    if not provider:
        raise ValueError("mind_connector_provider_required")
    if not secret:
        raise ValueError("mind_connector_api_key_required")
    try:
        get_cloud_provider(provider)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    _save_provider_api_key(provider, secret)
    return {
        "provider": provider,
        "saved": True,
        "saved_key_providers": saved_api_key_providers(),
    }


def _config_path() -> Path:
    """Gitignored, backend-neutral state path resolved lazily for tests and relocation."""
    from core.paths import OMEGA_ROOT

    return Path(OMEGA_ROOT) / "guardian" / "state" / "mind_backend.json"


def _connector_store_root() -> Path:
    from core.paths import OMEGA_ROOT

    return Path(OMEGA_ROOT) / "guardian" / "state" / "connectors"


def _load_secret_store(base_dir: Path) -> Dict[str, Any]:
    """Load the encrypted connector store; missing is empty, corruption is fatal."""
    path = base_dir / "omega-connect.secrets.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MindBackendConfigError(f"connector_secret_store_unreadable: {path}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("api_keys", {}), dict):
        raise MindBackendConfigError(f"connector_secret_store_invalid: {path}")
    return data


def _load() -> Dict[str, Any]:
    """Read the selected seat; missing is empty, corruption is never silently ignored."""
    path = _config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MindBackendConfigError(f"mind_backend_config_unreadable: {path}") from exc
    if not isinstance(data, dict):
        raise MindBackendConfigError(f"mind_backend_config_invalid: {path}")
    return data


def backend() -> str:
    """Return the selected mind backend: ``auto``, ``local``, or ``cloud``."""
    raw = (os.environ.get(_BACKEND_ENV) or "").strip().lower()
    if raw in _BACKENDS:
        return raw
    value = str(_load().get("backend") or "").strip().lower()
    if value in _BACKENDS:
        return value
    return "auto"


def local_model() -> str:
    """Optional local-runtime model selected by the operator, never by Omega code."""
    env = (os.environ.get(_LOCAL_MODEL_ENV) or "").strip()
    if env:
        return env
    value = _load().get("local_model")
    return str(value).strip() if isinstance(value, str) else ""


def local_descriptor() -> Optional[Dict[str, Any]]:
    """Return the configured LM Studio OpenAI-compatible descriptor."""
    if backend() == "cloud":
        return None
    data = _load()
    model_id = local_model() or str(data.get("model") or "").strip()
    if not model_id:
        return None
    endpoint = str(
        os.environ.get(_BASE_URL_ENV)
        or data.get("base_url")
        or LOCAL_BASE_URL
    ).strip().rstrip("/")
    if not endpoint:
        return None
    from runtime.mind_connector import MindConnector

    connector = MindConnector.from_mapping(
        {
            "backend": "local",
            "provider": "lmstudio",
            "model": model_id,
            "base_url": endpoint,
            "dialect": "openai",
        }
    )
    return {**connector.public_dict(), "api_key": ""}


def _cloud_provider():
    """Resolve the selected cloud provider or preserve an attributable config failure."""
    provider_id = _provider_id()
    try:
        return get_cloud_provider(provider_id)
    except ValueError as exc:
        raise MindBackendConfigError(
            f"mind_backend_config_invalid: {_config_path()} provider={provider_id}"
        ) from exc


def _write(data: Dict[str, Any]) -> None:
    persisted = dict(data)
    persisted.pop("api_key", None)
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(persisted, indent=2), encoding="utf-8")


def is_enabled() -> bool:
    """True when a cloud mind route is selected or completely configured in auto mode."""
    mode = backend()
    if mode == "local":
        return False
    _cloud_provider()
    data = _load()
    key_env = str(data.get("api_key_env") or "").strip() or _PROVIDER_KEY_ENVS.get(
        _provider_id(), ""
    )
    return mode == "cloud" or bool(data.get("enabled")) or bool(
        base_url() and model() and (api_key() or not key_env)
    )


def base_url() -> str:
    """The selected provider endpoint, unless the operator supplied an override."""
    mode = backend()
    env = os.environ.get(_BASE_URL_ENV)
    if env and env.strip():
        explicit = env.strip()
    else:
        v = _load().get("base_url")
        explicit = v.strip() if isinstance(v, str) and v.strip() else ""
    if explicit:
        if mode == "local":
            return explicit.rstrip("/")
        try:
            return provider_endpoint(_provider_id(), explicit)
        except ValueError as exc:
            raise MindBackendConfigError(
                f"mind_backend_config_invalid: {_config_path()}"
            ) from exc
    if mode == "local":
        return ""
    provider_id = _provider_id()
    if provider_id == "custom":
        return ""
    try:
        return provider_endpoint(provider_id)
    except ValueError as exc:
        raise MindBackendConfigError(
            f"mind_backend_config_invalid: {_config_path()}"
        ) from exc


def model() -> str:
    """The effective cloud model, with no provider default."""
    env = os.environ.get(_MODEL_ENV)
    if env and env.strip():
        return env.strip()
    v = _load().get("model")
    return str(v).strip() if isinstance(v, str) and str(v).strip() else MODEL


def _provider_id() -> str:
    env = (os.environ.get(_PROVIDER_ENV) or "").strip().lower()
    value = env or str(_load().get("provider") or "custom").strip().lower()
    return value or "custom"


def _provider_environment_api_key(provider: str) -> str:
    configured_env = str(_load().get("api_key_env") or "").strip()
    provider_envs = [configured_env] if configured_env else []
    provider_envs.extend(
        env_name
        for env_name in _provider_env_aliases(provider)
        if env_name not in provider_envs
    )
    for provider_env in provider_envs:
        value = (os.environ.get(provider_env) or "").strip()
        if value:
            return value
    return ""


def _provider_api_key(provider: str) -> str:
    """Resolve the portable vault before any inherited host environment key."""
    stored = str(_dropdown_api_key(provider) or "").strip()
    return stored or _provider_environment_api_key(provider)


def provider_api_key(provider_id: str) -> str:
    """Resolve one provider key for a connection probe without exposing it to callers."""
    provider = str(provider_id or "").strip().lower()
    if not provider:
        return ""
    stored = str(_dropdown_api_key(provider) or "").strip()
    if stored:
        return stored
    try:
        env_names = get_cloud_provider(provider).api_key_envs
    except ValueError as exc:
        raise MindBackendConfigError(
            f"mind_backend_config_invalid: {_config_path()}"
        ) from exc
    for env_name in env_names:
        value = (os.environ.get(env_name) or "").strip()
        if value:
            return value
    return ""


def _provider_env_aliases(provider: str) -> tuple[str, ...]:
    try:
        return tuple(get_cloud_provider(provider).api_key_envs)
    except ValueError as exc:
        if backend() == "local":
            return ()
        raise MindBackendConfigError(
            f"mind_backend_config_invalid: {_config_path()}"
        ) from exc


def api_key() -> str:
    """Resolve the selected cloud key without reading plaintext from state."""
    provider = _provider_id()
    provider_key = _provider_api_key(provider)
    if provider_key:
        return provider_key
    env = os.environ.get(_KEY_ENV)
    if env and env.strip():
        return env.strip()
    return _provider_environment_api_key(provider)


def descriptor() -> Optional[Dict[str, Any]]:
    """The resolved cloud mind backend, or None when it is not usable.

    A provider may require a key or may expose an unauthenticated local endpoint; the
    connector descriptor decides which case applies."""
    if backend() == "local" or not is_enabled():
        return None
    provider = _cloud_provider()
    key = api_key()
    provider_key_envs = provider.api_key_envs
    configured_env = str(_load().get("api_key_env") or "").strip()
    if configured_env:
        provider_key_envs = (configured_env, *provider_key_envs)
    if not key and provider_key_envs:
        return None
    from runtime.mind_connector import MindConnector

    try:
        connector = MindConnector.from_mapping(
            {
                "backend": "cloud",
                "provider": _provider_id(),
                "model": model(),
                "base_url": base_url(),
                "api_key_env": str(_load().get("api_key_env") or ""),
                "dialect": str(
                    _load().get("dialect")
                    or ("mistral" if _provider_id() == "mistral" else "openai")
                ),
            }
        )
        return connector.with_api_key(key)
    except ValueError as exc:
        raise MindBackendConfigError(
            f"mind_backend_config_invalid: {_config_path()}"
        ) from exc


def set_enabled(enabled: bool) -> None:
    data = _load()
    data["enabled"] = bool(enabled)
    data["backend"] = "cloud" if enabled else "local"
    _write(data)


def public_state() -> Dict[str, Any]:
    """HUD/telemetry-facing state without the raw key."""
    return {
        "backend": backend(),
        "enabled": is_enabled(),
        "provider": _provider_id(),
        "model": model(),
        "base_url": base_url(),
        "local_model": local_model(),
        "has_key": bool(api_key()),
        "saved_key_providers": saved_api_key_providers(),
    }


def configure_connector(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Persist one backend-neutral mind connector without persisting plaintext credentials."""
    from runtime.mind_connector import MindConnector

    if not isinstance(payload, Mapping):
        raise ValueError("mind_connector_payload_invalid")
    selected = str(payload.get("backend") or "auto").strip().lower()
    if selected not in {"auto", "local", "cloud"}:
        raise ValueError("mind_connector_backend_invalid")
    provider = str(payload.get("provider") or "custom").strip().lower()
    model_id = str(payload.get("model") or "").strip()
    endpoint = str(payload.get("base_url") or "").strip()
    dialect = str(payload.get("dialect") or "openai").strip().lower()
    api_key_env = str(payload.get("api_key_env") or "").strip()
    if selected == "cloud":
        if provider == "lmstudio":
            if not endpoint:
                raise ValueError("mind_connector_base_url_invalid")
        else:
            try:
                endpoint = provider_endpoint(provider, endpoint or None)
                catalog_provider = get_cloud_provider(provider)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            api_key_env = api_key_env or catalog_provider.api_key_env
            dialect = dialect or catalog_provider.dialect
        MindConnector.from_mapping(
            {
                "backend": "cloud",
                "provider": provider,
                "model": model_id,
                "base_url": endpoint,
                "api_key_env": api_key_env,
                "dialect": dialect,
            }
        )
    elif selected == "auto" and endpoint and model_id:
        if provider != "lmstudio":
            try:
                endpoint = provider_endpoint(provider, endpoint)
                catalog_provider = get_cloud_provider(provider)
            except ValueError as exc:
                raise ValueError(str(exc)) from exc
            api_key_env = api_key_env or catalog_provider.api_key_env
        MindConnector.from_mapping(
            {
                "backend": "cloud",
                "provider": provider,
                "model": model_id,
                "base_url": endpoint,
                "api_key_env": api_key_env,
                "dialect": dialect,
            }
        )
    data = {
        "backend": selected,
        "enabled": selected == "cloud",
        "provider": provider,
        "model": model_id,
        "base_url": endpoint,
        "api_key_env": api_key_env,
        "dialect": dialect,
        "local_model": str(payload.get("local_model") or (model_id if selected == "local" else "")).strip(),
    }
    _write(data)
    raw_key = payload.get("api_key")
    if isinstance(raw_key, str) and raw_key.strip():
        _save_provider_api_key(provider, raw_key.strip())
    elif payload.get("clear_api_key"):
        _clear_provider_api_key(provider)
    return public_state()

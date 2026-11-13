"""Governed directive routing (config + policy bundle)."""

from __future__ import annotations

from typing import Any

_DEFAULT_INTENTS = frozenset({"IMAGE_CREATE", "DOCUMENT_CREATE", "PLUGIN_OPERATION"})

_CAPABILITY_TOOL_MAP = {
    "image_gen": "image_tool",
    "document_parsing": "document_tool",
}

_SPECS_CACHE: dict[str, dict[str, Any]] | None = None


def reset_directives_config_for_tests() -> None:
    global _SPECS_CACHE
    _SPECS_CACHE = None


def _specs_by_intent() -> dict[str, dict[str, Any]]:
    global _SPECS_CACHE
    if _SPECS_CACHE is not None:
        return _SPECS_CACHE
    out: dict[str, dict[str, Any]] = {}
    try:
        from config.loader import load_subsystem_bundle

        block = (load_subsystem_bundle("directives").get("config") or {}).get("directives") or {}
        if isinstance(block, dict):
            for _key, body in block.items():
                if not isinstance(body, dict):
                    continue
                intent = str(body.get("intent") or "").strip().upper()
                if not intent:
                    continue
                out[intent] = {
                    "route": str(body.get("route") or ""),
                    "runtime_capability": str(body.get("runtime_capability") or ""),
                }
    except Exception:
        pass
    if not out:
        out = {
            "IMAGE_CREATE": {
                "route": "auto_loop.route",
                "runtime_capability": "image_gen",
            },
            "DOCUMENT_CREATE": {
                "route": "auto_loop.route",
                "runtime_capability": "document_parsing",
            },
            "PLUGIN_OPERATION": {
                "route": "auto_loop.route",
                "runtime_capability": "mcp_plugin",
            },
        }
    _SPECS_CACHE = out
    return out


def allowed_directive_intents() -> frozenset[str]:
    specs = _specs_by_intent()
    if specs:
        return frozenset(specs.keys())
    return _DEFAULT_INTENTS


def directive_spec(intent: str) -> dict[str, Any] | None:
    key = str(intent or "").strip().upper()
    if not key:
        return None
    spec = _specs_by_intent().get(key)
    return dict(spec) if spec else None


def tool_for_directive_intent(intent: str) -> str | None:
    spec = directive_spec(intent)
    if not spec:
        return None
    capability = str(spec.get("runtime_capability") or "")
    return _CAPABILITY_TOOL_MAP.get(capability)

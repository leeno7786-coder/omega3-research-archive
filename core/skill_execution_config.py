"""Governed Phase 6 skill execution settings (config + policy bundle)."""

from __future__ import annotations

import os
from typing import Any

_BUNDLE_CACHE: dict[str, Any] | None = None


def reset_skill_execution_config_for_tests() -> None:
    global _BUNDLE_CACHE
    _BUNDLE_CACHE = None


def _bundle() -> dict[str, Any]:
    global _BUNDLE_CACHE
    if _BUNDLE_CACHE is not None:
        return _BUNDLE_CACHE
    try:
        from config.loader import load_subsystem_bundle

        _BUNDLE_CACHE = load_subsystem_bundle("skill_execution")
    except Exception:
        _BUNDLE_CACHE = {"config": {"skill_execution": {}}, "policy": {}}
    return _BUNDLE_CACHE


def _cfg() -> dict[str, Any]:
    block = (_bundle().get("config") or {}).get("skill_execution") or {}
    return dict(block) if isinstance(block, dict) else {}


def _env_flag(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return None
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def use_skill_routing_enabled() -> bool:
    """Resolve USE_SKILL routing — env OMEGA_USE_SKILL_ROUTING overrides config default."""
    env = _env_flag("OMEGA_USE_SKILL_ROUTING")
    if env is not None:
        return env
    routing = _cfg().get("routing") or {}
    if isinstance(routing, dict) and "use_skill_default" in routing:
        return bool(routing.get("use_skill_default"))
    return True


def user_skill_trust_policy() -> tuple[list[str], bool]:
    """Trusted user skill names + trust-all flag (env overrides config)."""
    user = _cfg().get("user_skills") or {}
    trust_all_cfg = bool(user.get("trust_all_default")) if isinstance(user, dict) else False
    trust_all = _env_flag("OMEGA_TRUST_ALL_USER_SKILLS")
    if trust_all is None:
        trust_all = trust_all_cfg
    raw = os.environ.get("OMEGA_TRUST_USER_SKILLS", "").strip()
    if not raw and isinstance(user, dict):
        raw = str(user.get("trusted_names") or "").strip()
    names = [x.strip() for x in raw.split(",") if x.strip()]
    return names, bool(trust_all)


def bundled_skill_roots() -> list[str]:
    roots = _cfg().get("bundled_skill_roots") or ["skills"]
    return [str(r) for r in roots if str(r).strip()]


def user_skill_roots() -> list[str]:
    roots = _cfg().get("user_skill_roots") or []
    return [str(r) for r in roots if str(r).strip()]


def global_forbidden_tools() -> tuple[str, ...]:
    tools = _cfg().get("global_forbidden_tools") or []
    if not isinstance(tools, list):
        return ()
    return tuple(str(t) for t in tools if str(t).strip())


def operator_workspace_env() -> str:
    return "OMEGA_OPERATOR_WORKSPACE"

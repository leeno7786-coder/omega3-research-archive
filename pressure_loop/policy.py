"""Pressure + cognition slice policy loaders (rebuild path).

Replaces core.symphony_policy for the minimal boot / pressure_loop /
cognition_chain / global_loop extract surfaces only.
"""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional

import yaml

if TYPE_CHECKING:
    from runtime.socket import RuntimeSocket

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_POLICY_PATH = _REPO_ROOT / "policy" / "symphony.policy.yaml"
_PRESSURE_CONFIG_PATH = _REPO_ROOT / "config" / "pressure.yaml"
_GLOBAL_CONFIG_PATH = _REPO_ROOT / "config" / "global.yaml"
_META_CONFIG_PATH = _REPO_ROOT / "config" / "meta.yaml"
_AUTO_CONFIG_PATH = _REPO_ROOT / "config" / "auto.yaml"
_META_B_CONFIG_PATH = _REPO_ROOT / "config" / "meta_b.yaml"
_GLOBAL_B_CONFIG_PATH = _REPO_ROOT / "config" / "global_b.yaml"
_TOOLS_CONFIG_PATH = _REPO_ROOT / "config" / "tools.yaml"
_KNOWLEDGE_CONFIG_PATH = _REPO_ROOT / "config" / "knowledge.yaml"
_JOURNAL_CONFIG_PATH = _REPO_ROOT / "config" / "journal.yaml"
_DEEPSLEEP_CONFIG_PATH = _REPO_ROOT / "config" / "deepsleep.yaml"
_OMEGA_CONFIG_PATH = _REPO_ROOT / "config" / "omega.yaml"
_RUNTIME_CONFIG_PATH = _REPO_ROOT / "config" / "runtime.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected mapping at root of {path}")
    return data


def _validate_stem(stem: str, root: dict[str, Any]) -> None:
    """Schema-validate a cascade config read (2026-07-04 governance close).

    Deliberately OUTSIDE the readers' fail-soft try blocks: an absent or
    unparseable file keeps its pinned fallback semantics ({} → synthesized
    defaults), but a file that PARSES and violates schema/{stem}.schema.yaml
    raises ValueError loudly — a bad cascade config must stop the boot, never
    silently shadow-collapse (the P0 config-shadowing lesson, 2026-06-12).
    """
    from config.schema_validate import validate_config_document

    validate_config_document(stem, root)


@lru_cache(maxsize=1)
def _load_pressure_config_yaml() -> dict[str, Any]:
    if not _PRESSURE_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_PRESSURE_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("pressure", root)
    pressure = root.get("pressure")
    return pressure if isinstance(pressure, dict) else {}


@lru_cache(maxsize=1)
def _load_meta_config_yaml() -> dict[str, Any]:
    if not _META_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_META_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("meta", root)
    meta = root.get("meta")
    return meta if isinstance(meta, dict) else {}


@lru_cache(maxsize=1)
def _load_meta_b_config_yaml() -> dict[str, Any]:
    if not _META_B_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_META_B_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("meta_b", root)
    meta_b = root.get("meta_b")
    return meta_b if isinstance(meta_b, dict) else {}


@lru_cache(maxsize=1)
def _load_global_b_config_yaml() -> dict[str, Any]:
    if not _GLOBAL_B_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_GLOBAL_B_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("global_b", root)
    global_b = root.get("global_b")
    return global_b if isinstance(global_b, dict) else {}


@lru_cache(maxsize=1)
def _load_auto_config_yaml() -> dict[str, Any]:
    if not _AUTO_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_AUTO_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("auto", root)
    auto = root.get("auto")
    return auto if isinstance(auto, dict) else {}


@lru_cache(maxsize=1)
def _load_knowledge_config_yaml() -> dict[str, Any]:
    if not _KNOWLEDGE_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_KNOWLEDGE_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("knowledge", root)
    knowledge = root.get("knowledge")
    return knowledge if isinstance(knowledge, dict) else {}


@lru_cache(maxsize=1)
def _load_journal_config_yaml() -> dict[str, Any]:
    if not _JOURNAL_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_JOURNAL_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("journal", root)
    episode_journal = root.get("episode_journal")
    return episode_journal if isinstance(episode_journal, dict) else {}


@lru_cache(maxsize=1)
def _load_deepsleep_config_yaml() -> dict[str, Any]:
    if not _DEEPSLEEP_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_DEEPSLEEP_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("deepsleep", root)
    deepsleep = root.get("deepsleep")
    return deepsleep if isinstance(deepsleep, dict) else {}


@lru_cache(maxsize=1)
def _load_omega_config_yaml() -> dict[str, Any]:
    if not _OMEGA_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_OMEGA_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("omega", root)
    omega = root.get("omega")
    return omega if isinstance(omega, dict) else {}


@lru_cache(maxsize=1)
def _load_runtime_config_yaml() -> dict[str, Any]:
    try:
        from config.loader import load_runtime_config

        root = load_runtime_config()
    except Exception:
        if not _RUNTIME_CONFIG_PATH.is_file():
            return {}
        try:
            root = _read_yaml(_RUNTIME_CONFIG_PATH)
        except (OSError, ValueError, yaml.YAMLError):
            return {}
    _validate_stem("runtime", root)
    runtime = root.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


@lru_cache(maxsize=1)
def _load_tools_config_yaml() -> dict[str, Any]:
    if not _TOOLS_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_TOOLS_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("tools", root)
    tools = root.get("tools")
    return tools if isinstance(tools, dict) else {}


@lru_cache(maxsize=1)
def _load_global_config_yaml() -> dict[str, Any]:
    if not _GLOBAL_CONFIG_PATH.is_file():
        return {}
    try:
        root = _read_yaml(_GLOBAL_CONFIG_PATH)
    except (OSError, ValueError, yaml.YAMLError):
        return {}
    _validate_stem("global", root)
    global_sec = root.get("global")
    return global_sec if isinstance(global_sec, dict) else {}


@lru_cache(maxsize=1)
def load_policy(policy_path: Path | None = None) -> dict[str, Any]:
    path = policy_path or _DEFAULT_POLICY_PATH
    if path.is_file():
        try:
            return _read_yaml(path)
        except (OSError, ValueError, yaml.YAMLError):
            pass
    return {
        "conductor_mode": "symphony",
        "pressure": dict(_load_pressure_config_yaml()),
        "global": dict(_load_global_config_yaml()),
        "meta": dict(_load_meta_config_yaml()),
        "auto": dict(_load_auto_config_yaml()),
        "meta_b": dict(_load_meta_b_config_yaml()),
        "global_b": dict(_load_global_b_config_yaml()),
        "omega": dict(_load_omega_config_yaml()),
        "deepsleep": dict(_load_deepsleep_config_yaml()),
    }


def symphony_mode_enabled(policy: dict[str, Any] | None = None) -> bool:
    p = policy if policy is not None else load_policy()
    return str(p.get("conductor_mode", "symphony")).strip() == "symphony"


def pressure_conductor_wired(policy: dict[str, Any] | None = None) -> bool:
    p = policy if policy is not None else load_policy()
    pressure = p.get("pressure") if isinstance(p, dict) else None
    if not isinstance(pressure, dict):
        return False
    return bool(pressure.get("conductor_wired", False))


_PRESSURE_DEEP_KEYS = (
    "neuromod",
)


def _pressure_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """config/pressure.yaml is the authoritative base; policy/symphony.policy.yaml overlays
    flags per-key — the same merge used by _meta_section/_auto_section/
    _omega_section/_global_b_section.

    This keeps config/pressure.yaml LIVE: a tunable set only there is no longer
    silently shadowed by policy/symphony.policy.yaml (the prior HIGH-1 decay_rate-in-the-
    wrong-file footgun). The policy file still wins per-key for anything it
    declares, and when policy/symphony.policy.yaml is absent the base is the
    production-correct fallback.
    """
    yaml_pressure = _load_pressure_config_yaml()
    p = policy if policy is not None else load_policy()
    overlay = p.get("pressure") if isinstance(p, dict) else None
    if not isinstance(overlay, dict) or not overlay:
        return yaml_pressure
    merged = dict(yaml_pressure)
    merged.update(overlay)
    for key in _PRESSURE_DEEP_KEYS:
        base = yaml_pressure.get(key)
        if not isinstance(base, dict):
            continue
        over = overlay.get(key)
        if isinstance(over, dict) and over:
            sub = dict(base)
            sub.update(over)
            merged[key] = sub
        else:
            merged[key] = dict(base)
    return merged


def pressure_tick_interval_seconds(policy: dict[str, Any] | None = None) -> float:
    p = policy if policy is not None else load_policy()
    pressure = p.get("pressure") if isinstance(p, dict) else None
    if isinstance(pressure, dict):
        raw = pressure.get("tick_interval_seconds")
        if raw is not None:
            try:
                v = float(raw)
                if v > 0.0 and math.isfinite(v):
                    return v
            except (TypeError, ValueError):
                pass
    return 120.0


_MOTOR_PROGRAM_DEFAULT_ACTIONS = frozenset({"USE_TOOL", "USE_SKILL", "EXECUTE_MOTOR_PROGRAM"})


def _motor_programs_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return governed pre-execution planning policy.

    The live values belong to ``config/cognition_chain.yaml`` rather than the
    legacy symphony policy document.  An explicit policy argument is retained
    as a deterministic test/override seam.
    """
    if policy is None:
        try:
            from config.loader import load_subsystem_bundle

            policy = load_subsystem_bundle("cognition_chain").get("config") or {}
        except Exception:
            policy = {}
    cognition_chain = policy.get("cognition_chain") if isinstance(policy, dict) else None
    section = cognition_chain.get("motor_programs") if isinstance(cognition_chain, dict) else None
    return section if isinstance(section, dict) else {}


def motor_programs_enabled(policy: dict[str, Any] | None = None) -> bool:
    raw = _motor_programs_section(policy).get("enabled", True)
    return raw if isinstance(raw, bool) else True


def motor_programs_shadow_mode(policy: dict[str, Any] | None = None) -> bool:
    raw = _motor_programs_section(policy).get("shadow_mode", True)
    return raw if isinstance(raw, bool) else True


def motor_programs_eligible_actions(
    policy: dict[str, Any] | None = None,
) -> frozenset[str]:
    raw = _motor_programs_section(policy).get("eligible_actions")
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return _MOTOR_PROGRAM_DEFAULT_ACTIONS
    actions = frozenset(str(action).strip() for action in raw if str(action).strip())
    return actions & _MOTOR_PROGRAM_DEFAULT_ACTIONS


def _positive_motor_limit(key: str, default: int, policy: dict[str, Any] | None) -> int:
    raw = _motor_programs_section(policy).get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def motor_programs_max_parse_attempts(policy: dict[str, Any] | None = None) -> int:
    return _positive_motor_limit("max_parse_attempts", 2, policy)


def motor_programs_timeout_seconds(policy: dict[str, Any] | None = None) -> int:
    """Thinking-enabled motor planning gets its own longer runtime budget."""
    raw = _motor_programs_section(policy).get("timeout_seconds", 600)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 600
    return value if 60 <= value <= 900 else 600


def motor_programs_equivalent_no_progress_limit(
    policy: dict[str, Any] | None = None,
) -> int:
    return _positive_motor_limit("equivalent_no_progress_limit", 2, policy)


def motor_programs_distinct_failure_limit(
    policy: dict[str, Any] | None = None,
) -> int:
    return _positive_motor_limit("distinct_failure_limit", 3, policy)


def fetched_comprehension_timeout_seconds(
    policy: dict[str, Any] | None = None,
) -> int:
    """Runtime budget for the post-fetch comprehension pass on local iGPU."""
    if policy is None:
        try:
            from config.loader import load_subsystem_bundle

            policy = load_subsystem_bundle("cognition_chain").get("config") or {}
        except Exception:
            policy = {}
    cognition_chain = policy.get("cognition_chain") if isinstance(policy, dict) else None
    section = (
        cognition_chain.get("fetched_comprehension") if isinstance(cognition_chain, dict) else None
    )
    raw = section.get("timeout_seconds", 600) if isinstance(section, dict) else 600
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 600
    return value if 60 <= value <= 900 else 600


_OMEGA_DEEP_KEYS = ("intake", "reason", "reflect", "package")


def _omega_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    yaml_omega = _load_omega_config_yaml()
    p = policy if policy is not None else load_policy()
    overlay = p.get("omega") if isinstance(p, dict) else None
    if not isinstance(overlay, dict) or not overlay:
        return yaml_omega
    merged = dict(yaml_omega)
    merged.update(overlay)
    for key in _OMEGA_DEEP_KEYS:
        base = yaml_omega.get(key)
        if not isinstance(base, dict):
            continue
        over = overlay.get(key)
        if isinstance(over, dict) and over:
            sub = dict(base)
            sub.update(over)
            merged[key] = sub
        else:
            merged[key] = dict(base)
    return merged


def omega_reason_max_tokens(policy: dict[str, Any] | None = None) -> int:
    reason = _omega_section(policy).get("reason")
    if isinstance(reason, dict):
        raw = reason.get("max_tokens")
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 4096


def omega_reason_temperature(policy: dict[str, Any] | None = None) -> float:
    reason = _omega_section(policy).get("reason")
    if isinstance(reason, dict):
        raw = reason.get("temperature")
        try:
            v = float(raw)
            if 0.0 <= v <= 2.0:
                return v
        except (TypeError, ValueError):
            pass
    return 0.2


def omega_reason_top_p(policy: dict[str, Any] | None = None) -> float:
    reason = _omega_section(policy).get("reason")
    if isinstance(reason, dict):
        raw = reason.get("top_p")
        try:
            v = float(raw)
            if 0.0 < v <= 1.0:
                return v
        except (TypeError, ValueError):
            pass
    return 0.9


def omega_reason_timeout(policy: dict[str, Any] | None = None) -> float:
    reason = _omega_section(policy).get("reason")
    if isinstance(reason, dict):
        raw = reason.get("timeout_s")
        try:
            v = float(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 240.0


def omega_chat_reflection_max_tokens(policy: dict[str, Any] | None = None) -> int:
    """Bound the private Chat reflection independently from operational foldbacks."""
    reason = _omega_section(policy).get("reason")
    configured = reason.get("chat_reflection_max_tokens") if isinstance(reason, dict) else None
    try:
        value = int(configured)
    except (TypeError, ValueError):
        value = 512
    if value <= 0:
        value = 512
    return min(value, omega_reason_max_tokens(policy))


def omega_chat_reflection_timeout(policy: dict[str, Any] | None = None) -> float:
    """Bound a private Chat reflection while respecting the configured general timeout."""
    reason = _omega_section(policy).get("reason")
    configured = reason.get("chat_reflection_timeout_s") if isinstance(reason, dict) else None
    try:
        value = float(configured)
    except (TypeError, ValueError):
        value = 60.0
    if value <= 0:
        value = 60.0
    return min(value, omega_reason_timeout(policy))


def omega_reason_stop(policy: dict[str, Any] | None = None) -> list[str] | None:
    """Stop sequences for per-episode Omega and motor planning."""
    reason = _omega_section(policy).get("reason")
    if isinstance(reason, dict):
        raw = reason.get("stop")
        if isinstance(raw, list):
            tokens = [str(s) for s in raw if str(s)]
            if tokens:
                return tokens
    return ["<end_of_turn>"]


def pressure_neuromod_weight_modulation(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    pressure = _pressure_section(policy)
    neuromod = pressure.get("neuromod") if isinstance(pressure.get("neuromod"), dict) else {}
    wm = neuromod.get("weight_modulation")
    if not isinstance(wm, dict):
        wm = {}

    def _f(key: str, default: float) -> float:
        try:
            return float(wm.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(wm.get("enabled", True)),
        "dopamine_coeff": _f("dopamine_coeff", 0.5),
        "gamma_min": _f("gamma_min", 0.25),
        "gamma_max": _f("gamma_max", 2.5),
    }


def pressure_neuromod_tonic_decay(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    pressure = _pressure_section(policy)
    neuromod = pressure.get("neuromod") if isinstance(pressure.get("neuromod"), dict) else {}
    td = neuromod.get("tonic_decay")
    if not isinstance(td, dict):
        td = {}

    def _f(key: str, default: float) -> float:
        try:
            return float(td.get(key, default))
        except (TypeError, ValueError):
            return default

    return {
        "enabled": bool(td.get("enabled", True)),
        "decay_rate": max(0.0, min(1.0, _f("decay_rate", 0.25))),
        "baseline": _f("baseline", 0.0),
    }


def pressure_winner_ceiling(
    policy: dict[str, Any] | None = None,
    *,
    override: Optional[float] = None,
) -> float:
    if override is not None:
        return max(0.0, min(1.0, float(override)))
    return pressure_open_threshold(policy)


def pressure_open_threshold(policy: dict[str, Any] | None = None) -> float:
    pressure = _pressure_section(policy)
    raw: Any = pressure.get("open_threshold", 1.0)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 1.0
    if v < 0.0 or v > 1.0:
        return 1.0
    return v


def pressure_winner_margin(policy: dict[str, Any] | None = None) -> float:
    pressure = _pressure_section(policy)
    raw: Any = pressure.get("winner_margin", 0.05)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return 0.05
    if v < 0.0 or v > 1.0:
        return 0.05
    return v


def _global_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    p = policy if policy is not None else load_policy()
    global_sec = p.get("global") if isinstance(p, dict) else None
    if isinstance(global_sec, dict) and global_sec:
        return global_sec
    return _load_global_config_yaml()


_META_DEEP_KEYS = (
    "action",
    "tool",
    "memory",
    "pack",
    "meta_a",
    "meta_b",
    "meta_b_disposition",
    "consistency",
)


def _meta_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """config/meta.yaml is authoritative for phase tables; the policy file may overlay flags."""
    yaml_meta = _load_meta_config_yaml()
    p = policy if policy is not None else load_policy()
    overlay = p.get("meta") if isinstance(p, dict) else None
    if not isinstance(overlay, dict) or not overlay:
        return yaml_meta
    merged = dict(yaml_meta)
    merged.update(overlay)
    for key in _META_DEEP_KEYS:
        base = yaml_meta.get(key)
        if not isinstance(base, dict):
            continue
        over = overlay.get(key)
        if isinstance(over, dict) and over:
            sub = dict(base)
            sub.update(over)
            merged[key] = sub
        else:
            merged[key] = dict(base)
    return merged


def meta_intent_to_action(policy: dict[str, Any] | None = None) -> dict[str, str]:
    action = _meta_section(policy).get("action")
    if not isinstance(action, dict):
        return {}
    raw = action.get("intent_to_action")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def meta_capability_tool_map(policy: dict[str, Any] | None = None) -> dict[str, str]:
    tool = _meta_section(policy).get("tool")
    if not isinstance(tool, dict):
        return {}
    raw = tool.get("capability_map")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()}


def runtime_endpoint(policy: dict[str, Any] | None = None) -> str:
    """omega-portable-lite base URL (``OMEGA_RUNTIME_URL`` overrides in client)."""
    del policy
    raw = _load_runtime_config_yaml().get("endpoint")
    if isinstance(raw, str) and raw.strip():
        return raw.strip().rstrip("/")
    return "http://127.0.0.1:11434"


def runtime_failure_soft(policy: dict[str, Any] | None = None) -> bool:
    del policy
    raw = _load_runtime_config_yaml().get("failure_soft")
    if isinstance(raw, bool):
        return raw
    return True


def _parse_runtime_socket_entry(name: str, entry: Mapping[str, Any]) -> "RuntimeSocket":
    from runtime.socket import RuntimeSocket

    endpoint = str(entry.get("endpoint") or "").strip()
    capability = str(entry.get("capability") or "").strip()
    method = str(entry.get("method") or "").strip()
    if not endpoint or not capability or not method:
        raise KeyError(f"runtime socket incomplete: {name!r}")
    max_tokens: int | None = None
    raw_max = entry.get("max_tokens")
    if raw_max is not None:
        try:
            v = int(raw_max)
            if v > 0:
                max_tokens = v
        except (TypeError, ValueError):
            pass
    slot: str | None = None
    raw_slot = entry.get("slot")
    if isinstance(raw_slot, str) and raw_slot.strip():
        slot = raw_slot.strip()
    slots: dict[str, str] | None = None
    raw_slots = entry.get("slots")
    if isinstance(raw_slots, dict) and raw_slots:
        slots = {str(k): str(v) for k, v in raw_slots.items() if v}
    return RuntimeSocket(
        name=str(name),
        endpoint=endpoint,
        capability=capability,
        method=method,
        max_tokens=max_tokens,
        slot=slot,
        slots=slots,
    )


def runtime_generate_socket(policy: dict[str, Any] | None = None) -> "RuntimeSocket":
    """Shared /generate socket for cognition text passes (no model IDs)."""
    from runtime.socket import RuntimeSocket

    del policy
    entry = _load_runtime_config_yaml().get("generate")
    if isinstance(entry, dict):
        try:
            return _parse_runtime_socket_entry("generate", entry)
        except KeyError:
            pass
    return RuntimeSocket(
        name="generate",
        endpoint="/generate",
        capability="coding_model",
        method="generate_completion",
        max_tokens=None,
    )


def runtime_tool_socket(tool: str, policy: dict[str, Any] | None = None) -> "RuntimeSocket":
    """Resolve a runtime-backed tool to its HTTP endpoint socket."""
    del policy
    tools = _load_runtime_config_yaml().get("tools")
    if not isinstance(tools, dict):
        raise KeyError(f"runtime tool not configured: {tool!r}")
    entry = tools.get(tool)
    if not isinstance(entry, dict):
        raise KeyError(f"runtime tool not configured: {tool!r}")
    return _parse_runtime_socket_entry(tool, entry)


def runtime_tool_names(policy: dict[str, Any] | None = None) -> tuple[str, ...]:
    del policy
    tools = _load_runtime_config_yaml().get("tools")
    if not isinstance(tools, dict):
        return ()
    return tuple(str(name) for name in tools if isinstance(tools.get(name), dict))


def runtime_service_socket(service: str, policy: dict[str, Any] | None = None) -> "RuntimeSocket":
    """Resolve a substrate/sensory service to its HTTP endpoint socket."""
    del policy
    services = _load_runtime_config_yaml().get("services")
    if not isinstance(services, dict):
        raise KeyError(f"runtime service not configured: {service!r}")
    entry = services.get(service)
    if not isinstance(entry, dict):
        raise KeyError(f"runtime service not configured: {service!r}")
    return _parse_runtime_socket_entry(service, entry)


def runtime_service_names(policy: dict[str, Any] | None = None) -> tuple[str, ...]:
    del policy
    services = _load_runtime_config_yaml().get("services")
    if not isinstance(services, dict):
        return ()
    return tuple(str(name) for name in services if isinstance(services.get(name), dict))


def runtime_registry_slots(policy: dict[str, Any] | None = None) -> dict[str, str]:
    """Flatten slot / slots hints from all runtime.yaml sockets (for HUD/audit)."""
    del policy
    out: dict[str, str] = {}
    cfg = _load_runtime_config_yaml()

    def _collect(name: str, entry: Mapping[str, Any]) -> None:
        try:
            sock = _parse_runtime_socket_entry(name, entry)
        except KeyError:
            return
        if sock.slot:
            out[name] = sock.slot
        if sock.slots:
            for role, slot_id in sock.slots.items():
                out[f"{name}.{role}"] = slot_id

    gen = cfg.get("generate")
    if isinstance(gen, dict):
        _collect("generate", gen)
    services = cfg.get("services")
    if isinstance(services, dict):
        for name, entry in services.items():
            if isinstance(entry, dict):
                _collect(str(name), entry)
    tools = cfg.get("tools")
    if isinstance(tools, dict):
        for name, entry in tools.items():
            if isinstance(entry, dict):
                _collect(str(name), entry)
    return out


def runtime_action_socket(action: str, policy: dict[str, Any] | None = None) -> "RuntimeSocket":
    """Back-compat: all cognition text actions share the generate socket."""
    sock = runtime_generate_socket(policy)
    from runtime.socket import RuntimeSocket

    return RuntimeSocket(
        name=str(action),
        endpoint=sock.endpoint,
        capability=sock.capability,
        method=sock.method,
        max_tokens=sock.max_tokens,
        slot=sock.slot,
        slots=dict(sock.slots) if sock.slots else None,
    )


def tool_runtime_capability(name: str, policy: dict[str, Any] | None = None) -> str | None:
    """Broker capability for a runtime-backed tool (from runtime.yaml)."""
    try:
        return runtime_tool_socket(name, policy).capability
    except KeyError:
        return None


def tool_registry(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Declarative tool registry merged with runtime sockets and signed plugins."""
    del policy  # reserved for future policy/symphony.policy.yaml overlays
    raw = _load_tools_config_yaml()
    sockets = _load_runtime_config_yaml().get("tools")
    if not isinstance(sockets, dict):
        sockets = {}
    out: dict[str, Any] = {}
    for name, entry in raw.items():
        key = str(name)
        if entry is None:
            merged: dict[str, Any] = {}
        elif isinstance(entry, dict):
            merged = dict(entry)
        else:
            merged = {}
        sock = sockets.get(key)
        if isinstance(sock, dict):
            merged["socket"] = dict(sock)
        out[key] = merged
    # Plugin tools are discovered from the process-owned signed registries at
    # call time.  A failure to read plugin state must not hide the static tool
    # catalog; the plugin runtime itself remains fail-closed when invoked.
    try:
        from mcp_platform.plugin_dispatch import plugin_tool_registry_entries

        for name, entry in plugin_tool_registry_entries().items():
            out[name] = {
                "plugin": dict(entry),
                "capabilities": ["plugin", "mcp"],
                "description": str(entry.get("description") or "Signed MCP plugin capability"),
            }
    except Exception:
        pass
    return out


def plugin_tool_names(policy: dict[str, Any] | None = None) -> tuple[str, ...]:
    """Return currently installed/enabled signed plugin tool names."""

    del policy
    try:
        from mcp_platform.plugin_dispatch import plugin_tool_registry_entries

        return tuple(plugin_tool_registry_entries())
    except Exception:
        return ()


def tool_models(name: str, policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Runtime socket for a tool (endpoint/method/capability — no model IDs)."""
    try:
        sock = runtime_tool_socket(name, policy)
    except KeyError:
        return {}
    return {
        "endpoint": sock.endpoint,
        "capability": sock.capability,
        "method": sock.method,
    }


def meta_learn_default_tool(policy: dict[str, Any] | None = None) -> str:
    tool = _meta_section(policy).get("tool")
    if isinstance(tool, dict):
        v = tool.get("learn_default_tool")
        if isinstance(v, str) and v.strip():
            return v.strip()
    return "web_fetch"


def meta_pack_banned_keys(policy: dict[str, Any] | None = None) -> frozenset[str]:
    pack = _meta_section(policy).get("pack")
    if isinstance(pack, dict):
        keys = pack.get("banned_keys")
        if isinstance(keys, (list, tuple)) and keys:
            return frozenset(str(k) for k in keys)
    return frozenset(
        {
            "model",
            "llm",
            "engine",
            "prompt",
            "system_prompt",
            "temperature",
            "max_tokens",
            "tokens",
            "sampling",
            "top_p",
            "top_k",
        }
    )


def meta_memory_pattern(
    name: str,
    *,
    policy: dict[str, Any] | None = None,
    default: str = "",
) -> str:
    memory = _meta_section(policy).get("memory")
    if not isinstance(memory, dict):
        return default
    key = f"{name}_pattern" if not name.endswith("_pattern") else name
    raw = memory.get(key) or memory.get(name)
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return default


_AUTO_DEEP_KEYS = (
    "promptpack",
    "route",
    "execute",
    "recall",
    "fold",
)


def _auto_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """config/auto.yaml is authoritative; the policy file may overlay flags."""
    yaml_auto = _load_auto_config_yaml()
    p = policy if policy is not None else load_policy()
    overlay = p.get("auto") if isinstance(p, dict) else None
    if not isinstance(overlay, dict) or not overlay:
        return yaml_auto
    merged = dict(yaml_auto)
    merged.update(overlay)
    for key in _AUTO_DEEP_KEYS:
        base = yaml_auto.get(key)
        if not isinstance(base, dict):
            continue
        over = overlay.get(key)
        if isinstance(over, dict) and over:
            sub = dict(base)
            sub.update(over)
            merged[key] = sub
        else:
            merged[key] = dict(base)
    return merged


def auto_system_prompt(
    prompt_type: str | dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    *,
    memory_enriched: bool = False,
) -> str:
    if isinstance(prompt_type, dict) and policy is None:
        policy = prompt_type
        prompt_type = None
    promptpack = _auto_section(policy).get("promptpack")
    base = "You are Omega AUTO executing a Meta plan."
    variant = None
    if isinstance(promptpack, dict):
        variants = promptpack.get("system_prompts")
        if isinstance(variants, dict) and isinstance(prompt_type, str):
            raw_variant = variants.get(prompt_type)
            if isinstance(raw_variant, str) and raw_variant.strip():
                variant = raw_variant.strip()
        if variant is None:
            raw = promptpack.get("system_prompt")
            if isinstance(raw, str) and raw.strip():
                variant = raw.strip()
        body = variant or base
        formatting = promptpack.get("response_formatting")
        operator_output_types = {
            "RESPONSE_REASONING",
            "RESPONSE_QUESTION",
            "RESPONSE_ACK",
            "RESPONSE_FREEFORM",
            "PROACTIVE_REASONING",
            "PROACTIVE_ACK",
            "ASK_OPERATOR_QUERY",
            "TOOL_RESULT_SYNTHESIS",
            "DOCUMENT_SYSTEM",
        }
        if (
            prompt_type in operator_output_types
            and isinstance(formatting, str)
            and formatting.strip()
        ):
            body = f"{body}\n\n{formatting.strip()}"
        law = promptpack.get("memory_tool_law")
        if memory_enriched and isinstance(law, str) and law.strip():
            body = f"{body}\n\n{law.strip()}"
        return body
    return variant or base


def auto_response_ack_keywords(policy: dict[str, Any] | None = None) -> tuple[str, ...]:
    promptpack = _auto_section(policy).get("promptpack")
    if isinstance(promptpack, dict):
        raw = promptpack.get("response_ack_keywords")
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(k).lower() for k in raw)
    return ("acknowledge", "confirm", "receipt")


def auto_proactive_ack_keywords(policy: dict[str, Any] | None = None) -> tuple[str, ...]:
    promptpack = _auto_section(policy).get("promptpack")
    if isinstance(promptpack, dict):
        raw = promptpack.get("proactive_ack_keywords")
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(k).lower() for k in raw)
    return ("status", "notification", "notify", "update")


def auto_direct_tools_always(policy: dict[str, Any] | None = None) -> frozenset[str]:
    route = _auto_section(policy).get("route")
    if isinstance(route, dict):
        direct = route.get("direct_tools")
        if isinstance(direct, dict):
            always = direct.get("always")
            if isinstance(always, (list, tuple)) and always:
                values = {str(t) for t in always}
                return frozenset(values)
    defaults = {
        "file_reader",
        "ocr_tool",
        "desktop_tool",
        "image_tool",
    }
    return frozenset(defaults)


def auto_web_fetch_requires_query(policy: dict[str, Any] | None = None) -> bool:
    route = _auto_section(policy).get("route")
    if isinstance(route, dict):
        direct = route.get("direct_tools")
        if isinstance(direct, dict):
            wf = direct.get("web_fetch")
            if isinstance(wf, dict) and "requires_query_in_inputs" in wf:
                return bool(wf.get("requires_query_in_inputs"))
    return True


def auto_profile_max_tokens(
    profile: str,
    *,
    policy: dict[str, Any] | None = None,
    default: int = 1024,
) -> int:
    execute = _auto_section(policy).get("execute")
    if isinstance(execute, dict):
        raw_map = execute.get("profile_max_tokens")
        if isinstance(raw_map, dict) and profile in raw_map:
            try:
                v = int(raw_map[profile])
                if v > 0:
                    return v
            except (TypeError, ValueError):
                pass
    # Ceilings, not targets (operator 2026-07-12): TEXT_FAST 256 truncated real
    # teaching replies mid-sentence; keep in sync with config/auto.yaml.
    defaults = {
        "TEXT_REASONING": 2048,
        "TEXT_FAST": 1024,
        "TOOL_EXECUTOR": 512,
    }
    return defaults.get(profile, default)


def auto_web_fetch_default_max_results(policy: dict[str, Any] | None = None) -> int:
    execute = _auto_section(policy).get("execute")
    if isinstance(execute, dict):
        raw = execute.get("web_fetch_default_max_results")
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 3


def auto_memory_recall_limit(policy: dict[str, Any] | None = None) -> int:
    recall = _auto_section(policy).get("recall")
    if isinstance(recall, dict):
        raw = recall.get("default_limit")
        try:
            v = int(raw)
            if v > 0:
                return v
        except (TypeError, ValueError):
            pass
    return 10


def auto_generate_temperature(policy: dict[str, Any] | None = None) -> float:
    execute = _auto_section(policy).get("execute")
    if isinstance(execute, dict):
        raw = execute.get("default_temperature")
        try:
            v = float(raw)
            if 0.0 <= v <= 2.0:
                return v
        except (TypeError, ValueError):
            pass
    return 0.7


def auto_generate_top_p(policy: dict[str, Any] | None = None) -> float:
    execute = _auto_section(policy).get("execute")
    if isinstance(execute, dict):
        raw = execute.get("default_top_p")
        try:
            v = float(raw)
            if 0.0 < v <= 1.0:
                return v
        except (TypeError, ValueError):
            pass
    return 0.9


def auto_memory_enriched_bumps_fast_to_reasoning(
    policy: dict[str, Any] | None = None,
) -> bool:
    route = _auto_section(policy).get("route")
    if isinstance(route, dict) and "memory_enriched_bumps_fast_to_reasoning" in route:
        return bool(route.get("memory_enriched_bumps_fast_to_reasoning"))
    return True


_META_B_DEEP_KEYS = ("consistency", "disposition", "reflect")


def _meta_b_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    yaml_meta_b = _load_meta_b_config_yaml()
    p = policy if policy is not None else load_policy()
    overlay = p.get("meta_b") if isinstance(p, dict) else None
    if not isinstance(overlay, dict) or not overlay:
        return yaml_meta_b
    merged = dict(yaml_meta_b)
    merged.update(overlay)
    for key in _META_B_DEEP_KEYS:
        base = yaml_meta_b.get(key)
        if not isinstance(base, dict):
            continue
        over = overlay.get(key)
        if isinstance(over, dict) and over:
            sub = dict(base)
            sub.update(over)
            merged[key] = sub
        else:
            merged[key] = dict(base)
    return merged


def meta_b_pack_stable_fields(policy: dict[str, Any] | None = None) -> tuple[str, ...]:
    consistency = _meta_b_section(policy).get("consistency")
    if isinstance(consistency, dict):
        raw = consistency.get("pack_stable_fields")
        if isinstance(raw, (list, tuple)) and raw:
            return tuple(str(k) for k in raw)
    return ("episode_id", "action", "tool", "execution_intent")


def meta_b_cycle_kind_map(
    policy: dict[str, Any] | None = None,
) -> dict[str, tuple[str, str]]:
    disposition = _meta_b_section(policy).get("disposition")
    raw = None
    if isinstance(disposition, dict):
        raw = disposition.get("cycle_kind_map")
    if not isinstance(raw, dict) or not raw:
        raw = _meta_section(policy).get("meta_b_disposition", {}).get("cycle_kind_map")
    if isinstance(raw, dict) and raw:
        out: dict[str, tuple[str, str]] = {}
        for ck, pair in raw.items():
            if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                out[str(ck).strip().lower()] = (str(pair[0]), str(pair[1]))
        if out:
            out.setdefault("pending_input", ("skipped", "pending_input"))
            return out
    return {
        "success": ("completed", "success"),
        "partial": ("degraded", "partial"),
        "fail": ("failed", "failed"),
        "failed": ("failed", "failed"),
        "skipped": ("skipped", "skipped"),
        "pending_input": ("skipped", "pending_input"),
        "idle": ("idle", "idle"),
    }


def meta_b_unknown_cycle_kind(
    policy: dict[str, Any] | None = None,
) -> tuple[str, str]:
    disposition = _meta_b_section(policy).get("disposition")
    if isinstance(disposition, dict):
        raw = disposition.get("unknown_cycle_kind")
        if isinstance(raw, (list, tuple)) and len(raw) >= 2:
            return str(raw[0]), str(raw[1])
    return "invalid", "invalid"


_GLOBAL_B_DEEP_KEYS = (
    "ground",
    "summarize",
    "followups",
    "outcome_pack",
)


def _global_b_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    yaml_global_b = _load_global_b_config_yaml()
    p = policy if policy is not None else load_policy()
    overlay = p.get("global_b") if isinstance(p, dict) else None
    if not isinstance(overlay, dict) or not overlay:
        if yaml_global_b:
            return yaml_global_b
        g = _global_section(policy)
        return {k: g[k] for k in ("ground", "summarize", "followups", "outcome_pack") if k in g}
    merged = dict(yaml_global_b)
    merged.update(overlay)
    for key in _GLOBAL_B_DEEP_KEYS:
        base = yaml_global_b.get(key)
        if not isinstance(base, dict):
            base = _global_section(policy).get(key)
        if not isinstance(base, dict):
            continue
        over = overlay.get(key)
        if isinstance(over, dict) and over:
            sub = dict(base)
            sub.update(over)
            merged[key] = sub
        else:
            merged[key] = dict(base)
    return merged


def knowledge_artifact_store_path(policy: dict[str, Any] | None = None) -> str:
    """Relative path for knowledge_extractor JSONL artifact buffer."""
    if policy is not None:
        knowledge = policy.get("knowledge")
        if isinstance(knowledge, dict):
            raw = knowledge.get("artifact_store_path")
            if isinstance(raw, str) and raw.strip():
                return raw.strip()
    knowledge = _load_knowledge_config_yaml()
    raw = knowledge.get("artifact_store_path")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return "knowledge_extractor/state/artifacts.jsonl"


def knowledge_learn_integration_wired(policy: dict[str, Any] | None = None) -> bool:
    """Whether LEARN → knowledge_extractor runs after successful USE_TOOL."""
    if policy is not None:
        knowledge = policy.get("knowledge")
        if isinstance(knowledge, dict):
            learn = knowledge.get("learn_integration")
            if isinstance(learn, dict) and "wired" in learn:
                return bool(learn.get("wired"))
    knowledge = _load_knowledge_config_yaml()
    learn = knowledge.get("learn_integration")
    if isinstance(learn, dict) and "wired" in learn:
        return bool(learn.get("wired"))
    return False


def _deepsleep_knowledge_integration_section(
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if policy is not None:
        deepsleep = policy.get("deepsleep")
        if isinstance(deepsleep, dict):
            ki = deepsleep.get("knowledge_integration")
            if isinstance(ki, dict):
                return ki
    deepsleep = _load_deepsleep_config_yaml()
    ki = deepsleep.get("knowledge_integration")
    return ki if isinstance(ki, dict) else {}


def _deepsleep_stores_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return the configured DeepSleep store paths.

    Store paths are nested under ``deepsleep.stores`` in the governed config
    and schema. Keeping the lookup here prevents configured redirects from
    silently falling back to hard-coded defaults.
    """
    if policy is not None:
        deepsleep = policy.get("deepsleep")
        if isinstance(deepsleep, dict):
            stores = deepsleep.get("stores")
            if isinstance(stores, dict):
                return stores
    deepsleep = _load_deepsleep_config_yaml()
    stores = deepsleep.get("stores")
    return stores if isinstance(stores, dict) else {}


def deepsleep_knowledge_consume_on_consolidate(
    policy: dict[str, Any] | None = None,
) -> bool:
    ki = _deepsleep_knowledge_integration_section(policy)
    if "consume_on_consolidate" in ki:
        return bool(ki.get("consume_on_consolidate"))
    return False


def _deepsleep_memory_consolidation_section(
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if policy is not None:
        deepsleep = policy.get("deepsleep")
        if isinstance(deepsleep, dict):
            mc = deepsleep.get("memory_consolidation")
            if isinstance(mc, dict):
                return mc
    deepsleep = _load_deepsleep_config_yaml()
    mc = deepsleep.get("memory_consolidation")
    return mc if isinstance(mc, dict) else {}


def deepsleep_memory_consolidation_enabled(
    policy: dict[str, Any] | None = None,
) -> bool:
    mc = _deepsleep_memory_consolidation_section(policy)
    if "enabled" in mc:
        return bool(mc.get("enabled"))
    return False


def deepsleep_knowledge_artifact_store_path(
    policy: dict[str, Any] | None = None,
) -> str:
    ki = _deepsleep_knowledge_integration_section(policy)
    raw = ki.get("artifact_store")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return knowledge_artifact_store_path(policy)


def deepsleep_journal_path(policy: dict[str, Any] | None = None) -> Path:
    """Relative path for the Slice 6a DeepSleep journal JSONL buffer."""
    raw = _deepsleep_stores_section(policy).get("journal_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return Path("deepsleep_loop/state/journal.jsonl")


def _deepsleep_mastery_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    if policy is not None:
        deepsleep = policy.get("deepsleep")
        if isinstance(deepsleep, dict):
            mastery = deepsleep.get("mastery")
            if isinstance(mastery, dict):
                return mastery
    deepsleep = _load_deepsleep_config_yaml()
    mastery = deepsleep.get("mastery")
    return mastery if isinstance(mastery, dict) else {}


def deepsleep_mastery_path(policy: dict[str, Any] | None = None) -> Path:
    """Relative path for the Slice 6b DeepSleep mastery JSON store."""
    raw = _deepsleep_stores_section(policy).get("mastery_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return Path("deepsleep_loop/state/mastery.json")


def deepsleep_mastery_default_target(policy: dict[str, Any] | None = None) -> float:
    mastery = _deepsleep_mastery_section(policy)
    raw = mastery.get("default_target", 0.70)
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.70


def _deepsleep_identity_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    if policy is not None:
        deepsleep = policy.get("deepsleep")
        if isinstance(deepsleep, dict):
            identity = deepsleep.get("identity")
            if isinstance(identity, dict):
                return identity
    deepsleep = _load_deepsleep_config_yaml()
    identity = deepsleep.get("identity")
    return identity if isinstance(identity, dict) else {}


def deepsleep_identity_path(policy: dict[str, Any] | None = None) -> Path:
    """Legacy import path for pre-canonical DeepSleep identity traits.

    Promoted traits persist in canonical memory lobe 0 / subdomain 1; unpromoted
    pattern observations remain separate in lobe 0 / subdomain 3.
    This path is retained only so the old ``deepsleep_loop/state/identity.json``
    snapshot can be imported during cutover/testing.
    """
    raw = _deepsleep_stores_section(policy).get("identity_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return Path("deepsleep_loop/state/identity.json")


def deepsleep_identity_strength_increment(policy: dict[str, Any] | None = None) -> float:
    identity = _deepsleep_identity_section(policy)
    raw = identity.get("strength_increment", 0.10)
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.10


def deepsleep_identity_promote_evidence(policy: dict[str, Any] | None = None) -> int:
    """Independent supporting observations required for a system preference."""
    identity = _deepsleep_identity_section(policy)
    raw = identity.get("promote_evidence", 2)
    try:
        return max(2, int(raw))
    except (TypeError, ValueError):
        return 2


def _deepsleep_semantic_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    if policy is not None:
        deepsleep = policy.get("deepsleep")
        if isinstance(deepsleep, dict):
            semantic = deepsleep.get("semantic")
            if isinstance(semantic, dict):
                return semantic
    deepsleep = _load_deepsleep_config_yaml()
    semantic = deepsleep.get("semantic")
    return semantic if isinstance(semantic, dict) else {}


def deepsleep_semantic_path(policy: dict[str, Any] | None = None) -> Path:
    raw = _deepsleep_stores_section(policy).get("semantic_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return Path("deepsleep_loop/state/semantic.json")


def deepsleep_semantic_promote_evidence(policy: dict[str, Any] | None = None) -> int:
    semantic = _deepsleep_semantic_section(policy)
    raw = semantic.get("promote_evidence", 3)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 3


def deepsleep_semantic_developing_threshold(
    policy: dict[str, Any] | None = None,
) -> float:
    semantic = _deepsleep_semantic_section(policy)
    raw = semantic.get("developing_knowledge_threshold")
    try:
        v = float(raw)
        if 0.0 <= v <= 1.0:
            return v
    except (TypeError, ValueError):
        pass
    return 0.40


def deepsleep_mastery_status_thresholds(
    policy: dict[str, Any] | None = None,
) -> dict[str, float]:
    mastery = _deepsleep_mastery_section(policy)
    thresholds = mastery.get("status_thresholds")
    if isinstance(thresholds, dict):
        out: dict[str, float] = {}
        for key in ("learning_max", "operational_max"):
            raw = thresholds.get(key)
            try:
                out[key] = float(raw)
            except (TypeError, ValueError):
                pass
        if out:
            return out
    return {"learning_max": 0.60, "operational_max": 0.80}


def _deepsleep_trajectory_section(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    if policy is not None:
        deepsleep = policy.get("deepsleep")
        if isinstance(deepsleep, dict):
            trajectory = deepsleep.get("trajectory")
            if isinstance(trajectory, dict):
                return trajectory
    deepsleep = _load_deepsleep_config_yaml()
    trajectory = deepsleep.get("trajectory")
    return trajectory if isinstance(trajectory, dict) else {}


def deepsleep_trajectory_path(policy: dict[str, Any] | None = None) -> Path:
    raw = _deepsleep_stores_section(policy).get("trajectory_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return Path("deepsleep_loop/state/trajectory.json")


def deepsleep_trajectory_decay_after(policy: dict[str, Any] | None = None) -> int:
    trajectory = _deepsleep_trajectory_section(policy)
    raw = trajectory.get("decay_after", 10)
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 10


def deepsleep_trajectory_decay_increment(policy: dict[str, Any] | None = None) -> float:
    trajectory = _deepsleep_trajectory_section(policy)
    raw = trajectory.get("decay_increment", 0.05)
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.05


def deepsleep_failure_path(policy: dict[str, Any] | None = None) -> Path:
    raw = _deepsleep_stores_section(policy).get("failure_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return Path("deepsleep_loop/state/failure.json")


def deepsleep_projection_path(policy: dict[str, Any] | None = None) -> Path:
    """Relative path for the Slice 6d DeepSleep projection JSON store."""
    raw = _deepsleep_stores_section(policy).get("projection_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return Path("deepsleep_loop/state/projection.json")


def deepsleep_query_path(policy: dict[str, Any] | None = None) -> Path:
    """Relative path for the Slice 6d DeepSleep open query JSON store."""
    raw = _deepsleep_stores_section(policy).get("query_path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return Path("deepsleep_loop/state/open_queries.json")


def episode_journal_path(policy: dict[str, Any] | None = None) -> Path:
    """Relative path for the Slice 5a episode journal JSONL buffer."""
    if policy is not None:
        journal = policy.get("episode_journal")
        if isinstance(journal, dict):
            raw = journal.get("path")
            if isinstance(raw, str) and raw.strip():
                return Path(raw.strip())
    journal = _load_journal_config_yaml()
    raw = journal.get("path")
    if isinstance(raw, str) and raw.strip():
        return Path(raw.strip())
    return Path("pressure_loop/state/episode_journal.jsonl")


def episode_journal_window_default(policy: dict[str, Any] | None = None) -> int:
    """Default N used by `pressure_loop.episode_journal.journal_window()`."""
    if policy is not None:
        journal = policy.get("episode_journal")
        if isinstance(journal, dict):
            raw = journal.get("window_default")
            if isinstance(raw, int) and raw > 0:
                return raw
            if isinstance(raw, str):
                try:
                    v = int(raw)
                    if v > 0:
                        return v
                except ValueError:
                    pass
    journal = _load_journal_config_yaml()
    raw = journal.get("window_default")
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str):
        try:
            v = int(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return 50


def global_b_ground_auto_output_paths(
    policy: dict[str, Any] | None = None,
) -> dict[str, str]:
    ground = _global_b_section(policy).get("ground")
    if isinstance(ground, dict):
        raw = ground.get("auto_output_paths")
        if isinstance(raw, dict) and raw:
            paths = {str(k): str(v) for k, v in raw.items()}
            for key in (
                "plugin_plan",
                "plugin_step_results",
                "plugin_permission_request",
                "plugin_result",
                "plugin_failure",
            ):
                paths.setdefault(key, f"auto.outputs.{key}")
            return paths
    return {
        "response_text": "auto.outputs.response_text",
        "tool_result": "auto.outputs.tool_result",
        "tool_result_summary": "auto.outputs.tool_result_summary",
        "image_artifact": "auto.outputs.image_artifact",
        "plugin_plan": "auto.outputs.plugin_plan",
        "plugin_step_results": "auto.outputs.plugin_step_results",
        "plugin_permission_request": "auto.outputs.plugin_permission_request",
        "plugin_result": "auto.outputs.plugin_result",
        "plugin_failure": "auto.outputs.plugin_failure",
    }


def global_b_ground_meta_disposition_path(policy: dict[str, Any] | None = None) -> str:
    ground = _global_b_section(policy).get("ground")
    if isinstance(ground, dict):
        raw = ground.get("meta_disposition_path")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return "meta.execution.disposition"


def global_b_summarize_completed_by_action(
    policy: dict[str, Any] | None = None,
) -> dict[str, str]:
    summarize = _global_b_section(policy).get("summarize")
    if isinstance(summarize, dict):
        raw = summarize.get("completed_by_action")
        if isinstance(raw, dict) and raw:
            return {str(k): str(v) for k, v in raw.items()}
    return {
        "USE_TOOL": "Tool execution completed successfully.",
        "RESPONSE": "Response generated successfully.",
        "PROACTIVE_RESPONSE": "Proactive response generated successfully.",
        "IMAGE_CREATION": "Image creation completed successfully.",
        "default": "Execution completed successfully.",
    }


def global_b_summarize_template(
    key: str,
    *,
    policy: dict[str, Any] | None = None,
    default: str = "",
) -> str:
    summarize = _global_b_section(policy).get("summarize")
    if isinstance(summarize, dict):
        raw = summarize.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    defaults = {
        "degraded": "Execution partially completed with degradation.",
        "skipped": "No execution was performed.",
        "idle": "Chose idle with a recorded reason.",
        "invalid": "Outcome invalid due to packet inconsistency or missing trace.",
        "failed_template": "Execution failed: {failure_kind}.",
    }
    return defaults.get(key, default)


def global_b_followup_clarification_kinds(
    policy: dict[str, Any] | None = None,
) -> frozenset[str]:
    followups = _global_b_section(policy).get("followups")
    if isinstance(followups, dict):
        raw = followups.get("clarification_failure_kinds")
        if isinstance(raw, (list, tuple)) and raw:
            return frozenset(str(k).lower() for k in raw)
    return frozenset(
        {
            "missing_params",
            "invalid_execution_pack",
            "clarification",
            "missing_param",
        }
    )


def global_b_followup_tool_kinds(policy: dict[str, Any] | None = None) -> frozenset[str]:
    followups = _global_b_section(policy).get("followups")
    if isinstance(followups, dict):
        raw = followups.get("tool_failure_kinds")
        if isinstance(raw, (list, tuple)) and raw:
            return frozenset(str(k).lower() for k in raw)
    return frozenset({"tool_error", "transient", "tool_timeout", "runtime_unavailable"})


def global_b_followup_rules(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    followups = _global_b_section(policy).get("followups")
    rules = followups.get("rules") if isinstance(followups, dict) else None
    return dict(rules) if isinstance(rules, dict) else {}


def global_b_memory_success_status(policy: dict[str, Any] | None = None) -> str:
    outcome = _global_b_section(policy).get("outcome_pack")
    if isinstance(outcome, dict):
        raw = outcome.get("memory_success_status")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return "success"


def assert_pressure_boot_allowed() -> None:
    if not symphony_mode_enabled():
        return
    if pressure_conductor_wired():
        return
    raise RuntimeError(
        "conductor_mode is 'symphony' but pressure.conductor_wired is "
        "false (or unset) in policy/symphony.policy.yaml"
    )


def clear_policy_cache() -> None:
    load_policy.cache_clear()
    _load_pressure_config_yaml.cache_clear()
    _load_global_config_yaml.cache_clear()
    _load_meta_config_yaml.cache_clear()
    _load_auto_config_yaml.cache_clear()
    _load_meta_b_config_yaml.cache_clear()
    _load_global_b_config_yaml.cache_clear()
    _load_tools_config_yaml.cache_clear()
    _load_knowledge_config_yaml.cache_clear()
    _load_journal_config_yaml.cache_clear()
    _load_deepsleep_config_yaml.cache_clear()
    _load_omega_config_yaml.cache_clear()
    _load_runtime_config_yaml.cache_clear()

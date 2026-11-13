"""
Config Loader
=============

Loads YAML configuration files from the config/ directory.
Each loader returns a plain dict — no interpretation, no defaults.
If the file is missing or malformed, raises with file context.

ARCHITECTURAL NOTES:
  - Logger attached at DEBUG with file size + top-level key count per load.
  - In-process cache via ``_CACHE`` / ``_CACHE_LOCK`` (see ``clear_config_cache()``).
   - Governed config loaders validate against ``schema/{stem}.schema.yaml`` at
     read time; ``REQUIRED_KEYS`` remains a small pre-schema guard for clearer
     filename-specific errors.
   - Remaining direct YAML reads should migrate to loader functions for a
     single edit path.
"""

import logging
import threading
import yaml
from pathlib import Path
from typing import Dict, Iterable

_CONFIG_DIR = Path(__file__).resolve().parent
_POLICY_CONTRACT_DIR = _CONFIG_DIR.parent / "policy"

# Governed config stems whose on-disk filename differs from ``{stem}.yaml``.
CONFIG_STEM_ALIASES: dict[str, str] = {
    "guardian": "guardian_response_classes.yaml",
}

log = logging.getLogger("omega.config.loader")


# =====================================================================
# FLAW-23 RESOLVED: per-filename cache (per the "Configs are loaded
# once" doctrine in CLAUDE.md). Cache is process-local; tests can
# call clear_cache() to reset.
# =====================================================================

_CACHE: Dict[str, dict] = {}
_CACHE_LOCK = threading.RLock()


def clear_cache() -> None:
    """Drop the per-filename cache. Useful for tests + operator hot-reload."""
    with _CACHE_LOCK:
        _CACHE.clear()


# =====================================================================
# FLAW-22 RESOLVED: schema validation. Per-loader required-keys
# registry + _validate helper raises with filename context on missing
# required keys. Callers register their schemas via REQUIRED_KEYS;
# loaders without a registered schema skip validation (preserves the
# current any-shape contract for unregistered files).
# =====================================================================

# Map filename → set of required top-level keys. Add new entries as
# loaders ship their schema. Empty / missing entry = no validation
# (current behavior preserved).
REQUIRED_KEYS: Dict[str, Iterable[str]] = {
    # Sample registrations — extend as audits surface concrete shape
    # contracts. Each entry says "this file must have these top-level
    # keys to count as valid." Type checking left to consumers (the
    # "value-type dict" extension noted in the FLAW description is a
    # follow-up if the simple required-keys check proves insufficient).
    "deepsleep.yaml": ("deepsleep",),
    "curiosity.yaml": ("limits",),
    "dopamine.yaml": ("dopamine",),
    "strength_index.yaml": ("strength_index",),
    "boot.yaml": ("boot",),
    "cognition_chain.yaml": ("cognition_chain",),
    "reflection_window.yaml": ("reflection_window",),
    "operator_chat.yaml": ("operator_chat",),
    "sensory.yaml": ("sensory",),
    "tts.yaml": ("tts",),
    "directives.yaml": ("directives",),
    "memory.yaml": ("memory",),
    "skill_execution.yaml": ("skill_execution",),
    "guardian_response_classes.yaml": ("response_classes",),
}


def _validate(data: dict, filename: str) -> None:
    """Check `data` has the required top-level keys per REQUIRED_KEYS.
    No-op when filename has no registered schema. Raises ValueError
    with filename context on missing keys.
    """
    required = REQUIRED_KEYS.get(filename)
    if not required:
        return
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError(
            f"config/{filename} schema violation: missing required "
            f"top-level key(s) {missing} (expected: {sorted(required)})"
        )


def _load_yaml(filename: str, *, schema_stem: str | None = None) -> dict:
    """Load and parse a YAML config file. Logs DEBUG on success +
    raises with filename context on failure. Cached per-filename
    after first read (FLAW-23). Validated per REQUIRED_KEYS (FLAW-22)."""
    cache_key = f"yaml:{filename}"
    if schema_stem:
        cache_key = f"yaml:{filename}:schema:{schema_stem}"
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached
        path = _CONFIG_DIR / filename
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except FileNotFoundError as exc:
            log.error("config: %s not found at %s", filename, path)
            raise FileNotFoundError(f"config/{filename} missing — expected at {path}") from exc
        except yaml.YAMLError as exc:
            log.error("config: %s parse failed: %s", filename, exc)
            raise yaml.YAMLError(f"config/{filename} malformed: {exc}") from exc
        if data is None:
            data = {}
        if not isinstance(data, dict):
            log.warning(
                "config: %s top-level is %s (expected dict); wrapping",
                filename,
                type(data).__name__,
            )
            data = {"_value": data}
        _validate(data, filename)
        if schema_stem:
            from config.schema_validate import validate_config_document

            validate_config_document(schema_stem, data)
        log.debug(
            "config: loaded %s (%d bytes, %d top-level keys)",
            filename,
            path.stat().st_size,
            len(data),
        )
        _CACHE[cache_key] = data
        return data


def _load_governed_config(stem: str) -> dict:
    """Load config for a governed stem with JSON Schema validation."""
    return _load_yaml(config_filename_for_stem(stem), schema_stem=stem)


def load_dopamine_config() -> dict:
    return _load_governed_config("dopamine")


def load_strength_index_config() -> dict:
    return _load_governed_config("strength_index")


def load_memory_config() -> dict:
    return _load_governed_config("memory")


def load_governance_config() -> dict:
    """Load governance.yaml when present; absence is valid (defaults apply)."""
    cache_key = "yaml:governance.yaml"
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    path = _CONFIG_DIR / "governance.yaml"
    if not path.is_file():
        log.debug("config: governance.yaml absent — using defaults")
        with _CACHE_LOCK:
            _CACHE[cache_key] = {}
        return {}
    return _load_yaml("governance.yaml")


def load_tools_config() -> dict:
    return _load_yaml("tools.yaml")


def load_web_sources_config() -> dict:
    """Curated, live-verified URL catalog consumed by tools.web_fetch.

    Schema (config/web_sources.yaml):
        schema_version: int
        sources:        list[{url, name, tier, domain, note}]
        tier_order:     list[str]   # primary -> defer
        domain_defaults: dict[str, str]  # domain -> default tier ceiling
    """
    return _load_governed_config("web_sources")


def load_deepsleep_config() -> dict:
    """Canonical v2 name — config/deepsleep.yaml (pressure_loop.policy)."""
    return _load_governed_config("deepsleep")


def load_omega_config() -> dict:
    return _load_governed_config("omega")


# ---------------------------------------------------------------------
# Cognitive doctrine configs (per docs/contracts/02 + 03)
# ---------------------------------------------------------------------


def load_curiosity_config() -> dict:
    """Curiosity Engine thresholds + routing (per docs/contracts/03 v3 —
    supersedes the v2 form in docs/contracts/02)."""
    return _load_governed_config("curiosity")


def load_boot_config() -> dict:
    """Boot lifecycle component order and manifest paths."""
    return _load_governed_config("boot")


def load_cognition_chain_config() -> dict:
    """Executive cascade handoff after pressure ignition."""
    return _load_governed_config("cognition_chain")


def load_reflection_window_config() -> dict:
    """Windowed Omega reflection window tunables."""
    return _load_governed_config("reflection_window")


def load_operator_chat_config() -> dict:
    """Operator chat ingress adapter tunables."""
    return _load_governed_config("operator_chat")


def load_sensory_config() -> dict:
    """Sensory / operator presence latch tunables."""
    return _load_governed_config("sensory")


def load_tts_config() -> dict:
    """TTS voice defaults (runtime socket in config/runtime.yaml)."""
    return _load_governed_config("tts")


def load_directives_config() -> dict:
    """IMAGE_CREATE / DOCUMENT_CREATE directive routing."""
    return _load_governed_config("directives")


def load_skill_execution_config() -> dict:
    """Phase 6 — USE_SKILL routing, user skill trust, guard defaults."""
    return _load_governed_config("skill_execution")


def config_filename_for_stem(stem: str) -> str:
    """Resolve governed config filename for a policy/schema stem."""
    key = str(stem or "").strip()
    if not key:
        raise ValueError("config stem required")
    return CONFIG_STEM_ALIASES.get(key, f"{key}.yaml")


def config_path_for_stem(stem: str) -> Path:
    return _CONFIG_DIR / config_filename_for_stem(stem)


def load_runtime_config() -> dict:
    """Portable-lite runtime socket tunables."""
    return _load_governed_config("runtime")


def load_policy_contract(stem: str) -> dict:
    """Load ``policy/{stem}.policy.yaml`` behavioral contract."""
    key = str(stem or "").strip()
    if not key:
        raise ValueError("policy stem required")
    cache_key = f"policy_contract:{key}"
    cached = _CACHE.get(cache_key)
    if cached is not None:
        return cached
    with _CACHE_LOCK:
        cached = _CACHE.get(cache_key)
        if cached is not None:
            return cached
        path = _POLICY_CONTRACT_DIR / f"{key}.policy.yaml"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except FileNotFoundError as exc:
            log.error("policy contract: %s not found at %s", key, path)
            raise FileNotFoundError(
                f"policy/{key}.policy.yaml missing — expected at {path}"
            ) from exc
        except yaml.YAMLError as exc:
            log.error("policy contract: %s parse failed: %s", key, exc)
            raise yaml.YAMLError(f"policy/{key}.policy.yaml malformed: {exc}") from exc
        if data is None:
            data = {}
        if not isinstance(data, dict):
            data = {"_value": data}
        log.debug(
            "policy contract: loaded %s (%d bytes, %d top-level keys)",
            key,
            path.stat().st_size,
            len(data),
        )
        _CACHE[cache_key] = data
        return data


def load_subsystem_bundle(stem: str) -> dict[str, dict]:
    """Config tunables + policy contract for one governed stem."""
    return {
        "config": _load_governed_config(stem),
        "policy": load_policy_contract(stem),
    }


# Removal record (do not resurrect — see docs/audits/DEAD_CODE_MAP_CONFIG_SCHEMA_POLICY.md):
#
# 2026-05-14: removed load_tool_creation_policy() + load_deepsleep_policy().
# tool_creation_policy.yaml was deleted — its rules (sandbox required,
# guardian approval mandatory, rollback path declared) are enforced
# in code by guardian.tool_authority. deepsleep_policy.yaml was
# deleted — no live consumer.
#
# 2026-06-17: removed load_runtime_policy() — FLAW-21 closed by deletion. Its
# direct reader core/runtime_policy.py and config/runtime_policy.yaml are both
# gone; runtime config routes through load_runtime_config() / runtime.policy.yaml.
#
# 2026-06-22: removed retired pre-v2 loader stubs that pointed at missing YAML
# (queue, failure, goals, trait, autonomous_loop, brain_loop, phase_registry,
# phase_clock, omega_intervention, system, dialogue_acts, affect_attribution)
# plus unused load_meta_config / load_global_config (pressure_loop.policy owns
# those paths). Re-adding them would resurrect dead config surfaces.
#
# 2026-07-04: removed the core/policy tripwire block (_load_policy, load_policy
# alias, load_memory_policy, load_identity_policy_yaml, load_curiosity_policy,
# load_development_policy) — the doctrine-parity-stub pattern is retired; policy
# contracts live in policy/{stem}.policy.yaml via load_policy_contract(). Also
# removed load_identity_policy_json (target file deleted with identity_guard),
# load_knowledge_domains_config (target never existed), load_development_config
# (engine retired), and the load_deep_sleep_config alias.


def load_guardian_response_classes() -> dict:
    """Guardian response classes (decision-class taxonomy)."""
    return _load_governed_config("guardian")

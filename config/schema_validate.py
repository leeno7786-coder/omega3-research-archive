"""JSON Schema validation for governed config/policy triplets."""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger("omega.config.schema_validate")

_SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schema"

# stem -> root definition name in schema/{stem}.schema.yaml
SCHEMA_ROOT_BY_STEM: dict[str, str] = {
    "boot": "BootConfig",
    "cognition_chain": "CognitionChainConfig",
    "reflection_window": "ReflectionWindowConfig",
    "operator_chat": "OperatorChatConfig",
    "sensory": "SensoryConfig",
    "tts": "TTSConfig",
    "directives": "DirectiveConfig",
    "runtime": "RuntimeConfig",
    "memory": "MemoryConfig",
    "curiosity": "CuriosityConfig",
    "dopamine": "DopamineConfig",
    "strength_index": "StrengthIndexConfig",
    "guardian": "GuardianConfig",
    "skill_execution": "SkillExecutionConfig",
    # 2026-07-04 governance close — the cascade + remaining stems, validated at
    # read time by pressure_loop.policy._validate_stem / config.loader:
    "auto": "AutoConfig",
    "pressure": "PressureConfig",
    "global": "GlobalConfig",
    "global_b": "GlobalBConfig",
    "meta": "MetaConfig",
    "meta_b": "MetaBConfig",
    "omega": "OmegaConfig",
    "deepsleep": "DeepSleepConfig",
    "knowledge": "KnowledgeConfig",
    "journal": "EpisodeJournalConfig",
    "tools": "ToolsConfig",
    "global_intent": "IntentRoutingConfig",
    "web_sources": "WebSourcesConfig",
    "pressure_contributors": "PressureContributorsConfig",
}

# Stems without JSON Schema validation at load time.
SCHEMA_VALIDATE_SKIP: frozenset[str] = frozenset()


@lru_cache(maxsize=64)
def _load_schema_document(stem: str) -> dict[str, Any]:
    path = _SCHEMA_DIR / f"{stem}.schema.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"schema/{stem}.schema.yaml must be a mapping")
    return data


def _validator_for_stem(stem: str):
    from jsonschema import Draft7Validator
    from jsonschema.validators import RefResolver

    def_name = SCHEMA_ROOT_BY_STEM[stem]
    doc = _load_schema_document(stem)
    definitions = doc.get("definitions") or {}
    subschema = definitions.get(def_name)
    if not isinstance(subschema, dict):
        raise ValueError(f"schema/{stem}.schema.yaml missing definitions.{def_name}")
    resolver = RefResolver.from_schema(doc)
    return Draft7Validator(subschema, resolver=resolver)


def validate_config_document(stem: str, data: dict[str, Any]) -> None:
    """Raise ValueError when ``data`` violates the stem's JSON Schema."""
    if stem in SCHEMA_VALIDATE_SKIP:
        return
    if stem not in SCHEMA_ROOT_BY_STEM:
        return
    schema_path = _SCHEMA_DIR / f"{stem}.schema.yaml"
    if not schema_path.is_file():
        # A REGISTERED stem with no schema on disk is a governance hole, not a
        # skip condition — fail loud (2026-07-04; the silent return here was the
        # same class of bypass as the pre-P0 config shadowing).
        raise FileNotFoundError(f"schema/{stem}.schema.yaml missing for governed stem '{stem}'")
    validator = _validator_for_stem(stem)
    errors = sorted(validator.iter_errors(data), key=lambda e: e.path)
    if not errors:
        return
    first = errors[0]
    path = ".".join(str(p) for p in first.path) or "<root>"
    raise ValueError(f"config/{stem} schema violation at {path}: {first.message}")

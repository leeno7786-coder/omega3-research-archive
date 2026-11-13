"""Closed JSON Schema validation boundary for plugin manifests."""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache
import json

from jsonschema import Draft202012Validator, ValidationError, validators
from jsonschema.protocols import Validator

from core.paths import OMEGA_ROOT


_PLUGIN_MANIFEST_SCHEMA_PATH = (
    OMEGA_ROOT / "schema" / "plugins" / "plugin_manifest.schema.json"
)
_UNIQUE_BY_KEYWORD = "x-omega-uniqueBy"


def _validate_unique_by(
    _validator: object,
    field_name: object,
    instance: object,
    _schema: object,
) -> Iterator[ValidationError]:
    if not isinstance(instance, list) or not isinstance(field_name, str):
        return
    seen: set[str] = set()
    for index, item in enumerate(instance):
        if not isinstance(item, dict):
            continue
        identity = item.get(field_name)
        if not isinstance(identity, str):
            continue
        if identity in seen:
            yield ValidationError(
                f"array item {index} duplicates {field_name!r} identity",
                path=[index, field_name],
            )
        seen.add(identity)


_OMEGA_PLUGIN_MANIFEST_VALIDATOR = validators.extend(
    Draft202012Validator,
    {_UNIQUE_BY_KEYWORD: _validate_unique_by},
)


@lru_cache(maxsize=1)
def _plugin_manifest_schema_validator() -> Validator:
    schema_document = json.loads(_PLUGIN_MANIFEST_SCHEMA_PATH.read_text(encoding="utf-8"))
    if not isinstance(schema_document, dict):
        raise ValueError("plugin manifest schema must be an object")
    Draft202012Validator.check_schema(schema_document)
    return _OMEGA_PLUGIN_MANIFEST_VALIDATOR(schema_document)


def validate_plugin_manifest_schema_document(document: object) -> None:
    """Validate one raw manifest against Omega's complete schema vocabulary."""

    _plugin_manifest_schema_validator().validate(document)

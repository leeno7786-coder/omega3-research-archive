"""Governed configs must validate against schema/{stem}.schema.yaml."""

from __future__ import annotations

import pytest

from config import loader
from config.schema_validate import SCHEMA_ROOT_BY_STEM, validate_config_document


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    loader.clear_cache()
    yield
    loader.clear_cache()


@pytest.mark.parametrize("stem", sorted(SCHEMA_ROOT_BY_STEM.keys()))
def test_live_config_validates_against_schema(stem: str):
    data = loader._load_governed_config(stem)
    validate_config_document(stem, data)


def test_invalid_boot_config_raises():
    with pytest.raises(ValueError, match="schema violation"):
        validate_config_document("boot", {"boot": {"enabled": "yes"}})

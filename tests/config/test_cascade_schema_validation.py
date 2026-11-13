"""Cascade config governance (2026-07-04 reorg, Phase 3).

Pins the close of the validation bypass: the cascade configs load through
pressure_loop.policy's direct readers, which previously did raw yaml.safe_load
with NO schema validation — the most load-bearing configs were the unvalidated
ones. Now every stem is registered in SCHEMA_ROOT_BY_STEM and the readers call
_validate_stem() on every successful read.

Both directions of the gate, per the RED doctrine:
  - a live config VALIDATES and its VALUES flow through the reader;
  - a config that parses but violates its schema RAISES (never silently
    shadow-collapses to {} — the P0 config-shadowing lesson);
  - an ABSENT config keeps its pinned fail-soft {} fallback;
  - a REGISTERED stem whose schema file is missing RAISES (governance hole,
    not a skip).
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from config.schema_validate import SCHEMA_ROOT_BY_STEM, validate_config_document
from core.paths import OMEGA_ROOT

# Every stem governed as of the 2026-07-04 close. New config stems must be
# added HERE and to SCHEMA_ROOT_BY_STEM + schema/{stem}.schema.yaml.
CASCADE_STEMS = [
    "auto",
    "pressure",
    "global",
    "global_b",
    "meta",
    "meta_b",
    "omega",
    "deepsleep",
    "knowledge",
    "journal",
    "tools",
    "global_intent",
    "web_sources",
    "pressure_contributors",
]


@pytest.mark.parametrize("stem", CASCADE_STEMS)
def test_stem_registered_and_live_config_validates(stem):
    assert stem in SCHEMA_ROOT_BY_STEM, f"{stem} missing from SCHEMA_ROOT_BY_STEM"
    assert (OMEGA_ROOT / "schema" / f"{stem}.schema.yaml").is_file()
    doc = yaml.safe_load((OMEGA_ROOT / "config" / f"{stem}.yaml").read_text(encoding="utf-8"))
    validate_config_document(stem, doc)  # must not raise


@pytest.mark.parametrize("stem", CASCADE_STEMS)
def test_empty_doc_fails_validation(stem):
    with pytest.raises(ValueError):
        validate_config_document(stem, {})


def test_configured_value_reaches_reader():
    # Assert the VALUE, not presence-of-behavior: omega.reason.max_tokens as
    # configured must be what the reader returns.
    from pressure_loop import policy as plp

    plp._load_omega_config_yaml.cache_clear()
    try:
        cfg_val = yaml.safe_load(
            (OMEGA_ROOT / "config" / "omega.yaml").read_text(encoding="utf-8")
        )["omega"]["reason"]["max_tokens"]
        assert plp._load_omega_config_yaml()["reason"]["max_tokens"] == cfg_val
    finally:
        plp._load_omega_config_yaml.cache_clear()


def test_malformed_pressure_config_raises_not_collapses(tmp_path, monkeypatch):
    # Parses fine, violates schema (pressure must be an object) → the reader
    # must RAISE, never return {} and let the fallback synthesis shadow it.
    from pressure_loop import policy as plp

    bad = tmp_path / "pressure.yaml"
    bad.write_text("pressure: [not, a, mapping]\n", encoding="utf-8")
    monkeypatch.setattr(plp, "_PRESSURE_CONFIG_PATH", bad)
    plp._load_pressure_config_yaml.cache_clear()
    try:
        with pytest.raises(ValueError, match="schema violation"):
            plp._load_pressure_config_yaml()
    finally:
        plp._load_pressure_config_yaml.cache_clear()


def test_absent_pressure_config_stays_failsoft(tmp_path, monkeypatch):
    # The OTHER direction: an absent file keeps the pinned {} fallback
    # (production-correct synthesis path — see test_pressure_policy_wiring).
    from pressure_loop import policy as plp

    monkeypatch.setattr(plp, "_PRESSURE_CONFIG_PATH", tmp_path / "nope.yaml")
    plp._load_pressure_config_yaml.cache_clear()
    try:
        assert plp._load_pressure_config_yaml() == {}
    finally:
        plp._load_pressure_config_yaml.cache_clear()


def test_registered_stem_with_missing_schema_raises(monkeypatch):
    monkeypatch.setitem(SCHEMA_ROOT_BY_STEM, "phantom_stem", "PhantomConfig")
    with pytest.raises(FileNotFoundError, match="phantom_stem"):
        validate_config_document("phantom_stem", {"phantom_stem": {}})


def test_malformed_meta_config_raises(tmp_path, monkeypatch):
    # Second cascade reader pinned so the gate isn't a pressure-only accident.
    from pressure_loop import policy as plp

    bad = tmp_path / "meta.yaml"
    bad.write_text(
        textwrap.dedent(
            """
            meta:
              enabled: "yes-string-not-bool"
              meta_a: {}
              meta_b: {}
              action: {}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(plp, "_META_CONFIG_PATH", bad)
    plp._load_meta_config_yaml.cache_clear()
    try:
        with pytest.raises(ValueError, match="schema violation"):
            plp._load_meta_config_yaml()
    finally:
        plp._load_meta_config_yaml.cache_clear()

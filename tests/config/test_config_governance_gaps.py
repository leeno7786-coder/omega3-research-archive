"""Governance gaps — every governed stem loads cleanly; retired surfaces stay dead."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import loader
from core.paths import OMEGA_ROOT


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    from core import memory_config

    loader.clear_cache()
    memory_config.reset_memory_config_for_tests()
    yield
    loader.clear_cache()
    memory_config.reset_memory_config_for_tests()


def test_tripwire_policy_surface_fully_retired():
    # 2026-07-04 housekeeping: the doctrine-parity-stub pattern is retired.
    # The four core/policy tripwire yamls and their loaders (plus the
    # development-engine config, the deep_sleep alias, and the loaders whose
    # target files never existed) must not load or be re-addable. Policy
    # contracts live ONLY in policy/{stem}.policy.yaml via load_policy_contract.
    for fn_name in (
        "load_memory_policy",
        "load_identity_policy_yaml",
        "load_curiosity_policy",
        "load_development_policy",
        "load_development_config",
        "load_deep_sleep_config",
        "load_knowledge_domains_config",
        "load_identity_policy_json",
        "load_policy",
        "_load_policy",
    ):
        assert not hasattr(loader, fn_name), f"retired loader {fn_name} resurrected"
    for rel in (
        "core/policy/memory_policy.yaml",
        "core/policy/identity_policy.yaml",
        "core/policy/curiosity_policy.yaml",
        "core/policy/development_policy.yaml",
        "config/development.yaml",
        "config/pressure_scoring.yaml",
        "schema/pressure_scoring.schema.yaml",
        "schema/pressure_field.schema.yaml",
        "schema/execution_skill.schema.yaml",
    ):
        assert not (OMEGA_ROOT / rel).exists(), f"retired surface {rel} must stay deleted"


def test_deepsleep_config_is_mandatory_terminus():
    canonical = loader.load_deepsleep_config()
    deepsleep = canonical["deepsleep"]
    assert "enabled" not in deepsleep
    assert "failure_soft" not in deepsleep
    assert "sole_episode_closer" not in deepsleep
    assert deepsleep["module"] == "deepsleep_loop.loop.run_deepsleep_for_episode"


def test_cognition_cycle_has_no_configurable_stage_gates():
    import yaml

    for rel, root_key in (
        ("config/pressure.yaml", "pressure"),
        ("policy/symphony.policy.yaml", "pressure"),
    ):
        document = yaml.safe_load((OMEGA_ROOT / rel).read_text(encoding="utf-8"))
        section = document[root_key]
        assert not any(key.startswith("slice") and key.endswith("_front") for key in section)


def test_omega_config_contains_only_live_episode_sampling_surface():
    import yaml

    document = yaml.safe_load((OMEGA_ROOT / "config" / "omega.yaml").read_text(encoding="utf-8"))
    omega = document["omega"]
    assert set(omega) == {"reason"}
    assert set(omega["reason"]) == {
        "max_tokens",
        "temperature",
        "top_p",
        "stop",
        "timeout_s",
    }


def test_shared_state_policy_file_exists():
    # 2026-07-04 re-file: core/policy/ dissolved into root policy/ — the loaders
    # (guardian.state_persistence.POLICY_PATH, pressure_loop.policy._DEFAULT_POLICY_PATH)
    # read from policy/, and the old directory must stay gone.
    path = OMEGA_ROOT / "policy" / "shared_state.policy.yaml"
    assert path.is_file()
    assert (OMEGA_ROOT / "policy" / "symphony.policy.yaml").is_file()
    assert not (OMEGA_ROOT / "core" / "policy").exists()


def test_journal_schema_prior_episode_required_fields():
    import yaml

    path = OMEGA_ROOT / "schema" / "journal.schema.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    required = data["definitions"]["PriorEpisode"]["required"]
    assert "episode_id" in required
    assert "ignition_reason" in required
    assert "cascade_status" in required


def test_global_intent_policy_file_exists():
    path = OMEGA_ROOT / "policy" / "global_intent.policy.yaml"
    assert path.is_file()


@pytest.mark.parametrize(
    "loader_fn,root_key",
    [
        (loader.load_boot_config, "boot"),
        (loader.load_cognition_chain_config, "cognition_chain"),
        (loader.load_reflection_window_config, "reflection_window"),
        (loader.load_operator_chat_config, "operator_chat"),
        (loader.load_sensory_config, "sensory"),
        (loader.load_tts_config, "tts"),
        (loader.load_directives_config, "directives"),
        (loader.load_skill_execution_config, "skill_execution"),
        (loader.load_memory_config, "memory"),
    ],
)
def test_subsystem_configs_load(loader_fn, root_key):
    data = loader_fn()
    assert root_key in data
    assert isinstance(data[root_key], dict)


# THE EVERY-STEM INVARIANT (2026-07-04 reorg): every governed config stem has
# the complete triple — config/<stem>.yaml (tunables), schema/<stem>.schema.yaml
# (registered + validated at load), policy/<stem>.policy.yaml (loadable
# contract). Adding a config stem means adding all three, or this gate is RED.
ALL_STEMS = [
    "auto",
    "boot",
    "cognition_chain",
    "curiosity",
    "deepsleep",
    "directives",
    "dopamine",
    "global",
    "global_b",
    "global_intent",
    "guardian",
    "journal",
    "knowledge",
    "memory",
    "meta",
    "meta_b",
    "omega",
    "operator_chat",
    "pressure",
    "pressure_contributors",
    "reflection_window",
    "runtime",
    "sensory",
    "skill_execution",
    "strength_index",
    "tools",
    "tts",
    "web_sources",
]


@pytest.mark.parametrize("stem", ALL_STEMS)
def test_policy_and_schema_triples_exist(stem):
    from config.schema_validate import SCHEMA_ROOT_BY_STEM

    assert (OMEGA_ROOT / "policy" / f"{stem}.policy.yaml").is_file()
    assert (OMEGA_ROOT / "schema" / f"{stem}.schema.yaml").is_file()
    assert loader.config_path_for_stem(stem).is_file()
    assert stem in SCHEMA_ROOT_BY_STEM, f"{stem} not registered for validation"


@pytest.mark.parametrize("stem", ALL_STEMS)
def test_policy_contract_loads(stem):
    contract = loader.load_policy_contract(stem)
    assert isinstance(contract, dict) and contract, f"policy/{stem}.policy.yaml empty"


def test_no_config_stem_outside_the_invariant():
    # Sweep the config dir: every yaml on disk must be in ALL_STEMS (via its
    # stem or alias) — a new config can't dodge the triple.
    from config.loader import CONFIG_STEM_ALIASES

    alias_files = set(CONFIG_STEM_ALIASES.values())
    governed = {f"{s}.yaml" for s in ALL_STEMS} | alias_files
    on_disk = {p.name for p in (OMEGA_ROOT / "config").glob("*.yaml")}
    strays = on_disk - governed - {"governance.yaml"}  # governance.yaml is optional-by-design
    assert not strays, f"config yamls outside the every-stem invariant: {sorted(strays)}"


def test_autonomic_modulator_surface_fully_retired():
    # #21: the conv-cog affect blending surface (config + policy + schema triplet,
    # the PENDING_CONV_COG_AFFECT canonical key, load_autonomic_modulator_config)
    # was fully GOVERNED but had ZERO implementation — no writer (core.autonomic_
    # modulator never existed), no readers, no consumer. Per AGENTS.md, a phantom
    # governed surface must not load or be re-addable, so the whole surface is
    # deleted (symphony_neuromod RPE is the only live neuromod source).
    from config.schema_validate import SCHEMA_ROOT_BY_STEM
    from core.shared_state_keys import KEY_REGISTRY

    assert "pending_conv_cog_affect" not in KEY_REGISTRY
    assert "autonomic_modulator" not in SCHEMA_ROOT_BY_STEM
    assert not hasattr(loader, "load_autonomic_modulator_config")
    for rel in (
        "config/autonomic_modulator.yaml",
        "policy/autonomic_modulator.policy.yaml",
        "schema/autonomic_modulator.schema.yaml",
    ):
        assert not (OMEGA_ROOT / rel).exists(), f"phantom surface {rel} must be deleted"


def test_load_policy_contract_boot_aligns_with_config():
    contract = loader.load_policy_contract("boot")
    cfg = loader.load_boot_config()
    assert contract.get("enabled") is True
    assert contract.get("boot_order") == list(cfg["boot"]["components"])


def test_load_runtime_config_via_loader():
    data = loader.load_runtime_config()
    assert "runtime" in data
    assert isinstance(data["runtime"], dict)


def test_deepsleep_store_paths_follow_nested_config():
    from pressure_loop import policy

    stores = loader.load_deepsleep_config()["deepsleep"]["stores"]
    resolved = policy._deepsleep_stores_section()
    assert resolved == stores
    assert Path(resolved["journal_path"]).as_posix().endswith("deepsleep_loop/state/journal.jsonl")


def test_load_subsystem_bundle_merges_config_and_policy():
    bundle = loader.load_subsystem_bundle("boot")
    assert "config" in bundle and "policy" in bundle
    assert bundle["config"]["boot"]["components"]
    assert bundle["policy"]["boot_order"]


def test_reflection_window_size_from_config():
    from pressure_loop.reflection_window import WINDOW_SIZE

    cfg_size = loader.load_reflection_window_config()["reflection_window"]["window_size"]
    assert WINDOW_SIZE == cfg_size == 10


def test_sensory_presence_cfg_reads_yaml():
    from sensory.operator_presence import presence_cfg

    cfg = presence_cfg()
    sensory = loader.load_sensory_config()["sensory"]["presence"]
    assert cfg["acquire_threshold"] == sensory["acquire_threshold"]


def test_memory_contradiction_rules_from_bundle():
    from core.memory_config import contradiction_rules

    live = loader.load_subsystem_bundle("memory")["config"]["memory"]["contradiction"]
    rules = contradiction_rules()
    assert rules["resolution"] == live["resolution"]
    assert rules["loser_confidence_penalty"] == live["loser_confidence_penalty"]


def test_memory_bundle_enabled_flag():
    from core.memory_config import memory_enabled

    bundle = loader.load_subsystem_bundle("memory")
    assert memory_enabled() == bool(bundle["config"]["memory"]["enabled"])


def test_directives_bundle_exposes_image_and_document_intents():
    from core.directives_config import allowed_directive_intents, directive_spec

    bundle = loader.load_subsystem_bundle("directives")
    intents = {
        str(body.get("intent") or "").upper()
        for body in (bundle["config"].get("directives") or {}).values()
        if isinstance(body, dict)
    }
    assert allowed_directive_intents() == frozenset(intents)
    assert directive_spec("IMAGE_CREATE")["runtime_capability"] == "image_gen"

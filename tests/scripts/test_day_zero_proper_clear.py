"""Day-zero proper clear — assert cognition LOADERS empty, not just file presence."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.paths as core_paths  # noqa: E402
import pressure_loop.policy as pl_policy  # noqa: E402

import scripts.day_zero_reset as day_zero_reset  # noqa: E402
import memory.engine.validate_write as validate_write  # noqa: E402
import memory.engine.walker as walker  # noqa: E402
from memory.identity.seeds import CONSTITUTION_SEEDS  # noqa: E402

# The full founding constitution set day-zero re-seeds (single source of truth).
_CONSTITUTION_IDS = {s.record_id for s in CONSTITUTION_SEEDS}
from deepsleep_loop._deepsleep_state import ProjectionState  # noqa: E402
from deepsleep_loop.failure_store import load_failures  # noqa: E402
from deepsleep_loop.identity_store import load_identity  # noqa: E402
from deepsleep_loop.journal_store import read_journal  # noqa: E402
from deepsleep_loop.mastery_store import load_mastery  # noqa: E402
from deepsleep_loop.projection_store import read_projection  # noqa: E402
from deepsleep_loop.query_store import load_queries  # noqa: E402
from deepsleep_loop.semantic_store import load_semantic  # noqa: E402
from deepsleep_loop.trajectory_store import load_trajectory  # noqa: E402


def _patch_deepsleep_policy_paths(iso_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Route cognition loaders + reset wipes through the isolated tree."""
    state = iso_root / "deepsleep_loop" / "state"
    monkeypatch.setattr(
        pl_policy,
        "deepsleep_mastery_path",
        lambda policy=None: state / "mastery.json",
    )
    monkeypatch.setattr(
        pl_policy,
        "deepsleep_trajectory_path",
        lambda policy=None: state / "trajectory.json",
    )
    monkeypatch.setattr(
        pl_policy,
        "deepsleep_failure_path",
        lambda policy=None: state / "failure.json",
    )
    monkeypatch.setattr(
        pl_policy,
        "deepsleep_query_path",
        lambda policy=None: state / "open_queries.json",
    )
    monkeypatch.setattr(
        pl_policy,
        "deepsleep_identity_path",
        lambda policy=None: state / "identity.json",
    )
    monkeypatch.setattr(
        pl_policy,
        "deepsleep_semantic_path",
        lambda policy=None: state / "semantic.json",
    )
    monkeypatch.setattr(
        pl_policy,
        "deepsleep_projection_path",
        lambda policy=None: state / "projection.json",
    )
    monkeypatch.setattr(
        pl_policy,
        "deepsleep_journal_path",
        lambda policy=None: state / "journal.jsonl",
    )
    monkeypatch.setattr(
        pl_policy,
        "episode_journal_path",
        lambda policy=None: iso_root / "pressure_loop" / "state" / "episode_journal.jsonl",
    )


def _patch_isolated_reset_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point day_zero_reset, stores, and atomic_write at an isolated tmp tree."""
    iso_root = tmp_path / "omega"
    memory_root = iso_root / "memory"
    state_root = iso_root / "guardian" / "state"
    logs_root = iso_root / "guardian" / "logs"
    telemetry_root = iso_root / "guardian" / "telemetry"
    ledger_root = iso_root / "guardian" / "ledger"
    indexes_root = memory_root / "indexes"
    staging_root = state_root / "staging"
    deepsleep_state = iso_root / "deepsleep_loop" / "state"
    pressure_state = iso_root / "pressure_loop" / "state"

    for d in (
        memory_root,
        state_root,
        logs_root,
        telemetry_root,
        ledger_root,
        indexes_root,
        staging_root,
        deepsleep_state,
        pressure_state,
    ):
        d.mkdir(parents=True, exist_ok=True)

    strength_index = indexes_root / "strength_index.json"
    coord_index = indexes_root / "coord_index.json"

    monkeypatch.setattr(core_paths, "OMEGA_ROOT", iso_root)
    monkeypatch.setattr(core_paths, "GUARDIAN_STATE_ROOT", state_root)
    monkeypatch.setattr(core_paths, "GUARDIAN_LOGS_ROOT", logs_root)
    monkeypatch.setattr(core_paths, "LEDGER_ROOT", ledger_root)

    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", iso_root)
    monkeypatch.setattr(day_zero_reset, "GUARDIAN_STATE_ROOT", state_root)
    monkeypatch.setattr(day_zero_reset, "GUARDIAN_LOGS_ROOT", logs_root)
    monkeypatch.setattr(day_zero_reset, "LEDGER_ROOT", ledger_root)
    monkeypatch.setattr(day_zero_reset, "STAGING_ROOT", staging_root)
    monkeypatch.setattr(day_zero_reset, "GUARDIAN_STATE_DIR", state_root)
    monkeypatch.setattr(day_zero_reset, "GUARDIAN_LOGS_DIR", state_root)
    # Isolated trees must not abort on a live HUD from the operator machine.
    monkeypatch.setattr(day_zero_reset, "hud_listener_reachable", lambda: False)

    monkeypatch.setattr(validate_write, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(validate_write, "TMP_DIR", memory_root / "tmp_writes")
    monkeypatch.setattr(validate_write, "INDEX_PATH", coord_index)

    # load_identity() (and any canonical-lobe loader) now reads through the memory
    # walker's OWN copies of MEMORY_ROOT / INDEX_PATH + a process-global COORD_INDEX
    # cache — bound to the REAL tree at import. Without redirecting these too, the
    # loader reads the real on-disk identity store instead of the reset iso tree.
    # Drop the cache and null the mtime so the next load re-reads the iso index now,
    # and (after monkeypatch reverts the paths) self-heals to the real index on the
    # next call — no stale cross-test state.
    monkeypatch.setattr(walker, "MEMORY_ROOT", memory_root)
    monkeypatch.setattr(walker, "INDEX_PATH", coord_index)
    walker.COORD_INDEX.clear()
    walker._coord_index_mtime = None

    _patch_deepsleep_policy_paths(iso_root, monkeypatch)

    return iso_root


def _seed_contamination(iso_root: Path) -> None:
    """Pre-reset stale cognition, operator surfaces, ledger, and credentials."""
    deepsleep_state = iso_root / "deepsleep_loop" / "state"
    state_root = iso_root / "guardian" / "state"

    mastery = {
        "structured-learning": {
            "domain_id": "structured-learning",
            "domain_name": "structured learning and synthesis",
            "knowledge_mastery": 1.0,
            "application_mastery": 0.0,
            "confidence": 0.9,
            "evidence_count": 3,
            "subdomains": {},
            "target_mastery": 0.7,
            "status": "mastered",
        }
    }
    (deepsleep_state / "mastery.json").write_text(json.dumps(mastery, indent=2), encoding="utf-8")
    (deepsleep_state / "trajectory.json").write_text(
        json.dumps({"python::coding": {"target_domain": "python"}}),
        encoding="utf-8",
    )
    (deepsleep_state / "failure.json").write_text(
        json.dumps({"python": {"context": "python", "frequency": 2}}),
        encoding="utf-8",
    )
    (deepsleep_state / "open_queries.json").write_text(
        json.dumps({"q1": {"question_text": "what is X?"}}),
        encoding="utf-8",
    )
    (deepsleep_state / "identity.json").write_text(
        json.dumps({"curious": {"name": "curious", "strength": 0.5}}),
        encoding="utf-8",
    )
    (deepsleep_state / "semantic.json").write_text(
        json.dumps({"term": {"term": "term", "status": "developing"}}),
        encoding="utf-8",
    )
    (deepsleep_state / "projection.json").write_text(
        json.dumps(
            {
                "identity_projection": {"trait": 1},
                "mastery_projection": {"domain": 1},
                "trajectory_projection": {"path": 1},
            }
        ),
        encoding="utf-8",
    )
    (deepsleep_state / "journal.jsonl").write_text(
        '{"episode_id":"ep-1","summary":"stale"}\n', encoding="utf-8"
    )

    (state_root / "knowledge_mastery.json").write_text(
        json.dumps({"structured-learning": {"score": 1.0}}), encoding="utf-8"
    )
    (state_root / "save_state.json").write_text('{"hydrated": true}', encoding="utf-8")
    (state_root / "credential_ledger.enc").write_bytes(b"OPERATOR-SECRET-BLOB")
    (state_root / "face_anchors.json").write_text('{"operator": {"id": "stale"}}', encoding="utf-8")
    (state_root / "boot.manifest.json").write_text('{"boot": "stale"}', encoding="utf-8")

    for sub in ("hud", "documents", "images", "coding_snapshots", "face_images"):
        d = state_root / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / "stale.json").write_text('{"stale": true}', encoding="utf-8")

    (state_root / "reflection_lease.json").write_text(
        '{"window_id": "stale-lease"}', encoding="utf-8"
    )

    vision_images = iso_root / "memory" / "700_vision" / "images"
    vision_images.mkdir(parents=True, exist_ok=True)
    (vision_images / "thumb.jpg").write_bytes(b"JPEG-STALE")

    face_sidecar_dir = iso_root / "memory" / "700_vision" / "face_embeddings"
    face_sidecar_dir.mkdir(parents=True, exist_ok=True)
    (face_sidecar_dir / "face-abc.embedding.json").write_text('{"name": "stale"}', encoding="utf-8")
    (face_sidecar_dir / "capture.jpg").write_bytes(b"FACE-JPEG")

    episode_journal = iso_root / "pressure_loop" / "state" / "episode_journal.jsonl"
    episode_journal.parent.mkdir(parents=True, exist_ok=True)
    episode_journal.write_text('{"episode_id":"ep-stale"}\n', encoding="utf-8")

    # the knowledge_extractor artifact store — a top-level sibling dir OUTSIDE every
    # other wiped root (the real gap: 585KB of stale learned artifacts survived a reset).
    kx_state = iso_root / "knowledge_extractor" / "state"
    kx_state.mkdir(parents=True, exist_ok=True)
    (kx_state / "artifacts.jsonl").write_text(
        '{"domain":"structured-learning","artifact":"stale"}\n', encoding="utf-8"
    )

    ledger_root = iso_root / "guardian" / "ledger"
    ledger_root.mkdir(parents=True, exist_ok=True)
    (ledger_root / "key.bin").write_bytes(b"OLD-INSTANCE-KEY")
    (ledger_root / "chain.jsonl").write_text('{"stale": true}\n', encoding="utf-8")
    (ledger_root / ".activated").write_text("1", encoding="utf-8")

    agents_dir = iso_root / ".agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "bus.jsonl").write_text('{"from":"cursor","kind":"status"}\n', encoding="utf-8")

    stale_memory = iso_root / "memory" / "200_knowledge" / "general" / "stale.json"
    stale_memory.parent.mkdir(parents=True, exist_ok=True)
    stale_memory.write_text('{"id":"2.00.0001"}', encoding="utf-8")

    coord_index = iso_root / "memory" / "indexes" / "coord_index.json"
    coord_index.write_text(json.dumps({"stale": {"coord": [2, 0, 1]}}), encoding="utf-8")


def _memory_json_files(iso_root: Path) -> list[Path]:
    memory_root = iso_root / "memory"
    return [
        p
        for p in memory_root.rglob("*.json")
        if "indexes" not in p.parts
        and "source_ledger" not in p.parts
        and p.name not in ("strength_index.json",)
    ]


def _assert_projection_empty(state: ProjectionState) -> None:
    assert state.identity_projection == {}
    assert state.mastery_projection == {}
    assert state.trajectory_projection == {}


def test_day_zero_proper_clear_loaders_and_surfaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso_root = _patch_isolated_reset_roots(tmp_path, monkeypatch)
    _seed_contamination(iso_root)

    day_zero_reset.main()

    mastery = load_mastery()
    assert mastery == {}, f"load_mastery() still returns stale record: {mastery!r}"
    assert load_trajectory() == {}
    assert load_failures() == {}
    assert load_queries() == {}
    assert load_identity() == {}
    assert load_semantic() == {}
    _assert_projection_empty(read_projection())
    assert read_journal() == []

    state_root = iso_root / "guardian" / "state"
    assert state_root.is_dir()
    assert list(state_root.iterdir()) == [], "guardian/state must be empty after factory reset"
    assert not (iso_root / "memory" / "700_vision" / "images" / "thumb.jpg").exists()
    face_emb = iso_root / "memory" / "700_vision" / "face_embeddings"
    assert not (face_emb / "capture.jpg").exists()
    assert not (face_emb / "face-abc.embedding.json").exists()
    pressure_state = iso_root / "pressure_loop" / "state"
    assert not any(pressure_state.rglob("*")) if pressure_state.is_dir() else True

    # the knowledge_extractor artifact store (the gap) is cleared — no stale learned
    # artifacts survive into the fresh instance.
    kx_state = iso_root / "knowledge_extractor" / "state"
    assert not (kx_state / "artifacts.jsonl").exists(), (
        "knowledge_extractor artifacts must be wiped"
    )

    ledger_root = iso_root / "guardian" / "ledger"
    assert not (ledger_root / "key.bin").exists()
    assert not (ledger_root / "chain.jsonl").exists()
    assert not (ledger_root / ".activated").exists()

    # Operator directive 2026-06-12: day_zero does NOT touch .agents/ — the
    # bus and its cursors must live or die together (wiping the bus while
    # cursors survive desyncs readers past the new bus end -> silent message
    # loss). The seeded bus survives the wipe with content intact.
    bus_path = iso_root / ".agents" / "bus.jsonl"
    assert bus_path.exists(), ".agents/bus.jsonl must SURVIVE a factory reset"
    assert bus_path.read_text(encoding="utf-8") == '{"from":"cursor","kind":"status"}\n'

    memory_files = _memory_json_files(iso_root)
    assert len(memory_files) == len(CONSTITUTION_SEEDS)
    seen_ids = set()
    for path in memory_files:
        meta = json.loads(path.read_text(encoding="utf-8"))
        assert meta["coord"][0] == 0
        assert meta["id"] in _CONSTITUTION_IDS
        seen_ids.add(meta["id"])
    assert seen_ids == _CONSTITUTION_IDS  # every founding record re-seeded, none dropped

    coord_index = json.loads(
        (iso_root / "memory" / "indexes" / "coord_index.json").read_text(encoding="utf-8")
    )
    assert len(coord_index) == len(CONSTITUTION_SEEDS)
    assert all(entry["coord"][0] == 0 for entry in coord_index.values())

    label_map = iso_root / "memory" / "indexes" / "label_map.json"
    if label_map.exists():
        assert "label_map" in label_map.name

    # Lobes 2 (knowledge) and 8 (coding) start blank after reset — no static
    # subdomain dirs. Every other lobe keeps canonical subdirs.
    from memory.dewey_map import SUBDOMAIN_MAP

    lobe_2_dir = iso_root / "memory" / "200_knowledge"
    assert lobe_2_dir.is_dir()
    for (x, y), name in SUBDOMAIN_MAP.items():
        if x != 2:
            continue
        sub_path = lobe_2_dir / name
        assert not sub_path.exists(), f"Lobe 2 should NOT have canonical subdomain dir {name!r}"

    lobe_8_dir = iso_root / "memory" / "800_coding"
    assert lobe_8_dir.is_dir()
    for (x, y), name in SUBDOMAIN_MAP.items():
        if x != 8:
            continue
        sub_path = lobe_8_dir / name
        assert not sub_path.exists(), f"Lobe 8 should NOT have canonical subdomain dir {name!r}"


def test_day_zero_lobes_blank_subdomains_created_on_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Operator doctrine (2026-06-29): ALL lobes start blank after a factory reset —
    not just the dynamic-Y lobes 2 & 8. "i want the lobes to survive but not the
    folders beneath all of them ... the rest make on memory write already." A
    previously-static lobe (3/reasoning) is now ALSO empty after reset, and a write
    to it lazily creates the subdomain dir — same mechanism that already backs lobes
    2 & 8. Verified via dirs + a live write, not function return values.
    """
    iso_root = _patch_isolated_reset_roots(tmp_path, monkeypatch)

    # Seed some contamination to ensure wipe works
    _seed_contamination(iso_root)

    # Run day-zero reset
    day_zero_reset.main()

    # 1+2+3. Lobes 2 (knowledge), 8 (coding) AND 3 (reasoning, previously static)
    # all have ZERO subdomain dirs after reset.
    for lobe_name in ("200_knowledge", "800_coding", "300_reasoning"):
        lobe_dir = iso_root / "memory" / lobe_name
        assert lobe_dir.is_dir()
        subdirs = [d for d in lobe_dir.iterdir() if d.is_dir()]
        assert subdirs == [], (
            f"{lobe_name} should have 0 subdomain dirs after reset, got: {[d.name for d in subdirs]}"
        )

    # 4. Verify that the dynamic Y registry was wiped
    from memory.subdomain_allocator import _index_path

    dyn_index_path = _index_path()
    assert not dyn_index_path.exists(), "dynamic_y_index.json should be wiped by day-zero reset"

    # 5. A write lazily creates the subdomain dir on demand — for BOTH a dynamic-Y
    # lobe (2/knowledge) and a previously-static lobe (3/reasoning). This is why
    # day-zero needs to pre-scaffold nothing: "the rest make on memory write."
    from memory.core import build_memory_dict

    mem_knowledge = build_memory_dict(
        text="Test memory for flag-off static path verification",
        coord=[2, 0, 0],
        source_tag="lobe:knowledge",
        metadata={"type": "concept", "domain": "astronomy", "confidence": 0.5},
    )
    validate_write.atomic_write(mem_knowledge, update_index=False)
    assert (iso_root / "memory" / "200_knowledge" / "astronomy").is_dir(), (
        "write to lobe 2 should lazily create its subdomain dir"
    )

    mem_reasoning = build_memory_dict(
        text="A goal: confirm subdomains are created on write, not pre-scaffolded.",
        coord=[3, 0, 0],  # (3, 0) -> goals
        source_tag="lobe:reasoning",
        metadata={"type": "concept", "confidence": 0.5},
    )
    validate_write.atomic_write(mem_reasoning, update_index=False)
    assert (iso_root / "memory" / "300_reasoning" / "goals").is_dir(), (
        "write to the previously-static lobe 3 should lazily create its subdomain dir"
    )


def test_day_zero_clears_redirected_episode_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config-redirected episode journal OUTSIDE pressure_loop/state must still be wiped.

    The default journal lives at pressure_loop/state/episode_journal.jsonl and is
    caught by the directory sweep (wipe_pressure_loop_state). But
    episode_journal_path() is config-redirectable (journal config YAML) — when an
    operator points it elsewhere, the fixed-dir sweep MISSES it. Only the dedicated
    wipe_episode_journal(), which resolves the policy accessor, clears it. This guards
    that main() actually invokes it so a redirected episodic ledger never survives a
    factory reset.
    """
    iso_root = _patch_isolated_reset_roots(tmp_path, monkeypatch)

    # Redirect the journal to a top-level sibling that NO other wipe sweeps.
    redirected = iso_root / "external_journals" / "episode_journal.jsonl"
    redirected.parent.mkdir(parents=True, exist_ok=True)
    redirected.write_text('{"episode_id":"ep-redirected"}\n', encoding="utf-8")
    monkeypatch.setattr(pl_policy, "episode_journal_path", lambda policy=None: redirected)

    day_zero_reset.main()

    assert not redirected.exists(), (
        "a config-redirected episode journal must be wiped by day-zero "
        "(main() must call wipe_episode_journal, which resolves the policy accessor)"
    )


def test_day_zero_retires_tools_lobe_does_not_recreate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tools lobe (5 / 500_tools) was removed by the operator — tools live in the
    registry (config/tools.yaml), not the memory system. Day-zero must NOT resurrect it:
    normalize iterates LOBE_MAP (which may still list lobe 5), so a stale 500_tools dir
    must be dropped and never rebuilt, while every other lobe still gets its scaffold.
    """
    iso_root = _patch_isolated_reset_roots(tmp_path, monkeypatch)

    # Stale tools lobe left over from a prior layout — must not survive nor be rebuilt.
    stale_tool = iso_root / "memory" / "500_tools" / "cognitive" / "web_fetch.json"
    stale_tool.parent.mkdir(parents=True, exist_ok=True)
    stale_tool.write_text('{"id":"5.00.0001"}', encoding="utf-8")

    day_zero_reset.main()

    assert not (iso_root / "memory" / "500_tools").exists(), (
        "day-zero must not recreate the retired tools lobe (lobe 5)"
    )
    # Control: a non-retired lobe still gets its canonical scaffold.
    assert (iso_root / "memory" / "300_reasoning").is_dir(), (
        "non-retired lobes must still be normalized"
    )


def test_day_zero_creates_exact_canonical_lobe_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Day-zero must reproduce the EXACT top-level memory/ lobe layout the live system
    runs (operator 2026-06-29: "line up the day zero script to create this exact
    layout"). That layout = every lobe in LOBE_MAP EXCEPT the retired tools lobe
    (5 / 500_tools): the 11 dirs below — no more, no fewer. A stale retired lobe
    (500_tools) and pre-restructure legacy lobes (100_personal, 400_context) must
    ALL be gone; non-lobe code/index dirs are left untouched.
    """
    iso_root = _patch_isolated_reset_roots(tmp_path, monkeypatch)

    # Seed a retired lobe (5) AND two legacy pre-restructure lobes — none may survive.
    for stale in ("500_tools/cognitive", "100_personal/who", "400_context/what"):
        d = iso_root / "memory" / stale
        d.mkdir(parents=True, exist_ok=True)
        (d / "stale.json").write_text('{"x":1}', encoding="utf-8")
    # A non-lobe code dir (no digit prefix) must be LEFT ALONE — never dropped as data.
    code_dir = iso_root / "memory" / "enrichment"
    code_dir.mkdir(parents=True, exist_ok=True)
    (code_dir / "__init__.py").write_text("", encoding="utf-8")

    day_zero_reset.main()

    EXPECTED_LOBES = {
        "000_identity",
        "100_personas",
        "200_knowledge",
        "300_reasoning",
        "400_situational",
        "600_journal",
        "700_vision",
        "800_coding",
        "900_temporal",
        "1000_workflows",
        "1100_dev",
    }

    memory_root = iso_root / "memory"
    # Lobe dirs are digit-prefixed (000_.. 1100_); code/index dirs are not.
    present_lobes = {d.name for d in memory_root.iterdir() if d.is_dir() and d.name[:1].isdigit()}
    assert present_lobes == EXPECTED_LOBES, (
        "day-zero must create EXACTLY the live lobe layout.\n"
        f"  missing:    {sorted(EXPECTED_LOBES - present_lobes)}\n"
        f"  unexpected: {sorted(present_lobes - EXPECTED_LOBES)}"
    )

    # The pinned literal set must stay in sync with LOBE_MAP minus retired lobes,
    # so a future LOBE_MAP edit can't silently drift the contract.
    from memory.dewey_map import LOBE_MAP

    derived = set(LOBE_MAP.values()) - {
        LOBE_MAP[x] for x in day_zero_reset.RETIRED_LOBES if x in LOBE_MAP
    }
    assert derived == EXPECTED_LOBES, (
        "LOBE_MAP minus RETIRED_LOBES drifted from the pinned layout: "
        f"{sorted(derived ^ EXPECTED_LOBES)}"
    )

    # Non-lobe code dir untouched (it is not data; dropping it would risk a module).
    assert code_dir.is_dir(), "non-lobe code dir must NOT be dropped by normalize"

    # Operator doctrine (2026-06-29): "i want the lobes to survive but not the folders
    # beneath all of them ... the rest make on memory write already." Day-zero creates
    # each lobe EMPTY — every subdomain folder is created lazily on first write
    # (final_path_for_meta mkdir's the dest). The SOLE exception is 000_identity, where
    # the full constitution seed set lands in constitution/ (seed step runs after normalize).
    for lobe_name in EXPECTED_LOBES:
        lobe_dir = memory_root / lobe_name
        children = sorted(c.name for c in lobe_dir.iterdir())
        if lobe_name == "000_identity":
            assert children == ["constitution"], (
                "000_identity holds only constitution/ (the founding seeds) after day-zero, "
                f"got: {children}"
            )
            continue
        assert children == [], (
            f"lobe {lobe_name} must be EMPTY after day-zero (subdomains are created on "
            f"first memory write, not pre-scaffolded), but has: {children}"
        )


def test_day_zero_clears_provider_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A portable factory reset never carries credentials to the next operator."""
    iso_root = _patch_isolated_reset_roots(tmp_path, monkeypatch)
    state_root = iso_root / "guardian" / "state"
    openrouter = state_root / "openrouter.json"
    payload = '{"enabled": true, "api_key": "sk-or-v1-OPERATOR-KEY"}'
    openrouter.write_text(payload, encoding="utf-8")
    brain_cloud = state_root / "brain_cloud.json"
    brain_cloud.write_text('{"api_key":"sk-brain-OPERATOR-KEY"}', encoding="utf-8")
    # Sibling guardian/state contamination that MUST still be wiped.
    (state_root / "save_state.json").write_text('{"hydrated": true}', encoding="utf-8")

    day_zero_reset.main()

    assert not openrouter.exists(), "OpenRouter config must be removed by factory reset"
    assert not brain_cloud.exists(), "cloud brain config must be removed by factory reset"
    assert not (state_root / "save_state.json").exists(), "other guardian/state still wiped"
    assert list(state_root.iterdir()) == []


def test_day_zero_is_a_self_contained_factory_reset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The portable reset removes every operator attachment inside Omega only."""
    iso_root = _patch_isolated_reset_roots(tmp_path, monkeypatch)
    _seed_contamination(iso_root)

    state_root = iso_root / "guardian" / "state"
    (state_root / "openrouter.json").write_text('{"api_key":"stale"}', encoding="utf-8")
    (state_root / "brain_cloud.json").write_text('{"api_key":"stale"}', encoding="utf-8")
    (iso_root / "guardian" / "logs" / "stale.log").write_text("stale", encoding="utf-8")
    quarantine = iso_root / "memory" / "quarantine" / "stale" / "record.json"
    quarantine.parent.mkdir(parents=True, exist_ok=True)
    quarantine.write_text('{"stale":true}', encoding="utf-8")

    external = tmp_path / "operator-workspace"
    external.mkdir()
    sentinel = external / "must-survive.txt"
    sentinel.write_text("outside Omega", encoding="utf-8")
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(external))
    monkeypatch.setenv("OMEGA_OPERATOR_COMPLETED", str(tmp_path / "completed"))

    day_zero_reset.main()

    assert not any(state_root.iterdir()), "portable factory reset must remove all saved state"
    assert not list((iso_root / "guardian" / "logs").glob("*")), "reset logs must not survive"
    assert not (iso_root / "memory" / "quarantine").exists()
    assert sentinel.read_text(encoding="utf-8") == "outside Omega"


def test_wipe_legacy_console_fossils_clears_fossils(tmp_path, monkeypatch):
    iso = tmp_path / "brain"
    iso.mkdir()
    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", iso)
    (iso / "nul").write_text("", encoding="utf-8")

    removed = day_zero_reset.wipe_legacy_console_fossils()

    # ``nul`` is a reserved device on Windows, so write_text does not create a
    # filesystem entry there.
    assert removed == (0 if os.name == "nt" else 1)
    if os.name == "nt":
        assert not (iso / "nul").is_file()
    else:
        assert not (iso / "nul").exists()

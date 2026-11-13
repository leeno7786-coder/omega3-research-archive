"""An approved offline replacement cannot silently change live authority."""
from __future__ import annotations

import json
import shutil
from dataclasses import replace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp_platform.canonical import canonical_bytes
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from tests.mcp_platform.test_pre_auth_recovery import legacy_fixture


def saved_installation(tmp_path, plugin_id="github.official", *, key=None):
    package, version, app, current, ring = legacy_fixture(tmp_path / "fixture", plugin_id, key=key)
    state = tmp_path / "state"
    service = PluginLifecycleService(state_root=state)
    plugin_id = current.manifest.plugin_id
    service.install(plugin_id, confirmation=service.review(plugin_id).sha256)
    record = service.plugin_registry.get(plugin_id)
    service.close()
    plugins = state / "plugins"
    old_record = replace(record, selected_version=version.plugin_version,
                         versions=(version,), enabled=True)
    (plugins / "registry.json").write_bytes(canonical_bytes({
        "schema_version": 2, "plugins": {plugin_id: old_record.to_dict()},
    }))
    shutil.rmtree(plugins / "packages" / plugin_id)
    shutil.copytree(package, plugins / "packages" / plugin_id / version.plugin_version)
    (state / "mcp/apps_registry.json").write_bytes(canonical_bytes({
        "schema_version": 1, "apps": {app["app_id"]: app},
    }))
    protected = {
        "mcp/authorization/vault.bin": b"encrypted-vault-fixture\x00\xff",
        "mcp/plugin_grants.json": canonical_bytes({
            "schema_version": 6, "active": {}, "staged": {}, "generations": {},
            "disconnected": {}, "retired_legacy": {"fixture": {"history": True}},
        }),
        "mcp/plugin_authority.json": canonical_bytes({
            "schema_version": 4, "connections": {}, "authorities": {}, "executions": {},
        }),
    }
    for name, payload in protected.items():
        target = state / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
    return state, ring, {plugin_id: (version.plugin_version, current.manifest.plugin_version)}, protected


def test_recovery_publishes_real_disabled_installs_and_archives_old_bytes(tmp_path):
    from mcp_platform.offline_recovery import prepare_recovery, apply_recovery

    state, ring, upgrades, protected = saved_installation(tmp_path)
    archive = tmp_path / "archive"
    service = PluginLifecycleService(state_root=state)
    try:
        digest = prepare_recovery(service, archive, upgrades, key_ring=ring)
        # Preparation never edits the live declarative records.
        assert service.plugin_registry.get("github.official").selected_version == "1.0.0"
        apply_recovery(service, archive, confirmation=digest, key_ring=ring)
        apply_recovery(service, archive, confirmation=digest, key_ring=ring)
        record = service.plugin_registry.get("github.official")
        assert record.selected_version == upgrades["github.official"][1]
        assert record.enabled is False
        assert len(record.versions) == 1
        assert service.plugin_registry.reverify_installed("github.official")
        assert service.app_registry.snapshot().records[0].enabled is False
        assert (archive / "before/plugins/packages/github.official/1.0.0/plugin.json").is_file()
        assert not (state / "plugins/packages/github.official/1.0.0").exists()
        for name, payload in protected.items():
            assert (state / name).read_bytes() == payload
    finally:
        service.close()


@pytest.mark.parametrize("damage", ["approval", "metadata", "credential", "archive", "old_asset"])
def test_recovery_rejects_changed_approval_or_saved_state(tmp_path, damage):
    from mcp_platform.offline_recovery import prepare_recovery, apply_recovery

    state, ring, upgrades, _ = saved_installation(tmp_path)
    archive = tmp_path / "archive"
    service = PluginLifecycleService(state_root=state)
    try:
        digest = prepare_recovery(service, archive, upgrades, key_ring=ring)
        if damage == "approval":
            digest = "0" * 64
        elif damage == "metadata":
            service.plugin_registry.set_enabled(
                "github.official", False, expected_revision=service.plugin_registry.snapshot().revision,
            )
        elif damage == "credential":
            (state / "mcp/authorization/vault.bin").write_bytes(b"different encrypted bytes")
        elif damage == "old_asset":
            (state / "plugins/packages/github.official/1.0.0/tools/authority.json").write_bytes(b"{}")
        else:
            (archive / "after/mcp/apps_registry.json").write_bytes(b"{}")
        with pytest.raises(ValueError, match="plugin_legacy_recovery_invalid"):
            apply_recovery(service, archive, confirmation=digest, key_ring=ring)
        assert service.plugin_registry.get("github.official").selected_version == "1.0.0"
    finally:
        service.close()


def test_active_authority_cannot_be_recovered_as_dormant_history(tmp_path):
    from mcp_platform.offline_recovery import prepare_recovery

    state, ring, upgrades, _ = saved_installation(tmp_path)
    grants = state / "mcp/plugin_grants.json"
    raw = json.loads(grants.read_bytes())
    raw["active"] = {"active-account": {}}
    grants.write_bytes(canonical_bytes(raw))
    service = PluginLifecycleService(state_root=state)
    try:
        with pytest.raises(ValueError, match="plugin_legacy_recovery_invalid"):
            prepare_recovery(service, tmp_path / "archive", upgrades, key_ring=ring)
    finally:
        service.close()


@pytest.mark.parametrize("damage", ["execution", "missing_executions", "wrong_schema"])
def test_recovery_rejects_non_dormant_or_malformed_authority_document(tmp_path, damage):
    from mcp_platform.offline_recovery import prepare_recovery

    state, ring, upgrades, _ = saved_installation(tmp_path)
    authority = state / "mcp/plugin_authority.json"
    raw = json.loads(authority.read_bytes())
    if damage == "execution":
        raw["executions"] = {"pending-execution": {}}
    elif damage == "missing_executions":
        raw.pop("executions")
    else:
        raw["schema_version"] = 3
    damaged = canonical_bytes(raw)
    authority.write_bytes(damaged)
    archive = tmp_path / "archive"
    service = PluginLifecycleService(state_root=state)
    try:
        with pytest.raises(ValueError, match="plugin_legacy_recovery_invalid"):
            prepare_recovery(service, archive, upgrades, key_ring=ring)
        assert authority.read_bytes() == damaged
        assert not archive.exists()
    finally:
        service.close()


@pytest.mark.parametrize("after_document", ["mcp/apps_registry.json", "plugins/registry.json"])
def test_interruption_blocks_runtime_and_explicit_resume_preserves_authority(tmp_path, monkeypatch, after_document):
    import mcp_platform.offline_recovery as recovery
    from mcp_platform.plugin_lifecycle import compose_plugin_lifecycle_service

    state, ring, upgrades, protected = saved_installation(tmp_path)
    archive = tmp_path / "archive"
    service = PluginLifecycleService(state_root=state)
    try:
        digest = recovery.prepare_recovery(service, archive, upgrades, key_ring=ring)
        publish = recovery._publish_document

        def interrupted(root, name, payload, previous):
            publish(root, name, payload, previous)
            if name == after_document:
                raise RuntimeError("simulated interruption")

        monkeypatch.setattr(recovery, "_publish_document", interrupted)
        with pytest.raises(RuntimeError, match="simulated interruption"):
            recovery.apply_recovery(service, archive, confirmation=digest, key_ring=ring)
        service.close()
        with pytest.raises(ValueError, match="^plugin_legacy_recovery_required$"):
            compose_plugin_lifecycle_service(state_root=state, oauth_key_provider=lambda: b"x" * 32)
        service = PluginLifecycleService(state_root=state)
        monkeypatch.setattr(recovery, "_publish_document", publish)
        recovery.apply_recovery(service, archive, confirmation=digest, key_ring=ring)
        recovery.require_recovery_complete(state / "plugins")
        assert service.plugin_registry.get("github.official").enabled is False
        for name, payload in protected.items():
            assert (state / name).read_bytes() == payload
    finally:
        service.close()


def test_three_provider_batch_uses_fresh_installer_reviews_and_receipts(tmp_path):
    from mcp_platform.offline_recovery import prepare_recovery, apply_recovery

    key = Ed25519PrivateKey.generate()
    state, ring, upgrades, protected = saved_installation(tmp_path / "github", key=key)
    for plugin_id in ("google.workspace.drive", "google.workspace.calendar"):
        other, _, added, _ = saved_installation(tmp_path / plugin_id, plugin_id, key=key)
        upgrades.update(added)
        for name, field in (("plugins/registry.json", "plugins"),
                            ("mcp/apps_registry.json", "apps"),
                            ("mcp/app_provision_journal.json", "transactions")):
            raw = json.loads((state / name).read_bytes())
            raw[field].update(json.loads((other / name).read_bytes())[field])
            (state / name).write_bytes(canonical_bytes(raw))
        shutil.copytree(other / "plugins/packages" / plugin_id, state / "plugins/packages" / plugin_id)
    service = PluginLifecycleService(state_root=state)
    try:
        archive = tmp_path / "archive"
        digest = prepare_recovery(service, archive, upgrades, key_ring=ring)
        apply_recovery(service, archive, confirmation=digest, key_ring=ring)
        records = service.plugin_registry.snapshot().records
        assert {record.plugin_id for record in records} == set(upgrades)
        assert len({record.app_binding.dependency_receipt_id for record in records}) == 3
        assert all(not record.enabled and len(record.versions) == 1 for record in records)
        assert len(service.app_registry.snapshot().records) == 3
        for name, payload in protected.items():
            assert (state / name).read_bytes() == payload
    finally:
        service.close()

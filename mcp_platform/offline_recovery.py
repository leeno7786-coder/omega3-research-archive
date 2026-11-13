"""Explicit offline recovery of signed, dormant pre-authentication installs.

Never used as a runtime compatibility path. The ordinary installer builds a
new disabled installation in isolation; only its exact reviewed public state
is published. Credentials and authorization history are hashed, never copied
or interpreted as permission. A durable maintenance marker gates runtime
composition across the multi-document publication.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.state_store import McpStateStore
from plugin_platform.filesystem import PinnedDirectory, PinnedPackageTree
from plugin_platform.package_verifier import verify_plugin_package
from mcp_platform.pre_auth_recovery import _json, read_legacy_package, verify_legacy_successor
from plugin_platform.records import InstalledPluginRecord
from plugin_platform.trust import load_embedded_key_ring

if TYPE_CHECKING:
    from mcp_platform.plugin_lifecycle import PluginLifecycleService
    from plugin_platform.trust import TrustedPublisherKeyRing

INVALID = "plugin_legacy_recovery_invalid"
MARKER = "pre-auth-recovery.json"
DOCUMENTS = ("mcp/apps_registry.json", "mcp/app_provision_journal.json", "plugins/registry.json")
_LIMIT = 64 * 1024 * 1024


def _read(root: Path, name: str) -> bytes | None:
    with PinnedDirectory.open(root) as pinned:
        return pinned.read_bytes(name, limit=_LIMIT) if pinned.exists(name) else None


def _put(root: Path, name: str, payload: bytes) -> None:
    with PinnedDirectory.open(root) as pinned:
        pinned.write_bytes(name, payload)


def _hash(payload: bytes | None) -> str | None:
    return None if payload is None else sha256_hex(payload)


def _store(root: Path, name: str) -> McpStateStore:
    path = root / name
    return McpStateStore(state_root=path.parent, document_path=path, invalid_path_reason=INVALID)


def _protected(root: Path) -> dict[str, str | None]:
    names = {f"mcp/{name}.json" for name in (
        "plugin_grants", "plugin_authority", "plugin_permissions",
    )}
    authorization = root / "mcp/authorization"
    if authorization.exists():
        with PinnedDirectory.open(authorization):
            for path in authorization.rglob("*"):
                if path.is_file() and not path.name.endswith((".lock", ".lease")):
                    names.add(path.relative_to(root).as_posix())
    return {name: _hash(_read(root, name)) for name in sorted(names)}


def _require_dormant_authority(root: Path) -> None:
    grants = _read(root, "mcp/plugin_grants.json")
    if grants is not None:
        raw = _json(grants)
        if any(raw.get(field) != {} for field in ("active", "staged")):
            raise ValueError(INVALID)
    authority = _read(root, "mcp/plugin_authority.json")
    if authority is not None:
        raw = _json(authority)
        fields = {"schema_version", "connections", "authorities", "executions"}
        if (
            set(raw) != fields
            or type(raw["schema_version"]) is not int
            or raw["schema_version"] != 4
            or any(type(raw[field]) is not dict or raw[field] for field in fields - {"schema_version"})
        ):
            raise ValueError(INVALID)


def require_recovery_complete(plugin_root: Path) -> None:
    """Configured runtime must never observe a partly published recovery."""
    if not plugin_root.exists():
        return
    try:
        payload = _read(plugin_root, MARKER)
        if payload is None:
            return
        raw = _json(payload)
        if set(raw) != {"schema_version", "plan_sha256", "complete"} or (
            raw["schema_version"] != 1 or raw["complete"] is not True
        ):
            raise ValueError(INVALID)
    except Exception as exc:
        raise ValueError("plugin_legacy_recovery_required") from exc


def _record_document(payload: bytes) -> dict[str, InstalledPluginRecord]:
    raw = _json(payload)
    if set(raw) != {"schema_version", "plugins"} or raw["schema_version"] != 2:
        raise ValueError(INVALID)
    return {key: InstalledPluginRecord.from_dict(value, registry_key=key)
            for key, value in raw["plugins"].items()}


def _archive_public(archive: Path, prefix: str, files: dict) -> dict:
    hashes = {}
    for name, payload in files.items():
        hashes[name] = _hash(payload)
        if payload is not None:
            _put(archive, f"{prefix}/{name}", payload)
    return hashes


def prepare_recovery(
    service: PluginLifecycleService, archive: Path, upgrades: dict[str, tuple[str, str]], *,
    key_ring: TrustedPublisherKeyRing | None = None,
) -> str:
    """Prepare a no-authority-change batch; return its exact confirmation digest."""
    from mcp_platform.plugin_lifecycle import PluginLifecycleService

    root = service.plugin_registry.state_root.parent
    archive = Path(archive).absolute()
    ring = key_ring or load_embedded_key_ring()
    with service._authority_mutation_lock:
        if service.plugin_grants is not None or not upgrades or archive.exists() or (
            archive.is_relative_to(root) or root.is_relative_to(archive)
        ):
            raise ValueError(INVALID)
        _require_dormant_authority(root)
        protected = _protected(root)
        before = {name: _read(root, name) for name in DOCUMENTS}
        records = _record_document(before["plugins/registry.json"])
        apps = _json(before["mcp/apps_registry.json"])
        if set(records) != set(upgrades) or set(apps) != {"schema_version", "apps"} or (
            apps["schema_version"] != 1
        ):
            raise ValueError(INVALID)
        covered = set()
        for plugin_id, record in records.items():
            old_version, new_version = upgrades[plugin_id]
            _, _, successor = service._verified(plugin_id)
            if record.selected_version != old_version or (
                successor.manifest.plugin_version != new_version or record.blocker is not None
            ):
                raise ValueError(INVALID)
            app_id = successor.manifest.required_apps[0].app_id
            if app_id in covered:
                raise ValueError(INVALID)
            covered.add(app_id)
            for version in record.versions:
                path = service.plugin_registry.packages_root / plugin_id / version.plugin_version
                old, payloads = read_legacy_package(path, version, ring)
                if old["plugin_id"] != plugin_id:
                    raise ValueError(INVALID)
                if version.state == "selected":
                    verify_legacy_successor(path, version, apps["apps"][app_id], successor, ring)
                prefix = f"plugins/packages/{plugin_id}/{version.plugin_version}"
                before.update({f"{prefix}/{name}": payload for name, payload in payloads.items()})
        if covered != set(apps["apps"]):
            raise ValueError(INVALID)
        # Only public declarative state enters this archive. Never copy a vault.
        archive.mkdir(parents=True, exist_ok=False)
        before_hashes = _archive_public(archive, "before", before)
        stage = archive / "stage"
        staged = PluginLifecycleService(state_root=stage)
        after = {}
        try:
            for plugin_id in sorted(upgrades):
                result = staged.install(plugin_id, confirmation=staged.review(plugin_id).sha256)
                if result.record.enabled or result.record.selected_version != upgrades[plugin_id][1]:
                    raise ValueError(INVALID)
                verified = staged.plugin_registry.reverify_installed(plugin_id)
                prefix = f"plugins/packages/{plugin_id}/{result.record.selected_version}"
                with PinnedPackageTree.open(verified.package_root) as tree:
                    for file in tree.files:
                        name = file.relative_to(tree.root).as_posix()
                        after[f"{prefix}/{name}"] = tree.read_bytes(name, limit=_LIMIT)
            if any(record.enabled for record in staged.app_registry.snapshot().records):
                raise ValueError(INVALID)
            after.update({name: _read(stage, name) for name in DOCUMENTS})
        finally:
            staged.close()
        if _protected(root) != protected or any(_read(root, name) != before[name] for name in DOCUMENTS):
            raise ValueError(INVALID)
        plan = {"schema_version": 1, "source_root": str(root),
                "upgrades": {key: list(value) for key, value in upgrades.items()},
                "before": before_hashes, "after": _archive_public(archive, "after", after),
                "protected": protected}
        payload = canonical_bytes(plan)
        _put(archive, "plan.json", payload)
        return sha256_hex(payload)


def _load_plan(archive: Path, confirmation: str) -> tuple[dict, dict, dict]:
    payload = _read(archive, "plan.json")
    if payload is None or sha256_hex(payload) != confirmation:
        raise ValueError(INVALID)
    plan = _json(payload)
    if set(plan) != {"schema_version", "source_root", "upgrades", "before", "after", "protected"} or (
        plan["schema_version"] != 1
    ):
        raise ValueError(INVALID)
    decoded = []
    for prefix in ("before", "after"):
        files = {name: _read(archive, f"{prefix}/{name}") for name in plan[prefix]}
        if {name: _hash(payload) for name, payload in files.items()} != plan[prefix]:
            raise ValueError(INVALID)
        decoded.append(files)
    return plan, *decoded


def _publish_document(root: Path, name: str, payload: bytes, previous: bytes | None) -> None:
    store = _store(root, name)
    with store.locked():
        observed = store.read_bytes()
        if observed == payload:
            return
        if observed != previous:
            raise ValueError(INVALID)
        store.write_bytes(payload)


def _publish_asset(root: Path, name: str, payload: bytes) -> None:
    # The lifecycle owner lease excludes other plugin writers. Do not create
    # sidecar locks inside signed package trees (extra files invalidate them).
    relative = Path(name)
    with PinnedDirectory.open(root) as pinned:
        with pinned.ensure_directory(relative.parent) as parent:
            if parent.exists(relative.name):
                if parent.read_bytes(relative.name, limit=_LIMIT) != payload:
                    raise ValueError(INVALID)
                return
            temporary = f".recovery-{uuid4().hex}.part"
            try:
                parent.write_bytes(temporary, payload)
                parent.replace(temporary, relative.name)
            finally:
                parent.unlink(temporary, missing_ok=True)


def _validate_old_tree(root: Path, prefix: str, before: dict, *, partial: bool) -> bool:
    path = root / prefix
    if not path.exists():
        if not partial:
            raise ValueError(INVALID)
        return False
    expected = {name: payload for name, payload in before.items() if name.startswith(prefix + "/")}
    with PinnedPackageTree.open(path) as tree:
        actual = {f"{prefix}/{file.relative_to(tree.root).as_posix()}":
                  tree.read_bytes(file.relative_to(tree.root).as_posix(), limit=_LIMIT)
                  for file in tree.files}
        if (not partial and actual != expected) or any(
            payload != expected.get(name) for name, payload in actual.items()
        ):
            raise ValueError(INVALID)
    return True


def apply_recovery(
    service: PluginLifecycleService, archive: Path, *, confirmation: str,
    key_ring: TrustedPublisherKeyRing | None = None,
) -> None:
    """Publish or explicitly resume one exact prepared batch while runtime is gated."""
    root = service.plugin_registry.state_root.parent
    archive = Path(archive).absolute()
    ring = key_ring or load_embedded_key_ring()
    with service._authority_mutation_lock:
        if service.plugin_grants is not None:
            raise ValueError(INVALID)
        plan, before, after = _load_plan(archive, confirmation)
        if plan["source_root"] != str(root) or _protected(root) != plan["protected"]:
            raise ValueError(INVALID)
        _require_dormant_authority(root)
        old_records = _record_document(before["plugins/registry.json"])
        new_records = _record_document(after["plugins/registry.json"])
        if set(old_records) != set(new_records) or set(old_records) != set(plan["upgrades"]):
            raise ValueError(INVALID)
        old_apps = _json(before["mcp/apps_registry.json"])["apps"]
        allowed_before, allowed_after = set(DOCUMENTS), set(DOCUMENTS)
        for plugin_id, old_record in old_records.items():
            new_record = new_records[plugin_id]
            prefix = f"plugins/packages/{plugin_id}/{new_record.selected_version}"
            successor = verify_plugin_package(archive / "after" / prefix, ring, "3.0.0")
            if new_record.enabled or len(new_record.versions) != 1 or (
                [old_record.selected_version, new_record.selected_version] != plan["upgrades"][plugin_id]
                or new_record.versions[0].manifest_sha256 != successor.manifest_sha256
                or new_record.versions[0].package_sha256 != successor.package_sha256
            ):
                raise ValueError(INVALID)
            allowed_after.update({f"{prefix}/plugin.json", *(f"{prefix}/{n}" for n in successor.asset_bytes)})
            for version in old_record.versions:
                old_prefix = f"plugins/packages/{plugin_id}/{version.plugin_version}"
                path = archive / "before" / old_prefix
                _, payloads = read_legacy_package(path, version, ring)
                allowed_before.update(f"{old_prefix}/{name}" for name in payloads)
                if version.state == "selected":
                    app_id = successor.manifest.required_apps[0].app_id
                    verify_legacy_successor(path, version, old_apps[app_id], successor, ring)
        if set(before) != allowed_before or set(after) != allowed_after:
            raise ValueError(INVALID)
        marker = _store(root, f"plugins/{MARKER}")
        pending = canonical_bytes({"schema_version": 1, "plan_sha256": confirmation, "complete": False})
        complete = canonical_bytes({"schema_version": 1, "plan_sha256": confirmation, "complete": True})
        with marker.locked():
            observed_marker = marker.read_bytes()
            if observed_marker not in (None, pending, complete):
                raise ValueError(INVALID)
            for name in DOCUMENTS:
                observed = _read(root, name)
                allowed = (before[name],) if observed_marker is None else (before[name], after[name])
                if observed not in allowed:
                    raise ValueError(INVALID)
            partial = observed_marker is not None and all(
                _read(root, name) == after[name] for name in DOCUMENTS
            )
            for plugin_id, record in old_records.items():
                for version in record.versions:
                    _validate_old_tree(root, f"plugins/packages/{plugin_id}/{version.plugin_version}",
                                       before, partial=partial)
            if observed_marker is None:
                marker.write_bytes(pending)
        # New package paths do not overlap the archived versions. They remain
        # unselected until all bytes are verified and the registry is published.
        for name, payload in after.items():
            if name not in DOCUMENTS:
                _publish_asset(root, name, payload)
        for plugin_id, record in new_records.items():
            verify_plugin_package(service.plugin_registry.packages_root / plugin_id /
                                  record.selected_version, ring, "3.0.0")
        for name in DOCUMENTS:
            _publish_document(root, name, after[name], before[name])
        # Exact old bytes were archived before publication. A partial removal
        # can resume, but added or modified bytes never get recursively deleted.
        for plugin_id, record in old_records.items():
            for version in record.versions:
                prefix = f"plugins/packages/{plugin_id}/{version.plugin_version}"
                path = root / prefix
                if _validate_old_tree(root, prefix, before, partial=True):
                    with PinnedDirectory.open(path.parent) as parent:
                        with parent.pin_tree(path.name) as pinned:
                            pinned.remove()
        if _protected(root) != plan["protected"]:
            raise ValueError(INVALID)
        for plugin_id in new_records:
            service.plugin_registry.reverify_installed(plugin_id)
        if any(record.enabled for record in service.app_registry.snapshot().records):
            raise ValueError(INVALID)
        with marker.locked():
            marker.write_bytes(complete)

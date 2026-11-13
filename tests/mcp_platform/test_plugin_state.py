from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from hashlib import sha256
from types import SimpleNamespace

import pytest

from mcp_platform.canonical import canonical_bytes
from mcp_platform.plugin_authority import PluginExecutionPolicy
from mcp_platform.plugin_connections import AuthorizationStart, PluginConnectionCoordinator
from mcp_platform.plugin_state import (
    PluginAppState,
    PluginLifecycleSnapshot,
    PluginLifecycleState,
)
from plugin_platform.contracts import (
    AppRequirement,
    PluginAuthenticationKind,
    PluginKind,
    PluginManifest,
)
from plugin_platform.records import (
    InstalledPluginRecord,
    InstalledPluginVersionRecord,
    PluginAppBinding,
)


_ACCOUNT_KEY = "operator-account"
_ACCOUNT_DIGEST = sha256(_ACCOUNT_KEY.encode()).hexdigest()
_REVISION = "a" * 64
_REVISION_TWO = "b" * 64
# The operator's explicit consent for an ``_app``: its one offered OAuth scope
# and its (empty) declared tool capabilities.
_RECORDS_GRANT = {
    "scopes": ("records.read",),
    "capability_ceiling": (),
    "allowed_tool_schemas": {},
}


def _app(app_id: str, *, authentication: str = "oauth_authorization_code_pkce"):
    uses_declared_secret = authentication == "api_key"
    return AppRequirement.from_dict(
        {
            "app_id": app_id,
            "publisher_id": "test.publisher",
            "deployment": "remote",
            "provider_id": "test.provider",
            "protocol_version": "2026-07-28",
            "transports": ["streamable_http"],
            "remote_origin": f"https://{app_id}.example.test/mcp/v1",
            "authorization_issuer": "https://auth.example.test",
            "authentication": {
                "kind": authentication,
                "scopes": (
                    ["records.read"]
                    if authentication.startswith("oauth_")
                    else []
                ),
                "secret_fields": (
                    [
                        {
                            "field_id": "api_key",
                            "label": "API key",
                            "secret": True,
                            "required": True,
                        }
                    ]
                    if uses_declared_secret
                    else []
                ),
            },
            "capabilities": [],
        }
    )


def _manifest(
    plugin_id: str,
    *,
    kind: PluginKind,
    required: tuple[AppRequirement, ...] = (),
    optional: tuple[AppRequirement, ...] = (),
) -> PluginManifest:
    return PluginManifest(
        manifest_version=2,
        plugin_id=plugin_id,
        publisher_id="test.publisher",
        plugin_version="1.0.0",
        minimum_omega_version="3.0.0",
        kind=kind,
        title="Test plugin",
        description="Provider-neutral test plugin.",
        assets=(),
        entry_points=(),
        starter_requests=(),
        required_apps=required,
        optional_apps=optional,
        schedule_template_paths=(),
        proactive_event_rules=(),
        enterprise_roles=(),
        signing_key_id="test-key",
        signature="signed",
    )


def _binding(
    required_app_ids: tuple[str, ...],
    optional_app_ids: tuple[str, ...],
) -> PluginAppBinding:
    required = tuple(sorted(required_app_ids))
    optional = tuple(sorted(optional_app_ids))
    receipt = "dependency-receipt"
    revision = sha256(
        canonical_bytes(
            {
                "required_app_ids": list(required),
                "optional_app_ids": list(optional),
                "dependency_receipt_id": receipt,
            }
        )
    ).hexdigest()
    return PluginAppBinding(required, optional, receipt, revision)


def _record(manifest: PluginManifest, *, enabled: bool) -> InstalledPluginRecord:
    selected = InstalledPluginVersionRecord(
        plugin_version=manifest.plugin_version,
        signing_key_id=manifest.signing_key_id,
        manifest_sha256="b" * 64,
        package_sha256="c" * 64,
        state="selected",
    )
    return InstalledPluginRecord(
        plugin_id=manifest.plugin_id,
        publisher_id=manifest.publisher_id,
        kind=manifest.kind.value,
        selected_version=manifest.plugin_version,
        enabled=enabled,
        status="installed",
        blocker=None,
        versions=(selected,),
        app_binding=_binding(
            tuple(app.app_id for app in manifest.required_apps),
            tuple(app.app_id for app in manifest.optional_apps),
        ),
    )


class _Registry:
    def __init__(self, manifest: PluginManifest, *, enabled: bool = False) -> None:
        self.manifest = manifest
        self.record = _record(manifest, enabled=enabled)

    def get(self, plugin_id: str):
        assert plugin_id == self.record.plugin_id
        return self.record

    def reverify_installed(self, plugin_id: str):
        assert plugin_id == self.record.plugin_id
        return SimpleNamespace(manifest=self.manifest, package_sha256="c" * 64)

    def snapshot(self):
        return SimpleNamespace(revision=_REVISION)

    def verified_installed_snapshot(self, plugin_id: str):
        assert plugin_id == self.record.plugin_id
        return SimpleNamespace(
            record=self.record,
            verified=SimpleNamespace(manifest=self.manifest, package_sha256="c" * 64),
            revision=_REVISION,
        )


class _LegacyAuthorityRegistry(_Registry):
    """A registry whose reverified package the verifier flagged legacy_authority."""

    def reverify_installed(self, plugin_id: str):
        assert plugin_id == self.record.plugin_id
        return SimpleNamespace(
            manifest=self.manifest,
            package_sha256="c" * 64,
            legacy_authority=True,
        )

    def verified_installed_snapshot(self, plugin_id: str):
        assert plugin_id == self.record.plugin_id
        return SimpleNamespace(
            record=self.record,
            verified=SimpleNamespace(
                manifest=self.manifest,
                package_sha256="c" * 64,
                legacy_authority=True,
            ),
            revision=_REVISION,
        )


@dataclass
class _Connection:
    states: dict[str, str]
    authorization_flow: str = "authorization_code_pkce"

    def __post_init__(self) -> None:
        self.starts: list[str] = []
        self.start_revisions: list[str | None] = []
        self.revoked: list[str] = []
        self.revoke_revisions: list[str | None] = []
        self.status_reads: list[str] = []

    async def status(self, plugin_id: str, *, app_id: str, account_key: str):
        del plugin_id, account_key
        self.status_reads.append(app_id)
        state = self.states.get(app_id, "installed_disconnected")
        return SimpleNamespace(
            status=state,
            authority_revision=(_REVISION if state == "connected" else ""),
        )

    async def start(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        scopes: tuple[str, ...] | None = None,
        capability_ceiling: tuple[str, ...] | None = None,
        allowed_tool_schemas: Mapping[str, str] | None = None,
        execution_policy: PluginExecutionPolicy | str | None = None,
        source_constraints: Mapping[str, object] | None = None,
        destination_constraints: Mapping[str, object] | None = None,
        expected_registry_revision: str | None = None,
    ):
        del (
            scopes,
            capability_ceiling,
            allowed_tool_schemas,
            execution_policy,
            source_constraints,
            destination_constraints,
        )
        self.starts.append(app_id)
        self.start_revisions.append(expected_registry_revision)
        self.states[app_id] = "connected"
        return AuthorizationStart(
            plugin_id=plugin_id,
            app_id=app_id,
            account_key_sha256=_ACCOUNT_DIGEST,
            status="authorizing",
            request_id=_REVISION,
            authorization_url="https://auth.example.test/authorize",
            expires_at=60.0,
            authorization_context_revision=_REVISION,
            provider_scopes=("records.read",),
            execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
            authorization_flow=self.authorization_flow,
        )

    async def revoke(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key: str,
        expected_registry_revision: str | None = None,
    ):
        del plugin_id, account_key
        self.revoked.append(app_id)
        self.revoke_revisions.append(expected_registry_revision)
        self.states[app_id] = "installed_disconnected"


class _InterleavingRegistry(_Registry):
    def __init__(self, manifest: PluginManifest) -> None:
        super().__init__(manifest, enabled=True)
        self.revision = _REVISION
        self._next_manifest = replace(manifest, plugin_version="2.0.0")

    def promote(self) -> None:
        self.manifest = self._next_manifest
        self.record = _record(self.manifest, enabled=True)
        self.revision = _REVISION_TWO

    def snapshot(self):
        return SimpleNamespace(revision=self.revision)

    def verified_installed_snapshot(self, plugin_id: str):
        assert plugin_id == self.record.plugin_id
        return SimpleNamespace(
            record=self.record,
            verified=SimpleNamespace(manifest=self.manifest, package_sha256="c" * 64),
            revision=self.revision,
        )


class _InterleavingConnection(_Connection):
    def __init__(self, registry: _InterleavingRegistry) -> None:
        super().__init__({"mail": "connected"})
        self.registry = registry
        self.status_calls = 0

    async def status(self, plugin_id: str, *, app_id: str, account_key: str):
        result = await super().status(plugin_id, app_id=app_id, account_key=account_key)
        self.status_calls += 1
        if self.status_calls == 1:
            self.registry.promote()
        return result


def _coordinator(
    manifest: PluginManifest,
    states: dict[str, str] | None = None,
    *,
    enabled: bool = False,
) -> tuple[PluginConnectionCoordinator, _Connection]:
    connection = _Connection({} if states is None else states)
    return (
        PluginConnectionCoordinator(
            plugin_registry=_Registry(manifest, enabled=enabled),
            connection_service=connection,
        ),
        connection,
    )


@pytest.mark.anyio
async def test_composite_is_connected_only_when_all_required_apps_are_connected() -> None:
    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.COMPOSITE,
        required=(_app("mail"), _app("calendar")),
        optional=(_app("drive"),),
    )
    coordinator, connection = _coordinator(
        manifest,
        {"drive": "offline"},
        enabled=True,
    )

    assert (await coordinator.status("office.bundle", _ACCOUNT_KEY)).state == "setup_required"
    await coordinator.start("office.bundle", "mail", _ACCOUNT_KEY, None, **_RECORDS_GRANT)
    first = await coordinator.status("office.bundle", _ACCOUNT_KEY)
    assert first.state == "setup_required"
    assert first.next_required_app_id == "calendar"

    await coordinator.start("office.bundle", "calendar", _ACCOUNT_KEY, None, **_RECORDS_GRANT)
    snapshot = await coordinator.status("office.bundle", _ACCOUNT_KEY)

    assert snapshot.state == "connected"
    assert snapshot.next_required_app_id is None
    assert snapshot.dependencies_ready(("mail", "calendar")) is True
    assert snapshot.dependencies_ready(("drive",)) is False
    assert snapshot.apps[-1].app_id == "drive"
    assert snapshot.apps[-1].state == "offline"
    assert connection.starts == ["mail", "calendar"]


@pytest.mark.anyio
async def test_skill_only_plugin_enables_without_connection_or_fake_app() -> None:
    manifest = _manifest("writing.guide", kind=PluginKind.SKILL_ONLY)
    coordinator, connection = _coordinator(manifest, enabled=True)

    snapshot = await coordinator.status("writing.guide", _ACCOUNT_KEY)

    assert snapshot.state == "connected"
    assert snapshot.apps == ()
    assert snapshot.enabled is True
    assert connection.starts == []


@pytest.mark.anyio
async def test_blocked_skill_only_plugin_has_no_fake_apps() -> None:
    manifest = _manifest("writing.guide", kind=PluginKind.SKILL_ONLY)
    registry = _Registry(manifest, enabled=True)
    registry.record = replace(registry.record, blocker="dependency_unavailable")
    connection = _Connection({})
    coordinator = PluginConnectionCoordinator(
        plugin_registry=registry,
        connection_service=connection,
    )

    snapshot = await coordinator.status("writing.guide", _ACCOUNT_KEY)

    assert snapshot.state is PluginLifecycleState.BLOCKED
    assert snapshot.apps == ()
    assert snapshot.error_code == "plugin_blocked"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("durable_connection_state", "expected"),
    (
        ("revocation_pending", PluginLifecycleState.REVOCATION_PENDING),
        ("permission_changed", PluginLifecycleState.PERMISSION_CHANGED),
        ("incompatible", PluginLifecycleState.BLOCKED),
        ("offline", PluginLifecycleState.BLOCKED),
        ("connecting", PluginLifecycleState.BLOCKED),
        ("setup_required", PluginLifecycleState.BLOCKED),
        ("connected", PluginLifecycleState.BLOCKED),
    ),
)
async def test_registry_blocker_combines_with_real_durable_connection_evidence(
    durable_connection_state: str,
    expected: PluginLifecycleState,
) -> None:
    """A blocker is evidence at its approved precedence, not a read shortcut."""

    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.MCP_BACKED,
        required=(_app("mail"),),
    )
    registry = _Registry(manifest, enabled=True)
    registry.record = replace(registry.record, blocker="dependency_unavailable")
    connection = _Connection({"mail": durable_connection_state})
    coordinator = PluginConnectionCoordinator(
        plugin_registry=registry,
        connection_service=connection,
    )

    snapshot = await coordinator.status("office.bundle", _ACCOUNT_KEY)

    assert snapshot.state is expected
    assert connection.status_reads == ["mail"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("durable_connection_state", "expected"),
    (
        ("revocation_pending", PluginLifecycleState.REVOCATION_PENDING),
        ("permission_changed", PluginLifecycleState.UPDATE_REQUIRED),
        ("offline", PluginLifecycleState.UPDATE_REQUIRED),
        ("setup_required", PluginLifecycleState.UPDATE_REQUIRED),
        ("connected", PluginLifecycleState.UPDATE_REQUIRED),
    ),
)
async def test_legacy_authority_projects_update_required(
    durable_connection_state: str,
    expected: PluginLifecycleState,
) -> None:
    """A legacy-authority package is a mandatory migration signal, not a silent pass.

    Spec principle 8 forbids a compatibility fork: an already-installed
    package the verifier flagged ``legacy_authority`` must project
    ``update_required`` -- outranking ordinary connection states, including
    a live ``connected`` app -- so the operator is told to install the
    sequence-8 v2 package rather than keep executing on the retired shape.
    An active revocation still takes precedence.
    """

    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.MCP_BACKED,
        required=(_app("mail"),),
    )
    registry = _LegacyAuthorityRegistry(manifest, enabled=True)
    connection = _Connection({"mail": durable_connection_state})
    coordinator = PluginConnectionCoordinator(
        plugin_registry=registry,
        connection_service=connection,
    )

    snapshot = await coordinator.status("office.bundle", _ACCOUNT_KEY)

    assert snapshot.state is expected
    assert snapshot.error_code == (
        "plugin_revocation_pending"
        if expected is PluginLifecycleState.REVOCATION_PENDING
        else "plugin_update_required"
    )


@pytest.mark.anyio
async def test_legacy_authority_outranks_a_registry_blocker() -> None:
    """Catches a forced blocked state hiding the more actionable update signal."""

    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.MCP_BACKED,
        required=(_app("mail"),),
    )
    registry = _LegacyAuthorityRegistry(manifest, enabled=True)
    registry.record = replace(registry.record, blocker="dependency_unavailable")
    connection = _Connection({"mail": "connected"})
    coordinator = PluginConnectionCoordinator(
        plugin_registry=registry,
        connection_service=connection,
    )

    snapshot = await coordinator.status("office.bundle", _ACCOUNT_KEY)

    assert snapshot.state is PluginLifecycleState.UPDATE_REQUIRED
    assert snapshot.error_code == "plugin_update_required"


@pytest.mark.anyio
async def test_capability_dependencies_require_enabled_canonical_connected_state() -> None:
    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.MCP_BACKED,
        required=(_app("mail"),),
    )
    disabled, _connection = _coordinator(
        manifest,
        {"mail": "connected"},
        enabled=False,
    )
    blocked, _connection = _coordinator(
        manifest,
        {"mail": "blocked"},
        enabled=True,
    )

    disabled_snapshot = await disabled.status("office.bundle", _ACCOUNT_KEY)
    blocked_snapshot = await blocked.status("office.bundle", _ACCOUNT_KEY)

    assert disabled_snapshot.state is PluginLifecycleState.CONNECTED
    assert disabled_snapshot.dependencies_ready(("mail",)) is False
    assert blocked_snapshot.state is PluginLifecycleState.BLOCKED
    assert blocked_snapshot.dependencies_ready(()) is False


@pytest.mark.anyio
async def test_status_retries_when_registry_revision_changes_during_app_observation() -> None:
    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.MCP_BACKED,
        required=(_app("mail"),),
    )
    registry = _InterleavingRegistry(manifest)
    connection = _InterleavingConnection(registry)
    coordinator = PluginConnectionCoordinator(
        plugin_registry=registry,
        connection_service=connection,
    )

    snapshot = await coordinator.status("office.bundle", _ACCOUNT_KEY)

    assert snapshot.registry_revision == _REVISION_TWO
    assert snapshot.package_version == "2.0.0"
    assert connection.status_calls == 2


@pytest.mark.anyio
async def test_mutating_operations_carry_the_atomic_registry_revision() -> None:
    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.MCP_BACKED,
        required=(_app("mail"),),
    )
    coordinator, connection = _coordinator(manifest, enabled=True)

    await coordinator.start("office.bundle", "mail", _ACCOUNT_KEY, None, **_RECORDS_GRANT)
    await coordinator.disconnect("office.bundle", _ACCOUNT_KEY)

    assert connection.start_revisions == [_REVISION]
    assert connection.revoke_revisions == [_REVISION]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("higher", "lower"),
    (
        ("revocation_pending", "permission_changed"),
        ("permission_changed", "blocked"),
        ("blocked", "incompatible"),
        ("incompatible", "offline"),
        ("offline", "connecting"),
        ("connecting", "setup_required"),
        ("setup_required", "connected"),
    ),
)
async def test_required_app_state_precedence_is_exact(higher: str, lower: str) -> None:
    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.COMPOSITE,
        required=(_app("mail"), _app("calendar")),
    )
    coordinator, _connection = _coordinator(
        manifest,
        {"mail": lower, "calendar": higher},
        enabled=True,
    )

    assert (await coordinator.status("office.bundle", _ACCOUNT_KEY)).state == higher


def test_lifecycle_snapshot_rejects_connected_with_incomplete_required_app() -> None:
    with pytest.raises(ValueError, match="plugin_lifecycle_snapshot_invalid"):
        PluginLifecycleSnapshot(
            plugin_id="office.bundle",
            state=PluginLifecycleState.CONNECTED,
            registry_revision=_REVISION,
            package_version="1.0.0",
            apps=(
                PluginAppState(
                    app_id="mail",
                    required=True,
                    state=PluginLifecycleState.SETUP_REQUIRED,
                    authority_revision="",
                    error_code="plugin_setup_required",
                ),
            ),
            next_required_app_id=None,
            enabled=True,
            error_code=None,
        )


@pytest.mark.parametrize(
    ("state", "wrong_error"),
    (
        (PluginLifecycleState.SETUP_REQUIRED, "plugin_offline"),
        (PluginLifecycleState.OFFLINE, "plugin_setup_required"),
        (PluginLifecycleState.PERMISSION_CHANGED, "plugin_offline"),
        (PluginLifecycleState.REVOCATION_PENDING, "plugin_offline"),
        (PluginLifecycleState.BLOCKED, "plugin_offline"),
        (PluginLifecycleState.INCOMPATIBLE, "plugin_offline"),
    ),
)
def test_app_state_rejects_noncanonical_failure_error(
    state: PluginLifecycleState,
    wrong_error: str,
) -> None:
    with pytest.raises(ValueError, match="^plugin_lifecycle_snapshot_invalid$"):
        PluginAppState(
            app_id="mail",
            required=True,
            state=state,
            authority_revision="",
            error_code=wrong_error,
        )


@pytest.mark.parametrize(
    ("app_state", "app_error", "snapshot_state", "snapshot_error"),
    (
        (
            PluginLifecycleState.CONNECTED,
            None,
            PluginLifecycleState.OFFLINE,
            "plugin_offline",
        ),
        (
            PluginLifecycleState.OFFLINE,
            "plugin_offline",
            PluginLifecycleState.SETUP_REQUIRED,
            "plugin_setup_required",
        ),
        (
            PluginLifecycleState.PERMISSION_CHANGED,
            "plugin_permission_changed",
            PluginLifecycleState.REVOCATION_PENDING,
            "plugin_revocation_pending",
        ),
    ),
)
def test_lifecycle_snapshot_rejects_state_not_derived_from_required_apps(
    app_state: PluginLifecycleState,
    app_error: str | None,
    snapshot_state: PluginLifecycleState,
    snapshot_error: str | None,
) -> None:
    authority_revision = _REVISION if app_state is PluginLifecycleState.CONNECTED else ""
    next_required = None if app_state is PluginLifecycleState.CONNECTED else "mail"
    with pytest.raises(ValueError, match="^plugin_lifecycle_snapshot_invalid$"):
        PluginLifecycleSnapshot(
            plugin_id="office.bundle",
            state=snapshot_state,
            registry_revision=_REVISION,
            package_version="1.0.0",
            apps=(
                PluginAppState(
                    app_id="mail",
                    required=True,
                    state=app_state,
                    authority_revision=authority_revision,
                    error_code=app_error,
                ),
            ),
            next_required_app_id=next_required,
            enabled=True,
            error_code=snapshot_error,
        )


@pytest.mark.anyio
async def test_start_rejects_an_app_not_owned_by_verified_installed_binding() -> None:
    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.COMPOSITE,
        required=(_app("mail"), _app("calendar")),
    )
    coordinator, connection = _coordinator(manifest)

    with pytest.raises(ValueError, match="plugin_app_not_installed"):
        await coordinator.start("office.bundle", "drive", _ACCOUNT_KEY, None)

    assert connection.starts == []


@pytest.mark.anyio
async def test_start_rejects_oauth_flow_that_differs_from_reverified_manifest() -> None:
    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.MCP_BACKED,
        required=(_app("mail", authentication="oauth_device_code"),),
    )
    coordinator, connection = _coordinator(manifest)

    with pytest.raises(ValueError, match="plugin_authentication_mismatch"):
        await coordinator.start("office.bundle", "mail", _ACCOUNT_KEY, None, **_RECORDS_GRANT)

    assert connection.authorization_flow == "authorization_code_pkce"
    assert connection.starts == ["mail"]


@pytest.mark.anyio
async def test_declared_secret_auth_never_falls_back_to_oauth() -> None:
    manifest = _manifest(
        "records.bundle",
        kind=PluginKind.MCP_BACKED,
        required=(_app("records", authentication="api_key"),),
    )
    coordinator, connection = _coordinator(manifest)

    with pytest.raises(ValueError, match="plugin_secret_required"):
        await coordinator.start("records.bundle", "records", _ACCOUNT_KEY, None)

    assert connection.starts == []


@pytest.mark.anyio
async def test_disconnect_visits_every_bound_app_in_verified_manifest_order() -> None:
    manifest = _manifest(
        "office.bundle",
        kind=PluginKind.COMPOSITE,
        required=(_app("mail"), _app("calendar")),
        optional=(_app("drive"),),
    )
    coordinator, connection = _coordinator(
        manifest,
        {"mail": "connected", "calendar": "connected", "drive": "connected"},
        enabled=True,
    )

    snapshot = await coordinator.disconnect("office.bundle", _ACCOUNT_KEY)

    assert connection.revoked == ["mail", "calendar", "drive"]
    assert snapshot.state == "setup_required"
    assert snapshot.next_required_app_id == "mail"


def test_plugin_authentication_kind_enum_still_covers_no_auth_remote() -> None:
    assert PluginAuthenticationKind.NONE.value == "none"

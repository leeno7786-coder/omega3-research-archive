from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from mcp_types.version import LATEST_HANDSHAKE_VERSION

from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.plugin_authority import PluginAuthorityStore, PluginExecutionPolicy
from mcp_platform.plugin_connections import ConnectionStatus, PluginUpdateConnectionRecovery
from mcp_platform.state_store import McpStateStore
from mcp_platform.plugin_state import (
    PluginAppState,
    PluginLifecycleSnapshot,
    PluginLifecycleState,
)
from mcp_platform.plugin_lifecycle import (
    PluginAppSelectionRequired,
    PluginLifecycleService,
    compose_plugin_lifecycle_service,
)
import mcp_platform.plugin_lifecycle as lifecycle_module
from plugin_platform.installer import (
    DependencyCommitReceipt,
    PluginInstallResult,
    PluginMutationResult,
)
from plugin_platform.records import (
    InstalledPluginRecord,
    InstalledPluginVersionRecord,
    PluginAppBinding,
    PluginRegistrySnapshot,
)
from tests.helpers.plugin_consent import full_connection_grant, full_permission_settings
from tests.mcp_platform.generic_plugin_peers import TRACKER_APP, TRACKER_PLUGIN, catalogs_for
from tests.mcp_platform.test_plugin_connections import _Client, _remote_service


# --- explicit per-app consent ------------------------------------------------
#
# A connection starts only under the operator's explicit per-app selection. These
# fakes state one exact selection and the offer it is validated against.

_TOOL_DIGEST = "7" * 64


def _consent(*, scopes: tuple[str, ...] = ()) -> dict[str, object]:
    """One app's consent: a settings body, and the document recorded per candidate app."""
    return {
        "provider_scopes": list(scopes),
        "capability_ids": ["tool:send"],
        "execution_policy": "confirm_each",
        "source_constraints": {},
        "destination_constraints": {},
    }


def _offered(app_id: str, *, scopes: tuple[str, ...] = ()) -> dict[str, object]:
    """The ``permissions()`` offer a consent body is validated against."""
    return {
        "app_id": app_id,
        "offered": {"provider_scopes": list(scopes), "capability_ids": ["tool:send"]},
    }


def _app_descriptor(app_id: str) -> SimpleNamespace:
    return SimpleNamespace(app_id=app_id, declared_capabilities={"tool:send": _TOOL_DIGEST})


def _candidate_definition(*, scopes: tuple[str, ...] = ()):
    """Fake ``PluginConnectionService._candidate_definition``: the candidate app's offer."""

    def definition(_plugin_id: str, app_id: str, **_kwargs: object) -> tuple[object, ...]:
        requirement = SimpleNamespace(authentication=SimpleNamespace(scopes=scopes))
        return None, None, requirement, _app_descriptor(app_id)

    return definition


def _live_status(app_id: str, *, scopes: tuple[str, ...] = ()) -> ConnectionStatus:
    """The predecessor's live connected authority for one app it owns."""
    return ConnectionStatus(
        plugin_id="office.bundle",
        app_id=app_id,
        account_key_sha256=sha256_hex(b"operator"),
        status="connected",
        authorization_context_revision="8" * 64,
        token_revision=None,
        provider_scopes=scopes,
        execution_policy=PluginExecutionPolicy.CONFIRM_EACH,
        authority_revision="9" * 64,
        capability_ceiling=("tool:send",),
        allowed_tool_schemas={"send": _TOOL_DIGEST},
    )


def _production_python_text() -> str:
    roots = (
        Path("auto_loop"),
        Path("global_loop"),
        Path("hud"),
        Path("mcp_platform"),
    )
    return "\n".join(
        path.read_text(encoding="utf-8")
        for root in roots
        for path in sorted(root.rglob("*.py"))
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    "plugin_id",
    (
        "github.official",
        "google.workspace.drive",
        "google.workspace.gmail",
        "google.workspace.calendar",
    ),
)
async def test_current_provider_packages_use_one_lifecycle_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    plugin_id: str,
) -> None:
    monkeypatch.setenv("OMEGA_OPERATOR_ACCOUNT_KEY", "operator-account")
    service = compose_plugin_lifecycle_service(
        state_root=tmp_path,
        oauth_key_provider=lambda: b"l" * 32,
        operation_submitter=lambda operation, **_kwargs: asyncio.run(operation()),
    )
    review = service.review(plugin_id)
    installed = service.install(plugin_id, confirmation=review.sha256)

    assert installed.record.plugin_id == plugin_id
    assert (await service.status(plugin_id)).state == "setup_required"
    assert type(service).__name__ == "PluginLifecycleService"
    await asyncio.to_thread(service.close)


def test_current_signed_drive_descriptor_retains_pinned_sdk_wire_compatibility(
    tmp_path: Path,
) -> None:
    """Catches a signed retained package becoming unusable under the pinned SDK."""
    service = PluginLifecycleService(state_root=tmp_path)
    _install_catalog_plugin(service, "google.workspace.drive")

    descriptor = service.descriptor_for_app("google.workspace.drive.mcp")
    assert descriptor.runtime_compatibility == {
        "handshake_mode": "legacy",
        "missing_metadata": {
            "server_id": "StatelessServer",
            "server_version": "ESF",
        },
        "wire_protocol_version": LATEST_HANDSHAKE_VERSION,
    }


def test_retired_runtime_service_is_not_importable_or_referenced() -> None:
    production = _production_python_text()
    assert importlib.util.find_spec("mcp_platform.featured_plugins") is None
    assert "FeaturedPluginService" not in production
    assert "get_default_featured_plugin_service" not in production
    assert "AuthoritativeConnectorCatalogSourceAdapter" not in production
    assert "_account_key =" not in production


@pytest.mark.anyio
async def test_multi_app_start_requires_an_exact_app_selection() -> None:
    service = object.__new__(PluginLifecycleService)
    service._authority_mutation_lock = asyncio.Lock()
    service._lifecycle_revision_snapshot = lambda: SimpleNamespace(revision="stable")
    service._app_ids = lambda _plugin_id: ("office.mail", "office.calendar")
    service.operator_account_key = lambda: "operator-account"
    coordinator = SimpleNamespace(start=lambda *_args: None)
    service._connection_coordinator = coordinator

    result = await service.start_connection("office.bundle")

    assert result == PluginAppSelectionRequired(
        plugin_id="office.bundle",
        app_ids=("office.mail", "office.calendar"),
    )


@pytest.mark.anyio
async def test_start_connection_forwards_the_explicit_app_to_the_coordinator() -> None:
    calls: list[tuple[str, str, str, None, dict[str, object]]] = []
    started = SimpleNamespace(status="connecting")

    async def start(
        plugin_id: str,
        app_id: str,
        account_key: str,
        redirect_uri: None,
        **consent: object,
    ):
        calls.append((plugin_id, app_id, account_key, redirect_uri, consent))
        return started

    async def permissions(_plugin_id: str, app_id: str | None):
        return _offered(str(app_id), scopes=("mail.read",))

    service = object.__new__(PluginLifecycleService)
    service._authority_mutation_lock = asyncio.Lock()
    service._lifecycle_revision_snapshot = lambda: SimpleNamespace(revision="stable")
    service._app_ids = lambda _plugin_id: ("office.mail", "office.calendar")
    service.operator_account_key = lambda: "operator-account"
    service._connection_coordinator = SimpleNamespace(start=start)
    service.permissions = permissions
    service.descriptor_for_plugin_app = lambda _plugin_id, app_id: _app_descriptor(app_id)

    # No selection, no connection: nothing is granted on the operator's behalf.
    with pytest.raises(ValueError, match="^plugin_scopes_invalid$"):
        await service.start_connection("office.bundle", "office.mail")
    assert calls == []

    assert (
        await service.start_connection(
            "office.bundle",
            "office.mail",
            settings=_consent(scopes=("mail.read",)),
        )
        is started
    )
    assert calls == [
        (
            "office.bundle",
            "office.mail",
            "operator-account",
            None,
            {
                "scopes": ("mail.read",),
                "capability_ceiling": ("tool:send",),
                "allowed_tool_schemas": {"send": _TOOL_DIGEST},
                "execution_policy": "confirm_each",
                "source_constraints": {},
                "destination_constraints": {},
            },
        )
    ]


@pytest.mark.anyio
async def test_immediately_connected_remote_start_passes_compile_enable_publication() -> None:
    """Catches authentication-kind none bypassing the runtime authority gate."""
    started = SimpleNamespace(
        plugin_id="public.search",
        app_id="public.search.mcp",
        status="connected",
    )
    published = SimpleNamespace(plugin_id="public.search", state="connected", enabled=True)
    publish_calls: list[object] = []

    async def start(*_args, **_consent):
        return started

    async def publish(status: object):
        publish_calls.append(status)
        return published

    async def permissions(_plugin_id: str, app_id: str | None):
        return _offered(str(app_id))

    service = object.__new__(PluginLifecycleService)
    service._authority_mutation_lock = asyncio.Lock()
    service._lifecycle_revision_snapshot = lambda: SimpleNamespace(revision="stable")
    service._app_ids = lambda _plugin_id: ("public.search.mcp",)
    service.operator_account_key = lambda: "operator-account"
    service._connection_coordinator = SimpleNamespace(start=start)
    service._publish_connected_status = publish
    service.permissions = permissions
    service.descriptor_for_plugin_app = lambda _plugin_id, app_id: _app_descriptor(app_id)

    result = await service.start_connection("public.search", settings=_consent())

    assert result is published
    assert publish_calls == [started]


@pytest.mark.anyio
async def test_skill_only_status_does_not_require_an_operator_account() -> None:
    calls: list[tuple[str, str]] = []

    async def status(plugin_id: str, account_key: str):
        calls.append((plugin_id, account_key))
        return "connected"

    service = object.__new__(PluginLifecycleService)
    service._app_ids = lambda _plugin_id: ()
    service.operator_account_key = lambda: pytest.fail("zero-app status read account key")
    service._connection_coordinator = SimpleNamespace(status=status)

    assert await service.status("portable.skill") == "connected"
    assert calls == [("portable.skill", "")]


@pytest.mark.anyio
async def test_no_account_status_still_applies_registry_blocker_precedence() -> None:
    calls: list[tuple[str, object]] = []
    blocked = SimpleNamespace(state=PluginLifecycleState.BLOCKED)

    async def status(plugin_id: str, account_key: object) -> object:
        calls.append((plugin_id, account_key))
        return blocked

    service = object.__new__(PluginLifecycleService)
    service._app_ids = lambda _plugin_id: ("office.mail",)
    service.operator_account_key = lambda: (_ for _ in ()).throw(
        ValueError("plugin_account_unconfigured")
    )
    service._connection_coordinator = SimpleNamespace(status=status)

    assert await service.status("office.bundle") is blocked
    assert calls == [("office.bundle", None)]


@pytest.mark.anyio
@pytest.mark.parametrize("optional_installed", (False, True))
async def test_optional_only_plugin_preserves_blocker_without_requiring_account(
    optional_installed: bool,
) -> None:
    optional = SimpleNamespace(app_id="office.optional")
    service = object.__new__(PluginLifecycleService)
    service._installed_verified = lambda _plugin_id: SimpleNamespace(
        manifest=SimpleNamespace(required_apps=(), optional_apps=(optional,))
    )
    service.app_ids_for_plugin = lambda _plugin_id: (
        (optional.app_id,) if optional_installed else ()
    )
    service.plugin_registry = SimpleNamespace(
        get=lambda _plugin_id: SimpleNamespace(
            selected_version="1.0.0",
            enabled=True,
            blocker="blocked_by_policy",
        )
    )
    service.operator_account_key = lambda: (_ for _ in ()).throw(
        ValueError("plugin_account_unconfigured")
    )
    calls: list[object] = []

    async def status(plugin_id: str, account_key: object) -> object:
        calls.append((plugin_id, account_key))
        return SimpleNamespace(state=PluginLifecycleState.BLOCKED)

    service._connection_coordinator = SimpleNamespace(status=status)

    assert await service.catalog_state(
        "office.bundle",
        catalog_version="2.0.0",
    ) == PluginLifecycleState.BLOCKED.value
    assert calls == (
        [("office.bundle", None)] if optional_installed else []
    )


@pytest.mark.anyio
@pytest.mark.parametrize("app_ids", ((), ("office.mail",)))
async def test_catalog_blocker_precedes_zero_app_and_no_account_early_results(
    app_ids: tuple[str, ...],
) -> None:
    calls: list[str] = []
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = SimpleNamespace(
        get=lambda _plugin_id: SimpleNamespace(
            selected_version="1.0.0",
            enabled=True,
            blocker="blocked_by_policy",
        )
    )
    service._app_ids = lambda _plugin_id: app_ids

    async def status(plugin_id: str) -> object:
        calls.append(plugin_id)
        return SimpleNamespace(state=PluginLifecycleState.BLOCKED)

    service.status = status

    assert await service.catalog_state(
        "office.bundle",
        catalog_version="2.0.0",
    ) == PluginLifecycleState.BLOCKED.value
    assert calls == ([] if not app_ids else ["office.bundle"])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "local_state, expected",
    (
        ("revocation_pending", PluginLifecycleState.REVOCATION_PENDING),
        ("permission_changed", PluginLifecycleState.PERMISSION_CHANGED),
        (None, PluginLifecycleState.BLOCKED),
    ),
)
async def test_zero_app_catalog_applies_plugin_wide_durable_risk_before_blocker(
    local_state: str | None,
    expected: PluginLifecycleState,
) -> None:
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = SimpleNamespace(
        get=lambda _plugin_id: SimpleNamespace(
            selected_version="1.0.0",
            enabled=True,
            blocker="blocked_by_policy",
        )
    )
    service._app_ids = lambda _plugin_id: ()
    service._has_pending_update_retirement = lambda _plugin_id: False
    service._connection_service = SimpleNamespace(
        local_plugin_state_without_account=lambda _plugin_id: local_state
    )

    assert await service.catalog_state(
        "office.bundle",
        catalog_version="2.0.0",
    ) == expected.value


@pytest.mark.anyio
@pytest.mark.parametrize(
    "canonical_state",
    (
        PluginLifecycleState.REVOCATION_PENDING,
        PluginLifecycleState.PERMISSION_CHANGED,
        PluginLifecycleState.BLOCKED,
        PluginLifecycleState.INCOMPATIBLE,
        PluginLifecycleState.OFFLINE,
        PluginLifecycleState.CONNECTING,
        PluginLifecycleState.SETUP_REQUIRED,
    ),
)
async def test_catalog_update_does_not_hide_higher_priority_canonical_state(
    canonical_state: PluginLifecycleState,
) -> None:
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = SimpleNamespace(
        get=lambda _plugin_id: SimpleNamespace(
            selected_version="1.0.0",
            enabled=True,
            blocker=(
                "blocked_by_policy"
                if canonical_state is PluginLifecycleState.BLOCKED
                else None
            ),
        )
    )
    service._app_ids = lambda _plugin_id: ("office.app",)

    async def status(_plugin_id: str) -> object:
        return SimpleNamespace(state=canonical_state)

    service.status = status

    assert await service.catalog_state(
        "office.bundle",
        catalog_version="2.0.0",
    ) == canonical_state.value


@pytest.mark.anyio
async def test_catalog_connected_state_surfaces_newer_verified_version() -> None:
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = SimpleNamespace(
        get=lambda _plugin_id: SimpleNamespace(
            selected_version="1.0.0",
            enabled=True,
            blocker=None,
        )
    )
    service._app_ids = lambda _plugin_id: ("office.app",)

    async def status(_plugin_id: str) -> object:
        return SimpleNamespace(state=PluginLifecycleState.CONNECTED)

    service.status = status

    assert await service.catalog_state(
        "office.bundle",
        catalog_version="2.0.0",
    ) == PluginLifecycleState.UPDATE_AVAILABLE.value


def test_provider_scopes_are_selected_from_the_exact_signed_app() -> None:
    mail = SimpleNamespace(
        app_id="office.mail",
        authentication=SimpleNamespace(scopes=("mail.read",)),
    )
    calendar = SimpleNamespace(
        app_id="office.calendar",
        authentication=SimpleNamespace(scopes=("calendar.read",)),
    )
    service = object.__new__(PluginLifecycleService)
    service._installed_verified = lambda _plugin_id: SimpleNamespace(
        manifest=SimpleNamespace(required_apps=(mail, calendar), optional_apps=())
    )

    assert service._offered_provider_scopes("office.bundle", "office.calendar") == (
        "calendar.read",
    )


def _bare_lifecycle(*, backend: object | None = None) -> PluginLifecycleService:
    service = object.__new__(PluginLifecycleService)
    service.plugin_backend = backend
    service._oauth_protocol = None
    service._operation_submitter = None
    service._closed = False
    return service


def test_default_lifecycle_composes_once_for_deterministic_concurrent_callers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches an unlocked check-then-compose creating competing stateful roots."""
    lifecycle_module._DEFAULT_SERVICE = None
    compose_calls: list[PluginLifecycleService] = []
    second_compose = threading.Event()
    release_first = threading.Event()
    callers_ready = threading.Barrier(3)

    def compose() -> PluginLifecycleService:
        service = _bare_lifecycle()
        compose_calls.append(service)
        if len(compose_calls) == 1:
            release_first.wait(timeout=1.0)
        else:
            second_compose.set()
        return service

    monkeypatch.setattr(lifecycle_module, "compose_plugin_lifecycle_service", compose)
    results: list[PluginLifecycleService] = []

    def get_default() -> None:
        callers_ready.wait()
        results.append(lifecycle_module.get_default_plugin_lifecycle_service())

    threads = [threading.Thread(target=get_default) for _ in range(2)]
    for thread in threads:
        thread.start()
    callers_ready.wait()
    second_compose.wait(timeout=0.25)
    release_first.set()
    for thread in threads:
        thread.join(timeout=2.0)

    assert len(compose_calls) == 1
    assert len(results) == 2
    assert results[0] is results[1]


def test_closing_exact_default_is_idempotent_and_next_getter_reopens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a closed default being returned forever or closed more than once."""
    backend = SimpleNamespace(close_calls=0)

    def close_backend() -> None:
        backend.close_calls += 1

    backend.close = close_backend
    original = _bare_lifecycle(backend=backend)
    replacement = _bare_lifecycle()
    lifecycle_module._DEFAULT_SERVICE = original
    monkeypatch.setattr(
        lifecycle_module,
        "compose_plugin_lifecycle_service",
        lambda: replacement,
    )

    original.close()
    original.close()
    reopened = lifecycle_module.get_default_plugin_lifecycle_service()

    assert backend.close_calls == 1
    assert reopened is replacement
    assert reopened is not original


def test_closing_nondefault_cannot_clear_the_current_default() -> None:
    """Catches one auxiliary service invalidating the process-owned root."""
    current = _bare_lifecycle()
    auxiliary = _bare_lifecycle()
    lifecycle_module._DEFAULT_SERVICE = current

    auxiliary.close()

    assert lifecycle_module._DEFAULT_SERVICE is current


# --- local trust root derives from the portable vault key ------------------


def test_compose_zero_argument_local_trust_derives_from_the_oauth_vault_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``compose_plugin_lifecycle_service()``, called with no injected vault
    key at all -- today's real ``get_default_plugin_lifecycle_service()``
    path -- must derive the device-local trust root from the exact same key
    that backs the OAuth credential vault, never mint a second, independent,
    directory-bound secret of its own.

    ``DesktopOAuthKeyProvider`` is replaced with a deterministic stand-in
    (a pure function of the directory it is pointed at) so the key each
    minted instance would produce is observable without real DPAPI.
    """
    import mcp_platform.desktop_secrets as desktop_secrets
    from hashlib import sha256

    from plugin_platform.local_trust import LocalTrustRoot

    minted_roots: list[Path] = []

    class _DeterministicKeyProvider:
        def __init__(self, root: Path, vault_path: Path) -> None:
            self.root = Path(root)
            minted_roots.append(self.root)

        def __call__(self) -> bytes:
            return sha256(str(self.root).encode("utf-8")).digest()

    monkeypatch.setattr(desktop_secrets, "DesktopOAuthKeyProvider", _DeterministicKeyProvider)
    # With a sealing passphrase configured a brand-new vault is born sealed
    # (portable custody); this test pins the desktop-custody path.
    monkeypatch.delenv("OMEGA_SEAL_PASSPHRASE", raising=False)
    monkeypatch.delenv("OMEGA_SEAL_PASSPHRASE_FILE", raising=False)

    service = compose_plugin_lifecycle_service(state_root=tmp_path)

    # Exactly one device-local key provider is ever minted -- the OAuth
    # vault's own -- never a second, independent one for local trust.
    assert minted_roots == [tmp_path / "mcp" / "authorization"]

    expected_key = sha256(str(tmp_path / "mcp" / "authorization").encode("utf-8")).digest()
    expected_root = LocalTrustRoot.from_vault_key(expected_key)
    actual_root = service._local_trust_root()

    # LocalTrustRoot's authority MAC key is a deterministic function of the
    # vault key alone; a MAC one root seals must reverify under the other
    # only if both derived from the exact same key.
    document = {"probe": "vault-key-identity"}
    sealed = expected_root.seal_authority(document)
    actual_root.verify_authority(document, sealed)


def test_local_trust_root_reverifies_a_seal_across_fresh_state_roots_under_the_same_key(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The local trust root carries no directory- or host-bound secret of its
    own: given only the same injected vault key, a root composed at a
    brand-new state root -- sharing no file, directory, or prior state with
    the first -- reverifies exactly what the first root sealed."""

    def key_provider() -> bytes:
        return b"k" * 32

    root_a = tmp_path_factory.mktemp("state-a")
    root_b = tmp_path_factory.mktemp("state-b")

    service_a = compose_plugin_lifecycle_service(state_root=root_a, oauth_key_provider=key_provider)
    service_b = compose_plugin_lifecycle_service(state_root=root_b, oauth_key_provider=key_provider)

    document = {"tool": "forecast"}
    sealed = service_a._local_trust_root().seal_authority(document)

    service_b._local_trust_root().verify_authority(document, sealed)


class _RevisionSource:
    def __init__(self, revision: str) -> None:
        self.revision = revision

    def snapshot(self):
        return SimpleNamespace(revision=self.revision)


class _DurableRevisionSource:
    def __init__(self, revision: str) -> None:
        self.revision = revision

    def lifecycle_revision(self) -> str:
        return self.revision

    def invalidate_plugin(self, _plugin_id: str) -> None:
        return None


def _revision_lifecycle() -> PluginLifecycleService:
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = _RevisionSource("1" * 64)
    service.app_registry = _RevisionSource("2" * 64)
    service.package_registry = _RevisionSource("3" * 64)
    service.plugin_grants = _DurableRevisionSource("4" * 64)
    service.permission_store = _DurableRevisionSource("5" * 64)
    service.authority_service = SimpleNamespace(
        store=_DurableRevisionSource("6" * 64)
    )
    service._oauth_vault = _DurableRevisionSource("7" * 64)
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    return service


def test_lifecycle_revision_changes_for_every_displayed_authority_store() -> None:
    """Catches registry-only revisions accepting stale cross-store authority."""
    service = _revision_lifecycle()
    revisions = [service.revision]
    for source, value in (
        (service.app_registry, "8" * 64),
        (service.package_registry, "9" * 64),
        (service.plugin_grants, "a" * 64),
        (service.permission_store, "b" * 64),
        (service.authority_service.store, "c" * 64),
        (service._oauth_vault, "d" * 64),
    ):
        source.revision = value
        revisions.append(service.revision)

    assert len(revisions) == len(set(revisions))


def test_composed_lifecycle_revision_includes_its_durable_intent_store(
    tmp_path: Path,
) -> None:
    """Catches direct McpStateStore components being treated as unavailable."""
    service = PluginLifecycleService(state_root=tmp_path)

    assert len(service.revision) == 64


def test_lifecycle_revision_fails_closed_when_component_snapshot_keeps_drifting() -> None:
    """Catches a mixed-time digest being published as a coherent snapshot."""
    service = _revision_lifecycle()
    counter = 0

    def drifting_snapshot():
        nonlocal counter
        counter += 1
        return SimpleNamespace(revision=f"{counter:064x}")

    service.app_registry.snapshot = drifting_snapshot

    with pytest.raises(ValueError, match="^plugin_lifecycle_revision_conflict$"):
        _ = service.revision


@pytest.mark.anyio
async def test_permission_write_rejects_cross_store_interleaving_before_mutation() -> None:
    """Catches an app/grant drift after the client revision check but before write."""
    service = _revision_lifecycle()
    expected = service.revision
    connection_starts = 0

    async def permissions(_plugin_id: str, _app_id: str | None):
        service.app_registry.revision = "e" * 64
        return {
            "app_id": "office.mail",
            "offered": {"provider_scopes": [], "capability_ids": []},
        }

    async def start(*_args, **_kwargs):
        nonlocal connection_starts
        connection_starts += 1

    service.permissions = permissions
    service._connection_service = SimpleNamespace(start=start)

    with pytest.raises(ValueError, match="^plugin_permission_changed$"):
        await service.set_permissions(
            "office.bundle",
            revision=expected,
            app_id="office.mail",
            settings={
                "provider_scopes": [],
                "capability_ids": [],
                "execution_policy": "confirm_each",
                "source_constraints": {},
                "destination_constraints": {},
            },
        )

    assert connection_starts == 0


@pytest.mark.anyio
async def test_concurrent_permission_writes_accept_one_displayed_revision_once() -> None:
    """Catches two same-revision permission writers mutating after parallel prechecks."""
    service = _revision_lifecycle()
    expected = service.revision
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    starts = 0

    async def permissions(_plugin_id: str, _app_id: str | None):
        return {
            "app_id": "office.mail",
            "offered": {"provider_scopes": [], "capability_ids": []},
        }

    async def start(*_args, **_kwargs):
        nonlocal starts
        starts += 1
        if starts == 1:
            first_entered.set()
            await release_first.wait()
        service.plugin_grants.revision = f"{starts + 4:064x}"
        return SimpleNamespace(status="authorizing")

    service.permissions = permissions
    service.descriptor_for_plugin_app = lambda _plugin_id, _app_id: SimpleNamespace(
        declared_capabilities={}
    )
    service.operator_account_key = lambda: "operator"
    service._connection_service = SimpleNamespace(start=start)
    settings = {
        "provider_scopes": [],
        "capability_ids": [],
        "execution_policy": "confirm_each",
        "source_constraints": {},
        "destination_constraints": {},
    }

    first = asyncio.create_task(
        service.set_permissions(
            "office.bundle",
            revision=expected,
            app_id="office.mail",
            settings=settings,
        )
    )
    await asyncio.wait_for(first_entered.wait(), timeout=1.0)
    second = asyncio.create_task(
        service.set_permissions(
            "office.bundle",
            revision=expected,
            app_id="office.mail",
            settings=settings,
        )
    )
    await asyncio.sleep(0)
    release_first.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert starts == 1
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    errors = [item for item in results if isinstance(item, BaseException)]
    assert len(errors) == 1
    assert str(errors[0]) == "plugin_permission_changed"


@pytest.mark.anyio
async def test_concurrent_connection_starts_accept_one_displayed_revision_once() -> None:
    """Catches two same-revision starts creating authority from one stale snapshot."""
    service = _revision_lifecycle()
    expected = service.revision
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    starts = 0

    async def start(*_args, **_kwargs):
        nonlocal starts
        starts += 1
        if starts == 1:
            first_entered.set()
            await release_first.wait()
        service._oauth_vault.revision = f"{starts + 7:064x}"
        return SimpleNamespace(status="connecting")

    async def permissions(_plugin_id: str, app_id: str | None):
        return _offered(str(app_id), scopes=("mail.read",))

    service._app_ids = lambda _plugin_id: ("office.mail",)
    service.operator_account_key = lambda: "operator"
    service._connection_coordinator = SimpleNamespace(start=start)
    service.permissions = permissions
    service.descriptor_for_plugin_app = lambda _plugin_id, app_id: _app_descriptor(app_id)
    settings = _consent(scopes=("mail.read",))

    first = asyncio.create_task(
        service.start_connection(
            "office.bundle", "office.mail", revision=expected, settings=settings
        )
    )
    await asyncio.wait_for(first_entered.wait(), timeout=1.0)
    second = asyncio.create_task(
        service.start_connection(
            "office.bundle", "office.mail", revision=expected, settings=settings
        )
    )
    await asyncio.sleep(0)
    release_first.set()
    results = await asyncio.gather(first, second, return_exceptions=True)

    assert starts == 1
    assert sum(not isinstance(item, BaseException) for item in results) == 1
    errors = [item for item in results if isinstance(item, BaseException)]
    assert len(errors) == 1
    assert str(errors[0]) == "plugin_permission_changed"


@pytest.mark.anyio
async def test_oauth_callback_finishes_before_concurrent_uninstall_revokes_authority(
    tmp_path: Path,
) -> None:
    service = _revision_lifecycle()
    _attach_lifecycle_operation_state(service, tmp_path)
    callback_entered = asyncio.Event()
    release_callback = asyncio.Event()
    uninstall_entered = asyncio.Event()
    order: list[str] = []

    async def complete_callback(_callback_uri: str) -> object:
        callback_entered.set()
        await release_callback.wait()
        order.append("callback-published")
        return SimpleNamespace(status="connecting")

    async def uninstall_locked(_plugin_id: str) -> None:
        uninstall_entered.set()
        order.append("uninstall-revoked")

    service._connection_service = SimpleNamespace(
        callback_request_id=lambda _callback_uri: "a" * 64,
        complete_callback=complete_callback,
    )
    service._uninstall_locked = uninstall_locked

    callback = asyncio.create_task(service.complete_connection("https://omega.test/callback"))
    await asyncio.wait_for(callback_entered.wait(), timeout=1.0)
    uninstall = asyncio.create_task(service.uninstall("office.bundle"))
    await asyncio.sleep(0)

    assert uninstall_entered.is_set() is False

    release_callback.set()
    await asyncio.gather(callback, uninstall)
    assert order == ["callback-published", "uninstall-revoked"]


@pytest.mark.anyio
async def test_uninstall_delegates_disconnect_before_local_removal() -> None:
    service = object.__new__(PluginLifecycleService)
    service._reconcile_uninstall_intent = lambda: None
    # The pre-check uninstall makes is `_installed_for_removal`, which asks only
    # that the plugin exists and its package verifies. It is deliberately NOT
    # `_installed_verified`: that one also requires a provider-provenance record,
    # and requiring it here made a plugin whose record had been retired
    # impossible to remove.
    service._installed_for_removal = lambda _plugin_id: object()
    service.operator_account_key = lambda: "operator"
    calls: list[tuple[str, str]] = []

    async def disconnect(plugin_id: str, account_key: str) -> None:
        calls.append((plugin_id, account_key))
        raise RuntimeError("stop-after-delegation")

    service._connection_coordinator = SimpleNamespace(disconnect=disconnect)

    with pytest.raises(RuntimeError, match="^stop-after-delegation$"):
        await service._uninstall_locked("office.bundle")

    assert calls == [("office.bundle", "operator")]


@pytest.mark.anyio
async def test_disconnect_serializes_same_revision_permission_write() -> None:
    service = _revision_lifecycle()
    expected = service.revision
    disconnect_entered = asyncio.Event()
    release_disconnect = asyncio.Event()
    connection_starts = 0

    async def disconnect(_plugin_id: str, _account_key: str) -> None:
        disconnect_entered.set()
        await release_disconnect.wait()
        service.plugin_grants.revision = "e" * 64

    async def status(_plugin_id: str) -> object:
        return SimpleNamespace(state=PluginLifecycleState.SETUP_REQUIRED)

    async def permissions(_plugin_id: str, _app_id: str | None) -> dict[str, object]:
        return {
            "app_id": "office.mail",
            "offered": {"provider_scopes": [], "capability_ids": []},
        }

    async def start(*_args, **_kwargs) -> object:
        nonlocal connection_starts
        connection_starts += 1
        return SimpleNamespace(status="authorizing")

    service.operator_account_key = lambda: "operator"
    service._connection_coordinator = SimpleNamespace(disconnect=disconnect)
    service._set_enabled = lambda *_args: None
    service.status = status
    service.permissions = permissions
    service.descriptor_for_plugin_app = lambda _plugin_id, _app_id: SimpleNamespace(
        declared_capabilities={}
    )
    service._connection_service = SimpleNamespace(start=start)

    disconnect_task = asyncio.create_task(service.disconnect("office.bundle"))
    await asyncio.wait_for(disconnect_entered.wait(), timeout=1.0)
    permission_task = asyncio.create_task(
        service.set_permissions(
            "office.bundle",
            revision=expected,
            app_id="office.mail",
            settings={
                "provider_scopes": [],
                "capability_ids": [],
                "execution_policy": "confirm_each",
                "source_constraints": {},
                "destination_constraints": {},
            },
        )
    )
    await asyncio.sleep(0)
    release_disconnect.set()
    results = await asyncio.gather(
        disconnect_task,
        permission_task,
        return_exceptions=True,
    )

    assert connection_starts == 0
    assert str(results[1]) == "plugin_permission_changed"


@pytest.mark.anyio
async def test_device_completion_finishes_before_concurrent_update_mutates(
    tmp_path: Path,
) -> None:
    service = _revision_lifecycle()
    _attach_lifecycle_operation_state(service, tmp_path)
    device_entered = asyncio.Event()
    release_device = asyncio.Event()
    update_entered = threading.Event()

    async def complete_device(_request_id: str) -> object:
        device_entered.set()
        await release_device.wait()
        return SimpleNamespace(status="finalizing")

    def update_locked(*_args, **_kwargs) -> object:
        update_entered.set()
        return lifecycle_module.PluginUpdatePending(
            plugin_id="office.bundle",
            app_ids=("office.mail",),
            next_required_app_id="office.mail",
        )

    service._connection_service = SimpleNamespace(complete_device=complete_device)
    service._update_locked = update_locked
    service._verified = lambda _plugin_id: (
        object(),
        object(),
        SimpleNamespace(manifest=SimpleNamespace(plugin_version="1.0.0")),
    )
    service.plugin_registry.get = lambda _plugin_id: SimpleNamespace(
        selected_version="1.0.0"
    )
    service.install = lambda *_args, **_kwargs: update_entered.set()

    poll = asyncio.create_task(service.poll_connection("request-id"))
    await asyncio.wait_for(device_entered.wait(), timeout=1.0)
    update = asyncio.create_task(
        asyncio.to_thread(
            service.update,
            "office.bundle",
            "1.0.0",
            "confirmed",
        )
    )
    await asyncio.sleep(0.05)

    assert update_entered.is_set() is False

    release_device.set()
    await asyncio.gather(poll, update)
    assert update_entered.is_set() is True


@pytest.mark.anyio
@pytest.mark.parametrize("completion_kind", ("browser", "device"))
async def test_candidate_finalization_failure_uses_same_browser_and_device_rollback(
    completion_kind: str,
) -> None:
    service = object.__new__(PluginLifecycleService)
    candidate = ({}, {}, object(), object())
    service._pending_update = lambda *_args: object()
    service._pending_candidate_request = lambda _request_id: candidate
    service.plugin_registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(revision="a" * 64)
    )

    async def complete(*_args, **_kwargs) -> object:
        return object()

    service._connection_service = SimpleNamespace(
        complete_candidate=complete,
        complete_candidate_device=complete,
    )
    rollbacks: list[str] = []
    service._abort_pending_update = lambda: rollbacks.append("rollback")

    async def fail_finalization(*_args, **_kwargs) -> object:
        raise RuntimeError("post-promotion boundary")

    service._finish_candidate_preparation = fail_finalization

    with pytest.raises(RuntimeError, match="^post-promotion boundary$"):
        if completion_kind == "browser":
            await service._complete_connection_locked(
                "http://127.0.0.1/oauth/generic/callback?state=x",
                request_id="b" * 64,
            )
        else:
            await service._poll_connection_locked("b" * 64)

    assert rollbacks == ["rollback"]


def _install_catalog_plugin(service: PluginLifecycleService, plugin_id: str) -> None:
    review = service.review(plugin_id)
    service.install(plugin_id, confirmation=review.sha256)


@pytest.mark.anyio
async def test_uninstall_second_store_failure_restores_last_valid_registry_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches app records being lost when plugin-registry removal fails."""
    monkeypatch.delenv("OMEGA_OPERATOR_ACCOUNT_KEY", raising=False)
    service = PluginLifecycleService(state_root=tmp_path)
    # Uninstall delegates a disconnect before local removal (see
    # test_uninstall_delegates_disconnect_before_local_removal). A bare service
    # has no connection coordinator, and this test is about registry rollback,
    # not about connections.
    async def _disconnect(plugin_id: str, account_key: str) -> None:
        return None

    service._connection_coordinator = SimpleNamespace(disconnect=_disconnect)
    _install_catalog_plugin(service, "github.official")
    before_plugin = service.plugin_registry.get("github.official")
    before_app = service.app_registry.get("github.official.mcp")

    def fail_uninstall(*_args, **_kwargs):
        raise RuntimeError("simulated second-store failure")

    monkeypatch.setattr(service.plugin_registry, "uninstall", fail_uninstall)

    with pytest.raises(ValueError, match="^plugin_uninstall_incomplete$"):
        await service.uninstall("github.official")

    assert service.plugin_registry.get("github.official") == before_plugin
    assert service.app_registry.get("github.official.mcp") == before_app


@pytest.mark.anyio
async def test_restart_reconciles_partial_uninstall_back_to_installed_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches a durable partial uninstall becoming permanent after restart."""
    monkeypatch.delenv("OMEGA_OPERATOR_ACCOUNT_KEY", raising=False)
    service = PluginLifecycleService(state_root=tmp_path)
    # Uninstall delegates a disconnect before local removal (see
    # test_uninstall_delegates_disconnect_before_local_removal). A bare service
    # has no connection coordinator, and this test is about registry rollback,
    # not about connections.
    async def _disconnect(plugin_id: str, account_key: str) -> None:
        return None

    service._connection_coordinator = SimpleNamespace(disconnect=_disconnect)
    _install_catalog_plugin(service, "github.official")
    original_apply = service.app_registry.apply_exact

    def remove_then_block_restore(*, install_records, remove_records, expected_revision):
        if install_records:
            raise RuntimeError("simulated rollback interruption")
        return original_apply(
            install_records=install_records,
            remove_records=remove_records,
            expected_revision=expected_revision,
        )

    monkeypatch.setattr(service.app_registry, "apply_exact", remove_then_block_restore)
    monkeypatch.setattr(
        service.plugin_registry,
        "uninstall",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stop")),
    )

    with pytest.raises(ValueError, match="^plugin_uninstall_incomplete$"):
        await service.uninstall("github.official")

    assert service.plugin_registry.get("github.official").plugin_id == "github.official"
    with pytest.raises(Exception):
        service.app_registry.get("github.official.mcp")

    service.close()
    restarted = PluginLifecycleService(state_root=tmp_path)
    assert restarted.plugin_registry.get("github.official").plugin_id == "github.official"
    assert restarted.app_registry.get("github.official.mcp").app_id == "github.official.mcp"


def test_concurrent_uninstall_intent_admission_cannot_lose_an_accepted_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches split read/write locks accepting two intents but persisting only one."""
    service = PluginLifecycleService(state_root=tmp_path)
    _install_catalog_plugin(service, "github.official")
    _install_catalog_plugin(service, "google.workspace.drive")
    records = (
        service.plugin_registry.get("github.official"),
        service.plugin_registry.get("google.workspace.drive"),
    )
    original_write = service._write_uninstall_document
    both_read_empty = threading.Barrier(2)

    def delayed_write(document: dict[str, object]) -> None:
        both_read_empty.wait(timeout=5)
        original_write(document)

    monkeypatch.setattr(service, "_write_uninstall_document", delayed_write)
    accepted: list[str] = []
    rejected: list[str] = []

    def admit(record: InstalledPluginRecord) -> None:
        try:
            service._begin_uninstall_intent(record, ())
        except ValueError as exc:
            rejected.append(str(exc))
        else:
            accepted.append(record.plugin_id)

    threads = [threading.Thread(target=admit, args=(record,)) for record in records]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    operation = service._read_uninstall_document()["operation"]
    assert type(operation) is dict
    assert len(accepted) == 1
    assert rejected == ["plugin_uninstall_incomplete"]
    assert operation["plugin_id"] == accepted[0]


def _lifecycle_intent_replica(service: PluginLifecycleService) -> PluginLifecycleService:
    replica = object.__new__(PluginLifecycleService)
    replica._operation_state = McpStateStore(
        state_root=service._operation_state.state_root,
        document_path=service._operation_state.document_path,
        invalid_path_reason="plugin_lifecycle_operation_in_progress",
        max_document_bytes=1024 * 1024,
    )
    return replica


def _attach_lifecycle_operation_state(
    service: PluginLifecycleService,
    state_root: Path,
) -> None:
    service._operation_state = McpStateStore(
        state_root=state_root,
        document_path=state_root / "operation.json",
        invalid_path_reason="plugin_lifecycle_operation_unavailable",
        max_document_bytes=1024 * 1024,
    )


async def _completed_coroutine() -> None:
    return None


def test_long_lived_lifecycle_intent_admission_is_restart_and_conflict_safe(
    tmp_path: Path,
) -> None:
    """One accepted continuation survives requests and cannot be overwritten."""

    service = PluginLifecycleService(state_root=tmp_path)
    accepted = service._admit_lifecycle_operation(
        kind="update",
        plugin_id="plugin.alpha",
    )
    before = service._read_lifecycle_operation_document()
    restarted = _lifecycle_intent_replica(service)

    for kind, plugin_id in (
        ("update", "plugin.beta"),
        ("update", "plugin.alpha"),
        ("uninstall", "plugin.alpha"),
        ("install", "plugin.beta"),
    ):
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            restarted._admit_lifecycle_operation(kind=kind, plugin_id=plugin_id)
        assert restarted._read_lifecycle_operation_document() == before

    service._release_lifecycle_operation(accepted)
    uninstall = restarted._admit_lifecycle_operation(
        kind="uninstall",
        plugin_id="plugin.alpha",
    )
    with pytest.raises(
        ValueError,
        match="^plugin_lifecycle_operation_in_progress$",
    ):
        service._admit_lifecycle_operation(kind="update", plugin_id="plugin.beta")
    assert service._read_lifecycle_operation_document()["operation"] == uninstall

    restarted._release_lifecycle_operation(uninstall)
    replacement = service._admit_lifecycle_operation(
        kind="update",
        plugin_id="plugin.beta",
    )
    assert replacement["plugin_id"] == "plugin.beta"
    assert replacement["operation_id"] != accepted["operation_id"]


@pytest.mark.parametrize("kind", ("install", "update", "uninstall"))
def test_restart_releases_orphaned_lifecycle_admission(
    tmp_path: Path,
    kind: str,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    accepted = service._admit_lifecycle_operation(
        kind=kind,
        plugin_id="orphan.plugin",
    )
    assert service._read_lifecycle_operation_document()["operation"] == accepted
    service.close()

    restarted = PluginLifecycleService(state_root=tmp_path)

    assert restarted._read_lifecycle_operation_document()["operation"] is None
    replacement = restarted._admit_lifecycle_operation(
        kind="install",
        plugin_id="replacement.plugin",
    )
    assert replacement["plugin_id"] == "replacement.plugin"
    assert replacement["generation"] > accepted["generation"]


def test_concurrent_long_lived_intent_admission_accepts_exactly_one_identity(
    tmp_path: Path,
) -> None:
    """The durable CAS, not thread arrival order, chooses the sole accepted intent."""

    service = PluginLifecycleService(state_root=tmp_path)
    replica = _lifecycle_intent_replica(service)
    ready = threading.Barrier(2)
    accepted: list[dict[str, object]] = []
    rejected: list[str] = []

    def admit(target: PluginLifecycleService, plugin_id: str) -> None:
        ready.wait(timeout=5)
        try:
            intent = target._admit_lifecycle_operation(
                kind="update",
                plugin_id=plugin_id,
            )
        except ValueError as exc:
            rejected.append(str(exc))
        else:
            accepted.append(intent)

    threads = (
        threading.Thread(target=admit, args=(service, "plugin.alpha")),
        threading.Thread(target=admit, args=(replica, "plugin.beta")),
    )
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    durable = service._read_lifecycle_operation_document()["operation"]
    assert len(accepted) == 1
    assert rejected == ["plugin_lifecycle_operation_in_progress"]
    assert durable == accepted[0]


@pytest.mark.anyio
async def test_public_pending_update_owns_admission_until_exact_resolution(
    tmp_path: Path,
) -> None:
    """A 202 continuation excludes every later lifecycle mutation until resolved."""

    service = object.__new__(PluginLifecycleService)
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    service._operation_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "operation.json",
        invalid_path_reason="plugin_lifecycle_operation_unavailable",
        max_document_bytes=1024 * 1024,
    )
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    calls: list[tuple[str, str]] = []

    def update_locked(plugin_id: str, *, version: str, confirmation: object) -> object:
        calls.append(("update", plugin_id))
        assert version == "1.0.0"
        assert confirmation == "confirmed"
        return lifecycle_module.PluginUpdatePending(
            plugin_id=plugin_id,
            app_ids=("generic.app",),
            next_required_app_id="generic.app",
        )

    service._update_locked = update_locked
    service._install_locked = lambda plugin_id, **_kwargs: calls.append(
        ("install", plugin_id)
    )

    async def uninstall_locked(plugin_id: str) -> None:
        calls.append(("uninstall", plugin_id))

    service._uninstall_locked = uninstall_locked

    pending = service.update("plugin.alpha", "1.0.0", "confirmed")
    accepted = service._read_lifecycle_operation_document()["operation"]
    assert pending.next_required_app_id == "generic.app"
    assert type(accepted) is dict
    assert accepted["kind"] == "update"
    assert accepted["plugin_id"] == "plugin.alpha"

    with pytest.raises(
        ValueError,
        match="^plugin_lifecycle_operation_in_progress$",
    ):
        service.update("plugin.alpha", "1.0.0", "confirmed")
    with pytest.raises(
        ValueError,
        match="^plugin_lifecycle_operation_in_progress$",
    ):
        service.install("plugin.beta", confirmation="confirmed")
    with pytest.raises(
        ValueError,
        match="^plugin_lifecycle_operation_in_progress$",
    ):
        await service.uninstall("plugin.alpha")
    assert service._read_lifecycle_operation_document()["operation"] == accepted
    assert calls == [("update", "plugin.alpha")]

    service._release_lifecycle_operation(accepted)
    assert service._read_lifecycle_operation_document()["operation"] is None

    service.install("plugin.beta", confirmation="confirmed")
    assert calls[-1] == ("install", "plugin.beta")
    assert service._read_lifecycle_operation_document()["operation"] is None


def _binding(receipt_id: str) -> PluginAppBinding:
    material = {
        "required_app_ids": ["office.mail"],
        "optional_app_ids": [],
        "dependency_receipt_id": receipt_id,
    }
    return PluginAppBinding(
        required_app_ids=("office.mail",),
        optional_app_ids=(),
        dependency_receipt_id=receipt_id,
        revision=sha256_hex(canonical_bytes(material)),
    )


def _sealed_connection_recovery(
    base: InstalledPluginRecord,
    binding: PluginAppBinding,
) -> PluginUpdateConnectionRecovery:
    account = "a" * 64
    checkpoint_id = "plugin-update-recovery-" + "b" * 64
    predecessor_apps = (
        ()
        if base.app_binding is None
        else tuple(
            sorted(
                base.app_binding.required_app_ids + base.app_binding.optional_app_ids
            )
        )
    )
    candidate_apps = tuple(
        sorted(binding.required_app_ids + binding.optional_app_ids)
    )
    all_apps = tuple(sorted(set(predecessor_apps + candidate_apps)))
    grant_material = {
        "checkpoint_id": checkpoint_id,
        "vault_revision": "c" * 64,
        "plugin_id": base.plugin_id,
        "account_key_sha256": account,
        "app_ids": list(all_apps),
        "predecessor_grants": [],
        "permission_recovery": None,
    }
    grant_material["revision"] = sha256_hex(
        canonical_bytes(
            {
                "domain": "omega.plugin-grant-update-recovery.v1",
                "material": grant_material,
            }
        )
    )
    lineages = []
    for app_id in all_apps:
        lineage = {
            "plugin_id": base.plugin_id,
            "app_id": app_id,
            "account_key_hash": account,
            "connection_key": PluginAuthorityStore._connection_key(
                base.plugin_id,
                app_id,
                account,
            ),
            "connection": None,
            "authorities": {},
            "executions": {},
        }
        lineage["revision"] = sha256_hex(
            canonical_bytes(
                {
                    "domain": "omega.plugin-connection-lineage.v1",
                    "material": lineage,
                }
            )
        )
        lineages.append(lineage)
    material = {
        "checkpoint_id": checkpoint_id,
        "plugin_id": base.plugin_id,
        "account_key_sha256": account,
        "predecessor_app_ids": list(predecessor_apps),
        "candidate_app_ids": list(candidate_apps),
        "grant_recovery": grant_material,
        "authority_lineages": lineages,
        "live_sessions": [
            {
                "app_id": app_id,
                "had_live_session": False,
                "connection_revision": "",
            }
            for app_id in predecessor_apps
        ],
    }
    material["revision"] = sha256_hex(
        canonical_bytes(
            {
                "domain": "omega.plugin-connection-update-recovery.v1",
                "material": material,
            }
        )
    )
    return PluginUpdateConnectionRecovery.from_dict(material)


def _update_records() -> tuple[InstalledPluginRecord, InstalledPluginRecord]:
    old_version = InstalledPluginVersionRecord(
        plugin_version="1.0.0",
        signing_key_id="test.key",
        manifest_sha256="1" * 64,
        package_sha256="2" * 64,
        state="selected",
    )
    candidate = InstalledPluginVersionRecord(
        plugin_version="2.0.0",
        signing_key_id="test.key",
        manifest_sha256="3" * 64,
        package_sha256="4" * 64,
        state="candidate",
    )
    old = InstalledPluginRecord(
        plugin_id="office.bundle",
        publisher_id="test.publisher",
        kind="mcp_backed",
        selected_version="1.0.0",
        enabled=True,
        status="installed",
        blocker=None,
        versions=(old_version,),
        app_binding=_binding("old-receipt"),
    )
    published = InstalledPluginRecord(
        **{
            **old.__dict__,
            "versions": (old_version, candidate),
        }
    )
    return old, published


def test_stale_update_cleanup_cannot_clear_a_new_same_plugin_operation(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    old, _published = _update_records()

    first = service._admit_lifecycle_operation(
        kind="update",
        plugin_id=old.plugin_id,
    )
    first_update = {
        "plugin_id": old.plugin_id,
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "receipt-first",
        "phase": "installing",
        "lifecycle_operation_id": first["operation_id"],
    }
    service._write_update_document(service._update_document(first_update))

    # Model resolution of the first operation, followed by admission of an
    # indistinguishable same-kind/same-plugin operation with a new identity.
    service._write_update_document(service._empty_update_document())
    service._release_lifecycle_operation(first)
    second = service._admit_lifecycle_operation(
        kind="update",
        plugin_id=old.plugin_id,
    )
    second_update = {
        **first_update,
        "dependency_receipt_id": "receipt-second",
        "lifecycle_operation_id": second["operation_id"],
    }
    service._write_update_document(service._update_document(second_update))

    with pytest.raises(
        ValueError,
        match="^plugin_lifecycle_operation_conflict$",
    ):
        service._clear_update_document(first_update)

    assert service._read_update_document()["operation"] == second_update
    assert service._read_lifecycle_operation_document()["operation"] == second


@pytest.mark.parametrize("binding", (None, "f" * 64), ids=("unbound", "mismatched"))
def test_update_recovery_rejects_owner_binding_before_any_mutation(
    tmp_path: Path,
    binding: str | None,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    old, _published = _update_records()
    accepted = service._admit_lifecycle_operation(
        kind="update",
        plugin_id=old.plugin_id,
    )
    operation = {
        "plugin_id": old.plugin_id,
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "untrusted-receipt",
        "phase": "installing",
    }
    if binding is not None:
        operation["lifecycle_operation_id"] = binding
    payload = canonical_bytes(service._update_document(operation))
    with service._update_state.locked():
        service._update_state.write_bytes(payload)
    mutations: list[str] = []
    service._installer = SimpleNamespace(recover=lambda: mutations.append("recover"))

    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        service._reconcile_update_intent()

    assert mutations == []
    with service._update_state.locked():
        assert service._update_state.read_bytes() == payload
    assert service._read_lifecycle_operation_document()["operation"] == accepted
    with service._update_state.locked():
        service._update_state.write_bytes(canonical_bytes(service._empty_update_document()))
    service._release_lifecycle_operation(accepted)
    service.close()


@pytest.mark.parametrize(
    "shape",
    ("pre-settings-sealed", "pre-settings-unsealed", "tampered-seal"),
)
def test_pre_settings_update_document_is_refused_like_any_unrecoverable_document(
    tmp_path: Path,
    shape: str,
) -> None:
    """A candidate app recorded before per-app consent carried no ``settings``.

    That shape is gone: it is refused through the same path as any other
    unrecoverable update document (``plugin_update_recovery_required`` before
    any recovery mutation), never decoded into a candidate without consent.
    ``tampered-seal`` is a current-shape document with a broken seal: the
    refusal every unrecoverable document already gets.
    """

    old, _published = _update_records()
    binding = _binding("new-receipt")
    app: dict[str, object] = {
        "app_id": "office.mail",
        "state": "authorizing",
        "request_id": "a" * 64,
        "connection_revision": None,
        "activation": None,
    }
    if shape == "tampered-seal":
        app["settings"] = _consent(scopes=("mail.read",))
    operation: dict[str, object] = {
        "plugin_id": "office.bundle",
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "new-receipt",
        "phase": "candidate",
        "candidate_apps": [app],
    }
    if shape != "pre-settings-unsealed":
        identity = {
            "plugin_id": "office.bundle",
            "target_version": "2.0.0",
            "target_signing_key_id": "test.key",
            "target_manifest_sha256": "3" * 64,
            "target_package_sha256": "4" * 64,
            "candidate_binding_revision": binding.revision,
        }
        recovery = _sealed_connection_recovery(old, binding).to_dict()
        # Sealed exactly as it was written: over the apps as recorded.
        material = {
            "plugin_id": "office.bundle",
            "base_record": old.to_dict(),
            "dependency_receipt_id": "new-receipt",
            "phase": "candidate",
            "candidate_apps": [app],
            "candidate_binding": binding.to_dict(),
            "candidate_identity": identity,
            "connection_recovery": recovery,
        }
        seal = sha256_hex(
            canonical_bytes(
                {"domain": "omega.plugin-update-recovery.v1", "material": material}
            )
        )
        operation.update(
            {
                "candidate_binding": binding.to_dict(),
                "candidate_identity": identity,
                "connection_recovery": recovery,
                "recovery_sha256": "f" * 64 if shape == "tampered-seal" else seal,
            }
        )
    document = {
        "schema_version": 1 if shape == "pre-settings-unsealed" else 2,
        "operation": operation,
    }
    payload = canonical_bytes(document)
    mutations: list[str] = []
    service = object.__new__(PluginLifecycleService)
    service._installer = SimpleNamespace(recover=lambda: mutations.append("recover"))
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    with service._update_state.locked():
        service._update_state.write_bytes(payload)

    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        service._read_update_document()
    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        service._reconcile_update_intent()
    with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
        service._write_update_document(document)
    if shape != "tampered-seal":
        # Nor can the old shape be written from memory: consent is not optional.
        with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
            service._update_document(operation)

    assert mutations == []
    with service._update_state.locked():
        assert service._update_state.read_bytes() == payload


def test_closed_owner_cannot_write_after_a_new_generation_takes_the_lease(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    old, _published = _update_records()
    accepted = service._admit_lifecycle_operation(
        kind="update",
        plugin_id=old.plugin_id,
    )
    operation = {
        "plugin_id": old.plugin_id,
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "receipt-before-close",
        "phase": "installing",
        "lifecycle_operation_id": accepted["operation_id"],
    }
    service._write_update_document(service._update_document(operation))
    with service._update_state.locked():
        before = service._update_state.read_bytes()
    service.close()

    successor = PluginLifecycleService.__new__(PluginLifecycleService)
    successor._configure_lifecycle_owner_state(tmp_path / "plugins")
    assert successor._claim_lifecycle_operation_ownership() is True
    replacement = successor._read_lifecycle_operation_document()["operation"]
    assert replacement["generation"] == accepted["generation"] + 1

    with pytest.raises(
        ValueError,
        match="^plugin_lifecycle_operation_conflict$",
    ):
        service._write_update_document(
            service._update_document(
                {**operation, "dependency_receipt_id": "stale-write"}
            )
        )

    with service._update_state.locked():
        assert service._update_state.read_bytes() == before
    assert successor._read_lifecycle_operation_document()["operation"] == replacement
    successor.close()


def test_stale_uninstall_cleanup_cannot_clear_a_new_same_plugin_operation(
    tmp_path: Path,
) -> None:
    service = PluginLifecycleService(state_root=tmp_path)
    old, _published = _update_records()

    first = service._admit_lifecycle_operation(
        kind="uninstall",
        plugin_id=old.plugin_id,
    )
    first_intent = service._begin_uninstall_intent(
        old,
        (),
        lifecycle_operation_id=str(first["operation_id"]),
    )
    service._write_uninstall_document(service._empty_uninstall_document())
    service._release_lifecycle_operation(first)

    second = service._admit_lifecycle_operation(
        kind="uninstall",
        plugin_id=old.plugin_id,
    )
    second_intent = service._begin_uninstall_intent(
        old,
        (),
        lifecycle_operation_id=str(second["operation_id"]),
    )

    with pytest.raises(
        ValueError,
        match="^plugin_lifecycle_operation_conflict$",
    ):
        service._clear_uninstall_intent(first_intent)

    assert service._read_uninstall_document() == second_intent
    assert service._read_lifecycle_operation_document()["operation"] == second


class _UpdateRegistry:
    def __init__(self, old: InstalledPluginRecord, published: InstalledPluginRecord) -> None:
        self.old = old
        self.record = old
        self.events: list[str] = []
        self.fail_promotion = False

    def get(self, _plugin_id: str) -> InstalledPluginRecord:
        return self.record

    def snapshot(self) -> PluginRegistrySnapshot:
        return PluginRegistrySnapshot((self.record,), "b" * 64)

    def promote_candidate(self, *args, **kwargs) -> PluginMutationResult:
        self.events.append("promote")
        if self.fail_promotion:
            raise RuntimeError("promotion failed")
        target_version = str(args[1])
        app_binding = kwargs.get("app_binding")
        selected = tuple(
            InstalledPluginVersionRecord(
                **{
                    **item.__dict__,
                    "state": (
                        "selected"
                        if item.plugin_version == target_version
                        else "candidate"
                    ),
                }
            )
            for item in self.record.versions
        )
        self.record = InstalledPluginRecord(
            **{
                **self.record.__dict__,
                "selected_version": target_version,
                "versions": selected,
                "app_binding": (
                    _binding("new-receipt")
                    if app_binding is None
                    else app_binding
                ),
            }
        )
        return PluginMutationResult(self.record, "c" * 64)

    def discard_candidate(self, *_args, **_kwargs) -> PluginMutationResult:
        self.events.append("discard")
        self.record = self.old
        return PluginMutationResult(self.record, "d" * 64)


class _UpdateInstaller:
    def __init__(self, published: InstalledPluginRecord, registry: _UpdateRegistry) -> None:
        self.published = published
        self.registry = registry
        self.events: list[str] = []
        self.provisioner = SimpleNamespace(rollback=self._rollback)

    def install_from_catalog(self, *_args, **kwargs) -> PluginInstallResult:
        self.events.append(f"install:{kwargs.get('preserve_existing_binding')}")
        self.registry.record = self.published
        return PluginInstallResult(
            transaction_id=str(kwargs["transaction_id"]),
            review_sha256="5" * 64,
            record=self.published,
            dependency_receipt=DependencyCommitReceipt(
                receipt_id="new-receipt",
                prepared_id="new-prepared",
                introduced_app_ids=(),
            ),
            revision="b" * 64,
        )

    def _rollback(self, _receipt_id: str) -> None:
        self.events.append("rollback")

    def recover(self) -> None:
        return None


@pytest.mark.parametrize("failure", ("connection", "compile", "promotion"))
def test_enabled_update_preserves_old_selected_authority_on_readiness_failure(
    tmp_path: Path,
    failure: str,
) -> None:
    """Catches candidate promotion preceding connection/compile authority readiness."""
    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    installer = _UpdateInstaller(published, registry)
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service.app_registry = SimpleNamespace(get=lambda _app_id: object())
    service._installer = installer
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    _attach_lifecycle_operation_state(service, tmp_path)
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id="office.bundle",
            plugin_version="2.0.0",
            signing_key_id="test.key",
            required_apps=(
                SimpleNamespace(
                    app_id="office.mail",
                    authentication=SimpleNamespace(kind=SimpleNamespace(value="none")),
                ),
            ),
            optional_apps=(),
        ),
        manifest_sha256="3" * 64,
        package_sha256="4" * 64,
    )
    service._verified = lambda _plugin_id: (object(), object(), verified)
    async def probe_candidate(*_args, **_kwargs) -> object:
        registry.events.append("ready")
        if failure == "connection":
            raise ValueError("connection failed")
        return SimpleNamespace(connection_revision="6" * 64, status="connected")

    def compile_candidate(*_args, **_kwargs) -> None:
        if failure == "compile":
            raise ValueError("compile failed")

    async def status(_plugin_id: str, *, app_id: str, account_key: str) -> object:
        # The credentialless app is live; its consent carries into the candidate.
        return _live_status(app_id)

    service._connection_service = SimpleNamespace(
        status=status,
        _candidate_definition=_candidate_definition(),
        probe_candidate=probe_candidate,
        discard_update_recovery_checkpoint=lambda *_args, **_kwargs: None,
    )
    service._app_ids = lambda _plugin_id: old.app_binding.required_app_ids
    service._submit_connection_operation = lambda operation: asyncio.run(operation())
    service.operator_account_key = lambda: "operator"
    service._assert_candidate_bindings = compile_candidate
    service._assert_plugin_compiled = lambda _plugin_id: None
    registry.fail_promotion = failure == "promotion"

    with pytest.raises(ValueError, match="^plugin_update_failed$"):
        service.update("office.bundle", "2.0.0", "confirmed")

    assert registry.record == old
    assert registry.record.enabled is True
    assert registry.record.selected_version == "1.0.0"
    assert registry.events[:1] == ["ready"]
    assert registry.events[-1] == "discard"
    assert installer.events == ["install:True", "rollback"]


def test_restart_discards_exact_unpromoted_update_and_preserves_old_selected(
    tmp_path: Path,
) -> None:
    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    installer = _UpdateInstaller(published, registry)
    registry.record = published
    installer.recover = lambda: installer.events.append("recover")
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service._installer = installer
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    service._write_update_document(
        {
            "schema_version": 1,
            "operation": {
                "plugin_id": "office.bundle",
                "target_version": "2.0.0",
                "target_signing_key_id": "test.key",
                "target_manifest_sha256": "3" * 64,
                "target_package_sha256": "4" * 64,
                "base_record": old.to_dict(),
                "dependency_receipt_id": "new-receipt",
                "phase": "candidate",
            },
        }
    )

    service._reconcile_update_intent()

    assert registry.record == old
    assert installer.events == ["recover", "rollback"]
    assert service._read_update_document()["operation"] is None


def test_restart_cancels_durable_candidate_oauth_and_restores_old_selection(
    tmp_path: Path,
) -> None:
    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    registry.record = published
    installer = _UpdateInstaller(published, registry)
    cancelled: list[str] = []
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service.app_registry = SimpleNamespace()
    service._installer = installer
    service._connection_service = SimpleNamespace(
        cancel_candidate=lambda request_id, **_kwargs: cancelled.append(request_id)
    )
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id="office.bundle",
            plugin_version="2.0.0",
            signing_key_id="test.key",
            required_apps=(SimpleNamespace(app_id="office.mail"),),
            optional_apps=(),
        ),
        manifest_sha256="3" * 64,
        package_sha256="4" * 64,
    )
    service._verified = lambda _plugin_id: (object(), object(), verified)
    service._write_update_document(
        {
            "schema_version": 1,
            "operation": {
                "plugin_id": "office.bundle",
                "target_version": "2.0.0",
                "target_signing_key_id": "test.key",
                "target_manifest_sha256": "3" * 64,
                "target_package_sha256": "4" * 64,
                "base_record": old.to_dict(),
                "dependency_receipt_id": "new-receipt",
                "phase": "candidate",
                "candidate_apps": [
                    {
                        "app_id": "office.mail",
                        "state": "authorizing",
                        "request_id": "a" * 64,
                        "connection_revision": None,
                        "activation": None,
                        "settings": _consent(scopes=("mail.read",)),
                    }
                ],
            },
        }
    )

    service._reconcile_update_intent()

    assert cancelled == ["a" * 64]
    assert registry.record == old
    assert installer.events == ["rollback"]
    assert service._read_update_document()["operation"] is None


@pytest.mark.parametrize("attempt_error", [None, "oauth_transaction_expired", "oauth_transaction_binding_drift"])
def test_restart_cleanup_uses_only_sealed_local_candidate_identity(
    tmp_path: Path,
    attempt_error: str | None,
) -> None:
    """Catalog withdrawal cannot strand an accepted candidate or its OAuth attempt."""

    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    registry.record = published
    installer = _UpdateInstaller(published, registry)
    binding = _binding("new-receipt")
    connection_recovery = _sealed_connection_recovery(old, binding)
    candidate_identity = {
        "plugin_id": "office.bundle",
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "candidate_binding_revision": binding.revision,
    }
    recovery_material = {
        "plugin_id": "office.bundle",
        "base_record": old.to_dict(),
        "dependency_receipt_id": "new-receipt",
        "phase": "candidate",
        "candidate_apps": [
            {
                "app_id": "office.mail",
                "state": "authorizing",
                "request_id": "a" * 64,
                "connection_revision": None,
                "activation": None,
                # The consent recorded when this candidate app's sign-in began.
                "settings": _consent(scopes=("mail.read",)),
            }
        ],
        "candidate_binding": binding.to_dict(),
        "candidate_identity": candidate_identity,
        "connection_recovery": connection_recovery.to_dict(),
    }
    recovery_sha256 = sha256_hex(
        canonical_bytes(
            {
                "domain": "omega.plugin-update-recovery.v1",
                "material": recovery_material,
            }
        )
    )
    cancelled: list[tuple[str, dict[str, object]]] = []
    enumerated: list[dict[str, object]] = []
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service._installer = installer

    def cancel_exact(request_id, *, candidate_identity):
        cancelled.append((request_id, dict(candidate_identity)))
        if attempt_error is not None:
            raise ValueError(attempt_error)

    service._connection_service = SimpleNamespace(
        cancel_candidate_exact=cancel_exact,
        cancel_candidate_attempts_exact=lambda identity: enumerated.append(dict(identity)),
        restore_update_recovery=lambda _recovery: _completed_coroutine(),
        finalize_update_recovery=lambda _recovery: _completed_coroutine(),
    )
    service._submit_connection_operation = lambda operation: asyncio.run(operation())
    service._verified = lambda _plugin_id: (_ for _ in ()).throw(
        AssertionError("cleanup must not resolve the live catalog")
    )
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    service._write_update_document(
        {
            "schema_version": 2,
            "operation": {
                "plugin_id": "office.bundle",
                "target_version": "2.0.0",
                "target_signing_key_id": "test.key",
                "target_manifest_sha256": "3" * 64,
                "target_package_sha256": "4" * 64,
                "base_record": old.to_dict(),
                "dependency_receipt_id": "new-receipt",
                "phase": "candidate",
                "candidate_apps": [
                    {
                        "app_id": "office.mail",
                        "state": "authorizing",
                        "request_id": "a" * 64,
                        "connection_revision": None,
                        "activation": None,
                        "settings": _consent(scopes=("mail.read",)),
                    }
                ],
                "candidate_binding": binding.to_dict(),
                "candidate_identity": candidate_identity,
                "connection_recovery": connection_recovery.to_dict(),
                "recovery_sha256": recovery_sha256,
            },
        }
    )

    if attempt_error == "oauth_transaction_binding_drift":
        with pytest.raises(ValueError, match="^plugin_update_recovery_required$"):
            service._reconcile_update_intent()
        assert registry.record == published
        assert installer.events == []
        assert service._read_update_document()["operation"] is not None
        return

    service._reconcile_update_intent()

    assert cancelled == [("a" * 64, candidate_identity)]
    assert enumerated == [candidate_identity]
    assert registry.record == old
    assert installer.events == ["rollback"]
    assert service._read_update_document()["operation"] is None


@pytest.mark.anyio
async def test_pending_update_continues_from_local_candidate_when_catalog_is_offline(
    tmp_path: Path,
) -> None:
    """A 202 continuation re-verifies local signed bytes, never the live catalog."""

    old, published = _update_records()
    binding = _binding("new-receipt")
    registry = _UpdateRegistry(old, published)
    registry.record = published
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id="office.bundle",
            plugin_version="2.0.0",
            signing_key_id="test.key",
            required_apps=(SimpleNamespace(app_id="office.mail"),),
            optional_apps=(),
        ),
        manifest_sha256="3" * 64,
        package_sha256="4" * 64,
    )
    expected_candidate = published.versions[-1]

    def reverify_candidate(plugin_id: str, candidate: object) -> object:
        assert plugin_id == "office.bundle"
        assert candidate == expected_candidate
        return verified

    registry.reverify_candidate = reverify_candidate
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    service._lifecycle_revision_snapshot = lambda: SimpleNamespace(revision="stable")
    service.operator_account_key = lambda: "operator"
    service._app_ids = lambda _plugin_id: old.app_binding.required_app_ids
    service._verified = lambda _plugin_id: (_ for _ in ()).throw(
        AssertionError("continuation must not resolve the live catalog")
    )

    async def start_candidate(*_args, **_kwargs) -> object:
        return SimpleNamespace(request_id="a" * 64)

    async def status(_plugin_id: str, *, app_id: str, account_key: str) -> object:
        return _live_status(app_id, scopes=("mail.read",))

    service._connection_service = SimpleNamespace(
        start_candidate=start_candidate,
        status=status,
        _candidate_definition=_candidate_definition(scopes=("mail.read",)),
    )
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    operation = {
        "plugin_id": "office.bundle",
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "new-receipt",
        "phase": "candidate",
        "candidate_apps": [
            {
                "app_id": "office.mail",
                "state": "setup_required",
                "request_id": None,
                "connection_revision": None,
                "activation": None,
                "settings": None,
            }
        ],
    }
    service._seal_update_recovery(
        operation,
        binding,
        _sealed_connection_recovery(old, binding),
    )
    service._write_update_document(service._update_document(operation))

    selection = await service.permissions("office.bundle", "office.mail")
    settings = full_permission_settings(selection)
    started = await service.start_connection(
        "office.bundle",
        app_id="office.mail",
        revision=selection["revision"],
        settings=settings,
    )

    assert started.request_id == "a" * 64
    durable = service._read_update_document()["operation"]
    assert durable["candidate_apps"][0]["state"] == "authorizing"
    assert durable["candidate_apps"][0]["request_id"] == "a" * 64
    assert durable["candidate_apps"][0]["settings"] == settings


def test_authenticated_update_returns_server_held_pending_without_promotion(
    tmp_path: Path,
) -> None:
    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    installer = _UpdateInstaller(published, registry)
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service.app_registry = SimpleNamespace()
    service._installer = installer
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    _attach_lifecycle_operation_state(service, tmp_path)
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id="office.bundle",
            plugin_version="2.0.0",
            signing_key_id="test.key",
            required_apps=(
                SimpleNamespace(
                    app_id="office.mail",
                    authentication=SimpleNamespace(
                        kind=SimpleNamespace(value="oauth_authorization_code_pkce")
                    ),
                ),
            ),
            optional_apps=(),
        ),
        manifest_sha256="3" * 64,
        package_sha256="4" * 64,
    )
    service._verified = lambda _plugin_id: (object(), object(), verified)
    recovery = _sealed_connection_recovery(old, _binding("new-receipt"))
    service._connection_service = SimpleNamespace(
        begin_update_recovery=lambda **_kwargs: recovery
    )
    service.operator_account_key = lambda: "operator"

    result = service.update("office.bundle", "2.0.0", "confirmed")

    assert result == lifecycle_module.PluginUpdatePending(
        plugin_id="office.bundle",
        app_ids=("office.mail",),
        next_required_app_id="office.mail",
    )
    assert registry.record == published
    assert registry.record.selected_version == "1.0.0"
    assert registry.events == []
    operation = service._read_update_document()["operation"]
    assert type(operation) is dict
    assert operation["candidate_binding"] == _binding("new-receipt").to_dict()
    assert operation["candidate_identity"] == {
        "plugin_id": "office.bundle",
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "candidate_binding_revision": _binding("new-receipt").revision,
    }
    assert operation["connection_recovery"] == recovery.to_dict()
    assert len(str(operation["recovery_sha256"])) == 64
    # No consent is recorded until the operator selects this app's settings.
    assert operation["candidate_apps"] == [
        {
            "app_id": "office.mail",
            "state": "setup_required",
            "request_id": None,
            "connection_revision": None,
            "activation": None,
            "settings": None,
        }
    ]


def test_restart_rolls_back_dependencies_when_candidate_published_before_phase_write(
    tmp_path: Path,
) -> None:
    """Catches `installing` journal state orphaning dependencies of a visible candidate."""
    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    installer = _UpdateInstaller(published, registry)
    registry.record = published
    installer.recover = lambda: installer.events.append("recover")
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service._installer = installer
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    _attach_lifecycle_operation_state(service, tmp_path)
    accepted = service._admit_lifecycle_operation(
        kind="update",
        plugin_id="office.bundle",
    )
    discarded_checkpoints: list[tuple[str, str, str]] = []
    service._connection_service = SimpleNamespace(
        discard_update_recovery_checkpoint=(
            lambda checkpoint_id, *, plugin_id, account_key: (
                discarded_checkpoints.append(
                    (checkpoint_id, plugin_id, account_key)
                )
            )
        )
    )
    service.operator_account_key = lambda: "operator"
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    service._write_update_document(
        {
            "schema_version": 1,
            "operation": {
                "plugin_id": "office.bundle",
                "target_version": "2.0.0",
                "target_signing_key_id": "test.key",
                "target_manifest_sha256": "3" * 64,
                "target_package_sha256": "4" * 64,
                "base_record": old.to_dict(),
                "dependency_receipt_id": "new-receipt",
                "phase": "installing",
                "lifecycle_operation_id": accepted["operation_id"],
            },
        }
    )

    service._reconcile_update_intent()

    assert registry.record == old
    assert installer.events == ["recover", "rollback"]
    assert discarded_checkpoints == [
        (
            "plugin-update-recovery-" + str(accepted["operation_id"]),
            "office.bundle",
            "operator",
        )
    ]
    assert service._read_update_document()["operation"] is None
    assert service._read_lifecycle_operation_document()["operation"] is None


@pytest.mark.anyio
@pytest.mark.parametrize("mutation", ("permissions", "disconnect"))
async def test_pending_update_rejects_stale_predecessor_authority_mutation(
    tmp_path: Path,
    mutation: str,
) -> None:
    """The predecessor checkpoint stays exact throughout the 202 continuation.

    A disconnect is refused outright. A permission write is the candidate app's
    consent: it is recorded on the candidate and never reaches predecessor
    authority.
    """

    old, published = _update_records()
    binding = _binding("new-receipt")
    recovery = _sealed_connection_recovery(old, binding)
    registry = _UpdateRegistry(old, published)
    registry.record = published
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id="office.bundle",
            plugin_version="2.0.0",
            signing_key_id="test.key",
            required_apps=(SimpleNamespace(app_id="office.mail"),),
            optional_apps=(),
        ),
        manifest_sha256="3" * 64,
        package_sha256="4" * 64,
    )
    registry.reverify_candidate = lambda _plugin_id, _candidate: verified
    predecessor_starts: list[object] = []

    async def status(_plugin_id: str, *, app_id: str, account_key: str) -> object:
        return _live_status(app_id, scopes=("mail.read",))

    async def start_candidate(*_args, **_kwargs) -> object:
        return SimpleNamespace(request_id="a" * 64)

    async def start_predecessor(*args, **kwargs) -> object:
        predecessor_starts.append((args, kwargs))
        return SimpleNamespace(status="connected")

    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service.permission_store = None
    service._lifecycle_revision_snapshot = lambda: SimpleNamespace(revision="a" * 64)
    service.operator_account_key = lambda: "operator"
    service._app_ids = lambda _plugin_id: old.app_binding.required_app_ids
    service._connection_service = SimpleNamespace(
        status=status,
        _candidate_definition=_candidate_definition(scopes=("mail.read",)),
        start_candidate=start_candidate,
        start=start_predecessor,
    )
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    operation = {
        "plugin_id": "office.bundle",
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "new-receipt",
        "phase": "candidate",
        "candidate_apps": [
            {
                "app_id": "office.mail",
                "state": "setup_required",
                "request_id": None,
                "connection_revision": None,
                "activation": None,
                "settings": None,
            }
        ],
    }
    service._seal_update_recovery(operation, binding, recovery)
    service._write_update_document(service._update_document(operation))

    if mutation == "disconnect":
        with pytest.raises(
            ValueError,
            match="^plugin_lifecycle_operation_in_progress$",
        ):
            await service._disconnect_locked("office.bundle")
        return

    selection = await service.permissions("office.bundle", "office.mail")
    assert selection["candidate"] is True
    settings = full_permission_settings(selection)
    await service._set_permissions_locked(
        "office.bundle",
        revision=selection["revision"],
        settings=settings,
        app_id="office.mail",
    )

    assert predecessor_starts == []
    durable = service._read_update_document()["operation"]
    assert durable["connection_recovery"] == recovery.to_dict()
    assert durable["candidate_apps"][0]["state"] == "authorizing"
    assert durable["candidate_apps"][0]["settings"] == settings


@pytest.mark.parametrize(
    "fault_phase",
    (
        "cutover",
        "plugin_promoted",
        "apps_promoted",
        "activating",
        "activated",
        "compiled",
        "committed",
        "retirement_complete",
    ),
)
@pytest.mark.parametrize(
    "candidate_app_ids",
    (("office.mail",), ("office.calendar", "office.mail")),
    ids=("changed-app", "added-app"),
)
def test_every_post_promotion_boundary_restores_predecessor_authority(
    tmp_path: Path,
    fault_phase: str,
    candidate_app_ids: tuple[str, ...],
) -> None:
    """Every durable pre-commit phase rolls back grant/session/authority and records."""

    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    registry.record = published
    registry.rollback_update_candidate = lambda **_kwargs: (
        setattr(registry, "record", old)
        or PluginMutationResult(old, "d" * 64)
    )
    installer = _UpdateInstaller(published, registry)
    installer.provisioner = SimpleNamespace(
        promote_candidate_records=lambda *_args, **_kwargs: "e" * 64,
        rollback=installer._rollback,
    )
    binding_material = {
        "required_app_ids": list(candidate_app_ids),
        "optional_app_ids": [],
        "dependency_receipt_id": "new-receipt",
    }
    binding = PluginAppBinding(
        required_app_ids=candidate_app_ids,
        optional_app_ids=(),
        dependency_receipt_id="new-receipt",
        revision=sha256_hex(canonical_bytes(binding_material)),
    )
    recovery = _sealed_connection_recovery(old, binding)
    connection_events: list[str] = []

    async def connected(*_args, **consent) -> object:
        connection_events.append("candidate_active")
        # A started connection reports the exact consent it was started under.
        return SimpleNamespace(
            status="connected",
            provider_scopes=consent["scopes"],
            capability_ceiling=consent["capability_ceiling"],
            execution_policy=PluginExecutionPolicy(consent["execution_policy"]),
        )

    async def restore(_recovery: object) -> None:
        connection_events.append("predecessor_restored")

    async def finalize(_recovery: object) -> None:
        connection_events.append("checkpoint_finalized")

    async def retire(_recovery: object, *, app_id: str, **_kwargs: object) -> None:
        connection_events.append("predecessor_retired:" + app_id)

    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service.app_registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(revision="f" * 64)
    )
    service._installer = installer
    service._connection_service = SimpleNamespace(
        begin_update_cutover=lambda _recovery: connection_events.append("cutover"),
        start=connected,
        restore_update_recovery=restore,
        retire_predecessor_for_update=retire,
        finalize_update_recovery=finalize,
        cancel_candidate_attempts_exact=lambda _identity: None,
    )
    service._submit_connection_operation = lambda operation: asyncio.run(operation())
    service.operator_account_key = lambda: "operator"
    service._assert_candidate_bindings = lambda *_args, **_kwargs: None
    service._assert_plugin_compiled = lambda _plugin_id: None
    service.descriptor_for_plugin_app = lambda _plugin_id, app_id: _app_descriptor(app_id)
    service._offered_provider_scopes = lambda _plugin_id, _app_id: ()
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    operation = {
        "plugin_id": "office.bundle",
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "new-receipt",
        "phase": "candidate",
        "candidate_apps": [
            {
                "app_id": app_id,
                "state": "ready",
                "request_id": None,
                "connection_revision": "6" * 64,
                "activation": None,
                # Each credentialless candidate app became ready under the
                # consent its setup recorded (start_connection / set_permissions).
                "settings": _consent(),
            }
            for app_id in candidate_app_ids
        ],
    }
    service._seal_update_recovery(operation, binding, recovery)
    service._write_update_document(service._update_document(operation))
    original_write = service._write_update_document
    injected = False

    def faulting_write(document: dict[str, object]) -> None:
        nonlocal injected
        original_write(document)
        durable_operation = document["operation"]
        if (
            not injected
            and type(durable_operation) is dict
            and durable_operation.get("phase") == fault_phase
        ):
            injected = True
            raise RuntimeError("fault after durable boundary")

    service._write_update_document = faulting_write
    verified = SimpleNamespace(
        manifest=SimpleNamespace(plugin_version="2.0.0"),
    )

    with pytest.raises(RuntimeError, match="^fault after durable boundary$"):
        asyncio.run(service._promote_update_candidate(operation, verified, binding))
    service._reconcile_update_intent()

    assert injected is True
    if fault_phase in {"committed", "retirement_complete"}:
        assert registry.record.selected_version == "2.0.0"
        assert "predecessor_restored" not in connection_events
        assert connection_events[-1] == "checkpoint_finalized"
        assert "rollback" not in installer.events
    else:
        assert registry.record == old
        assert connection_events[-2:] == [
            "predecessor_restored",
            "checkpoint_finalized",
        ]
        assert installer.events[-1] == "rollback"
    assert service._read_update_document()["operation"] is None


def test_restart_after_rollback_checkpoint_finalization_only_retries_finalize(
    tmp_path: Path,
) -> None:
    """A durable completed rollback must not require its consumed checkpoint."""

    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    installer = _UpdateInstaller(published, registry)
    binding = _binding("new-receipt")
    recovery = _sealed_connection_recovery(old, binding)
    events: list[str] = []
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service._installer = installer
    service._connection_service = SimpleNamespace(
        restore_update_recovery=lambda _recovery: (_ for _ in ()).throw(
            AssertionError("completed rollback cannot restore a consumed checkpoint")
        ),
        finalize_update_recovery=lambda _recovery: events.append("finalize"),
        cancel_candidate_attempts_exact=lambda _identity: None,
    )
    service._submit_connection_operation = lambda operation: operation()
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    operation = {
        "plugin_id": "office.bundle",
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "new-receipt",
        "phase": "rolled_back",
        "candidate_apps": [
            {
                "app_id": "office.mail",
                "state": "active",
                "request_id": None,
                "connection_revision": "6" * 64,
                "activation": None,
                "settings": _consent(),
            }
        ],
    }
    service._seal_update_recovery(operation, binding, recovery)
    service._write_update_document(service._update_document(operation))

    service._reconcile_update_intent()

    assert events == ["finalize"]
    assert installer.events == []
    assert registry.record == old
    assert service._read_update_document()["operation"] is None


@pytest.mark.parametrize(
    "provider_error",
    (
        "oauth_provider_rejected",
        "oauth_provider_timeout",
        "oauth_provider_unreachable",
    ),
)
def test_committed_update_retries_removed_app_retirement_before_checkpoint_finalize(
    tmp_path: Path,
    provider_error: str,
) -> None:
    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    registry.record = published
    binding_material = {
        "required_app_ids": ["office.calendar"],
        "optional_app_ids": [],
        "dependency_receipt_id": "new-receipt",
    }
    binding = PluginAppBinding(
        required_app_ids=("office.calendar",),
        optional_app_ids=(),
        dependency_receipt_id="new-receipt",
        revision=sha256_hex(canonical_bytes(binding_material)),
    )
    promoted = registry.promote_candidate(
        "office.bundle",
        "2.0.0",
        expected_revision="b" * 64,
        app_binding=binding,
    )
    recovery = _sealed_connection_recovery(old, binding)
    events: list[str] = []
    fail_retirement = True

    async def retire(_recovery: object, *, app_id: str, **_kwargs: object) -> None:
        nonlocal fail_retirement
        events.append("retire:" + app_id)
        if fail_retirement:
            raise ValueError(provider_error)

    async def finalize(_recovery: object) -> None:
        events.append("finalize")

    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service._installer = _UpdateInstaller(published, registry)
    service._connection_service = SimpleNamespace(
        retire_predecessor_for_update=retire,
        finalize_update_recovery=finalize,
        cancel_candidate_attempts_exact=lambda _identity: None,
    )
    service._submit_connection_operation = lambda operation: asyncio.run(operation())
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    operation = {
        "plugin_id": "office.bundle",
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "new-receipt",
        "phase": "committed",
        "candidate_apps": [
            {
                "app_id": "office.calendar",
                "state": "active",
                "request_id": None,
                "connection_revision": "6" * 64,
                "activation": None,
                "settings": _consent(),
            }
        ],
    }
    service._seal_update_recovery(operation, binding, recovery)
    service._write_update_document(service._update_document(operation))

    service._reconcile_update_intent()

    assert events == ["retire:office.mail"]
    assert service._read_update_document()["operation"]["phase"] == "committed"
    assert registry.record == promoted.record

    fail_retirement = False
    service._reconcile_update_intent()

    assert events == ["retire:office.mail", "retire:office.mail", "finalize"]
    assert service._read_update_document()["operation"] is None
    assert registry.record == promoted.record


@pytest.mark.anyio
@pytest.mark.parametrize("phase", ("committed", "retirement_complete"))
async def test_catalog_surfaces_durable_post_commit_retirement_as_pending(
    tmp_path: Path,
    phase: str,
) -> None:
    old, _published = _update_records()
    binding = _binding("new-receipt")
    operation = {
        "plugin_id": "office.bundle",
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": "4" * 64,
        "base_record": old.to_dict(),
        "dependency_receipt_id": "new-receipt",
        "phase": phase,
        "candidate_apps": [
            {
                "app_id": "office.mail",
                "state": "active",
                "request_id": None,
                "connection_revision": "6" * 64,
                "activation": None,
                "settings": _consent(),
            }
        ],
    }
    service = object.__new__(PluginLifecycleService)
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    service._seal_update_recovery(
        operation,
        binding,
        _sealed_connection_recovery(old, binding),
    )
    service._write_update_document(service._update_document(operation))
    service.plugin_registry = SimpleNamespace(
        get=lambda _plugin_id: SimpleNamespace(
            selected_version="2.0.0",
            enabled=True,
            blocker=None,
        )
    )
    service._app_ids = lambda _plugin_id: ("office.mail",)
    service.status = lambda _plugin_id: asyncio.sleep(
        0,
        result=SimpleNamespace(state=PluginLifecycleState.CONNECTED),
    )

    assert await service.catalog_state(
        "office.bundle",
        catalog_version="2.0.0",
    ) == PluginLifecycleState.REVOCATION_PENDING.value


@pytest.mark.anyio
@pytest.mark.parametrize("path", ("start", "complete", "poll"))
@pytest.mark.parametrize("phase", ("committed", "retirement_complete"))
async def test_committed_retirement_blocks_non_candidate_connection_mutation(
    path: str,
    phase: str,
) -> None:
    service = object.__new__(PluginLifecycleService)
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    service._update_state = object()
    service._pending_update = lambda *_args: None
    service._read_update_document = lambda: {
        "schema_version": 1,
        "operation": {"plugin_id": "office.bundle", "phase": phase},
    }
    calls: list[str] = []

    async def mutate(*_args, **_kwargs) -> object:
        calls.append("mutated")
        return SimpleNamespace(status="connected")

    service._connection_coordinator = SimpleNamespace(start=mutate)
    service._connection_service = SimpleNamespace(
        callback_request_id=lambda _callback: "a" * 64,
        request_plugin_id=lambda _request: "office.bundle",
        complete_callback=mutate,
        complete=mutate,
        complete_device=mutate,
    )
    service._app_ids = lambda _plugin_id: ("office.mail",)
    service._lifecycle_revision_snapshot = lambda: SimpleNamespace(revision="f" * 64)

    with pytest.raises(
        ValueError,
        match="^plugin_lifecycle_operation_in_progress$",
    ):
        if path == "start":
            await service.start_connection("office.bundle", "office.mail")
        elif path == "complete":
            await service.complete_connection(
                "http://127.0.0.1/oauth/profile/callback?state=x&code=y"
            )
        else:
            await service.poll_connection("a" * 64)
    assert calls == []


@pytest.mark.parametrize(
    "required_app_ids",
    (("office.calendar", "office.mail"), ("office.mail",)),
    ids=("adds-required-app", "changes-existing-app-authority"),
)
def test_update_readiness_uses_exact_candidate_scope_without_replacing_old_authority(
    monkeypatch: pytest.MonkeyPatch,
    required_app_ids: tuple[str, ...],
) -> None:
    """Catches candidate setup being verified through the selected manifest/authority."""
    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    registry.record = published
    candidate_binding = PluginAppBinding(
        required_app_ids=required_app_ids,
        optional_app_ids=(),
        dependency_receipt_id="new-receipt",
        revision=sha256_hex(
            canonical_bytes(
                {
                    "required_app_ids": list(required_app_ids),
                    "optional_app_ids": [],
                    "dependency_receipt_id": "new-receipt",
                }
            )
        ),
    )
    tool_kind = SimpleNamespace(value="tool")
    requirements = tuple(
        SimpleNamespace(
            app_id=app_id,
            capabilities=(SimpleNamespace(kind=tool_kind),),
        )
        for app_id in required_app_ids
    )
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id="office.bundle",
            plugin_version="2.0.0",
            required_apps=requirements,
            optional_apps=(),
        ),
        package_sha256="4" * 64,
    )
    candidate_revisions = {
        app_id: sha256_hex(f"candidate:{app_id}".encode())
        for app_id in required_app_ids
    }
    predecessor_app_ids = (
        old.app_binding.required_app_ids + old.app_binding.optional_app_ids
    )
    probed: list[str] = []
    probed_consent: list[dict[str, object]] = []

    async def status(plugin_id: str, *, app_id: str, account_key: str) -> object:
        assert plugin_id == "office.bundle"
        assert account_key == "operator"
        # The connection service owns only the selected binding's apps; an app
        # the candidate adds is never asked about -- it has no connection.
        assert app_id in predecessor_app_ids
        return _live_status(app_id, scopes=("mail.read",))

    async def probe_candidate(
        plugin_id: str,
        *,
        app_id: str,
        candidate_verified: object,
        candidate_binding: PluginAppBinding,
        account_key: str,
        expected_registry_revision: str,
        **consent: object,
    ) -> object:
        assert plugin_id == "office.bundle"
        assert candidate_verified is verified
        assert candidate_binding == candidate_binding_value
        assert account_key == "operator"
        assert expected_registry_revision == "b" * 64
        assert registry.record.selected_version == "1.0.0"
        assert registry.record.enabled is True
        probed.append(app_id)
        probed_consent.append(consent)
        return SimpleNamespace(
            status="connected",
            connection_revision=candidate_revisions[app_id],
        )

    candidate_binding_value = candidate_binding
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service.app_registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(revision="a" * 64, records=())
    )
    service._connection_service = SimpleNamespace(
        status=status,
        _candidate_definition=_candidate_definition(scopes=("mail.read",)),
        probe_candidate=probe_candidate,
    )
    service._app_ids = lambda _plugin_id: predecessor_app_ids
    service.operator_account_key = lambda: "operator"
    service._submit_connection_operation = lambda operation: asyncio.run(operation())
    service._candidate_record = lambda _verified, _binding, app_id: SimpleNamespace(
        app_id=app_id,
        to_dict=lambda: {"app_id": app_id},
    )
    service._candidate_descriptor = lambda _verified, _binding, app_id: SimpleNamespace(
        app_id=app_id
    )
    service.planning_connection_revision = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("selected authority must not compile the candidate")
    )

    def compile_candidate(*, connection_revision_provider, **_kwargs):
        for app_id in required_app_ids:
            descriptor = SimpleNamespace(app_id=app_id)
            assert connection_revision_provider(
                "office.bundle", app_id, "account-hash", descriptor
            ) == candidate_revisions[app_id]
        return SimpleNamespace(
            entries=(
                SimpleNamespace(
                    plugin_id="office.bundle",
                    capability_kind="tool",
                ),
            )
        )

    monkeypatch.setattr(
        "mcp_platform.plugin_catalog.compile_plugin_runtime_catalog",
        compile_candidate,
    )

    if set(required_app_ids) - set(predecessor_app_ids):
        # An app the candidate adds has no consent to carry forward: readiness
        # needs the operator's explicit selection before anything is probed.
        with pytest.raises(ValueError, match="^plugin_authorization_required$"):
            service._assert_update_candidate_ready(
                "office.bundle",
                verified,
                candidate_binding,
            )
        assert probed == []
    else:
        service._assert_update_candidate_ready(
            "office.bundle",
            verified,
            candidate_binding,
        )
        assert probed == list(required_app_ids)
        # The existing app is probed under exactly the consent it already holds.
        assert probed_consent == [
            {
                "scopes": ("mail.read",),
                "capability_ceiling": ("tool:send",),
                "allowed_tool_schemas": {"send": _TOOL_DIGEST},
                "execution_policy": "confirm_each",
                "source_constraints": {},
                "destination_constraints": {},
            }
        ]
    assert registry.record.selected_version == "1.0.0"
    assert registry.record.enabled is True


@pytest.mark.anyio
async def test_candidate_probe_uses_exact_unselected_signed_package_without_publication(
    tmp_path: Path,
) -> None:
    """Exercises the real candidate-only connection path, not a selected-version fake."""

    client = _Client(tuple(tool.to_dict() for tool in catalogs_for("tracker")[0].tools))
    service, _broker, installed = _remote_service(
        tmp_path,
        authentication_kind="none",
        client_factory=lambda _descriptor, authorization: (
            client
            if authorization is None
            else pytest.fail("credentialless candidate received authorization")
        ),
    )
    durable_registry = installed["plugin_registry"]
    selected = durable_registry.get(TRACKER_PLUGIN)
    verified = durable_registry.reverify_installed(TRACKER_PLUGIN)
    # The candidate is the selected package's exact bytes, so the operator's full
    # grant of the selected app is exactly the candidate's full offer.
    grant = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    target = selected.versions[0]
    candidate = InstalledPluginVersionRecord(
        plugin_version=target.plugin_version,
        signing_key_id=target.signing_key_id,
        manifest_sha256=target.manifest_sha256,
        package_sha256=target.package_sha256,
        state="candidate",
    )
    old = InstalledPluginVersionRecord(
        plugin_version="0.9.0",
        signing_key_id=target.signing_key_id,
        manifest_sha256="1" * 64,
        package_sha256="2" * 64,
        state="selected",
    )
    unselected = InstalledPluginRecord(
        **{
            **selected.__dict__,
            "selected_version": old.plugin_version,
            "enabled": True,
            "versions": (old, candidate),
        }
    )
    expected_revision = "a" * 64
    service._plugin_registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            records=(unselected,),
            revision=expected_revision,
        )
    )
    service._candidate_descriptor_provider = lambda candidate_verified, _binding, app_id: (
        lifecycle_module.descriptor_for_record(
            installed["app_registry"].get(app_id),
            signed_capability_authority=(
                lifecycle_module.signed_capability_authority_for_candidate(
                    installed["package_registry"],
                    installed["app_registry"].get(app_id),
                    candidate_verified,
                )
            ),
        )
    )

    proof = await service.probe_candidate(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        candidate_verified=verified,
        candidate_binding=selected.app_binding,
        account_key="operator-account",
        **grant,
        expected_registry_revision=expected_revision,
    )

    assert proof.status == "connected"
    assert len(proof.connection_revision) == 64
    assert client.started is True
    assert client.closed is True
    assert service._sessions == {}
    with pytest.raises(ValueError, match="^plugin_connection_authority_not_found$"):
        service.authority_store.connection(
            TRACKER_PLUGIN,
            TRACKER_APP,
            sha256_hex(b"operator-account"),
        )
    assert durable_registry.get(TRACKER_PLUGIN) == selected


def test_candidate_authority_fails_closed_when_installed_package_reverification_fails() -> None:
    class _CorruptRegistry:
        def reverify_installed(self, _app_id: str) -> object:
            raise ValueError("corrupt signed package")

    candidate = SimpleNamespace(
        asset_bytes={
            "tools/authority.json": (
                b'{"tools":{"fallback":{"title":"Fallback"}}}'
            )
        }
    )

    with pytest.raises(ValueError, match="^plugin_catalog_authority_missing$"):
        lifecycle_module.signed_capability_authority_for_candidate(
            _CorruptRegistry(),
            SimpleNamespace(app_id=TRACKER_APP),
            candidate,
        )


def test_an_abandoned_lifecycle_operation_is_reclaimed_not_kept_forever(
    tmp_path: Path,
) -> None:
    """One interrupted install must not wedge every install after it.

    `_admit_lifecycle_operation` refuses while any operation is recorded, and
    `_reconcile_lifecycle_operation` -- which runs at construction to clear
    exactly this -- refused to touch a record it had not admitted itself. A new
    process always starts with `_owned_lifecycle_operation = None`, so a record
    left by an owner that died mid-install made the service raise
    `plugin_lifecycle_operation_unavailable` on construction and every install
    after it failed. The operator hit it live on 2026-09-07: a Gmail sign-in
    came back `plugin_lifecycle_operation_in_progress` with nothing running.

    Acquiring the owner lease is the proof that the previous owner is gone --
    a live one still holds it, and is still refused below.
    """
    service = PluginLifecycleService(state_root=tmp_path)
    abandoned = service._admit_lifecycle_operation(
        kind="install",
        plugin_id="plugin.alpha",
    )
    assert service._read_lifecycle_operation_document()["operation"] == abandoned

    # The owner goes away without releasing: its lease is gone, its record is not.
    service._close_lifecycle_owner_lease()

    successor = _lifecycle_intent_replica(service)
    successor._installer = _RecordingInstaller()
    successor._update_state = service._update_state
    successor._uninstall_state = service._uninstall_state

    successor._reconcile_lifecycle_operation()

    assert successor._installer.recovered is True
    assert successor._read_lifecycle_operation_document()["operation"] is None
    # And the path it was blocking is open again.
    readmitted = successor._admit_lifecycle_operation(
        kind="install",
        plugin_id="plugin.beta",
    )
    assert readmitted["plugin_id"] == "plugin.beta"


def test_a_live_owners_operation_is_still_refused(tmp_path: Path) -> None:
    """The reclaim must not become a way past a running install.

    A second process that cannot take the lease has learned the first is alive,
    and leaves its work alone -- which is what the refusal always meant.
    """
    service = PluginLifecycleService(state_root=tmp_path)
    service._admit_lifecycle_operation(kind="install", plugin_id="plugin.alpha")

    successor = _lifecycle_intent_replica(service)
    successor._installer = _RecordingInstaller()

    with pytest.raises(ValueError, match="^plugin_lifecycle_operation_unavailable$"):
        successor._reconcile_lifecycle_operation()

    assert successor._installer.recovered is False


class _RecordingInstaller:
    def __init__(self) -> None:
        self.recovered = False

    def recover(self) -> None:
        self.recovered = True


def _retire_provenance(service, plugin_id: str) -> None:
    """Make one plugin's provenance record unfindable, as retiring it does."""
    from plugin_platform.marketplace_provenance import ProviderProvenanceVerificationError

    real = service._verified_provenance()

    class _WithoutRecord:
        publisher_id = real.publisher_id
        records = tuple(r for r in real.records if r.plugin_id != plugin_id)

        def for_plugin(self, requested: str):
            if requested == plugin_id:
                raise ProviderProvenanceVerificationError("provider_provenance_not_found")
            return real.for_plugin(requested)

        def for_verified_plugin(self, verified):
            return real.for_verified_plugin(verified)

    service._verified_provenance = _WithoutRecord


@pytest.mark.anyio
async def test_a_retired_provider_leaves_its_installed_plugin_readable(
    tmp_path: Path,
) -> None:
    """Retiring a provider must not brick the plugin panel.

    Removing four providers from the catalog on 2026-09-08 deleted their signed
    provenance records while the packages were still installed.
    `_installed_verified` requires a provenance record for a first-party
    package, so `status()` raised `provider_provenance_not_found` -- a reason the
    HUD's map does not carry, so the WHOLE installed listing answered "The plugin
    service is unavailable. Restart the HUD and retry.", taking the Uninstall
    button down with it.

    BLOCKED is the honest state: installed, unusable, and still renderable so it
    can be cleared.
    """
    plugin_id = "google.workspace.drive"
    service = PluginLifecycleService(state_root=tmp_path)
    try:
        _install_catalog_plugin(service, plugin_id)
        _retire_provenance(service, plugin_id)

        snapshot = await service.status(plugin_id)

        assert snapshot.plugin_id == plugin_id
        assert snapshot.state is PluginLifecycleState.BLOCKED
    finally:
        service.close()


def test_a_plugin_with_no_provenance_record_stays_removable(tmp_path: Path) -> None:
    """Removal must never require a trust artifact that governs USE.

    `_uninstall_locked` read the installed plugin through the helper that also
    demands provenance, so the plugin whose record had been retired -- the one an
    operator most needs to clear -- became the one that could not be removed.
    Uninstall now asks only what that read was there to establish: the plugin
    exists and its package still verifies.
    """
    from plugin_platform.marketplace_provenance import ProviderProvenanceVerificationError

    plugin_id = "google.workspace.drive"
    service = PluginLifecycleService(state_root=tmp_path)
    try:
        _install_catalog_plugin(service, plugin_id)
        _retire_provenance(service, plugin_id)

        with pytest.raises(ProviderProvenanceVerificationError):
            service._installed_verified(plugin_id)

        verified = service._installed_for_removal(plugin_id)
        assert verified.manifest.plugin_id == plugin_id
    finally:
        service.close()


@pytest.mark.anyio
async def test_a_retired_provider_leaves_its_catalog_card_renderable(
    tmp_path: Path,
) -> None:
    """The directory listing dies on one bad card too.

    `search_catalog` projects a lifecycle state for every INSTALLED entry it
    lists, and `catalog_state` reaches the same `_app_ids` that `status` does. So
    the same retired provider that broke the installed listing separately broke
    the public one -- fixing `status` alone left the operator with a panel whose
    top half worked and whose bottom half still said the plugin service was
    unavailable.
    """
    plugin_id = "google.workspace.drive"
    service = PluginLifecycleService(state_root=tmp_path)
    try:
        _install_catalog_plugin(service, plugin_id)
        _retire_provenance(service, plugin_id)

        state = await service.catalog_state(
            plugin_id,
            catalog_version=service.plugin_registry.get(plugin_id).selected_version,
        )

        assert state == PluginLifecycleState.BLOCKED.value
    finally:
        service.close()


def _pending_update_service(tmp_path: Path) -> PluginLifecycleService:
    """A service holding one promoted-but-unauthorized update, as `update()` leaves it."""

    old, published = _update_records()
    registry = _UpdateRegistry(old, published)
    service = object.__new__(PluginLifecycleService)
    service.plugin_registry = registry
    service.app_registry = SimpleNamespace()
    service._installer = _UpdateInstaller(published, registry)
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    _attach_lifecycle_operation_state(service, tmp_path)
    service._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id="office.bundle",
            plugin_version="2.0.0",
            signing_key_id="test.key",
            required_apps=(
                SimpleNamespace(
                    app_id="office.mail",
                    authentication=SimpleNamespace(
                        kind=SimpleNamespace(value="oauth_authorization_code_pkce")
                    ),
                ),
            ),
            optional_apps=(),
        ),
        manifest_sha256="3" * 64,
        package_sha256="4" * 64,
    )
    service._verified = lambda _plugin_id: (object(), object(), verified)
    recovery = _sealed_connection_recovery(old, _binding("new-receipt"))
    service._connection_service = SimpleNamespace(
        begin_update_recovery=lambda **_kwargs: recovery
    )
    service.operator_account_key = lambda: "operator"
    # `_pending_update` reverifies the candidate against the registry before it
    # will describe the operation at all -- the same gate the live service goes
    # through on every read.
    registry.reverify_candidate = lambda _plugin_id, _candidate: verified
    service.update("office.bundle", "2.0.0", "confirmed")
    return service


def _connected_snapshot() -> PluginLifecycleSnapshot:
    """What the connection coordinator answers mid-update: the predecessor, live."""

    return PluginLifecycleSnapshot(
        plugin_id="office.bundle",
        state=PluginLifecycleState.CONNECTED,
        registry_revision="a" * 64,
        package_version="1.0.0",
        apps=(
            PluginAppState(
                app_id="office.mail",
                required=True,
                state=PluginLifecycleState.CONNECTED,
                authority_revision="b" * 64,
                error_code=None,
            ),
        ),
        next_required_app_id=None,
        enabled=True,
        error_code=None,
    )


def test_status_names_the_app_a_pending_update_is_waiting_on(tmp_path: Path) -> None:
    """A promoted update leaves the predecessor connected -- and status said so.

    That is honest about the predecessor and useless to the operator: the
    operation is blocked on exactly the sign-in status called unnecessary.
    The panel may not guess `setup_action` (the connection route is its only
    source), so clicking Update opened the authorize popup and closed it again.
    """

    service = _pending_update_service(tmp_path)
    refined = service._with_pending_update_setup("office.bundle", _connected_snapshot())

    assert refined.state is PluginLifecycleState.SETUP_REQUIRED
    assert refined.next_required_app_id == "office.mail"
    assert refined.apps[0].state is PluginLifecycleState.SETUP_REQUIRED
    assert refined.error_code == "plugin_setup_required"
    # The predecessor keeps serving until the candidate is authorized, so the
    # version on show is still its own.
    assert refined.package_version == "1.0.0"


def test_status_is_unchanged_for_a_plugin_with_no_pending_update(tmp_path: Path) -> None:
    """The refinement is scoped to the plugin the operation is actually about."""

    service = _pending_update_service(tmp_path)
    snapshot = _connected_snapshot()

    other = service._with_pending_update_setup("unrelated.plugin", snapshot)

    assert other is snapshot


def test_status_never_fails_because_the_update_document_cannot_be_read(
    tmp_path: Path,
) -> None:
    """A display refinement must never be the reason a status read fails."""

    service = _pending_update_service(tmp_path)
    snapshot = _connected_snapshot()

    def _unreadable(_plugin_id: str | None = None) -> object:
        raise ValueError("plugin_update_recovery_required")

    service._pending_update = _unreadable

    assert service._with_pending_update_setup("office.bundle", snapshot) is snapshot


_RETAINED_PLUGIN = "northwind.calendar"
_RETAINED_REQUIRED_APP = "northwind.calendar.mcp"
_RETAINED_OPTIONAL_APP = "northwind.tracker.mcp"


def _update_waiting_on_retained_optional_app():
    """A pending update whose required app is ready and whose next app is a retained
    optional one that now needs sign-in (for example, its sign-in method changed)."""
    service = object.__new__(PluginLifecycleService)
    operation = {
        "candidate_apps": [
            {"app_id": _RETAINED_REQUIRED_APP, "state": "ready"},
            {"app_id": _RETAINED_OPTIONAL_APP, "state": "setup_required"},
        ]
    }
    binding = SimpleNamespace(
        required_app_ids=(_RETAINED_REQUIRED_APP,),
        optional_app_ids=(_RETAINED_OPTIONAL_APP,),
    )
    verified = object()
    service._pending_update = lambda plugin_id: (
        (operation, verified, binding) if plugin_id == _RETAINED_PLUGIN else None
    )
    return service, operation, verified, binding


def test_status_names_a_retained_optional_app_a_pending_update_waits_on() -> None:
    """The update cannot finish without the retained optional app's sign-in, so status
    names it as the next app instead of reporting the predecessor as connected."""
    service, _operation, _verified, _binding = _update_waiting_on_retained_optional_app()
    connected = PluginLifecycleSnapshot(
        plugin_id=_RETAINED_PLUGIN,
        state=PluginLifecycleState.CONNECTED,
        registry_revision="b" * 64,
        package_version="1.0.0",
        apps=(
            PluginAppState(
                app_id=_RETAINED_REQUIRED_APP,
                required=True,
                state=PluginLifecycleState.CONNECTED,
                authority_revision="a" * 64,
                error_code=None,
            ),
            PluginAppState(
                app_id=_RETAINED_OPTIONAL_APP,
                required=False,
                state=PluginLifecycleState.CONNECTED,
                authority_revision="c" * 64,
                error_code=None,
            ),
        ),
        next_required_app_id=None,
        enabled=True,
        error_code=None,
    )

    snapshot = service._with_pending_update_setup(_RETAINED_PLUGIN, connected)

    assert snapshot.state is PluginLifecycleState.SETUP_REQUIRED
    assert snapshot.next_required_app_id == _RETAINED_OPTIONAL_APP
    apps = {app.app_id: app for app in snapshot.apps}
    assert apps[_RETAINED_OPTIONAL_APP].state is PluginLifecycleState.SETUP_REQUIRED
    assert apps[_RETAINED_REQUIRED_APP].state is PluginLifecycleState.CONNECTED


@pytest.mark.anyio
@pytest.mark.parametrize("named", (True, False), ids=("named", "default-next"))
async def test_setup_accepts_the_retained_optional_app_a_pending_update_waits_on(
    named: bool,
) -> None:
    """Continue setup for the app the update names reaches its candidate connection
    instead of answering plugin_app_not_installed, which left the update unfinishable."""
    service, operation, verified, binding = _update_waiting_on_retained_optional_app()
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    service._lifecycle_revision_snapshot = lambda: SimpleNamespace(revision="d" * 64)
    service.plugin_registry = SimpleNamespace(snapshot=lambda: SimpleNamespace(revision="e" * 64))
    selection = {"revision": "d" * 64}

    async def permissions(plugin_id, app_id):
        assert (plugin_id, app_id) == (_RETAINED_PLUGIN, _RETAINED_OPTIONAL_APP)
        return selection

    service.permissions = permissions
    service._connection_service = SimpleNamespace(
        _candidate_definition=lambda *_args, **_kwargs: (None, None, None, "descriptor")
    )
    service._validated_permission_settings = lambda *_args, **_kwargs: {"validated": True}
    started = []

    async def start_candidate(plugin_id, app_id, **kwargs):
        started.append((plugin_id, app_id, kwargs["operation"], kwargs["candidate_binding"]))
        return "started"

    service._start_candidate_connection_locked = start_candidate

    result = await service.start_connection(
        _RETAINED_PLUGIN,
        _RETAINED_OPTIONAL_APP if named else None,
        settings={},
    )

    assert result == "started"
    assert started == [(_RETAINED_PLUGIN, _RETAINED_OPTIONAL_APP, operation, binding)]


@pytest.mark.anyio
async def test_setup_still_refuses_an_app_the_pending_update_is_not_waiting_on() -> None:
    service, _operation, _verified, _binding = _update_waiting_on_retained_optional_app()
    service._authority_mutation_lock = lifecycle_module._LifecycleMutationBoundary()
    service._lifecycle_revision_snapshot = lambda: SimpleNamespace(revision="d" * 64)

    with pytest.raises(ValueError, match="^plugin_app_not_installed$"):
        await service.start_connection(_RETAINED_PLUGIN, _RETAINED_REQUIRED_APP, settings={})

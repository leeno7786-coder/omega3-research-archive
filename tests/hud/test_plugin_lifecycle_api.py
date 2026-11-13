from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import hud.server as hud_server
import mcp_platform.plugin_lifecycle as lifecycle_module
from hud.server import create_app
from mcp_platform.plugin_connections import PluginConnectionCoordinator
from mcp_platform.plugin_lifecycle import (
    PluginLifecycleService,
    PluginUpdatePending,
    PluginUpdateReview,
)
from mcp_platform.plugin_state import PluginAppState, PluginLifecycleSnapshot, PluginLifecycleState
from plugin_platform.catalog_sources import CatalogSourceDescriptor, CatalogSourceKind
from plugin_platform.catalog_sync import CatalogSyncResult, CatalogSyncStatus
from plugin_platform.contracts import AppDeployment, PluginKind
from tests.mcp_platform.test_plugin_state import (
    _Connection,
    _Registry,
    _app,
    _manifest,
)


class RecordingLifecycleService:
    revision = "a" * 64
    plugin_registry = None
    # The real search_catalog()/_merged_catalog_revision() are bound onto this
    # fake wholesale rather than reimplemented: the HUD catalog route now
    # delegates to the one real implementation, so a route-level test double
    # must run that real implementation too, not a second copy of its logic.
    search_catalog = PluginLifecycleService.search_catalog
    _merged_catalog_revision = PluginLifecycleService._merged_catalog_revision
    # Same for the connection route's setup requirement: with no update state the
    # real _pending_update answers None, so the installed manifest answers.
    setup_requirement = PluginLifecycleService.setup_requirement
    _pending_update = PluginLifecycleService._pending_update

    def __init__(self) -> None:
        # (plugin_id, app_id, settings): the exact per-app consent selection
        # each accepted start carried, so a route test can prove the operator's
        # own selection for that app -- not another app's, and not none --
        # reached the lifecycle service.
        self.started: list[tuple[str, str | None, dict[str, object] | None]] = []
        self.polled: list[str] = []
        self.permission_reads: list[tuple[str, str | None]] = []
        self.permission_writes: list[tuple[str, str | None, str, dict[str, object]]] = []
        self.callback_status = SimpleNamespace(
            plugin_id="office.bundle",
            state="connected",
            next_required_app_id=None,
        )
        self.update_result = None
        self.sync_result = None
        self.review_calls: list[str] = []
        self.review_result = None
        self.install_calls: list[tuple[str, str]] = []
        self.install_result = None
        self.update_review_calls: list[tuple[str, str]] = []
        self.update_review_result = None
        self.disconnect_calls: list[str] = []
        self.disconnect_result = None
        self.uninstall_calls: list[str] = []
        self._catalog_cursor_key = b"k" * 32
        self._directory = SimpleNamespace(
            list_directory=lambda: SimpleNamespace(status="ready", plugins=())
        )
        self._revision_counter = 0

    @staticmethod
    def _submit_connection_operation(operation):
        # search_catalog() calls this synchronously; run_in_threadpool already
        # moved it off the event loop, so a fresh loop here is safe.
        return asyncio.run(operation())

    def _advance_revision(self) -> None:
        """Mirror the real server: every completed connection step changes
        the composite lifecycle revision (mcp_platform.plugin_lifecycle's
        `_lifecycle_components()` folds in grants/authority/vault/
        permissions, all of which a completed connection just changed).
        Shadows the class-level `revision` default with a fresh
        per-instance value so a caller still holding the pre-connection
        revision is rejected on its next mutating call -- task-5 fix
        round 1, finding 1."""
        self._revision_counter += 1
        self.revision = hashlib.sha256(
            f"recording-lifecycle-revision-{self._revision_counter}".encode()
        ).hexdigest()

    def catalog_source(self):
        plugin = SimpleNamespace(
            plugin_id="office.bundle",
            title="Office Bundle",
            to_dict=lambda: {
                "plugin_id": "office.bundle",
                "title": "Office Bundle",
            },
        )
        return SimpleNamespace(
            snapshot=lambda: SimpleNamespace(
                revision="b" * 64,
                plugins=(plugin,),
            )
        )

    async def start_connection(
        self,
        plugin_id: str,
        app_id: str | None = None,
        *,
        revision: str | None = None,
        settings: dict[str, object] | None = None,
    ):
        # Real behavior (PluginLifecycleService.start_connection,
        # mcp_platform/plugin_lifecycle.py): a revision that no longer
        # matches the server's current one is a stale-caller signal, not an
        # internal contract violation -- reject it the same way, with the
        # same stable cause, rather than crashing the test process.
        if revision != self.revision:
            raise ValueError("plugin_permission_changed")
        # Real behavior: an exact app connection starts only under the
        # operator's explicit per-app selection -- never an implied grant.
        if app_id is not None and type(settings) is not dict:
            raise ValueError("plugin_scopes_invalid")
        self.started.append((plugin_id, app_id, settings))
        result = SimpleNamespace(
            to_dict=lambda: {
                "plugin_id": plugin_id,
                "app_id": app_id,
                "status": "connecting",
            }
        )
        self._advance_revision()
        return result

    async def poll_connection(self, request_id: str):
        self.polled.append(request_id)
        result = SimpleNamespace(
            plugin_id="office.bundle",
            to_dict=lambda: {
                "plugin_id": "office.bundle",
                "status": "connected",
            },
        )
        self._advance_revision()
        return result

    async def complete_connection(self, callback_uri: str):
        del callback_uri
        return self.callback_status

    async def catalog_state(self, plugin_id: str, *, catalog_version: str) -> str:
        del catalog_version
        return getattr(self, "catalog_states", {}).get(plugin_id, "connected")

    async def permissions(self, plugin_id: str, app_id: str | None = None):
        self.permission_reads.append((plugin_id, app_id))
        return {
            "plugin_id": plugin_id,
            "app_id": app_id,
            "offered": {},
            "selected": {},
        }

    async def set_permissions(
        self,
        plugin_id: str,
        *,
        revision: str,
        settings: dict[str, object],
        app_id: str | None = None,
    ):
        self.permission_writes.append((plugin_id, app_id, revision, settings))
        return {"plugin_id": plugin_id, "app_id": app_id, "status": "connecting"}

    def review(self, plugin_id: str):
        self.review_calls.append(plugin_id)
        if self.review_result is None:
            raise AssertionError("review_result was not configured")
        return self.review_result

    def update_review(self, plugin_id: str, version: str):
        self.update_review_calls.append((plugin_id, version))
        if self.update_review_result is None:
            raise AssertionError("update_review_result was not configured")
        if isinstance(self.update_review_result, Exception):
            raise self.update_review_result
        return self.update_review_result

    def install(self, plugin_id: str, *, confirmation: object):
        self.install_calls.append((plugin_id, confirmation))
        if self.install_result is None:
            raise AssertionError("install_result was not configured")
        return self.install_result

    def update(self, plugin_id: str, version: str, confirmation: object):
        assert plugin_id == "office.bundle"
        assert version == "2.0.0"
        assert confirmation == "a" * 64
        if self.update_result is None:
            raise AssertionError("update_result was not configured")
        return self.update_result

    def sync_catalog(self):
        if self.sync_result is None:
            raise AssertionError("sync_result was not configured")
        return self.sync_result

    async def disconnect(self, plugin_id: str):
        self.disconnect_calls.append(plugin_id)
        if self.disconnect_result is None:
            raise AssertionError("disconnect_result was not configured")
        return self.disconnect_result

    async def uninstall(self, plugin_id: str) -> None:
        self.uninstall_calls.append(plugin_id)


def _app_connection_settings(
    provider_scopes: list[str], capability_ids: list[str]
) -> dict[str, object]:
    """One app's exact consent selection, in the closed settings shape the
    permissions editor submits (settings-plugins.js ``connectionSettings``)
    and /connection/start now requires for every exact app."""

    return {
        "provider_scopes": provider_scopes,
        "capability_ids": capability_ids,
        "execution_policy": "confirm_each",
        "source_constraints": {},
        "destination_constraints": {},
    }


def test_update_returns_closed_server_held_candidate_continuation(monkeypatch) -> None:
    """The client receives setup routing, never candidate identity or signed digests."""

    service = RecordingLifecycleService()
    service.update_result = PluginUpdatePending(
        plugin_id="office.bundle",
        app_ids=("office.mail",),
        next_required_app_id="office.mail",
    )
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())

    response = client.post(
        "/api/plugins/office.bundle/update",
        json={"confirmation": "a" * 64, "version": "2.0.0", "revision": service.revision},
    )
    missing_version = client.post(
        "/api/plugins/office.bundle/update",
        json={"confirmation": "a" * 64, "revision": service.revision},
    )
    forged = client.post(
        "/api/plugins/office.bundle/update",
        json={
            "confirmation": "a" * 64,
            "version": "2.0.0",
            "revision": service.revision,
            "candidate_version": "9.9.9",
        },
    )

    assert response.status_code == 202
    assert response.json() == {
        "ok": True,
        "revision": service.revision,
        "plugin_id": "office.bundle",
        "status": "setup_required",
        "app_ids": ["office.mail"],
        "next_required_app_id": "office.mail",
    }
    assert missing_version.status_code == 422
    assert missing_version.json() == {
        "ok": False,
        "error": "invalid_plugin_lifecycle_request",
    }
    assert forged.status_code == 422
    assert forged.json() == {
        "ok": False,
        "error": "invalid_plugin_lifecycle_request",
    }


def test_update_returns_a_completed_lifecycle_snapshot(monkeypatch) -> None:
    """The 200 (non-pending) branch: a real PluginLifecycleSnapshot, spread whole."""

    service = RecordingLifecycleService()
    service.update_result = PluginLifecycleSnapshot(
        plugin_id="office.bundle",
        state=PluginLifecycleState.CONNECTED,
        registry_revision="b" * 64,
        package_version="2.0.0",
        apps=(),
        next_required_app_id=None,
        enabled=True,
        error_code=None,
    )
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())

    response = client.post(
        "/api/plugins/office.bundle/update",
        json={"confirmation": "a" * 64, "version": "2.0.0", "revision": service.revision},
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "revision": service.revision,
        "plugin_id": "office.bundle",
        "state": "connected",
        "registry_revision": "b" * 64,
        "package_version": "2.0.0",
        "apps": [],
        "next_required_app_id": None,
        "enabled": True,
        "error_code": None,
    }


def test_catalog_sync_route_reports_updated_unchanged_and_failed(monkeypatch) -> None:
    """Delegates only: never installs/enables, reports the synchronizer's own outcome."""

    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())

    service.sync_result = CatalogSyncResult(
        status=CatalogSyncStatus.UPDATED,
        sequence=7,
        revision="d" * 64,
    )
    updated = client.post("/api/plugins/catalog/sync")

    service.sync_result = CatalogSyncResult(
        status=CatalogSyncStatus.UNCHANGED,
        sequence=6,
        revision="c" * 64,
    )
    unchanged = client.post("/api/plugins/catalog/sync")

    service.sync_result = CatalogSyncResult(
        status=CatalogSyncStatus.FAILED,
        error="plugin_catalog_unreachable",
    )
    failed = client.post("/api/plugins/catalog/sync")

    assert updated.status_code == 200
    assert updated.json() == {
        "ok": True,
        "revision": service.revision,
        "status": "updated",
        "sequence": 7,
        "catalog_revision": "d" * 64,
        "error": None,
    }
    assert unchanged.status_code == 200
    assert unchanged.json() == {
        "ok": True,
        "revision": service.revision,
        "status": "unchanged",
        "sequence": 6,
        "catalog_revision": "c" * 64,
        "error": None,
    }
    assert failed.status_code == 200
    assert failed.json() == {
        "ok": True,
        "revision": service.revision,
        "status": "failed",
        "sequence": None,
        "catalog_revision": None,
        "error": "plugin_catalog_unreachable",
    }


def test_plugin_routes_use_only_lifecycle_service(monkeypatch) -> None:
    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    app = create_app()
    client = TestClient(app)

    # A successful /connection/start advances service.revision in place
    # (task-5 fix round 1, finding 1's test-double fix -- see
    # RecordingLifecycleService._advance_revision): capture the revision
    # each response was actually built against at the moment of that call,
    # not the live value read after both requests have completed.
    revision_before_start = service.revision
    mail_settings = _app_connection_settings(["mail.read"], ["tool:mail.search"])
    catalog = client.get("/api/plugins/catalog")
    started = client.post(
        "/api/plugins/office.bundle/connection/start",
        json={
            "app_id": "office.mail",
            "revision": revision_before_start,
            "settings": mail_settings,
        },
    )

    assert app.state.plugin_lifecycle is service
    assert catalog.status_code == 200
    assert catalog.json()["revision"] == revision_before_start
    assert started.status_code == 200
    assert started.json()["revision"] == service.revision
    assert started.json()["revision"] != revision_before_start
    # The operator's exact selection for this app is what reached the
    # lifecycle service -- verbatim, not rebuilt or defaulted by the route.
    assert service.started == [("office.bundle", "office.mail", mail_settings)]


def test_connection_start_preserves_issuer_mismatch_without_provider_details(monkeypatch) -> None:
    from mcp_platform.oauth_protocol import OAuthProtocolError

    service = RecordingLifecycleService()

    async def rejected(*args, **kwargs):
        raise OAuthProtocolError("oauth_issuer_mismatch")

    service.start_connection = rejected
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())
    response = client.post(
        "/api/plugins/office.bundle/connection/start",
        json={"revision": service.revision},
    )
    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "oauth_issuer_mismatch"}


def test_connection_start_route_rejects_the_prior_apps_revision_once_it_connected(
    monkeypatch,
) -> None:
    """A composite plugin's second required app cannot be started with the
    revision the first app's own connection response returned as *current*
    -- start_connection's real implementation
    (mcp_platform/plugin_lifecycle.py:1596-1598) re-checks the caller's held
    revision against the server's own before every app and rejects a stale
    one with plugin_permission_changed, because grants/authority/vault/
    permissions all changed once the first app connected. This is the
    server-side contract the HUD's per-app consent rounds must track: the
    setup machine (hud/ui-src/core/plugin-setup.js) stops after each app and
    hands the fresh revision back, and the panel starts the next app only
    from that app's own consent form (task-5 fix round 1, finding 1; per-app
    operator consent, 2af18bdf).

    Each app is also started only under its own exact consent selection:
    the second app can never ride on the first app's settings or on none."""

    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())
    stale_revision = service.revision
    mail_settings = _app_connection_settings(["mail.read"], ["tool:mail.search"])
    calendar_settings = _app_connection_settings(
        ["calendar.read"], ["tool:calendar.list_events"]
    )

    first = client.post(
        "/api/plugins/office.bundle/connection/start",
        json={
            "app_id": "office.mail",
            "revision": stale_revision,
            "settings": mail_settings,
        },
    )
    assert first.status_code == 200
    fresh_revision = first.json()["revision"]
    assert fresh_revision != stale_revision

    retried_with_stale = client.post(
        "/api/plugins/office.bundle/connection/start",
        json={
            "app_id": "office.calendar",
            "revision": stale_revision,
            "settings": calendar_settings,
        },
    )
    assert retried_with_stale.status_code == 409
    assert retried_with_stale.json() == {"ok": False, "error": "plugin_permission_changed"}
    # The rejected attempt must never have reached the lifecycle service at
    # all -- only the one real, accepted call is recorded.
    assert service.started == [("office.bundle", "office.mail", mail_settings)]

    # A fresh revision alone is not consent: the next app without its own
    # exact selection is refused at the route boundary and never reaches the
    # lifecycle service.
    without_own_consent = client.post(
        "/api/plugins/office.bundle/connection/start",
        json={"app_id": "office.calendar", "revision": fresh_revision},
    )
    assert without_own_consent.status_code == 422
    assert without_own_consent.json() == {
        "ok": False,
        "error": "invalid_plugin_lifecycle_request",
    }
    assert service.started == [("office.bundle", "office.mail", mail_settings)]

    second = client.post(
        "/api/plugins/office.bundle/connection/start",
        json={
            "app_id": "office.calendar",
            "revision": fresh_revision,
            "settings": calendar_settings,
        },
    )
    assert second.status_code == 200
    assert second.json()["revision"] != fresh_revision
    assert service.started == [
        ("office.bundle", "office.mail", mail_settings),
        ("office.bundle", "office.calendar", calendar_settings),
    ]


def test_connection_start_rejects_unknown_json_keys(monkeypatch) -> None:
    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())

    response = client.post(
        "/api/plugins/office.bundle/connection/start",
        json={
            "app_id": "office.mail",
            "revision": service.revision,
            "provider": "example",
        },
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "error": "invalid_plugin_lifecycle_request",
    }
    assert service.started == []


def test_device_poll_uses_the_strict_same_origin_closed_json_boundary(monkeypatch) -> None:
    """Catches poll accepting cross-origin, non-JSON, or duplicate-key bodies."""
    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())
    request_id = "a" * 64

    wrong_type = client.post(
        "/api/plugins/office.bundle/connection/poll",
        content='{"request_id":"' + request_id + '"}',
        headers={"content-type": "text/plain"},
    )
    cross_origin = client.post(
        "/api/plugins/office.bundle/connection/poll",
        content='{"request_id":"' + request_id + '"}',
        headers={
            "content-type": "application/json",
            "origin": "https://evil.example",
        },
    )
    duplicate = client.post(
        "/api/plugins/office.bundle/connection/poll",
        content=(
            '{"request_id":"'
            + request_id
            + '","request_id":"'
            + request_id
            + '"}'
        ),
        headers={"content-type": "application/json"},
    )

    assert wrong_type.status_code == 415
    assert cross_origin.status_code == 403
    assert duplicate.status_code == 422
    assert service.polled == []


def test_permissions_routes_preserve_exact_app_and_closed_body(monkeypatch) -> None:
    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())
    settings = {
        "provider_scopes": [],
        "capability_ids": [],
        "execution_policy": "confirm_each",
        "source_constraints": {},
        "destination_constraints": {},
    }

    read = client.get("/api/plugins/office.bundle/permissions?app_id=office.mail")
    written = client.put(
        "/api/plugins/office.bundle/permissions",
        json={
            "revision": service.revision,
            "app_id": "office.mail",
            "settings": settings,
        },
    )
    rejected = client.put(
        "/api/plugins/office.bundle/permissions",
        json={"revision": service.revision, "settings": settings, "provider": "x"},
    )

    assert read.status_code == 200
    assert written.status_code == 200
    assert rejected.status_code == 422
    assert service.permission_reads == [("office.bundle", "office.mail")]
    assert service.permission_writes == [
        ("office.bundle", "office.mail", service.revision, settings)
    ]


def test_oauth_callback_reports_canonical_partial_multi_app_state(monkeypatch) -> None:
    """Catches one app callback falsely publishing the whole plugin connected."""
    service = RecordingLifecycleService()
    service.callback_status = SimpleNamespace(
        plugin_id="office.bundle",
        state="setup_required",
        next_required_app_id="office.calendar",
    )
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())

    response = client.get(
        "/oauth/office.desktop/callback?code=opaque&state=opaque"
    )

    assert response.status_code == 200
    assert "Additional plugin setup is required" in response.text
    assert "omega:plugin-setup-required" in response.text
    assert "office.calendar" in response.text
    assert "omega:plugin-connected" not in response.text


def test_failed_oauth_callback_notifies_its_attempt_without_reflecting_secrets(monkeypatch):
    service = RecordingLifecycleService()
    service.connection_service = SimpleNamespace(callback_request_id=lambda _uri: "a" * 64)

    async def expired(_uri):
        raise ValueError("oauth_transaction_expired")

    service.complete_connection = expired
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    response = TestClient(create_app()).get(
        "/oauth/office.desktop/callback?code=secret-code&state=secret-state"
    )
    assert response.status_code == 400
    assert "omega:plugin-authorization-failed" in response.text
    assert "plugin_auth_expired" in response.text
    assert "a" * 64 in response.text
    assert "secret-code" not in response.text
    assert "secret-state" not in response.text
    assert "postMessage" in response.text
    assert "Return to Omega" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_stale_authority_write_maps_to_permission_changed_recovery(monkeypatch) -> None:
    """Catches a stale authority CAS surfacing as a misleading 503."""
    service = RecordingLifecycleService()

    async def stale(*_args, **_kwargs):
        raise ValueError("plugin_authority_revision_conflict")

    service.set_permissions = stale
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())

    response = client.put(
        "/api/plugins/office.bundle/permissions",
        json={
            "revision": service.revision,
            "app_id": "office.mail",
            "settings": {
                "provider_scopes": [],
                "capability_ids": [],
                "execution_policy": "confirm_each",
                "source_constraints": {},
                "destination_constraints": {},
            },
        },
    )

    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "plugin_permission_changed"}


def test_long_lived_lifecycle_operation_conflict_is_typed_provider_neutral() -> None:
    response = hud_server._plugin_lifecycle_error(
        ValueError("plugin_lifecycle_operation_in_progress")
    )

    assert response.status_code == 409
    assert json.loads(bytes(response.body)) == {
        "ok": False,
        "error": "plugin_lifecycle_operation_in_progress",
    }


def test_installed_route_projects_title_summary_and_trust_tier_from_the_sealed_manifest(
    monkeypatch,
) -> None:
    """/api/plugins/installed must name each installed plugin from its own
    verified manifest (mcp_platform.plugin_directory.PluginDirectoryService.
    installed_plugin_summary), independent of catalog pagination or a
    Personal plugin with no catalog listing at all -- task-3 review
    finding 2. A plugin whose reverification fails must still appear (its
    lifecycle state is still authoritative) but without a fabricated title.
    """
    service = RecordingLifecycleService()
    connected_snapshot = PluginLifecycleSnapshot(
        plugin_id="demo.one",
        state=PluginLifecycleState.CONNECTED,
        registry_revision="a" * 64,
        package_version="1.0.0",
        apps=(),
        next_required_app_id=None,
        enabled=True,
        error_code=None,
    )
    unverifiable_snapshot = PluginLifecycleSnapshot(
        plugin_id="demo.two",
        state=PluginLifecycleState.CONNECTED,
        registry_revision="b" * 64,
        package_version="2.0.0",
        apps=(),
        next_required_app_id=None,
        enabled=True,
        error_code=None,
    )
    snapshots_by_id = {"demo.one": connected_snapshot, "demo.two": unverifiable_snapshot}

    async def canonical_status(plugin_id: str):
        return snapshots_by_id[plugin_id]

    service.status = canonical_status
    verified_one = SimpleNamespace(
        manifest=SimpleNamespace(title="Demo One", description="A demo plugin."),
        trust_tier=SimpleNamespace(value="omega_verified"),
    )

    def reverify(plugin_id: str):
        if plugin_id == "demo.two":
            raise RuntimeError("package_missing")
        return verified_one

    service.plugin_registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            records=(
                SimpleNamespace(plugin_id="demo.one"),
                SimpleNamespace(plugin_id="demo.two"),
            )
        ),
        reverify_installed=reverify,
    )
    # create_app() also eagerly builds its own (unrelated to this route)
    # app.state.plugin_directory from catalog_source() + plugin_registry;
    # give it a real descriptor so that incidental construction succeeds
    # (the route under test never reads app.state.plugin_directory).
    service.catalog_source = lambda: SimpleNamespace(
        descriptor=CatalogSourceDescriptor(
            source_id="marketplace", title="Marketplace", kind=CatalogSourceKind.MARKETPLACE
        ),
        snapshot=lambda: SimpleNamespace(revision="c" * 64, plugins=()),
    )
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    app = create_app()
    client = TestClient(app)

    response = client.get("/api/plugins/installed")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    by_id = {item["plugin_id"]: item for item in body["plugins"]}
    assert by_id["demo.one"]["title"] == "Demo One"
    assert by_id["demo.one"]["summary"] == "A demo plugin."
    assert by_id["demo.one"]["trust_tier"] == "omega_verified"
    # Lifecycle fields the route already returned stay present alongside
    # the new projection.
    assert by_id["demo.one"]["state"] == "connected"
    assert "title" not in by_id["demo.two"]
    assert "summary" not in by_id["demo.two"]
    assert "trust_tier" not in by_id["demo.two"]
    assert by_id["demo.two"]["state"] == "connected"


def _directory_plugin(
    plugin_id: str,
    *,
    installed: bool,
    trust: str = "omega_verified",
) -> SimpleNamespace:
    document = {
        "plugin_id": plugin_id,
        "plugin_version": "1.0.0",
        "title": f"Office {plugin_id.rsplit('.', 1)[-1].title()}",
        "description": "Provider-neutral office tools.",
        "categories": ["featured", "productivity"],
        "trust_state": trust,
        "installed": installed,
        "enabled": installed,
        "connection_state": "connected" if installed else "available",
    }
    return SimpleNamespace(**document, to_dict=lambda: dict(document))


def test_catalog_filters_and_pages_over_one_stable_snapshot(monkeypatch) -> None:
    """Catches advertised filters being ignored and cursors never being emitted."""
    service = RecordingLifecycleService()
    plugins = (
        _directory_plugin("office.calendar", installed=False),
        _directory_plugin("office.mail", installed=False),
        _directory_plugin("office.storage", installed=True),
    )
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    app = create_app()
    service._directory = SimpleNamespace(
        list_directory=lambda: SimpleNamespace(status="ready", plugins=plugins)
    )
    client = TestClient(app)

    first = client.get(
        "/api/plugins/catalog",
        params={
            "q": "office",
            "category": "featured",
            "trust": "omega_verified",
            "state": "available",
            "page_size": "1",
        },
    )
    assert first.status_code == 200
    assert [item["plugin_id"] for item in first.json()["plugins"]] == [
        "office.calendar"
    ]
    cursor = first.json()["next_cursor"]
    assert type(cursor) is str and cursor

    second = client.get(
        "/api/plugins/catalog",
        params={
            "q": "office",
            "category": "featured",
            "trust": "omega_verified",
            "state": "available",
            "page_size": "1",
            "cursor": cursor,
        },
    )
    assert second.status_code == 200
    assert [item["plugin_id"] for item in second.json()["plugins"]] == ["office.mail"]
    assert second.json()["next_cursor"] is None


def test_catalog_route_rejects_a_page_size_beyond_the_service_ceiling(monkeypatch) -> None:
    """The route enforces MAX_CATALOG_PAGE_SIZE, not a separate local ceiling."""

    service = RecordingLifecycleService()
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())

    response = client.get(
        "/api/plugins/catalog",
        params={"page_size": str(lifecycle_module.MAX_CATALOG_PAGE_SIZE + 1)},
    )

    assert response.status_code == 422
    assert response.json() == {
        "ok": False,
        "error": "invalid_plugin_lifecycle_request",
    }


def test_catalog_route_reports_degraded_status_on_a_source_failure(monkeypatch) -> None:
    """A failed catalog source degrades the response rather than erroring out."""

    service = RecordingLifecycleService()
    # PluginDirectoryService itself catches per-source failures and reports
    # status="degraded" with an empty overlay when nothing is installed --
    # exercise that exact contract through the route.
    service._directory = SimpleNamespace(
        list_directory=lambda: SimpleNamespace(status="degraded", plugins=())
    )
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())

    response = client.get("/api/plugins/catalog")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "degraded"
    assert body["plugins"] == []
    assert body["total_plugins"] == 0


def test_catalog_route_maps_a_cross_source_identity_collision(monkeypatch) -> None:
    service = RecordingLifecycleService()

    def colliding_list_directory():
        raise lifecycle_module.PluginDirectoryError("plugin_directory_identity_ambiguous")

    service._directory = SimpleNamespace(list_directory=colliding_list_directory)
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())

    response = client.get("/api/plugins/catalog")

    assert response.json() == {
        "ok": False,
        "error": "plugin_catalog_identity_ambiguous",
    }


_CANONICAL_CATALOG_STATES = (
    "available",
    "review_required",
    "installing",
    "installed",
    "setup_required",
    "connecting",
    "connected",
    "offline",
    "update_available",
    "permission_changed",
    "revocation_pending",
    "blocked",
    "incompatible",
)


@pytest.mark.parametrize("state", _CANONICAL_CATALOG_STATES)
def test_catalog_uses_each_canonical_lifecycle_state_with_stable_pagination(
    monkeypatch,
    state: str,
) -> None:
    """Catches aliases replacing the canonical lifecycle state vocabulary."""
    service = RecordingLifecycleService()
    service.catalog_states = {
        f"plugin.{state}.a": state,
        f"plugin.{state}.b": state,
    }

    async def catalog_state(plugin_id: str, *, catalog_version: str) -> str:
        del catalog_version
        return service.catalog_states[plugin_id]

    service.catalog_state = catalog_state
    plugins = tuple(
        _directory_plugin(plugin_id, installed=state != "available")
        for plugin_id in service.catalog_states
    )
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    app = create_app()
    service._directory = SimpleNamespace(
        list_directory=lambda: SimpleNamespace(status="ready", plugins=plugins)
    )
    client = TestClient(app)

    first = client.get(
        "/api/plugins/catalog",
        params={"state": state, "trust": "omega_verified", "page_size": "1"},
    )
    assert first.status_code == 200
    assert first.json()["plugins"][0]["plugin_id"] == f"plugin.{state}.a"
    assert first.json()["total_plugins"] == 2
    cursor = first.json()["next_cursor"]
    assert type(cursor) is str

    second = client.get(
        "/api/plugins/catalog",
        params={
            "state": state,
            "trust": "omega_verified",
            "page_size": "1",
            "cursor": cursor,
        },
    )
    assert second.status_code == 200
    assert second.json()["plugins"][0]["plugin_id"] == f"plugin.{state}.b"
    assert second.json()["next_cursor"] is None


@pytest.mark.parametrize("trust", ("omega_verified", "publisher_verified", "personal"))
def test_catalog_accepts_each_canonical_trust_tier(monkeypatch, trust: str) -> None:
    service = RecordingLifecycleService()
    plugins = (
        _directory_plugin(f"plugin.{trust}.a", installed=False, trust=trust),
        _directory_plugin(f"plugin.{trust}.b", installed=False, trust=trust),
    )
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    app = create_app()
    service._directory = SimpleNamespace(
        list_directory=lambda: SimpleNamespace(status="ready", plugins=plugins)
    )
    response = TestClient(app).get(
        "/api/plugins/catalog",
        params={"trust": trust, "state": "available", "page_size": "1"},
    )

    assert response.status_code == 200
    assert response.json()["plugins"][0]["trust_state"] == trust
    assert response.json()["next_cursor"] is not None


@pytest.mark.parametrize(
    ("parameters", "error"),
    (
        ({"trust": "verified"}, "invalid_plugin_lifecycle_request"),
        ({"state": "not_installed"}, "invalid_plugin_lifecycle_request"),
        ({"state": "connection_required"}, "invalid_plugin_lifecycle_request"),
        ({"trust": "omega_verified", "state": "unknown"}, "invalid_plugin_lifecycle_request"),
    ),
)
def test_catalog_rejects_noncanonical_trust_and_state_values(
    monkeypatch,
    parameters: dict[str, str],
    error: str,
) -> None:
    service = RecordingLifecycleService()
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)

    response = TestClient(create_app()).get("/api/plugins/catalog", params=parameters)

    assert response.status_code == 422
    assert response.json() == {"ok": False, "error": error}


def test_catalog_cursor_rejects_recomputed_forgery_query_mismatch_and_stale_snapshot(
    monkeypatch,
) -> None:
    """Catches caller-recomputable cursor digests and cross-query/snapshot replay."""
    service = RecordingLifecycleService()
    plugins = (
        _directory_plugin("office.calendar", installed=False),
        _directory_plugin("office.mail", installed=False),
    )
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    app = create_app()
    service._directory = SimpleNamespace(
        list_directory=lambda: SimpleNamespace(status="ready", plugins=plugins),
        # Ties the merged-catalog cursor fingerprint to `service.revision` so
        # this test's later `service.revision = "c" * 64` mutation actually
        # changes it -- a bare SimpleNamespace directory has no registry
        # revision of its own for the fingerprint to reflect.
        plugin_registry=SimpleNamespace(
            snapshot=lambda: SimpleNamespace(revision=service.revision)
        ),
    )
    client = TestClient(app)
    parameters = {"q": "office", "page_size": "1"}
    cursor = client.get("/api/plugins/catalog", params=parameters).json()["next_cursor"]
    assert type(cursor) is str

    padded = cursor + "=" * (-len(cursor) % 4)
    envelope = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
    envelope["payload"]["offset"] = 0
    forged_payload = json.dumps(
        envelope["payload"], sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    envelope["mac"] = hashlib.sha256(forged_payload).hexdigest()
    forged = base64.urlsafe_b64encode(
        json.dumps(envelope, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")

    forged_response = client.get(
        "/api/plugins/catalog", params={**parameters, "cursor": forged}
    )
    mismatched = client.get(
        "/api/plugins/catalog",
        params={"q": "calendar", "page_size": "1", "cursor": cursor},
    )
    service.revision = "c" * 64
    stale = client.get(
        "/api/plugins/catalog", params={**parameters, "cursor": cursor}
    )

    assert forged_response.status_code == 422
    assert forged_response.json()["error"] == "plugin_catalog_cursor_invalid"
    assert mismatched.status_code == 422
    assert mismatched.json()["error"] == "plugin_catalog_cursor_invalid"
    assert stale.status_code == 409
    assert stale.json()["error"] == "plugin_catalog_cursor_stale"


@pytest.mark.parametrize(
    "state",
    (
        "revocation_pending",
        "permission_changed",
        "blocked",
        "incompatible",
        "offline",
        "connecting",
        "setup_required",
    ),
)
def test_catalog_api_uses_real_lifecycle_precedence_over_newer_version(
    monkeypatch,
    state: str,
) -> None:
    """Route filtering consumes the canonical method rather than echoing the filter."""

    service = RecordingLifecycleService()
    plugin_ids = (f"plugin.{state}.a", f"plugin.{state}.b")
    records = {
        plugin_id: SimpleNamespace(
            selected_version="0.9.0",
            enabled=True,
            blocker="blocked" if state == "blocked" else None,
        )
        for plugin_id in plugin_ids
    }
    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = SimpleNamespace(get=lambda plugin_id: records[plugin_id])
    lifecycle._app_ids = lambda _plugin_id: ("generic.app",)

    async def canonical_status(_plugin_id: str):
        return SimpleNamespace(state=PluginLifecycleState(state))

    lifecycle.status = canonical_status

    async def real_catalog_state(plugin_id: str, *, catalog_version: str) -> str:
        return await PluginLifecycleService.catalog_state(
            lifecycle,
            plugin_id,
            catalog_version=catalog_version,
        )

    service.catalog_state = real_catalog_state
    plugins = tuple(_directory_plugin(plugin_id, installed=True) for plugin_id in plugin_ids)
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    app = create_app()
    service._directory = SimpleNamespace(
        list_directory=lambda: SimpleNamespace(status="ready", plugins=plugins)
    )
    client = TestClient(app)

    first = client.get(
        "/api/plugins/catalog",
        params={"state": state, "page_size": "1"},
    )
    second = client.get(
        "/api/plugins/catalog",
        params={
            "state": state,
            "page_size": "1",
            "cursor": first.json()["next_cursor"],
        },
    )

    assert first.status_code == 200
    assert first.json()["plugins"][0]["plugin_id"] == plugin_ids[0]
    assert second.status_code == 200
    assert second.json()["plugins"][0]["plugin_id"] == plugin_ids[1]
    assert second.json()["next_cursor"] is None


def test_catalog_filter_paginates_real_blocker_and_connection_precedence(
    monkeypatch,
) -> None:
    """Filtering consumes coordinator evidence before slicing stable pages."""

    definitions = {
        "plugin.blocked.a": "connected",
        "plugin.blocked.b": "offline",
        "plugin.revoked": "revocation_pending",
    }
    registries: dict[str, _Registry] = {}
    coordinators: dict[str, PluginConnectionCoordinator] = {}
    connections: dict[str, _Connection] = {}
    for plugin_id, state in definitions.items():
        app_id = f"{plugin_id}.app"
        manifest = _manifest(
            plugin_id,
            kind=PluginKind.MCP_BACKED,
            required=(_app(app_id),),
        )
        registry = _Registry(manifest, enabled=True)
        registry.record = replace(registry.record, blocker="dependency_unavailable")
        connection = _Connection({app_id: state})
        registries[plugin_id] = registry
        connections[plugin_id] = connection
        coordinators[plugin_id] = PluginConnectionCoordinator(
            plugin_registry=registry,
            connection_service=connection,
        )
    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = SimpleNamespace(
        get=lambda plugin_id: registries[plugin_id].record
    )
    lifecycle._app_ids = lambda plugin_id: (
        f"{plugin_id}.app",
    )

    async def status(plugin_id: str):
        return await coordinators[plugin_id].status(plugin_id, "operator")

    lifecycle.status = status
    service = RecordingLifecycleService()

    async def catalog_state(plugin_id: str, *, catalog_version: str) -> str:
        return await PluginLifecycleService.catalog_state(
            lifecycle,
            plugin_id,
            catalog_version=catalog_version,
        )

    service.catalog_state = catalog_state
    plugins = tuple(
        _directory_plugin(plugin_id, installed=True)
        for plugin_id in sorted(definitions)
    )
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    app = create_app()
    service._directory = SimpleNamespace(
        list_directory=lambda: SimpleNamespace(status="ready", plugins=plugins)
    )
    client = TestClient(app)

    revoked = client.get(
        "/api/plugins/catalog",
        params={"state": "revocation_pending", "page_size": "1"},
    )
    blocked_first = client.get(
        "/api/plugins/catalog",
        params={"state": "blocked", "page_size": "1"},
    )
    blocked_second = client.get(
        "/api/plugins/catalog",
        params={
            "state": "blocked",
            "page_size": "1",
            "cursor": blocked_first.json()["next_cursor"],
        },
    )

    assert [item["plugin_id"] for item in revoked.json()["plugins"]] == [
        "plugin.revoked"
    ]
    assert revoked.json()["total_plugins"] == 1
    assert [item["plugin_id"] for item in blocked_first.json()["plugins"]] == [
        "plugin.blocked.a"
    ]
    assert [item["plugin_id"] for item in blocked_second.json()["plugins"]] == [
        "plugin.blocked.b"
    ]
    assert connections["plugin.revoked"].status_reads == [
        "plugin.revoked.app",
        "plugin.revoked.app",
        "plugin.revoked.app",
    ]


def test_catalog_filter_paginates_zero_app_blockers_after_plugin_wide_risk(
    monkeypatch,
) -> None:
    """Skill-only cards still consume durable plugin-wide precedence before paging."""

    local_states = {
        "plugin.skill.blocked.a": None,
        "plugin.skill.blocked.b": None,
        "plugin.skill.permission": "permission_changed",
        "plugin.skill.revoked": "revocation_pending",
    }
    records = {
        plugin_id: SimpleNamespace(
            selected_version="1.0.0",
            enabled=True,
            blocker="dependency_unavailable",
        )
        for plugin_id in local_states
    }
    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = SimpleNamespace(get=lambda plugin_id: records[plugin_id])
    lifecycle._app_ids = lambda _plugin_id: ()
    lifecycle._has_pending_update_retirement = lambda _plugin_id: False
    lifecycle._connection_service = SimpleNamespace(
        local_plugin_state_without_account=lambda plugin_id: local_states[plugin_id]
    )
    service = RecordingLifecycleService()

    async def catalog_state(plugin_id: str, *, catalog_version: str) -> str:
        return await PluginLifecycleService.catalog_state(
            lifecycle,
            plugin_id,
            catalog_version=catalog_version,
        )

    service.catalog_state = catalog_state
    plugins = tuple(
        _directory_plugin(plugin_id, installed=True)
        for plugin_id in sorted(local_states)
    )
    service.catalog_source = lambda: SimpleNamespace(
        snapshot=lambda: SimpleNamespace(
            revision="b" * 64,
            plugins=plugins,
        )
    )
    service._directory = SimpleNamespace(
        list_directory=lambda: SimpleNamespace(status="ready", plugins=plugins)
    )
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())

    blocked_first = client.get(
        "/api/plugins/catalog",
        params={"state": "blocked", "page_size": "1"},
    )
    blocked_second = client.get(
        "/api/plugins/catalog",
        params={
            "state": "blocked",
            "page_size": "1",
            "cursor": blocked_first.json()["next_cursor"],
        },
    )
    permission = client.get(
        "/api/plugins/catalog",
        params={"state": "permission_changed", "page_size": "1"},
    )
    revoked = client.get(
        "/api/plugins/catalog",
        params={"state": "revocation_pending", "page_size": "1"},
    )

    assert [item["plugin_id"] for item in blocked_first.json()["plugins"]] == [
        "plugin.skill.blocked.a"
    ]
    assert [item["plugin_id"] for item in blocked_second.json()["plugins"]] == [
        "plugin.skill.blocked.b"
    ]
    assert blocked_second.json()["next_cursor"] is None
    assert [item["plugin_id"] for item in permission.json()["plugins"]] == [
        "plugin.skill.permission"
    ]
    assert [item["plugin_id"] for item in revoked.json()["plugins"]] == [
        "plugin.skill.revoked"
    ]


def test_featured_route_family_is_absent(monkeypatch) -> None:
    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: service,
    )
    client = TestClient(create_app())

    assert client.get("/api/plugins/featured").status_code == 404
    assert (
        client.post(
            "/api/plugins/featured/office.bundle/connection/start",
            json={},
        ).status_code
        == 404
    )
    source = Path(hud_server.__file__).read_text(encoding="utf-8")
    assert "/api/plugins/featured" not in source
    assert "featured_plugin_" not in source


def test_review_route_accepts_matching_revision_and_rejects_stale_or_missing(
    monkeypatch,
) -> None:
    """Task-4 controller ruling 2: /review now requires and checks the
    client's held lifecycle revision. A stale revision returns the stable
    plugin_revision_conflict cause without ever reaching the service; a
    missing revision is an ordinary strict-body rejection."""

    service = RecordingLifecycleService()
    service.review_result = SimpleNamespace(
        to_dict=lambda: {
            "plugin_id": "office.bundle",
            "publisher_id": "office",
            "plugin_version": "1.0.0",
            "kind": "mcp_backed",
            "catalog_sha256": "c" * 64,
            "manifest_sha256": "m" * 64,
            "package_sha256": "p" * 64,
            "signing_key_id": "office-key",
            "expected_registry_revision": "a" * 64,
            "required_app_ids": ["office.mail"],
            "optional_app_ids": [],
            "dependencies": [],
        },
        sha256="d" * 64,
    )
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    accepted = client.post(
        "/api/plugins/office.bundle/review", json={"revision": service.revision}
    )
    stale = client.post(
        "/api/plugins/office.bundle/review", json={"revision": "b" * 64}
    )
    missing = client.post("/api/plugins/office.bundle/review", json={})

    assert accepted.status_code == 200
    body = accepted.json()
    assert body["ok"] is True
    assert body["revision"] == service.revision
    assert body["review"]["sha256"] == "d" * 64
    assert body["review"]["plugin_version"] == "1.0.0"

    assert stale.status_code == 409
    assert stale.json() == {"ok": False, "error": "plugin_revision_conflict"}

    assert missing.status_code == 422
    assert missing.json() == {"ok": False, "error": "invalid_plugin_lifecycle_request"}

    # Neither the stale nor the missing-revision request ever reached the
    # lifecycle service's own review() -- only the one accepted call did.
    assert service.review_calls == ["office.bundle"]


def test_install_route_accepts_matching_revision_and_rejects_stale_or_missing(
    monkeypatch,
) -> None:
    """Task-4 controller ruling 2, applied to /install."""

    service = RecordingLifecycleService()
    service.install_result = SimpleNamespace(
        record=SimpleNamespace(plugin_id="office.bundle"),
        revision="e" * 64,
        dependency_receipt=SimpleNamespace(receipt_id="receipt-1"),
    )
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    accepted = client.post(
        "/api/plugins/office.bundle/install",
        json={"confirmation": "d" * 64, "revision": service.revision},
    )
    stale = client.post(
        "/api/plugins/office.bundle/install",
        json={"confirmation": "d" * 64, "revision": "b" * 64},
    )
    missing = client.post(
        "/api/plugins/office.bundle/install", json={"confirmation": "d" * 64}
    )

    assert accepted.status_code == 200
    body = accepted.json()
    assert body["ok"] is True
    assert body["plugin_id"] == "office.bundle"
    assert body["registry_revision"] == "e" * 64
    assert body["dependency_receipt_id"] == "receipt-1"

    assert stale.status_code == 409
    assert stale.json() == {"ok": False, "error": "plugin_revision_conflict"}

    assert missing.status_code == 422
    assert missing.json() == {"ok": False, "error": "invalid_plugin_lifecycle_request"}

    assert service.install_calls == [("office.bundle", "d" * 64)]


def test_disconnect_route_accepts_matching_revision_and_rejects_stale_or_missing(
    monkeypatch,
) -> None:
    """Task-6 controller ruling 2, applied to /disconnect: disconnect removes
    connection authority only (the returned snapshot's own state, not a
    fabricated 'removed' marker) -- a stale or missing revision never
    reaches the lifecycle service."""

    service = RecordingLifecycleService()
    service.disconnect_result = SimpleNamespace(
        to_dict=lambda: {
            "plugin_id": "office.bundle",
            "state": "installed",
            "registry_revision": "a" * 64,
            "package_version": "1.0.0",
            "apps": [],
            "next_required_app_id": None,
            "enabled": False,
            "error_code": None,
        }
    )
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    accepted = client.post(
        "/api/plugins/office.bundle/disconnect", json={"revision": service.revision}
    )
    stale = client.post(
        "/api/plugins/office.bundle/disconnect", json={"revision": "b" * 64}
    )
    missing = client.post("/api/plugins/office.bundle/disconnect", json={})

    assert accepted.status_code == 200
    body = accepted.json()
    assert body["ok"] is True
    assert body["state"] == "installed"
    assert body["plugin_id"] == "office.bundle"

    assert stale.status_code == 409
    assert stale.json() == {"ok": False, "error": "plugin_revision_conflict"}

    assert missing.status_code == 422
    assert missing.json() == {"ok": False, "error": "invalid_plugin_lifecycle_request"}

    # Neither the stale nor the missing-revision request ever reached the
    # lifecycle service's own disconnect().
    assert service.disconnect_calls == ["office.bundle"]


def test_uninstall_route_accepts_matching_revision_and_rejects_stale_or_missing(
    monkeypatch,
) -> None:
    """Task-6 controller ruling 2, applied to /uninstall: a stale or missing
    revision never reaches the lifecycle service, so a plugin is never
    removed against a revision the client no longer holds."""

    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    accepted = client.post(
        "/api/plugins/office.bundle/uninstall", json={"revision": service.revision}
    )
    stale = client.post(
        "/api/plugins/office.bundle/uninstall", json={"revision": "b" * 64}
    )
    missing = client.post("/api/plugins/office.bundle/uninstall", json={})

    assert accepted.status_code == 200
    body = accepted.json()
    assert body["ok"] is True
    assert body["status"] == "uninstalled"
    assert body["plugin_id"] == "office.bundle"

    assert stale.status_code == 409
    assert stale.json() == {"ok": False, "error": "plugin_revision_conflict"}

    assert missing.status_code == 422
    assert missing.json() == {"ok": False, "error": "invalid_plugin_lifecycle_request"}

    assert service.uninstall_calls == ["office.bundle"]


def test_permissions_put_route_rejects_a_stale_revision_before_computing_a_diff(
    monkeypatch,
) -> None:
    """Task-6 controller ruling 2, applied to PUT /permissions: a stale
    client-held revision is now rejected at the route level with the
    ruling's own stable cause (plugin_revision_conflict) before the
    lifecycle service ever runs -- distinct from
    test_stale_authority_write_maps_to_permission_changed_recovery above,
    which covers a race *during* an otherwise-accepted request (still
    plugin_permission_changed, from the service's own internal re-check)."""

    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())
    settings = {
        "provider_scopes": [],
        "capability_ids": [],
        "execution_policy": "confirm_each",
        "source_constraints": {},
        "destination_constraints": {},
    }

    stale = client.put(
        "/api/plugins/office.bundle/permissions",
        json={"revision": "b" * 64, "app_id": "office.mail", "settings": settings},
    )

    assert stale.status_code == 409
    assert stale.json() == {"ok": False, "error": "plugin_revision_conflict"}
    assert service.permission_writes == []


def test_update_route_rejects_stale_and_missing_revision(monkeypatch) -> None:
    """Task-4 controller ruling 2, applied to /update (the accepted-revision
    path is already covered by test_update_returns_a_completed_lifecycle_snapshot
    and test_update_returns_closed_server_held_candidate_continuation)."""

    service = RecordingLifecycleService()
    service.update_result = PluginLifecycleSnapshot(
        plugin_id="office.bundle",
        state=PluginLifecycleState.CONNECTED,
        registry_revision="b" * 64,
        package_version="2.0.0",
        apps=(),
        next_required_app_id=None,
        enabled=True,
        error_code=None,
    )
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    stale = client.post(
        "/api/plugins/office.bundle/update",
        json={"confirmation": "a" * 64, "version": "2.0.0", "revision": "b" * 64},
    )
    missing = client.post(
        "/api/plugins/office.bundle/update",
        json={"confirmation": "a" * 64, "version": "2.0.0"},
    )

    assert stale.status_code == 409
    assert stale.json() == {"ok": False, "error": "plugin_revision_conflict"}
    assert missing.status_code == 422
    assert missing.json() == {"ok": False, "error": "invalid_plugin_lifecycle_request"}


def _verified_office_bundle_authority() -> SimpleNamespace:
    capability = SimpleNamespace(kind=SimpleNamespace(value="tool"), name="send_mail")
    app_requirement = SimpleNamespace(
        app_id="office.mail",
        authentication=SimpleNamespace(
            kind=SimpleNamespace(value="oauth_authorization_code_pkce"),
            scopes=("mail.send",),
        ),
        remote_origin="https://mail.example.com",
        deployment=AppDeployment.REMOTE,
        capabilities=(capability,),
    )
    manifest = SimpleNamespace(required_apps=(app_requirement,), optional_apps=())
    return SimpleNamespace(manifest=manifest)


def test_catalog_entry_route_merges_installed_authority_from_the_sealed_manifest(
    monkeypatch,
) -> None:
    """The details route (task-4 controller ruling 1) must show an installed
    plugin's currently active capabilities, per-app requested provider
    scopes/authentication kind, and network boundary -- read from its already
    re-verified sealed manifest, never a claim about an unreviewed candidate
    and never a package download."""

    service = RecordingLifecycleService()
    plugin = SimpleNamespace(
        plugin_id="office.bundle",
        title="Office Bundle",
        to_dict=lambda: {
            "plugin_id": "office.bundle",
            "title": "Office Bundle",
            "summary": "Office tools.",
            "trust_tier": "omega_verified",
            "plugin_version": "1.0.0",
        },
    )
    service.catalog_source = lambda: SimpleNamespace(
        descriptor=CatalogSourceDescriptor(
            source_id="marketplace", title="Marketplace", kind=CatalogSourceKind.MARKETPLACE
        ),
        snapshot=lambda: SimpleNamespace(revision="c" * 64, plugins=(plugin,)),
    )
    service.plugin_registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(records=()),
        reverify_installed=lambda plugin_id: _verified_office_bundle_authority(),
    )
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    response = client.get("/api/plugins/catalog/office.bundle")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    plugin_payload = body["plugin"]
    assert plugin_payload["plugin_id"] == "office.bundle"
    assert plugin_payload["trust_tier"] == "omega_verified"
    assert plugin_payload["installed_authority"] == {
        "capabilities": ["tool:send_mail"],
        "apps": [
            {
                "app_id": "office.mail",
                "authentication_kind": "oauth_authorization_code_pkce",
                "requested_scopes": ["mail.send"],
                "remote_origin": "https://mail.example.com",
            }
        ],
        "network_origins": ["https://mail.example.com"],
        "filesystem_access": False,
    }
    # Never seal material or secrets.
    assert "signature" not in json.dumps(plugin_payload)
    assert "seal" not in json.dumps(plugin_payload).lower()


def test_catalog_entry_route_omits_installed_authority_when_not_installed(
    monkeypatch,
) -> None:
    """A hiccup in reverify_installed (including "not installed") only omits
    installed_authority -- it never fails the whole details response."""

    service = RecordingLifecycleService()
    plugin = SimpleNamespace(
        plugin_id="office.bundle",
        title="Office Bundle",
        to_dict=lambda: {"plugin_id": "office.bundle", "title": "Office Bundle"},
    )
    service.catalog_source = lambda: SimpleNamespace(
        descriptor=CatalogSourceDescriptor(
            source_id="marketplace", title="Marketplace", kind=CatalogSourceKind.MARKETPLACE
        ),
        snapshot=lambda: SimpleNamespace(revision="c" * 64, plugins=(plugin,)),
    )

    def _not_installed(plugin_id: str) -> None:
        raise ValueError("plugin_not_installed")

    service.plugin_registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(records=()),
        reverify_installed=_not_installed,
    )
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    response = client.get("/api/plugins/catalog/office.bundle")

    assert response.status_code == 200
    plugin_payload = response.json()["plugin"]
    assert plugin_payload["plugin_id"] == "office.bundle"
    assert "installed_authority" not in plugin_payload


def test_update_review_route_returns_the_complete_change_set(monkeypatch) -> None:
    """Task-4 fix round 1 finding 2: GET /api/plugins/{plugin_id}/update-review
    is validation-only, delegating entirely to
    PluginLifecycleService.update_review(), and returns the full permission
    change set (never just the 4-field subset the client used to compute
    itself)."""

    service = RecordingLifecycleService()
    service.update_review_result = PluginUpdateReview(
        plugin_id="office.bundle",
        from_version="1.0.0",
        to_version="2.0.0",
        permission_changes=(
            "endpoint_changed:office.mail",
            "scopes_changed:office.mail",
            "capabilities_changed:office.mail",
            "network_boundary_changed",
        ),
        sha256="a" * 64,
    )
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    response = client.get(
        "/api/plugins/office.bundle/update-review",
        params={"version": "2.0.0", "revision": service.revision},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["revision"] == service.revision
    assert body["review"] == {
        "plugin_id": "office.bundle",
        "from_version": "1.0.0",
        "to_version": "2.0.0",
        "permission_changes": [
            "endpoint_changed:office.mail",
            "scopes_changed:office.mail",
            "capabilities_changed:office.mail",
            "network_boundary_changed",
        ],
        "sha256": "a" * 64,
    }
    assert service.update_review_calls == [("office.bundle", "2.0.0")]


def test_update_review_route_rejects_unknown_version_and_missing_query(monkeypatch) -> None:
    service = RecordingLifecycleService()

    def _changed(plugin_id: str, version: str) -> None:
        raise ValueError("plugin_install_review_changed")

    service.update_review = _changed
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    unknown_version = client.get(
        "/api/plugins/office.bundle/update-review",
        params={"version": "9.9.9", "revision": service.revision},
    )
    assert unknown_version.status_code == 409
    assert unknown_version.json() == {
        "ok": False,
        "error": "plugin_install_review_changed",
    }

    missing_version = client.get(
        "/api/plugins/office.bundle/update-review",
        params={"revision": service.revision},
    )
    assert missing_version.status_code == 422
    assert missing_version.json() == {
        "ok": False,
        "error": "invalid_plugin_lifecycle_request",
    }

    malformed_version = client.get(
        "/api/plugins/office.bundle/update-review",
        params={"version": "not-a-semver", "revision": service.revision},
    )
    assert malformed_version.status_code == 422
    assert malformed_version.json() == {
        "ok": False,
        "error": "invalid_plugin_lifecycle_request",
    }


def test_update_review_route_rejects_stale_revision_without_computing_a_diff(
    monkeypatch,
) -> None:
    service = RecordingLifecycleService()
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    stale = client.get(
        "/api/plugins/office.bundle/update-review",
        params={"version": "2.0.0", "revision": "b" * 64},
    )
    assert stale.status_code == 409
    assert stale.json() == {"ok": False, "error": "plugin_revision_conflict"}
    assert service.update_review_calls == []

    missing_revision = client.get(
        "/api/plugins/office.bundle/update-review",
        params={"version": "2.0.0"},
    )
    assert missing_revision.status_code == 422
    assert missing_revision.json() == {
        "ok": False,
        "error": "invalid_plugin_lifecycle_request",
    }
    assert service.update_review_calls == []


def test_update_review_route_maps_not_available_and_offline_snapshot(monkeypatch) -> None:
    service = RecordingLifecycleService()

    def _not_available(plugin_id: str, version: str) -> None:
        raise ValueError("plugin_update_not_available")

    service.update_review = _not_available
    monkeypatch.setattr(
        hud_server, "get_default_plugin_lifecycle_service", lambda: service
    )
    client = TestClient(create_app())

    not_available = client.get(
        "/api/plugins/office.bundle/update-review",
        params={"version": "2.0.0", "revision": service.revision},
    )
    assert not_available.status_code == 409
    assert not_available.json() == {"ok": False, "error": "plugin_update_not_available"}

    def _offline(plugin_id: str, version: str) -> None:
        raise RuntimeError("plugin_catalog_unavailable")

    service.update_review = _offline
    offline = client.get(
        "/api/plugins/office.bundle/update-review",
        params={"version": "2.0.0", "revision": service.revision},
    )
    assert offline.status_code == 503
    assert offline.json() == {"ok": False, "error": "plugin_catalog_unavailable"}


# ---------------------------------------------------------------------------
# GET /api/plugins/{plugin_id}/connection -- task-5 controller ruling 1
# ---------------------------------------------------------------------------


def _app_requirement(app_id: str, authentication_kind: str, secret_fields=()) -> SimpleNamespace:
    return SimpleNamespace(
        app_id=app_id,
        authentication=SimpleNamespace(
            kind=SimpleNamespace(value=authentication_kind),
            scopes=(),
            secret_fields=secret_fields,
        ),
        remote_origin="https://provider.example",
        deployment=AppDeployment.REMOTE,
    )


def _secret_field(field_id: str, label: str, *, secret: bool, required: bool) -> SimpleNamespace:
    document = {"field_id": field_id, "label": label, "secret": secret, "required": required}
    return SimpleNamespace(**document, to_dict=lambda: dict(document))


def _marketplace_catalog_source() -> SimpleNamespace:
    # create_app() eagerly builds app.state.plugin_directory whenever
    # service.plugin_registry is not None (unrelated to the /connection
    # route under test) -- give it a real descriptor so that incidental
    # construction succeeds, matching the pattern task 3/4's tests already
    # established for the same eager-construction path.
    return SimpleNamespace(
        descriptor=CatalogSourceDescriptor(
            source_id="marketplace", title="Marketplace", kind=CatalogSourceKind.MARKETPLACE
        ),
        snapshot=lambda: SimpleNamespace(revision="c" * 64, plugins=()),
    )


def _connection_snapshot(
    plugin_id: str,
    *,
    state: str,
    next_required_app_id: str | None,
) -> PluginLifecycleSnapshot:
    """One real, self-consistent PluginLifecycleSnapshot: 'connected' has no
    apps at all (a zero-required-app plugin), 'setup_required' has exactly
    one required app in its own SETUP_REQUIRED state -- PluginLifecycleSnapshot
    derives its own top-level state/error_code from `apps` and rejects any
    snapshot whose fields disagree with that derivation."""

    if state == "connected":
        apps: tuple = ()
    else:
        assert next_required_app_id is not None
        apps = (
            PluginAppState(
                app_id=next_required_app_id,
                required=True,
                state=PluginLifecycleState.SETUP_REQUIRED,
                authority_revision="",
                error_code="plugin_setup_required",
            ),
        )
    return PluginLifecycleSnapshot(
        plugin_id=plugin_id,
        state=PluginLifecycleState(state),
        registry_revision="a" * 64,
        package_version="1.0.0",
        apps=apps,
        next_required_app_id=next_required_app_id,
        enabled=True,
        error_code=None if state == "connected" else "plugin_setup_required",
    )


@pytest.mark.parametrize(
    ("authentication_kind", "expected_action"),
    [
        ("oauth_authorization_code_pkce", "browser"),
        ("oauth_client_metadata_document", "browser"),
        ("oauth_device_code", "device"),
        ("api_key", "secret_form"),
        ("bearer_token", "secret_form"),
        ("basic", "secret_form"),
        ("declared_secret_fields", "secret_form"),
        ("none", "none"),
    ],
)
def test_connection_status_route_maps_every_authentication_kind_to_its_closed_setup_action(
    monkeypatch, authentication_kind: str, expected_action: str
) -> None:
    """The generic client (hud/ui-src/core/plugin-setup.js) never brands or
    guesses a provider -- it only executes whatever `setup_action` this
    route names, derived from the next required app's own declared
    PluginAuthenticationKind (plugin_platform/contracts.py)."""

    service = RecordingLifecycleService()

    async def canonical_status(plugin_id: str):
        return _connection_snapshot(plugin_id, state="setup_required", next_required_app_id="mail")

    service.status = canonical_status
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            required_apps=(_app_requirement("mail", authentication_kind),),
            optional_apps=(),
        )
    )
    service.plugin_registry = SimpleNamespace(reverify_installed=lambda plugin_id: verified)
    service.catalog_source = _marketplace_catalog_source
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())

    response = client.get("/api/plugins/office.bundle/connection")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["revision"] == service.revision
    assert body["setup_action"] == expected_action
    assert body["next_required_app_id"] == "mail"
    assert body["state"] == "setup_required"
    if expected_action != "secret_form":
        assert "secret_fields" not in body


def test_connection_status_route_reports_connected_when_no_app_needs_setup(monkeypatch) -> None:
    service = RecordingLifecycleService()

    async def canonical_status(plugin_id: str):
        return _connection_snapshot(plugin_id, state="connected", next_required_app_id=None)

    service.status = canonical_status
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())

    response = client.get("/api/plugins/office.bundle/connection")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["setup_action"] == "connected"
    assert body["state"] == "connected"


def test_connection_status_route_reports_connected_for_a_blocked_plugin_with_no_incomplete_app(
    monkeypatch,
) -> None:
    """A plugin blocked for a reason unrelated to any incomplete required app
    (no next_required_app_id) has no app for the generic setup driver to
    act on -- the panel's own per-state UI (not this route) owns rendering
    blocked/offline/permission_changed, so this route still reports
    `setup_action: 'connected'` rather than inventing a sixth action."""

    service = RecordingLifecycleService()

    async def canonical_status(plugin_id: str):
        return PluginLifecycleSnapshot._from_registry_blocker(
            plugin_id=plugin_id,
            registry_revision="a" * 64,
            package_version="1.0.0",
            apps=(),
            next_required_app_id=None,
            enabled=True,
        )

    service.status = canonical_status
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())

    response = client.get("/api/plugins/office.bundle/connection")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["state"] == "blocked"
    assert body["setup_action"] == "connected"


def test_connection_status_route_declared_secret_fields_carries_only_non_secret_metadata(
    monkeypatch,
) -> None:
    service = RecordingLifecycleService()

    async def canonical_status(plugin_id: str):
        return _connection_snapshot(plugin_id, state="setup_required", next_required_app_id="crm")

    service.status = canonical_status
    fields = (
        _secret_field("api_key", "API key", secret=True, required=True),
        _secret_field("account", "Account name", secret=False, required=False),
    )
    verified = SimpleNamespace(
        manifest=SimpleNamespace(
            required_apps=(_app_requirement("crm", "declared_secret_fields", fields),),
            optional_apps=(),
        )
    )
    service.plugin_registry = SimpleNamespace(reverify_installed=lambda plugin_id: verified)
    service.catalog_source = _marketplace_catalog_source
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())

    response = client.get("/api/plugins/office.bundle/connection")

    assert response.status_code == 200
    body = response.json()
    assert body["setup_action"] == "secret_form"
    assert body["secret_fields"] == [
        {"field_id": "api_key", "label": "API key", "secret": True, "required": True},
        {"field_id": "account", "label": "Account name", "secret": False, "required": False},
    ]
    # Never a secret value -- only the declared field's own non-secret metadata.
    payload_text = json.dumps(body)
    assert "sk-" not in payload_text
    assert "password" not in payload_text.lower()


def test_connection_status_route_fails_closed_when_the_next_app_cannot_be_verified(
    monkeypatch,
) -> None:
    """A verification hiccup here is not best-effort like the details/installed
    routes' enrichments (task 3/4): setup_action is the entire reason this
    route exists, so an unreadable manifest fails the whole response with the
    same stable, provider-neutral code the client itself uses for an
    unrecognized action."""

    service = RecordingLifecycleService()

    async def canonical_status(plugin_id: str):
        return _connection_snapshot(plugin_id, state="setup_required", next_required_app_id="mail")

    service.status = canonical_status

    def _unreadable(plugin_id: str):
        raise RuntimeError("package_missing")

    service.plugin_registry = SimpleNamespace(reverify_installed=_unreadable)
    service.catalog_source = _marketplace_catalog_source
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())

    response = client.get("/api/plugins/office.bundle/connection")

    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "plugin_connection_state_invalid"}


def test_connection_status_route_maps_plugin_not_installed(monkeypatch) -> None:
    service = RecordingLifecycleService()

    async def not_installed(plugin_id: str):
        raise ValueError("plugin_not_installed")

    service.status = not_installed
    monkeypatch.setattr(hud_server, "get_default_plugin_lifecycle_service", lambda: service)
    client = TestClient(create_app())

    response = client.get("/api/plugins/office.bundle/connection")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "plugin_not_installed"}

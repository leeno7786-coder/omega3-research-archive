from __future__ import annotations

import asyncio
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import httpx
import pytest

import hud.server as hud_server
from hud.server import create_app
from mcp_platform.plugin_authority import PluginExecutionPolicy
from pressure_loop.plugin_continuations import PluginPermissionDecision


ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"
PLUGIN_ID = "google.workspace.drive"
APP_ID = "google.workspace.drive.mcp"
REQUEST_ID = "a" * 48
REVISION = "b" * 64
TASK_ID = "logical-plugin-task"
OFFERED_SCOPES = (
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/drive.readonly",
)
OFFERED_CAPABILITIES = ("tool:search_files", "tool:create_file")
SCHEMA_SEARCH = "c" * 64
SCHEMA_CREATE = "d" * 64
HUD_ORIGIN = {"origin": "http://127.0.0.1:13310"}
FORBIDDEN = (
    "access_token",
    "refresh_token",
    "client_secret",
    "authorization: bearer",
    "callback_uri",
    "ya29.",
    r"C:\Users\leeno",
    "secret-argument-value",
    "https://oauth.example/callback",
)


@pytest.mark.parametrize("reason", [
    "mcp_app_registry_unreadable",
    "plugin_registry_unreadable",
    "plugin_registry_reconciliation_failed",
    "plugin_legacy_recovery_required",
])
def test_saved_plugin_state_failures_are_actionable_and_redacted(reason):
    response = hud_server._plugin_lifecycle_error(ValueError(reason + ": private detail"))
    assert response.status_code == 409
    assert json.loads(response.body) == {"ok": False, "error": reason}


@pytest.mark.parametrize("reason", [
    "plugin_execution_policy_invalid",
    "plugin_scopes_invalid",
    "plugin_capability_scope_invalid",
])
def test_connection_form_validation_failures_are_the_operators_to_fix(reason):
    """The connection route maps through the lifecycle mapper. An invalid form choice is
    a 422 the HUD explains, never the 503 that says the plugin service is down."""
    response = hud_server._plugin_lifecycle_error(ValueError(reason))
    assert response.status_code == 422
    assert json.loads(response.body) == {"ok": False, "error": reason}


def _hud_dict() -> dict[str, object]:
    return {
        "request_id": REQUEST_ID,
        "revision": REVISION,
        "display": {
            "plugin_apps": [
                {
                    "plugin_id": PLUGIN_ID,
                    "plugin_name": "Workspace Drive",
                    "app_id": APP_ID,
                    "app_name": "Workspace Drive Mcp",
                }
            ],
            "effect_summary": "Create data",
            "ordered_operations": ["Create file"],
            "audience": "{}",
            "destination": "{}",
            "crosses_providers": False,
            "field_names": ["name"],
            "risks": ["write"],
        },
    }


class _PoisonedRequest:
    access_token = "ya29.secret"
    refresh_token = "1//secret"
    plan = {"arguments": {"body": "secret-argument-value"}}
    callback_url = "https://oauth.example/callback"

    def to_hud_dict(self) -> dict[str, object]:
        return _hud_dict()


class _FakePermissionStore:
    def __init__(self) -> None:
        self.approve_calls: list[tuple[str, str]] = []
        self.cancel_calls: list[tuple[str, str]] = []
        self.invalidate_calls: list[str] = []
        self.pending_requests: tuple[object, ...] = ()

    def pending(self) -> tuple[object, ...]:
        return self.pending_requests

    def approve(self, request_id: str, *, expected_revision: str):
        self.approve_calls.append((request_id, expected_revision))
        raise AssertionError("HUD must not approve a grant")

    def cancel(self, request_id: str, *, expected_revision: str) -> None:
        self.cancel_calls.append((request_id, expected_revision))
        raise AssertionError("HUD must not cancel a grant")

    def invalidate_plugin(self, plugin_id: str) -> int:
        self.invalidate_calls.append(plugin_id)
        return 1

    def resolve_continuation_lineage(self, request_id: str, expected_revision: str):
        if expected_revision != REVISION:
            raise ValueError("plugin_permission_revision_conflict")
        if request_id != REQUEST_ID:
            raise ValueError("plugin_permission_request_not_found")
        return SimpleNamespace(
            prior_episode_id="episode-before-permission",
            task_id=TASK_ID,
            plan_id="e" * 64,
            plan_digest="f" * 64,
            revision=REVISION,
            status="pending",
        )


class _FakeConnections:
    def __init__(self) -> None:
        self.starts: list[dict[str, object]] = []
        self.provider_invokes = 0
        self.selected_scopes = list(OFFERED_SCOPES)
        self.execution_policy = PluginExecutionPolicy.CONFIRM_EACH
        self.capability_ceiling: list[str] = list(OFFERED_CAPABILITIES)
        self.authority_revision = REVISION
        self.status_name = "authorized"
        self.start_status = "authorized"
        self.authorization_url = None

    async def status(self, plugin_id: str, *, app_id: str, account_key: str):
        del account_key
        return SimpleNamespace(
            plugin_id=plugin_id,
            app_id=app_id,
            account_key_sha256="1" * 64,
            status=self.status_name,
            provider_scopes=tuple(self.selected_scopes),
            execution_policy=self.execution_policy,
            authority_revision=self.authority_revision,
            capability_ceiling=tuple(self.capability_ceiling),
            allowed_tool_schemas={
                "search_files": SCHEMA_SEARCH,
                "create_file": SCHEMA_CREATE,
            },
            source_constraints={},
            destination_constraints={},
        )

    async def start(self, plugin_id: str, **kwargs: object):
        self.starts.append({"plugin_id": plugin_id, **kwargs})
        scopes = kwargs.get("scopes")
        if scopes is not None:
            self.selected_scopes = list(scopes)
        policy = kwargs.get("execution_policy")
        if policy is not None:
            self.execution_policy = PluginExecutionPolicy(policy)
        ceiling = kwargs.get("capability_ceiling")
        if ceiling is not None:
            self.capability_ceiling = list(ceiling)
        self.authority_revision = "9" * 64
        if tuple(self.selected_scopes) != OFFERED_SCOPES:
            self.start_status = "authorization_required"
            self.authorization_url = "https://accounts.google.com/o/oauth2/auth?state=s"
            self.status_name = "authorization_required"
        return SimpleNamespace(
            plugin_id=plugin_id,
            status=self.start_status,
            authorization_url=self.authorization_url,
            to_dict=lambda: {
                "plugin_id": plugin_id,
                "status": self.start_status,
                "authorization_url": self.authorization_url,
                "provider_scopes": list(self.selected_scopes),
                "execution_policy": self.execution_policy.value,
                "capability_ceiling": list(self.capability_ceiling),
                "authority_revision": self.authority_revision,
            },
        )


class _FakeLifecycle:
    revision = "9" * 64

    def __init__(self) -> None:
        self.connection_service = _FakeConnections()
        self.plugin_runtime = SimpleNamespace(calls=0, invoke_tool=self._invoke)

    def _invoke(self, *_args: object, **_kwargs: object) -> None:
        self.plugin_runtime.calls += 1
        self.connection_service.provider_invokes += 1
        raise AssertionError("HUD must not invoke a provider")

    @staticmethod
    def operator_account_key() -> str:
        return "account-1"

    @staticmethod
    def app_ids_for_plugin(plugin_id: str) -> tuple[str, ...]:
        if plugin_id != PLUGIN_ID:
            return ()
        return (APP_ID,)

    @staticmethod
    def connection_handoff(plugin_id: str) -> dict[str, object]:
        del plugin_id
        return {"scopes": list(OFFERED_SCOPES)}

    @staticmethod
    def descriptor_for_app(app_id: str) -> SimpleNamespace:
        del app_id
        return SimpleNamespace(
            declared_capabilities={
                "tool:search_files": SCHEMA_SEARCH,
                "tool:create_file": SCHEMA_CREATE,
            }
        )

    @staticmethod
    def has_standing_grant(*_args: object, **_kwargs: object) -> bool:
        return False


def _client(monkeypatch, lifecycle=None, store=None, enqueue=None):
    lifecycle = lifecycle or _FakeLifecycle()
    store = store or _FakePermissionStore()
    queued: list[tuple[PluginPermissionDecision, int]] = []

    def _enqueue(decision, *, arrival_seq: int) -> bool:
        queued.append((decision, arrival_seq))
        return True

    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: lifecycle,
    )
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_permission_store",
        lambda: store,
        raising=False,
    )
    monkeypatch.setattr(
        hud_server,
        "enqueue_plugin_permission_decision",
        enqueue or _enqueue,
        raising=False,
    )
    monkeypatch.setattr(hud_server, "next_arrival_seq", lambda: 7)
    monkeypatch.delenv("OMEGA_HUD_HOST", raising=False)
    monkeypatch.delenv("OMEGA_HUD_PORT", raising=False)
    client = TestClient(create_app(), base_url="http://127.0.0.1:13310")
    return client, lifecycle, store, queued


def _serialized(response) -> str:
    return json.dumps(response.json()).lower() + response.text.lower()


def test_permission_mutations_are_closed_and_same_origin(monkeypatch) -> None:
    client, lifecycle, store, queued = _client(monkeypatch)
    path = f"/api/plugins/permissions/{REQUEST_ID}/approve"

    extra = client.post(
        path,
        json={"expected_revision": REVISION, "token": "nope"},
        headers=HUD_ORIGIN,
    )
    missing = client.post(path, json={}, headers=HUD_ORIGIN)
    evil = client.post(
        path,
        json={"expected_revision": REVISION},
        headers={"origin": "http://evil.example:13310"},
    )

    assert extra.status_code == 422
    assert missing.status_code == 422
    assert evil.status_code == 403
    assert queued == []
    assert store.approve_calls == []
    assert lifecycle.plugin_runtime.calls == 0


def test_permission_approve_is_queue_only_and_returns_original_task(monkeypatch) -> None:
    client, lifecycle, store, queued = _client(monkeypatch)
    ignited: list[str] = []
    monkeypatch.setattr(
        hud_server,
        "handle_operator_chat",
        lambda *args, **kwargs: ignited.append("opened"),
        raising=False,
    )

    response = client.post(
        f"/api/plugins/permissions/{REQUEST_ID}/approve",
        json={"expected_revision": REVISION},
        headers=HUD_ORIGIN,
    )

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "status": "queued",
        "request_id": REQUEST_ID,
        "task_id": TASK_ID,
    }
    assert len(queued) == 1
    decision, arrival_seq = queued[0]
    assert arrival_seq == 7
    assert decision == PluginPermissionDecision(
        request_id=REQUEST_ID,
        expected_revision=REVISION,
        decision="allow",
    )
    assert store.approve_calls == []
    assert ignited == []
    assert lifecycle.plugin_runtime.calls == 0


def test_permission_approve_requires_exact_revision(monkeypatch) -> None:
    client, _lifecycle, store, queued = _client(monkeypatch)

    response = client.post(
        f"/api/plugins/permissions/{REQUEST_ID}/approve",
        json={"expected_revision": "0" * 64},
        headers=HUD_ORIGIN,
    )

    assert response.status_code == 409
    assert response.json() == {"ok": False, "error": "plugin_permission_revision_conflict"}
    assert queued == []
    assert store.approve_calls == []


def test_permission_cancel_queues_denial_without_provider_call(monkeypatch) -> None:
    client, lifecycle, store, queued = _client(monkeypatch)

    response = client.post(
        f"/api/plugins/permissions/{REQUEST_ID}/cancel",
        json={"expected_revision": REVISION},
        headers=HUD_ORIGIN,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "queued"
    assert queued[0][0].decision == "cancel"
    assert store.cancel_calls == []
    assert lifecycle.connection_service.provider_invokes == 0
    assert lifecycle.plugin_runtime.calls == 0


def test_pending_permissions_project_only_hud_dict(monkeypatch) -> None:
    store = _FakePermissionStore()
    store.pending_requests = (_PoisonedRequest(),)
    client, _lifecycle, _store, _queued = _client(monkeypatch, store=store)

    response = client.get("/api/plugins/permissions/pending")

    assert response.status_code == 200
    assert response.json() == {"ok": True, "requests": [_hud_dict()]}
    serialized = _serialized(response)
    for token in FORBIDDEN:
        assert token.lower() not in serialized


@pytest.mark.anyio
async def test_pending_permissions_read_never_blocks_the_hud_event_loop(
    monkeypatch,
) -> None:
    store = _FakePermissionStore()
    entered = threading.Event()
    release = threading.Event()

    def blocking_pending() -> tuple[object, ...]:
        entered.set()
        assert release.wait(2.0)
        return ()

    store.pending = blocking_pending  # type: ignore[method-assign]
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        _FakeLifecycle,
    )
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_permission_store",
        lambda: store,
        raising=False,
    )
    app = create_app()
    timer = threading.Timer(0.3, release.set)
    timer.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1:13310",
        ) as client:
            pending = asyncio.create_task(
                client.get("/api/plugins/permissions/pending")
            )
            loop = asyncio.get_running_loop()
            started = loop.time()
            assert await asyncio.to_thread(entered.wait, 2.0)
            assert loop.time() - started < 0.15
            release.set()
            response = await pending
        assert response.status_code == 200
        assert response.json() == {"ok": True, "requests": []}
    finally:
        release.set()
        timer.cancel()


def test_permission_failures_are_stable_and_content_free(monkeypatch) -> None:
    store = _FakePermissionStore()

    def _fail(request_id: str, expected_revision: str):
        del request_id, expected_revision
        raise ValueError("plugin_permission_expired: secret-argument-value")

    store.resolve_continuation_lineage = _fail  # type: ignore[method-assign]
    client, _lifecycle, _store, queued = _client(monkeypatch, store=store)

    expired = client.post(
        f"/api/plugins/permissions/{REQUEST_ID}/approve",
        json={"expected_revision": REVISION},
        headers=HUD_ORIGIN,
    )
    assert expired.status_code == 409
    assert expired.json() == {"ok": False, "error": "plugin_permission_expired"}
    assert "secret-argument-value" not in expired.text
    assert queued == []


def test_settings_source_has_user_reachable_connection_review() -> None:
    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    assert "/permissions" in source
    assert "data-plugin-execution-policy" in source
    assert "data-plugin-capability" in source
    assert "data-plugin-provider-scope" in source
    assert "disabled" in source
    # permissionsEditorMarkup (the permission editor's own control markup:
    # the execution-policy radio labels and the explicit autonomous-authority status,
    # and the read-only network/filesystem info block) moved to
    # core/plugin-views.js (follow-up refactor wave W1) -- the panel's
    # loadAuthority still calls it and reads the checked/selected values
    # back out of the same rendered form via the data-plugin-* selectors
    # asserted above, which is why those still read the panel.
    views_source = (UI_SRC / "core" / "plugin-views.js").read_text(encoding="utf-8")
    assert "run selected scopes directly in chat" in views_source
    assert "ask before changes (reads run directly)" in views_source
    assert "data-plugin-autonomous" not in views_source
    assert "Unattended actions are unavailable because standing job grants are not supported." in views_source
    # Task 6: the permission editor also covers network and filesystem
    # scope (read-only, sourced from the same installed_authority
    # projection the Details drawer already renders) alongside the
    # editable provider/capability scope above.
    assert "data-plugin-scope-readonly" in views_source
    assert "Network access" in views_source
    assert "Filesystem access" in views_source


def test_uninstall_and_disconnect_are_revision_safe_mutations() -> None:
    """Task-6 controller ruling 2: disconnect, uninstall, and the
    permissions PUT route all require and check the client's held lifecycle
    revision before mutating anything -- a stale revision is the same
    stable plugin_revision_conflict cause review/install/update already
    use (task-4 controller ruling 2), not a route-specific one."""

    source = (ROOT / "hud" / "server.py").read_text(encoding="utf-8")
    disconnect_route = source[source.index('@app.post("/api/plugins/{plugin_id}/disconnect")'):]
    disconnect_route = disconnect_route[:disconnect_route.index("@app.post", 1)]
    assert 'allowed=frozenset({"revision"})' in disconnect_route
    assert 'required=frozenset({"revision"})' in disconnect_route
    assert "_plugin_revision_conflict()" in disconnect_route

    uninstall_route = source[source.index('@app.post("/api/plugins/{plugin_id}/uninstall")'):]
    uninstall_route = uninstall_route[:uninstall_route.index("@app.post", 1)]
    assert 'allowed=frozenset({"revision"})' in uninstall_route
    assert 'required=frozenset({"revision"})' in uninstall_route
    assert "_plugin_revision_conflict()" in uninstall_route

    permissions_put_route = source[source.index('@app.put("/api/plugins/{plugin_id}/permissions")'):]
    permissions_put_route = permissions_put_route[:permissions_put_route.index("@app.", 1)]
    assert "_plugin_revision_conflict()" in permissions_put_route

    # The client-side counterpart: plugin-api.js now places revision on the
    # wire for disconnect/uninstall (it already did for the PUT above).
    api_source = (UI_SRC / "core" / "plugin-api.js").read_text(encoding="utf-8")
    assert "disconnect: (pluginId, revision) => requestJson(pluginPath(pluginId, '/disconnect'), mutation({revision}))" in api_source
    assert "uninstall: (pluginId, revision) => requestJson(pluginPath(pluginId, '/uninstall'), mutation({revision}))" in api_source


def test_manifest_registers_plugin_permissions_module() -> None:
    manifest = json.loads((UI_SRC / "manifest.json").read_text(encoding="utf-8"))
    files = [item["file"] for item in manifest["core"]]
    assert "core/plugin-permissions.js" in files
    assert (UI_SRC / "core" / "plugin-permissions.js").is_file()
    entry = next(item for item in manifest["core"] if item["file"] == "core/plugin-permissions.js")
    assert entry.get("inject") == "chat_feed"


def test_chat_socket_copies_only_hud_dict_onto_reply(monkeypatch) -> None:
    from hud import chat_socket

    chat_socket._reset_for_tests()
    seen: list[dict] = []
    chat_socket.subscribe(seen.append)
    hud = _hud_dict()
    delivered = chat_socket.publish_reply(
        {
            "cycle_id": TASK_ID,
            "episode_id": "episode-1",
            "text": "Permission required",
            "plugin_permission_request": hud,
            "access_token": "ya29.secret",
            "plan": {"arguments": {"body": "secret-argument-value"}},
        }
    )

    assert delivered == 1
    assert len(seen) == 1
    assert seen[0]["kind"] == "chat.reply"
    assert seen[0]["plugin_permission_request"] == hud
    serialized = json.dumps(seen[0]).lower()
    for token in FORBIDDEN:
        assert token.lower() not in serialized
    chat_socket._reset_for_tests()

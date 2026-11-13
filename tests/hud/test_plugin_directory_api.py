from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import hud.server as hud_server
import pressure_loop.cognition_queue as operator_queue
from hud.server import create_app
from mcp_platform.plugin_directory import PluginDirectoryError
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from pressure_loop.contributors import make_contributor


def test_plugin_directory_exposes_authoritative_provider_source(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lifecycle = PluginLifecycleService(state_root=tmp_path)
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: lifecycle,
    )
    response = TestClient(create_app()).get("/api/plugins/catalog")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    by_id = {item["plugin_id"]: item for item in payload["plugins"]}
    assert "google.workspace.drive" in by_id
    drive = by_id["google.workspace.drive"]
    assert drive["publisher_id"] == "google"
    assert drive["publisher_display_name"] == "Google"
    assert drive["package_publisher_id"] == "omega.first_party"
    assert drive["provider_id"] == "google.workspace.drive"
    assert drive["remote_origin"] == "https://drivemcp.googleapis.com/mcp/v1"
    assert drive["authoritative_source_url"] == (
        "https://developers.google.com/workspace/drive/api/guides/configure-mcp-server"
    )
    assert drive["provenance"] == "authoritative_remote_mcp"
    assert drive["required_app_ids"] == ["google.workspace.drive.mcp"]


def test_installed_plugins_expose_verified_provider_identity_for_hud_icons(
    tmp_path: Path,
    monkeypatch,
) -> None:
    lifecycle = PluginLifecycleService(state_root=tmp_path)
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: lifecycle,
    )
    app = create_app()

    record = SimpleNamespace(plugin_id="google.workspace.drive", selected_version="1.0.0")

    class _Registry:
        def snapshot(self):
            return SimpleNamespace(records=(record,))

        def reverify_installed(self, plugin_id: str):
            raise ValueError("not needed for provider identity")

    class _Lifecycle:
        revision = "a" * 64
        plugin_registry = _Registry()

        async def status(self, plugin_id: str):
            return SimpleNamespace(
                to_dict=lambda: {
                    "plugin_id": plugin_id,
                    "state": "connected",
                    "registry_revision": "b" * 64,
                    "package_version": "1.0.0",
                    "apps": [],
                    "next_required_app_id": None,
                    "enabled": True,
                    "error_code": None,
                }
            )

    class _Directory:
        def list_directory(self):
            return SimpleNamespace(
                plugins=(
                    SimpleNamespace(
                        plugin_id="google.workspace.drive",
                        to_dict=lambda: {
                            "plugin_id": "google.workspace.drive",
                            "publisher_id": "google",
                            "publisher_display_name": "Google",
                            "provider_id": "google.workspace.drive",
                            "provenance": "authoritative_remote_mcp",
                        },
                    ),
                )
            )

    app.state.plugin_lifecycle = _Lifecycle()
    app.state.plugin_directory = _Directory()

    response = TestClient(app).get("/api/plugins/installed")

    assert response.status_code == 200
    installed = response.json()["plugins"]
    assert installed == [
        {
            "plugin_id": "google.workspace.drive",
            "state": "connected",
            "registry_revision": "b" * 64,
            "package_version": "1.0.0",
            "apps": [],
            "next_required_app_id": None,
            "enabled": True,
            "error_code": None,
            "publisher_id": "google",
            "publisher_display_name": "Google",
            "provider_id": "google.workspace.drive",
            "provenance": "authoritative_remote_mcp",
        }
    ]


class _MinimalLifecycle:
    revision = "a" * 64

    def __init__(self) -> None:
        # Every method call this double receives is recorded here, verbatim,
        # in call order -- the "recording lifecycle double's operation log"
        # task-7 controller ruling 3 asks the API test to inspect: proof
        # that POST .../try-in-chat reaches exactly one lifecycle operation
        # (its own) and never an MCP-submitting one such as
        # start_connection/poll_connection.
        self.operations: list[str] = []

    def try_in_chat(self, plugin_id: str):
        self.operations.append("try_in_chat")
        return SimpleNamespace(
            to_public_dict=lambda: {
                "plugin_id": plugin_id,
                "title": "Northwind Docs",
                "intro_text": (
                    "Northwind Docs is connected. I can help you:\n\n"
                    "- Find the launch brief.\n\n"
                    "Choose an option or describe what you want to do."
                ),
            }
        )

    async def start_connection(self, *args: object, **kwargs: object) -> None:
        # Never legitimately reachable from try-in-chat (PluginLifecycleService.
        # try_in_chat is a pure, synchronous directory read -- see
        # mcp_platform/plugin_lifecycle.py). Present only so an accidental
        # call from a future regression shows up in `operations` as a
        # concrete, named MCP-submitting operation instead of an opaque
        # AttributeError.
        self.operations.append("start_connection")
        raise AssertionError("try-in-chat must never start an MCP connection")

    def close(self) -> None:
        pass


class _ChatDirectory:
    def __init__(self) -> None:
        self.selected: list[str] = []

    def chat_selector(self, plugin_id: str) -> dict[str, str]:
        self.selected.append(plugin_id)
        return {"plugin_id": plugin_id}

    def try_in_chat_handoff(self, plugin_id: str):
        return SimpleNamespace(
            to_public_dict=lambda: {
                "plugin_id": plugin_id,
                "title": "Northwind Docs",
                "intro_text": (
                    "Northwind Docs is connected. I can help you:\n\n"
                    "- Find the launch brief.\n\n"
                    "Choose an option or describe what you want to do."
                ),
            }
        )


def _chat_app(monkeypatch):
    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: _MinimalLifecycle(),
    )
    app = create_app()
    directory = _ChatDirectory()
    app.state.plugin_directory = directory
    queued: dict[str, object] = {}

    def handle(text: str, **kwargs: object) -> dict[str, object]:
        queued["text"] = text
        queued.update(kwargs)
        return {"ok": True, "queued": True, "cycle_id": "cycle-plugin"}

    monkeypatch.setattr("pressure_loop.operator_chat.handle_operator_chat", handle)
    return app, directory, queued


def test_chat_plugin_selector_derives_privileged_operation_server_side(monkeypatch) -> None:
    app, directory, queued = _chat_app(monkeypatch)

    response = TestClient(app).post(
        "/api/chat",
        json={"text": "Find the launch brief", "plugin_id": "northwind.docs"},
    )

    assert response.status_code == 200
    assert directory.selected == ["northwind.docs"]
    assert queued["plugin_operation"] == {"plugin_id": "northwind.docs"}


def test_try_in_chat_returns_a_non_executing_capability_introduction(monkeypatch) -> None:
    app, directory, queued = _chat_app(monkeypatch)
    client = TestClient(app)

    response = client.post("/api/plugins/northwind.docs/try-in-chat", json={})

    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "revision": "a" * 64,
        "plugin_id": "northwind.docs",
        "title": "Northwind Docs",
        "intro_text": (
            "Northwind Docs is connected. I can help you:\n\n"
            "- Find the launch brief.\n\n"
            "Choose an option or describe what you want to do."
        ),
    }
    assert directory.selected == []
    assert queued == {}


def test_try_in_chat_does_not_enqueue_or_submit_an_mcp_operation(tmp_path: Path, monkeypatch) -> None:
    """Task-7 controller ruling 3: POST .../try-in-chat is a pure, non-
    executing read. Isolate the durable operator/cognition queue
    (pressure_loop/operator_queue.py -- the same queue /api/chat enqueues
    into), seed it with one pre-existing turn so the "no change" assertion
    below proves neither an enqueue nor a drain happened (not just "still
    zero"), then confirm the recording lifecycle double's operation log
    shows exactly the one non-executing operation -- never an MCP-submitting
    one such as start_connection.
    """

    monkeypatch.setattr(operator_queue, "_queue_path", lambda: tmp_path / "operator_queue.json")
    operator_queue._reset_cache_for_tests()
    operator_queue.enqueue_input(
        make_contributor("operator", "operator_query", 1.0, {"message": "pre-existing turn", "cycle_id": "existing"})
    , operator_activity=False)
    try:
        app, _directory, queued = _chat_app(monkeypatch)
        client = TestClient(app)
        lifecycle = app.state.plugin_lifecycle
        before = len(operator_queue.peek_inputs())

        response = client.post("/api/plugins/northwind.docs/try-in-chat", json={})

        assert response.status_code == 200
        after = len(operator_queue.peek_inputs())
        assert after == before
        assert lifecycle.operations == ["try_in_chat"]
        assert queued == {}
    finally:
        operator_queue.clear_inputs()


@pytest.mark.parametrize(
    ("cause", "status_code"),
    [
        # mcp_platform/plugin_directory.py's try_in_chat_handoff (via
        # chat_selector and _introduction_options) can raise every one of
        # these PluginDirectoryError causes. Controller ruling 1: POST
        # .../try-in-chat must map each through the shared
        # _plugin_lifecycle_error/_plugin_directory_error table so they
        # reach the client verbatim with their proper status codes --
        # never collapsing to a generic 503 plugin_lifecycle_unavailable.
        ("plugin_update_required", 409),
        ("plugin_connection_required", 409),
        ("plugin_interaction_unavailable", 409),
        ("plugin_directory_entry_unavailable", 404),
    ],
)
def test_try_in_chat_maps_directory_lifecycle_causes_verbatim(
    monkeypatch, cause: str, status_code: int
) -> None:
    class _FailingLifecycle(_MinimalLifecycle):
        def try_in_chat(self, plugin_id: str):
            self.operations.append("try_in_chat")
            raise PluginDirectoryError(cause)

    monkeypatch.setattr(
        hud_server,
        "get_default_plugin_lifecycle_service",
        lambda: _FailingLifecycle(),
    )
    app = create_app()
    app.state.plugin_directory = _ChatDirectory()
    client = TestClient(app)

    response = client.post("/api/plugins/northwind.docs/try-in-chat", json={})

    assert response.status_code == status_code
    assert response.json() == {"ok": False, "error": cause}

"""HUD source-of-truth checks for the current Plugins-only surface."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MONOLITH = ROOT / "hud" / "omega_interface.html"
UI_SRC = ROOT / "hud" / "ui-src"
SERVER = ROOT / "hud" / "server.py"

#: Every symbol a retired Featured HUD path would have to reintroduce.
RETIRED = (
    "FeaturedPluginService",
    "get_default_featured_plugin_service",
    "_FEATURED_PACKAGES",
    "resolve_authoritative_connector",
    "GOOGLE_DRIVE_SCOPES",
    "/api/plugins/featured",
    "featured_plugin_",
)

#: The one generic route family every plugin card drives, provider-neutral.
GENERIC_PLUGIN_ROUTES = (
    "/api/plugins/catalog",
    "/api/plugins/installed",
    "/api/plugins/{plugin_id}/review",
    "/api/plugins/{plugin_id}/install",
    "/api/plugins/{plugin_id}/connection/start",
    "/api/plugins/{plugin_id}/connection/poll",
    "/api/plugins/{plugin_id}/try-in-chat",
    "/api/plugins/{plugin_id}/permissions",
    "/api/plugins/{plugin_id}/disconnect",
    "/api/plugins/{plugin_id}/uninstall",
)


def hud_source_paths() -> tuple[Path, ...]:
    """Return the bundle, the HUD server, and every current UI source module."""

    return (MONOLITH, SERVER, *sorted(UI_SRC.rglob("*.js")))


def plugin_composer_source() -> str:
    return (UI_SRC / "core" / "plugin-composer.js").read_text(encoding="utf-8")


def try_in_chat_function(source: str) -> str:
    """Return the source text of the composer's non-executing handoff
    function (``acceptTryInChat``) -- the one place a "Try in chat" click
    can reach the chat feed/composer -- so a check can look for a substring
    only within it, not anywhere else in the file (task-7 controller
    ruling 2: no ``sendChat(`` inside this function)."""

    marker = "function acceptTryInChat("
    start = source.index(marker)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError("acceptTryInChat function body is never closed")


def test_legacy_hud_trees_are_removed() -> None:
    assert not (ROOT / "hud" / "ui").is_dir()
    assert not (ROOT / "hud" / "ui-spa").is_dir()


def test_ui_source_scaffold_and_plugins_module_exist() -> None:
    assert UI_SRC.is_dir()
    assert (UI_SRC / "manifest.json").is_file()
    assert (UI_SRC / "panels" / "settings.js").is_file()
    assert (UI_SRC / "panels" / "settings-markup.js").is_file()
    assert (UI_SRC / "panels" / "settings-wire.js").is_file()
    assert (UI_SRC / "panels" / "settings-plugins.js").is_file()
    assert (UI_SRC / "core" / "plugin-composer.js").is_file()
    assert (UI_SRC / "core" / "plugin-api.js").is_file()


def test_physical_pipeline_modules_replace_lane_named_modules() -> None:
    manifest = (UI_SRC / "manifest.json").read_text(encoding="utf-8")
    assert (UI_SRC / "core" / "poll-pipelines.js").is_file()
    assert (UI_SRC / "panels" / "pipelines.js").is_file()
    assert not (UI_SRC / "core" / "poll-lanes.js").exists()
    assert not (UI_SRC / "panels" / "lanes.js").exists()
    assert "poll-pipelines" in manifest
    assert "panels/pipelines.js" in manifest
    assert "poll-lanes" not in manifest
    assert "panels/lanes.js" not in manifest


def test_manifest_registers_only_current_settings_integration_panel() -> None:
    manifest = json.loads((UI_SRC / "manifest.json").read_text(encoding="utf-8"))
    ids = {item["id"] for item in manifest["panels"]}
    assert "settings_plugins" in ids
    assert not ids.intersection(
        {"settings_connectors", "settings_connectivity", "settings_workspace"}
    )


def test_settings_source_has_no_retired_integration_views() -> None:
    markup = (UI_SRC / "panels" / "settings-markup.js").read_text(encoding="utf-8")
    wire = (UI_SRC / "panels" / "settings-wire.js").read_text(encoding="utf-8")
    for value in ("connectors", "connectivity"):
        assert f'data-settings-view="{value}"' not in markup
        assert f'data-settings-view-panel="{value}"' not in markup
        assert f"id === '{value}'" not in wire
    assert 'data-settings-view="workspace"' in markup
    assert 'data-settings-view-panel="workspace"' in markup


def test_cloud_model_settings_use_the_portable_provider_catalog() -> None:
    markup = (UI_SRC / "panels" / "settings-markup.js").read_text(encoding="utf-8")
    wire = (UI_SRC / "panels" / "settings-wire.js").read_text(encoding="utf-8")
    server = SERVER.read_text(encoding="utf-8")

    assert 'data-settings-view="cloud_model"' in markup
    assert 'data-settings-view-panel="cloud_model"' in markup
    assert "wireCloudProviderSettingsForm" in wire
    assert "/api/settings/cloud-providers" in server
    assert "/api/settings/mind-backend/test" in server
    assert 'id="mind-backend-save-key"' in markup
    assert "/api/settings/mind-backend/key" in server
    assert "/api/settings/mind-backend/key" in wire
    assert "saved_key_providers" in wire
    assert "renderModels(provider);\n    keyInput.value = '';\n    currentHasKey" in wire
    assert "autocomplete=\"new-password\"" in markup


def test_plugins_panel_is_signed_api_only() -> None:
    """The panel now calls through the isolated OmegaPluginApi client rather
    than naming raw ``/api/plugins/*`` routes at its own call sites; the
    client module owns the exact route family (see
    ``test_plugin_marketplace_ui.py::test_plugin_api_uses_only_universal_routes``).
    """
    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    api_source = (UI_SRC / "core" / "plugin-api.js").read_text(encoding="utf-8")
    assert "OmegaPluginApi" in source
    assert "api.catalog(" in source
    assert "api.installed(" in source
    assert "/api/plugins/catalog" in api_source
    assert "/api/plugins/installed" in api_source
    assert "/api/plugins/directory" not in source
    assert "/api/plugins/featured" not in source
    assert "/api/connectors" not in source
    assert "/api/workspace" not in source
    assert "review signature" in source
    assert "authorization_required" in source


def test_chat_wiring_has_one_brain_input_path() -> None:
    source = (UI_SRC / "core" / "chat-wiring.js").read_text(encoding="utf-8")
    assert "fetch('/api/chat'" in source
    assert "connectors/status" not in source
    assert "workspace_alignment" not in source
    assert "question_id" not in source


def test_generated_bundle_is_current_and_plugins_only() -> None:
    html = MONOLITH.read_text(encoding="utf-8")
    assert "settings-plugins.js" in html
    assert "settings-connectors.js" not in html
    assert "settings-connectivity.js" not in html
    assert "settings-workspace.js" not in html
    assert 'data-settings-view="plugins"' in html


def test_generated_bundle_does_not_expose_native_integration_routes() -> None:
    html = MONOLITH.read_text(encoding="utf-8")
    assert "/api/plugins/catalog" in html
    assert "/api/plugins/installed" in html
    assert "/api/plugins/directory" not in html
    assert "/api/connectors/status" not in html
    assert "/api/workspace/alignment" not in html


def test_plugin_composer_introduces_then_sends_only_unprivileged_selection() -> None:
    source = (UI_SRC / "core" / "plugin-composer.js").read_text(encoding="utf-8")
    chat = (UI_SRC / "core" / "chat-wiring.js").read_text(encoding="utf-8")
    assert "plugin_id" in source
    assert "intro_text" in source
    assert "appendLine('assistant'" in source
    assert "input.value = '/plugin:'" in source
    assert "input.dataset.pluginId" in source
    assert "plugin_chat_ticket" not in source
    assert "app_id" not in source
    assert "capability" not in source
    assert "authority_revision" not in source
    assert "OmegaPluginComposer.decorate" not in chat


def test_chat_composer_keeps_documents_and_uploads_but_drops_image_button() -> None:
    shell = (UI_SRC / "shell.html").read_text(encoding="utf-8")
    omega_ui = (UI_SRC / "core" / "omega-ui.js").read_text(encoding="utf-8")
    bundle = MONOLITH.read_text(encoding="utf-8")

    assert 'data-composer-action="plugins"' not in shell
    assert 'data-composer-action="create-document"' in shell
    assert 'data-composer-action="upload"' in shell
    assert 'data-composer-action="create-image"' not in shell
    assert 'data-composer-action="plugins"' not in bundle
    assert "create-image" not in omega_ui
    assert "createImage" not in omega_ui
    assert "IMAGE_CREATE" not in bundle
    assert 'data-composer-action="create-image"' not in bundle


def test_settings_drops_the_created_images_library_surface() -> None:
    markup = (UI_SRC / "panels" / "settings-markup.js").read_text(encoding="utf-8")
    wire = (UI_SRC / "panels" / "settings-wire.js").read_text(encoding="utf-8")
    shell = (UI_SRC / "shell.html").read_text(encoding="utf-8")
    bundle = MONOLITH.read_text(encoding="utf-8")

    for source in (markup, wire, shell, bundle):
        assert "created_images" not in source
        assert "Created Images" not in source
        assert "created-images" not in source
        assert "_renderSettingsCreatedImages" not in source


def test_marketplace_try_in_chat_does_not_submit_or_enqueue() -> None:
    source = plugin_composer_source()
    assert "appendLine('assistant', handoff.intro_text" in source
    assert "input.value = '/plugin:'" in source
    assert "input.dataset.pluginId" in source
    assert "plugin_chat_ticket" not in source
    assert "sendChat(" not in try_in_chat_function(source)


def test_hud_sources_carry_no_retired_featured_path() -> None:
    """Catches a Featured route or service name surviving anywhere in the HUD."""

    offenders: list[str] = []
    for path in hud_source_paths():
        text = path.read_text(encoding="utf-8")
        for symbol in RETIRED:
            if symbol in text:
                offenders.append(f"{path.relative_to(ROOT).as_posix()}: {symbol}")

    assert offenders == []


def test_retired_hud_path_gate_reads_the_bundle_and_every_ui_module() -> None:
    """Catches a scan narrowed to one panel while another module keeps a route."""

    scanned = {path.relative_to(ROOT).as_posix() for path in hud_source_paths()}

    assert "hud/omega_interface.html" in scanned
    assert "hud/server.py" in scanned
    assert "hud/ui-src/panels/settings-plugins.js" in scanned
    assert "hud/ui-src/core/plugin-composer.js" in scanned


def test_hud_server_exposes_one_generic_plugin_route_family() -> None:
    """Catches a provider-shaped route returning beside the generic family."""

    source = SERVER.read_text(encoding="utf-8")

    for route in GENERIC_PLUGIN_ROUTES:
        assert f'"{route}"' in source
    assert "/api/connectors" not in source
    assert "/api/workspace" not in source
    for package in ("github.official", "google.workspace"):
        assert package not in source


def test_try_in_chat_route_maps_generic_directory_causes_not_only_lifecycle_ones() -> None:
    """Task-8 controller ruling 1: POST .../try-in-chat delegates to
    PluginLifecycleService.try_in_chat, which is a pure pass-through to
    PluginDirectoryService.try_in_chat_handoff -- a call that can raise the
    generic directory's own closed causes (plugin_connection_required,
    plugin_interaction_unavailable, plugin_directory_entry_unavailable), not
    only lifecycle ones. The route must catch through _plugin_directory_error
    (which special-cases those three and falls through to
    _plugin_lifecycle_error for the rest), and plugin_update_required --
    raised by _introduction_options for a legacy-authority package -- must be
    a real entry in the shared status map, not absent (which silently
    collapses to a generic 503). Source-level pin for the runtime proof in
    tests/hud/test_plugin_directory_api.py::test_try_in_chat_maps_directory_lifecycle_causes_verbatim."""

    source = SERVER.read_text(encoding="utf-8")
    route = source.index('@app.post("/api/plugins/{plugin_id}/try-in-chat")')
    next_route = source.index("@app.post(", route + 1)
    route_body = source[route:next_route]
    assert "_plugin_directory_error(exc)" in route_body
    assert "_plugin_lifecycle_error(exc)" not in route_body
    assert '"plugin_update_required": 409,' in source

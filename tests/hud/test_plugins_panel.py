from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UI_SRC = ROOT / "hud" / "ui-src"


def _js_code_view(source: str) -> tuple[str, dict[int, int]]:
    """Return ``source`` with every comment blanked to spaces (offsets and
    newlines preserved, so a search over it finds code only -- never prose
    that merely names a call), plus a map of every code ``{`` offset to its
    matching ``}``.

    Comment text, quoted-string text, and template-literal text are skipped
    for brace matching (a template's ``${...}`` substitutions are code), so a
    brace inside any of them can never mis-nest the real blocks. An
    unbalanced or unterminated construct fails loudly rather than yielding a
    guess.
    """

    pairs: dict[int, int] = {}
    opens: list[tuple[str, int]] = []
    length = len(source)
    code = list(source)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if code[position] != "\n":
                code[position] = " "

    def skip_template_text(index: int) -> int:
        while index < length:
            char = source[index]
            if char == "\\":
                index += 2
                continue
            if char == "`":
                return index + 1
            if source.startswith("${", index):
                opens.append(("template", index + 1))
                return index + 2
            index += 1
        raise AssertionError("unterminated template literal in JS source")

    index = 0
    while index < length:
        char = source[index]
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end == -1 else end
            blank(index, end)
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            assert end != -1, "unterminated block comment in JS source"
            blank(index, end + 2)
            index = end + 2
            continue
        if char in "'\"":
            end = index + 1
            while source[end] != char:
                assert source[end] != "\n", "unterminated string literal in JS source"
                end += 2 if source[end] == "\\" else 1
            index = end + 1
            continue
        if char == "`":
            index = skip_template_text(index + 1)
            continue
        if char == "{":
            opens.append(("code", index))
        elif char == "}":
            assert opens, f"unbalanced '}}' at offset {index} in JS source"
            kind, start = opens.pop()
            pairs[start] = index
            if kind == "template":
                index = skip_template_text(index + 1)
                continue
        index += 1
    assert not opens, "unbalanced '{' in JS source"
    return "".join(code), pairs


def _js_innermost_block(
    source: str, pairs: dict[int, int], offset: int, opener: re.Pattern[str]
) -> tuple[int, int]:
    """The innermost block containing ``offset`` whose text just before its
    ``{`` matches ``opener`` (e.g. an arrow function's ``=>`` or ``try``)."""

    enclosing = [
        (start, end)
        for start, end in pairs.items()
        if start < offset < end and opener.search(source, max(0, start - 40), start)
    ]
    assert enclosing, f"no enclosing block matching {opener.pattern!r} at offset {offset}"
    return max(enclosing)


def _js_block_header(source: str, start: int) -> str:
    return source[source.rfind("\n", 0, start) + 1 : start + 1].strip()


_ARROW_BODY = re.compile(r"=>\s*$")
_TRY_BODY = re.compile(r"\btry\s*$")
_CATCH_BLOCK = re.compile(r"\s*catch\s*(?:\([^)]*\))?\s*\{")
_FINALLY_BLOCK = re.compile(r"\s*finally\s*\{")


def test_settings_manifest_has_one_plugins_integration_panel() -> None:
    manifest = json.loads((UI_SRC / "manifest.json").read_text(encoding="utf-8"))
    settings = [item for item in manifest["panels"] if item["id"].startswith("settings_")]
    ids = {item["id"] for item in settings}
    assert "settings_plugins" in ids
    assert not ids.intersection(
        {"settings_connectors", "settings_connectivity", "settings_workspace"}
    )


def test_plugins_panel_uses_generic_plugin_directory_api_only() -> None:
    """The panel calls through the isolated OmegaPluginApi client rather than
    naming raw ``/api/plugins/*`` routes at its own call sites; the client
    module (hud/ui-src/core/plugin-api.js) owns the exact route family."""
    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    api_source = (UI_SRC / "core" / "plugin-api.js").read_text(encoding="utf-8")
    assert "OmegaPluginApi" in source
    assert "/api/plugins/catalog" in api_source
    assert "/api/plugins/installed" in api_source
    assert "/api/plugins/directory" not in source
    assert "/api/plugins/featured" not in source
    assert "/api/connectors" not in source
    assert "/api/workspace" not in source
    assert "signature" in source
    assert "authorization_required" in source


def test_plugins_panel_uses_authoritative_provider_icons() -> None:
    """Known host providers receive their own mark from signed provider
    metadata; arbitrary plugin-id text never becomes an icon class."""
    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    views_source = (UI_SRC / "core" / "plugin-views.js").read_text(encoding="utf-8")
    assert "data-plugin-id" in source
    assert "google.workspace.drive.mcp" not in source
    assert "_PLUGIN_PROVIDER_ICON_SPECS" in views_source
    assert "provider-icon-' + spec.tone" in views_source
    assert "google: {label: 'Google'" in views_source
    assert "github: {label: 'GitHub'" in views_source
    assert "root + ' ' + root + '-default" in views_source
    assert "root + ' ' + root + '-asset" in views_source


def test_plugins_panel_labels_authoritative_owner_not_host_wrapper() -> None:
    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")

    assert "publisher_display_name" in source
    assert "authoritative_source_url" in source
    # renderCard (the "Official remote MCP" provenance badge's one producer)
    # moved to core/plugin-views.js (follow-up refactor wave W1).
    views_source = (UI_SRC / "core" / "plugin-views.js").read_text(encoding="utf-8")
    assert "Official remote MCP" in views_source
    assert "package_publisher_id" not in source


def test_plugins_panel_has_directory_layout_and_clear_connection_recovery() -> None:
    """``data-plugin-scope`` is renamed to ``data-plugin-tab`` in task 3 to
    match the OmegaPluginStore's ``tab`` state field the marketplace now
    dispatches through (see task-2-report.md's state shape) -- the tab
    buttons are otherwise unchanged (role="tab", aria-selected)."""
    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    for marker in (
        "data-plugin-search",
        "data-plugin-installed-list",
        "data-plugin-tab",
        "data-plugin-public-list",
        "data-plugin-retry",
    ):
        assert marker in source
    # "Install this plugin before connecting" (plugin_not_installed's
    # recovery message) moved to core/plugin-permissions.js's shared,
    # single error vocabulary on task-6 fix round 1 (finding 2) -- the
    # panel no longer carries its own private cause table at all.
    permissions_source = (UI_SRC / "core" / "plugin-permissions.js").read_text(encoding="utf-8")
    assert "Install this plugin before connecting" in permissions_source
    # The no-store fetch directive now lives once, in the isolated client.
    api_source = (UI_SRC / "core" / "plugin-api.js").read_text(encoding="utf-8")
    assert "cache: 'no-store'" in api_source


def test_plugins_panel_layout_is_container_aware_and_expands_reviews_visibly() -> None:
    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    shell = (UI_SRC / "shell.html").read_text(encoding="utf-8")

    assert "aria-expanded" in source
    assert "scrollIntoView" in source
    # renderCard (disconnectDisabled's one producer) moved to
    # core/plugin-views.js (follow-up refactor wave W1).
    views_source = (UI_SRC / "core" / "plugin-views.js").read_text(encoding="utf-8")
    assert "disconnectDisabled" in views_source
    assert "container-type: inline-size" in shell
    assert "@container plugins-directory (max-width: 860px)" in shell
    assert "grid-column: 1 / -1" in shell


def test_oauth_popup_preserves_opener_and_callback_returns_to_plugin_settings() -> None:
    """Task 5 moves the Connect/Continue-setup button's own OAuth/device
    driving onto window.OmegaPluginSetup (core/plugin-setup.js); the popup
    reservation and "return to Omega" navigation stay in settings-plugins.js
    (a synchronous user-gesture / window-management concern, not "which
    auth flow"), passed to the setup module as `popup`/`finish` controls.
    Final fix wave W2: the permissions editor's "Save and (re)connect"
    submit path now shares this exact same popup reservation/return
    machinery (driveConnectionSetup) instead of its own private
    navigateAuthorization/completeAuthorizationStart copy, which is
    deleted -- see test_plugin_marketplace_ui.py's
    test_panel_drives_save_and_reconnect_through_the_shared_setup_machine."""

    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    setup_source = (UI_SRC / "core" / "plugin-setup.js").read_text(encoding="utf-8")
    register = (UI_SRC / "panels" / "register.js").read_text(encoding="utf-8")
    assert "omega-plugin-oauth" in source
    assert "omega-google-oauth" not in source
    assert "omega-github-oauth" not in source
    assert "noopener,noreferrer" not in source
    assert "window.open('', 'omega-plugin-oauth'" in source
    assert "omega:plugin-connected" in register
    assert 'data-settings-view="plugins"' in register
    assert "#settings/plugins" in register
    assert "DOMContentLoaded" in register
    assert "'/#settings/plugins'" in source
    assert "oauthPopup.location = omegaReturnUrl" in source
    assert "window.location.replace(omegaReturnUrl)" in source
    # The generic setup module navigates the caller-reserved popup itself
    # once it has a real authorization_url, and falls back to same-window
    # navigation when the popup was blocked or already spent -- the panel
    # itself no longer navigates a popup directly (navigateAuthorization is
    # deleted; final fix wave W2).
    assert "popup.location.replace(url)" in setup_source
    assert "navigateSameWindow" in setup_source
    assert "oauthPopup.location.replace" not in source


def test_plugins_panel_no_longer_duplicates_the_device_flow_shape() -> None:
    """Final fix wave W2: the panel's own private copy of the device-flow
    shape (`authorization_flow === 'device_code'`, polling, user_code --
    previously kept for the "Save and (re)connect" permissions-review path
    only) is deleted entirely. That path now drives the exact same
    window.OmegaPluginSetup machine Connect/Continue-setup use, so
    core/plugin-setup.js's own device-flow handling (task 5, unchanged) is
    the only implementation left."""

    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    setup_source = (UI_SRC / "core" / "plugin-setup.js").read_text(encoding="utf-8")
    api_source = (UI_SRC / "core" / "plugin-api.js").read_text(encoding="utf-8")

    assert "authorization_flow === 'device_code'" not in source
    assert "pollDeviceConnection" not in source
    assert "completeAuthorizationStart" not in source
    assert "connection/poll" in api_source
    for marker in ("user_code", "poll_interval_seconds", "expires_at", "onDeviceCode", "pollConnection"):
        assert marker in setup_source, f"plugin-setup.js device flow missing {marker!r}"
    assert "window.OmegaPluginSetup.start(" in source


def test_plugins_panel_cancels_an_in_flight_setup_through_one_shared_tracker() -> None:
    """Final fix wave W2: `activeDeviceConnections` (the "Save and
    (re)connect" path's own private in-flight tracker) is deleted --
    disconnectPlugin() now cancels an in-flight setup for either call site
    through the one shared `activeSetupControllers` map, which cancels into
    core/plugin-setup.js's own `isCancelled()`/`plugin_connection_cancelled`
    contract."""

    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    setup_source = (UI_SRC / "core" / "plugin-setup.js").read_text(encoding="utf-8")

    assert "activeDeviceConnections" not in source
    assert "plugin.status === 'authorizing'" not in source
    assert "activeSetupControllers" in source
    assert "isCancelled" in setup_source
    assert "plugin_connection_cancelled" in setup_source

    # The invariant, checked per call site rather than as a raw count: every
    # path that drives an in-flight setup must be cancellable through the one
    # shared tracker. Per-app consent (2af18bdf) moved Connect/Continue-setup,
    # and then the sign-in a confirmed update leaves behind
    # (continueSetupAfterUpdate), onto the consent form, whose submit handler
    # now drives the setup -- so the set of driving call sites changes with
    # the design, and each one that exists is checked for its own
    # registration.
    code, pairs = _js_code_view(source)

    # The setup machine runs only inside driveConnectionSetup, and that
    # helper hands the caller's cancel flag to the machine -- so a
    # registered caller's cancel() really stops the in-flight setup.
    machine_starts = [m.start() for m in re.finditer(r"OmegaPluginSetup\.start\(", code)]
    assert machine_starts
    for offset in machine_starts:
        start, end = _js_innermost_block(code, pairs, offset, _ARROW_BODY)
        assert _js_block_header(code, start).startswith(
            "const driveConnectionSetup = async (card, status, oauthPopup, initial, pluginRequestId, isCancelled) =>"
        ), "window.OmegaPluginSetup.start must run only inside driveConnectionSetup"
        assert re.search(r"\n\s*isCancelled,\n", code[offset:end]), (
            "driveConnectionSetup must pass its caller's isCancelled to the setup machine"
        )

    register = "activeSetupControllers.set(pluginId, {cancel: () => { cancelled = true; }});"
    release = "activeSetupControllers.delete(pluginId);"
    drive_sites = [m.start() for m in re.finditer(r"\bawait driveConnectionSetup\(", code)]
    assert drive_sites, "no call site drives the shared setup machine"
    assert len(drive_sites) == code.count("driveConnectionSetup("), (
        "every driveConnectionSetup call must be an "
        "awaited drive checked below"
    )
    for offset in drive_sites:
        fn_start, _fn_end = _js_innermost_block(code, pairs, offset, _ARROW_BODY)
        site = _js_block_header(code, fn_start)
        # Registered before it drives, with its own cancel flag...
        registered_at = code.find(register, fn_start, offset)
        assert registered_at != -1, f"{site}: drives a setup without registering it in activeSetupControllers"
        assert code.find("let cancelled = false;", fn_start, registered_at) != -1, (
            f"{site}: its registered cancel() must flip a flag local to this attempt"
        )
        # ...which this exact drive observes...
        call = code[offset : code.index(";", offset)]
        assert call.endswith(", () => cancelled)"), (
            f"{site}: the drive must hand the setup machine this attempt's own cancel flag"
        )
        # ...and released on every exit from the drive (success or failure).
        try_start, try_end = _js_innermost_block(code, pairs, offset, _TRY_BODY)
        assert try_start > fn_start, f"{site}: the drive is not inside its own try block"
        after_try = try_end + 1
        catch = _CATCH_BLOCK.match(code, after_try)
        if catch:
            after_try = pairs[catch.end() - 1] + 1
        final = _FINALLY_BLOCK.match(code, after_try)
        assert final, f"{site}: the try around the drive has no finally releasing the tracker"
        finally_start = final.end() - 1
        assert release in code[finally_start : pairs[finally_start]], (
            f"{site}: the finally after the drive must release the tracker entry"
        )

    # And disconnect cancels whatever is registered before it disconnects.
    disconnect_start = code.index("const disconnectPlugin = async (card, button) => {")
    disconnect_body = code[disconnect_start : pairs[code.index("{", disconnect_start)]]
    cancel_at = disconnect_body.find(
        "const activeSetup = activeSetupControllers.get(pluginId);\n"
        "    if (activeSetup) activeSetup.cancel();"
    )
    assert cancel_at != -1, "disconnectPlugin must cancel the registered in-flight setup"
    assert cancel_at < disconnect_body.index("api.disconnect("), (
        "disconnectPlugin must cancel the in-flight setup before it disconnects"
    )


def test_plugins_panel_uses_explicit_plugin_card_actions() -> None:
    source = (UI_SRC / "panels" / "settings-plugins.js").read_text(encoding="utf-8")
    # renderCard (the <article class="plugins-directory-card"> shell's one
    # producer) moved to core/plugin-views.js (follow-up refactor wave W1);
    # every other marker here is a data attribute the panel's own event
    # wiring still selects on, so this checks both files together.
    combined = source + "\n" + (UI_SRC / "core" / "plugin-views.js").read_text(encoding="utf-8")
    for marker in (
        "data-plugin-action",
        "data-plugin-disconnect",
        "data-plugin-uninstall",
        "data-plugin-review",
        "data-plugin-more",
        "data-plugin-try",
        "plugins-directory-card",
    ):
        assert marker in combined
    # The exact route paths for these card actions now live once, in the
    # isolated client (hud/ui-src/core/plugin-api.js).
    api_source = (UI_SRC / "core" / "plugin-api.js").read_text(encoding="utf-8")
    for marker in ("/disconnect", "/uninstall", "/try-in-chat"):
        assert marker in api_source


def test_settings_markup_has_plugins_view_and_no_legacy_views() -> None:
    markup = (UI_SRC / "panels" / "settings-markup.js").read_text(encoding="utf-8")
    assert 'data-settings-view="plugins"' in markup
    assert 'data-settings-view-panel="plugins"' in markup
    for value in ("connectors", "connectivity"):
        assert f'data-settings-view="{value}"' not in markup
        assert f'data-settings-view-panel="{value}"' not in markup
    # "workspace" returned as the trusted-folder grant view, not the retired sibling workspace panel.
    assert 'data-settings-view="workspace">workspace access<span>trusted folders</span>' in markup


def test_generated_hud_contains_plugins_source_and_no_legacy_panel_modules() -> None:
    html = (ROOT / "hud" / "omega_interface.html").read_text(encoding="utf-8")
    assert "settings-plugins.js" in html
    assert "settings-connectors.js" not in html
    assert "settings-connectivity.js" not in html
    assert "settings-workspace.js" not in html


def test_hud_lifecycle_routes_do_not_hardcode_drive_app_id() -> None:
    source = (ROOT / "hud" / "server.py").read_text(encoding="utf-8")
    assert "GOOGLE_DRIVE_APP_ID" not in source

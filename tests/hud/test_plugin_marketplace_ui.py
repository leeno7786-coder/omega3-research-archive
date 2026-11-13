"""Source-contract checks for the provider-neutral plugin API client."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = ROOT / "hud" / "ui-src"
MONOLITH = ROOT / "hud" / "omega_interface.html"
TESTS_HUD = ROOT / "tests" / "hud"


def plugin_panel_source() -> str:
    return (UI_ROOT / "panels" / "settings-plugins.js").read_text(encoding="utf-8")


def plugin_views_source() -> str:
    """core/plugin-views.js -- the pure catalog card/installed tile/drawer
    body/permissions editor view builders follow-up refactor wave W1 moved
    out of settings-plugins.js. Several source-contract checks below assert
    on literal text (a control's exact button/label wording, an aria
    attribute value, a CSS class, a variable name) that now lives here
    instead of (or split across) the panel -- those checks read this
    directly, or this concatenated with plugin_panel_source(), rather than
    the panel alone, so a regression in this file still fails them."""

    return (UI_ROOT / "core" / "plugin-views.js").read_text(encoding="utf-8")


def plugin_api_source() -> str:
    return (UI_ROOT / "core" / "plugin-api.js").read_text(encoding="utf-8")


def panel_arrow_function(source: str, header: str) -> str:
    """Return the source text of the panel's arrow function declared by the
    exact ``header`` (through its closing brace), so a check can look for a
    call only within that one function -- the same brace-balanced extraction
    tests/hud/test_hud_ui_modules.py's try_in_chat_function uses."""

    start = source.index(header)
    brace = start + len(header) - 1
    assert source[brace] == "{", f"{header!r} must end at its body's opening brace"
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"{header!r} function body is never closed")


def test_plugin_api_uses_only_universal_routes() -> None:
    source = (UI_ROOT / "core" / "plugin-api.js").read_text(encoding="utf-8")
    assert "OmegaPluginApi" in source
    assert "/api/plugins/catalog" in source
    assert "/api/plugins/installed" in source
    assert "/api/plugins/featured" not in source
    assert "google" not in source.lower()
    assert "github" not in source.lower()


def test_manifest_loads_plugin_api_before_settings_plugins() -> None:
    """The catalog client must exist as a global before the panel that calls it.

    ``settings_plugins`` is bundled through the ``panels`` injection marker;
    ``plugin-api`` must land through a core injection marker that the build
    places earlier in the assembled document, matching real script order.
    """

    manifest = json.loads((UI_ROOT / "manifest.json").read_text(encoding="utf-8"))
    core_files = [entry["file"] for entry in manifest["core"]]
    assert "core/plugin-api.js" in core_files
    panel_files = [entry["file"] for entry in manifest["panels"]]
    assert "panels/settings-plugins.js" in panel_files

    html = MONOLITH.read_text(encoding="utf-8")
    api_marker = "// @module core/plugin-api.js"
    panel_marker = "// @module panels/settings-plugins.js"
    assert api_marker in html
    assert panel_marker in html
    assert html.index(api_marker) < html.index(panel_marker)


def test_plugin_store_defines_the_closed_reducer_contract() -> None:
    """The state reducer must expose exactly the brief's closed event set
    and mirror mcp_platform/plugin_state.py's lifecycle states verbatim --
    it must never invent a state or a route of its own."""

    source = (UI_ROOT / "core" / "plugin-store.js").read_text(encoding="utf-8")
    assert "OmegaPluginStore" in source
    assert "create" in source
    assert "dispatch" in source
    assert "snapshot" in source
    assert "subscribe" in source

    for event_type in (
        "catalogLoading",
        "catalogLoaded",
        "catalogFailed",
        "installedLoaded",
        "tabChanged",
        "queryChanged",
        "filtersChanged",
        "detailsOpened",
        "detailsClosed",
        "mutationStarted",
        "pluginChanged",
        "mutationFailed",
    ):
        assert f"'{event_type}'" in source, f"missing closed event type {event_type}"

    lifecycle_source = (
        ROOT / "mcp_platform" / "plugin_state.py"
    ).read_text(encoding="utf-8")
    for state_name in (
        "available",
        "review_required",
        "installing",
        "installed",
        "setup_required",
        "connecting",
        "connected",
        "offline",
        "update_available",
        "update_required",
        "permission_changed",
        "revocation_pending",
        "blocked",
        "incompatible",
    ):
        assert f'"{state_name}"' in lifecycle_source, (
            f"server lifecycle state {state_name!r} moved -- update plugin-store.js to match"
        )
        assert f"'{state_name}'" in source, f"missing mirrored lifecycle state {state_name}"

    # No route family, provider table, or hardcoded OAuth flow belongs here --
    # the reducer only stores what the server already sent through
    # window.OmegaPluginApi.
    assert "/api/plugins" not in source
    assert "/featured" not in source
    assert "google" not in source.lower()
    assert "github" not in source.lower()


def test_manifest_loads_plugin_store_after_plugin_api_and_before_settings_plugins() -> None:
    manifest = json.loads((UI_ROOT / "manifest.json").read_text(encoding="utf-8"))
    core_files = [entry["file"] for entry in manifest["core"]]
    assert "core/plugin-store.js" in core_files
    panel_files = [entry["file"] for entry in manifest["panels"]]
    assert "panels/settings-plugins.js" in panel_files

    html = MONOLITH.read_text(encoding="utf-8")
    api_marker = "// @module core/plugin-api.js"
    store_marker = "// @module core/plugin-store.js"
    panel_marker = "// @module panels/settings-plugins.js"
    assert api_marker in html
    assert store_marker in html
    assert panel_marker in html
    assert html.index(api_marker) < html.index(store_marker) < html.index(panel_marker)


def test_marketplace_has_public_personal_search_filters_and_paging() -> None:
    source = plugin_panel_source()
    for marker in (
        "data-plugin-installed-list", "data-plugin-tab=\"public\"",
        "data-plugin-tab=\"personal\"", "data-plugin-search",
        "data-plugin-filter", "data-plugin-load-more",
    ):
        assert marker in source
    assert "featuredList" not in source
    assert "iconFor(plugin)" not in source


def test_catalog_icons_come_from_verified_metadata_not_provider_names() -> None:
    source = plugin_panel_source().lower()
    assert "includes('github')" not in source
    assert "includes('google')" not in source


def test_marketplace_bundle_has_accessible_tabs_labeled_search_and_focus_styles() -> None:
    """The brief asks for bundle assertions on aria-live, tab roles/selection,
    labeled search, keyboard-focus styles, and a responsive one-column
    fallback -- this is that coverage."""

    source = plugin_panel_source()
    shell = (UI_ROOT / "shell.html").read_text(encoding="utf-8")

    # aria-live regions announce catalog/installed-strip state changes.
    assert "aria-live" in source
    # Public/Personal tabs are real ARIA tabs, not just styled buttons.
    assert 'role="tablist"' in source
    assert 'role="tab"' in source
    assert "aria-selected" in source
    # The search field carries both a visible label and an aria-label.
    assert 'aria-label="Search plugins"' in source
    assert 'for="plugins-search-input"' in source
    assert 'id="plugins-search-input"' in source
    # Keyboard users get a visible focus indicator inside the directory.
    assert ".plugins-directory" in shell
    assert ":focus-visible" in shell
    # The compact two-column layout collapses to one column on narrow
    # viewports and narrow containers (panel docked in a side drawer).
    assert "container-type: inline-size" in shell
    assert "@container plugins-directory (max-width: 860px)" in shell
    assert "grid-template-columns: 1fr" in shell


def test_marketplace_catalog_search_sends_filters_to_the_api() -> None:
    """Search/filters must be sent to OmegaPluginApi.catalog(...), never
    applied only to an already-loaded page -- see task-3 controller ruling 1."""

    source = plugin_panel_source()
    assert "api.catalog(" in source
    assert "OmegaPluginStore" in source
    assert "catalogLoading" in source
    assert "catalogLoaded" in source
    assert "catalogFailed" in source
    assert "No plugins match this search" in source


def test_marketplace_panel_runtime_behavior_mjs() -> None:
    """Runs the Node runtime-behavior test for the marketplace panel so
    ``pytest`` alone (not only a separately run ``node ...``) covers the
    panel's actual rendering/search/pagination/banner/composer-sync
    behavior against a real DOM shim and the real OmegaPluginStore --
    mirrors how test_plugin_store_defines_the_closed_reducer_contract above
    covers core/plugin-store.js's contract for its sibling
    test_plugin_marketplace_state.mjs. See task-3 review finding 3."""

    script = ROOT / "tests" / "hud" / "test_plugin_marketplace_panel.mjs"
    assert script.exists()
    result = subprocess.run(
        ["node", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_marketplace_has_a_details_drawer_and_explicit_review_confirm_transition() -> None:
    """Task-4: the action button transitions Available -> Review -> Install
    (and the equivalent for Update) through one accessible drawer/dialog --
    never a browser confirm() for a permission-bearing review."""

    source = plugin_panel_source()
    # The drawer shell markup (role="dialog"/aria-modal="true") and the
    # details/trust-badge/update-diff view builders moved to
    # core/plugin-views.js (follow-up refactor wave W1); the panel still
    # wires and calls them (openPluginReview/openPluginDetails/
    # _pluginRenderDetailsPanel), so this checks the two files together --
    # the drawer/review machinery this test verifies now spans both.
    combined = source + "\n" + plugin_views_source()
    for marker in (
        "data-plugin-details",
        "data-plugin-drawer",
        'role="dialog"',
        'aria-modal="true"',
        "data-plugin-drawer-close",
        "data-plugin-review-confirm",
        "data-plugin-review-cancel",
        "_pluginDetailsSectionsMarkup",
        "_pluginTrustBadgeMarkup",
        "_pluginUpdateDiffMarkup",
        "_pluginRenderDetailsPanel",
        "openPluginReview",
        "openPluginDetails",
        "plugin_revision_conflict",
    ):
        assert marker in combined, f"missing {marker}"
    # Confirming an install/update never uses a generic browser confirm().
    # Task 6 removes the file's one remaining window.confirm() call too
    # (uninstallPlugin's destructive-action confirmation) in favor of the
    # same in-panel destructive review pattern -- see
    # test_uninstall_uses_an_in_panel_destructive_review_never_confirm below.
    assert "window.confirm(" not in source


def test_trust_labels_come_from_a_closed_map_and_never_say_official() -> None:
    """Task-4 controller ruling 3: the closed trust-tier label map, and the
    literal label text for each tier. (A pre-existing, unrelated
    "Official remote MCP" provenance badge -- task 3's authoritative-source
    marker, not a trust-tier label -- is untouched by this task and is not
    what this assertion is about.)

    _PLUGIN_TRUST_LABELS moved to core/plugin-views.js (follow-up refactor
    wave W1, alongside _pluginTrustBadgeMarkup, its one consumer) -- this
    reads that file, not the panel."""

    source = plugin_views_source()
    assert "_PLUGIN_TRUST_LABELS" in source
    assert "'Omega Verified'" in source
    assert "'Publisher verified'" in source
    assert "'Personal'" in source
    labels_block = source[source.index("_PLUGIN_TRUST_LABELS"):source.index("_PLUGIN_TRUST_LABELS") + 400]
    assert "Official" not in labels_block


def test_review_and_install_send_the_exact_digest_and_current_revision() -> None:
    """Task-4 controller ruling 4: install/update submit exactly
    review.sha256 and the client's held revision -- never a value guessed or
    recomputed client-side."""

    source = plugin_panel_source()
    assert "review.sha256" in source
    assert "knownRevision" in source
    assert "loadedReview" in source


def test_marketplace_review_runtime_behavior_mjs() -> None:
    """Runs the Node runtime-behavior test for the details/trust/review/
    install/update surfaces, mirroring test_marketplace_panel_runtime_behavior_mjs
    above for its sibling test_plugin_marketplace_review.mjs."""

    script = ROOT / "tests" / "hud" / "test_plugin_marketplace_review.mjs"
    assert script.exists()
    result = subprocess.run(
        ["node", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_plugin_setup_module_defines_the_closed_action_contract() -> None:
    """Task-5: hud/ui-src/core/plugin-setup.js drives generic multi-app
    authentication and setup through the brief's closed SETUP_ACTIONS map
    -- it must expose window.OmegaPluginSetup.start, define exactly the
    five closed actions, throw the stable code for an unrecognized one, and
    never brand or guess a provider."""

    source = (UI_ROOT / "core" / "plugin-setup.js").read_text(encoding="utf-8")
    assert "window.OmegaPluginSetup" in source
    assert "{start: start, SETUP_ACTIONS: SETUP_ACTIONS}" in source
    assert "SETUP_ACTIONS" in source
    for action in ("browser:", "device:", "secret_form:", "none:", "connected:"):
        assert action in source, f"missing closed setup action {action!r}"
    assert "plugin_connection_state_invalid" in source
    assert "next_required_app_id" in source
    assert "google" not in source.lower()
    assert "github" not in source.lower()
    assert "fetch(" not in source  # every network call goes through the caller's controls.api, never a raw fetch


def test_manifest_loads_plugin_setup_after_plugin_store_and_before_settings_plugins() -> None:
    manifest = json.loads((UI_ROOT / "manifest.json").read_text(encoding="utf-8"))
    core_files = [entry["file"] for entry in manifest["core"]]
    assert "core/plugin-setup.js" in core_files
    panel_files = [entry["file"] for entry in manifest["panels"]]
    assert "panels/settings-plugins.js" in panel_files

    html = MONOLITH.read_text(encoding="utf-8")
    store_marker = "// @module core/plugin-store.js"
    setup_marker = "// @module core/plugin-setup.js"
    panel_marker = "// @module panels/settings-plugins.js"
    assert store_marker in html
    assert setup_marker in html
    assert panel_marker in html
    assert html.index(store_marker) < html.index(setup_marker) < html.index(panel_marker)


def test_plugins_panel_drives_connect_through_the_generic_setup_module() -> None:
    """The Connect/Continue-setup button must call
    window.OmegaPluginSetup.start(...) -- never its own ad hoc
    OAuth/device-flow branching -- with a `revision`-current extended
    snapshot from GET .../connection (api.status), never a possibly-stale
    locally-tracked app id."""

    source = plugin_panel_source()
    assert "window.OmegaPluginSetup.start(" in source
    assert "activeSetupControllers" in source
    assert "api.status(pluginId)" in source


def test_panel_drives_save_and_reconnect_through_the_shared_setup_machine() -> None:
    """Final fix wave W2: the permissions editor's "Save and (re)connect"
    submit path (loadAuthority) used to drive its own ad hoc popup/device/
    postMessage implementation (completeAuthorizationStart/
    _pluginAuthorizationAction/pollDeviceConnection/navigateAuthorization,
    plus a standing panel-lifetime `message` listener) -- all deleted. It
    now re-fetches GET .../connection after a successful savePermissions
    and drives window.OmegaPluginSetup.start(...) through the exact same
    driveConnectionSetup(...) helper Connect/Continue-setup use -- there is
    exactly one setup machine. See
    tests/hud/test_plugin_permissions.mjs's "Save and (re)connect on a
    browser setup_action drives window.OmegaPluginSetup.start" runtime
    proof."""

    source = plugin_panel_source()
    assert "completeAuthorizationStart" not in source
    assert "_pluginAuthorizationAction" not in source
    assert "pollDeviceConnection" not in source
    assert "navigateAuthorization" not in source
    assert "activeDeviceConnections" not in source
    assert "_pluginCallbackMessage" not in source
    assert "_pluginCanonicalSetupTarget" not in source
    assert "_omegaPluginSettingsMessageHandler" not in source
    assert "driveConnectionSetup" in source
    # Every call site shares the one helper -- not independent copies of the
    # controls object it builds. The single `OmegaPluginSetup.start(` is the
    # invariant; the call count is just how many entry points reach it.
    assert source.count("await window.OmegaPluginSetup.start(") == 1
    # Per-app operator consent (2af18bdf): a connection starts only after the
    # operator submits that app's exact scope/capability selection, so the
    # machine is driven from exactly one place -- the consent form's submit
    # handler (Save-and-(re)connect), after savePermissions has recorded that
    # selection. Every other entry point only opens that form for its exact
    # app: Connect/Continue-setup (startConnection) and the sign-in a
    # confirmed update leaves behind (continueSetupAfterUpdate -- promoting a
    # re-signed package retires the old connection, so an authenticated
    # plugin needs authorizing again immediately afterwards).
    assert source.count("driveConnectionSetup(card, status, oauthPopup,") == 1
    load_authority = panel_arrow_function(
        source, "const loadAuthority = async (card, host, appId = null) => {"
    )
    submit_at = load_authority.index("review.addEventListener('submit', async event => {")
    assert load_authority.index(
        "await driveConnectionSetup(card, status, oauthPopup, fresh, pluginRequestId, () => cancelled);"
    ) > load_authority.index("await api.savePermissions(", submit_at)
    # A confirmed update's sign-in opens the consent form for exactly the app
    # the update named -- it never drives the machine itself.
    continue_after_update = panel_arrow_function(
        source, "const continueSetupAfterUpdate = async (pluginId, appId) => {"
    )
    assert "driveConnectionSetup(" not in continue_after_update
    assert "OmegaPluginSetup.start(" not in continue_after_update
    assert "api.status(" not in continue_after_update
    assert (
        "await loadAuthority(card, card.querySelector('[data-plugin-review-panel]'), appId);"
        in continue_after_update
    )
    confirm_review = panel_arrow_function(source, "const confirmPluginReview = async () => {")
    assert (
        "await continueSetupAfterUpdate(pluginId, updateResult.next_required_app_id);"
        in confirm_review
    )
    # Connect/Continue-setup no longer drives the machine under an implied
    # grant: it opens the next app's own consent form (loadAuthority for that
    # exact app), and composite setup returns there after every app.
    start_connection = panel_arrow_function(
        source, "const startConnection = async (card, button) => {"
    )
    assert "driveConnectionSetup(" not in start_connection
    assert "OmegaPluginSetup.start(" not in start_connection
    assert (
        "const exactAppId = card.dataset.nextPluginAppId || plugin.next_required_app_id || null;"
        in start_connection
    )
    assert (
        "await loadAuthority(card, card.querySelector('[data-plugin-review-panel]'), exactAppId);"
        in start_connection
    )
    # The machine's start call reuses only the operator's exact selection for
    # that app -- with none recorded it fails closed, never an implied grant.
    drive = panel_arrow_function(
        source,
        "const driveConnectionSetup = async (card, status, oauthPopup, initial, pluginRequestId, isCancelled) => {",
    )
    assert "const selection = perApp && perApp.get(targetAppId);" in drive
    assert "if (!selection) throw new Error('plugin_scopes_invalid');" in drive
    assert "return api.startConnection(pid, revision, targetAppId, selection.settings);" in drive


def test_marketplace_setup_runtime_behavior_mjs() -> None:
    """Runs the Node runtime-behavior test for the generic multi-app setup
    driver, mirroring test_marketplace_panel_runtime_behavior_mjs above for
    its sibling test_plugin_marketplace_setup.mjs."""

    script = ROOT / "tests" / "hud" / "test_plugin_marketplace_setup.mjs"
    assert script.exists()
    result = subprocess.run(
        ["node", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_connect_flow_sources_carry_no_provider_substring() -> None:
    """Task-5 fix round 1, finding 2: the generic setup driver and the panel
    that wires it must never brand or select behavior on a GitHub/Google
    substring anywhere in the file -- not only in one code pattern (see
    test_catalog_icons_come_from_verified_metadata_not_provider_names above,
    which checks only one specific `includes(...)` call shape). This is the
    whole-file, case-insensitive gate Task 6's brief also requires of these
    same two files."""

    setup_source = (UI_ROOT / "core" / "plugin-setup.js").read_text(encoding="utf-8").lower()
    panel_source = plugin_panel_source().lower()
    for name, source in (("plugin-setup.js", setup_source), ("settings-plugins.js", panel_source)):
        assert "github" not in source, f"{name} must not name a provider"
        assert "google" not in source, f"{name} must not name a provider"


def test_plugin_permissions_module_defines_the_lifecycle_and_error_contract() -> None:
    """Task-6 Step 1: core/plugin-permissions.js exposes the closed
    CANONICAL_PLUGIN_STATES set, lifecycleView(), and readablePluginError()
    -- test_plugin_permissions.mjs exercises them directly with the brief's
    two verbatim tests. No GitHub Device Flow sentence and no
    provider-specific error table anywhere in the file (controller
    ruling 3's own "closed, provider-neutral" requirement)."""

    source = (UI_ROOT / "core" / "plugin-permissions.js").read_text(encoding="utf-8")
    assert "CANONICAL_PLUGIN_STATES" in source
    assert "function lifecycleView(" in source
    assert "function readablePluginError(" in source
    for state in (
        "available", "review_required", "installing", "installed",
        "setup_required", "connecting", "connected", "offline",
        "update_available", "update_required", "permission_changed",
        "revocation_pending", "blocked", "incompatible",
    ):
        assert f"'{state}'" in source or f'"{state}"' in source, f"missing lifecycle state {state!r}"
    lowered = source.lower()
    assert "github device flow" not in lowered
    assert "google" not in lowered
    assert "github" not in lowered


def test_panel_shares_one_error_vocabulary_with_no_private_cause_table() -> None:
    """Task-6 controller ruling 3, fix round 1 finding 2: the panel calls
    core/plugin-permissions.js's readablePluginError directly -- it no
    longer carries its own private, independently-maintained cause table
    (which had drifted: plugin_permission_changed told the user to "try
    Connect again", contradicting ruling 4's "opens the diff, not
    Connect"). Exactly one shared vocabulary now exists."""

    panel_source = plugin_panel_source()
    assert "const readableError = window.readablePluginError;" in panel_source
    # The old private map (69 literal `code: 'message'` entries under
    # `const messages = {`) is gone entirely -- not shrunk, not renamed.
    assert "const messages = {" not in panel_source
    assert "try Connect again" not in panel_source

    permissions_source = (UI_ROOT / "core" / "plugin-permissions.js").read_text(encoding="utf-8")
    assert "plugin_blocked" in permissions_source
    # Every cause the panel's own (now-deleted) private map used to carry
    # is folded into the one shared map.
    for cause in (
        "protocol_mismatch", "identity_mismatch", "capability_drift",
        "authorization_required", "authorization_invalid",
        "oauth_device_flow_disabled",
    ):
        assert f"{cause}:" in permissions_source, f"missing panel-only cause {cause!r} in the shared map"


def test_disconnect_and_uninstall_send_the_current_revision() -> None:
    """Task-6 controller ruling 2: disconnect and uninstall now send the
    panel's current lifecycle revision (never the always-undefined
    plugin.revision the task-4 review/install path already had to fix)."""

    source = plugin_panel_source()
    assert "api.disconnect(pluginId, knownRevision)" in source
    assert "api.uninstall(pluginId, knownRevision)" in source
    api_source = (UI_ROOT / "core" / "plugin-api.js").read_text(encoding="utf-8")
    assert "disconnect: (pluginId, revision) => requestJson(pluginPath(pluginId, '/disconnect'), mutation({revision}))" in api_source
    assert "uninstall: (pluginId, revision) => requestJson(pluginPath(pluginId, '/uninstall'), mutation({revision}))" in api_source


def test_uninstall_uses_an_in_panel_destructive_review_never_confirm() -> None:
    """Global HUD-plan constraint: uninstall requires an in-panel
    destructive review, never a browser confirm(). The card is removed
    only through the next authoritative /installed refresh after the
    server accepts the uninstall -- never an optimistic local removal."""

    source = plugin_panel_source()
    assert "window.confirm(" not in source
    assert "_pluginUninstallReviewMarkup" in source
    assert "data-plugin-uninstall-cancel" in source
    assert "data-plugin-uninstall-confirm" in source
    assert "await refreshInstalled();" in source


def test_panel_has_no_private_lifecycle_action_table() -> None:
    """Final fix wave W1: settings-plugins.js's own _pluginPrimaryAction
    (which hand-encoded a second, independently-maintained 14-state->label/
    action mapping alongside core/plugin-permissions.js's lifecycleView, and
    the sibling _PLUGIN_LIFECYCLE_STATE_LABELS/_PLUGIN_LIFECYCLE_STATE_TONE
    literals) is deleted; the panel calls lifecycleView(...) directly for
    every state's label, tone, primary action, and disabled-actions
    decision instead -- exactly one shared table."""

    source = plugin_panel_source()
    assert "_pluginPrimaryAction" not in source
    assert "_PLUGIN_LIFECYCLE_STATE_LABELS" not in source
    assert "_PLUGIN_LIFECYCLE_STATE_TONE" not in source
    assert "lifecycleView(" in source

    # No private object literal keyed by lifecycle state names remains: a
    # genuine per-state table lists several of the 14 canonical states as
    # object keys close together, so two or more distinct state-name keys
    # within a short window is the signal a private table would leave -- a
    # lone incidental `installed:`/`available:` object key elsewhere in the
    # file (there are a couple) is not a table and must not trip this.
    states = (
        "available", "review_required", "installing", "installed",
        "setup_required", "connecting", "connected", "offline",
        "update_available", "update_required", "permission_changed",
        "revocation_pending", "blocked", "incompatible",
    )
    matches = sorted(
        (match.start(), match.group(1))
        for match in re.finditer(r"\b(" + "|".join(states) + r"):\s", source)
    )
    for (pos_a, state_a), (pos_b, state_b) in zip(matches, matches[1:]):
        assert pos_b - pos_a > 300 or state_a == state_b, (
            f"private lifecycle table suspected: {state_a!r} and {state_b!r} "
            f"object keys only {pos_b - pos_a} characters apart"
        )


def test_panel_no_longer_defines_the_moved_view_builders() -> None:
    """Follow-up refactor wave W1: the catalog card markup, installed tile
    markup, drawer body builders (details sections, trust badge, review
    summary, update-diff row/matcher/markup, drawer shell, uninstall
    review), and the permissions editor markup all moved out of
    settings-plugins.js into core/plugin-views.js's window.OmegaPluginViews
    -- grep-style, like test_panel_has_no_private_lifecycle_action_table
    above, so the split cannot silently regress into a second, drifting
    copy. The panel keeps state, wiring, event handlers, store/setup calls,
    focus handling, and the drawer keydown handler; it only ever calls
    these by name now (openPluginDetails/openPluginReview/openDrawer/
    uninstallPlugin/renderCatalogSection/renderInstalled/loadAuthority)."""

    source = plugin_panel_source()
    views_source = (UI_ROOT / "core" / "plugin-views.js").read_text(encoding="utf-8")
    moved_definitions = (
        "function _pluginTrustedIconPath(",
        "function _pluginIconMarkup(",
        "function _pluginGroupByCategory(",
        "var _PLUGIN_TRUST_LABELS",
        "function _pluginTrustBadgeMarkup(",
        "function _pluginDetailsSectionsMarkup(",
        "function _pluginReviewSummaryMarkup(",
        "function _pluginChangeCodeMatches(",
        "function _pluginUpdateDiffRow(",
        "function _pluginUpdateDiffMarkup(",
        "function _pluginRenderDetailsPanel(",
        "function _pluginDrawerShellMarkup(",
        "function _pluginUninstallReviewMarkup(",
        "function renderCard(",
        "function installedTileMarkup(",
        "function permissionsEditorMarkup(",
    )
    for definition in moved_definitions:
        assert definition not in source, (
            f"panel must not redefine {definition!r} -- it moved to core/plugin-views.js"
        )
        assert definition in views_source, (
            f"core/plugin-views.js must define {definition!r}"
        )


def test_card_actions_cover_the_closed_lifecycle_vocabulary() -> None:
    """Task-6 Interfaces: card actions cover retry, continue setup,
    permissions, update review, disconnect, and uninstall -- driven
    entirely by the plugin's own canonical state via core/plugin-
    permissions.js's lifecycleView (final fix wave W1), never a bare
    "Connect" default for permission_changed/revocation_pending."""

    source = plugin_panel_source()
    # renderCard/installedTileMarkup (the callers of lifecycleView(plugin)
    # and the disabledActions consumers) moved to core/plugin-views.js
    # (follow-up refactor wave W1) -- both checked below read that file.
    views_source = plugin_views_source()
    assert "lifecycleView(plugin)" in views_source
    permissions_source = (UI_ROOT / "core" / "plugin-permissions.js").read_text(encoding="utf-8")
    for action_type in ("'retry'", "'continue_setup'", "'permissions'", "'update_review'"):
        assert action_type in permissions_source, f"missing card action type {action_type}"
    assert "disconnectPlugin" in source
    assert "uninstallPlugin" in source
    # revocation_pending disables the actions that would otherwise conflict
    # with the disconnect already in flight (controller ruling 4) -- now
    # sourced from lifecycleView's own disabledActions, not a panel-local
    # `revoking` flag.
    assert "disabledActions" in views_source


# ===========================================================================
# Task 8: final accessibility / responsive / source-of-truth gate
# ===========================================================================


def test_every_marketplace_mjs_harness_runs_via_pytest() -> None:
    """Task-8 Step 1: run every browser-independent marketplace .mjs harness
    through pytest, not only the subset each individual wrapper above
    already covers. Enumerated by glob (not a fixed list) so a future new
    marketplace .mjs harness is picked up automatically without editing this
    test."""

    scripts = sorted(
        p for p in TESTS_HUD.glob("test_plugin*.mjs")
    ) + sorted(TESTS_HUD.glob("test_settings_plugins*.mjs"))
    assert len(scripts) >= 8, f"expected at least 8 marketplace .mjs harnesses, found {scripts}"
    failures = []
    for script in scripts:
        result = subprocess.run(
            ["node", str(script)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            failures.append((script.name, result.stdout + result.stderr))
    assert not failures, "\n\n".join(f"{name}:\n{output}" for name, output in failures)


def test_tab_semantics_link_tabs_to_the_tabpanel_they_control() -> None:
    """Task-8 Step 1: role="tablist"/tab/tabpanel is not sufficient on its
    own -- each tab must declare aria-controls pointing at the tabpanel it
    drives, and the tabpanel must declare aria-labelledby pointing back at
    the tab that currently owns it (updated live as the active tab
    changes), in addition to the aria-selected toggle already covered by
    test_marketplace_bundle_has_accessible_tabs_labeled_search_and_focus_styles
    above."""

    source = plugin_panel_source()
    assert 'id="plugins-tab-public"' in source
    assert 'id="plugins-tab-personal"' in source
    assert 'id="plugins-directory-panel"' in source
    assert 'role="tabpanel"' in source
    # Both tabs point at the one shared results panel they control.
    assert re.search(
        r'id="plugins-tab-public"[^>]*aria-controls="plugins-directory-panel"',
        source,
    ), "the Public tab must declare aria-controls pointing at the results tabpanel"
    assert re.search(
        r'id="plugins-tab-personal"[^>]*aria-controls="plugins-directory-panel"',
        source,
    ), "the Personal tab must declare aria-controls pointing at the results tabpanel"
    # The tabpanel starts labelled by the initially-active tab...
    assert re.search(
        r'id="plugins-directory-panel"[^>]*aria-labelledby="plugins-tab-public"',
        source,
    ), "the tabpanel must start labelled by the initially-selected Public tab"
    # ...and is kept current (not just set once at mount) as the active tab
    # changes -- renderCatalogSection() re-runs on every tab switch.
    assert "tabpanel.setAttribute('aria-labelledby', tabButton.id)" in source


def test_state_is_never_conveyed_by_color_alone() -> None:
    """Task-8 Step 1 "non-color state text": every lifecycle state that
    carries a color tone class also carries a real, non-empty text label --
    the tone is decoration, never the only signal. A state present in the
    tone map but missing (or blank) in the label map would be a real
    accessibility regression this catches. Final fix wave W1: both maps now
    live once, in core/plugin-permissions.js's lifecycleView table (the
    panel calls it directly rather than keeping its own copies)."""

    source = (UI_ROOT / "core" / "plugin-permissions.js").read_text(encoding="utf-8")
    tone_block = source[
        source.index("_PLUGIN_LIFECYCLE_VIEW_TONE"):
        source.index("_PLUGIN_LIFECYCLE_VIEW_TONE") + 700
    ]
    label_block = source[
        source.index("_PLUGIN_LIFECYCLE_VIEW_LABELS"):
        source.index("_PLUGIN_LIFECYCLE_VIEW_LABELS") + 700
    ]
    tone_states = set(re.findall(r"^\s*(\w+):\s*'is-", tone_block, re.MULTILINE))
    label_pairs = dict(re.findall(r"^\s*(\w+):\s*'([^']*)'", label_block, re.MULTILINE))
    assert len(tone_states) >= 14, "expected the full 14-state tone map"
    for state in tone_states:
        assert state in label_pairs, f"state {state!r} has a color tone but no text label"
        assert label_pairs[state].strip(), f"state {state!r} has a color tone but a blank text label"


def test_every_external_link_carries_noopener_noreferrer() -> None:
    """Task-8 Step 1 "safe external links": every target="_blank" anchor in
    the panel is immediately paired with rel="noopener noreferrer" -- never
    a bare target="_blank" that would hand the opened tab a live
    window.opener reference back into the HUD."""

    source = plugin_panel_source()
    blank_targets = source.count('target="_blank"')
    assert blank_targets > 0, "expected at least one external link in this file"
    safe_targets = len(re.findall(r'target="_blank"\s+rel="noopener noreferrer"', source))
    assert safe_targets == blank_targets, (
        "every target=\"_blank\" anchor must be immediately followed by "
        'rel="noopener noreferrer"'
    )


def test_a_catalog_page_over_the_ceiling_is_truncated_before_it_ever_reaches_the_store() -> None:
    """Task-8 Step 1 "bounded result rendering": this panel only ever
    requests _PLUGIN_CATALOG_PAGE_SIZE items per page. loadCatalog() must
    truncate an oversized single-page response to that ceiling before
    dispatching it -- source-contract half of the runtime proof in
    test_plugin_marketplace_panel.mjs ("a catalog page over the ceiling
    never renders more than the ceiling")."""

    source = plugin_panel_source()
    assert "page.plugins.slice(0, _PLUGIN_CATALOG_PAGE_SIZE)" in source


def test_live_region_status_containers_carry_the_exact_aria_live_politeness() -> None:
    """Task-8 Step 1 "live-region status": each of the three status
    surfaces a screen reader must be told about carries the exact
    aria-live politeness this panel already commits to -- the installed
    strip and result count are "polite" (announced without interrupting),
    and the directory error banner is "assertive" (interrupts, since it
    reports a failed load). A stronger, more sensitive check than the
    existing bare "aria-live" in source substring test above."""

    source = plugin_panel_source()
    assert (
        '<div class="plugins-installed-list" data-plugin-installed-list aria-live="polite">'
        in source
    ), "the installed strip must be a polite live region"
    assert (
        '<span class="plugins-directory-count" data-plugin-count aria-live="polite">'
        in source
    ), "the result count must be a polite live region"
    assert (
        '<p class="plugins-directory-error" data-plugin-directory-error aria-live="assertive" hidden></p>'
        in source
    ), "the directory error banner must be an assertive live region"


def test_exactly_one_generic_route_family_in_the_panel_and_the_client() -> None:
    """Task-8 Step 1: re-confirms, as one consolidated check spanning both
    the panel (settings-plugins.js) and the client (plugin-api.js), that
    there is exactly one generic /api/plugins/* route family -- no
    /featured route and no provider substring anywhere in either file. This
    subsumes and re-states test_plugin_api_uses_only_universal_routes and
    test_connect_flow_sources_carry_no_provider_substring's own file-pair
    check as the brief's single Step 1 bullet."""

    panel_source = plugin_panel_source()
    api_source = plugin_api_source()
    for name, source in (("settings-plugins.js", panel_source), ("plugin-api.js", api_source)):
        assert "/api/plugins/featured" not in source, f"{name} must not define a /featured route"
        assert "google" not in source.lower(), f"{name} must not name a provider"
        assert "github" not in source.lower(), f"{name} must not name a provider"
    # The client's one route builder every mutation/read funnels through.
    assert "function pluginPath(pluginId, suffix)" in api_source
    assert "'/api/plugins/' + encodeURIComponent(pluginId) + suffix" in api_source

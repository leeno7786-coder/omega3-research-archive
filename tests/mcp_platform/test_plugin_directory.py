from __future__ import annotations

from types import SimpleNamespace

import pytest

from mcp_platform.plugin_directory import (
    CatalogPluginEntry,
    CatalogSourceSnapshot,
    PluginDirectoryError,
    PluginDirectoryService,
    _introduction_options,
)
from plugin_platform.catalog import TrustTier
from plugin_platform.catalog_sources import (
    CatalogSourceDescriptor,
    CatalogSourceKind,
    CatalogSourcePolicy,
)


DIGEST = "b" * 64
REVISION = "c" * 64


def _entry(
    plugin_id: str = "northwind.docs",
    *,
    title: str = "Northwind Docs",
    description: str = "Search and read project documents.",
    categories: tuple[str, ...] = (),
    trust_tier: str = "omega_verified",
) -> CatalogPluginEntry:
    return CatalogPluginEntry(
        plugin_id=plugin_id,
        publisher_id="northwind",
        plugin_version="1.0.0",
        title=title,
        description=description,
        signing_key_id="northwind-release",
        manifest_sha256=DIGEST,
        package_sha256="d" * 64,
        required_app_ids=("northwind.docs.mcp",),
        optional_app_ids=(),
        starter_requests=("Explore Northwind Docs.",),
        entry_point_ids=("get_started",),
        categories=categories,
        trust_tier=trust_tier,
    )


class Source:
    descriptor = CatalogSourceDescriptor(
        source_id="omega.featured",
        title="Omega featured",
        kind=CatalogSourceKind.MARKETPLACE,
    )

    def __init__(
        self,
        *,
        failure: Exception | None = None,
        entry: CatalogPluginEntry | None = None,
    ) -> None:
        self.failure = failure
        self.entry = entry

    def snapshot(self) -> CatalogSourceSnapshot:
        if self.failure is not None:
            raise self.failure
        return CatalogSourceSnapshot(
            source=self.descriptor,
            revision=REVISION,
            plugins=(self.entry or _entry(),),
        )

    def review(self, plugin_id: str):
        return {"plugin_id": plugin_id, "sha256": DIGEST}

    def install(self, plugin_id: str, *, confirmation: object):
        return {"plugin_id": plugin_id, "confirmation": confirmation}


class Registry:
    def __init__(self, records=(), *, installed_trust_tier: TrustTier = TrustTier.OMEGA_VERIFIED) -> None:
        self.records = tuple(records)
        self.revision = REVISION
        self.installed_trust_tier = installed_trust_tier

    def snapshot(self):
        return SimpleNamespace(records=self.records, revision=self.revision)

    def reverify_installed(self, plugin_id: str):
        record = next(item for item in self.records if item.plugin_id == plugin_id)
        return SimpleNamespace(
            trust_tier=self.installed_trust_tier,
            manifest=SimpleNamespace(
                plugin_id=record.plugin_id,
                publisher_id=record.publisher_id,
                plugin_version=record.selected_version,
                title="Installed Northwind Docs",
                description="Available from the verified installed package.",
                signing_key_id="northwind-release",
                starter_requests=("Search installed docs.",),
                entry_points=(SimpleNamespace(entry_point_id="get_started"),),
                required_apps=(SimpleNamespace(app_id="northwind.docs.mcp"),),
                optional_apps=(),
            ),
            manifest_sha256=DIGEST,
            package_sha256="d" * 64,
            capability_authority={
                "northwind.docs.mcp": {
                    "tools": {
                        "find": {"title": "Find the launch brief"},
                        "issues": {"title": "List open launch issues"},
                        "read": {"title": "Read the project roadmap"},
                    },
                    "resources": {},
                    "resource_templates": {},
                    "prompts": {},
                }
            },
        )


def _installed(*, enabled: bool = True):
    return SimpleNamespace(
        plugin_id="northwind.docs",
        publisher_id="northwind",
        selected_version="1.0.0",
        enabled=enabled,
        blocker=None,
        app_binding=SimpleNamespace(
            required_app_ids=("northwind.docs.mcp",),
            optional_app_ids=(),
            revision="e" * 64,
        ),
    )


def test_directory_overlays_install_and_connection_state_on_signed_source() -> None:
    service = PluginDirectoryService(sources=(Source(),), plugin_registry=Registry((_installed(),)))

    snapshot = service.list_directory()

    assert snapshot.status == "ready"
    assert snapshot.source_errors == ()
    assert len(snapshot.plugins) == 1
    plugin = snapshot.plugins[0]
    assert plugin.plugin_id == "northwind.docs"
    assert plugin.source_id == "omega.featured"
    assert plugin.trust_state == "omega_verified"
    assert plugin.installed is True
    assert plugin.enabled is True
    assert plugin.connection_state == "connected"


def test_directory_projects_signed_title_categories_and_trust_tier_from_the_entry() -> None:
    """Catches projecting manifest text/hard-coded values instead of the signed entry."""
    entry = _entry(
        title="Docs Search",
        description="A publisher-verified documentation search plugin.",
        categories=("productivity", "docs"),
        trust_tier="publisher_verified",
    )
    service = PluginDirectoryService(
        sources=(Source(entry=entry),),
        plugin_registry=Registry(),
    )

    plugin = service.list_directory().plugins[0]

    assert plugin.title == "Docs Search"
    assert plugin.description == "A publisher-verified documentation search plugin."
    assert plugin.categories == ("productivity", "docs")
    assert plugin.trust_state == "publisher_verified"


def test_installed_fallback_lane_reports_the_installed_records_sealed_tier() -> None:
    """Catches the installed.local lane losing its own sealed trust tier."""
    service = PluginDirectoryService(
        sources=(),
        plugin_registry=Registry((_installed(),)),
    )

    plugin = service.list_directory().plugins[0]

    assert plugin.source_id == "installed.local"
    assert plugin.trust_state == "omega_verified"


def test_installed_fallback_lane_reports_a_publisher_verified_records_own_sealed_tier() -> None:
    """Catches hard-coding omega_verified instead of projecting the record's own sealed tier."""
    service = PluginDirectoryService(
        sources=(),
        plugin_registry=Registry(
            (_installed(),), installed_trust_tier=TrustTier.PUBLISHER_VERIFIED
        ),
    )

    plugin = service.list_directory().plugins[0]

    assert plugin.source_id == "installed.local"
    assert plugin.trust_state == "publisher_verified"


def test_catalog_failure_keeps_verified_installs_and_is_never_empty_success() -> None:
    service = PluginDirectoryService(
        sources=(Source(failure=OSError("offline")),),
        plugin_registry=Registry((_installed(enabled=False),)),
    )

    snapshot = service.list_directory()

    assert snapshot.status == "degraded"
    assert snapshot.source_errors == (("omega.featured", "catalog_source_unavailable"),)
    assert len(snapshot.plugins) == 1
    assert snapshot.plugins[0].installed is True
    assert snapshot.plugins[0].source_state == "unavailable"
    assert snapshot.plugins[0].connection_state == "connection_required"


def test_try_in_chat_handoff_introduces_every_signed_option_without_an_executable_turn() -> None:
    service = PluginDirectoryService(
        sources=(Source(),),
        plugin_registry=Registry((_installed(),)),
    )

    handoff = service.try_in_chat_handoff("northwind.docs")

    assert handoff.to_public_dict() == {
        "plugin_id": "northwind.docs",
        "title": "Northwind Docs",
        "intro_text": (
            "Northwind Docs is connected. I can help you:\n\n"
            "- Find the launch brief.\n"
            "- List open launch issues.\n"
            "- Read the project roadmap.\n\n"
            "Choose an option or describe what you want to do."
        ),
    }


def test_chat_selection_accepts_only_current_connected_plugin_id() -> None:
    service = PluginDirectoryService(sources=(Source(),), plugin_registry=Registry((_installed(),)))

    assert service.chat_selector("northwind.docs") == {"plugin_id": "northwind.docs"}

    disconnected = PluginDirectoryService(
        sources=(Source(),),
        plugin_registry=Registry((_installed(enabled=False),)),
    )
    with pytest.raises(PluginDirectoryError, match="plugin_connection_required"):
        disconnected.chat_selector("northwind.docs")


def test_directory_applies_source_policy_before_any_catalog_is_read() -> None:
    class DeveloperSource(Source):
        descriptor = CatalogSourceDescriptor(
            source_id="developer.local",
            title="Developer local",
            kind=CatalogSourceKind.DEVELOPER,
        )

    with pytest.raises(ValueError, match="catalog_source_developer_mode_required"):
        PluginDirectoryService(
            sources=(DeveloperSource(),),
            plugin_registry=Registry(),
        )

    service = PluginDirectoryService(
        sources=(DeveloperSource(),),
        plugin_registry=Registry(),
        source_policy=CatalogSourcePolicy(developer_mode=True),
    )
    assert service.list_directory().status == "ready"


def test_registry_recovered_plugins_use_the_installed_lane_not_the_marketplace() -> None:
    """Catches labelling locally recovered registry state as signed marketplace trust."""
    service = PluginDirectoryService(
        sources=(),
        plugin_registry=Registry((_installed(),)),
    )

    snapshot = service.list_directory()

    assert len(snapshot.plugins) == 1
    plugin = snapshot.plugins[0]
    assert plugin.source_id == "installed.local"
    assert plugin.source_kind == "installed"
    assert plugin.source_state == "installed"
    assert all(item.source_kind != "marketplace" for item in snapshot.plugins)


def test_unavailable_source_keeps_its_own_lane_for_recovered_plugins() -> None:
    """Catches a failed signed source being relabelled as the local installed lane."""
    service = PluginDirectoryService(
        sources=(Source(failure=RuntimeError("catalog offline")),),
        plugin_registry=Registry((_installed(),)),
    )

    snapshot = service.list_directory()

    assert snapshot.status == "degraded"
    plugin = snapshot.plugins[0]
    assert plugin.source_id == "omega.featured"
    assert plugin.source_kind == "marketplace"
    assert plugin.source_state == "unavailable"


# --- signed capability-authority projection ----------------------------------

PLUGIN = "northwind.docs"
APP_A = "northwind.docs.mcp"
APP_B = "northwind.tracker.mcp"


def _groups(tools: dict[str, object]) -> dict[str, object]:
    return {
        "tools": tools,
        "resources": {},
        "resource_templates": {},
        "prompts": {},
    }


def _verified(
    *,
    capability_authority: object,
    app_ids: tuple[str, ...] = (APP_A,),
    starter_requests: tuple[str, ...] = ("Search installed docs.",),
    legacy_authority: bool = False,
):
    return SimpleNamespace(
        manifest=SimpleNamespace(
            plugin_id=PLUGIN,
            starter_requests=starter_requests,
            required_apps=tuple(SimpleNamespace(app_id=item) for item in app_ids),
            optional_apps=(),
        ),
        capability_authority=capability_authority,
        legacy_authority=legacy_authority,
    )


def test_introduction_reads_every_app_of_a_multi_app_authority() -> None:
    """Catches an introduction that stops at the first signed app dependency."""

    verified = _verified(
        app_ids=(APP_A, APP_B),
        capability_authority={
            APP_A: _groups({"find": {"title": "Find the launch brief"}}),
            APP_B: _groups({"issues": {"title": "List open launch issues"}}),
        },
    )

    assert _introduction_options(verified) == (
        "Find the launch brief.",
        "List open launch issues.",
    )


def test_cross_app_capability_name_collision_still_introduces_the_plugin() -> None:
    """Catches one shared capability name disabling Try in chat for the plugin."""

    verified = _verified(
        app_ids=(APP_A, APP_B),
        capability_authority={
            APP_A: _groups({"search": {"title": "Search documents"}}),
            APP_B: _groups({"search": {"title": "Search issues"}}),
        },
    )

    assert _introduction_options(verified) == ("Search documents.", "Search issues.")


def test_identical_option_across_two_apps_is_introduced_once() -> None:
    """Catches a cross-app duplicate being rejected instead of collapsed."""

    verified = _verified(
        app_ids=(APP_A, APP_B),
        capability_authority={
            APP_A: _groups({"search": {"title": "Search everything"}}),
            APP_B: _groups({"search": {"title": "Search everything"}}),
        },
    )

    assert _introduction_options(verified) == ("Search everything.",)


def test_duplicate_options_inside_one_app_stay_rejected() -> None:
    """Catches one app publishing two capabilities under one indistinct title."""

    verified = _verified(
        capability_authority={
            APP_A: _groups(
                {"a": {"title": "Search docs"}, "b": {"title": "Search docs"}}
            )
        },
    )

    with pytest.raises(PluginDirectoryError, match="plugin_interaction_unavailable"):
        _introduction_options(verified)


@pytest.mark.parametrize(
    "capability_authority",
    [
        pytest.param({}, id="no-app-partition"),
        pytest.param({"other.app": _groups({})}, id="declared-app-absent"),
        pytest.param({APP_A: {"resources": {}}, "x": 1}, id="app-without-tool-group"),
        pytest.param({APP_A: _groups({})}, id="no-signed-tool"),
        pytest.param({APP_A: _groups(["find"])}, id="non-mapping-tool-group"),
        pytest.param({APP_A: _groups({"find": "Find"})}, id="non-mapping-policy"),
        pytest.param({APP_A: _groups({"find": {"title": ""}})}, id="blank-title"),
        pytest.param({APP_A: _groups({"find": {}})}, id="untitled-capability"),
        pytest.param({APP_A: ["tools"]}, id="non-mapping-app"),
        pytest.param("tools", id="non-mapping-authority"),
    ],
)
def test_unusable_capability_authority_fails_closed(capability_authority: object) -> None:
    """Catches a weakened projection introducing an unverified capability set."""

    with pytest.raises(PluginDirectoryError, match="plugin_interaction_unavailable"):
        _introduction_options(_verified(capability_authority=capability_authority))


def test_only_declared_app_dependencies_are_introduced() -> None:
    """Catches iterating authority keys instead of the manifest's signed app ids."""

    verified = _verified(
        capability_authority={
            APP_A: _groups({"read": {"title": "Read"}}),
            APP_B: _groups({"find": {"title": "Find"}}),
        },
    )

    assert _introduction_options(verified) == ("Read.",)


def test_absent_authority_asset_falls_back_to_signed_starter_requests() -> None:
    """Catches the manifest fallback being lost when no authority asset ships."""

    assert _introduction_options(
        _verified(capability_authority=None)
    ) == ("Search installed docs.",)


def test_legacy_authority_blocks_try_in_chat_instead_of_falling_back() -> None:
    """Catches a legacy flat-authority package silently using the starter fallback.

    Spec principle 8 forbids a compatibility fork: a package the verifier
    flagged ``legacy_authority`` has no admitted capability authority, but it
    is not the same case as a package that never shipped one -- Try in chat
    must fail closed with a stable cause instead of quietly offering the
    manifest's generic starter requests.
    """

    with pytest.raises(PluginDirectoryError, match="^plugin_update_required$"):
        _introduction_options(
            _verified(capability_authority=None, legacy_authority=True)
        )

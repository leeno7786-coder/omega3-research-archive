from __future__ import annotations

import pytest

from plugin_platform.catalog_sources import (
    CatalogSourceDescriptor,
    CatalogSourceKind,
    CatalogSourcePolicy,
)


REVISION = "a" * 64


def test_normal_mode_accepts_only_marketplace_or_exact_enterprise_approval() -> None:
    policy = CatalogSourcePolicy(
        developer_mode=False,
        enterprise_approvals={"acme.catalog": REVISION},
    )

    marketplace = CatalogSourceDescriptor(
        source_id="omega.marketplace",
        title="Omega marketplace",
        kind=CatalogSourceKind.MARKETPLACE,
    )
    enterprise = CatalogSourceDescriptor(
        source_id="acme.catalog",
        title="Acme approved",
        kind=CatalogSourceKind.ENTERPRISE,
        approval_revision=REVISION,
    )

    assert policy.admit(marketplace) is marketplace
    assert policy.admit(enterprise) is enterprise

    with pytest.raises(ValueError, match="catalog_source_not_approved"):
        policy.admit(
            CatalogSourceDescriptor(
                source_id="other.catalog",
                title="Other",
                kind=CatalogSourceKind.ENTERPRISE,
                approval_revision=REVISION,
            )
        )


def test_developer_catalog_is_a_separate_explicit_mode() -> None:
    source = CatalogSourceDescriptor(
        source_id="developer.local",
        title="Local development",
        kind=CatalogSourceKind.DEVELOPER,
    )

    with pytest.raises(ValueError, match="catalog_source_developer_mode_required"):
        CatalogSourcePolicy(developer_mode=False).admit(source)

    assert CatalogSourcePolicy(developer_mode=True).admit(source) is source


@pytest.mark.parametrize(
    "changes",
    (
        {"source_id": "https://catalog.example"},
        {"title": ""},
        {"approval_revision": "not-a-digest"},
    ),
)
def test_catalog_source_descriptor_is_closed_and_content_free(changes: dict[str, str]) -> None:
    values: dict[str, object] = {
        "source_id": "acme.catalog",
        "title": "Acme",
        "kind": CatalogSourceKind.ENTERPRISE,
        "approval_revision": REVISION,
    }
    values.update(changes)

    with pytest.raises(ValueError, match="catalog_source_invalid"):
        CatalogSourceDescriptor(**values)  # type: ignore[arg-type]


def test_personal_catalog_fails_closed_without_a_local_trust_root() -> None:
    """Catches a personal lane admitted before its local trust root exists."""
    source = CatalogSourceDescriptor(
        source_id="personal.local",
        title="Personal",
        kind=CatalogSourceKind.PERSONAL,
    )

    for developer_mode in (False, True):
        with pytest.raises(
            ValueError,
            match="^catalog_source_personal_trust_root_unavailable$",
        ):
            CatalogSourcePolicy(developer_mode=developer_mode).admit(source)


def test_catalog_source_kinds_are_exactly_the_marketplace_lanes() -> None:
    """Catches a retained embedded lane or an undeclared new trust lane."""
    assert {kind.value for kind in CatalogSourceKind} == {
        "marketplace",
        "installed",
        "enterprise",
        "developer",
        "personal",
    }
    with pytest.raises(ValueError, match="^catalog_source_invalid$"):
        CatalogSourceDescriptor(
            source_id="omega.marketplace",
            title="Omega marketplace",
            kind="embedded",  # type: ignore[arg-type]
        )


def test_installed_lane_is_admitted_without_inheriting_marketplace_trust() -> None:
    """Catches the reverified-registry fallback needing catalog approval or a signed lane."""
    source = CatalogSourceDescriptor(
        source_id="installed.local",
        title="Installed plugins",
        kind=CatalogSourceKind.INSTALLED,
    )

    assert CatalogSourcePolicy(developer_mode=False).admit(source) is source
    assert CatalogSourcePolicy(developer_mode=True).admit(source) is source
    assert source.kind is not CatalogSourceKind.MARKETPLACE
    assert source.kind.value == "installed"
    with pytest.raises(ValueError, match="^catalog_source_invalid$"):
        CatalogSourceDescriptor(
            source_id="installed.local",
            title="Installed plugins",
            kind=CatalogSourceKind.INSTALLED,
            approval_revision=REVISION,
        )

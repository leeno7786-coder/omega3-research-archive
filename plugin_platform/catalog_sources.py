"""Provider-neutral catalog source admission for the plugin directory."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
import re
from types import MappingProxyType


_SOURCE_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")


class CatalogSourceKind(StrEnum):
    """Closed trust lanes for catalogs visible in the product."""

    MARKETPLACE = "marketplace"
    INSTALLED = "installed"
    ENTERPRISE = "enterprise"
    DEVELOPER = "developer"
    PERSONAL = "personal"


@dataclass(frozen=True, slots=True)
class CatalogSourceDescriptor:
    """Content-free identity and admission evidence for one catalog source."""

    source_id: str
    title: str
    kind: CatalogSourceKind
    approval_revision: str | None = None

    def __post_init__(self) -> None:
        try:
            if type(self.source_id) is not str or _SOURCE_ID_RE.fullmatch(self.source_id) is None:
                raise ValueError
            if (
                type(self.title) is not str
                or not self.title.strip()
                or self.title != self.title.strip()
                or len(self.title) > 120
            ):
                raise ValueError
            kind = CatalogSourceKind(self.kind)
            revision = self.approval_revision
            if revision is not None and (
                type(revision) is not str or _DIGEST_RE.fullmatch(revision) is None
            ):
                raise ValueError
            if kind is CatalogSourceKind.ENTERPRISE and revision is None:
                raise ValueError
            if kind is not CatalogSourceKind.ENTERPRISE and revision is not None:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("catalog_source_invalid") from None
        object.__setattr__(self, "kind", kind)


class CatalogSourcePolicy:
    """Admit sources without letting normal mode inherit developer or personal trust."""

    def __init__(
        self,
        *,
        developer_mode: bool,
        enterprise_approvals: Mapping[str, str] | None = None,
    ) -> None:
        if type(developer_mode) is not bool:
            raise ValueError("catalog_source_policy_invalid")
        approvals = dict(enterprise_approvals or {})
        if any(
            type(source_id) is not str
            or _SOURCE_ID_RE.fullmatch(source_id) is None
            or type(revision) is not str
            or _DIGEST_RE.fullmatch(revision) is None
            for source_id, revision in approvals.items()
        ):
            raise ValueError("catalog_source_policy_invalid")
        self.developer_mode = developer_mode
        self.enterprise_approvals = MappingProxyType(approvals)

    def admit(self, source: CatalogSourceDescriptor) -> CatalogSourceDescriptor:
        """Return the exact admitted descriptor or a stable policy failure."""

        if type(source) is not CatalogSourceDescriptor:
            raise ValueError("catalog_source_invalid")
        if source.kind in {
            CatalogSourceKind.MARKETPLACE,
            CatalogSourceKind.INSTALLED,
        }:
            return source
        if source.kind is CatalogSourceKind.ENTERPRISE:
            if self.enterprise_approvals.get(source.source_id) != source.approval_revision:
                raise ValueError("catalog_source_not_approved")
            return source
        if source.kind is CatalogSourceKind.PERSONAL:
            raise ValueError("catalog_source_personal_trust_root_unavailable")
        if not self.developer_mode:
            raise ValueError("catalog_source_developer_mode_required")
        return source


__all__ = [
    "CatalogSourceDescriptor",
    "CatalogSourceKind",
    "CatalogSourcePolicy",
]

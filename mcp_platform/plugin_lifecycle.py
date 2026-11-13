"""Provider-neutral signed plugin lifecycle composition.

Catalog entries are still ordinary signed plugin packages.  This module only
locates the embedded catalog/package and composes the existing installer and
remote-app provisioner; it does not mint an unsigned app requirement or an
OAuth grant.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import asyncio
import logging
from contextvars import ContextVar
import base64
import hmac
import json
import os
from pathlib import Path
import secrets
import sys
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager, nullcontext
from hashlib import sha256
import threading
import time
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from core import paths as core_paths
from mcp_platform.app_registry import (
    InstalledMcpAppRecord,
    McpAppNotInstalled,
    McpAppRegistry,
)
from mcp_platform.app_sources import TrustedRemoteAppSource
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.capabilities import project_declared_capabilities
from mcp_platform.client_contracts import (
    McpClientDeployment,
    McpClientLimits,
    McpConnectionDescriptor,
    McpTransport,
)
from mcp_platform.client import McpProviderDriftStatus, McpProviderRecovery
from mcp_platform.operation_contracts import McpAuthoritySnapshot, McpConnectionSnapshot
from mcp_platform.package_contracts import RiskClass
from mcp_platform.plugin_discovery_transport import build_restricted_discovery_peer
from mcp_platform.package_verifier import current_platform_tag, verify_package
from mcp_platform.provisioner import McpAppProvisioner
from mcp_platform.oauth_broker import OAuthBroker
from mcp_platform.oauth_profiles import OAuthProfileMatchInput, OAuthProfileRegistry
from mcp_platform.oauth_protocol import OAuthProtocolClient
from mcp_platform.oauth_vault import OAuthCredentialVault
from mcp_platform.plugin_authority import (
    PluginAuthorityService,
    PluginAuthorityStore,
    PluginExecutionPolicy,
    plugin_plan_digest,
)
from mcp_platform.plugin_connections import (
    CandidateConnectionProbe,
    CandidateConnectionPreparation,
    PluginConnectionCoordinator,
    PluginConnectionService,
    PluginUpdateConnectionRecovery,
    descriptor_for_record,
    signed_capability_authority_for_candidate,
)
from mcp_platform.plugin_grants import PluginGrantRecord, PluginGrantService
from mcp_platform.plugin_grants import PluginGrantActivation
from mcp_platform.plugin_update_policy import (
    PluginUpdateCheckResult,
    PluginUpdatePolicy,
    PluginUpdateResult,
    SAFE_UPDATE_ERRORS,
    defer_policy_if_current,
    policy_store,
    read_policy,
    read_update_status,
    update_status_store,
    write_policy,
    write_update_status,
)
from mcp_platform.plugin_execution_worker import (
    PluginExecutionWorker,
    execute_mcp_operation,
)
from mcp_platform.plugin_permissions import PluginPermissionStore
from mcp_platform.plugin_backend import CurrentMcpPluginBackend, PluginOperationSubmitter
from mcp_platform.plugin_directory import (
    CatalogPluginEntry,
    CatalogSourceAdapter,
    CatalogSourceSnapshot,
    PluginDirectoryError,
    PluginDirectoryService,
    TryInChatHandoff,
)
from mcp_platform.plugin_runtime import PluginRetryAfterFence, PluginRuntime
from mcp_platform.plugin_state import (
    ConnectionStart,
    PluginAppState,
    PluginLifecycleSnapshot,
    PluginLifecycleState,
    _ERROR_BY_STATE,
)
from mcp_platform.registry import PackageRegistry
from mcp_platform.trust_store import (
    load_embedded_key_ring as load_mcp_embedded_key_ring,
)
from mcp_platform.state_store import McpStateStore
from plugin_platform.catalog import (
    CatalogVerificationError,
    DistributionKind,
    MarketplaceCatalogEntry,
    TrustTier,
    VerifiedMarketplaceCatalog,
)
from plugin_platform.catalog_store import CatalogStore, CatalogStoreError
from plugin_platform.catalog_sync import (
    CatalogFetch,
    CatalogSynchronizer,
    CatalogSyncResult,
)
from mcp_platform.catalog_fetch import build_marketplace_catalog_fetch
from mcp_platform.package_fetch import PackageArtifactFetcher, PackageArtifactError
from mcp_platform.plugin_discovery import (
    CapabilityDiscoveryBinding,
    CapabilityDiscoveryReview,
    DiscoveryPeer,
    PluginDiscoveryError,
    RestrictedPluginDiscovery,
)
from plugin_platform.embedded_catalog import (
    EmbeddedCatalogPackageResolver,
    EmbeddedCatalogResolutionError,
)
from plugin_platform.installer import (
    PluginInstallResult,
    PluginInstallReview,
    PluginInstaller,
    PluginRegistry,
    PluginRegistryError,
    delete_local_seal_document,
    read_local_seal_document,
)
from plugin_platform.local_trust import LocalTrustError, LocalTrustRoot
from plugin_platform.marketplace_importer import MarketplaceImporter
from plugin_platform.mcp_registry import (
    McpRegistryRemote,
    McpRegistryServer,
    NamespaceKind,
    RegistryStatus,
    RemoteTransport,
)
from plugin_platform.records import (
    InstalledPluginRecord,
    InstalledPluginVersionRecord,
    PluginAppBinding,
    PluginRegistrySnapshot,
)
from plugin_platform.package_verifier import (
    PackageVerificationError,
    VerifiedPlugin,
    verify_locally_sealed_plugin_package,
    verify_plugin_package,
)
from plugin_platform.marketplace_provenance import (
    ProviderProvenanceRecord,
    ProviderProvenanceVerificationError,
    VerifiedProviderProvenance,
    verify_provider_provenance,
)
from plugin_platform.catalog_sources import CatalogSourceDescriptor, CatalogSourceKind
from plugin_platform.contracts import (
    PLUGIN_PROTOCOL_VERSION,
    AppDeployment,
    PluginAuthentication,
)
from plugin_platform.trust import (
    PublisherTrustError,
    load_embedded_key_ring,
)
from mcp_platform.plugin_planning import plain_arguments


_OMEGA_VERSION = "3.0.0"
_MARKETPLACE_ROOT = Path(__file__).resolve().parent.parent / "plugin_platform" / "marketplace"
_PLUGIN_TRANSPORT_CONTEXT: ContextVar[bool] = ContextVar(
    "omega_plugin_transport_context",
    default=False,
)


def _default_plugin_operation_submitter() -> PluginOperationSubmitter:
    """Submit plugin coroutines to the boot-owned transport loop.

    The default MCP host is resolved at call time, never at service
    construction, so tests and early boot can construct the plugin lifecycle service
    before the host starts.
    """

    def submit(operation: Any, *, timeout_s: float | None = None) -> Any:
        from mcp_platform.service import get_default_mcp_host

        if _PLUGIN_TRANSPORT_CONTEXT.get():
            return operation()

        async def run_owned() -> Any:
            token = _PLUGIN_TRANSPORT_CONTEXT.set(True)
            try:
                return await operation()
            finally:
                _PLUGIN_TRANSPORT_CONTEXT.reset(token)

        return get_default_mcp_host().supervisor.submit_plugin_operation_sync(
            run_owned,
            timeout_s=timeout_s,
        )

    return submit


_CATALOG_PATH = _MARKETPLACE_ROOT / "catalog.json"
_PROVENANCE_PATH = _MARKETPLACE_ROOT.parent / "marketplace_provenance.json"
_DEFAULT_SERVICE: PluginLifecycleService | None = None
_DEFAULT_SERVICE_LOCK = threading.RLock()
_MISSING_COMPONENT_GUARD = object()
_COMPONENT_GUARD_LOCK = threading.RLock()

# Bounded page size ceiling for PluginLifecycleService.search_catalog(). The HUD
# route family enforces its own ceiling (hud/server.py's `page_size > 100` gate);
# this is the service-layer bound so any caller -- HTTP or direct -- is bounded.
MAX_CATALOG_PAGE_SIZE = 100
_CATALOG_CURSOR_DOMAIN = b"omega.plugin-lifecycle.catalog-cursor.v1\0"
_CATALOG_CURSOR_SCHEMA_VERSION = 1
_UPDATE_HEARTBEAT_MIN_WAIT_SECONDS = 0.25
_UPDATE_HEARTBEAT_MAX_WAIT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _LifecycleRevisionSnapshot:
    revision: str
    plugin_registry_revision: str
    components: tuple[tuple[str, str], ...]


class _LifecycleMutationBoundary:
    """One non-recursive lock shared by synchronous and asynchronous mutations."""

    def __init__(self, mutation_lease: Callable[[], object] | None = None) -> None:
        self._lock = threading.Lock()
        self._closed = False
        self._mutation_lease = mutation_lease or nullcontext
        self._active_lease: object | None = None

    def _entered(self) -> _LifecycleMutationBoundary:
        if self._closed:
            self._lock.release()
            raise ValueError("plugin_lifecycle_operation_conflict")
        try:
            lease = self._mutation_lease()
            getattr(lease, "__enter__")()
        except BaseException:
            self._lock.release()
            raise
        self._active_lease = lease
        return self

    def __enter__(self) -> _LifecycleMutationBoundary:
        self._lock.acquire()
        return self._entered()

    def __exit__(self, *args: object) -> None:
        lease = self._active_lease
        self._active_lease = None
        try:
            if lease is not None:
                getattr(lease, "__exit__")(*args)
        finally:
            self._lock.release()

    async def __aenter__(self) -> _LifecycleMutationBoundary:
        acquisition = asyncio.create_task(asyncio.to_thread(self._lock.acquire))
        try:
            await asyncio.shield(acquisition)
        except asyncio.CancelledError:
            await acquisition
            self._lock.release()
            raise
        return self._entered()

    async def __aexit__(self, *args: object) -> None:
        self.__exit__(*args)

    def seal_held(self) -> None:
        """Reject queued entrants before the current owner releases the lock."""

        if self._active_lease is None:
            raise RuntimeError("plugin_lifecycle_operation_conflict")
        self._closed = True

    def close(self) -> None:
        self._lock.acquire()
        try:
            self._closed = True
        finally:
            self._lock.release()


def _closed_revision(value: object) -> str:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError("plugin_lifecycle_revision_unavailable")
    return value


def _durable_owner_revision(owner: object | None, *, label: str) -> str:
    if owner is None:
        return sha256_hex(
            canonical_bytes(
                {"domain": "omega.plugin-lifecycle.component.v1", "label": label, "absent": True}
            )
        )
    projector = getattr(owner, "lifecycle_revision", None)
    if callable(projector):
        return _closed_revision(projector())
    snapshot = getattr(owner, "snapshot", None)
    if callable(snapshot):
        return _closed_revision(getattr(snapshot(), "revision", None))
    state = owner
    if not callable(getattr(state, "locked", None)) or not callable(
        getattr(state, "read_bytes", None)
    ):
        state = getattr(owner, "_state", None)
    locked = getattr(state, "locked", None)
    read_bytes = getattr(state, "read_bytes", None)
    if not callable(locked) or not callable(read_bytes):
        raise ValueError("plugin_lifecycle_revision_unavailable")
    with locked():
        payload = read_bytes()
    if payload is not None and type(payload) is not bytes:
        raise ValueError("plugin_lifecycle_revision_unavailable")
    return sha256_hex(
        canonical_bytes(
            {
                "domain": "omega.plugin-lifecycle.component.v1",
                "label": label,
                "present": payload is not None,
                "payload_sha256": "" if payload is None else sha256(payload).hexdigest(),
            }
        )
    )


@dataclass(frozen=True, slots=True)
class PluginAppSelectionRequired:
    """Typed response for a lifecycle mutation that needs one exact app."""

    plugin_id: str
    app_ids: tuple[str, ...]
    error_code: str = "plugin_app_selection_required"

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "app_ids": list(self.app_ids),
            "error_code": self.error_code,
        }


@dataclass(frozen=True, slots=True)
class PluginUpdatePending:
    """Server-held candidate continuation exposed without candidate identity."""

    plugin_id: str
    app_ids: tuple[str, ...]
    next_required_app_id: str
    status: str = "setup_required"
    pending: bool = True

    def __post_init__(self) -> None:
        if (
            type(self.plugin_id) is not str
            or not self.plugin_id
            or type(self.app_ids) is not tuple
            or not self.app_ids
            or len(self.app_ids) != len(set(self.app_ids))
            or any(type(item) is not str or not item for item in self.app_ids)
            or self.next_required_app_id not in self.app_ids
            or self.status != "setup_required"
            or self.pending is not True
        ):
            raise ValueError("plugin_update_state_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "status": self.status,
            "app_ids": list(self.app_ids),
            "next_required_app_id": self.next_required_app_id,
        }


@dataclass(frozen=True, slots=True)
class PluginUpdateContinuation:
    plugin_id: str
    state: PluginLifecycleState
    next_required_app_id: str | None

    @property
    def status(self) -> str:
        return self.state.value

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "state": self.state.value,
            "status": self.state.value,
            "next_required_app_id": self.next_required_app_id,
        }


@dataclass(frozen=True, slots=True)
class PluginCatalogPage:
    """One bounded, deterministically ordered page of a marketplace search.

    ``items`` are fully projected, response-ready documents (each source
    entry's own ``to_dict()`` plus the computed ``lifecycle_state`` for an
    installed plugin) -- the exact shape the HTTP catalog route used to build
    by hand.
    """

    items: tuple[dict[str, object], ...]
    next_cursor: str | None
    status: str
    total: int

    def __post_init__(self) -> None:
        if (
            type(self.items) is not tuple
            or any(type(item) is not dict for item in self.items)
            or self.status not in {"ok", "degraded"}
            or (self.next_cursor is not None and type(self.next_cursor) is not str)
            or type(self.total) is not int
            or self.total < 0
        ):
            raise ValueError("plugin_catalog_page_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "items": [dict(item) for item in self.items],
            "next_cursor": self.next_cursor,
            "status": self.status,
            "total": self.total,
        }


@dataclass(frozen=True, slots=True)
class PluginUpdateReview:
    """The complete identity/capability/permission diff for one catalog update.

    ``sha256`` is the exact digest ``PluginLifecycleService.update`` requires as
    its ``confirmation`` -- it is the same digest ``review()``/``install()``
    already compute for the candidate version, so update confirmation reuses
    the one existing signed-install confirmation boundary rather than minting a
    second one.
    """

    plugin_id: str
    from_version: str
    to_version: str
    permission_changes: tuple[str, ...]
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.plugin_id) is not str
            or not self.plugin_id
            or type(self.from_version) is not str
            or not self.from_version
            or type(self.to_version) is not str
            or not self.to_version
            or type(self.permission_changes) is not tuple
            or any(type(item) is not str for item in self.permission_changes)
            or len(self.permission_changes) != len(set(self.permission_changes))
            or type(self.sha256) is not str
            or len(self.sha256) != 64
        ):
            raise ValueError("plugin_update_review_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "permission_changes": list(self.permission_changes),
            "sha256": self.sha256,
        }


@dataclass(frozen=True, slots=True)
class PluginDiscoveryInstallReview:
    """A discovery-based review for one publisher-verified marketplace entry.

    Every listed capability carries ``RiskClass.EXTERNAL_UNREVIEWED``
    (``mcp_platform.plugin_discovery``): Omega has not endorsed this plugin's
    behavior, only its source namespace. Nothing here has ever been called.
    """

    plugin_id: str
    publisher_id: str
    plugin_version: str
    trust_tier: str
    review: CapabilityDiscoveryReview
    sha256: str

    def __post_init__(self) -> None:
        if (
            type(self.plugin_id) is not str
            or not self.plugin_id
            or type(self.publisher_id) is not str
            or not self.publisher_id
            or type(self.plugin_version) is not str
            or not self.plugin_version
            or self.trust_tier != TrustTier.PUBLISHER_VERIFIED.value
            or type(self.review) is not CapabilityDiscoveryReview
            or self.sha256 != self.review.review_sha256
        ):
            raise ValueError("plugin_discovery_install_review_invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "plugin_id": self.plugin_id,
            "publisher_id": self.publisher_id,
            "plugin_version": self.plugin_version,
            "trust_tier": self.trust_tier,
            "sha256": self.sha256,
            "capabilities": [
                capability.to_dict()
                for group in (
                    self.review.tools,
                    self.review.resources,
                    self.review.resource_templates,
                    self.review.prompts,
                )
                for capability in group
            ],
        }


def _catalog_cursor(
    key: bytes,
    *,
    offset: int,
    query: str,
    filters: tuple[tuple[str, str], ...],
    page_size: int,
    catalog_revision: str,
) -> str:
    payload = {
        "schema_version": _CATALOG_CURSOR_SCHEMA_VERSION,
        "offset": offset,
        "query": query,
        "filters": list(list(item) for item in filters),
        "page_size": page_size,
        "catalog_revision": catalog_revision,
    }
    payload_bytes = canonical_bytes(payload)
    envelope = {
        "payload": payload,
        "mac": hmac.new(key, _CATALOG_CURSOR_DOMAIN + payload_bytes, sha256).hexdigest(),
    }
    return base64.urlsafe_b64encode(canonical_bytes(envelope)).decode("ascii").rstrip("=")


def _decode_catalog_cursor(
    key: bytes,
    value: str,
    *,
    query: str,
    filters: tuple[tuple[str, str], ...],
    page_size: int,
    catalog_revision: str,
) -> int:
    try:
        if type(value) is not str or not value:
            raise ValueError
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        envelope = json.loads(raw.decode("utf-8"))
        if type(envelope) is not dict or set(envelope) != {"payload", "mac"}:
            raise ValueError
        payload = envelope["payload"]
        supplied_mac = envelope["mac"]
        if (
            type(payload) is not dict
            or set(payload)
            != {"schema_version", "offset", "query", "filters", "page_size", "catalog_revision"}
            or type(supplied_mac) is not str
        ):
            raise ValueError
        expected_mac = hmac.new(
            key,
            _CATALOG_CURSOR_DOMAIN + canonical_bytes(payload),
            sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied_mac, expected_mac):
            raise ValueError
        if (
            payload["schema_version"] != _CATALOG_CURSOR_SCHEMA_VERSION
            or payload["query"] != query
            or [list(item) for item in filters] != payload["filters"]
            or payload["page_size"] != page_size
            or type(payload["offset"]) is not int
            or payload["offset"] < 1
        ):
            raise ValueError("plugin_catalog_cursor_invalid")
        if payload["catalog_revision"] != catalog_revision:
            raise ValueError("plugin_catalog_cursor_stale")
        return payload["offset"]
    except ValueError as exc:
        if str(exc) in {"plugin_catalog_cursor_invalid", "plugin_catalog_cursor_stale"}:
            raise
        raise ValueError("plugin_catalog_cursor_invalid") from None
    except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("plugin_catalog_cursor_invalid") from None


def _provenance_changed(
    installed: ProviderProvenanceRecord | None,
    candidate: ProviderProvenanceRecord | None,
) -> bool:
    """Compare provider identity, source URL, and a whole-record digest.

    Provenance can change while every manifest/package digest stays the same
    (a re-issued signed provenance record for the same bytes) -- this must
    still surface as a permission change, never silently pass review.
    """

    if (installed is None) != (candidate is None):
        return True
    if installed is None or candidate is None:
        return False
    return (
        installed.provider_publisher_id != candidate.provider_publisher_id
        or installed.provider_id != candidate.provider_id
        or installed.authoritative_source_url != candidate.authoritative_source_url
        or sha256_hex(canonical_bytes(installed.to_dict()))
        != sha256_hex(canonical_bytes(candidate.to_dict()))
    )


def _update_permission_changes(
    installed: VerifiedPlugin,
    candidate: VerifiedPlugin,
    *,
    installed_provenance: ProviderProvenanceRecord | None,
    candidate_provenance: ProviderProvenanceRecord | None,
) -> tuple[str, ...]:
    """Diff package, artifact, provenance, endpoint, authentication, scopes,
    capabilities, and the network/filesystem boundary.

    Purely a comparison of already-verified evidence; nothing here mutates or
    grants anything. Any listed change means the candidate's execution
    authority is not identical to the installed one's. The network boundary is
    the set of remote origins the candidate's apps declare; the filesystem
    boundary is any local launch descriptor -- a remote-only candidate (no
    ``AppDeployment.LOCAL`` app) never reports one.
    """

    changes: list[str] = []
    if installed.manifest_sha256 != candidate.manifest_sha256:
        changes.append("manifest_changed")
    if installed.package_sha256 != candidate.package_sha256:
        changes.append("package_changed")
    if _provenance_changed(installed_provenance, candidate_provenance):
        changes.append("provenance_changed")
    installed_apps = {
        item.app_id: item
        for item in (
            installed.manifest.required_apps + installed.manifest.optional_apps
        )
    }
    candidate_apps = {
        item.app_id: item
        for item in (
            candidate.manifest.required_apps + candidate.manifest.optional_apps
        )
    }

    def capability_tuple(item: Any) -> tuple[str, str, str | None]:
        return (item.kind.value, item.name, item.schema_sha256)

    for app_id in sorted(candidate_apps):
        candidate_app = candidate_apps[app_id]
        installed_app = installed_apps.get(app_id)
        if installed_app is None:
            changes.append(f"app_added:{app_id}")
            continue
        if installed_app.remote_origin != candidate_app.remote_origin:
            changes.append(f"endpoint_changed:{app_id}")
        if installed_app.authentication.kind != candidate_app.authentication.kind:
            changes.append(f"authentication_changed:{app_id}")
        if tuple(installed_app.authentication.scopes) != tuple(candidate_app.authentication.scopes):
            changes.append(f"scopes_changed:{app_id}")
        installed_capabilities = tuple(
            capability_tuple(item)
            for item in sorted(
                installed_app.capabilities, key=lambda item: (item.kind.value, item.name)
            )
        )
        candidate_capabilities = tuple(
            capability_tuple(item)
            for item in sorted(
                candidate_app.capabilities, key=lambda item: (item.kind.value, item.name)
            )
        )
        if installed_capabilities != candidate_capabilities:
            changes.append(f"capabilities_changed:{app_id}")
    for app_id in sorted(set(installed_apps) - set(candidate_apps)):
        changes.append(f"app_removed:{app_id}")

    installed_origins = frozenset(
        item.remote_origin
        for item in installed_apps.values()
        if item.deployment is AppDeployment.REMOTE and item.remote_origin is not None
    )
    candidate_origins = frozenset(
        item.remote_origin
        for item in candidate_apps.values()
        if item.deployment is AppDeployment.REMOTE and item.remote_origin is not None
    )
    if installed_origins != candidate_origins:
        changes.append("network_boundary_changed")

    candidate_has_local = any(
        item.deployment is AppDeployment.LOCAL for item in candidate_apps.values()
    )
    if candidate_has_local:
        installed_has_local = any(
            item.deployment is AppDeployment.LOCAL for item in installed_apps.values()
        )
        if not installed_has_local or installed.package_sha256 != candidate.package_sha256:
            changes.append("filesystem_boundary_changed")
    return tuple(changes)


def _complete_authority_projection(
    plugin: VerifiedPlugin,
    provenance: ProviderProvenanceRecord | None,
) -> dict[str, object]:
    """Project every declared authority field, excluding package identity.

    Version, signature, and byte digests identify an artifact; they are not
    permission.  Everything else in the signed manifest and provenance is
    retained in this projection so an automatic update can only reuse a grant
    when the complete declared authority is byte-for-byte equivalent.
    """

    def json_safe(value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): json_safe(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [json_safe(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        raise ValueError("plugin_authority_projection_invalid")

    manifest = json_safe(plugin.manifest.to_dict())
    if type(manifest) is not dict:
        raise ValueError("plugin_authority_projection_invalid")
    for field in (
        "plugin_version",
        "signing_key_id",
        "signature",
        # Assets and presentation metadata identify the package contents, not
        # the authority granted to its MCP applications.  The signed app
        # requirements and tools/authority.json carry the complete permission
        # boundary; capability authority is therefore deliberately retained.
        "assets",
        "title",
        "description",
    ):
        manifest.pop(field, None)
    return {
        "manifest": manifest,
        "capability_authority": None
        if plugin.capability_authority is None
        else json_safe(plugin.capability_authority),
        "trust_tier": plugin.trust_tier.value,
        "legacy_authority": plugin.legacy_authority,
        "provenance": None if provenance is None else provenance.to_dict(),
    }


def complete_authority_equivalent(
    installed: VerifiedPlugin,
    candidate: VerifiedPlugin,
    *,
    installed_provenance: ProviderProvenanceRecord | None,
    candidate_provenance: ProviderProvenanceRecord | None,
) -> bool:
    """Return whether an installed grant may be reused for a candidate."""

    return _complete_authority_projection(
        installed, installed_provenance
    ) == _complete_authority_projection(candidate, candidate_provenance)


class VerifiedCatalogSourceAdapter:
    """Expose exact provider-owned remote MCP servers through the directory."""

    descriptor = CatalogSourceDescriptor(
        source_id="providers.authoritative",
        title="Authoritative MCP providers",
        kind=CatalogSourceKind.MARKETPLACE,
    )

    def __init__(self, service: PluginLifecycleService) -> None:
        self.service = service

    def snapshot(self) -> CatalogSourceSnapshot:
        catalog = self.service._current_catalog()
        plugins: list[CatalogPluginEntry] = []
        for catalog_entry in sorted(catalog.entries, key=lambda item: item.plugin_id):
            verified_catalog, entry, verified, provenance = (
                self.service._verified_with_provenance(catalog_entry.plugin_id)
            )
            if verified_catalog.catalog_sha256 != catalog.catalog_sha256:
                raise RuntimeError("plugin_catalog_unavailable")
            manifest = verified.manifest
            plugins.append(
                CatalogPluginEntry(
                    plugin_id=manifest.plugin_id,
                    publisher_id=manifest.publisher_id,
                    plugin_version=manifest.plugin_version,
                    title=catalog_entry.title,
                    description=catalog_entry.summary,
                    signing_key_id=manifest.signing_key_id,
                    manifest_sha256=entry.manifest_sha256,
                    package_sha256=entry.package_sha256,
                    required_app_ids=tuple(item.app_id for item in manifest.required_apps),
                    optional_app_ids=tuple(item.app_id for item in manifest.optional_apps),
                    starter_requests=tuple(manifest.starter_requests),
                    entry_point_ids=tuple(item.entry_point_id for item in manifest.entry_points),
                    categories=catalog_entry.categories,
                    trust_tier=catalog_entry.trust_tier.value,
                    provider_publisher_id=provenance.provider_publisher_id,
                    publisher_display_name=provenance.publisher_display_name,
                    provider_id=provenance.provider_id,
                    remote_origin=provenance.remote_origin,
                    authoritative_source_url=provenance.authoritative_source_url,
                    provenance="authoritative_remote_mcp",
                )
            )
        return CatalogSourceSnapshot(
            source=self.descriptor,
            revision=catalog.catalog_sha256,
            plugins=tuple(plugins),
        )

    def review(self, plugin_id: str) -> PluginInstallReview:
        return self.service.review(plugin_id)

    def install(self, plugin_id: str, *, confirmation: object) -> PluginInstallResult:
        return self.service.install(plugin_id, confirmation=confirmation)


class _RemoteOnlyResolver:
    """Resolve only signed remote requirements for the verified catalog."""

    def resolve(self, requirement: Any) -> TrustedRemoteAppSource:
        if requirement.deployment.value != "remote":
            raise ValueError("plugin_runtime_unsupported")
        return TrustedRemoteAppSource(requirement=requirement)


class _DigestConfirmation:
    """Require the caller to confirm the exact review digest it was shown."""

    def consume(self, confirmation: object, expected_review_sha256: str) -> None:
        if type(confirmation) is not str or confirmation != expected_review_sha256:
            from plugin_platform.installer import PluginInstallError

            raise PluginInstallError("plugin_install_confirmation_mismatch")


def _synthetic_registry_server(entry: MarketplaceCatalogEntry) -> McpRegistryServer:
    """Project one publisher-verified catalog entry into the minimal registry-
    listing shape ``MarketplaceImporter.normalize`` needs to build a candidate.

    Nothing here is fetched from the network or invented: every field either
    comes directly from the signed catalog entry, or is a fixed, content-free
    constant. The entry's own ``plugin_id``/``publisher_id`` (both already
    validated identifiers the signed catalog vouches for) must decompose the
    way every plugin identity in this system does -- ``publisher_id`` plus a
    dot plus one further, dot-free segment -- because that is the one mapping
    ``MarketplaceImporter`` can re-derive without being told; a catalog entry
    that does not fits fails closed here rather than importing under a
    silently different identity than the one the user is installing.
    """
    if entry.distribution.kind is not DistributionKind.REMOTE_MCP:
        raise ValueError("plugin_runtime_unsupported")
    prefix = f"{entry.publisher_id}."
    server_name = entry.plugin_id.removeprefix(prefix)
    if server_name == entry.plugin_id or "." in server_name:
        raise ValueError("plugin_catalog_identity_ambiguous")
    return McpRegistryServer(
        name=f"{entry.publisher_id}/{server_name}",
        namespace=entry.publisher_id,
        namespace_kind=NamespaceKind.DNS,
        namespace_owner=entry.publisher_id,
        server_name=server_name,
        version=entry.plugin_version,
        description=entry.summary,
        title=entry.title,
        status=RegistryStatus.ACTIVE,
        published_at=entry.released_at,
        is_latest=True,
        website_url=None,
        repository_url=None,
        packages=(),
        remotes=(
            McpRegistryRemote(
                transport=RemoteTransport.STREAMABLE_HTTP,
                url=entry.distribution.remote_origin,
                headers=(),
            ),
        ),
        unsupported_reasons=(),
        trust_tier=TrustTier.PUBLISHER_VERIFIED,
        extensions_sha256=None,
    )


#: The revision of a genuinely empty declared-capability set, computed the
#: same way ``mcp_platform.capabilities._capability_revision`` would for
#: ``{"tools": (), "prompts": (), "resources": (), "extensions": ()}`` --
#: every discovery-only descriptor's declared capabilities are empty by
#: construction (nothing has been reviewed yet), so this is always the
#: right value, not a placeholder. Restricted discovery never reconciles
#: against it (``CurrentMcpClient.discover_restricted`` never calls
#: ``reconcile_live_capabilities``), so nothing ever actually compares this
#: field to anything; it exists only because ``McpConnectionDescriptor``
#: requires a syntactically valid digest here regardless.
_DISCOVERY_CAPABILITY_REVISION = sha256_hex(
    canonical_bytes({"tools": [], "prompts": [], "resources": [], "extensions": []})
)


def _discovery_descriptor_for_entry(entry: MarketplaceCatalogEntry) -> McpConnectionDescriptor:
    """Build the discovery-only descriptor for one publisher-verified entry.

    Endpoint and authorization issuer both come from the entry's own
    declared remote origin -- ``entry.distribution.remote_origin`` is
    already canonical-HTTPS-validated at catalog-parse time
    (``plugin_platform.catalog.MarketplaceDistribution.from_dict``), and the
    issuer is derived from it exactly as
    ``plugin_platform.marketplace_importer._endpoint_issuer`` already does
    for the same field elsewhere (the endpoint's own origin, since the
    signed catalog states no separate issuer). Authentication is always
    ``none``: neither ``MarketplaceCatalogEntry`` nor
    ``MarketplaceDistribution`` carries any field that could declare a
    different kind, so this is not a placeholder -- it is the only
    authentication a signed catalog entry can state today. Declared
    capabilities are empty by construction: nothing has been reviewed yet.
    """
    if entry.distribution.kind is not DistributionKind.REMOTE_MCP:
        raise ValueError("plugin_runtime_unsupported")
    origin = entry.distribution.remote_origin
    if type(origin) is not str:
        raise ValueError("plugin_runtime_unsupported")
    parsed = urlsplit(origin)
    issuer = f"{parsed.scheme}://{parsed.netloc}"
    return McpConnectionDescriptor(
        app_id=entry.plugin_id,
        publisher_id=entry.publisher_id,
        deployment=McpClientDeployment.REMOTE,
        provider_id=None,
        protocol_version=PLUGIN_PROTOCOL_VERSION,
        transport=McpTransport.STREAMABLE_HTTP,
        verified_launch_sha256=None,
        remote_origin=origin,
        authorization_issuer=issuer,
        authentication=PluginAuthentication.from_dict(
            {"kind": "none", "scopes": [], "secret_fields": []}
        ),
        declared_capabilities={},
        extension_versions={},
        capability_revision=_DISCOVERY_CAPABILITY_REVISION,
        limits=McpClientLimits(),
    )


def _default_discovery_peer_provider(entry: MarketplaceCatalogEntry) -> DiscoveryPeer:
    """The default ``discovery_peer_provider`` -- production restricted discovery.

    Builds the one production ``DiscoveryPeer``
    (``mcp_platform.plugin_discovery_transport.RestrictedDiscoveryPeer``) for
    one publisher-verified marketplace entry. This is what makes the
    zero-argument ``compose_plugin_lifecycle_service()`` composition perform
    real restricted discovery instead of raising ``plugin_runtime_unsupported``.
    """
    descriptor = _discovery_descriptor_for_entry(entry)
    return build_restricted_discovery_peer(descriptor)


def _discovered_capability_scope(review: CapabilityDiscoveryReview) -> tuple[str, ...]:
    """Every compatible capability tag one restricted-discovery review enumerated.

    A publisher-verified install has no separate scoping step today (the
    discovery review the user confirmed already lists every capability that
    will compile at ``external_unreviewed`` risk) -- this names the exact set
    ``RestrictedPluginDiscovery.approve`` requires as its ``connection_scope``,
    in the same ``kind:name`` shape it validates internally. A capability
    whose literal schema fails the planner's closed-schema projection
    (``DiscoveredCapability.compatible is False``) is never auto-scoped: it
    stays discoverable and reviewable but incompatible and unsealed, rather
    than failing the whole install or being sealed with a looser schema.
    """
    return tuple(
        sorted(
            {
                f"{item.kind}:{item.name}"
                for item in (
                    *review.tools,
                    *review.prompts,
                    *review.resources,
                    *review.resource_templates,
                )
                if item.compatible
            }
        )
    )


def _combined_plugin_package_verifier(
    key_ring: Any,
    omega_version: str,
    state_root: Path,
    local_trust_root_provider: Callable[[], LocalTrustRoot],
) -> Callable[[Path], VerifiedPlugin]:
    """Return the one verifier the plugin registry uses for every package tree.

    Dispatch never widens trust: a tree is only ever routed to the locally-
    sealed path when its own manifest declares the fixed unsealed signing key
    marketplace-imported candidates carry (see
    ``plugin_platform.marketplace_importer.UNSEALED_SIGNING_KEY_ID``), and
    that path still re-verifies everything from scratch through
    ``verify_locally_sealed_plugin_package`` -- a wrong dispatch can only ever
    fail closed (the Ed25519 path can never validate an unsealed manifest, and
    the sealed path can never validate a real marketplace signature), never
    grant a tier the tree's own content does not independently re-prove. The
    device-local seal cannot live inside the manifest-declared asset set the
    registry publishes from (see ``plugin_platform.installer.accept_normalized``),
    so it is read from the durable seal store by the sealed manifest's own
    digest instead of from the tree.
    """
    from plugin_platform.marketplace_importer import UNSEALED_SIGNING_KEY_ID

    def verify(path: Path) -> VerifiedPlugin:
        root = Path(path)
        manifest_bytes: bytes | None = None
        sealed = False
        try:
            manifest_bytes = (root / "plugin.json").read_bytes()
            manifest_document = json.loads(manifest_bytes)
            sealed = (
                type(manifest_document) is dict
                and manifest_document.get("signing_key_id") == UNSEALED_SIGNING_KEY_ID
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            sealed = False
        if not sealed or manifest_bytes is None:
            return verify_plugin_package(root, key_ring, omega_version)
        manifest_sha256 = sha256_hex(manifest_bytes)
        seal_document = read_local_seal_document(state_root, manifest_sha256)
        if seal_document is None:
            raise PackageVerificationError("local_seal_unavailable")
        trust_root = local_trust_root_provider()
        return verify_locally_sealed_plugin_package(
            root, trust_root, external_seal_document=seal_document
        )

    return verify


_OPERATOR_ACCOUNT_FILENAME = "operator-account.json"


def _durable_operator_account_key(root: Path) -> str:
    """Read this installation's operator account key, birthing it once.

    Written with an exclusive create so two processes racing a first launch
    agree on one key rather than partitioning plugin state two ways.
    """
    path = Path(root) / _OPERATOR_ACCOUNT_FILENAME
    try:
        document = json.loads(path.read_bytes().decode("utf-8"))
        existing = document["account_key"]
        if type(existing) is str and existing.strip():
            return existing.strip()
        raise ValueError("plugin_account_unconfigured")
    except FileNotFoundError:
        pass
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError):
        raise ValueError("plugin_account_unconfigured") from None

    minted = secrets.token_hex(32)
    payload = json.dumps({"schema_version": 1, "account_key": minted}).encode("utf-8")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        return _durable_operator_account_key(root)
    except OSError:
        raise ValueError("plugin_account_unconfigured") from None
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return minted


_LOG = logging.getLogger("omega.mcp.plugin.lifecycle")


def _raising_site(error: BaseException) -> str:
    """Where in Omega's own source this was raised, as ``file.py:line in func``.

    Only Omega's source names appear -- never the exception message, which is
    the one part a provider could have written.
    """

    frame = None
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback
        traceback = traceback.tb_next
    if frame is None:
        return "no traceback"
    code = frame.tb_frame.f_code
    return f"{os.path.basename(code.co_filename)}:{frame.tb_lineno} in {code.co_name}"


class PluginLifecycleService:
    """Own verified catalog, install, connection, authority, and runtime lifecycle."""

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        oauth_broker: OAuthBroker | None = None,
        plugin_grants: PluginGrantService | None = None,
        profile_match_provider: (
            Callable[[str, InstalledMcpAppRecord], OAuthProfileMatchInput] | None
        ) = None,
        operation_submitter: PluginOperationSubmitter | None = None,
        transport_authority: object | None = None,
        permission_store: PluginPermissionStore | None = None,
        authority_service: PluginAuthorityService | None = None,
        oauth_protocol: OAuthProtocolClient | None = None,
        discovery_peer_provider: (
            Callable[[MarketplaceCatalogEntry], DiscoveryPeer] | None
        ) = None,
        local_trust_key_provider: Callable[[], bytes] | None = None,
    ) -> None:
        try:
            self._initialize(
                state_root=state_root,
                oauth_broker=oauth_broker,
                plugin_grants=plugin_grants,
                profile_match_provider=profile_match_provider,
                operation_submitter=operation_submitter,
                transport_authority=transport_authority,
                permission_store=permission_store,
                authority_service=authority_service,
                oauth_protocol=oauth_protocol,
                discovery_peer_provider=discovery_peer_provider,
                local_trust_key_provider=local_trust_key_provider,
            )
        except BaseException:
            self._lifecycle_initializing = False
            self._exit_initialization_mutation_scope(sys.exc_info())
            self._restore_detached_component_guards()
            self._close_lifecycle_owner_lease()
            raise

    def _restore_detached_component_guards(self) -> None:
        detached = getattr(self, "_detached_component_guards", ())
        self._detached_component_guards = []
        with _COMPONENT_GUARD_LOCK:
            for component, previous, previous_async in detached:
                current = getattr(component, "_lifecycle_mutation_guard", None)
                if getattr(current, "__self__", None) is self:
                    if previous is _MISSING_COMPONENT_GUARD:
                        try:
                            delattr(component, "_lifecycle_mutation_guard")
                        except AttributeError:
                            pass
                    else:
                        setattr(component, "_lifecycle_mutation_guard", previous)
                current_async = getattr(
                    component,
                    "_lifecycle_mutation_async_guard",
                    None,
                )
                if getattr(current_async, "__self__", None) is self:
                    if previous_async is _MISSING_COMPONENT_GUARD:
                        try:
                            delattr(component, "_lifecycle_mutation_async_guard")
                        except AttributeError:
                            pass
                    else:
                        setattr(
                            component,
                            "_lifecycle_mutation_async_guard",
                            previous_async,
                        )

    def _exit_initialization_mutation_scope(
        self,
        exc_info: tuple[object, object, object],
    ) -> None:
        scope = getattr(self, "_initialization_mutation_scope", None)
        self._initialization_mutation_scope = None
        if scope is not None:
            getattr(scope, "__exit__")(*exc_info)

    def _provider_update_lookup(self, status: McpProviderDriftStatus) -> McpProviderRecovery:
        """Perform one lifecycle-owned signed update lookup for provider drift.

        The backend owns the one-attempt budget.  This adapter deliberately
        delegates catalog verification and compatible-update policy to the
        lifecycle service instead of letting a provider worker fetch or trust
        an arbitrary package. A missing candidate or lifecycle conflict returns
        a typed reconnect outcome; authority changes return the stable approval
        diff so the backend can expose it to the action caller.
        """

        if type(status) is not McpProviderDriftStatus:
            return McpProviderRecovery(status="reconnect_required")
        if type(status.plugin_id) is not str:
            return McpProviderRecovery(status="reconnect_required")
        if status.reason.value not in {
            "capability_drift",
            "identity_mismatch",
            "protocol_mismatch",
            "transport_unavailable",
        }:
            return McpProviderRecovery(status="reconnect_required")
        inside_fence = self._lifecycle_mutation_depth.get() > 0
        try:
            if inside_fence:
                # Backend execution already owns the non-recursive lifecycle
                # fence.  A forced lookup may inspect the signed catalog here,
                # but cannot begin a nested journaled update; that is deferred
                # to the normal lifecycle owner boundary.
                result = self.check_updates(
                    force=True, _already_locked=True, _apply=False
                )
            else:
                result = self.check_updates(force=True)
        except Exception:
            return McpProviderRecovery(status="reconnect_required")
        affected = tuple(
            candidate
            for candidate in result.candidate_versions
            if candidate[0] == status.plugin_id
        )
        staged = getattr(result, "staged", ())
        approval_required = getattr(result, "approval_required", ())
        if result.status != "checked" or getattr(result, "error", None):
            return McpProviderRecovery(status="reconnect_required")
        affected_approval = tuple(
            item for item in approval_required if item[0] == status.plugin_id
        )
        if affected_approval:
            return McpProviderRecovery(
                status="approval_required", approval_diff=tuple(affected_approval[0][2])
            )
        if any(candidate[0] == status.plugin_id for candidate in staged):
            return McpProviderRecovery(
                status="approval_required", approval_diff=("update_pending",)
            )
        if not affected:
            return McpProviderRecovery(status="reconnect_required")
        if inside_fence:
            # The execution fence is intentionally non-recursive.  Queue the
            # exact signed candidate for the lifecycle execution boundary,
            # which drains it after the provider action releases the fence.
            # Reporting a pending typed outcome keeps this compatible path
            # distinct from a credential reconnect and prevents a premature
            # retry against the old authority snapshot.
            self._queue_deferred_update_application(
                *affected[0], generation=getattr(result, "generation", 0)
            )
            return McpProviderRecovery(
                status="approval_required",
                approval_diff=("update_pending",),
                retry_candidate=affected[0],
                generation=getattr(result, "generation", 0),
            )
        # check_updates applies the compatible candidate before returning.
        # A candidate still present here was therefore verified and applied.
        return McpProviderRecovery(status="applied")

    def _initialize(
        self,
        *,
        state_root: Path | None = None,
        oauth_broker: OAuthBroker | None = None,
        plugin_grants: PluginGrantService | None = None,
        profile_match_provider: (
            Callable[[str, InstalledMcpAppRecord], OAuthProfileMatchInput] | None
        ) = None,
        operation_submitter: PluginOperationSubmitter | None = None,
        transport_authority: object | None = None,
        permission_store: PluginPermissionStore | None = None,
        authority_service: PluginAuthorityService | None = None,
        oauth_protocol: OAuthProtocolClient | None = None,
        discovery_peer_provider: (
            Callable[[MarketplaceCatalogEntry], DiscoveryPeer] | None
        ) = None,
        local_trust_key_provider: Callable[[], bytes] | None = None,
    ) -> None:
        self._closed = False
        self._close_lock = threading.Lock()
        self._retain_lifecycle_owner_lease = False
        if state_root is None:
            plugin_state_root = core_paths.PLUGIN_STATE_ROOT
            mcp_state_root = core_paths.MCP_STATE_ROOT
        else:
            root = Path(state_root)
            plugin_state_root = root / "plugins"
            mcp_state_root = root / "mcp"
        self._plugin_state_root = plugin_state_root
        self._publisher_ring = load_embedded_key_ring()
        self._mcp_publisher_ring = load_mcp_embedded_key_ring()
        self._discovery_peer_provider = discovery_peer_provider
        self._local_trust_key_provider = local_trust_key_provider
        # The on-device catalog source of truth: bundled snapshot plus any
        # locally synced replacement, both signed. sync_catalog() is the only
        # path that ever replaces `catalog_path`'s content, and it never
        # installs or enables anything. Cached below (_current_catalog) keyed
        # by a cheap on-disk fingerprint so repeated reads inside one search
        # or install pass do not each re-verify both signatures.
        self._catalog_store = CatalogStore(
            root=plugin_state_root / "marketplace",
            bundled_path=_CATALOG_PATH,
            key_ring=self._publisher_ring,
        )
        self._catalog_fetch: CatalogFetch = build_marketplace_catalog_fetch()
        self._catalog_cache_lock = threading.Lock()
        self._catalog_cache: VerifiedMarketplaceCatalog | None = None
        self._catalog_cache_fingerprint: tuple[object, ...] | None = None
        # Process-local opaque-cursor MAC key: cursors are never valid across a
        # process restart, matching hud/server.py's own catalog cursor.
        self._catalog_cursor_key = secrets.token_bytes(32)
        self._lifecycle_initializing = True
        self._configure_lifecycle_owner_state(plugin_state_root)
        if not self._claim_lifecycle_operation_ownership():
            raise ValueError("plugin_lifecycle_operation_in_progress")
        self._uninstall_state = McpStateStore(
            state_root=plugin_state_root,
            document_path=plugin_state_root / "lifecycle-uninstall.json",
            invalid_path_reason="plugin_uninstall_incomplete",
            max_document_bytes=1024 * 1024,
        )
        self._update_state = McpStateStore(
            state_root=plugin_state_root,
            document_path=plugin_state_root / "lifecycle-update.json",
            invalid_path_reason="plugin_update_recovery_required",
            max_document_bytes=1024 * 1024,
        )
        self._update_policy_store = policy_store(plugin_state_root)
        self._update_policy = read_policy(self._update_policy_store)
        self._update_status_store = update_status_store(plugin_state_root)
        self._last_update_check = read_update_status(self._update_status_store)
        self._due_check_trigger_lock = threading.Lock()
        self._update_heartbeat_stop = threading.Event()
        self._update_heartbeat_thread: threading.Thread | None = None
        self._deferred_update_lock = threading.Lock()
        self._deferred_update_applications: list[tuple[str, str]] = []
        self._deferred_update_generations: dict[tuple[str, str], int] = {}
        self._package_resolver = EmbeddedCatalogPackageResolver(
            root=_MARKETPLACE_ROOT,
            key_ring=self._publisher_ring,
            omega_version=_OMEGA_VERSION,
        )
        self._package_fetcher = PackageArtifactFetcher(
            root=plugin_state_root / "artifacts",
            key_ring=self._publisher_ring,
            omega_version=_OMEGA_VERSION,
        )
        self.app_registry = McpAppRegistry(state_root=mcp_state_root)
        package_registry = PackageRegistry(
            state_root=mcp_state_root,
            verifier=lambda path: verify_package(
                path,
                self._mcp_publisher_ring,
                omega_version=_OMEGA_VERSION,
                platform_tag=current_platform_tag(),
            ),
        )
        self.package_registry = package_registry
        provisioner = McpAppProvisioner(
            app_registry=self.app_registry,
            package_registry=package_registry,
            source_resolver=_RemoteOnlyResolver(),
        )
        # Uninstall retires this plugin's provision journal entries; without a
        # handle here they outlive the app records and keep protecting them.
        self._provisioner = provisioner
        self.plugin_registry = PluginRegistry(
            state_root=plugin_state_root,
            verifier=_combined_plugin_package_verifier(
                self._publisher_ring,
                _OMEGA_VERSION,
                Path(os.path.abspath(os.fspath(plugin_state_root))),
                self._local_trust_root,
            ),
        )
        self._installer = PluginInstaller(
            registry=self.plugin_registry,
            provisioner=provisioner,
            confirmation_verifier=_DigestConfirmation(),
        )
        connection_dependencies = (
            oauth_broker,
            plugin_grants,
            profile_match_provider,
            operation_submitter,
        )
        configured = any(item is not None for item in connection_dependencies)
        if configured:
            from mcp_platform.offline_recovery import require_recovery_complete

            require_recovery_complete(plugin_state_root)
        if configured and any(item is None for item in connection_dependencies):
            raise ValueError("oauth_profile_catalog_unavailable")
        self.plugin_grants = plugin_grants
        self.permission_store = (
            permission_store
            if permission_store is not None
            else getattr(plugin_grants, "permission_store", None)
        )
        self.authority_service = authority_service
        self._operation_submitter = operation_submitter
        self._oauth_protocol = oauth_protocol
        self._oauth_vault = getattr(oauth_broker, "_vault", None)
        self._detached_component_guards: list[tuple[object, object, object]] = []
        self._authority_mutation_lock = _LifecycleMutationBoundary(
            self._lifecycle_mutation_lease
        )
        with _COMPONENT_GUARD_LOCK:
            seen_components: set[int] = set()
            for component in (
                self.permission_store,
                self.authority_service,
                self.plugin_grants,
            ):
                if component is None or id(component) in seen_components:
                    continue
                seen_components.add(id(component))
                previous = getattr(
                    component,
                    "_lifecycle_mutation_guard",
                    _MISSING_COMPONENT_GUARD,
                )
                previous_async = getattr(
                    component,
                    "_lifecycle_mutation_async_guard",
                    _MISSING_COMPONENT_GUARD,
                )
                for guard in (previous, previous_async):
                    owner = getattr(guard, "__self__", None)
                    if (
                        isinstance(owner, PluginLifecycleService)
                        and owner is not self
                        and not getattr(owner, "_closed", False)
                    ):
                        raise ValueError("plugin_lifecycle_operation_conflict")
                self._detached_component_guards.append(
                    (component, previous, previous_async)
                )
            for component, _previous, _previous_async in self._detached_component_guards:
                setattr(
                    component,
                    "_lifecycle_mutation_guard",
                    self._component_mutation_lease,
                )
                setattr(
                    component,
                    "_lifecycle_mutation_async_guard",
                    self._component_mutation_async_lease,
                )
        initialization_scope = self._lifecycle_mutation_lease()
        initialization_scope.__enter__()
        self._initialization_mutation_scope = initialization_scope
        self._connection_service: PluginConnectionService | None = None
        self._connection_coordinator: PluginConnectionCoordinator | None = None
        self.plugin_backend: CurrentMcpPluginBackend | None = None
        self.plugin_runtime: PluginRuntime | None = None
        if configured:
            assert oauth_broker is not None
            assert plugin_grants is not None
            assert profile_match_provider is not None
            assert operation_submitter is not None
            # A receipt committed before a prior process crashed must be
            # published and claimed before any status path can say Connected.
            plugin_grants.reconcile()
            execution_worker = PluginExecutionWorker(
                child_handler=execute_mcp_operation,
                shutdown_timeout_seconds=10.0,
                reap_pending_callback=(
                    self._retain_lifecycle_owner_lease_for_worker_reap
                ),
            )
            self._connection_service = PluginConnectionService(
                app_registry=self.app_registry,
                plugin_registry=self.plugin_registry,
                package_registry=self.package_registry,
                broker=oauth_broker,
                grant_service=plugin_grants,
                descriptor_provider=self.descriptor_for_plugin_app,
                candidate_descriptor_provider=self._candidate_descriptor,
                candidate_record_provider=self._candidate_record,
                requirement_provider=self._connection_requirement,
                offered_scopes_provider=self._offered_provider_scopes,
                profile_match_provider=profile_match_provider,
                operation_submitter=operation_submitter,
                transport_authority=transport_authority,
                execution_worker=execution_worker,
            )
            self._connection_coordinator = PluginConnectionCoordinator(
                plugin_registry=self.plugin_registry,
                connection_service=self._connection_service,
            )
            self.plugin_backend = CurrentMcpPluginBackend(
                connection_service=self._connection_service,
                descriptor_provider=self.descriptor_for_plugin_app,
                plugin_id_for_app=self.plugin_id_for_app,
                account_key_provider=self.operator_account_key,
                operation_submitter=operation_submitter,
                verified_plugin_provider=self.plugin_registry.reverify_installed,
                execution_worker=execution_worker,
                provider_update_lookup=self._provider_update_lookup,
            )
            self.plugin_runtime = PluginRuntime(
                plugin_registry=self.plugin_registry,
                app_registry=self.app_registry,
                backend=self.plugin_backend,
                mutation_scope=self._plugin_execution_mutation_scope,
                catalog_provider=self.runtime_catalog,
            )
        self._catalog_source = VerifiedCatalogSourceAdapter(self)
        self._directory = PluginDirectoryService(
            sources=(self._catalog_source,),
            plugin_registry=self.plugin_registry,
        )
        self._reconcile_uninstall_intent()
        self._reconcile_update_intent()
        self._reconcile_lifecycle_operation()
        # Recovery runs beneath the exact constructor capability. Component
        # guards are already bound so foreign callers cannot bypass the
        # process lease while reused stores are being reconciled.
        for component in (
            self.permission_store,
            self.authority_service,
            self.plugin_grants,
        ):
            if component is not None:
                setattr(
                    component,
                    "_lifecycle_mutation_guard",
                    self._component_mutation_lease,
                )
                setattr(
                    component,
                    "_lifecycle_mutation_async_guard",
                    self._component_mutation_async_lease,
                )
        if self.plugin_backend is not None:
            self.plugin_backend._lifecycle_mutation_guard = (
                self._plugin_execution_mutation_scope
            )
            self.plugin_backend._lifecycle_mutation_handoff = (
                self._handoff_lifecycle_mutation_operation
            )
        if self._connection_service is not None:
            self._connection_service._lifecycle_mutation_guard = (
                self._component_mutation_lease
            )
            self._connection_service._lifecycle_mutation_async_guard = (
                self._component_mutation_async_lease
            )
            self._connection_service._lifecycle_mutation_handoff = (
                self._handoff_lifecycle_mutation_operation
            )
        if self._connection_coordinator is not None:
            self._connection_coordinator._lifecycle_mutation_guard = (
                self._component_mutation_lease
            )
            self._connection_coordinator._lifecycle_mutation_async_guard = (
                self._component_mutation_async_lease
            )
        self._lifecycle_initializing = False
        self._exit_initialization_mutation_scope((None, None, None))
        self._detached_component_guards = []
        self._start_update_heartbeat()

    @property
    def connection_service(self) -> PluginConnectionService:
        """Return the explicitly composed connection service or fail closed."""

        if self._connection_service is None:
            raise ValueError("oauth_profile_catalog_unavailable")
        return self._connection_service

    @property
    def connection_coordinator(self) -> PluginConnectionCoordinator:
        if self._connection_coordinator is None:
            raise ValueError("oauth_profile_catalog_unavailable")
        return self._connection_coordinator

    @property
    def revision(self) -> str:
        return self._lifecycle_revision_snapshot().revision

    def _lifecycle_components(self) -> tuple[tuple[str, str], ...]:
        authority_store = getattr(self.authority_service, "store", None)
        if authority_store is None:
            authority_store = getattr(self.plugin_grants, "authority_store", None)
        return (
            (
                "plugin_registry",
                _durable_owner_revision(self.plugin_registry, label="plugin_registry"),
            ),
            (
                "app_registry",
                _durable_owner_revision(self.app_registry, label="app_registry"),
            ),
            (
                "package_registry",
                _durable_owner_revision(self.package_registry, label="package_registry"),
            ),
            (
                "connection_grants",
                _durable_owner_revision(self.plugin_grants, label="connection_grants"),
            ),
            (
                "connection_authority",
                _durable_owner_revision(authority_store, label="connection_authority"),
            ),
            (
                "permissions",
                _durable_owner_revision(self.permission_store, label="permissions"),
            ),
            (
                "oauth_vault",
                _durable_owner_revision(self._oauth_vault, label="oauth_vault"),
            ),
            (
                "lifecycle_operation",
                _durable_owner_revision(
                    getattr(self, "_operation_state", None),
                    label="lifecycle_operation",
                ),
            ),
            (
                "uninstall_intent",
                _durable_owner_revision(
                    getattr(self, "_uninstall_state", None),
                    label="uninstall_intent",
                ),
            ),
            (
                "update_intent",
                _durable_owner_revision(
                    getattr(self, "_update_state", None),
                    label="update_intent",
                ),
            ),
        )

    def _lifecycle_revision_snapshot(self) -> _LifecycleRevisionSnapshot:
        observed = self._lifecycle_components()
        for _attempt in range(3):
            current = self._lifecycle_components()
            if current == observed:
                material = {
                    "domain": "omega.plugin-lifecycle.revision.v1",
                    "components": [
                        {"name": name, "revision": revision}
                        for name, revision in current
                    ],
                }
                return _LifecycleRevisionSnapshot(
                    revision=sha256_hex(canonical_bytes(material)),
                    plugin_registry_revision=dict(current)["plugin_registry"],
                    components=current,
                )
            observed = current
        raise ValueError("plugin_lifecycle_revision_conflict")

    def catalog_source(self) -> CatalogSourceAdapter:
        """Return the one verified, provider-neutral catalog source adapter."""

        return self._catalog_source

    @staticmethod
    def operator_account_key() -> str:
        """Return the durable key that partitions this operator's plugin state.

        It is an identifier, not a credential: it is hashed to partition the
        runtime catalog, permission store and every stored connection, so it
        must be stable for the life of an installation and must travel with a
        portable one. Requiring an environment variable made it neither -- no
        production path ever set it, so every plugin surface answered
        "operator account not configured" -- and a value that changed between
        launches would orphan every grant already stored under the old hash.

        ``OMEGA_OPERATOR_ACCOUNT_KEY`` still wins when set, for tests and for
        an operator pinning a specific partition. Otherwise the key is born
        once beside the vault it partitions and read back from there
        afterwards. A day-zero reset clears both together, which is coherent:
        the credentials the key partitioned are gone too.
        """
        value = os.environ.get("OMEGA_OPERATOR_ACCOUNT_KEY", "").strip()
        if value:
            return value
        return _durable_operator_account_key(core_paths.MCP_AUTHORIZATION_ROOT)

    def _local_trust_root(self) -> LocalTrustRoot:
        """Rederive this device's local-trust root from the configured vault key.

        Nothing about the derived Ed25519 seed or authority-MAC key is ever
        cached on ``self`` -- each call rebuilds a fresh :class:`LocalTrustRoot`
        from whatever the provider returns right now, matching
        ``plugin_platform.local_trust``'s own "reconstructable on demand,
        never persisted" contract. A different provider (a different vault
        key) on a later call -- across a restart, for instance -- reconstructs
        a different root, so a package this device sealed under one vault key
        fails closed under another rather than silently reverifying.
        """
        provider = self._local_trust_key_provider
        if provider is None:
            raise ValueError("plugin_local_trust_unavailable")
        try:
            vault_key = provider()
        except Exception as exc:
            raise ValueError("plugin_local_trust_unavailable") from exc
        try:
            return LocalTrustRoot.from_vault_key(vault_key)
        except LocalTrustError as exc:
            raise ValueError("plugin_local_trust_unavailable") from exc

    def _app_ids(self, plugin_id: str) -> tuple[str, ...]:
        verified = self._installed_verified(plugin_id)
        required = tuple(app.app_id for app in verified.manifest.required_apps)
        optional = tuple(app.app_id for app in verified.manifest.optional_apps)
        declared = required + optional
        durable = self.app_ids_for_plugin(plugin_id)
        durable_ids = set(durable)
        if (
            len(durable_ids) != len(durable)
            or not set(required).issubset(durable_ids)
            or not durable_ids.issubset(set(declared))
        ):
            raise ValueError("plugin_catalog_identity_ambiguous")
        return tuple(app_id for app_id in declared if app_id in durable_ids)

    async def status(self, plugin_id: str) -> PluginLifecycleSnapshot:
        await self._run_due_update_check()
        try:
            app_ids = self._app_ids(plugin_id)
        except ProviderProvenanceVerificationError as exc:
            if str(exc) != "provider_provenance_not_found":
                raise
            # Installed, and no longer attributable to a reviewed provider --
            # which happens when a provider is retired from the catalog while
            # its package is still installed. BLOCKED is the honest state: the
            # plugin cannot be used, and the panel can still render it so the
            # operator can uninstall it. Raising here took down the whole
            # installed listing instead.
            return self._retired_provenance_snapshot(plugin_id)
        if app_ids:
            try:
                account_key: str | None = self.operator_account_key()
            except ValueError as exc:
                if str(exc) != "plugin_account_unconfigured":
                    raise
                account_key = None
        else:
            account_key = ""
        snapshot = await self.connection_coordinator.status(
            plugin_id,
            account_key,
        )
        return self._with_pending_update_setup(plugin_id, snapshot)

    async def _run_due_update_check(self) -> None:
        """Use the existing lifecycle status boundary to service a due check.

        This is deliberately demand-driven: no timer, browser poller, or
        second scheduler is created.  Status is already a lifecycle-owned
        boundary used by dispatch and the HUD, so a live instance rechecks on
        the first boundary reached after its persisted deadline.
        """

        await asyncio.to_thread(self._run_due_update_check_sync)

    def _run_due_update_check_sync(self) -> None:
        if (
            getattr(self, "_closed", False)
            or not hasattr(self, "_update_policy_store")
            or getattr(self, "_operation_submitter", None) is None
        ):
            return
        try:
            policy = read_policy(self._update_policy_store)
            # A first-ever check belongs to the startup handoff.  A running
            # instance only services an explicitly persisted deadline.
            current = time.time()
            if policy.next_check_at is None:
                return
            if not policy.enabled:
                if current >= policy.next_check_at:
                    self._defer_update_check(policy, current, status="disabled")
                return
            if current < policy.next_check_at:
                return
        except (OSError, ValueError):
            return
        trigger_lock = getattr(self, "_due_check_trigger_lock", None)
        if trigger_lock is None:
            trigger_lock = threading.Lock()
            self._due_check_trigger_lock = trigger_lock
        if not trigger_lock.acquire(blocking=False):
            return
        try:
            # A status call can be made by the operation worker while another
            # lifecycle mutation owns the boundary.  Never wait recursively;
            # the next status boundary will retry after the owner releases it.
            mutation_lock = getattr(self._authority_mutation_lock, "_lock", None)
            if mutation_lock is not None and not mutation_lock.acquire(False):
                self._defer_update_check(policy, current, status="not_due")
                return
            if mutation_lock is not None:
                mutation_lock.release()
            self._check_updates_clean_context()
        finally:
            trigger_lock.release()

    def _defer_update_check(
        self,
        policy: PluginUpdatePolicy,
        current: float,
        *,
        status: str,
    ) -> None:
        """Durably move a skipped deadline without claiming a successful sync."""

        try:
            # Compare and publish while holding the policy store lock. A
            # manual check may complete between the heartbeat's due read and
            # its non-blocking lifecycle-lock attempt; never overwrite that
            # newer success with a stale deferred deadline.
            deferred = defer_policy_if_current(
                self._update_policy_store,
                policy,
                current,
            )
            if deferred is None:
                return
            self._update_policy = deferred
            # Deferral is scheduling telemetry only.  The last completed
            # check/status is owned by the check that produced it; publishing
            # a synthetic ``not_due``/``disabled`` result here would race a
            # concurrent completed check and erase its error or candidates.
        except (OSError, ValueError):
            # A later lifecycle boundary can retry a durable read/write error;
            # the heartbeat must remain bounded and shutdown-safe.
            return

    def _update_heartbeat_tick(self) -> None:
        """Service one persisted deadline for an otherwise idle Omega owner."""

        self._run_due_update_check_sync()

    def _update_heartbeat_loop(self) -> None:
        stop = self._update_heartbeat_stop
        heartbeat_active = True
        while heartbeat_active:
            try:
                policy = read_policy(self._update_policy_store)
                deadline = policy.next_check_at
                if deadline is None:
                    delay = _UPDATE_HEARTBEAT_MAX_WAIT_SECONDS
                else:
                    delay = max(
                        _UPDATE_HEARTBEAT_MIN_WAIT_SECONDS,
                        min(
                            _UPDATE_HEARTBEAT_MAX_WAIT_SECONDS,
                            deadline - time.time(),
                        ),
                    )
            except (OSError, ValueError):
                delay = _UPDATE_HEARTBEAT_MAX_WAIT_SECONDS
            heartbeat_active = not stop.wait(delay)
            if not heartbeat_active:
                return
            if getattr(self, "_closed", False):
                return
            self._update_heartbeat_tick()

    def _start_update_heartbeat(self) -> None:
        if (
            getattr(self, "_operation_submitter", None) is None
            or getattr(self, "_closed", False)
        ):
            return
        thread = getattr(self, "_update_heartbeat_thread", None)
        if thread is not None and thread.is_alive():
            return
        thread = threading.Thread(
            target=self._update_heartbeat_loop,
            name="omega-plugin-update-heartbeat",
            daemon=True,
        )
        self._update_heartbeat_thread = thread
        thread.start()

    def _stop_update_heartbeat(self) -> None:
        stop = getattr(self, "_update_heartbeat_stop", None)
        if stop is not None:
            stop.set()

    def _queue_deferred_update_application(
        self, plugin_id: str, version: str, *, generation: int | None = None
    ) -> None:
        lock = getattr(self, "_deferred_update_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._deferred_update_lock = lock
        with lock:
            pending = getattr(self, "_deferred_update_applications", None)
            if pending is None:
                pending = []
                self._deferred_update_applications = pending
            item = (plugin_id, version)
            if item not in pending:
                pending.append(item)
            if generation is not None:
                generations = getattr(self, "_deferred_update_generations", None)
                if generations is None:
                    generations = {}
                    self._deferred_update_generations = generations
                generations[item] = generation

    def _register_deferred_update_retry(
        self,
        plugin_id: str,
        version: str,
        retry: PluginRetryAfterFence,
    ) -> None:
        if type(retry) is not PluginRetryAfterFence:
            raise ValueError("plugin_retry_invalid")
        lock = getattr(self, "_deferred_update_lock", None)
        if lock is None:
            lock = threading.Lock()
            self._deferred_update_lock = lock
        with lock:
            retries = getattr(self, "_deferred_update_retries", None)
            if retries is None:
                retries = {}
                self._deferred_update_retries = retries
            retries.setdefault((plugin_id, version), []).append(retry)

    def _drain_deferred_update_applications(self) -> None:
        """Apply fenced recovery candidates after the execution lease exits."""

        lock = getattr(self, "_deferred_update_lock", None)
        if lock is None:
            return
        with lock:
            pending = tuple(getattr(self, "_deferred_update_applications", ()))
            getattr(self, "_deferred_update_applications", []).clear()
            generations = getattr(self, "_deferred_update_generations", {})
            origins = {item: generations.pop(item, None) for item in pending}
        failures: list[str] = []
        failure_entries: list[tuple[tuple[str, str], str]] = []
        approval: list[tuple[str, str, tuple[str, ...]]] = []
        unresolved: list[tuple[str, str]] = []
        for plugin_id, version in pending:
            with lock:
                retries = getattr(self, "_deferred_update_retries", None)
                waiters = tuple(
                    retries.pop((plugin_id, version), ())
                    if retries is not None
                    else ()
                )
            if self._closed:
                for waiter in waiters:
                    waiter.resolve(
                        PluginUpdateResult(
                            plugin_id,
                            version,
                            "failed",
                            error="plugin_lifecycle_closed",
                        )
                    )
                return
            try:
                result = self.apply_compatible_update(plugin_id, version)
            except (RuntimeError, ValueError):
                # The normal update path has already converted delivery
                # failures to typed results.  A queue drain must not turn an
                # action-fence release into an unhandled worker exception.
                result = PluginUpdateResult(
                    plugin_id=plugin_id,
                    version=version,
                    status="failed",
                    error="plugin_update_failed",
                )
            for waiter in waiters:
                waiter.resolve(result)
            if result.status == "approval_required":
                approval.append((plugin_id, version, result.approval_diff))
                unresolved.append((plugin_id, version))
            elif result.status != "applied":
                error = result.error or "plugin_update_failed"
                failures.append(error)
                failure_entries.append(((plugin_id, version), error))
                unresolved.append((plugin_id, version))
        if failures or approval:
            self._persist_deferred_update_results(
                tuple(unresolved),
                failures=failures,
                approval_required=approval,
                origins={item: origins.get(item) for item in unresolved},
                failure_entries=failure_entries,
            )

    def _persist_deferred_update_results(
        self,
        pending: tuple[tuple[str, str], ...],
        *,
        failures: list[str],
        approval_required: list[tuple[str, str, tuple[str, ...]]],
        origins: Mapping[tuple[str, str], int | None] | None = None,
        failure_entries: list[tuple[tuple[str, str], str]] | None = None,
    ) -> None:
        """Persist queue-drain outcomes without exposing provider text."""

        try:
            status_store = getattr(self, "_update_status_store", None)
            if status_store is None:
                status_store = update_status_store(
                    getattr(self, "_plugin_state_root", self._update_policy_store.state_root)
                )
                self._update_status_store = status_store
            with self._authority_mutation_lock:
                prior = read_update_status(status_store)
                origin_values = {
                    origins.get(item)
                    for item in pending
                    if origins is not None and origins.get(item) is not None
                }
                current_generation = prior.generation if prior is not None else None
                if origin_values and current_generation is None:
                    self._last_update_check = prior
                    return
                retained = tuple(
                    item
                    for item in pending
                    if origins is None
                    or origins.get(item) is None
                    or origins.get(item) == current_generation
                )
                if not retained:
                    self._last_update_check = prior
                    return
                retained_set = set(retained)
                if failure_entries is not None:
                    failures = [
                        error
                        for item, error in failure_entries
                        if item in retained_set
                    ]
                approval_required = [
                    item for item in approval_required if item[:2] in retained_set
                ]
                policy = read_policy(self._update_policy_store)
                current = time.time()
                next_check_at = policy.next_check_at
                status = "checked"
                if failures:
                    policy = policy.after_failure(current)
                    write_policy(self._update_policy_store, policy)
                    self._update_policy = policy
                    next_check_at = policy.next_check_at
                    status = "failed"
                candidates = (
                    prior.candidate_versions if prior is not None else retained
                )
                staged = list(prior.staged if prior is not None else ())
                for item in retained:
                    if item not in staged:
                        staged.append(item)
                result = PluginUpdateCheckResult(
                    status=status,
                    checked_at=(prior.checked_at if prior is not None else current),
                    next_check_at=next_check_at,
                    candidate_versions=candidates,
                    staged=tuple(staged),
                    approval_required=tuple(approval_required),
                    error=failures[0] if failures else None,
                    generation=current_generation if current_generation is not None else 0,
                )
                write_update_status(status_store, result)
                self._last_update_check = result
        except (OSError, ValueError):
            return

    @contextmanager
    def _plugin_execution_mutation_scope(self) -> Iterator[None]:
        drain_at_exit = self._lifecycle_mutation_depth.get() == 0
        try:
            with self._component_mutation_lease():
                yield
        finally:
            if drain_at_exit:
                self._drain_deferred_update_applications()

    def _check_updates_clean_context(self) -> PluginUpdateCheckResult:
        """Run the synchronous worker with transport recursion explicitly off."""

        token = _PLUGIN_TRANSPORT_CONTEXT.set(False)
        try:
            return self.check_updates()
        finally:
            _PLUGIN_TRANSPORT_CONTEXT.reset(token)

    def _with_pending_update_setup(
        self,
        plugin_id: str,
        snapshot: PluginLifecycleSnapshot,
    ) -> PluginLifecycleSnapshot:
        """Report the app a pending update is still waiting to have authorized.

        The predecessor stays connected through an update, so the coordinator
        answers `connected` for a plugin that cannot finish without a sign-in.
        Naming the app the operation is actually blocked on is what a client
        needs, and `next_required_app_id` is the field for it -- but the
        snapshot derives that from its own apps, so the app has to carry the
        state too. It is the same app the update response and `permissions()`
        name (`_next_candidate_app`), and the only one a pending update's setup
        accepts; an app the candidate adds has no predecessor state, so it is
        reported here in its own right. A retained optional app that needs a
        fresh sign-in (its sign-in method changed) blocks the update just the
        same, so it is reported as required for the update.

        Failure-soft on purpose: this is a display refinement over an answer
        that is already correct about the predecessor, and it must never be the
        reason a status read fails.
        """
        try:
            pending = self._pending_update(plugin_id)
            next_app_id = (
                None if pending is None else self._next_candidate_app(pending[0])
            )
        except Exception:  # noqa: BLE001 - never fail a status read for this
            return snapshot
        if pending is None or next_app_id is None:
            return snapshot
        operation, _verified, _binding = pending
        awaiting = {
            str(item["app_id"])
            for item in operation["candidate_apps"]
            if type(item) is dict and item.get("state") == "setup_required"
        }
        state = PluginLifecycleState.SETUP_REQUIRED

        def awaiting_setup(app_id: str, *, required: bool) -> PluginAppState:
            return PluginAppState(
                app_id=app_id,
                required=required,
                state=state,
                authority_revision="",
                error_code=_ERROR_BY_STATE.get(state),
            )

        present = {app.app_id for app in snapshot.apps}
        # The next app leads, so it is the snapshot's first incomplete required
        # app; apps the candidate adds follow the predecessor's own.
        apps = (
            awaiting_setup(next_app_id, required=True),
            *(
                awaiting_setup(app.app_id, required=app.required)
                if app.app_id in awaiting
                else app
                for app in snapshot.apps
                if app.app_id != next_app_id
            ),
            *(
                awaiting_setup(app_id, required=True)
                for app_id in sorted(awaiting - present - {next_app_id})
            ),
        )
        try:
            return PluginLifecycleSnapshot(
                plugin_id=snapshot.plugin_id,
                state=state,
                registry_revision=snapshot.registry_revision,
                package_version=snapshot.package_version,
                apps=apps,
                next_required_app_id=next_app_id,
                enabled=snapshot.enabled,
                error_code=_ERROR_BY_STATE.get(state),
            )
        except ValueError:
            return snapshot

    def _retired_provenance_snapshot(self, plugin_id: str) -> PluginLifecycleSnapshot:
        """A blocked snapshot for an installed plugin with no provenance record."""
        snapshot = self.plugin_registry.snapshot()
        record = next(
            (item for item in snapshot.records if item.plugin_id == plugin_id),
            None,
        )
        if record is None:
            raise ValueError("plugin_not_installed")
        return PluginLifecycleSnapshot._from_registry_blocker(
            plugin_id=plugin_id,
            registry_revision=snapshot.revision,
            package_version=record.selected_version,
            apps=(),
            next_required_app_id=None,
            enabled=record.enabled,
        )

    def _has_pending_update_retirement(self, plugin_id: str) -> bool:
        if not hasattr(self, "_update_state"):
            return False
        operation = self._read_update_document()["operation"]
        return bool(
            type(operation) is dict
            and operation["plugin_id"] == plugin_id
            and operation["phase"] in {"committed", "retirement_complete"}
        )

    async def catalog_state(self, plugin_id: str, *, catalog_version: str) -> str:
        """Project one catalog card from verified durable lifecycle authority."""

        if type(catalog_version) is not str or not catalog_version:
            raise ValueError("plugin_catalog_signature_invalid")
        try:
            record = self.plugin_registry.get(plugin_id)
        except PluginRegistryError as exc:
            if str(exc) == "plugin_not_installed":
                return PluginLifecycleState.AVAILABLE.value
            raise
        try:
            app_ids = self._app_ids(plugin_id)
        except ProviderProvenanceVerificationError as exc:
            if str(exc) != "provider_provenance_not_found":
                raise
            # Same fact as in `status`: installed, and no longer attributable to
            # a reviewed provider. The catalog card says BLOCKED rather than
            # taking down the whole directory listing, which is what raising
            # here did -- one retired provider with its package still installed
            # made every card unrenderable.
            return PluginLifecycleState.BLOCKED.value
        if self._has_pending_update_retirement(plugin_id):
            return PluginLifecycleState.REVOCATION_PENDING.value
        connection_service = getattr(self, "_connection_service", None)
        plugin_projector = getattr(
            connection_service,
            "local_plugin_state_without_account",
            None,
        )
        local_plugin_state = (
            None if not callable(plugin_projector) else plugin_projector(plugin_id)
        )
        if local_plugin_state == "revocation_pending":
            return PluginLifecycleState.REVOCATION_PENDING.value
        if local_plugin_state == "permission_changed":
            return PluginLifecycleState.PERMISSION_CHANGED.value
        if local_plugin_state is not None:
            raise ValueError("plugin_connection_state_invalid")
        if not app_ids:
            if record.blocker is not None:
                return PluginLifecycleState.BLOCKED.value
            if record.enabled and record.selected_version != catalog_version:
                return PluginLifecycleState.UPDATE_AVAILABLE.value
            return (
                PluginLifecycleState.CONNECTED.value
                if record.enabled
                else PluginLifecycleState.INSTALLED.value
            )
        snapshot = await self.status(plugin_id)
        if (
            snapshot.state is PluginLifecycleState.CONNECTED
            and record.selected_version != catalog_version
        ):
            return PluginLifecycleState.UPDATE_AVAILABLE.value
        return snapshot.state.value

    def _validated_permission_settings(
        self,
        plugin_id: str,
        selection: Mapping[str, object],
        settings: object,
        *,
        descriptor: McpConnectionDescriptor,
    ) -> dict[str, object]:
        expected_keys = {
            "provider_scopes",
            "capability_ids",
            "execution_policy",
            "source_constraints",
            "destination_constraints",
        }
        if type(settings) is not dict or set(settings) != expected_keys:
            raise ValueError("plugin_permission_changed")
        raw_scopes = settings["provider_scopes"]
        raw_capabilities = settings["capability_ids"]
        execution_policy = settings["execution_policy"]
        source_constraints = settings["source_constraints"]
        destination_constraints = settings["destination_constraints"]
        if (
            type(raw_scopes) is not list
            or any(type(item) is not str for item in raw_scopes)
            or len(raw_scopes) != len(set(raw_scopes))
            or type(raw_capabilities) is not list
            or any(type(item) is not str for item in raw_capabilities)
            or len(raw_capabilities) != len(set(raw_capabilities))
            or type(execution_policy) is not str
            or type(source_constraints) is not dict
            or type(destination_constraints) is not dict
        ):
            raise ValueError("plugin_permission_changed")
        # The policy is one of the form's closed choices; anything else is refused here
        # with its own code, not left to fail later as a bare enum error.
        if execution_policy not in {policy.value for policy in PluginExecutionPolicy}:
            raise ValueError("plugin_execution_policy_invalid")
        offered = selection.get("offered")
        if type(offered) is not dict:
            raise ValueError("plugin_permission_changed")
        offered_scopes = offered.get("provider_scopes")
        offered_capabilities = offered.get("capability_ids")
        if (
            type(offered_scopes) is not list
            or type(offered_capabilities) is not list
            or not set(raw_scopes).issubset(offered_scopes)
            or not set(raw_capabilities).issubset(offered_capabilities)
        ):
            raise ValueError("plugin_permission_changed")
        declared = descriptor.declared_capabilities
        schemas = {
            capability.removeprefix("tool:"): declared[capability]
            for capability in raw_capabilities
            if capability in declared and capability.startswith("tool:")
        }
        if len(schemas) != len(raw_capabilities):
            raise ValueError("plugin_capability_invalid")
        return {
            "scopes": tuple(sorted(raw_scopes)),
            "capability_ceiling": tuple(sorted(raw_capabilities)),
            "allowed_tool_schemas": schemas,
            "execution_policy": execution_policy,
            "source_constraints": source_constraints,
            "destination_constraints": destination_constraints,
        }

    @staticmethod
    def _permission_settings_document(
        validated: Mapping[str, object],
    ) -> dict[str, object]:
        policy = validated["execution_policy"]
        return {
            "provider_scopes": list(validated["scopes"]),
            "capability_ids": list(validated["capability_ceiling"]),
            "execution_policy": getattr(policy, "value", policy),
            "source_constraints": dict(validated["source_constraints"]),
            "destination_constraints": dict(validated["destination_constraints"]),
        }

    async def _selected_app_status(self, plugin_id: str, app_id: str) -> object | None:
        """Return the live connection of one app the selected version owns.

        A pending update's candidate may require an app the selected version
        never bound. That app has no connection to report, so its status is
        ``None``. Membership is decided here, from the selected binding: the
        connection service is never asked about an app it does not own,
        because its refusal (``plugin_not_installed``) also means the plugin
        itself is gone, which must stay a hard failure.
        """

        if app_id not in self._app_ids(plugin_id):
            return None
        return await self.connection_service.status(
            plugin_id,
            app_id=app_id,
            account_key=self.operator_account_key(),
        )

    def setup_requirement(self, plugin_id: str, app_id: str) -> Any:
        """The signed requirement of the app a setup is about to authorize.

        While an update is pending, setup authorizes the candidate's apps -- the
        candidate may add an app or change one's authentication -- so its
        re-verified manifest answers; otherwise the installed version's does.
        Status names the next app from the same source (`_next_candidate_app`),
        so the setup action always matches the app it is for.
        """
        pending = self._pending_update(plugin_id)
        verified = (
            self.plugin_registry.reverify_installed(plugin_id)
            if pending is None
            else pending[1]
        )
        requirement = next(
            (
                item
                for item in (
                    *verified.manifest.required_apps,
                    *verified.manifest.optional_apps,
                )
                if item.app_id == app_id
            ),
            None,
        )
        if requirement is None:
            raise ValueError("plugin_connection_state_invalid")
        return requirement

    def _candidate_settings_from_status(
        self,
        plugin_id: str,
        app_id: str,
        *,
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        status: object,
    ) -> tuple[dict[str, object], dict[str, object]] | None:
        """Carry forward only selections represented by live durable authority."""

        if (
            getattr(status, "status", None) != "connected"
            or not getattr(status, "authority_revision", None)
        ):
            return None
        _current, _record, requirement, descriptor = (
            self.connection_service._candidate_definition(
                plugin_id,
                app_id,
                candidate_verified=candidate_verified,
                candidate_binding=candidate_binding,
                expected_registry_revision=self.plugin_registry.snapshot().revision,
            )
        )
        offered_scopes = list(requirement.authentication.scopes)
        offered_capabilities = sorted(
            capability
            for capability in descriptor.declared_capabilities
            if capability.startswith("tool:")
        )
        # An update may remove previously granted items. Carry only their
        # intersection; newly offered scopes and capabilities always need an
        # explicit selection.
        raw_settings = {
            "provider_scopes": [
                item
                for item in (getattr(status, "provider_scopes", ()) or ())
                if item in offered_scopes
            ],
            "capability_ids": [
                item
                for item in (getattr(status, "capability_ceiling", ()) or ())
                if item in offered_capabilities
            ],
            "execution_policy": getattr(
                getattr(status, "execution_policy", None),
                "value",
                getattr(status, "execution_policy", "confirm_each"),
            ),
            "source_constraints": dict(
                getattr(status, "source_constraints", {}) or {}
            ),
            "destination_constraints": dict(
                getattr(status, "destination_constraints", {}) or {}
            ),
        }
        selection = {
            "offered": {
                "provider_scopes": offered_scopes,
                "capability_ids": offered_capabilities,
            }
        }
        validated = self._validated_permission_settings(
            plugin_id,
            selection,
            raw_settings,
            descriptor=descriptor,
        )
        document = self._permission_settings_document(validated)
        return validated, document

    async def start_connection(
        self,
        plugin_id: str,
        app_id: str | None = None,
        *,
        revision: str | None = None,
        settings: dict[str, object] | None = None,
    ) -> ConnectionStart | PluginAppSelectionRequired:
        async with self._authority_mutation_lock:
            before = self._lifecycle_revision_snapshot()
            if revision is not None and revision != before.revision:
                raise ValueError("plugin_permission_changed")
            try:
                pending = self._pending_update(plugin_id)
            except BaseException:
                if self._read_update_document()["operation"] is not None:
                    self._abort_pending_update()
                raise
            if pending is not None:
                operation, verified, binding = pending
                next_app_id = self._next_candidate_app(operation)
                if app_id is None:
                    if next_app_id is None:
                        raise ValueError("plugin_authorization_pending")
                    app_id = next_app_id
                # The next candidate app is required or retained-optional
                # (`_pending_update` proves the candidate set); either way the
                # update cannot finish until it is set up.
                if app_id != next_app_id:
                    raise ValueError("plugin_app_not_installed")
                try:
                    selection = await self.permissions(plugin_id, app_id)
                    if type(selection) is PluginAppSelectionRequired:
                        raise ValueError("plugin_connection_state_invalid")
                    descriptor = self.connection_service._candidate_definition(
                        plugin_id,
                        app_id,
                        candidate_verified=verified,
                        candidate_binding=binding,
                        expected_registry_revision=self.plugin_registry.snapshot().revision,
                    )[3]
                    validated = self._validated_permission_settings(
                        plugin_id,
                        selection,
                        settings,
                        descriptor=descriptor,
                    )
                    return await self._start_candidate_connection_locked(
                        plugin_id,
                        app_id,
                        operation=operation,
                        candidate_verified=verified,
                        candidate_binding=binding,
                        validated=validated,
                    )
                except BaseException:
                    self._abort_pending_update()
                    raise
            self._assert_plugin_authority_mutation_available(plugin_id)
            app_ids = self._app_ids(plugin_id)
            if app_id is None:
                if len(app_ids) > 1:
                    return PluginAppSelectionRequired(plugin_id=plugin_id, app_ids=app_ids)
                if not app_ids:
                    raise ValueError("plugin_setup_not_required")
                app_id = app_ids[0]
            elif app_id not in app_ids:
                raise ValueError("plugin_app_not_installed")
            if type(settings) is not dict:
                raise ValueError("plugin_scopes_invalid")
            selection = await self.permissions(plugin_id, app_id)
            if type(selection) is PluginAppSelectionRequired:
                return selection
            descriptor = self.descriptor_for_plugin_app(plugin_id, app_id)
            validated = self._validated_permission_settings(
                plugin_id,
                selection,
                settings,
                descriptor=descriptor,
            )
            if self._lifecycle_revision_snapshot().revision != before.revision:
                raise ValueError("plugin_permission_changed")
            try:
                started = await self.connection_coordinator.start(
                    plugin_id,
                    app_id,
                    self.operator_account_key(),
                    None,
                    **validated,
                )
            except ValueError as exc:
                if str(exc) in {
                    "plugin_authority_revision_conflict",
                    "plugin_grant_revision_conflict",
                    "plugin_registry_conflict",
                    "mcp_app_registry_conflict",
                    "plugin_lifecycle_revision_conflict",
                }:
                    raise ValueError("plugin_permission_changed") from exc
                raise
            if started.status == "connected":
                return await self._publish_connected_status(started)
            return started

    def planning_connection_revision(
        self,
        plugin_id: str,
        app_id: str,
        account_key_hash: str,
        descriptor: McpConnectionDescriptor,
    ) -> str | None:
        """Return the exact live authority revision bound into planner candidates.

        Remote candidates must follow the durable OAuth grant, not merely the
        static descriptor. Otherwise a reconnect can change the token/scopes
        while leaving an old candidate apparently executable. Local apps have
        no OAuth grant, so their descriptor revision remains their connection
        identity.
        """
        descriptor_revision = sha256_hex(canonical_bytes(descriptor.to_dict()))
        if descriptor.deployment is McpClientDeployment.LOCAL:
            return descriptor_revision
        if self.plugin_grants is None:
            return None
        grant = self.plugin_grants.current(
            plugin_id,
            app_id=app_id,
            account_key_sha256=account_key_hash,
        )
        if grant is None:
            return None
        activation = grant.activation
        if (
            activation.plugin_id != plugin_id
            or activation.app_id != app_id
            or activation.account_key_sha256 != account_key_hash
            or activation.capability_revision != descriptor.capability_revision
            or activation.descriptor_revision != descriptor_revision
        ):
            return None
        return grant.authority.connection_revision

    def planning_app_authority(
        self,
        plugin_id: str,
        app_id: str,
        account_key_hash: str,
        descriptor: McpConnectionDescriptor,
    ) -> Any | None:
        """Return the exact durable authority envelope used by runtime catalogs."""

        from mcp_platform.plugin_catalog import PluginCatalogAppAuthority

        descriptor_revision = sha256_hex(canonical_bytes(descriptor.to_dict()))
        try:
            verified = self.plugin_registry.reverify_installed(plugin_id)
            package_revision = verified.package_sha256
        except Exception:
            return None
        if descriptor.deployment is McpClientDeployment.REMOTE and self.plugin_grants is not None:
            grant = self.plugin_grants.current(
                plugin_id,
                app_id=app_id,
                account_key_sha256=account_key_hash,
            )
            if grant is not None:
                activation = grant.activation
                if (
                    activation.plugin_id == plugin_id
                    and activation.app_id == app_id
                    and activation.account_key_sha256 == account_key_hash
                    and activation.capability_revision == descriptor.capability_revision
                    and activation.descriptor_revision == descriptor_revision
                    and activation.package_revision == package_revision
                    and grant.authority == activation.authority()
                ):
                    return PluginCatalogAppAuthority(
                        app_id=app_id,
                        account_key_hash=account_key_hash,
                        connection_revision=grant.authority.connection_revision,
                        authority_revision=grant.authority.revision,
                        capability_revision=activation.capability_revision,
                        capability_ceiling=tuple(grant.authority.capability_ceiling),
                    )
            if descriptor.authentication.kind.value != "none":
                return None
        connection_service = self._connection_service
        if connection_service is None:
            return None
        try:
            authority = connection_service.authority_store.connection(
                plugin_id,
                app_id,
                account_key_hash,
            )
        except (AttributeError, ValueError):
            return None
        if (
            authority.plugin_id != plugin_id
            or authority.app_id != app_id
            or authority.account_key_hash != account_key_hash
        ):
            return None
        if (
            connection_service._credentialless_authority_is_current(
                authority,
                plugin_id=plugin_id,
                descriptor=descriptor,
            )
            is None
        ):
            return None
        return PluginCatalogAppAuthority(
            app_id=app_id,
            account_key_hash=account_key_hash,
            connection_revision=authority.connection_revision,
            authority_revision=authority.revision,
            capability_revision=descriptor.capability_revision,
            capability_ceiling=tuple(authority.capability_ceiling),
        )

    def app_ids_for_plugin(self, plugin_id: str) -> tuple[str, ...]:
        """Return the signed app dependencies owned by one installed plugin.

        Ownership is the durable ``app_binding`` written at install.  An
        unknown plugin or a record without a binding is an empty result, not a
        hardcoded featured-service map.
        """

        try:
            record = self.plugin_registry.get(plugin_id)
        except PluginRegistryError:
            return ()
        binding = getattr(record, "app_binding", None)
        if binding is None:
            return ()
        required = tuple(getattr(binding, "required_app_ids", ()) or ())
        optional = tuple(getattr(binding, "optional_app_ids", ()) or ())
        return required + optional

    def plugin_id_for_app(self, app_id: str) -> str:
        """Invert durable ownership. Fail closed unless exactly one plugin owns the app."""

        from mcp_platform.plugin_runtime import PluginRuntimeError

        if type(app_id) is not str or not app_id or app_id.strip() != app_id:
            raise PluginRuntimeError("plugin_not_installed")
        matches: list[str] = []
        for record in self.plugin_registry.snapshot().records:
            binding = record.app_binding
            if binding is None:
                continue
            owned = binding.required_app_ids + binding.optional_app_ids
            if app_id in owned:
                matches.append(record.plugin_id)
        if len(matches) != 1:
            raise PluginRuntimeError("plugin_not_installed")
        return matches[0]

    def ensure_plugin_ready(self, plugin_id: str, *, app_id: str | None = None) -> bool:
        with self._component_mutation_lease():
            return self._ensure_plugin_ready_locked(plugin_id, app_id=app_id)

    def _ensure_plugin_ready_locked(
        self,
        plugin_id: str,
        *,
        app_id: str | None = None,
    ) -> bool:
        """Reconcile an authorized operator account with the plugin enable bit.

        Installation deliberately leaves a plugin dormant until OAuth succeeds.
        The connection store is durable, so a HUD restart can have an
        authorized token while the registry bit is still false (for example,
        after the callback process exited between its two durable writes).
        Omega may repair that narrow state synchronously before dispatch; it
        never enables an unauthenticated plugin.
        """

        async def reconcile() -> bool:
            record = self.plugin_registry.get(plugin_id)
            if record.enabled:
                return True
            selected_app_id = app_id or next(iter(self.app_ids_for_plugin(plugin_id)), None)
            if selected_app_id is None:
                return False
            account_key = self.operator_account_key()
            status = await self.connection_service.status(
                plugin_id,
                app_id=selected_app_id,
                account_key=account_key,
            )
            if status.status != "connected":
                return False
            try:
                await self._enable_if_compiled(plugin_id)
            except ValueError as exc:
                if str(exc) != "plugin_catalog_authority_missing":
                    raise
                await self.connection_service.invalidate_execution(
                    plugin_id,
                    app_id=selected_app_id,
                    account_key=account_key,
                )
                return False
            return True

        try:
            if self._operation_submitter is None:
                raise ValueError("oauth_profile_catalog_unavailable")
            result = self._operation_submitter(
                self._handoff_lifecycle_mutation_operation(reconcile)
            )
            if hasattr(result, "result") and callable(result.result):
                result = result.result()
            if type(result) is not bool:
                raise ValueError("plugin_connection_not_live")
            return result
        except (RuntimeError, ValueError, OSError, PluginRegistryError):
            return False

    def plugin_tool_bindings(self) -> tuple[Any, ...]:
        from mcp_platform.plugin_dispatch import plugin_tool_bindings

        return plugin_tool_bindings(service=self)

    def bindings_for_plan(self, plan: Any, account_key_hash: str) -> tuple[Any, ...]:
        available = self.plugin_tool_bindings()
        resolved: list[Any] = []
        for step in plan.steps:
            matches = tuple(
                binding
                for binding in available
                if binding.candidate_id == step.candidate_id
                and binding.account_key_hash == account_key_hash
            )
            if len(matches) != 1:
                raise ValueError("plugin_permission_consent_mismatch")
            resolved.append(matches[0])
        return tuple(resolved)

    def resolve_plugin_authority(self, plan: Any, binding: Any, step: Any) -> Any:
        return self.authority_context(plan, binding, step)

    def _submit_connection_operation(self, operation: Any) -> Any:
        if self._operation_submitter is None:
            raise ValueError("oauth_profile_catalog_unavailable")
        result = self._operation_submitter(
            self._handoff_lifecycle_mutation_operation(operation),
            timeout_s=125.0,
        )
        if hasattr(result, "result") and callable(result.result):
            result = result.result()
        return result

    def authority_context(self, plan: Any, binding: Any, step: Any) -> tuple[Any, ...]:
        with self._component_mutation_lease():
            return self._authority_context_locked(plan, binding, step)

    def _authority_context_locked(
        self,
        plan: Any,
        binding: Any,
        step: Any,
    ) -> tuple[Any, ...]:
        del plan, step
        account_key = self.operator_account_key()
        account_key_hash = sha256(account_key.encode("utf-8")).hexdigest()
        if (
            getattr(binding, "account_key_hash", None) != account_key_hash
            or getattr(binding, "plugin_id", None) is None
            or getattr(binding, "app_id", None) is None
        ):
            raise ValueError("plugin_authority_context_invalid")

        async def resolve() -> tuple[Any, McpConnectionSnapshot]:
            descriptor = self.descriptor_for_plugin_app(
                binding.plugin_id,
                binding.app_id,
            )
            prepared = await self.connection_service.prepare_authority_context(
                binding.plugin_id,
                app_id=binding.app_id,
                account_key=account_key,
            )
            durable_connection = prepared.authority
            current_bindings = tuple(
                item
                for item in self.plugin_tool_bindings()
                if item.plugin_id == binding.plugin_id
                and item.app_id == binding.app_id
                and item.account_key_hash == account_key_hash
                and item.connection_revision == durable_connection.connection_revision
                and item.authority_revision == durable_connection.revision
            )
            if not current_bindings:
                raise ValueError("plugin_connection_not_live")
            allowed_schemas = {
                item.tool_name: item.input_schema_sha256 for item in current_bindings
            }
            snapshot = McpConnectionSnapshot(
                connection_id=sha256_hex(
                    canonical_bytes(
                        {
                            "plugin_id": binding.plugin_id,
                            "app_id": binding.app_id,
                            "account_key_hash": account_key_hash,
                        }
                    )
                ),
                connection_revision=durable_connection.connection_revision,
                server_id=prepared.server_id,
                server_version=prepared.server_version,
                artifact_sha256=prepared.package_revision,
                provider_id=(descriptor.provider_id or descriptor.publisher_id),
                account_ref=account_key_hash,
                enabled=True,
                capability_ceiling=durable_connection.capability_ceiling,
                allowed_tool_schemas=allowed_schemas,
                source_constraints=durable_connection.source_constraints,
                destination_constraints=durable_connection.destination_constraints,
                authority_revision=durable_connection.revision,
            )
            return durable_connection, snapshot

        durable_connection, snapshot = self._submit_connection_operation(resolve)
        if self.authority_service is None:
            raise ValueError("plugin_authority_context_invalid")
        return durable_connection, snapshot, self.authority_service, None

    def permission_authorities(
        self,
        plan: Any,
        account_key_hash: str,
        grant_id: str,
    ) -> tuple[McpAuthoritySnapshot, ...]:
        bindings = self.bindings_for_plan(plan, account_key_hash)
        digest = plugin_plan_digest(plan)
        authorities: list[McpAuthoritySnapshot] = []
        for binding, step in zip(bindings, plan.steps, strict=True):
            connection, snapshot, _service, _contributor = self.authority_context(
                plan,
                binding,
                step,
            )
            authority_id = sha256_hex(
                canonical_bytes(
                    {
                        "grant_id": grant_id,
                        "plan_digest": digest,
                        "candidate_id": step.candidate_id,
                        "step_id": step.step_id,
                    }
                )
            )
            revision = sha256_hex(
                canonical_bytes(
                    {
                        "authority_id": authority_id,
                        "connection_authority_revision": connection.revision,
                        "tool_schema_sha256": binding.input_schema_sha256,
                        "arguments_sha256": sha256_hex(canonical_bytes(plain_arguments(step.arguments))),
                    }
                )
            )
            risk = binding.risk_class
            authorities.append(
                McpAuthoritySnapshot(
                    authority_kind="direct_turn",
                    authority_id=authority_id,
                    authority_revision=revision,
                    episode_id=plan.episode_id,
                    job_id="",
                    job_revision="",
                    connection_id=snapshot.connection_id,
                    connection_revision=snapshot.connection_revision,
                    server_id=snapshot.server_id,
                    server_version=snapshot.server_version,
                    artifact_sha256=snapshot.artifact_sha256,
                    tool_name=binding.tool_name,
                    tool_schema_sha256=binding.input_schema_sha256,
                    capability_tags=(f"tool:{binding.capability}",),
                    source_constraints=connection.source_constraints,
                    destination_constraints=connection.destination_constraints,
                    risk_classes=(risk.value,),
                    direct_execution=True,
                    standing_execution=False,
                    allow_send_publish=risk is RiskClass.SEND_PUBLISH,
                    allow_destructive=risk is RiskClass.DESTRUCTIVE,
                    allow_cross_provider=risk is RiskClass.CROSS_PROVIDER_TRANSFER,
                    plan_digest=digest,
                    account_key_hash=account_key_hash,
                    candidate_id=step.candidate_id,
                    plan_step_id=step.step_id,
                    arguments_sha256=sha256_hex(canonical_bytes(plain_arguments(step.arguments))),
                    connection_authority_revision=connection.revision,
                    effect=binding.effect,
                )
            )
        return tuple(authorities)

    def _bind_local_launch_descriptor(
        self,
        record: InstalledMcpAppRecord,
        descriptor: McpConnectionDescriptor,
    ) -> McpConnectionDescriptor:
        if descriptor.deployment is not McpClientDeployment.LOCAL:
            return descriptor
        try:
            package = self.package_registry.reverify_installed(record.app_id)
            if (
                package.manifest.server_id != record.app_id
                or package.manifest.publisher_id != record.publisher_id
                or package.manifest_sha256
                != record.local_package_manifest_sha256
            ):
                raise ValueError
            return replace(
                descriptor,
                verified_launch_sha256=package.manifest.artifact_sha256,
            )
        except Exception:
            raise ValueError("plugin_catalog_authority_missing") from None

    def _descriptor_for_record(self, plugin_id: str, record: Any) -> Any:
        from mcp_platform.plugin_connections import signed_capability_authority_from_package

        descriptor = descriptor_for_record(
            record,
            signed_capability_authority=signed_capability_authority_from_package(
                self.package_registry,
                record,
                plugin_registry=self.plugin_registry,
                plugin_id=plugin_id,
            ),
        )
        return self._bind_local_launch_descriptor(record, descriptor)

    def descriptor_for_app(self, app_id: str) -> Any:
        """Return the signed connection descriptor used by plugin dispatch."""

        plugin_id = self.plugin_id_for_app(app_id)
        return self.descriptor_for_plugin_app(plugin_id, app_id)

    def descriptor_for_plugin_app(self, plugin_id: str, app_id: str) -> Any:
        """Return one exact plugin-and-app partitioned signed descriptor."""

        self._installed_verified(plugin_id)
        if app_id not in self.app_ids_for_plugin(plugin_id):
            raise ValueError("plugin_catalog_identity_ambiguous")
        return self._descriptor_for_record(plugin_id, self.app_registry.get(app_id))

    def _connection_requirement(self, plugin_id: str, record: Any) -> Any:
        verified = self._installed_verified(plugin_id)
        record = self.app_registry.get(record.app_id)
        requirement = next(
            (
                item
                for item in (verified.manifest.required_apps + verified.manifest.optional_apps)
                if item.app_id == record.app_id
            ),
            None,
        )
        if requirement is None:
            raise ValueError("plugin_catalog_identity_ambiguous")
        if (
            record.publisher_id != requirement.publisher_id
            or record.remote_origin != requirement.remote_origin
            or record.authorization_issuer != requirement.authorization_issuer
            or record.authentication != requirement.authentication
            or tuple(capability.to_dict() for capability in record.capabilities)
            != tuple(capability.to_dict() for capability in requirement.capabilities)
        ):
            raise ValueError("plugin_catalog_identity_ambiguous")
        return requirement

    def _installed_verified(self, plugin_id: str) -> VerifiedPlugin:
        """Reverify any installed plugin without consulting an embedded catalog."""

        return self._installed_verified_with_provenance(plugin_id)[0]

    def _installed_for_removal(self, plugin_id: str) -> VerifiedPlugin:
        """Verify enough to remove a plugin: it exists, and its package is intact.

        Deliberately not `_installed_verified`, which additionally requires a
        signed provider-provenance record. That record decides whether a plugin
        may be USED; a plugin whose record is gone is exactly the one an
        operator most needs to be able to uninstall.
        """
        verified = self.plugin_registry.reverify_installed(plugin_id)
        if verified.manifest.plugin_id != plugin_id:
            raise ValueError("plugin_catalog_identity_ambiguous")
        return verified

    def _installed_verified_with_provenance(
        self, plugin_id: str
    ) -> tuple[VerifiedPlugin, ProviderProvenanceRecord | None]:
        """Reverify an installed plugin and return its provenance record, if any.

        ``None`` means this plugin has no signed provider-provenance record at
        all (a self-published, non-authoritative-remote package) -- itself a
        fact ``update_review`` must be able to compare against a candidate that
        does carry one, or vice versa.
        """

        verified = self.plugin_registry.reverify_installed(plugin_id)
        if verified.manifest.plugin_id != plugin_id:
            raise ValueError("plugin_catalog_identity_ambiguous")
        provenance = self._verified_provenance()
        try:
            record = provenance.for_plugin(plugin_id)
        except ProviderProvenanceVerificationError as exc:
            if (
                str(exc) != "provider_provenance_not_found"
                or verified.manifest.publisher_id == provenance.publisher_id
            ):
                raise
            return verified, None
        provenance.for_verified_plugin(verified)
        return verified, record

    def _verified_provenance(self) -> VerifiedProviderProvenance:
        try:
            return verify_provider_provenance(_PROVENANCE_PATH, self._publisher_ring)
        except ProviderProvenanceVerificationError as exc:
            raise RuntimeError("plugin_catalog_unavailable") from exc

    def _verified_with_provenance(
        self,
        plugin_id: str,
    ) -> tuple[
        VerifiedMarketplaceCatalog,
        MarketplaceCatalogEntry,
        VerifiedPlugin,
        ProviderProvenanceRecord,
    ]:
        catalog = self._current_catalog()
        entry = next((item for item in catalog.entries if item.plugin_id == plugin_id), None)
        if entry is None:
            raise ValueError("plugin_catalog_entry_unavailable")
        try:
            package_path = self._package_resolver.resolve(entry, catalog=catalog)
        except EmbeddedCatalogResolutionError as exc:
            if str(exc) not in {
                "embedded_package_missing",
                "embedded_package_catalog_mismatch",
            }:
                reason = (
                    "plugin_catalog_identity_ambiguous"
                    if str(exc) == "embedded_package_catalog_mismatch"
                    else "plugin_catalog_unavailable"
                )
                raise RuntimeError(reason) from None
            try:
                package_path = self._package_fetcher.fetch(entry, catalog)
            except PackageArtifactError as fetch_error:
                raise RuntimeError("plugin_catalog_unavailable") from fetch_error
        try:
            verified = verify_plugin_package(
                package_path,
                self._publisher_ring,
                _OMEGA_VERSION,
            )
        except (OSError, TypeError, ValueError, PublisherTrustError) as exc:
            raise RuntimeError("plugin_catalog_unavailable") from exc
        try:
            provenance_document = self._verified_provenance()
            provenance = provenance_document.for_verified_plugin(verified)
            if sha256_hex(canonical_bytes(provenance.to_dict())) != entry.provenance_sha256:
                raise ProviderProvenanceVerificationError("provider_provenance_mismatch")
        except ProviderProvenanceVerificationError as exc:
            raise RuntimeError("plugin_catalog_identity_ambiguous") from exc
        return catalog, entry, verified, provenance

    def _verified(
        self, plugin_id: str
    ) -> tuple[VerifiedMarketplaceCatalog, MarketplaceCatalogEntry, VerifiedPlugin]:
        catalog, entry, verified, _provenance = self._verified_with_provenance(plugin_id)
        return catalog, entry, verified

    @staticmethod
    def _catalog_store_fingerprint(store: CatalogStore) -> tuple[object, ...]:
        def stat_of(path: Path) -> tuple[int, int] | None:
            try:
                observed = path.stat()
            except OSError:
                return None
            return (observed.st_mtime_ns, observed.st_size)

        return (stat_of(store.catalog_path), stat_of(store.bundled_path))

    def _current_catalog(self) -> VerifiedMarketplaceCatalog:
        """Serve the store's verified snapshot, re-verifying only on a real change.

        ``CatalogStore.current()`` fully re-verifies both the synced and
        bundled snapshot signatures on every call. Cached here, keyed by a
        cheap on-disk fingerprint (mtime/size) of both files, so repeated
        catalog reads inside one search or install pass do not each pay that
        cost. ``sync_catalog()`` and any successful promotion invalidate it.
        """

        with self._catalog_cache_lock:
            fingerprint = self._catalog_store_fingerprint(self._catalog_store)
            if self._catalog_cache is not None and fingerprint == self._catalog_cache_fingerprint:
                return self._catalog_cache
        try:
            catalog = self._catalog_store.current()
        except CatalogStoreError as exc:
            raise RuntimeError("plugin_catalog_unavailable") from exc
        with self._catalog_cache_lock:
            self._catalog_cache = catalog
            self._catalog_cache_fingerprint = fingerprint
        return catalog

    def _invalidate_catalog_cache(self) -> None:
        with self._catalog_cache_lock:
            self._catalog_cache = None
            self._catalog_cache_fingerprint = None

    def sync_catalog(self) -> CatalogSyncResult:
        """Fetch, verify, and atomically promote a newer signed snapshot.

        Synchronization only replaces the retained catalog snapshot; it never
        installs, updates, or enables a plugin. A failed or unreachable fetch
        leaves the last verified snapshot readable and reports the failure
        separately -- it never presents a fabricated empty marketplace.
        """

        result = CatalogSynchronizer(store=self._catalog_store, fetch=self._catalog_fetch).sync()
        self._invalidate_catalog_cache()
        return result

    @property
    def update_policy(self) -> PluginUpdatePolicy:
        """Return the strict persisted update policy used by this owner."""

        return read_policy(self._update_policy_store)

    @property
    def last_update_check(self) -> PluginUpdateCheckResult | None:
        """Return the last durable check outcome, including after restart."""

        return read_update_status(self._update_status_store)

    def set_update_policy(self, policy: PluginUpdatePolicy) -> PluginUpdatePolicy:
        """Persist a user-selected policy; no scheduler is created."""

        if type(policy) is not PluginUpdatePolicy:
            raise ValueError("plugin_update_policy_invalid")
        if self._closed:
            raise ValueError("plugin_lifecycle_closed")
        with self._authority_mutation_lock:
            write_policy(self._update_policy_store, policy)
            self._update_policy = policy
        return policy

    def _update_candidates(self) -> tuple[tuple[str, str], ...]:
        """Project version changes from the verified catalog without installing."""

        catalog = self._current_catalog()
        installed = {
            item.plugin_id: item.selected_version
            for item in self.plugin_registry.snapshot().records
        }
        return tuple(
            (entry.plugin_id, entry.plugin_version)
            for entry in sorted(catalog.entries, key=lambda item: item.plugin_id)
            if entry.plugin_id in installed
            and installed[entry.plugin_id] != entry.plugin_version
        )

    def check_updates(
        self,
        *,
        now: float | None = None,
        force: bool = False,
        _already_locked: bool = False,
        _apply: bool = True,
    ) -> PluginUpdateCheckResult:
        """Perform at most one bounded catalog check when due.

        This method is intentionally synchronous and lifecycle-owned.  Callers
        may ask for a manual check with ``force=True``; the HUD never schedules
        or repeats this work itself.
        """

        if type(_already_locked) is not bool or type(_apply) is not bool:
            raise ValueError("plugin_update_check_result_invalid")
        boundary = nullcontext() if _already_locked else self._authority_mutation_lock
        with boundary:
            status_store = getattr(self, "_update_status_store", None)
            if status_store is None:
                status_root = getattr(
                    self,
                    "_plugin_state_root",
                    getattr(self._update_policy_store, "state_root"),
                )
                status_store = update_status_store(status_root)
                self._update_status_store = status_store
            if self._closed:
                return PluginUpdateCheckResult(
                    status="shutdown", checked_at=None, next_check_at=None
                )
            current = time.time() if now is None else now
            prior_status = read_update_status(self._update_status_store)
            generation = (prior_status.generation if prior_status is not None else 0) + 1
            policy = read_policy(self._update_policy_store)
            if not force and not policy.enabled:
                return PluginUpdateCheckResult(
                    status="disabled",
                    checked_at=None,
                    next_check_at=policy.next_check_at,
                )
            if not force and not policy.due(current):
                return PluginUpdateCheckResult(
                    status="not_due",
                    checked_at=None,
                    next_check_at=policy.next_check_at,
                )
            try:
                sync = self.sync_catalog()
                if sync.status.value == "failed":
                    raise ValueError(sync.error or "plugin_catalog_sync_failed")
                # A fenced provider lookup only verifies the catalog and
                # queues promotion for the post-action lifecycle boundary.
                # Keep its due deadline intact until that promotion resolves;
                # it must not look like a successful daily update check.
                next_policy = policy if not _apply else policy.after_success(current)
                if _apply:
                    write_policy(self._update_policy_store, next_policy)
                    self._update_policy = next_policy
                candidates = self._update_candidates()
                result = PluginUpdateCheckResult(
                    status="checked",
                    checked_at=float(current),
                    next_check_at=next_policy.next_check_at,
                    candidate_versions=candidates,
                    generation=generation,
                )
                write_update_status(self._update_status_store, result)
                self._last_update_check = result
            except Exception as exc:
                next_policy = policy.after_failure(current)
                write_policy(self._update_policy_store, next_policy)
                self._update_policy = next_policy
                error = str(exc)
                if error not in SAFE_UPDATE_ERRORS:
                    error = "plugin_catalog_unavailable"
                result = PluginUpdateCheckResult(
                    status="failed",
                    checked_at=float(current),
                    next_check_at=next_policy.next_check_at,
                    error=error,
                    generation=generation,
                )
                write_update_status(self._update_status_store, result)
                self._last_update_check = result
                return result

        if not _apply:
            return result

        # The catalog check is complete and durable before updates are applied.
        # Applying outside the lock lets the normal update journal own its own
        # cutover while a concurrent due check observes the new deadline.
        if not hasattr(self, "plugin_registry"):
            return result
        staged: list[tuple[str, str]] = []
        approval: list[tuple[str, str, tuple[str, ...]]] = []
        errors: list[str] = []
        for candidate_plugin_id, candidate_version in result.candidate_versions:
            try:
                update_result = self.apply_compatible_update(
                    candidate_plugin_id, candidate_version
                )
            except (RuntimeError, ValueError) as exc:
                error = str(exc)
                if error not in SAFE_UPDATE_ERRORS:
                    error = "plugin_update_failed"
                update_result = PluginUpdateResult(
                    plugin_id=candidate_plugin_id,
                    version=candidate_version,
                    status="failed",
                    error=error,
                )
            if update_result.status == "approval_required":
                staged.append((candidate_plugin_id, candidate_version))
                approval.append(
                    (
                        candidate_plugin_id,
                        candidate_version,
                        update_result.approval_diff,
                    )
                )
            elif update_result.status not in {"applied"}:
                staged.append((candidate_plugin_id, candidate_version))
                if update_result.error is not None:
                    errors.append(update_result.error)
        if staged or approval:
            with self._authority_mutation_lock:
                # Candidate delivery happens outside the lifecycle fence, so
                # another forced check may have completed while this run was
                # applying.  Never let this older result or its failure
                # backoff overwrite that newer durable generation.
                latest = read_update_status(self._update_status_store)
                if (
                    latest is not None
                    and latest.generation > result.generation
                ):
                    self._last_update_check = latest
                    return latest
                latest_policy = read_policy(self._update_policy_store)
                next_check_at = result.next_check_at
                if errors:
                    # The catalog was fetched, but delivery did not complete.
                    # Replace the optimistic post-sync interval with failure
                    # backoff only for this still-current check generation.
                    latest_policy = latest_policy.after_failure(current)
                    write_policy(self._update_policy_store, latest_policy)
                    self._update_policy = latest_policy
                    next_check_at = latest_policy.next_check_at
                result = replace(
                    result,
                    status="failed" if errors else result.status,
                    staged=tuple(staged),
                    approval_required=tuple(approval),
                    error=errors[0] if errors else None,
                    next_check_at=next_check_at,
                )
                write_update_status(self._update_status_store, result)
                self._last_update_check = result
        return result

    def apply_compatible_update(
        self,
        plugin_id: str,
        version: str,
    ) -> PluginUpdateResult:
        """Apply a verified update only when complete authority is unchanged."""

        if self._closed:
            return PluginUpdateResult(
                plugin_id=plugin_id,
                version=version,
                status="failed",
                error="plugin_lifecycle_closed",
            )
        try:
            review = self.update_review(plugin_id, version)
            installed = self._installed_verified_with_provenance(plugin_id)
            _catalog, _entry, candidate, provenance = self._verified_with_provenance(plugin_id)
            if not complete_authority_equivalent(
                installed[0],
                candidate,
                installed_provenance=installed[1],
                candidate_provenance=provenance,
            ):
                return PluginUpdateResult(
                    plugin_id=plugin_id,
                    version=version,
                    status="approval_required",
                    approval_diff=review.permission_changes,
                )
            result = self.update(plugin_id, version, review.sha256, automatic=True)
            if isinstance(result, PluginUpdatePending):
                return PluginUpdateResult(
                    plugin_id=plugin_id,
                    version=version,
                    status="approval_required",
                    approval_diff=("connection_reconnect_required",),
                )
            return PluginUpdateResult(
                plugin_id=plugin_id,
                version=version,
                status="applied",
            )
        except (RuntimeError, ValueError) as exc:
            error = str(exc)
            if error == "plugin_reconnect_required":
                return PluginUpdateResult(
                    plugin_id=plugin_id,
                    version=version,
                    status="approval_required",
                    approval_diff=("connection_reconnect_required",),
                )
            if error not in SAFE_UPDATE_ERRORS:
                error = "plugin_update_failed"
            status = "not_available" if error == "plugin_update_not_available" else "failed"
            return PluginUpdateResult(
                plugin_id=plugin_id,
                version=version,
                status=status,
                error=error,
            )

    def _merged_catalog_revision(self) -> str:
        """A cheap, deterministic fingerprint of every source search reads.

        Bound into every catalog search cursor so a page fetched after any
        admitted source (or the installed registry) changed is reported as
        stale rather than silently mixing two different merged views.
        """

        parts: list[tuple[str, str]] = []
        for source in getattr(self._directory, "sources", ()):
            try:
                parts.append((source.descriptor.source_id, source.snapshot().revision))
            except Exception:
                parts.append((source.descriptor.source_id, ""))
        try:
            registry_revision = self._directory.plugin_registry.snapshot().revision
        except Exception:
            registry_revision = ""
        return sha256_hex(
            canonical_bytes(
                {
                    "sources": [list(item) for item in sorted(parts)],
                    "registry_revision": registry_revision,
                }
            )
        )

    def search_catalog(
        self,
        query: str,
        filters: Mapping[str, object],
        cursor: str | None,
        page_size: int,
    ) -> PluginCatalogPage:
        """Search the merged, signed marketplace catalog: bounded and deterministic.

        Results are drawn from every admitted catalog source (this device's
        verified marketplace snapshot plus any approved enterprise/installed
        source), merged by ``PluginDirectoryService`` exactly as the rest of
        the directory is. Two sources naming the same plugin identity is a
        closed failure -- neither source is selected. This is the *only* place
        that computes a plugin's lifecycle state for catalog display -- the
        HUD route delegates here rather than repeating this projection.
        """

        if type(query) is not str:
            raise ValueError("plugin_catalog_signature_invalid")
        normalized_query = query.strip().casefold()
        if len(normalized_query) > 200:
            raise ValueError("plugin_catalog_signature_invalid")
        if not isinstance(filters, Mapping):
            raise ValueError("plugin_catalog_signature_invalid")
        allowed_filter_keys = {"trust", "category", "state"}
        if set(filters) - allowed_filter_keys:
            raise ValueError("plugin_catalog_signature_invalid")
        normalized_filters: dict[str, str] = {}
        for key in sorted(allowed_filter_keys):
            value = filters.get(key)
            if value is None:
                continue
            if type(value) is not str or not value:
                raise ValueError("plugin_catalog_signature_invalid")
            normalized_filters[key] = value.strip().casefold()
        if (
            "trust" in normalized_filters
            and normalized_filters["trust"] not in {tier.value for tier in TrustTier}
        ):
            raise ValueError("plugin_catalog_signature_invalid")
        if (
            "state" in normalized_filters
            and normalized_filters["state"]
            not in {state.value for state in PluginLifecycleState}
        ):
            raise ValueError("plugin_catalog_signature_invalid")
        if type(page_size) is not int or not 1 <= page_size <= MAX_CATALOG_PAGE_SIZE:
            raise ValueError("plugin_catalog_signature_invalid")
        if cursor is not None and (type(cursor) is not str or not cursor):
            raise ValueError("plugin_catalog_signature_invalid")

        stable: tuple[str, str, tuple[dict[str, object], ...]] | None = None
        for _attempt in range(3):
            before = self._merged_catalog_revision()
            try:
                snapshot = self._directory.list_directory()
            except PluginDirectoryError as exc:
                if str(exc) == "plugin_directory_identity_ambiguous":
                    raise ValueError("plugin_catalog_identity_ambiguous") from None
                raise
            projected: list[dict[str, object]] = []
            for entry in snapshot.plugins:
                document = entry.to_dict()
                if document.get("installed") is True:
                    lifecycle_state = self._submit_connection_operation(
                        lambda entry=entry: self.catalog_state(
                            entry.plugin_id,
                            catalog_version=entry.plugin_version,
                        )
                    )
                else:
                    lifecycle_state = PluginLifecycleState.AVAILABLE.value
                document["lifecycle_state"] = lifecycle_state
                projected.append(document)
            after = self._merged_catalog_revision()
            if before == after:
                stable = (after, snapshot.status, tuple(projected))
                break
        if stable is None:
            raise ValueError("plugin_lifecycle_revision_conflict")
        catalog_revision, directory_status, entries = stable

        def matches(document: dict[str, object]) -> bool:
            if normalized_query:
                haystack = "\n".join(
                    str(document.get(field, ""))
                    for field in (
                        "plugin_id",
                        "title",
                        "description",
                        "publisher_display_name",
                    )
                ).casefold()
                if normalized_query not in haystack:
                    return False
            trust = normalized_filters.get("trust")
            if trust and str(document.get("trust_state", "")).casefold() != trust:
                return False
            category = normalized_filters.get("category")
            categories = document.get("categories")
            if type(categories) is not list:
                categories = []
            if category and category not in {str(item).casefold() for item in categories}:
                return False
            state = normalized_filters.get("state")
            if state and str(document.get("lifecycle_state", "")).casefold() != state:
                return False
            return True

        filtered = tuple(
            item
            for item in sorted(entries, key=lambda item: str(item.get("plugin_id", "")))
            if matches(item)
        )
        filter_items = tuple(sorted(normalized_filters.items()))
        offset = 0
        if cursor is not None:
            offset = _decode_catalog_cursor(
                self._catalog_cursor_key,
                cursor,
                query=normalized_query,
                filters=filter_items,
                page_size=page_size,
                catalog_revision=catalog_revision,
            )
        if offset > len(filtered) or (cursor is not None and offset == len(filtered)):
            raise ValueError("plugin_catalog_cursor_invalid")
        page_items = filtered[offset : offset + page_size]
        next_offset = offset + len(page_items)
        next_cursor = (
            _catalog_cursor(
                self._catalog_cursor_key,
                offset=next_offset,
                query=normalized_query,
                filters=filter_items,
                page_size=page_size,
                catalog_revision=catalog_revision,
            )
            if next_offset < len(filtered)
            else None
        )
        return PluginCatalogPage(
            items=page_items,
            next_cursor=next_cursor,
            status="ok" if directory_status == "ready" else "degraded",
            total=len(filtered),
        )

    def import_marketplace(
        self, plugin_id: str, version: str
    ) -> PluginInstallReview | PluginDiscoveryInstallReview:
        """Review one exact catalog version before any install proceeds.

        An Omega-verified entry follows the existing signed-package install
        review path (:meth:`review`) unchanged. A publisher-verified entry is
        never installed sight-unseen: it enters restricted capability
        discovery against its declared endpoint and returns every discovered
        capability at its ``external_unreviewed`` risk. No tool call happens
        before the user approves a review.
        """

        catalog = self._current_catalog()
        try:
            entry = catalog.entry(plugin_id)
        except CatalogVerificationError as exc:
            raise ValueError("plugin_catalog_entry_unavailable") from exc
        if type(version) is not str or entry.plugin_version != version:
            raise ValueError("plugin_install_review_changed")
        if entry.trust_tier is TrustTier.OMEGA_VERIFIED:
            return self.review(plugin_id)
        if entry.trust_tier is TrustTier.PUBLISHER_VERIFIED:
            return self._discovery_review(entry)
        raise ValueError("plugin_runtime_unsupported")

    def _discovery_review(
        self, entry: MarketplaceCatalogEntry
    ) -> PluginDiscoveryInstallReview:
        if (
            self._discovery_peer_provider is None
            or entry.distribution.kind is not DistributionKind.REMOTE_MCP
        ):
            raise ValueError("plugin_runtime_unsupported")
        try:
            peer = self._discovery_peer_provider(entry)
        except PluginDiscoveryError:
            raise
        except Exception as exc:
            raise ValueError("plugin_discovery_transport_unavailable") from exc
        binding = CapabilityDiscoveryBinding(
            plugin_id=entry.plugin_id,
            app_id=entry.plugin_id,
            publisher_id=entry.publisher_id,
            account_key_hash=sha256_hex(
                self.operator_account_key().encode("utf-8")
            ),
            endpoint=entry.distribution.remote_origin,
            protocol_version=PLUGIN_PROTOCOL_VERSION,
        )
        review = self._submit_connection_operation(
            lambda: RestrictedPluginDiscovery(peer).discover(binding)
        )
        return PluginDiscoveryInstallReview(
            plugin_id=entry.plugin_id,
            publisher_id=entry.publisher_id,
            plugin_version=entry.plugin_version,
            trust_tier=entry.trust_tier.value,
            review=review,
            sha256=review.review_sha256,
        )

    def update_review(self, plugin_id: str, version: str) -> PluginUpdateReview:
        """Diff the installed version against one exact catalog version.

        Purely read-only: nothing here mutates the installed record. The
        returned ``sha256`` is the same signed-install confirmation digest
        :meth:`update` requires, so a review the user actually saw is the only
        thing that can confirm the update it describes.
        """

        record = self.plugin_registry.get(plugin_id)
        _catalog, entry, candidate, candidate_provenance = self._verified_with_provenance(
            plugin_id
        )
        if type(version) is not str or entry.plugin_version != version:
            raise ValueError("plugin_install_review_changed")
        if record.selected_version == candidate.manifest.plugin_version:
            raise ValueError("plugin_update_not_available")
        installed, installed_provenance = self._installed_verified_with_provenance(plugin_id)
        changes = _update_permission_changes(
            installed,
            candidate,
            installed_provenance=installed_provenance,
            candidate_provenance=candidate_provenance,
        )
        install_review = self.review(plugin_id)
        return PluginUpdateReview(
            plugin_id=plugin_id,
            from_version=record.selected_version,
            to_version=candidate.manifest.plugin_version,
            permission_changes=changes,
            sha256=install_review.sha256,
        )

    def review(self, plugin_id: str) -> PluginInstallReview:
        catalog, entry, verified = self._verified(plugin_id)
        snapshots = tuple(
            project_declared_capabilities(requirement).snapshot
            for requirement in (verified.manifest.required_apps + verified.manifest.optional_apps)
        )
        from plugin_platform.dependencies import resolve_dependencies

        resolution = resolve_dependencies(verified.manifest, snapshots)
        return self._installer.review_from_catalog(
            catalog,
            entry,
            verified,
            resolution,
            expected_registry_revision=self.plugin_registry.snapshot().revision,
        )

    def install(self, plugin_id: str, *, confirmation: object) -> PluginInstallResult:
        with self._authority_mutation_lock:
            accepted = self._admit_lifecycle_operation(
                kind="install",
                plugin_id=plugin_id,
            )
            try:
                return self._install_locked(plugin_id, confirmation=confirmation)
            finally:
                self._release_lifecycle_operation_if_current(accepted)

    def _install_locked(
        self,
        plugin_id: str,
        *,
        confirmation: object,
    ) -> PluginInstallResult:
        catalog = self._current_catalog()
        try:
            entry = catalog.entry(plugin_id)
        except CatalogVerificationError as exc:
            raise ValueError("plugin_catalog_entry_unavailable") from exc
        if entry.trust_tier is TrustTier.PUBLISHER_VERIFIED:
            return self._install_publisher_verified_locked(entry, confirmation=confirmation)
        catalog, entry, verified = self._verified(plugin_id)
        result = self._installer.install_from_catalog(catalog, entry, verified, confirmation)
        target_version = verified.manifest.plugin_version
        if result.record.selected_version != target_version:
            promoted = self.plugin_registry.promote_candidate(
                plugin_id,
                target_version,
                expected_revision=result.revision,
            )
            result = replace(
                result,
                record=promoted.record,
                revision=promoted.revision,
            )
        return result

    def _install_publisher_verified_locked(
        self,
        entry: MarketplaceCatalogEntry,
        *,
        confirmation: object,
    ) -> PluginInstallResult:
        """Durably install one publisher-verified remote-MCP marketplace entry.

        There is no signed marketplace package to download for this trust
        tier: the confirmation the caller presents is the exact digest of a
        fresh restricted-discovery review of the same declared endpoint (the
        one ``import_marketplace``/``_discovery_review`` already showed the
        user), re-run here rather than trusted from an earlier call, so a
        capability that changed since the user last saw it is caught before
        anything installs. The normalized candidate is built from the signed
        catalog entry's own fields -- never fetched from the network -- and
        every discovered capability enters at ``external_unreviewed`` risk;
        this device seals nothing beyond what restricted discovery actually
        enumerated.
        """
        review = self._discovery_review(entry)
        trust_root = self._local_trust_root()
        server = _synthetic_registry_server(entry)
        normalized = MarketplaceImporter().normalize(server, entry.plugin_version)
        candidate = MarketplaceImporter().verify(normalized)
        if (
            candidate.manifest.plugin_id != entry.plugin_id
            or candidate.manifest.publisher_id != entry.publisher_id
            or candidate.manifest.plugin_version != entry.plugin_version
        ):
            raise ValueError("plugin_catalog_identity_ambiguous")
        peer = self._discovery_peer_provider(entry)
        scope = _discovered_capability_scope(review.review)
        if not scope:
            raise ValueError("plugin_catalog_authority_missing")
        envelope = RestrictedPluginDiscovery(peer, trust_root=trust_root).approve(
            review.review, scope
        )
        return self._installer.accept_normalized(
            candidate,
            envelope,
            trust_root,
            confirmation,
            expected_review_sha256=review.sha256,
        )

    def _offered_provider_scopes(self, plugin_id: str, app_id: str) -> tuple[str, ...]:
        verified = self._installed_verified(plugin_id)
        requirements = (
            verified.manifest.required_apps + verified.manifest.optional_apps
        )
        matches = tuple(app for app in requirements if app.app_id == app_id)
        if len(matches) != 1:
            raise ValueError("plugin_catalog_identity_ambiguous")
        return tuple(matches[0].authentication.scopes)

    def _assert_plugin_compiled(self, plugin_id: str) -> None:
        from hashlib import sha256

        from mcp_platform.plugin_catalog import compile_plugin_runtime_catalog

        account_key = self.operator_account_key()
        try:
            # The gate admits exactly the authority the runtime catalog serves
            # (plugin_dispatch.plugin_runtime_catalog): an OAuth grant or a
            # credentialless connection authority, whichever the app holds.
            catalog = compile_plugin_runtime_catalog(
                plugin_snapshot=self.plugin_registry.snapshot(),
                app_snapshot=self.app_registry.snapshot(),
                descriptor_for_app=self.descriptor_for_plugin_app,
                account_key_hash=sha256(account_key.encode("utf-8")).hexdigest(),
                app_authority_provider=self.planning_app_authority,
                verified_plugin_provider=self._installed_verified,
            )
        except ValueError as exc:
            if str(exc) == "plugin_catalog_authority_missing":
                raise ValueError("plugin_catalog_authority_missing") from exc
            raise
        record = self.plugin_registry.get(plugin_id)
        has_remote_apps = record.app_binding is not None and bool(
            record.app_binding.required_app_ids + record.app_binding.optional_app_ids
        )
        remote_kinds = {"tool", "resource", "resource_template", "prompt"}
        if not any(
            item.plugin_id == plugin_id
            and (not has_remote_apps or item.capability_kind in remote_kinds)
            for item in catalog.entries
        ):
            raise ValueError("plugin_catalog_authority_missing")

    def _enable_if_compiled_sync(self, plugin_id: str) -> None:
        self._set_enabled(plugin_id, True)
        try:
            self._assert_plugin_compiled(plugin_id)
        except BaseException:
            self._set_enabled(plugin_id, False)
            raise

    async def _enable_if_compiled(self, plugin_id: str) -> None:
        await asyncio.to_thread(self._enable_if_compiled_sync, plugin_id)

    def _set_enabled(self, plugin_id: str, enabled: bool) -> None:
        record = self.plugin_registry.get(plugin_id)
        if record.enabled is enabled:
            return
        snapshot = self.plugin_registry.snapshot()
        self.plugin_registry.set_enabled(
            plugin_id,
            enabled,
            expected_revision=snapshot.revision,
        )

    async def _publish_connected_status(self, status: Any) -> Any:
        """Enable a plugin the connection service has just reported connected.

        This used to re-derive a status snapshot first and skip the enable
        unless that snapshot already said CONNECTED. A snapshot taken in the
        same breath as the grant it describes can still report the connection
        settling, and when it did the enable never ran: the authorization had
        succeeded and been committed, yet the plugin stayed disabled with no
        callable tools while the callback reported failure. The connection
        service's own verdict is the authority here, and ``_enable_if_compiled``
        already guards itself -- it disables again and raises if the plugin does
        not compile -- so there is nothing for the extra gate to protect.
        """
        if getattr(status, "status", None) != "connected":
            return status
        try:
            await self._enable_if_compiled(status.plugin_id)
        except BaseException:
            await self.connection_service.invalidate_execution(
                status.plugin_id,
                app_id=status.app_id,
                account_key=self.operator_account_key(),
            )
            raise
        return await self.status(status.plugin_id)

    def _pending_candidate_request(
        self,
        request_id: str,
    ) -> tuple[dict[str, object], dict[str, object], VerifiedPlugin, PluginAppBinding] | None:
        pending = self._pending_update()
        if pending is None:
            return None
        operation, verified, binding = pending
        candidate_apps = operation["candidate_apps"]
        assert type(candidate_apps) is list
        matches = tuple(
            item
            for item in candidate_apps
            if item["state"] == "authorizing" and item["request_id"] == request_id
        )
        if not matches:
            return None
        if len(matches) != 1:
            raise ValueError("plugin_update_recovery_required")
        return operation, matches[0], verified, binding

    async def _finish_candidate_preparation(
        self,
        operation: dict[str, object],
        app_state: dict[str, object],
        verified: VerifiedPlugin,
        binding: PluginAppBinding,
        preparation: CandidateConnectionPreparation,
    ) -> Any:
        if (
            preparation.plugin_id != operation["plugin_id"]
            or preparation.app_id != app_state["app_id"]
            or preparation.request_id != app_state["request_id"]
        ):
            raise ValueError("plugin_update_recovery_required")
        app_state["state"] = "ready"
        app_state["connection_revision"] = preparation.activation.connection_revision
        app_state["activation"] = preparation.activation.to_dict()
        self._write_update_document(self._update_document(operation))
        next_app_id = self._next_candidate_app(operation)
        if next_app_id is not None:
            return PluginUpdateContinuation(
                plugin_id=str(operation["plugin_id"]),
                state=PluginLifecycleState.SETUP_REQUIRED,
                next_required_app_id=next_app_id,
            )
        await self._promote_update_candidate(operation, verified, binding)
        return await self.status(str(operation["plugin_id"]))

    @staticmethod
    def _connection_update_recovery(
        operation: dict[str, object],
    ) -> PluginUpdateConnectionRecovery | None:
        recovery = operation.get("connection_recovery")
        if recovery is None:
            return None
        return PluginUpdateConnectionRecovery.from_dict(recovery)

    async def _promote_update_candidate(
        self,
        operation: dict[str, object],
        verified: VerifiedPlugin,
        binding: PluginAppBinding,
    ) -> Any:
        """Run one journaled reversible cutover and establish the commit point."""

        candidate_apps = operation["candidate_apps"]
        assert type(candidate_apps) is list
        if any(
            type(item.get("settings")) is not dict
            for item in candidate_apps
            if type(item) is dict
        ):
            raise ValueError("plugin_authorization_required")
        revisions = {
            str(item["app_id"]): str(item["connection_revision"])
            for item in candidate_apps
        }
        snapshot = self.plugin_registry.snapshot()
        self._assert_candidate_bindings(
            str(operation["plugin_id"]),
            verified,
            binding,
            revisions,
            snapshot=snapshot,
        )
        recovery = self._connection_update_recovery(operation)
        if recovery is not None:
            self.connection_service.begin_update_cutover(recovery)
        operation["phase"] = "cutover"
        self._write_update_document(self._update_document(operation))
        promoted = self.plugin_registry.promote_candidate(
            str(operation["plugin_id"]),
            verified.manifest.plugin_version,
            expected_revision=snapshot.revision,
            app_binding=binding,
        )
        operation["phase"] = "plugin_promoted"
        self._write_update_document(self._update_document(operation))
        self._installer.provisioner.promote_candidate_records(
            binding.dependency_receipt_id,
            expected_revision=self.app_registry.snapshot().revision,
        )
        operation["phase"] = "apps_promoted"
        self._write_update_document(self._update_document(operation))
        account_key = self.operator_account_key()
        for item in candidate_apps:
            item["state"] = "activating"
            operation["phase"] = "activating"
            self._write_update_document(self._update_document(operation))
            activation_document = item["activation"]
            if activation_document is not None and operation.get("automatic") is True:
                rebind = getattr(self.plugin_grants, "rebind_compatible_update", None)
                if not callable(rebind):
                    raise ValueError("plugin_reconnect_required")
                account_hash = sha256(
                    account_key.encode("utf-8")
                ).hexdigest()
                rebound = rebind(
                    str(operation["plugin_id"]),
                    app_id=str(item["app_id"]),
                    account_key_sha256=account_hash,
                    package_revision=str(operation["target_package_sha256"]),
                )
                if type(rebound) is not PluginGrantRecord:
                    raise ValueError("plugin_reconnect_required")
                item["request_id"] = rebound.activation.attempt_state_sha256
                item["connection_revision"] = rebound.activation.connection_revision
                item["activation"] = rebound.activation.to_dict()
                self._seal_update_recovery(operation, binding, recovery)
                self._write_update_document(self._update_document(operation))
                start_compatible = getattr(
                    self.connection_service, "start_compatible_update", None
                )
                if not callable(start_compatible):
                    raise ValueError("plugin_connection_drift")
                connected = await start_compatible(
                    str(operation["plugin_id"]),
                    app_id=str(item["app_id"]),
                    account_key=account_key,
                    activation=rebound.activation,
                    expected_registry_revision=promoted.revision,
                )
            elif activation_document is None:
                descriptor = self.descriptor_for_plugin_app(
                    str(operation["plugin_id"]),
                    str(item["app_id"]),
                )
                offered_capabilities = sorted(
                    capability
                    for capability in descriptor.declared_capabilities
                    if capability.startswith("tool:")
                )
                validated = self._validated_permission_settings(
                    str(operation["plugin_id"]),
                    {
                        "offered": {
                            "provider_scopes": list(
                                self._offered_provider_scopes(
                                    str(operation["plugin_id"]),
                                    str(item["app_id"]),
                                )
                            ),
                            "capability_ids": offered_capabilities,
                        }
                    },
                    item["settings"],
                    descriptor=descriptor,
                )
                connected = await self.connection_service.start(
                    str(operation["plugin_id"]),
                    app_id=str(item["app_id"]),
                    account_key=account_key,
                    expected_registry_revision=promoted.revision,
                    **validated,
                )
                if (
                    tuple(getattr(connected, "provider_scopes", ()))
                    != tuple(validated["scopes"])
                    or tuple(getattr(connected, "capability_ceiling", ()))
                    != tuple(validated["capability_ceiling"])
                    or getattr(
                        getattr(connected, "execution_policy", None),
                        "value",
                        getattr(connected, "execution_policy", None),
                    )
                    != getattr(
                        validated["execution_policy"],
                        "value",
                        validated["execution_policy"],
                    )
                ):
                    raise ValueError("plugin_connection_state_invalid")
            else:
                activation = PluginGrantActivation.from_dict(activation_document)
                candidate_preparation = CandidateConnectionPreparation(
                    request_id=str(item["request_id"]),
                    plugin_id=str(operation["plugin_id"]),
                    app_id=str(item["app_id"]),
                    target_version=str(operation["target_version"]),
                    target_manifest_sha256=str(operation["target_manifest_sha256"]),
                    target_package_sha256=str(operation["target_package_sha256"]),
                    candidate_binding_revision=binding.revision,
                    activation=activation,
                )
                connected = await self.connection_service.activate_candidate(
                    candidate_preparation,
                    candidate_verified=verified,
                    expected_registry_revision=promoted.revision,
                )
            if getattr(connected, "status", None) != "connected":
                raise ValueError("plugin_update_failed")
            item["state"] = "active"
            operation["phase"] = "activated"
            self._write_update_document(self._update_document(operation))
        self._assert_plugin_compiled(str(operation["plugin_id"]))
        operation["phase"] = "compiled"
        self._write_update_document(self._update_document(operation))
        operation["phase"] = "committed"
        self._write_update_document(self._update_document(operation))
        try:
            await self._retire_update_predecessors(
                operation,
                binding,
                expected_registry_revision=promoted.revision,
            )
        except BaseException as exc:
            if not self._retryable_update_retirement_error(exc):
                raise
            raise ValueError("plugin_revocation_pending") from exc
        operation["phase"] = "retirement_complete"
        self._write_update_document(self._update_document(operation))
        if recovery is not None:
            await self.connection_service.finalize_update_recovery(recovery)
        self._clear_update_document(operation)
        return promoted

    async def _retire_update_predecessors(
        self,
        operation: dict[str, object],
        binding: PluginAppBinding,
        *,
        expected_registry_revision: str,
    ) -> None:
        previous = InstalledPluginRecord.from_dict(
            operation["base_record"],
            registry_key=str(operation["plugin_id"]),
        )
        previous_binding = previous.app_binding
        if previous_binding is None:
            return
        recovery = self._connection_update_recovery(operation)
        current_apps = set(binding.required_app_ids + binding.optional_app_ids)
        candidate_activations = tuple(
            PluginGrantActivation.from_dict(item["activation"])
            for item in operation.get("candidate_apps", [])
            if type(item) is dict and item.get("activation") is not None
        )
        candidate_connection_revisions = tuple(
            (str(item["app_id"]), str(item["connection_revision"]))
            for item in operation.get("candidate_apps", [])
            if type(item) is dict and item.get("connection_revision") is not None
        )
        for app_id in (
            previous_binding.required_app_ids + previous_binding.optional_app_ids
        ):
            if recovery is None:
                if app_id in current_apps:
                    continue
                await self.connection_service.retire_predecessor(
                    previous.plugin_id,
                    app_id=app_id,
                    account_key=self.operator_account_key(),
                    previous_record=previous,
                    expected_registry_revision=expected_registry_revision,
                )
            else:
                await self.connection_service.retire_predecessor_for_update(
                    recovery,
                    app_id=app_id,
                    candidate_activations=candidate_activations,
                    candidate_connection_revisions=candidate_connection_revisions,
                )

    @staticmethod
    def _retryable_update_retirement_error(error: BaseException) -> bool:
        return str(error) in {
            "oauth_provider_rejected",
            "oauth_provider_timeout",
            "oauth_provider_unreachable",
            "oauth_revocation_pending",
            "oauth_revocation_unsupported",
            "plugin_revocation_pending",
        }

    def _abort_pending_update(self) -> None:
        self._reconcile_update_intent()

    async def complete_connection(
        self,
        callback_uri: str,
        *,
        request_id: str | None = None,
    ) -> Any:
        async with self._authority_mutation_lock:
            return await self._complete_connection_locked(
                callback_uri,
                request_id=request_id,
            )

    async def _complete_connection_locked(
        self,
        callback_uri: str,
        *,
        request_id: str | None = None,
    ) -> Any:
        try:
            pending_update = self._pending_update()
        except BaseException:
            if self._read_update_document()["operation"] is not None:
                self._abort_pending_update()
            raise
        candidate_request_id = request_id
        if pending_update is not None and candidate_request_id is None:
            candidate_request_id = self.connection_service.callback_request_id(
                callback_uri
            )
        candidate = (
            None
            if candidate_request_id is None
            else self._pending_candidate_request(candidate_request_id)
        )
        if candidate is not None:
            operation, app_state, verified, binding = candidate
            try:
                preparation = await self.connection_service.complete_candidate(
                    candidate_request_id,
                    callback_uri,
                    candidate_verified=verified,
                    candidate_binding=binding,
                    expected_registry_revision=self.plugin_registry.snapshot().revision,
                )
                return await self._finish_candidate_preparation(
                    operation,
                    app_state,
                    verified,
                    binding,
                    preparation,
                )
            except BaseException:
                self._abort_pending_update()
                raise
        normal_request_id = request_id
        update_operation = (
            None
            if not hasattr(self, "_update_state")
            else self._read_update_document()["operation"]
        )
        lifecycle_operation = (
            None
            if not hasattr(self, "_operation_state")
            else self._read_lifecycle_operation_document()["operation"]
        )
        if normal_request_id is None and (
            pending_update is not None
            or update_operation is not None
            or lifecycle_operation is not None
        ):
            normal_request_id = (
                candidate_request_id
                if candidate_request_id is not None
                else self.connection_service.callback_request_id(callback_uri)
            )
        if normal_request_id is not None:
            self._assert_connection_request_mutation_available(normal_request_id)
        if request_id is None:
            status = await self.connection_service.complete_callback(callback_uri)
        else:
            status = await self.connection_service.complete(request_id, callback_uri)
        return await self._publish_connected_status(status)

    async def poll_connection(self, request_id: str) -> Any:
        async with self._authority_mutation_lock:
            return await self._poll_connection_locked(request_id)

    async def _poll_connection_locked(self, request_id: str) -> Any:
        try:
            candidate = self._pending_candidate_request(request_id)
        except BaseException:
            if self._read_update_document()["operation"] is not None:
                self._abort_pending_update()
            raise
        if candidate is not None:
            operation, app_state, verified, binding = candidate
            try:
                preparation = await self.connection_service.complete_candidate_device(
                    request_id,
                    candidate_verified=verified,
                    candidate_binding=binding,
                    expected_registry_revision=self.plugin_registry.snapshot().revision,
                )
                return await self._finish_candidate_preparation(
                    operation,
                    app_state,
                    verified,
                    binding,
                    preparation,
                )
            except BaseException as exc:
                if str(exc) not in {
                    "oauth_device_authorization_pending",
                    "oauth_device_slow_down",
                    "oauth_device_poll_too_soon",
                }:
                    self._abort_pending_update()
                raise
        self._assert_connection_request_mutation_available(request_id)
        status = await self.connection_service.complete_device(request_id)
        if getattr(status, "status", None) == "finalizing":
            return status
        if getattr(status, "status", None) != "connected":
            raise ValueError("plugin_authorization_required")
        return await self._publish_connected_status(status)

    async def _start_candidate_connection_locked(
        self,
        plugin_id: str,
        app_id: str,
        *,
        operation: dict[str, object],
        candidate_verified: VerifiedPlugin,
        candidate_binding: PluginAppBinding,
        validated: dict[str, object],
    ) -> Any:
        next_app_id = self._next_candidate_app(operation)
        if app_id != next_app_id:
            raise ValueError("plugin_app_not_installed")
        snapshot = self.plugin_registry.snapshot()
        started = await self.connection_service.start_candidate(
            plugin_id,
            app_id=app_id,
            candidate_verified=candidate_verified,
            candidate_binding=candidate_binding,
            account_key=self.operator_account_key(),
            expected_registry_revision=snapshot.revision,
            **validated,
        )
        candidate_apps = operation["candidate_apps"]
        assert type(candidate_apps) is list
        app_state = next(
            item
            for item in candidate_apps
            if type(item) is dict and item.get("app_id") == app_id
        )
        app_state["settings"] = {
            "provider_scopes": list(validated["scopes"]),
            "capability_ids": list(validated["capability_ceiling"]),
            "execution_policy": getattr(
                validated["execution_policy"], "value", validated["execution_policy"]
            ),
            "source_constraints": dict(validated["source_constraints"]),
            "destination_constraints": dict(validated["destination_constraints"]),
        }
        if isinstance(started, CandidateConnectionProbe):
            app_state["state"] = "ready"
            app_state["connection_revision"] = started.connection_revision
            self._write_update_document(self._update_document(operation))
            next_app_id = self._next_candidate_app(operation)
            if next_app_id is not None:
                return PluginUpdateContinuation(
                    plugin_id=plugin_id,
                    state=PluginLifecycleState.SETUP_REQUIRED,
                    next_required_app_id=next_app_id,
                )
            await self._promote_update_candidate(
                operation,
                candidate_verified,
                candidate_binding,
            )
            return await self.status(plugin_id)
        request_id = getattr(started, "request_id", None)
        if type(request_id) is not str:
            raise ValueError("plugin_connection_state_invalid")
        app_state["state"] = "authorizing"
        app_state["request_id"] = request_id
        self._write_update_document(self._update_document(operation))
        return started

    async def permissions(
        self,
        plugin_id: str,
        app_id: str | None = None,
    ) -> dict[str, object] | PluginAppSelectionRequired:
        pending = self._pending_update(plugin_id)
        pending_settings: dict[str, object] | None = None
        if pending is None:
            app_ids = self._app_ids(plugin_id)
        else:
            operation, _verified, binding = pending
            app_ids = binding.required_app_ids + binding.optional_app_ids
        if app_id is None:
            if len(app_ids) > 1:
                return PluginAppSelectionRequired(plugin_id, app_ids)
            if not app_ids:
                raise ValueError("plugin_setup_not_required")
            app_id = app_ids[0]
        elif app_id not in app_ids:
            raise ValueError("plugin_app_not_installed")
        if pending is None:
            status = await self.connection_service.status(
                plugin_id,
                app_id=app_id,
                account_key=self.operator_account_key(),
            )
            descriptor = self.descriptor_for_plugin_app(plugin_id, app_id)
            offered_scopes = self._offered_provider_scopes(plugin_id, app_id)
        else:
            _operation, verified, binding = pending
            candidate_app = next(
                (
                    item
                    for item in _operation.get("candidate_apps", [])
                    if type(item) is dict and item.get("app_id") == app_id
                ),
                None,
            )
            if type(candidate_app) is dict:
                pending_settings = candidate_app.get("settings")
            requirement, descriptor = self.connection_service._candidate_definition(
                plugin_id,
                app_id,
                candidate_verified=verified,
                candidate_binding=binding,
                expected_registry_revision=self.plugin_registry.snapshot().revision,
            )[2:4]
            status = await self._selected_app_status(plugin_id, app_id)
            offered_scopes = tuple(requirement.authentication.scopes)
        offered_capabilities = tuple(
            sorted(
                capability
                for capability in descriptor.declared_capabilities
                if capability.startswith("tool:")
            )
        )
        selected_scopes = tuple(
            pending_settings["provider_scopes"]
            if pending_settings is not None
            else (
                scope
                for scope in (getattr(status, "provider_scopes", ()) or ())
                if scope in offered_scopes
            )
        )
        selected_capabilities = tuple(
            pending_settings["capability_ids"]
            if pending_settings is not None
            else (
                capability
                for capability in (getattr(status, "capability_ceiling", ()) or ())
                if capability in offered_capabilities
            )
        )
        selected_policy = (
            pending_settings["execution_policy"]
            if pending_settings is not None
            else getattr(status, "execution_policy", None)
        )
        selected_source_constraints = (
            pending_settings["source_constraints"]
            if pending_settings is not None
            else getattr(status, "source_constraints", {})
        )
        selected_destination_constraints = (
            pending_settings["destination_constraints"]
            if pending_settings is not None
            else getattr(status, "destination_constraints", {})
        )
        return {
            "plugin_id": plugin_id,
            "app_id": app_id,
            "revision": self.revision,
            "status": (
                "candidate_setup_required"
                if pending is not None
                else str(getattr(status, "status", ""))
            ),
            "candidate": pending is not None,
            "offered": {
                "provider_scopes": list(offered_scopes),
                "capability_ids": list(offered_capabilities),
                "execution_policies": ["direct_turn", "confirm_each"],
            },
            "selected": {
                "provider_scopes": list(selected_scopes),
                "capability_ids": list(selected_capabilities),
                "execution_policy": getattr(
                    selected_policy,
                    "value",
                    "confirm_each",
                ),
                "source_constraints": dict(selected_source_constraints or {}),
                "destination_constraints": dict(
                    selected_destination_constraints or {}
                ),
            },
        }

    async def set_permissions(
        self,
        plugin_id: str,
        *,
        revision: str,
        settings: dict[str, object],
        app_id: str | None = None,
    ) -> Any:
        async with self._authority_mutation_lock:
            return await self._set_permissions_locked(
                plugin_id,
                revision=revision,
                settings=settings,
                app_id=app_id,
            )

    async def _set_permissions_locked(
        self,
        plugin_id: str,
        *,
        revision: str,
        settings: dict[str, object],
        app_id: str | None = None,
    ) -> Any:
        pending = self._pending_update(plugin_id)
        if pending is None:
            self._assert_plugin_authority_mutation_available(plugin_id)
        before = self._lifecycle_revision_snapshot()
        if revision != before.revision:
            raise ValueError("plugin_permission_changed")
        selection = await self.permissions(plugin_id, app_id)
        if self._lifecycle_revision_snapshot().revision != before.revision:
            raise ValueError("plugin_permission_changed")
        if type(selection) is PluginAppSelectionRequired:
            return selection
        exact_app_id = str(selection["app_id"])
        if pending is None:
            descriptor = self.descriptor_for_plugin_app(plugin_id, exact_app_id)
        else:
            _operation, verified, binding = pending
            descriptor = self.connection_service._candidate_definition(
                plugin_id,
                exact_app_id,
                candidate_verified=verified,
                candidate_binding=binding,
                expected_registry_revision=self.plugin_registry.snapshot().revision,
            )[3]
        validated = self._validated_permission_settings(
            plugin_id,
            selection,
            settings,
            descriptor=descriptor,
        )
        if self.permission_store is not None:
            self.permission_store.invalidate_plugin(plugin_id)
        try:
            if pending is not None:
                operation, verified, binding = pending
                return await self._start_candidate_connection_locked(
                    plugin_id,
                    exact_app_id,
                    operation=operation,
                    candidate_verified=verified,
                    candidate_binding=binding,
                    validated=validated,
                )
            return await self.connection_service.start(
                plugin_id,
                app_id=exact_app_id,
                account_key=self.operator_account_key(),
                **validated,
                expected_registry_revision=before.plugin_registry_revision,
            )
        except ValueError as exc:
            if str(exc) in {
                "plugin_authority_revision_conflict",
                "plugin_grant_revision_conflict",
                "plugin_registry_conflict",
                "mcp_app_registry_conflict",
                "plugin_lifecycle_revision_conflict",
            }:
                raise ValueError("plugin_permission_changed") from exc
            raise

    def try_in_chat(self, plugin_id: str) -> TryInChatHandoff:
        return self._directory.try_in_chat_handoff(plugin_id)

    async def disconnect(self, plugin_id: str) -> PluginLifecycleSnapshot:
        async with self._authority_mutation_lock:
            return await self._disconnect_locked(plugin_id)

    async def _disconnect_locked(self, plugin_id: str) -> PluginLifecycleSnapshot:
        self._assert_plugin_authority_mutation_available(plugin_id)
        await self.connection_coordinator.disconnect(
            plugin_id,
            self.operator_account_key(),
        )
        await asyncio.to_thread(self._set_enabled, plugin_id, False)
        return await self.status(plugin_id)

    def _configure_lifecycle_owner_state(self, plugin_state_root: Path) -> None:
        self._operation_state = McpStateStore(
            state_root=plugin_state_root,
            document_path=plugin_state_root / "lifecycle-operation.json",
            invalid_path_reason="plugin_lifecycle_operation_unavailable",
            max_document_bytes=1024 * 1024,
        )
        self._lifecycle_owner_lease = None
        self._lifecycle_owner_lease_generation: int | None = None
        self._owned_lifecycle_operation = None
        self._lifecycle_owner_instance_id = sha256_hex(os.urandom(32))
        self._lifecycle_mutation_depth: ContextVar[int] = ContextVar(
            f"plugin_lifecycle_mutation_{id(self)}",
            default=0,
        )
        self._lifecycle_mutation_capability: ContextVar[object | None] = ContextVar(
            f"plugin_lifecycle_mutation_capability_{id(self)}",
            default=None,
        )
        self._lifecycle_mutation_execution: ContextVar[
            tuple[int, object | None] | None
        ] = ContextVar(
            f"plugin_lifecycle_mutation_execution_{id(self)}",
            default=None,
        )
        self._active_lifecycle_mutation_capability: object | None = None
        if not hasattr(self, "_lifecycle_initializing"):
            self._lifecycle_initializing = False
        if not hasattr(self, "_closed"):
            self._closed = False

    def _ensure_lifecycle_owner_fields(self) -> None:
        if not hasattr(self, "_lifecycle_owner_lease"):
            self._lifecycle_owner_lease = None
        if not hasattr(self, "_lifecycle_owner_lease_generation"):
            self._lifecycle_owner_lease_generation = None
        if not hasattr(self, "_owned_lifecycle_operation"):
            self._owned_lifecycle_operation = None
        if not hasattr(self, "_lifecycle_owner_instance_id"):
            self._lifecycle_owner_instance_id = sha256_hex(os.urandom(32))
        if not hasattr(self, "_lifecycle_initializing"):
            self._lifecycle_initializing = False
        if not hasattr(self, "_lifecycle_mutation_depth"):
            self._lifecycle_mutation_depth = ContextVar(
                f"plugin_lifecycle_mutation_{id(self)}",
                default=0,
            )
        if not hasattr(self, "_lifecycle_mutation_capability"):
            self._lifecycle_mutation_capability = ContextVar(
                f"plugin_lifecycle_mutation_capability_{id(self)}",
                default=None,
            )
        if not hasattr(self, "_lifecycle_mutation_execution"):
            self._lifecycle_mutation_execution = ContextVar(
                f"plugin_lifecycle_mutation_execution_{id(self)}",
                default=None,
            )
        if not hasattr(self, "_active_lifecycle_mutation_capability"):
            self._active_lifecycle_mutation_capability = None
        if not hasattr(self, "_closed"):
            self._closed = False

    @staticmethod
    def _empty_lifecycle_operation_document(
        generation: int = 0,
    ) -> dict[str, object]:
        if (
            type(generation) is not int
            or generation < 0
            or generation > 9_223_372_036_854_775_807
        ):
            raise ValueError("plugin_lifecycle_operation_unavailable")
        return {"schema_version": 2, "generation": generation, "operation": None}

    @staticmethod
    def _decode_lifecycle_operation_document(payload: bytes | None) -> dict[str, object]:
        if payload is None:
            return PluginLifecycleService._empty_lifecycle_operation_document()

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError
                result[key] = item
            return result

        try:
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=reject_duplicates,
            )
            if type(document) is not dict:
                raise ValueError
            if set(document) == {"schema_version", "operation"}:
                if document["schema_version"] != 1 or document["operation"] is not None:
                    # An active pre-lease owner could still be live.  It cannot
                    # be migrated safely merely because the new lease is free.
                    raise ValueError
                return PluginLifecycleService._empty_lifecycle_operation_document()
            if (
                set(document) != {"schema_version", "generation", "operation"}
                or document["schema_version"] != 2
                or type(document["generation"]) is not int
                or document["generation"] < 0
                or document["generation"] > 9_223_372_036_854_775_807
            ):
                raise ValueError
            generation = document["generation"]
            operation = document["operation"]
            if operation is None:
                return PluginLifecycleService._empty_lifecycle_operation_document(generation)
            if type(operation) is not dict or set(operation) != {
                "operation_id",
                "generation",
                "kind",
                "plugin_id",
                "owner_instance_id",
                "lease_identity",
            }:
                raise ValueError
            operation_id = _closed_revision(operation["operation_id"])
            owner_instance_id = _closed_revision(operation["owner_instance_id"])
            operation_generation = operation["generation"]
            kind = operation["kind"]
            plugin_id = operation["plugin_id"]
            lease_identity = operation["lease_identity"]
            if (
                type(operation_generation) is not int
                or operation_generation <= 0
                or operation_generation != generation
                or generation > 9_223_372_036_854_775_807
                or kind not in {"install", "update", "uninstall"}
                or type(plugin_id) is not str
                or not plugin_id
                or plugin_id != plugin_id.strip()
                or len(plugin_id) > 128
                or type(lease_identity) is not list
                or len(lease_identity) != 2
                or any(
                    type(item) is not str
                    or len(item) != 16
                    or any(character not in "0123456789abcdef" for character in item)
                    for item in lease_identity
                )
            ):
                raise ValueError
            return {
                "schema_version": 2,
                "generation": generation,
                "operation": {
                    "operation_id": operation_id,
                    "generation": operation_generation,
                    "kind": kind,
                    "plugin_id": plugin_id,
                    "owner_instance_id": owner_instance_id,
                    "lease_identity": list(lease_identity),
                },
            }
        except (TypeError, UnicodeError, ValueError, json.JSONDecodeError):
            raise ValueError("plugin_lifecycle_operation_unavailable") from None

    def _read_lifecycle_operation_document(self) -> dict[str, object]:
        with self._operation_state.locked():
            return self._decode_lifecycle_operation_document(
                self._operation_state.read_bytes()
            )

    @staticmethod
    def _lifecycle_owner_lease_name(generation: int) -> str:
        if generation < 0 or generation >= 9_223_372_036_854_775_807:
            raise ValueError("plugin_lifecycle_operation_unavailable")
        if os.name == "nt":
            return "lifecycle-operation.owner.lease"
        return f"lifecycle-operation.owner.{generation + 1}.lease"

    def _lifecycle_owner_lease_expected_generation(self) -> int:
        generation = self._lifecycle_owner_lease_generation
        if type(generation) is not int or generation < 0:
            raise ValueError("plugin_lifecycle_operation_conflict")
        return generation

    def _acquire_lifecycle_owner_lease(self) -> bool:
        self._ensure_lifecycle_owner_fields()
        if self._closed:
            raise ValueError("plugin_lifecycle_operation_conflict")
        if self._lifecycle_owner_lease is not None:
            try:
                self._lifecycle_owner_lease.validate()
            except ValueError:
                raise ValueError("plugin_lifecycle_operation_conflict") from None
            return True
        with self._operation_state.locked():
            document = self._decode_lifecycle_operation_document(
                self._operation_state.read_bytes()
            )
        expected_generation = int(document["generation"])
        lease = self._operation_state.try_acquire_lease(
            self._lifecycle_owner_lease_name(expected_generation)
        )
        if lease is None:
            return False
        self._lifecycle_owner_lease = lease
        self._lifecycle_owner_lease_generation = expected_generation
        return True

    def _lifecycle_owner_lease_identity(self) -> tuple[str, str]:
        self._ensure_lifecycle_owner_fields()
        lease = self._lifecycle_owner_lease
        if lease is None:
            raise ValueError("plugin_lifecycle_operation_conflict")
        try:
            lease.validate()
            identity = lease.identity
            if any(item > 18_446_744_073_709_551_615 for item in identity):
                raise ValueError
            return (f"{identity[0]:016x}", f"{identity[1]:016x}")
        except ValueError:
            raise ValueError("plugin_lifecycle_operation_conflict") from None

    def _retain_lifecycle_owner_lease_for_worker_reap(self) -> None:
        """Keep the exact process lease while a worker tree remains unproven."""

        self._ensure_lifecycle_owner_fields()
        self._lifecycle_owner_lease_identity()
        self._retain_lifecycle_owner_lease = True

    @contextmanager
    def _lifecycle_mutation_lease(self) -> Iterator[None]:
        """Fence every normal authority mutation against a cross-process 202 owner."""

        if not hasattr(self, "_operation_state"):
            # Narrow unit doubles predate the composed durable service boundary.
            yield
            return
        self._ensure_lifecycle_owner_fields()
        already_held = self._lifecycle_owner_lease is not None
        if not self._acquire_lifecycle_owner_lease():
            raise ValueError("plugin_lifecycle_operation_in_progress")
        acquired_here = not already_held
        release_preheld_on_exit = already_held and self._lifecycle_initializing
        try:
            lease_identity = self._lifecycle_owner_lease_identity()
            with self._operation_state.locked():
                document = self._decode_lifecycle_operation_document(
                    self._operation_state.read_bytes()
                )
            if (
                document["generation"]
                != self._lifecycle_owner_lease_expected_generation()
            ):
                raise ValueError("plugin_lifecycle_operation_conflict")
            current = document["operation"]
            owned = self._owned_lifecycle_operation
            if current is None:
                if owned is not None:
                    raise ValueError("plugin_lifecycle_operation_conflict")
            elif (
                owned != current
                or tuple(current["lease_identity"]) != lease_identity
            ):
                reason = (
                    "plugin_lifecycle_operation_in_progress"
                    if acquired_here or owned is None
                    else "plugin_lifecycle_operation_conflict"
                )
                raise ValueError(reason)
            if self._active_lifecycle_mutation_capability is not None:
                raise ValueError("plugin_lifecycle_operation_conflict")
            capability = object()
            self._active_lifecycle_mutation_capability = capability
            execution = self._current_lifecycle_mutation_execution()
            depth_token = self._lifecycle_mutation_depth.set(
                self._lifecycle_mutation_depth.get() + 1
            )
            capability_token = self._lifecycle_mutation_capability.set(capability)
            execution_token = self._lifecycle_mutation_execution.set(execution)
            try:
                yield
                lease = self._lifecycle_owner_lease
                if lease is not None:
                    lease.validate()
            finally:
                if (
                    (acquired_here or release_preheld_on_exit)
                    and self._owned_lifecycle_operation is None
                    and not getattr(
                        self,
                        "_retain_lifecycle_owner_lease",
                        False,
                    )
                ):
                    # Close the kernel lease while the capability is still
                    # active. Clearing the capability first opens a same-
                    # process handoff race that can strand an unfenced caller.
                    self._close_lifecycle_owner_lease()
                self._lifecycle_mutation_execution.reset(execution_token)
                self._lifecycle_mutation_capability.reset(capability_token)
                self._lifecycle_mutation_depth.reset(depth_token)
                if self._active_lifecycle_mutation_capability is capability:
                    self._active_lifecycle_mutation_capability = None
        finally:
            if (
                acquired_here
                and self._owned_lifecycle_operation is None
                and not getattr(
                    self,
                    "_retain_lifecycle_owner_lease",
                    False,
                )
            ):
                self._close_lifecycle_owner_lease()

    @contextmanager
    def _component_mutation_lease(self) -> Iterator[None]:
        """Fence direct component calls unless they are nested in this owner task."""

        self._ensure_lifecycle_owner_fields()
        if self._lifecycle_mutation_depth.get() > 0:
            capability = self._lifecycle_mutation_capability.get()
            if (
                capability is None
                or capability is not self._active_lifecycle_mutation_capability
                or self._current_lifecycle_mutation_execution()
                != self._lifecycle_mutation_execution.get()
            ):
                raise ValueError("plugin_lifecycle_operation_in_progress")
            self._lifecycle_owner_lease_identity()
            current = self._read_lifecycle_operation_document()["operation"]
            if current != self._owned_lifecycle_operation:
                if current is not None or self._owned_lifecycle_operation is not None:
                    raise ValueError("plugin_lifecycle_operation_conflict")
            yield
            return
        if self._owned_lifecycle_operation is not None:
            raise ValueError("plugin_lifecycle_operation_in_progress")
        boundary = getattr(self, "_authority_mutation_lock", None)
        if boundary is None:
            raise ValueError("plugin_lifecycle_operation_conflict")
        with boundary:
            yield

    def plugin_execution_mutation_scope(self) -> object:
        """Hold one exact lifecycle fence from authority resolution through dispatch."""

        return self._plugin_execution_mutation_scope()

    @asynccontextmanager
    async def _component_mutation_async_lease(self) -> AsyncIterator[None]:
        """Async entry fence that never blocks the shared transport loop."""

        self._ensure_lifecycle_owner_fields()
        if self._lifecycle_mutation_depth.get() > 0:
            with self._component_mutation_lease():
                yield
            return
        if self._owned_lifecycle_operation is not None:
            raise ValueError("plugin_lifecycle_operation_in_progress")
        boundary = getattr(self, "_authority_mutation_lock", None)
        if boundary is None:
            raise ValueError("plugin_lifecycle_operation_conflict")
        async with boundary:
            yield

    def _handoff_lifecycle_mutation_operation(self, operation: Any) -> Any:
        """Authorize one joined supervisor task beneath the caller's held lease."""

        capability = self._lifecycle_mutation_capability.get()
        if (
            self._lifecycle_mutation_depth.get() <= 0
            or capability is None
            or capability is not self._active_lifecycle_mutation_capability
            or self._lifecycle_mutation_execution.get()
            != self._current_lifecycle_mutation_execution()
        ):
            return operation

        async def run_handed_off() -> Any:
            if capability is not self._active_lifecycle_mutation_capability:
                raise ValueError("plugin_lifecycle_operation_in_progress")
            self._lifecycle_owner_lease_identity()
            execution_token = self._lifecycle_mutation_execution.set(
                self._current_lifecycle_mutation_execution()
            )
            capability_token = self._lifecycle_mutation_capability.set(capability)
            depth_token = self._lifecycle_mutation_depth.set(1)
            try:
                return await operation()
            finally:
                self._lifecycle_mutation_depth.reset(depth_token)
                self._lifecycle_mutation_capability.reset(capability_token)
                self._lifecycle_mutation_execution.reset(execution_token)

        return run_handed_off

    @staticmethod
    def _current_lifecycle_mutation_execution() -> tuple[int, object | None]:
        try:
            task: object | None = asyncio.current_task()
        except RuntimeError:
            task = None
        return threading.get_ident(), task

    def _claim_lifecycle_operation_ownership(self) -> bool:
        """Claim one free kernel lease and generation-fence a dead owner."""

        if not self._acquire_lifecycle_owner_lease():
            return False
        lease_identity = self._lifecycle_owner_lease_identity()
        expected_generation = self._lifecycle_owner_lease_expected_generation()
        with self._operation_state.locked():
            document = self._decode_lifecycle_operation_document(
                self._operation_state.read_bytes()
            )
            if document["generation"] != expected_generation:
                self._close_lifecycle_owner_lease()
                return False
            current = document["operation"]
            if current is None:
                self._owned_lifecycle_operation = None
                if self._operation_state.read_bytes() is None:
                    self._operation_state.write_bytes(canonical_bytes(document))
                return True
            assert type(current) is dict
            generation = expected_generation + 1
            claimed = {
                **current,
                "generation": generation,
                "owner_instance_id": self._lifecycle_owner_instance_id,
                "lease_identity": list(lease_identity),
            }
            self._operation_state.write_bytes(
                canonical_bytes(
                    {
                        "schema_version": 2,
                        "generation": generation,
                        "operation": claimed,
                    }
                )
            )
            self._owned_lifecycle_operation = dict(claimed)
            self._lifecycle_owner_lease_generation = generation
        return True

    def _release_lifecycle_owner_lease_if_idle(self) -> None:
        self._ensure_lifecycle_owner_fields()
        if self._lifecycle_initializing or self._owned_lifecycle_operation is not None:
            return
        lease = self._lifecycle_owner_lease
        self._lifecycle_owner_lease = None
        self._lifecycle_owner_lease_generation = None
        if lease is not None:
            lease.close()

    def _close_lifecycle_owner_lease(self) -> None:
        self._ensure_lifecycle_owner_fields()
        lease = self._lifecycle_owner_lease
        self._lifecycle_owner_lease = None
        self._lifecycle_owner_lease_generation = None
        if lease is not None:
            lease.close()

    def _admit_lifecycle_operation(
        self,
        *,
        kind: str,
        plugin_id: str,
    ) -> dict[str, object]:
        if not self._acquire_lifecycle_owner_lease():
            raise ValueError("plugin_lifecycle_operation_in_progress")
        try:
            with self._operation_state.locked():
                document = self._decode_lifecycle_operation_document(
                    self._operation_state.read_bytes()
                )
                expected_generation = self._lifecycle_owner_lease_expected_generation()
                if document["generation"] != expected_generation:
                    raise ValueError("plugin_lifecycle_operation_conflict")
                if document["operation"] is not None:
                    raise ValueError("plugin_lifecycle_operation_in_progress")
                generation = expected_generation + 1
                lease_identity = self._lifecycle_owner_lease_identity()
                proposed = {
                    "operation_id": sha256_hex(os.urandom(32)),
                    "generation": generation,
                    "kind": kind,
                    "plugin_id": plugin_id,
                    "owner_instance_id": self._lifecycle_owner_instance_id,
                    "lease_identity": list(lease_identity),
                }
                validated = self._decode_lifecycle_operation_document(
                    canonical_bytes(
                        {
                            "schema_version": 2,
                            "generation": generation,
                            "operation": proposed,
                        }
                    )
                )
                proposed = validated["operation"]
                assert type(proposed) is dict
                self._operation_state.write_bytes(canonical_bytes(validated))
                self._lifecycle_owner_lease_generation = generation
        except BaseException:
            self._release_lifecycle_owner_lease_if_idle()
            raise
        self._owned_lifecycle_operation = dict(proposed)
        return dict(proposed)

    def _release_lifecycle_operation(self, expected: object) -> None:
        if type(expected) is not dict or type(expected.get("generation")) is not int:
            raise ValueError("plugin_lifecycle_operation_conflict")
        validated = self._decode_lifecycle_operation_document(
            canonical_bytes(
                {
                    "schema_version": 2,
                    "generation": expected["generation"],
                    "operation": expected,
                }
            )
        )["operation"]
        assert type(validated) is dict
        self._ensure_lifecycle_owner_fields()
        self._lifecycle_owner_lease_identity()
        if (
            self._lifecycle_owner_lease is None
            or self._owned_lifecycle_operation != validated
        ):
            raise ValueError("plugin_lifecycle_operation_conflict")
        with self._operation_state.locked():
            document = self._decode_lifecycle_operation_document(
                self._operation_state.read_bytes()
            )
            current = document["operation"]
            if current != validated:
                raise ValueError("plugin_lifecycle_operation_conflict")
            self._operation_state.write_bytes(
                canonical_bytes(
                    self._empty_lifecycle_operation_document(int(document["generation"]))
                )
            )
        self._owned_lifecycle_operation = None
        self._release_lifecycle_owner_lease_if_idle()

    def _release_lifecycle_operation_if_current(self, expected: object) -> bool:
        if type(expected) is not dict or type(expected.get("generation")) is not int:
            raise ValueError("plugin_lifecycle_operation_conflict")
        validated = self._decode_lifecycle_operation_document(
            canonical_bytes(
                {
                    "schema_version": 2,
                    "generation": expected["generation"],
                    "operation": expected,
                }
            )
        )["operation"]
        assert type(validated) is dict
        self._ensure_lifecycle_owner_fields()
        with self._operation_state.locked():
            document = self._decode_lifecycle_operation_document(
                self._operation_state.read_bytes()
            )
            current = document["operation"]
            if current is None:
                return False
            self._lifecycle_owner_lease_identity()
            if (
                current != validated
                or self._lifecycle_owner_lease is None
                or self._owned_lifecycle_operation != validated
            ):
                raise ValueError("plugin_lifecycle_operation_conflict")
            self._operation_state.write_bytes(
                canonical_bytes(
                    self._empty_lifecycle_operation_document(int(document["generation"]))
                )
            )
        self._owned_lifecycle_operation = None
        self._release_lifecycle_owner_lease_if_idle()
        return True

    def _lifecycle_operation_for_subordinate(
        self,
        operation: dict[str, object],
        *,
        kind: str,
    ) -> dict[str, object] | None:
        """Return the exact fenced owner bound to one subordinate journal.

        Legacy journals did not carry an operation id.  They may be recovered
        only while this process holds the kernel lease, and only against an
        exact matching current kind/plugin owner.  Newly written journals bind
        the stable operation id; generation remains the global CAS fence and is
        deliberately advanced when a dead owner is recovered.
        """

        if not hasattr(self, "_operation_state"):
            return None
        self._ensure_lifecycle_owner_fields()
        operation_id = operation.get("lifecycle_operation_id")
        if operation_id is not None:
            operation_id = _closed_revision(operation_id)
        plugin_id = operation.get("plugin_id")
        if (
            self._lifecycle_owner_lease is None
            or type(plugin_id) is not str
            or not plugin_id
        ):
            raise ValueError("plugin_lifecycle_operation_conflict")
        lease_identity = self._lifecycle_owner_lease_identity()
        owned = self._owned_lifecycle_operation
        if owned is None:
            current = self._read_lifecycle_operation_document()["operation"]
            if current is not None or operation_id is not None:
                raise ValueError("plugin_lifecycle_operation_conflict")
            return None
        if tuple(owned["lease_identity"]) != lease_identity:
            raise ValueError("plugin_lifecycle_operation_conflict")
        if (
            operation_id is None
            or owned["kind"] != kind
            or owned["plugin_id"] != plugin_id
            or owned["operation_id"] != operation_id
        ):
            raise ValueError("plugin_lifecycle_operation_conflict")
        if self._read_lifecycle_operation_document()["operation"] != owned:
            raise ValueError("plugin_lifecycle_operation_conflict")
        return dict(owned)

    def _require_owned_lifecycle_operation(
        self,
        *,
        kind: str,
        plugin_id: str,
    ) -> dict[str, object]:
        if not hasattr(self, "_operation_state"):
            raise ValueError("plugin_lifecycle_operation_conflict")
        self._ensure_lifecycle_owner_fields()
        owned = self._owned_lifecycle_operation
        if (
            self._lifecycle_owner_lease is None
            or owned is None
            or owned["kind"] != kind
            or owned["plugin_id"] != plugin_id
            or self._read_lifecycle_operation_document()["operation"] != owned
        ):
            raise ValueError("plugin_lifecycle_operation_conflict")
        if tuple(owned["lease_identity"]) != self._lifecycle_owner_lease_identity():
            raise ValueError("plugin_lifecycle_operation_conflict")
        return dict(owned)

    def _reconcile_lifecycle_operation(self) -> None:
        """Release an admission whose subordinate durable journal is resolved."""

        # The owner fields are what this compares against, and reconcile runs
        # early enough that a reused store may not have them yet.
        self._ensure_lifecycle_owner_fields()
        current = self._read_lifecycle_operation_document()["operation"]
        if current is None:
            return
        if current != self._owned_lifecycle_operation:
            # Left behind by an owner that did not finish. If that owner is
            # still running it holds this lease and the refusal below is
            # correct; if it is gone, acquiring the lease proves it, and the
            # record it left must be recovered rather than kept forever --
            # `_admit_lifecycle_operation` refuses while any record stands, so
            # one interrupted install otherwise wedges every install after it.
            if not self._acquire_lifecycle_owner_lease():
                raise ValueError("plugin_lifecycle_operation_unavailable")
            self._owned_lifecycle_operation = current
        if (
            self._read_update_document()["operation"] is not None
            or self._read_uninstall_document()["operation"] is not None
        ):
            return
        self._installer.recover()
        self._release_lifecycle_operation(current)

    def _assert_plugin_authority_mutation_available(self, plugin_id: str) -> None:
        """Protect a pending update's exact predecessor authority checkpoint."""

        if hasattr(self, "_operation_state"):
            operation = self._read_lifecycle_operation_document()["operation"]
            if operation is not None and operation["plugin_id"] == plugin_id:
                raise ValueError("plugin_lifecycle_operation_in_progress")
        if hasattr(self, "_update_state"):
            update = self._read_update_document()["operation"]
            if update is not None and update["plugin_id"] == plugin_id:
                raise ValueError("plugin_lifecycle_operation_in_progress")

    def _assert_connection_request_mutation_available(self, request_id: str) -> None:
        lifecycle = (
            None
            if not hasattr(self, "_operation_state")
            else self._read_lifecycle_operation_document()["operation"]
        )
        update = (
            None
            if not hasattr(self, "_update_state")
            else self._read_update_document()["operation"]
        )
        if lifecycle is None and update is None:
            return
        resolver = getattr(self.connection_service, "request_plugin_id", None)
        if not callable(resolver):
            raise ValueError("plugin_lifecycle_operation_in_progress")
        self._assert_plugin_authority_mutation_available(resolver(request_id))

    def _update_checkpoint_id(self, plugin_id: str) -> str | None:
        if not hasattr(self, "_operation_state"):
            return None
        operation = self._read_lifecycle_operation_document()["operation"]
        if operation is None:
            return None
        if operation["kind"] != "update" or operation["plugin_id"] != plugin_id:
            raise ValueError("plugin_update_recovery_required")
        return "plugin-update-recovery-" + str(operation["operation_id"])

    def _discard_unsealed_update_checkpoint(
        self,
        operation: dict[str, object],
    ) -> None:
        checkpoint_id = self._update_checkpoint_id(str(operation["plugin_id"]))
        if checkpoint_id is None:
            return
        service = getattr(self, "_connection_service", None)
        discard = getattr(service, "discard_update_recovery_checkpoint", None)
        if not callable(discard):
            raise ValueError("plugin_update_recovery_required")
        discard(
            checkpoint_id,
            plugin_id=str(operation["plugin_id"]),
            account_key=self.operator_account_key(),
        )

    @staticmethod
    def _empty_update_document() -> dict[str, object]:
        return {"schema_version": 1, "operation": None}

    @staticmethod
    def _candidate_settings_document(value: object) -> dict[str, object] | None:
        if value is None:
            return None
        keys = {
            "provider_scopes",
            "capability_ids",
            "execution_policy",
            "source_constraints",
            "destination_constraints",
        }
        if type(value) is not dict or set(value) != keys:
            raise ValueError("plugin_update_recovery_required")
        scopes = value["provider_scopes"]
        capabilities = value["capability_ids"]
        policy = value["execution_policy"]
        source = value["source_constraints"]
        destination = value["destination_constraints"]
        if (
            type(scopes) is not list
            or any(type(item) is not str for item in scopes)
            or len(scopes) != len(set(scopes))
            or type(capabilities) is not list
            or any(type(item) is not str for item in capabilities)
            or len(capabilities) != len(set(capabilities))
            or type(policy) is not str
            or policy not in {"direct_turn", "confirm_each"}
            or type(source) is not dict
            or type(destination) is not dict
        ):
            raise ValueError("plugin_update_recovery_required")
        try:
            source = json.loads(canonical_bytes(source).decode("utf-8"))
            destination = json.loads(canonical_bytes(destination).decode("utf-8"))
        except (TypeError, ValueError):
            raise ValueError("plugin_update_recovery_required") from None
        return {
            "provider_scopes": sorted(scopes),
            "capability_ids": sorted(capabilities),
            "execution_policy": policy,
            "source_constraints": source,
            "destination_constraints": destination,
        }

    @staticmethod
    def _activation_settings_document(
        activation: PluginGrantActivation,
    ) -> dict[str, object]:
        return {
            "provider_scopes": list(activation.scopes),
            "capability_ids": list(activation.capability_ceiling),
            "execution_policy": activation.execution_policy.value,
            "source_constraints": dict(activation.source_constraints),
            "destination_constraints": dict(activation.destination_constraints),
        }

    @staticmethod
    def _update_document(operation: dict[str, object]) -> dict[str, object]:
        normalized = dict(operation)
        if type(normalized.get("candidate_apps")) is list:
            candidate_apps = []
            for raw in normalized["candidate_apps"]:
                if type(raw) is not dict or "settings" not in raw:
                    raise ValueError("plugin_update_recovery_required")
                item = dict(raw)
                settings = PluginLifecycleService._candidate_settings_document(
                    item["settings"]
                )
                if item.get("activation") is not None:
                    activation = PluginGrantActivation.from_dict(item["activation"])
                    activation_settings = (
                        PluginLifecycleService._candidate_settings_document(
                            PluginLifecycleService._activation_settings_document(activation)
                        )
                    )
                    if settings is not None and settings != activation_settings:
                        raise ValueError("plugin_update_recovery_required")
                    settings = activation_settings
                item["settings"] = settings
                candidate_apps.append(item)
            normalized["candidate_apps"] = candidate_apps
        if "recovery_sha256" in normalized:
            material = {
                "plugin_id": normalized["plugin_id"],
                "base_record": normalized["base_record"],
                "dependency_receipt_id": normalized["dependency_receipt_id"],
                "phase": normalized["phase"],
                "candidate_apps": normalized["candidate_apps"],
                "candidate_binding": normalized["candidate_binding"],
                "candidate_identity": normalized["candidate_identity"],
                "connection_recovery": normalized["connection_recovery"],
            }
            if "automatic" in normalized:
                material["automatic"] = normalized["automatic"]
            if "lifecycle_operation_id" in normalized:
                material["lifecycle_operation_id"] = normalized[
                    "lifecycle_operation_id"
                ]
            normalized["recovery_sha256"] = sha256_hex(
                canonical_bytes(
                    {
                        "domain": "omega.plugin-update-recovery.v1",
                        "material": material,
                    }
                )
            )
        return {
            "schema_version": 2 if "recovery_sha256" in normalized else 1,
            "operation": normalized,
        }

    @staticmethod
    def _decode_update_document(payload: bytes | None) -> dict[str, object]:
        if payload is None:
            return PluginLifecycleService._empty_update_document()

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            result: dict[str, object] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError
                result[key] = item
            return result

        try:
            document = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicates)
            if (
                type(document) is not dict
                or set(document) != {"schema_version", "operation"}
                or document["schema_version"] not in {1, 2}
            ):
                raise ValueError
            schema_version = document["schema_version"]
            operation = document["operation"]
            if operation is None:
                return PluginLifecycleService._empty_update_document()
            legacy_fields = {
                "plugin_id",
                "target_version",
                "target_signing_key_id",
                "target_manifest_sha256",
                "target_package_sha256",
                "base_record",
                "dependency_receipt_id",
                "phase",
            }
            recovery_fields = {
                "candidate_binding",
                "candidate_identity",
                "connection_recovery",
                "recovery_sha256",
            }
            binding_fields = {"lifecycle_operation_id"}
            allowed_fields = (
                {
                    frozenset(legacy_fields),
                    frozenset((*legacy_fields, *binding_fields)),
                    frozenset((*legacy_fields, "candidate_apps")),
                    frozenset((*legacy_fields, "candidate_apps", *binding_fields)),
                    frozenset((*legacy_fields, "candidate_apps", "automatic")),
                    frozenset((*legacy_fields, "candidate_apps", "automatic", *binding_fields)),
                }
                if schema_version == 1
                else {
                    frozenset((*legacy_fields, "candidate_apps", *recovery_fields)),
                    frozenset(
                        (
                            *legacy_fields,
                            "candidate_apps",
                            *recovery_fields,
                            *binding_fields,
                        )
                    ),
                    frozenset((*legacy_fields, "candidate_apps", "automatic", *recovery_fields)),
                    frozenset(
                        (
                            *legacy_fields,
                            "candidate_apps",
                            "automatic",
                            *recovery_fields,
                            *binding_fields,
                        )
                    ),
                }
            )
            if type(operation) is not dict or frozenset(operation) not in allowed_fields:
                raise ValueError
            if "automatic" in operation and type(operation["automatic"]) is not bool:
                raise ValueError
            base_record = InstalledPluginRecord.from_dict(
                operation["base_record"],
                registry_key=operation["plugin_id"],
            )
            for field in ("plugin_id", "target_version", "target_signing_key_id"):
                value = operation[field]
                if type(value) is not str or not value or value != value.strip() or len(value) > 128:
                    raise ValueError
            _closed_revision(operation["target_manifest_sha256"])
            _closed_revision(operation["target_package_sha256"])
            if "lifecycle_operation_id" in operation:
                _closed_revision(operation["lifecycle_operation_id"])
            receipt_id = operation["dependency_receipt_id"]
            if type(receipt_id) is not str or not receipt_id or receipt_id != receipt_id.strip():
                raise ValueError
            if operation["phase"] not in {
                "installing",
                "candidate",
                "cutover",
                "plugin_promoted",
                "apps_promoted",
                "activating",
                "activated",
                "retiring",
                "retired",
                "compiled",
                "committed",
                "retirement_complete",
                "rolled_back",
                "promoted",
            }:
                raise ValueError
            candidate_apps = operation.get("candidate_apps")
            if candidate_apps is not None:
                if type(candidate_apps) is not list or not candidate_apps:
                    raise ValueError
                app_ids: list[str] = []
                normalized_apps: list[dict[str, object]] = []
                for raw in candidate_apps:
                    if type(raw) is not dict or set(raw) != {
                        "app_id",
                        "state",
                        "request_id",
                        "connection_revision",
                        "activation",
                        "settings",
                    }:
                        raise ValueError
                    app_id = raw["app_id"]
                    state = raw["state"]
                    request_id = raw["request_id"]
                    connection_revision = raw["connection_revision"]
                    activation = raw["activation"]
                    settings = PluginLifecycleService._candidate_settings_document(
                        raw["settings"]
                    )
                    if (
                        type(app_id) is not str
                        or not app_id
                        or app_id != app_id.strip()
                        or state not in {
                            "setup_required",
                            "authorizing",
                            "ready",
                            "activating",
                            "active",
                        }
                        or request_id is not None
                        and (
                            type(request_id) is not str
                            or len(request_id) != 64
                            or any(
                                character not in "0123456789abcdef"
                                for character in request_id
                            )
                        )
                        or connection_revision is not None
                        and (
                            type(connection_revision) is not str
                            or len(connection_revision) != 64
                            or any(
                                character not in "0123456789abcdef"
                                for character in connection_revision
                            )
                        )
                    ):
                        raise ValueError
                    if activation is not None:
                        parsed_activation = PluginGrantActivation.from_dict(activation)
                        if (
                            parsed_activation.plugin_id != operation["plugin_id"]
                            or parsed_activation.app_id != app_id
                            or parsed_activation.attempt_state_sha256 != request_id
                        ):
                            raise ValueError
                        activation = parsed_activation.to_dict()
                        activation_settings = PluginLifecycleService._candidate_settings_document(
                            PluginLifecycleService._activation_settings_document(parsed_activation)
                        )
                        if settings is not None and settings != activation_settings:
                            raise ValueError
                        settings = activation_settings
                    if (
                        state == "setup_required"
                        and any(
                            value is not None
                            for value in (request_id, connection_revision, activation)
                        )
                        or state == "authorizing"
                        and (request_id is None or connection_revision is not None or activation is not None)
                        or state == "ready"
                        and (connection_revision is None or (request_id is None) != (activation is None))
                        or state in {"activating", "active"}
                        and (connection_revision is None or (request_id is None) != (activation is None))
                    ):
                        raise ValueError
                    app_ids.append(app_id)
                    normalized_apps.append(
                        {
                            "app_id": app_id,
                            "state": state,
                            "request_id": request_id,
                            "connection_revision": connection_revision,
                            "activation": activation,
                            "settings": settings,
                        }
                    )
                if app_ids != sorted(app_ids) or len(app_ids) != len(set(app_ids)):
                    raise ValueError
                operation = {**operation, "candidate_apps": normalized_apps}
            if recovery_fields.intersection(operation):
                if not recovery_fields.issubset(operation) or candidate_apps is None:
                    raise ValueError
                binding = PluginAppBinding.from_dict(operation["candidate_binding"])
                if binding.dependency_receipt_id != receipt_id:
                    raise ValueError
                identity = operation["candidate_identity"]
                if type(identity) is not dict or set(identity) != {
                    "plugin_id",
                    "target_version",
                    "target_signing_key_id",
                    "target_manifest_sha256",
                    "target_package_sha256",
                    "candidate_binding_revision",
                }:
                    raise ValueError
                expected_identity = {
                    "plugin_id": operation["plugin_id"],
                    "target_version": operation["target_version"],
                    "target_signing_key_id": operation["target_signing_key_id"],
                    "target_manifest_sha256": operation["target_manifest_sha256"],
                    "target_package_sha256": operation["target_package_sha256"],
                    "candidate_binding_revision": binding.revision,
                }
                if identity != expected_identity:
                    raise ValueError
                candidate_ids = tuple(
                    str(item["app_id"]) for item in normalized_apps
                )
                previous_optional = (
                    ()
                    if base_record.app_binding is None
                    else base_record.app_binding.optional_app_ids
                )
                allowed_candidate_ids = set(binding.required_app_ids) | (
                    set(previous_optional) & set(binding.optional_app_ids)
                )
                if (
                    not set(binding.required_app_ids).issubset(candidate_ids)
                    or not set(candidate_ids).issubset(allowed_candidate_ids)
                ):
                    raise ValueError
                connection_recovery = operation["connection_recovery"]
                if connection_recovery is not None:
                    checked_recovery = PluginUpdateConnectionRecovery.from_dict(
                        connection_recovery
                    )
                    predecessor_apps = (
                        ()
                        if base_record.app_binding is None
                        else tuple(
                            sorted(
                                base_record.app_binding.required_app_ids
                                + base_record.app_binding.optional_app_ids
                            )
                        )
                    )
                    candidate_recovery_apps = tuple(
                        sorted(binding.required_app_ids + binding.optional_app_ids)
                    )
                    if (
                        checked_recovery.plugin_id != operation["plugin_id"]
                        or checked_recovery.predecessor_app_ids != predecessor_apps
                        or checked_recovery.candidate_app_ids
                        != candidate_recovery_apps
                    ):
                        raise ValueError
                    connection_recovery = checked_recovery.to_dict()
                elif (
                    base_record.app_binding is not None
                    and (
                        base_record.app_binding.required_app_ids
                        or base_record.app_binding.optional_app_ids
                    )
                ) or binding.required_app_ids or binding.optional_app_ids:
                    raise ValueError
                recovery_material = {
                    "plugin_id": operation["plugin_id"],
                    "base_record": base_record.to_dict(),
                    "dependency_receipt_id": receipt_id,
                    "phase": operation["phase"],
                    "candidate_apps": normalized_apps,
                    "candidate_binding": binding.to_dict(),
                    "candidate_identity": expected_identity,
                    "connection_recovery": connection_recovery,
                }
                if "automatic" in operation:
                    recovery_material["automatic"] = operation["automatic"]
                if "lifecycle_operation_id" in operation:
                    recovery_material["lifecycle_operation_id"] = operation[
                        "lifecycle_operation_id"
                    ]
                expected_recovery = sha256_hex(
                    canonical_bytes(
                        {
                            "domain": "omega.plugin-update-recovery.v1",
                            "material": recovery_material,
                        }
                    )
                )
                if operation["recovery_sha256"] != expected_recovery:
                    raise ValueError
                operation = {
                    **operation,
                    "candidate_binding": binding.to_dict(),
                    "candidate_identity": expected_identity,
                    "connection_recovery": connection_recovery,
                    "recovery_sha256": expected_recovery,
                }
            return {
                "schema_version": schema_version,
                "operation": {
                    **operation,
                    "base_record": base_record.to_dict(),
                },
            }
        except (KeyError, TypeError, UnicodeError, ValueError, json.JSONDecodeError):
            raise ValueError("plugin_update_recovery_required") from None

    def _read_update_document(self) -> dict[str, object]:
        with self._update_state.locked():
            return self._decode_update_document(self._update_state.read_bytes())

    def _write_update_document(self, document: dict[str, object]) -> None:
        validated = self._decode_update_document(canonical_bytes(document))
        operation = validated["operation"]
        if type(operation) is dict:
            self._lifecycle_operation_for_subordinate(operation, kind="update")
        with self._update_state.locked():
            self._update_state.write_bytes(canonical_bytes(validated))

    def _candidate_binding(
        self,
        verified: VerifiedPlugin,
        dependency_receipt_id: str,
    ) -> PluginAppBinding:
        required = tuple(sorted(item.app_id for item in verified.manifest.required_apps))
        installed_optional: list[str] = []
        for requirement in verified.manifest.optional_apps:
            try:
                self.app_registry.get(requirement.app_id)
            except McpAppNotInstalled:
                continue
            installed_optional.append(requirement.app_id)
        optional = tuple(sorted(installed_optional))
        material = {
            "required_app_ids": list(required),
            "optional_app_ids": list(optional),
            "dependency_receipt_id": dependency_receipt_id,
        }
        return PluginAppBinding(
            required_app_ids=required,
            optional_app_ids=optional,
            dependency_receipt_id=dependency_receipt_id,
            revision=sha256_hex(canonical_bytes(material)),
        )

    @staticmethod
    def _seal_update_recovery(
        operation: dict[str, object],
        binding: PluginAppBinding,
        connection_recovery: PluginUpdateConnectionRecovery | None,
    ) -> None:
        """Bind rollback to locally durable candidate and predecessor identities."""

        identity = {
            "plugin_id": operation["plugin_id"],
            "target_version": operation["target_version"],
            "target_signing_key_id": operation["target_signing_key_id"],
            "target_manifest_sha256": operation["target_manifest_sha256"],
            "target_package_sha256": operation["target_package_sha256"],
            "candidate_binding_revision": binding.revision,
        }
        recovery_document = (
            None if connection_recovery is None else connection_recovery.to_dict()
        )
        material = {
            "plugin_id": operation["plugin_id"],
            "base_record": operation["base_record"],
            "dependency_receipt_id": operation["dependency_receipt_id"],
            "phase": operation["phase"],
            "candidate_apps": operation["candidate_apps"],
            "candidate_binding": binding.to_dict(),
            "candidate_identity": identity,
            "connection_recovery": recovery_document,
        }
        if "automatic" in operation:
            material["automatic"] = operation["automatic"]
        if "lifecycle_operation_id" in operation:
            material["lifecycle_operation_id"] = operation[
                "lifecycle_operation_id"
            ]
        operation.update(
            {
                "candidate_binding": binding.to_dict(),
                "candidate_identity": identity,
                "connection_recovery": recovery_document,
                "recovery_sha256": sha256_hex(
                    canonical_bytes(
                        {
                            "domain": "omega.plugin-update-recovery.v1",
                            "material": material,
                        }
                    )
                ),
            }
        )

    def _pending_update(
        self,
        plugin_id: str | None = None,
    ) -> tuple[dict[str, object], VerifiedPlugin, PluginAppBinding] | None:
        if not hasattr(self, "_update_state"):
            return None
        operation = self._read_update_document()["operation"]
        if operation is None:
            return None
        assert type(operation) is dict
        self._lifecycle_operation_for_subordinate(operation, kind="update")
        if plugin_id is not None and operation["plugin_id"] != plugin_id:
            return None
        if "candidate_apps" not in operation or operation["phase"] not in {
            "candidate",
            "promoted",
        }:
            return None
        if "candidate_binding" in operation:
            binding = PluginAppBinding.from_dict(operation["candidate_binding"])
            expected_candidate = InstalledPluginVersionRecord(
                plugin_version=str(operation["target_version"]),
                signing_key_id=str(operation["target_signing_key_id"]),
                manifest_sha256=str(operation["target_manifest_sha256"]),
                package_sha256=str(operation["target_package_sha256"]),
                state="candidate",
            )
            try:
                verified = self.plugin_registry.reverify_candidate(
                    str(operation["plugin_id"]),
                    expected_candidate,
                )
            except (PluginRegistryError, ValueError):
                raise ValueError("plugin_update_recovery_required") from None
        else:
            _catalog, _entry, verified = self._verified(str(operation["plugin_id"]))
            binding = self._candidate_binding(
                verified,
                str(operation["dependency_receipt_id"]),
            )
        if (
            verified.manifest.plugin_version != operation["target_version"]
            or verified.manifest.signing_key_id != operation["target_signing_key_id"]
            or verified.manifest_sha256 != operation["target_manifest_sha256"]
            or verified.package_sha256 != operation["target_package_sha256"]
        ):
            raise ValueError("plugin_update_recovery_required")
        candidate_apps = operation["candidate_apps"]
        assert type(candidate_apps) is list
        candidate_ids = tuple(str(item["app_id"]) for item in candidate_apps)
        base_record = InstalledPluginRecord.from_dict(
            operation["base_record"],
            registry_key=str(operation["plugin_id"]),
        )
        previous_optional = (
            ()
            if base_record.app_binding is None
            else base_record.app_binding.optional_app_ids
        )
        allowed_candidate_ids = set(binding.required_app_ids) | (
            set(previous_optional) & set(binding.optional_app_ids)
        )
        if (
            not set(binding.required_app_ids).issubset(candidate_ids)
            or not set(candidate_ids).issubset(allowed_candidate_ids)
        ):
            raise ValueError("plugin_update_recovery_required")
        return operation, verified, binding

    @staticmethod
    def _next_candidate_app(operation: dict[str, object]) -> str | None:
        candidate_apps = operation.get("candidate_apps")
        if type(candidate_apps) is not list:
            raise ValueError("plugin_update_recovery_required")
        return next(
            (
                str(item["app_id"])
                for item in candidate_apps
                if item["state"] == "setup_required"
            ),
            None,
        )

    def _discard_candidate_attempts(
        self,
        operation: dict[str, object],
        verified: VerifiedPlugin | None,
        binding: PluginAppBinding,
    ) -> None:
        candidate_apps = operation.get("candidate_apps")
        if type(candidate_apps) is not list or not hasattr(self, "_connection_service"):
            return
        for item in candidate_apps:
            request_id = item.get("request_id")
            if type(request_id) is not str:
                continue
            try:
                if "candidate_identity" in operation:
                    self.connection_service.cancel_candidate_exact(
                        request_id,
                        candidate_identity=operation["candidate_identity"],
                    )
                else:
                    assert verified is not None
                    self.connection_service.cancel_candidate(
                        request_id,
                        candidate_verified=verified,
                        candidate_binding=binding,
                    )
            except ValueError as exc:
                if str(exc) not in {
                    "oauth_transaction_missing",
                    "oauth_transaction_expired",
                    "oauth_callback_invalid",
                    "oauth_callback_replayed",
                }:
                    raise
        if "candidate_identity" in operation:
            cancel_all = getattr(
                self.connection_service,
                "cancel_candidate_attempts_exact",
                None,
            )
            if callable(cancel_all):
                cancel_all(operation["candidate_identity"])

    def _candidate_record(
        self,
        verified: VerifiedPlugin,
        binding: PluginAppBinding,
        app_id: str,
    ) -> InstalledMcpAppRecord:
        record = self._installer.provisioner.candidate_record(
            binding.dependency_receipt_id,
            app_id,
        )
        requirements = verified.manifest.required_apps + verified.manifest.optional_apps
        matches = tuple(item for item in requirements if item.app_id == app_id)
        if len(matches) != 1 or record.app_id != app_id:
            raise ValueError("plugin_update_failed")
        return record

    def _candidate_descriptor(
        self,
        verified: VerifiedPlugin,
        binding: PluginAppBinding,
        app_id: str,
    ) -> Any:
        record = self._candidate_record(verified, binding, app_id)
        authority = signed_capability_authority_for_candidate(
            self.package_registry,
            record,
            verified,
        )
        if authority is None:
            raise ValueError("plugin_catalog_authority_missing")
        descriptor = descriptor_for_record(
            record,
            signed_capability_authority=authority,
        )
        return self._bind_local_launch_descriptor(record, descriptor)

    def _candidate_update_app_ids(
        self,
        plugin_id: str,
        base: InstalledPluginRecord,
        binding: PluginAppBinding,
    ) -> tuple[str, ...]:
        """Return required apps plus retained optional apps with live authority.

        Every selected-version grant is package-bound.  A retained optional app
        that is still executable therefore needs a candidate connection even
        when its endpoint happens to be unchanged; otherwise its v1 grant could
        be misrepresented as v2 package authority after promotion.
        """

        current_binding = base.app_binding
        selected = set(binding.required_app_ids)
        if current_binding is None:
            return tuple(sorted(selected))
        retained_optional = set(current_binding.optional_app_ids) & set(
            binding.optional_app_ids
        )
        if not retained_optional:
            return tuple(sorted(selected))
        account_hash = sha256(
            self.operator_account_key().encode("utf-8")
        ).hexdigest()
        for app_id in sorted(retained_optional):
            descriptor = self.descriptor_for_plugin_app(plugin_id, app_id)
            # Live means the authority the runtime catalog serves -- an OAuth grant
            # or a credentialless connection authority -- as the enable gate uses.
            if (
                self.planning_app_authority(
                    plugin_id,
                    app_id,
                    account_hash,
                    descriptor,
                )
                is not None
            ):
                selected.add(app_id)
        return tuple(sorted(selected))

    def _assert_update_candidate_ready(
        self,
        plugin_id: str,
        verified: VerifiedPlugin,
        binding: PluginAppBinding,
    ) -> None:
        snapshot = self.plugin_registry.snapshot()
        current = next(
            (record for record in snapshot.records if record.plugin_id == plugin_id),
            None,
        )
        if current is None:
            raise ValueError("plugin_not_installed")
        target_version = verified.manifest.plugin_version
        candidate = next(
            (item for item in current.versions if item.plugin_version == target_version),
            None,
        )
        if candidate is None or candidate.state != "candidate":
            raise ValueError("plugin_update_failed")
        account_key = self.operator_account_key() if binding.required_app_ids else ""

        async def probe_required() -> tuple[object, ...]:
            probes = []
            for app_id in binding.required_app_ids:
                status = await self._selected_app_status(plugin_id, app_id)
                retained = self._candidate_settings_from_status(
                    plugin_id,
                    app_id,
                    candidate_verified=verified,
                    candidate_binding=binding,
                    status=status,
                )
                if retained is None:
                    raise ValueError("plugin_authorization_required")
                validated, _settings_document = retained
                probes.append(
                    await self.connection_service.probe_candidate(
                        plugin_id,
                        app_id=app_id,
                        candidate_verified=verified,
                        candidate_binding=binding,
                        account_key=account_key,
                        expected_registry_revision=snapshot.revision,
                        **validated,
                    )
                )
            return tuple(probes)

        probes = self._submit_connection_operation(probe_required)
        if any(getattr(probe, "status", None) != "connected" for probe in probes):
            raise ValueError("plugin_authorization_required")
        candidate_revisions = {
            app_id: str(getattr(probe, "connection_revision", ""))
            for app_id, probe in zip(binding.required_app_ids, probes, strict=True)
        }
        if any(
            len(revision) != 64
            or any(character not in "0123456789abcdef" for character in revision)
            for revision in candidate_revisions.values()
        ):
            raise ValueError("plugin_connection_state_invalid")
        self._assert_candidate_bindings(
            plugin_id,
            verified,
            binding,
            candidate_revisions,
            snapshot=snapshot,
        )

    def _assert_candidate_bindings(
        self,
        plugin_id: str,
        verified: VerifiedPlugin,
        binding: PluginAppBinding,
        candidate_revisions: dict[str, str],
        *,
        snapshot: PluginRegistrySnapshot | None = None,
    ) -> None:
        from mcp_platform.plugin_catalog import compile_plugin_runtime_catalog

        if snapshot is None:
            snapshot = self.plugin_registry.snapshot()
        current = next(
            (record for record in snapshot.records if record.plugin_id == plugin_id),
            None,
        )
        if current is None:
            raise ValueError("plugin_not_installed")
        target_version = verified.manifest.plugin_version
        synthetic = replace(
            current,
            selected_version=target_version,
            enabled=True,
            blocker=None,
            app_binding=binding,
            versions=tuple(
                replace(
                    item,
                    state="selected" if item.plugin_version == target_version else "candidate",
                )
                for item in current.versions
            ),
        )
        records = {
            record.plugin_id: synthetic if record.plugin_id == plugin_id else record
            for record in snapshot.records
        }
        material = {
            "schema_version": 2,
            "plugins": {key: records[key].to_dict() for key in sorted(records)},
        }
        candidate_snapshot = PluginRegistrySnapshot(
            records=tuple(records[key] for key in sorted(records)),
            revision=sha256_hex(canonical_bytes(material)),
        )
        account_hash = (
            sha256(self.operator_account_key().encode("utf-8")).hexdigest()
            if binding.required_app_ids or binding.optional_app_ids
            else sha256(b"").hexdigest()
        )
        app_snapshot = self.app_registry.snapshot()
        candidate_app_records = {
            record.app_id: record for record in app_snapshot.records
        }
        for app_id in binding.required_app_ids + binding.optional_app_ids:
            candidate_app_records[app_id] = self._candidate_record(
                verified,
                binding,
                app_id,
            )
        candidate_app_snapshot = type(app_snapshot)(
            records=tuple(candidate_app_records[key] for key in sorted(candidate_app_records)),
            revision=sha256_hex(
                canonical_bytes(
                    {
                        "schema_version": 1,
                        "apps": {
                            key: candidate_app_records[key].to_dict()
                            for key in sorted(candidate_app_records)
                        },
                    }
                )
            ),
        )
        catalog = compile_plugin_runtime_catalog(
            plugin_snapshot=candidate_snapshot,
            app_snapshot=candidate_app_snapshot,
            descriptor_for_app=lambda _plugin_id, app_id: self._candidate_descriptor(
                verified,
                binding,
                app_id,
            ),
            account_key_hash=account_hash,
            connection_revision_provider=lambda candidate_plugin_id, app_id, _account, _descriptor: (
                candidate_revisions.get(app_id)
                if candidate_plugin_id == plugin_id
                else None
            ),
            verified_plugin_provider=lambda candidate_plugin_id: (
                verified
                if candidate_plugin_id == plugin_id
                else self._installed_verified(candidate_plugin_id)
            ),
        )
        has_remote_apps = bool(binding.required_app_ids + binding.optional_app_ids)
        remote_kinds = {"tool", "resource", "resource_template", "prompt"}
        if not any(
            item.plugin_id == plugin_id
            and (not has_remote_apps or item.capability_kind in remote_kinds)
            for item in catalog.entries
        ):
            raise ValueError("plugin_catalog_authority_missing")

    def _clear_update_document(self, expected_operation: dict[str, object]) -> None:
        expected = self._decode_update_document(
            canonical_bytes(self._update_document(expected_operation))
        )
        operation = expected["operation"]
        if type(operation) is not dict:
            raise ValueError("plugin_lifecycle_operation_conflict")
        owner = self._lifecycle_operation_for_subordinate(operation, kind="update")
        with self._update_state.locked():
            current = self._decode_update_document(self._update_state.read_bytes())
            if current != expected:
                raise ValueError("plugin_lifecycle_operation_conflict")
            self._update_state.write_bytes(canonical_bytes(self._empty_update_document()))
        if owner is not None:
            self._release_lifecycle_operation(owner)

    def _run_update_recovery_operation(
        self,
        operation: dict[str, object],
        method_name: str,
    ) -> None:
        recovery = self._connection_update_recovery(operation)
        if recovery is None:
            return
        service = getattr(self, "_connection_service", None)
        method = getattr(service, method_name, None)
        if not callable(method):
            raise ValueError("plugin_update_recovery_required")
        self._submit_connection_operation(lambda: method(recovery))

    def _reconcile_sealed_update_operation(
        self,
        operation: dict[str, object],
    ) -> None:
        binding = PluginAppBinding.from_dict(operation["candidate_binding"])
        base = InstalledPluginRecord.from_dict(
            operation["base_record"],
            registry_key=str(operation["plugin_id"]),
        )
        candidate = InstalledPluginVersionRecord(
            plugin_version=str(operation["target_version"]),
            signing_key_id=str(operation["target_signing_key_id"]),
            manifest_sha256=str(operation["target_manifest_sha256"]),
            package_sha256=str(operation["target_package_sha256"]),
            state="candidate",
        )
        self._discard_candidate_attempts(operation, None, binding)
        if operation["phase"] == "rolled_back":
            current = self.plugin_registry.get(str(operation["plugin_id"]))
            target = next(
                (
                    item
                    for item in current.versions
                    if item.plugin_version == candidate.plugin_version
                ),
                None,
            )
            if current != base or target is not None:
                raise ValueError("plugin_update_recovery_required")
            self._run_update_recovery_operation(
                operation,
                "finalize_update_recovery",
            )
            self._clear_update_document(operation)
            return
        if operation["phase"] in {"committed", "retirement_complete"}:
            current = self.plugin_registry.get(str(operation["plugin_id"]))
            selected = next(
                (
                    item
                    for item in current.versions
                    if item.plugin_version == candidate.plugin_version
                ),
                None,
            )
            if (
                current.selected_version != candidate.plugin_version
                or selected != replace(candidate, state="selected")
                or current.app_binding != binding
            ):
                raise ValueError("plugin_update_recovery_required")
            if operation["phase"] == "committed":
                try:
                    self._submit_connection_operation(
                        lambda: self._retire_update_predecessors(
                            operation,
                            binding,
                            expected_registry_revision=self.plugin_registry.snapshot().revision,
                        )
                    )
                except BaseException as exc:
                    if self._retryable_update_retirement_error(exc):
                        return
                    raise
                operation["phase"] = "retirement_complete"
                self._write_update_document(self._update_document(operation))
            self._run_update_recovery_operation(
                operation,
                "finalize_update_recovery",
            )
            self._clear_update_document(operation)
            return

        self._run_update_recovery_operation(operation, "restore_update_recovery")
        current = self.plugin_registry.get(str(operation["plugin_id"]))
        target = next(
            (
                item
                for item in current.versions
                if item.plugin_version == candidate.plugin_version
            ),
            None,
        )
        if current.selected_version == candidate.plugin_version:
            self.plugin_registry.rollback_update_candidate(
                expected_base=base,
                expected_candidate=candidate,
                candidate_binding=binding,
                expected_revision=self.plugin_registry.snapshot().revision,
            )
        elif current.selected_version == base.selected_version:
            if target is not None:
                if target != candidate:
                    raise ValueError("plugin_update_recovery_required")
                mutation = self.plugin_registry.discard_candidate(
                    str(operation["plugin_id"]),
                    candidate.plugin_version,
                    expected_revision=self.plugin_registry.snapshot().revision,
                    expected_candidate=candidate,
                )
                current = mutation.record
            if current != base:
                raise ValueError("plugin_update_recovery_required")
        else:
            raise ValueError("plugin_update_recovery_required")
        self._installer.provisioner.rollback(str(operation["dependency_receipt_id"]))
        operation["phase"] = "rolled_back"
        self._write_update_document(self._update_document(operation))
        self._run_update_recovery_operation(operation, "finalize_update_recovery")
        self._clear_update_document(operation)

    def _reconcile_update_intent(self) -> None:
        document = self._read_update_document()
        operation = document["operation"]
        if operation is None:
            return
        assert type(operation) is dict
        try:
            self._lifecycle_operation_for_subordinate(operation, kind="update")
            self._installer.recover()
            if "candidate_identity" in operation:
                self._reconcile_sealed_update_operation(operation)
                return
            self._discard_unsealed_update_checkpoint(operation)
            if "candidate_apps" in operation:
                if "candidate_binding" in operation:
                    verified = None
                    binding = PluginAppBinding.from_dict(
                        operation["candidate_binding"]
                    )
                else:
                    _catalog, _entry, verified = self._verified(
                        str(operation["plugin_id"])
                    )
                    binding = self._candidate_binding(
                        verified,
                        str(operation["dependency_receipt_id"]),
                    )
                self._discard_candidate_attempts(operation, verified, binding)
            current = self.plugin_registry.get(operation["plugin_id"])
            base = InstalledPluginRecord.from_dict(
                operation["base_record"],
                registry_key=operation["plugin_id"],
            )
            target = next(
                (
                    item
                    for item in current.versions
                    if item.plugin_version == operation["target_version"]
                ),
                None,
            )
            expected_candidate = InstalledPluginVersionRecord(
                plugin_version=operation["target_version"],
                signing_key_id=operation["target_signing_key_id"],
                manifest_sha256=operation["target_manifest_sha256"],
                package_sha256=operation["target_package_sha256"],
                state="candidate",
            )
            if current.selected_version == operation["target_version"]:
                if (
                    target is None
                    or target.manifest_sha256 != operation["target_manifest_sha256"]
                    or target.package_sha256 != operation["target_package_sha256"]
                    or target.signing_key_id != operation["target_signing_key_id"]
                ):
                    raise ValueError
                if "candidate_apps" in operation:
                    restored = self.plugin_registry.promote_candidate(
                        operation["plugin_id"],
                        base.selected_version,
                        expected_revision=self.plugin_registry.snapshot().revision,
                        app_binding=base.app_binding,
                    )
                    current = restored.record
                    target = next(
                        (
                            item
                            for item in current.versions
                            if item.plugin_version == operation["target_version"]
                        ),
                        None,
                    )
                    if target != expected_candidate:
                        raise ValueError
                    discarded = self.plugin_registry.discard_candidate(
                        operation["plugin_id"],
                        operation["target_version"],
                        expected_revision=restored.revision,
                        expected_candidate=expected_candidate,
                    )
                    if discarded.record != base:
                        raise ValueError
                    self._installer.provisioner.rollback(
                        operation["dependency_receipt_id"]
                    )
                    self._clear_update_document(operation)
                    return
                self._clear_update_document(operation)
                return
            candidate_was_visible = target is not None
            if candidate_was_visible or operation["phase"] == "candidate":
                if (
                    target is not None
                    and target != expected_candidate
                ):
                    raise ValueError
                mutation = self.plugin_registry.discard_candidate(
                    operation["plugin_id"],
                    operation["target_version"],
                    expected_revision=self.plugin_registry.snapshot().revision,
                    expected_candidate=expected_candidate,
                )
                current = mutation.record
            if current != base:
                raise ValueError
            if candidate_was_visible or operation["phase"] == "candidate":
                self._installer.provisioner.rollback(operation["dependency_receipt_id"])
            self._clear_update_document(operation)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            # One word for every failure in this body, and `from None` drops the
            # cause -- which boot then reports as `hud.app_import failed`, taking
            # the whole operator surface down while naming nothing. The chain
            # stays suppressed (this reason is a closed contract callers match
            # on); the cause is logged instead, in Omega's own vocabulary.
            _LOG.warning(
                "plugin update reconciliation refused: %s raised at %s",
                type(exc).__name__,
                _raising_site(exc),
            )
            raise ValueError("plugin_update_recovery_required") from None

    def update(
        self,
        plugin_id: str,
        version: str,
        confirmation: object,
        *,
        automatic: bool = False,
    ) -> PluginLifecycleSnapshot | PluginUpdatePending:
        """Atomically promote one exact, previously reviewed catalog version.

        ``version`` and ``confirmation`` must be the exact pair
        :meth:`update_review` issued: a stale review (the catalog moved since
        the user looked at it) or a digest that does not match fails closed
        rather than installing a version the user never actually saw. The old
        version stays selected and enabled until every required app of the
        candidate is durably installed and its authority compiles; a failed
        activation discards the candidate and preserves the old current
        version untouched -- atomic version promotion, never a runtime
        fallback path.
        """

        with self._authority_mutation_lock:
            accepted = self._admit_lifecycle_operation(
                kind="update",
                plugin_id=plugin_id,
            )
            try:
                if automatic:
                    result = self._update_locked(
                        plugin_id,
                        version=version,
                        confirmation=confirmation,
                        automatic=True,
                    )
                else:
                    result = self._update_locked(
                        plugin_id,
                        version=version,
                        confirmation=confirmation,
                    )
            except BaseException:
                if self._read_update_document()["operation"] is None:
                    self._release_lifecycle_operation_if_current(accepted)
                raise
            if isinstance(result, PluginUpdatePending):
                return result
            self._release_lifecycle_operation_if_current(accepted)
        # A completed cutover has already released this update's lifecycle operation
        # (_clear_update_document), so the status read runs after the mutation lock:
        # handed off beneath an operation no longer owned, it would be refused and a
        # completed update reported as failed.
        return self._submit_connection_operation(lambda: self.status(plugin_id))

    def _update_locked(
        self,
        plugin_id: str,
        *,
        version: str,
        confirmation: object,
        automatic: bool = False,
    ) -> PluginInstallResult | PluginUpdatePending:
        catalog, entry, verified = self._verified(plugin_id)
        if type(version) is not str or verified.manifest.plugin_version != version:
            raise ValueError("plugin_install_review_changed")
        base = self.plugin_registry.get(plugin_id)
        if base.selected_version == verified.manifest.plugin_version:
            return self._install_locked(plugin_id, confirmation=confirmation)
        target = next(
            (
                item
                for item in base.versions
                if item.plugin_version == verified.manifest.plugin_version
            ),
            None,
        )
        if target is not None:
            raise ValueError("plugin_update_recovery_required")
        transaction_id = uuid4().hex
        receipt_id = f"mcp-receipt-{transaction_id}"
        lifecycle_operation = self._require_owned_lifecycle_operation(
            kind="update",
            plugin_id=plugin_id,
        )
        operation = {
            "plugin_id": plugin_id,
            "target_version": verified.manifest.plugin_version,
            "target_signing_key_id": verified.manifest.signing_key_id,
            "target_manifest_sha256": verified.manifest_sha256,
            "target_package_sha256": verified.package_sha256,
            "base_record": base.to_dict(),
            "dependency_receipt_id": receipt_id,
            "phase": "installing",
            "lifecycle_operation_id": lifecycle_operation["operation_id"],
        }
        self._write_update_document(self._update_document(operation))
        try:
            result = self._installer.install_from_catalog(
                catalog,
                entry,
                verified,
                confirmation,
                preserve_existing_binding=True,
                transaction_id=transaction_id,
            )
            binding = self._candidate_binding(
                verified,
                result.dependency_receipt.receipt_id,
            )
            operation["dependency_receipt_id"] = result.dependency_receipt.receipt_id
            operation["phase"] = "candidate"
            requirements = {
                requirement.app_id: requirement
                for requirement in (
                    verified.manifest.required_apps
                    + verified.manifest.optional_apps
                )
            }
            candidate_app_ids = self._candidate_update_app_ids(
                plugin_id,
                base,
                binding,
            )
            snapshot = self.plugin_registry.snapshot()

            candidate_apps: list[dict[str, object]] = []
            for app_id in candidate_app_ids:
                requirement = requirements[app_id]
                if requirement.authentication.kind.value == "none":
                    current_status = self._submit_connection_operation(
                        lambda app_id=app_id: self._selected_app_status(plugin_id, app_id)
                    )
                    retained = (
                        None
                        if current_status is None
                        else self._candidate_settings_from_status(
                            plugin_id,
                            app_id,
                            candidate_verified=verified,
                            candidate_binding=binding,
                            status=current_status,
                        )
                    )
                    if retained is None:
                        candidate_apps.append(
                            {
                                "app_id": app_id,
                                "state": "setup_required",
                                "request_id": None,
                                "connection_revision": None,
                                "activation": None,
                                "settings": None,
                            }
                        )
                        continue
                    validated, settings_document = retained
                    proof = self._submit_connection_operation(
                        lambda app_id=app_id, validated=validated: (
                            self.connection_service.probe_candidate(
                                plugin_id,
                                app_id=app_id,
                                candidate_verified=verified,
                                candidate_binding=binding,
                                account_key=self.operator_account_key(),
                                expected_registry_revision=snapshot.revision,
                                **validated,
                            )
                        )
                    )
                    revision = str(getattr(proof, "connection_revision", ""))
                    if len(revision) != 64 or any(
                        character not in "0123456789abcdef" for character in revision
                    ):
                        raise ValueError("plugin_connection_state_invalid")
                    candidate_apps.append(
                        {
                            "app_id": app_id,
                            "state": "ready",
                            "request_id": None,
                            "connection_revision": revision,
                            "activation": None,
                            "settings": settings_document,
                        }
                    )
                else:
                    retained_activation = None
                    if automatic and self.plugin_grants is not None:
                        account_hash = sha256(
                            self.operator_account_key().encode("utf-8")
                        ).hexdigest()
                        current_grant = self.plugin_grants.current(
                            plugin_id,
                            app_id=app_id,
                            account_key_sha256=account_hash,
                        )
                        live_check = getattr(
                            self.plugin_grants, "_broker_receipt_is_live", None
                        )
                        if current_grant is not None and callable(live_check):
                            try:
                                if live_check(current_grant):
                                    retained_activation = current_grant.activation
                            except ValueError:
                                retained_activation = None
                    if retained_activation is not None:
                        candidate_apps.append(
                            {
                                "app_id": app_id,
                                "state": "ready",
                                "request_id": retained_activation.attempt_state_sha256,
                                "connection_revision": retained_activation.connection_revision,
                                "activation": retained_activation.to_dict(),
                                "settings": self._activation_settings_document(
                                    retained_activation
                                ),
                            }
                        )
                        continue
                    candidate_apps.append(
                        {
                            "app_id": app_id,
                            "state": "setup_required",
                            "request_id": None,
                            "connection_revision": None,
                            "activation": None,
                            "settings": None,
                        }
                    )
            operation["candidate_apps"] = candidate_apps
            # The automatic marker is only written once the update journal has
            # its complete candidate shape; the initial installing record is
            # intentionally valid under the legacy journal schema.
            if automatic:
                operation["automatic"] = True
            connection_recovery = None
            connection_service = getattr(self, "_connection_service", None)
            begin_recovery = getattr(
                connection_service,
                "begin_update_recovery",
                None,
            )
            if callable(begin_recovery):
                connection_recovery = begin_recovery(
                    previous_record=base,
                    candidate_binding=binding,
                    account_key=self.operator_account_key(),
                    checkpoint_id=self._update_checkpoint_id(plugin_id),
                )
                if type(connection_recovery) is not PluginUpdateConnectionRecovery:
                    raise ValueError("plugin_update_recovery_required")
            self._seal_update_recovery(
                operation,
                binding,
                connection_recovery,
            )
            self._write_update_document(self._update_document(operation))
            setup = tuple(
                item["app_id"]
                for item in candidate_apps
                if item["state"] == "setup_required"
            )
            if setup:
                return PluginUpdatePending(
                    plugin_id=plugin_id,
                    app_ids=candidate_app_ids,
                    next_required_app_id=str(setup[0]),
                )
            promoted = self._submit_connection_operation(
                lambda: self._promote_update_candidate(
                    operation,
                    verified,
                    binding,
                )
            )
            return replace(result, record=promoted.record, revision=promoted.revision)
        except BaseException as active:
            pending_after_commit = self._read_update_document()["operation"]
            if (
                str(active) == "plugin_revocation_pending"
                and type(pending_after_commit) is dict
                and pending_after_commit["phase"] == "committed"
            ):
                raise
            try:
                self._reconcile_update_intent()
            except ValueError:
                if not isinstance(active, Exception):
                    raise active
                raise ValueError("plugin_update_recovery_required") from None
            if not isinstance(active, Exception):
                raise active
            raise ValueError("plugin_update_failed") from None

    def runtime_catalog(self) -> Any:
        from mcp_platform.plugin_dispatch import plugin_runtime_catalog

        return plugin_runtime_catalog(service=self)

    def add_personal_remote(self, source_url: str) -> dict[str, object]:
        """Reject personal sources until the verified personal resolver is composed."""

        del source_url
        raise ValueError("plugin_runtime_unsupported")

    def import_personal(self, upload_id: str) -> dict[str, object]:
        """Reject portable imports until the content-store resolver is composed."""

        del upload_id
        raise ValueError("plugin_runtime_unsupported")

    @staticmethod
    def _empty_uninstall_document() -> dict[str, object]:
        return {"schema_version": 1, "operation": None}

    @staticmethod
    def _decode_uninstall_document(payload: bytes | None) -> dict[str, object]:
        if payload is None:
            return PluginLifecycleService._empty_uninstall_document()

        def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
            value: dict[str, object] = {}
            for key, item in pairs:
                if key in value:
                    raise ValueError
                value[key] = item
            return value

        try:
            document = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=reject_duplicates,
            )
            if type(document) is not dict or set(document) != {
                "schema_version",
                "operation",
            } or document["schema_version"] != 1:
                raise ValueError
            operation = document["operation"]
            if operation is None:
                return PluginLifecycleService._empty_uninstall_document()
            operation_fields = {
                "plugin_id",
                "plugin_record",
                "app_records",
            }
            if type(operation) is not dict or frozenset(operation) not in {
                frozenset(operation_fields),
                frozenset((*operation_fields, "lifecycle_operation_id")),
            }:
                raise ValueError
            lifecycle_operation_id = operation.get("lifecycle_operation_id")
            if lifecycle_operation_id is not None:
                lifecycle_operation_id = _closed_revision(lifecycle_operation_id)
            plugin_id = operation["plugin_id"]
            plugin_record = InstalledPluginRecord.from_dict(
                operation["plugin_record"],
                registry_key=plugin_id,
            )
            raw_apps = operation["app_records"]
            if type(raw_apps) is not list:
                raise ValueError
            app_records = tuple(
                InstalledMcpAppRecord.from_dict(raw, registry_key=raw["app_id"])
                for raw in raw_apps
                if type(raw) is dict and type(raw.get("app_id")) is str
            )
            if (
                len(app_records) != len(raw_apps)
                or tuple(item.app_id for item in app_records)
                != tuple(sorted(item.app_id for item in app_records))
                or len({item.app_id for item in app_records}) != len(app_records)
            ):
                raise ValueError
            normalized_operation = {
                "plugin_id": plugin_record.plugin_id,
                "plugin_record": plugin_record.to_dict(),
                "app_records": [item.to_dict() for item in app_records],
            }
            if lifecycle_operation_id is not None:
                normalized_operation["lifecycle_operation_id"] = (
                    lifecycle_operation_id
                )
            normalized = {
                "schema_version": 1,
                "operation": normalized_operation,
            }
            if canonical_bytes(normalized) != canonical_bytes(document):
                raise ValueError
            return normalized
        except (KeyError, TypeError, UnicodeError, ValueError):
            raise ValueError("plugin_uninstall_incomplete") from None

    def _read_uninstall_document(self) -> dict[str, object]:
        with self._uninstall_state.locked():
            return self._decode_uninstall_document(self._uninstall_state.read_bytes())

    def _write_uninstall_document(self, document: dict[str, object]) -> None:
        validated = self._decode_uninstall_document(canonical_bytes(document))
        operation = validated["operation"]
        if type(operation) is dict:
            self._lifecycle_operation_for_subordinate(operation, kind="uninstall")
        with self._uninstall_state.locked():
            self._uninstall_state.write_bytes(canonical_bytes(validated))

    def _begin_uninstall_intent(
        self,
        plugin_record: InstalledPluginRecord,
        app_records: tuple[InstalledMcpAppRecord, ...],
        *,
        lifecycle_operation_id: str | None = None,
    ) -> dict[str, object]:
        with self._uninstall_state.locked():
            document = self._decode_uninstall_document(
                self._uninstall_state.read_bytes()
            )
            if document["operation"] is not None:
                raise ValueError("plugin_uninstall_incomplete")
            operation = {
                "plugin_id": plugin_record.plugin_id,
                "plugin_record": plugin_record.to_dict(),
                "app_records": [
                    item.to_dict()
                    for item in sorted(app_records, key=lambda item: item.app_id)
                ],
            }
            if lifecycle_operation_id is not None:
                operation["lifecycle_operation_id"] = _closed_revision(
                    lifecycle_operation_id
                )
            intent = {
                "schema_version": 1,
                "operation": operation,
            }
            self._uninstall_state.write_bytes(canonical_bytes(intent))
            return intent

    def _clear_uninstall_intent(self, expected: dict[str, object]) -> None:
        expected_operation = expected.get("operation")
        if type(expected_operation) is not dict:
            raise ValueError("plugin_uninstall_incomplete")
        owner = self._lifecycle_operation_for_subordinate(
            expected_operation,
            kind="uninstall",
        )
        with self._uninstall_state.locked():
            current = self._decode_uninstall_document(
                self._uninstall_state.read_bytes()
            )
            if current != expected:
                raise ValueError("plugin_uninstall_incomplete")
            self._uninstall_state.write_bytes(
                canonical_bytes(self._empty_uninstall_document())
            )
        if owner is not None:
            self._release_lifecycle_operation(owner)

    def _restore_uninstall_apps(self, intent: dict[str, object]) -> None:
        operation = intent["operation"]
        assert type(operation) is dict
        plugin_id = str(operation["plugin_id"])
        current = self.plugin_registry.get(plugin_id)
        if current.to_dict() != operation["plugin_record"]:
            raise ValueError("plugin_uninstall_incomplete")
        verified = self.plugin_registry.reverify_installed(plugin_id)
        if (
            verified.manifest.plugin_id != current.plugin_id
            or verified.manifest.plugin_version != current.selected_version
        ):
            raise ValueError("plugin_uninstall_incomplete")
        requirements = verified.manifest.required_apps + verified.manifest.optional_apps
        transaction_id = os.urandom(16).hex()
        prepared = self._installer.provisioner.prepare(transaction_id, requirements)
        self._installer.provisioner.commit(prepared)
        by_id = {record.app_id: record for record in self.app_registry.snapshot().records}
        for expected in operation["app_records"]:
            if by_id.get(expected["app_id"]) is None or by_id[expected["app_id"]].to_dict() != expected:
                raise ValueError("plugin_uninstall_incomplete")

    def _reconcile_uninstall_intent(self) -> None:
        intent = self._read_uninstall_document()
        operation = intent["operation"]
        if operation is None:
            return
        assert type(operation) is dict
        self._lifecycle_operation_for_subordinate(operation, kind="uninstall")
        plugin_id = str(operation["plugin_id"])
        try:
            current = self.plugin_registry.get(plugin_id)
        except PluginRegistryError as exc:
            if str(exc) != "plugin_not_installed":
                raise ValueError("plugin_uninstall_incomplete") from exc
            app_snapshot = self.app_registry.snapshot()
            by_id = {record.app_id: record for record in app_snapshot.records}
            removals: list[InstalledMcpAppRecord] = []
            owned_elsewhere = {
                app_id
                for record in self.plugin_registry.snapshot().records
                if record.app_binding is not None
                for app_id in (
                    record.app_binding.required_app_ids
                    + record.app_binding.optional_app_ids
                )
            }
            for expected in operation["app_records"]:
                app_id = expected["app_id"]
                if app_id in owned_elsewhere:
                    raise ValueError("plugin_uninstall_incomplete")
                installed = by_id.get(app_id)
                if installed is None:
                    continue
                if installed.to_dict() != expected:
                    raise ValueError("plugin_uninstall_incomplete")
                removals.append(installed)
            if removals:
                self.app_registry.apply_exact(
                    install_records=(),
                    remove_records=tuple(removals),
                    expected_revision=app_snapshot.revision,
                )
        else:
            if current.to_dict() != operation["plugin_record"]:
                raise ValueError("plugin_uninstall_incomplete")
            self._restore_uninstall_apps(intent)
        self._clear_uninstall_intent(intent)

    async def uninstall(self, plugin_id: str) -> None:
        async with self._authority_mutation_lock:
            accepted = self._admit_lifecycle_operation(
                kind="uninstall",
                plugin_id=plugin_id,
            )
            try:
                await self._uninstall_locked(plugin_id)
            except BaseException:
                if self._read_uninstall_document()["operation"] is None:
                    self._release_lifecycle_operation_if_current(accepted)
                raise
            self._release_lifecycle_operation_if_current(accepted)

    async def _uninstall_locked(self, plugin_id: str) -> None:
        """Disconnect and remove one signed plugin and its owned app records.

        Uninstall is deliberately stronger than disconnect: it revokes the
        operator's durable authorization, removes the signed MCP dependency
        through the public exact registry CAS, and finally removes the signed
        plugin package through the plugin registry's revision CAS.  Missing
        operator configuration is tolerated so an already-disconnected plugin
        can still be removed from a portable/offline install.
        """
        self._reconcile_uninstall_intent()
        # Read this first so an uninstall of an unknown plugin is a stable
        # 409/404 boundary rather than a partially applied mutation. It asks
        # only that the plugin exists and its package still verifies -- NOT
        # that its provider provenance is still on file. Provenance governs
        # whether a plugin may be used; requiring it here made a plugin whose
        # record had been retired impossible to remove, which is the one
        # operation that must always be available.
        self._installed_for_removal(plugin_id)
        try:
            account_key = self.operator_account_key()
        except ValueError as exc:
            if str(exc) != "plugin_account_unconfigured":
                raise
            account_key = None
        if account_key is not None:
            await self.connection_coordinator.disconnect(plugin_id, account_key)

        self._set_enabled(plugin_id, False)

        app_ids = set(self.app_ids_for_plugin(plugin_id))
        app_snapshot = self.app_registry.snapshot()
        remove_records = tuple(
            record for record in app_snapshot.records if record.app_id in app_ids
        )
        plugin_snapshot = self.plugin_registry.snapshot()
        plugin_record = self.plugin_registry.get(plugin_id)
        lifecycle_operation = self._require_owned_lifecycle_operation(
            kind="uninstall",
            plugin_id=plugin_id,
        )
        intent = self._begin_uninstall_intent(
            plugin_record,
            remove_records,
            lifecycle_operation_id=str(lifecycle_operation["operation_id"]),
        )
        try:
            if remove_records:
                self.app_registry.apply_exact(
                    install_records=(),
                    remove_records=remove_records,
                    expected_revision=app_snapshot.revision,
                )
                # The records are gone; the provision journal must stop claiming
                # them. A transaction left owning a removed app keeps that app
                # in the protected set, which silently blocks the NEXT
                # uninstall, and a re-release at the same version then leaves
                # two committed transactions disagreeing about one app -- a
                # state rollback deliberately refuses to resolve. Reinstalling
                # has to start from nothing.
                self._provisioner.retire_absent_app_ids(
                    tuple(record.app_id for record in remove_records)
                )
            self.plugin_registry.uninstall(
                plugin_id,
                expected_revision=plugin_snapshot.revision,
            )
            # Best-effort: a locally sealed publisher-verified install's seal
            # never lived in the manifest-declared asset set the registry row
            # and package tree above already removed (see
            # ``plugin_platform.installer.accept_normalized``); this is the
            # only place its durable sidecar entry is cleared. Every version
            # is covered, not just the selected one, in case a candidate
            # version was ever staged.
            for version in plugin_record.versions:
                delete_local_seal_document(
                    self.plugin_registry.state_root, version.manifest_sha256
                )
            self._clear_uninstall_intent(intent)
        except Exception as exc:
            try:
                self._reconcile_uninstall_intent()
            except Exception:
                pass
            raise ValueError("plugin_uninstall_incomplete") from exc

    def close(self) -> None:
        global _DEFAULT_SERVICE
        close_lock = getattr(self, "_close_lock", None)
        if close_lock is None:
            close_lock = threading.Lock()
            self._close_lock = close_lock
        with close_lock:
            self._stop_update_heartbeat()
            if self._closed:
                return
            mutation_boundary = getattr(self, "_authority_mutation_lock", None)
            close_boundary = getattr(mutation_boundary, "close", None)
            seal_boundary = getattr(mutation_boundary, "seal_held", None)
            retain_reap_lease = getattr(self, "plugin_backend", None) is not None
            if retain_reap_lease:
                self._retain_lifecycle_owner_lease = True
            closed_here = False
            sealed_here = False
            try:
                scope = (
                    mutation_boundary
                    if mutation_boundary is not None
                    else nullcontext()
                )
                # Execution takes this boundary before resolving the process
                # default, so shutdown must acquire the generation lease only
                # through that same ordered boundary.  The retain flag keeps
                # the lease after boundary exit when worker reaping fails.
                with scope:
                    with _DEFAULT_SERVICE_LOCK:
                        if self._closed:
                            return
                    if getattr(self, "plugin_backend", None) is not None:
                        self.plugin_backend.close()
                    if (
                        getattr(self, "_oauth_protocol", None) is not None
                        and getattr(self, "_operation_submitter", None) is not None
                    ):
                        protocol = self._oauth_protocol
                        self._oauth_protocol = None
                        try:
                            self._operation_submitter(protocol.aclose, timeout_s=10.0)
                        except (RuntimeError, ValueError):
                            pass
                    # Only a proven worker-tree reap permits this owner to
                    # become closed, seal queued mutations, and release the
                    # cross-process lifecycle lease.  A reap failure exits
                    # above with every ownership fence still retryable.
                    with _DEFAULT_SERVICE_LOCK:
                        if self._closed:
                            return
                        self._closed = True
                        closed_here = True
                        if _DEFAULT_SERVICE is self:
                            _DEFAULT_SERVICE = None
                    if callable(seal_boundary):
                        seal_boundary()
                        sealed_here = True
            finally:
                if closed_here:
                    # Session/protocol cleanup is part of the old owner's
                    # mutation lifetime. A successor may acquire the kernel
                    # lease only after every such cleanup has returned.
                    self._close_lifecycle_owner_lease()
                    self._retain_lifecycle_owner_lease = False
                    if not sealed_here and callable(close_boundary):
                        close_boundary()
                elif getattr(self, "_lifecycle_owner_lease", None) is None:
                    self._retain_lifecycle_owner_lease = False


def _compose_oauth_broker(
    profile_registry: OAuthProfileRegistry, *, vault: Any, protocol: Any
) -> OAuthBroker:
    """The production broker. Hosted-bridge profiles reach the bridge at the origin of
    their signed redirect (docs/plugin-hosted-oauth-bridge.md); without it they could
    never start a connection on a unit that holds no provider secret."""
    from mcp_platform.oauth_protocol import HttpHostedOAuthBridge

    return OAuthBroker(
        registry=profile_registry,
        vault=vault,
        protocol=protocol,
        hosted_bridge=HttpHostedOAuthBridge(profile_registry.resolve_by_profile_id, protocol=protocol),
    )


def compose_plugin_lifecycle_service(
    *,
    state_root: Path | None = None,
    oauth_key_provider: Callable[[], bytes] | None = None,
    operation_submitter: PluginOperationSubmitter | None = None,
) -> PluginLifecycleService:
    """Compose Omega's signed plugin/OAuth/runtime stack for one desktop state root.

    There is exactly one portable vault key in play. The device-local trust
    root that seals and reverifies publisher-verified packages
    (``plugin_platform.local_trust.LocalTrustRoot``) is derived from the same
    unlocked vault key that backs the OAuth credential vault --
    ``oauth_key_provider`` when the caller supplies one (tests, most notably)
    and otherwise the generate-once/persist/reload
    ``PortableOAuthKeyProvider`` (passphrase-sealed custody when configured,
    this device's DPAPI custody otherwise), pointed at the OAuth vault's own
    path. Never
    a second, independent, directory- or host-bound secret: the two derived
    secrets (Ed25519 seal seed, authority MAC key) get their domain
    separation entirely from ``LocalTrustRoot.from_vault_key``'s own HKDF
    labels, not from using different root keys.

    ``discovery_peer_provider`` is always
    ``_default_discovery_peer_provider`` -- the zero-argument composition
    therefore performs real restricted discovery
    (``mcp_platform.plugin_discovery_transport.RestrictedDiscoveryPeer``,
    over a discovery-only ``CurrentMcpClient``) for every publisher-verified
    marketplace entry, rather than raising ``plugin_runtime_unsupported``.
    This is not a caller-configurable parameter here (unlike
    ``oauth_key_provider``): a test that needs a different peer constructs
    ``PluginLifecycleService`` directly with its own
    ``discovery_peer_provider``, exactly as the existing discovery/marketplace
    tests already do.
    """

    root = core_paths.GUARDIAN_STATE_ROOT if state_root is None else Path(state_root)
    from mcp_platform.offline_recovery import require_recovery_complete

    require_recovery_complete(root / "plugins")
    mcp_root = root / "mcp"
    vault_root = mcp_root / "authorization"
    vault_path = vault_root / "oauth-vault.json"
    if oauth_key_provider is None:
        from mcp_platform.sealed_secrets import PortableOAuthKeyProvider

        oauth_key_provider = PortableOAuthKeyProvider(vault_root, vault_path)
    key_ring = load_embedded_key_ring()
    profile_registry = OAuthProfileRegistry.from_path(
        core_paths.OAUTH_PROFILE_CATALOG_PATH,
        key_ring,
    )
    vault = OAuthCredentialVault(root=vault_root, key_provider=oauth_key_provider)
    protocol = OAuthProtocolClient()
    broker = _compose_oauth_broker(profile_registry, vault=vault, protocol=protocol)
    authority_store = PluginAuthorityStore(path=mcp_root / "plugin_authority.json")
    service_holder: dict[str, PluginLifecycleService] = {}

    def binding_resolver(plan: Any, account_key_hash: str) -> Any:
        return service_holder["service"].bindings_for_plan(plan, account_key_hash)

    def authority_factory(plan: Any, account_key_hash: str, grant_id: str) -> Any:
        return service_holder["service"].permission_authorities(
            plan,
            account_key_hash,
            grant_id,
        )

    permission_store = PluginPermissionStore(
        path=mcp_root / "plugin_permissions.json",
        authority_factory=authority_factory,
        binding_resolver=binding_resolver,
    )
    authority_service = PluginAuthorityService(
        store=authority_store,
        binding_resolver=binding_resolver,
    )
    grant_service = PluginGrantService(
        broker=broker,
        authority_store=authority_store,
        permission_store=permission_store,
        journal_path=mcp_root / "plugin_grants.json",
    )

    def profile_match(
        plugin_id: str,
        record: InstalledMcpAppRecord,
    ) -> OAuthProfileMatchInput:
        plugin = service_holder["service"].plugin_registry.get(plugin_id)
        declared = OAuthProfileMatchInput(
            catalog_signer="omega.featured",
            plugin_publisher=plugin.publisher_id,
            app_publisher=record.publisher_id,
            provider_id=str(record.provider_id),
            authorization_issuer=str(record.authorization_issuer),
            resource_origin=str(record.remote_origin),
        )
        profile = profile_registry.resolve_declared_app(declared)
        return OAuthProfileMatchInput(
            catalog_signer=declared.catalog_signer,
            plugin_publisher=declared.plugin_publisher,
            app_publisher=declared.app_publisher,
            provider_id=declared.provider_id,
            authorization_issuer=profile.authorization_issuer,
            resource_origin=declared.resource_origin,
        )

    service = PluginLifecycleService(
        state_root=root,
        oauth_broker=broker,
        plugin_grants=grant_service,
        profile_match_provider=profile_match,
        operation_submitter=operation_submitter or _default_plugin_operation_submitter(),
        permission_store=permission_store,
        authority_service=authority_service,
        oauth_protocol=protocol,
        local_trust_key_provider=oauth_key_provider,
        discovery_peer_provider=_default_discovery_peer_provider,
    )
    service_holder["service"] = service
    return service


def get_default_plugin_lifecycle_service() -> PluginLifecycleService:
    """Return the process-owned signed plugin service used by HUD and AUTO."""
    global _DEFAULT_SERVICE
    with _DEFAULT_SERVICE_LOCK:
        if _DEFAULT_SERVICE is None or _DEFAULT_SERVICE._closed:
            _DEFAULT_SERVICE = compose_plugin_lifecycle_service()
        return _DEFAULT_SERVICE


__all__ = [
    "complete_authority_equivalent",
    "PluginAppSelectionRequired",
    "PluginLifecycleService",
    "VerifiedCatalogSourceAdapter",
    "compose_plugin_lifecycle_service",
    "get_default_plugin_lifecycle_service",
]

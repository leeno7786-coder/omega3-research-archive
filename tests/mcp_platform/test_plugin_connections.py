from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

import mcp_platform.plugin_connections as plugin_connections
import mcp_platform.plugin_discovery as plugin_discovery_module
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.client_contracts import McpOperationResult, McpOperationStatus
from mcp_platform.oauth_broker import (
    OAuthAuthorizationAttempt,
    OAuthAuthorizationStart,
    OAuthConnectionRequest,
    OAuthPreparedAuthorization,
    OAuthRevocationResult,
    OAuthBroker,
)
from mcp_platform.oauth_profiles import (
    OAuthAuthorizationFlow,
    OAuthClientMode,
    OAuthProfile,
    OAuthProfileMatchInput,
    computed_profile_revision,
)
from mcp_platform.oauth_vault import OAuthCommittedGrant, OAuthCredentialVault
from mcp_platform.plugin_authority import PluginAuthorityStore, PluginExecutionPolicy
from mcp_platform.plugin_lifecycle import PluginLifecycleService
from mcp_platform.plugin_connections import (
    PluginConnectionCoordinator,
    PluginConnectionService,
    descriptor_for_record,
    locally_sealed_capability_authority,
    signed_capability_authority_for_candidate,
)
from mcp_platform.plugin_discovery import (
    CapabilityDiscoveryBinding,
    LocalCapabilityEnvelope,
    RestrictedPluginDiscovery,
)
from mcp_platform.plugin_grants import (
    PluginGrantDisconnect,
    PluginGrantRecord,
    PluginGrantService,
)
from mcp_platform.state_store import McpStateStore
from plugin_platform.contracts import PluginAuthentication, PluginManifest
from plugin_platform.local_trust import LocalTrustRoot
from plugin_platform.records import PluginAppBinding
from tests.helpers.plugin_consent import full_connection_grant
from tests.mcp_platform.generic_plugin_install import (
    descriptor_from_installed,
    install_signed_northwind_plugins,
    stage_signed_northwind_candidate,
)
from tests.mcp_platform.generic_plugin_peers import (
    CALENDAR_APP,
    CALENDAR_PLUGIN,
    TRACKER_APP,
    TRACKER_PLUGIN,
    catalogs_for,
    TRACKER_ISSUER,
    TRACKER_ORIGIN,
)
from tests.mcp_platform.test_oauth_broker import _Clock as _RealClock
from tests.mcp_platform.test_oauth_broker import _Protocol as _RealProtocol


ACCOUNT_KEY = "operator-account"
ACCOUNT_DIGEST = sha256(ACCOUNT_KEY.encode()).hexdigest()
PROFILE_ID = "northwind-oauth"
PROFILE_REVISION = f"sha256:{'1' * 64}"
PARTITION = "2" * 64
STATE = sha256(b"callback-state").hexdigest()
STATE_TWO = sha256(b"callback-state-two").hexdigest()
SCOPES = ("issues.read", "issues.write")


class _ExactProfileRegistry:
    def __init__(self, profile: OAuthProfile) -> None:
        self.profile = profile

    def resolve(self, match: OAuthProfileMatchInput) -> OAuthProfile:
        if (
            match.catalog_signer != "omega.release"
            or match.plugin_publisher != "test.publisher"
            or match.app_publisher != "omega.first_party"
            or match.provider_id != "northwind"
            or match.authorization_issuer != TRACKER_ISSUER
            or match.resource_origin not in self.profile.resource_origins
        ):
            raise ValueError("oauth_profile_unavailable")
        return self.profile

    def get_exact(self, profile_id: str, revision: str) -> OAuthProfile:
        if (profile_id, revision) != (self.profile.profile_id, self.profile.revision):
            raise ValueError("oauth_profile_unavailable")
        return self.profile


def _real_profile(
    *,
    resource_origin: str = TRACKER_ORIGIN,
    additional_resource_origins: tuple[str, ...] = (),
    authorization_flow: OAuthAuthorizationFlow = (
        OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
    ),
) -> OAuthProfile:
    profile = OAuthProfile(
        schema_version=1,
        profile_id=PROFILE_ID,
        catalog_signer="omega.release",
        allowed_plugin_publishers=("test.publisher",),
        app_publisher="omega.first_party",
        provider_id="northwind",
        authorization_issuer=TRACKER_ISSUER,
        resource_origins=(resource_origin, *additional_resource_origins),
        client_mode=OAuthClientMode.PREREGISTERED,
        client_id="northwind-public-client",
        client_secret_ref=None,
        client_metadata_document_url=None,
        redirect_uri=(
            None
            if authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE
            else f"http://127.0.0.1:13310/oauth/{PROFILE_ID}/callback"
        ),
        account_partition="issuer_account",
        revision="sha256:" + "0" * 64,
        authorization_flow=authorization_flow,
    )
    return replace(profile, revision=computed_profile_revision(profile))


def _callback_uri(start: OAuthAuthorizationStart, profile: OAuthProfile, *, error=None) -> str:
    assert start.authorization_url is not None
    raw_state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    query = {"state": raw_state, "iss": TRACKER_ISSUER}
    if error is None:
        query["code"] = "opaque-code"
    else:
        query["error"] = error
    return f"{profile.redirect_uri}?{urlencode(query)}"


def test_resolved_oauth_issuer_may_specialize_only_a_declared_root_origin() -> None:
    assert hasattr(plugin_connections, "_resolved_issuer_matches_declared")
    matches = plugin_connections._resolved_issuer_matches_declared

    assert matches("https://github.com", "https://github.com/login/oauth") is True
    assert matches("https://github.com", "https://github.com") is True
    assert matches("https://github.com/oauth", "https://github.com/oauth/device") is False
    assert matches("https://github.com", "https://evil.example/login/oauth") is False


class _MemoryAuthorityStore:
    def __init__(self) -> None:
        self._connections: dict[tuple[str, str, str], object] = {}

    def connection(self, plugin_id: str, app_id: str, account: str):
        try:
            return self._connections[(plugin_id, app_id, account)]
        except KeyError:
            raise ValueError("plugin_connection_authority_not_found") from None

    def publish_connection(self, authority, *, expected_revision: str) -> None:
        key = (authority.plugin_id, authority.app_id, authority.account_key_hash)
        current = self._connections.get(key)
        observed = "" if current is None else current.revision
        if observed != expected_revision:
            raise ValueError("plugin_authority_conflict")
        self._connections[key] = authority

    def invalidate_connection(self, plugin_id: str, app_id: str, account: str) -> None:
        self._connections.pop((plugin_id, app_id, account), None)


class _GrantService:
    def __init__(self, broker: _Broker) -> None:
        self._broker = broker
        self._records: dict[tuple[str, str, str], PluginGrantRecord] = {}
        self._pending: dict[tuple[str, str, str], PluginGrantRecord] = {}
        self.finish_errors: list[BaseException] = []
        self.authority_store = _MemoryAuthorityStore()

    @staticmethod
    def _key(plugin_id: str, app_id: str, account: str) -> tuple[str, str, str]:
        return plugin_id, app_id, account

    def current(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> PluginGrantRecord | None:
        return self._records.get(self._key(plugin_id, app_id, account_key_sha256))

    def current_for_attempt(self, state_sha256: str) -> PluginGrantRecord | None:
        return next(
            (
                record
                for record in self._records.values()
                if record.activation.attempt_state_sha256 == state_sha256
            ),
            None,
        )

    def pending_disconnect(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ):
        key = self._key(plugin_id, app_id, account_key_sha256)
        record = self._pending.get(key)
        if record is None:
            return None
        return SimpleNamespace(key=key, record=record)

    def suspend(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> None:
        self._records.pop(self._key(plugin_id, app_id, account_key_sha256), None)

    def begin_disconnect(
        self,
        plugin_id: str,
        *,
        app_id: str | None = None,
        account_key_sha256: str,
    ):
        matches = [
            (key, record)
            for key, record in self._records.items()
            if key[0] == plugin_id and key[2] == account_key_sha256
            and (app_id is None or key[1] == app_id)
        ]
        if matches:
            key, record = matches[0]
            self._records.pop(key)
            self._pending[key] = record
        else:
            matches = [
                (key, record)
                for key, record in self._pending.items()
                if key[0] == plugin_id and key[2] == account_key_sha256
                and (app_id is None or key[1] == app_id)
            ]
            if not matches:
                raise ValueError("plugin_grant_not_found")
            key, record = matches[0]
        return SimpleNamespace(key=key, record=record)

    async def finish_disconnect(
        self,
        pending,
        *,
        expected_receipts=None,
        tolerate_provider_refusal=False,
    ):
        if self.finish_errors:
            raise self.finish_errors.pop(0)
        self._pending.pop(pending.key, None)
        return PluginGrantDisconnect(True, ())

    def activate(self, prepared, activation):
        del prepared
        receipt = OAuthCommittedGrant(
            receipt_id="4" * 64,
            plugin_id=activation.plugin_id,
            app_id=activation.app_id,
            account_key_sha256=activation.account_key_sha256,
            profile_id=activation.profile_id,
            profile_revision=activation.profile_revision,
            token_partition_key=activation.token_partition_key,
            connection_revision=activation.connection_revision,
            capability_revision=activation.capability_revision,
            authority_revision=activation.authority_revision,
            capability_binding_revision=activation.capability_binding_revision,
            scopes=activation.scopes,
            execution_policy=activation.execution_policy.value,
        )
        record = PluginGrantRecord(receipt, activation, activation.authority())
        self._records[
            self._key(
                activation.plugin_id,
                activation.app_id,
                activation.account_key_sha256,
            )
        ] = record
        self._broker.committed = True
        self._broker.committed_states.add(str(activation.attempt_state_sha256))
        return record.authority

    async def revoke(self, plugin_id: str, *, account_key_sha256: str):
        pending = self.begin_disconnect(plugin_id, account_key_sha256=account_key_sha256)
        return await self.finish_disconnect(pending)


class _Broker:
    def __init__(self) -> None:
        self.request: OAuthConnectionRequest | None = None
        self.committed = False
        self.committed_states: set[str] = set()
        self.transport_error: str | None = None
        self.complete_error: str | None = None
        self.prepared_calls = 0
        self.prepared_bindings: list[object] = []
        self.transport_calls = 0
        self.recovered: tuple[OAuthPreparedAuthorization, ...] = ()
        self.discarded_prepared: list[str] = []
        self.complete_calls = 0
        self.requests: dict[str, OAuthConnectionRequest] = {}
        self.authorization_flow = OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE

    async def begin(self, request: OAuthConnectionRequest) -> OAuthAuthorizationStart:
        if request.expected_authorization_flow is not self.authorization_flow:
            raise ValueError("oauth_authorization_flow_mismatch")
        self.request = request
        state_sha256 = STATE if not self.requests else STATE_TWO
        self.requests[state_sha256] = request
        return OAuthAuthorizationStart(
            profile_id=PROFILE_ID,
            plugin_id=request.plugin_id,
            authorization_url="https://auth.northwind.test/authorize",
            redirect_uri="http://127.0.0.1:8765/oauth/northwind-oauth/callback",
            expires_at=60.0,
            state_sha256=state_sha256,
            authorization_flow=self.authorization_flow,
        )

    async def complete(self, *, profile_id: str, callback_uri: str):
        self.complete_calls += 1
        if self.complete_error is not None:
            raise ValueError(self.complete_error)
        assert profile_id == PROFILE_ID
        state_sha256 = self.callback_state_sha256(callback_uri)
        request = self.requests[state_sha256]
        return OAuthPreparedAuthorization(
            profile_id=PROFILE_ID,
            plugin_id=request.plugin_id,
            app_id=request.app_id,
            account_key_sha256=request.account_key_sha256,
            state_sha256=state_sha256,
            partition_key=PARTITION,
            profile_revision=PROFILE_REVISION,
            authorization_context_revision=request.authorization_context_revision,
            scopes=request.scopes,
            attempt_context={
                **dict(request.attempt_context),
                "omega_authorization_context_revision": (request.authorization_context_revision),
            },
        )

    @staticmethod
    def callback_state_sha256(callback_uri: str) -> str:
        state = parse_qs(urlsplit(callback_uri).query).get("state", ["callback-state"])[0]
        return sha256(state.encode()).hexdigest()

    def recover(self):
        return self.recovered, ()

    def is_transaction_consumed(self, state_sha256: str) -> bool:
        return state_sha256 in self.committed_states

    def authorization_attempt(self, state_sha256: str) -> OAuthAuthorizationAttempt:
        request = self.requests[state_sha256]
        return OAuthAuthorizationAttempt(
            profile_id=PROFILE_ID,
            plugin_id=request.plugin_id,
            app_id=request.app_id,
            account_key_sha256=request.account_key_sha256,
            state_sha256=state_sha256,
            authorization_context_revision=request.authorization_context_revision,
            attempt_context=request.attempt_context,
        )

    def discard_prepared(self, state_sha256: str) -> None:
        self.discarded_prepared.append(state_sha256)

    def discard_attempt(self, state_sha256: str) -> None:
        self.discarded_prepared.append(state_sha256)
        self.requests.pop(state_sha256, None)
        self.recovered = tuple(
            prepared for prepared in self.recovered if prepared.state_sha256 != state_sha256
        )

    def discard_connection_attempts(
        self,
        plugin_id: str,
        *,
        app_id: str,
        account_key_sha256: str,
    ) -> None:
        for state, request in tuple(self.requests.items()):
            if (request.plugin_id, request.app_id, request.account_key_sha256) == (
                plugin_id,
                app_id,
                account_key_sha256,
            ):
                self.discard_attempt(state)

    def authorize_prepared_transport(self, prepared, binding, authority):
        del authority
        self.prepared_calls += 1
        self.prepared_bindings.append(binding)
        if prepared.state_sha256 in self.committed_states:
            raise ValueError("oauth_prepared_authorization_invalid")
        return object()

    async def _current_prepared_access_token(self, prepared, binding) -> str:
        # The mandatory-worker probe path reads a token from the broker
        # directly instead of taking `authorize_prepared_transport`'s
        # opaque-authority shape; it shares the same call/validation
        # accounting so existing `prepared_calls`/`prepared_bindings`
        # assertions still describe one prepared-transport probe.
        self.prepared_calls += 1
        self.prepared_bindings.append(binding)
        if prepared.state_sha256 in self.committed_states:
            raise ValueError("oauth_prepared_authorization_invalid")
        return "worker-access-token"

    def authorize_transport(self, binding, authority):
        del binding, authority
        self.transport_calls += 1
        if self.transport_error is not None:
            raise ValueError(self.transport_error)
        if not self.committed:
            raise ValueError("oauth_grant_not_found")
        return object()

    async def _issue_execution_worker_access_token(
        self,
        binding,
        authority,
        **_kwargs,
    ) -> str:
        self.authorize_transport(binding, authority)
        return "worker-access-token"

    async def revoke(self, partition_key: str, *, plugin_id: str):
        del partition_key, plugin_id
        return OAuthRevocationResult(True, (), True)


class _DiscoveryWorker:
    """Deterministic child-boundary double; it never creates a parent client."""

    def run_sync(self, *, authority, body, credential, timeout_seconds):
        del authority, credential, timeout_seconds
        descriptor = dict(body["descriptor"])
        signed = dict(descriptor["signed_capability_authority"])
        return {
            "discovery": {
                "app_id": descriptor["app_id"],
                "publisher_id": descriptor["publisher_id"],
                "deployment": descriptor["deployment"],
                "transport": descriptor["transport"],
                "protocol_version": descriptor["protocol_version"],
                "source": (
                    "live_observed"
                    if descriptor["deployment"] == "local"
                    else "live_authenticated"
                ),
                "server_id": descriptor["app_id"],
                "server_version": "1.0.0",
                "tools": {name: {} for name in signed["tools"]},
                "prompts": {name: {} for name in signed["prompts"]},
                "resources": {name: {} for name in signed["resources"]},
                "resource_templates": {
                    name: {} for name in signed["resource_templates"]
                },
                "extensions": {},
                "cache_metadata": {},
                "capability_revision": descriptor["capability_revision"],
                "signed_capability_authority": signed,
            }
        }

    async def run_async(self, **kwargs):
        return self.run_sync(**kwargs)

    def close(self) -> None:
        return None


class _ClientDiscoveryWorker:
    """Routes worker discovery through the test's injected client factory.

    ``PluginConnectionService`` now requires an injected worker for every
    operation, including the ones the retired direct-client branches used to
    serve.  Many remote/credentialless tests still need to observe or drive
    the fake provider client itself (call order, a registry mutation timed
    to a snapshot read, drift detection).  This worker performs the same
    ``client.start()`` + ``client.snapshot_plugin_tools()`` sequence the
    direct-client path used, through the caller's own ``client_factory``,
    then reports discovery using the descriptor's own signed capability
    names -- the shape ``_validate_worker_discovery`` requires -- so the
    client's observable side effects still happen while the service always
    exercises its mandatory worker path.
    """

    def __init__(self, client_factory) -> None:
        self._client_factory = client_factory

    async def run_async(self, *, authority, body, credential, timeout_seconds):
        del authority, timeout_seconds
        descriptor = dict(body["descriptor"])
        signed = dict(descriptor["signed_capability_authority"])
        authorization = credential.get("access_token")
        client = self._client_factory(descriptor, authorization)
        try:
            await client.start()
            request = SimpleNamespace(
                request_id="worker-discovery-probe",
                app_id=descriptor["app_id"],
                capability_id="discovery",
                capability_revision=descriptor["capability_revision"],
            )
            snapshot = await client.snapshot_plugin_tools(request)
        finally:
            # A real worker's provider client does not outlive the
            # operation; the retired direct-client branch closed it in a
            # `finally` too, so a probe still tears its client down exactly
            # once regardless of success or failure.
            await client.close()
        # Report exactly the tool names the live client observed, not a
        # blind echo of the signed set: a shrunk live snapshot must still
        # surface as the same structural drift `_validate_worker_discovery`
        # already fails closed on.
        observed_tool_names = {
            str(tool["name"]) for tool in snapshot.data.get("tools", ())
        }
        return {
            "discovery": {
                "app_id": descriptor["app_id"],
                "publisher_id": descriptor["publisher_id"],
                "deployment": descriptor["deployment"],
                "transport": descriptor["transport"],
                "protocol_version": descriptor["protocol_version"],
                "source": (
                    "live_observed"
                    if descriptor["deployment"] == "local"
                    else "live_authenticated"
                ),
                "server_id": descriptor["app_id"],
                "server_version": "1.0.0",
                "tools": {name: {} for name in observed_tool_names},
                "prompts": {name: {} for name in signed["prompts"]},
                "resources": {name: {} for name in signed["resources"]},
                "resource_templates": {
                    name: {} for name in signed["resource_templates"]
                },
                "extensions": {},
                "cache_metadata": {},
                "capability_revision": descriptor["capability_revision"],
                "signed_capability_authority": signed,
            }
        }

    def run_sync(self, **_kwargs):
        raise AssertionError("remote/credentialless probes use run_async")

    def close(self) -> None:
        return None


class _ServiceClientDiscoveryWorker(_ClientDiscoveryWorker):
    """Like ``_ClientDiscoveryWorker``, but re-reads the live client factory.

    A test that swaps ``service._client_factory`` mid-flight -- to simulate
    drift observed during a live probe -- needs the very next discovery
    call to see it; a frozen closure captured once at construction cannot.
    """

    def __init__(self, service: PluginConnectionService) -> None:
        self._service = service

    async def run_async(self, **kwargs):
        self._client_factory = self._service._client_factory
        return await super().run_async(**kwargs)


class _AsyncDiscoveryOnlyWorker(_DiscoveryWorker):
    def run_sync(self, **_kwargs):
        raise AssertionError("discovery must use cancellation-aware worker management")

    async def run_async(self, **kwargs):
        return super().run_sync(**kwargs)


class _Client:
    def __init__(
        self,
        tool_documents: tuple[dict[str, object], ...],
        *,
        snapshot_hook=None,
        close_entered: asyncio.Event | None = None,
        close_release: asyncio.Event | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.tool_documents = tool_documents
        self.snapshot_hook = snapshot_hook
        self.close_entered = close_entered
        self.close_release = close_release
        self.close_error = close_error
        self.started = False
        self.closed = False

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        if self.close_entered is not None:
            self.close_entered.set()
        if self.close_release is not None:
            await self.close_release.wait()
        if self.close_error is not None:
            raise self.close_error
        self.closed = True

    async def snapshot_plugin_tools(self, request) -> McpOperationResult:
        if self.snapshot_hook is not None:
            result = self.snapshot_hook()
            if inspect.isawaitable(result):
                await result
        return McpOperationResult(
            request_id=request.request_id,
            app_id=request.app_id,
            capability_id=request.capability_id,
            capability_revision=request.capability_revision,
            status=McpOperationStatus.COMPLETED,
            data={"tools": list(self.tool_documents)},
            input_request=None,
            task_handle=None,
            error=None,
        )


def _tool_documents(peer: str) -> tuple[dict[str, object], ...]:
    catalog, _resources = catalogs_for(peer)
    return tuple(tool.to_dict() for tool in catalog.tools)


def _profile_match(_plugin_id: str, record) -> OAuthProfileMatchInput:
    return OAuthProfileMatchInput(
        catalog_signer="omega.release",
        plugin_publisher="test.publisher",
        app_publisher=record.publisher_id,
        provider_id=record.provider_id,
        authorization_issuer=str(record.authorization_issuer),
        resource_origin=str(record.remote_origin),
    )


def _direct_submit(operation, **_kwargs):
    return operation()


def _full_candidate_grant(
    service: PluginConnectionService,
    plugin_id: str,
    app_id: str,
    *,
    candidate_verified,
    candidate_binding: PluginAppBinding,
    expected_registry_revision: str,
) -> dict[str, Any]:
    """``start_candidate``/``probe_candidate`` keywords granting everything a candidate offers.

    ``full_connection_grant`` reads the selected app; a candidate's offer is its own
    signed requirement and descriptor, which only the candidate definition names.
    """
    _current, _record, requirement, descriptor = service._candidate_definition(
        plugin_id,
        app_id,
        candidate_verified=candidate_verified,
        candidate_binding=candidate_binding,
        expected_registry_revision=expected_registry_revision,
    )
    declared = {
        capability: digest
        for capability, digest in descriptor.declared_capabilities.items()
        if capability.startswith("tool:") and type(digest) is str
    }
    ceiling = tuple(sorted(declared))
    return {
        "scopes": tuple(sorted(requirement.authentication.scopes)),
        "capability_ceiling": ceiling,
        "allowed_tool_schemas": {
            capability.removeprefix("tool:"): declared[capability] for capability in ceiling
        },
    }


def _remote_service(
    tmp_path: Path,
    *,
    tools: tuple[dict[str, object], ...] | None = None,
    installed: dict[str, Any] | None = None,
    broker: _Broker | None = None,
    grants: _GrantService | None = None,
    client_factory=None,
    authentication_kind: str = "oauth_authorization_code_pkce",
    execution_worker=None,
) -> tuple[PluginConnectionService, _Broker, dict[str, Any]]:
    installed = (
        install_signed_northwind_plugins(
            tmp_path,
            peers=("tracker",),
            tracker_authentication_kind=authentication_kind,
        )
        if installed is None
        else installed
    )
    registry = installed["plugin_registry"]
    if registry.get(TRACKER_PLUGIN).enabled:
        registry.set_enabled(
            TRACKER_PLUGIN,
            False,
            expected_revision=registry.snapshot().revision,
        )
    broker = _Broker() if broker is None else broker
    broker.authorization_flow = (
        OAuthAuthorizationFlow.DEVICE_CODE
        if authentication_kind == "oauth_device_code"
        else OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
    )
    grants = _GrantService(broker) if grants is None else grants
    if authentication_kind == "none":
        grants.authority_store = PluginAuthorityStore(
            path=tmp_path / "credentialless-authority.json"
        )
    selected_tools = _tool_documents("tracker") if tools is None else tools
    resolved_client_factory = (
        client_factory
        if client_factory is not None
        else lambda _descriptor, _authorization: _Client(selected_tools)
    )
    service = PluginConnectionService(
        app_registry=installed["app_registry"],
        plugin_registry=registry,
        broker=broker,
        grant_service=grants,
        descriptor_provider=lambda _plugin_id, app_id: descriptor_from_installed(
            installed["app_registry"], installed["package_registry"], app_id
        ),
        requirement_provider=lambda _plugin_id, _record: registry.reverify_installed(
            TRACKER_PLUGIN
        ).manifest.required_apps[0],
        offered_scopes_provider=lambda _plugin_id, _app_id: SCOPES,
        profile_match_provider=_profile_match,
        operation_submitter=_direct_submit,
        client_factory=resolved_client_factory,
        transport_authority=object(),
        execution_worker=(
            _ClientDiscoveryWorker(resolved_client_factory)
            if execution_worker is None
            else execution_worker
        ),
    )
    return service, broker, installed


def _shared_client_remote_service(
    tmp_path: Path,
    **kwargs: Any,
) -> tuple[PluginConnectionService, _Broker, dict[str, Any], _Client]:
    """Build a `_remote_service` whose live client is one shared, mutable fake.

    The mandatory worker constructs a fresh client per discovery call
    through `client_factory`; a test that wants to mutate the *live*
    provider's observable state between calls (a shrunk tool set, a call
    hook) needs that factory to keep returning the same instance instead of
    a new one each time.
    """

    tools = kwargs.pop("tools", None)
    shared_client = _Client(_tool_documents("tracker") if tools is None else tools)
    service, broker, installed = _remote_service(
        tmp_path,
        client_factory=lambda _descriptor, _authorization: shared_client,
        **kwargs,
    )
    return service, broker, installed, shared_client


@pytest.mark.anyio
async def test_credentialless_discovery_uses_cancellation_aware_worker_operation(
    tmp_path: Path,
) -> None:
    service, _broker, _installed = _remote_service(
        tmp_path,
        authentication_kind="none",
        execution_worker=_AsyncDiscoveryOnlyWorker(),
    )

    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )

    assert started.status == "connected"


@pytest.mark.anyio
async def test_compatible_update_reuses_grant_without_new_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, broker, installed = _remote_service(tmp_path)
    profile = _real_profile()
    initial = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        initial.request_id or "",
        f"{profile.redirect_uri}?code=opaque-code&state=callback-state",
    )
    grant = service._grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=ACCOUNT_DIGEST,
    )
    assert grant is not None
    target_package = "f" * 64
    rebound_activation = replace(grant.activation, package_revision=target_package)
    grant_key = service._grants._key(
        TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST
    )
    service._grants._records[grant_key] = PluginGrantRecord(
        grant.receipt, rebound_activation, rebound_activation.authority()
    )
    registry_revision = installed["plugin_registry"].snapshot().revision
    monkeypatch.setattr(
        service,
        "_mutation_definition",
        lambda *_args, **_kwargs: (None, None, None, target_package, registry_revision),
    )
    monkeypatch.setattr(service, "_package_revision", lambda _plugin_id: target_package)
    prepared_calls = broker.prepared_calls
    result = await service.start_compatible_update(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        activation=rebound_activation,
        expected_registry_revision=registry_revision,
    )
    assert result.status == "connected"
    assert broker.prepared_calls == prepared_calls
    assert broker.transport_calls > 0
    assert service._grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=ACCOUNT_DIGEST,
    ).activation.package_revision == target_package


@pytest.mark.anyio
async def test_lifecycle_promotion_reuses_authenticated_grant_without_oauth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, broker, installed = _remote_service(tmp_path)
    profile = _real_profile()
    initial = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        initial.request_id or "",
        f"{profile.redirect_uri}?code=opaque-code&state=callback-state",
    )
    grant = service._grants.current(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key_sha256=ACCOUNT_DIGEST
    )
    assert grant is not None
    target_package = "e" * 64
    registry_revision = installed["plugin_registry"].snapshot().revision
    monkeypatch.setattr(
        service,
        "_mutation_definition",
        lambda *_args, **_kwargs: (None, None, None, target_package, registry_revision),
    )
    monkeypatch.setattr(service, "_package_revision", lambda _plugin_id: target_package)

    binding_material = {
        "required_app_ids": [TRACKER_APP],
        "optional_app_ids": [],
        "dependency_receipt_id": "candidate-receipt",
    }
    binding = PluginAppBinding(
        required_app_ids=(TRACKER_APP,),
        optional_app_ids=(),
        dependency_receipt_id="candidate-receipt",
        revision=sha256_hex(canonical_bytes(binding_material)),
    )
    old_record = installed["plugin_registry"].get(TRACKER_PLUGIN)
    lifecycle = object.__new__(PluginLifecycleService)
    lifecycle.plugin_registry = SimpleNamespace(
        snapshot=lambda: SimpleNamespace(revision=registry_revision),
        promote_candidate=lambda *_args, **_kwargs: SimpleNamespace(
            revision=registry_revision
        ),
    )
    lifecycle.app_registry = SimpleNamespace(snapshot=lambda: SimpleNamespace(revision="a" * 64))
    lifecycle._installer = SimpleNamespace(
        provisioner=SimpleNamespace(promote_candidate_records=lambda *_args, **_kwargs: None)
    )
    def rebind_compatible_update(
        plugin_id: str, *, app_id: str, account_key_sha256: str, package_revision: str
    ) -> PluginGrantRecord:
        rebound_activation = replace(
            grant.activation, package_revision=package_revision
        )
        rebound = PluginGrantRecord(
            grant.receipt, rebound_activation, rebound_activation.authority()
        )
        service._grants._records[(plugin_id, app_id, account_key_sha256)] = rebound
        return rebound

    lifecycle.plugin_grants = SimpleNamespace(
        rebind_compatible_update=rebind_compatible_update
    )
    lifecycle._connection_service = service
    lifecycle.operator_account_key = lambda: ACCOUNT_KEY
    lifecycle._assert_candidate_bindings = lambda *_args, **_kwargs: None
    lifecycle._assert_plugin_compiled = lambda _plugin_id: None
    lifecycle._write_update_document = lambda _document: None
    lifecycle._clear_update_document = lambda _operation: None
    lifecycle._update_state = McpStateStore(
        state_root=tmp_path,
        document_path=tmp_path / "lifecycle-update.json",
        invalid_path_reason="plugin_update_recovery_required",
        max_document_bytes=1024 * 1024,
    )
    operation = {
        "plugin_id": TRACKER_PLUGIN,
        "target_version": "2.0.0",
        "target_signing_key_id": "test.key",
        "target_manifest_sha256": "3" * 64,
        "target_package_sha256": target_package,
        "base_record": old_record.to_dict(),
        "dependency_receipt_id": "candidate-receipt",
        "phase": "candidate",
        "automatic": True,
        "candidate_apps": [
            {
                "app_id": TRACKER_APP,
                "state": "ready",
                "request_id": grant.activation.attempt_state_sha256,
                "connection_revision": grant.activation.connection_revision,
                "activation": grant.activation.to_dict(),
                # A retained grant carries its recorded per-app consent, exactly
                # as the update planner records it for a retained activation.
                "settings": PluginLifecycleService._activation_settings_document(
                    grant.activation
                ),
            }
        ],
    }
    verified = SimpleNamespace(manifest=SimpleNamespace(plugin_version="2.0.0"))
    prepared_calls = broker.prepared_calls

    await lifecycle._promote_update_candidate(operation, verified, binding)

    assert broker.prepared_calls == prepared_calls
    rebound = service._grants.current(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key_sha256=ACCOUNT_DIGEST
    )
    assert rebound is not None
    assert rebound.activation.package_revision == target_package
    assert rebound.activation.profile_id == grant.activation.profile_id
    assert rebound.activation.profile_revision == grant.activation.profile_revision
    assert rebound.activation.token_partition_key == grant.activation.token_partition_key
    assert service._states[(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)] == "connected"


@pytest.mark.anyio
async def test_coordinator_combines_registry_blocker_with_real_app_state(
    tmp_path: Path,
) -> None:
    service, _broker, installed = _remote_service(
        tmp_path,
        authentication_kind="none",
    )
    registry = installed["plugin_registry"]
    registry.block(
        TRACKER_PLUGIN,
        "dependency_unavailable",
        expected_revision=registry.snapshot().revision,
    )
    coordinator = PluginConnectionCoordinator(
        plugin_registry=registry,
        connection_service=service,
    )

    lifecycle = await coordinator.status(TRACKER_PLUGIN, ACCOUNT_KEY)

    assert lifecycle.state == "blocked"
    assert lifecycle.error_code == "plugin_blocked"
    assert tuple(app.state for app in lifecycle.apps if app.required) == (
        "setup_required",
    )
    assert service._sessions == {}


@pytest.mark.anyio
async def test_credentialless_start_rejects_registry_change_before_authority_commit(
    tmp_path: Path,
) -> None:
    installed: dict[str, Any] = {}

    def mutate_registry() -> None:
        registry = installed["plugin_registry"]
        registry.set_enabled(
            TRACKER_PLUGIN,
            True,
            expected_revision=registry.snapshot().revision,
        )

    client = _Client(_tool_documents("tracker"), snapshot_hook=mutate_registry)
    service, _broker, installed = _remote_service(
        tmp_path,
        authentication_kind="none",
        client_factory=lambda _descriptor, _authorization: client,
    )
    expected_revision = installed["plugin_registry"].snapshot().revision
    grant = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)

    with pytest.raises(ValueError, match="^plugin_registry_conflict$"):
        await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            expected_registry_revision=expected_revision,
            **grant,
        )

    with pytest.raises(ValueError, match="^plugin_connection_authority_not_found$"):
        service.authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    assert service._sessions == {}


@pytest.mark.anyio
async def test_disconnect_rejects_stale_registry_revision_before_mutation(
    tmp_path: Path,
) -> None:
    service, _broker, installed = _remote_service(
        tmp_path,
        authentication_kind="none",
    )
    await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    registry = installed["plugin_registry"]
    stale_revision = registry.snapshot().revision
    registry.set_enabled(
        TRACKER_PLUGIN,
        True,
        expected_revision=stale_revision,
    )

    with pytest.raises(ValueError, match="^plugin_registry_conflict$"):
        await service.revoke(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            expected_registry_revision=stale_revision,
        )

    assert service.authority_store.connection(
        TRACKER_PLUGIN,
        TRACKER_APP,
        ACCOUNT_DIGEST,
    ).plugin_id == TRACKER_PLUGIN


@pytest.mark.anyio
async def test_start_rejects_oauth_flow_that_differs_from_signed_authentication(
    tmp_path: Path,
) -> None:
    service, broker, _installed = _remote_service(tmp_path)
    broker.authorization_flow = OAuthAuthorizationFlow.DEVICE_CODE

    with pytest.raises(ValueError, match="oauth_authorization_flow_mismatch"):
        await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
        )

    assert service._states == {}
    assert broker.requests == {}


@pytest.mark.anyio
async def test_prepared_activation_revalidates_signed_oauth_flow_and_discards_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, broker, installed = _remote_service(tmp_path)
    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    prepared = await broker.complete(
        profile_id=PROFILE_ID,
        callback_uri=("http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state"),
    )
    registry = installed["plugin_registry"]
    verified = registry.reverify_installed(TRACKER_PLUGIN)
    manifest_document = verified.manifest.to_dict()
    manifest_document["required_apps"][0]["authentication"] = {
        "kind": "oauth_device_code",
        "scopes": ["issues.read", "issues.write"],
        "secret_fields": [],
    }
    changed_manifest = PluginManifest.from_dict(manifest_document)
    monkeypatch.setattr(
        registry,
        "reverify_installed",
        lambda _plugin_id: replace(verified, manifest=changed_manifest),
    )

    with pytest.raises(ValueError, match="oauth_authorization_flow_mismatch"):
        await service.activate_prepared(prepared)

    assert broker.discarded_prepared == [started.request_id]
    assert service._grants.current(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key_sha256=ACCOUNT_DIGEST,
    ) is None


@pytest.mark.anyio
async def test_no_auth_remote_uses_restricted_probe_then_live_authority(tmp_path: Path) -> None:
    installed = install_signed_northwind_plugins(tmp_path, peers=("tracker",))
    original_record = installed["app_registry"].get(TRACKER_APP)
    none_auth = PluginAuthentication.from_dict(
        {"kind": "none", "scopes": [], "secret_fields": []}
    )
    record = replace(original_record, authentication=none_auth)
    original_manifest = installed["plugin_registry"].reverify_installed(
        TRACKER_PLUGIN
    ).manifest
    requirement = replace(original_manifest.required_apps[0], authentication=none_auth)
    manifest = replace(original_manifest, required_apps=(requirement,))
    plugin = installed["plugin_registry"].get(TRACKER_PLUGIN)
    calls: list[str] = []

    class _NoAuthClient(_Client):
        async def start(self) -> None:
            calls.append("initialize")
            await super().start()

        async def snapshot_plugin_tools(self, request):
            calls.append("capabilities")
            return await super().snapshot_plugin_tools(request)

    class _AppRegistry:
        def get(self, app_id: str):
            assert app_id == TRACKER_APP
            return record

    class _PluginRegistry:
        def get(self, plugin_id: str):
            assert plugin_id == TRACKER_PLUGIN
            return plugin

        def reverify_installed(self, plugin_id: str):
            assert plugin_id == TRACKER_PLUGIN
            return SimpleNamespace(manifest=manifest, package_sha256="d" * 64)

        def snapshot(self):
            return installed["plugin_registry"].snapshot()

        def verified_installed_snapshot(self, plugin_id: str):
            assert plugin_id == TRACKER_PLUGIN
            return SimpleNamespace(
                record=plugin,
                verified=SimpleNamespace(manifest=manifest, package_sha256="d" * 64),
                revision=installed["plugin_registry"].snapshot().revision,
            )

        def set_enabled(self, plugin_id: str, enabled: bool, *, expected_revision: str):
            return installed["plugin_registry"].set_enabled(
                plugin_id, enabled, expected_revision=expected_revision
            )

    broker = _Broker()
    grants = _GrantService(broker)
    grants.authority_store = PluginAuthorityStore(path=tmp_path / "no-auth-authority.json")

    def no_auth_client_factory(_descriptor, authorization):
        if authorization is not None:
            pytest.fail("no-auth transport received OAuth authorization")
        return _NoAuthClient(_tool_documents("tracker"))

    service = PluginConnectionService(
        app_registry=_AppRegistry(),
        plugin_registry=_PluginRegistry(),
        broker=broker,
        grant_service=grants,
        descriptor_provider=lambda _plugin_id, _app_id: descriptor_for_record(
            record,
            signed_capability_authority=(
                plugin_connections.signed_capability_authority_from_package(
                    installed["package_registry"],
                    original_record,
                    plugin_registry=installed["plugin_registry"],
                    plugin_id=TRACKER_PLUGIN,
                )
            ),
        ),
        requirement_provider=lambda _plugin_id, _record: requirement,
        offered_scopes_provider=lambda _plugin_id, _app_id: (),
        operation_submitter=_direct_submit,
        client_factory=no_auth_client_factory,
        transport_authority=object(),
        execution_worker=_ClientDiscoveryWorker(no_auth_client_factory),
    )

    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )

    assert started.status == "connected"
    assert started.authorization_url is None
    assert calls == ["initialize", "capabilities"]
    assert broker.requests == {}

    key = (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    active = service._sessions[key]

    async def failing_close() -> None:
        raise RuntimeError("close_failed")

    # The worker owns the provider client end to end, so the parent-held
    # session client is always the `_WorkerOnlyLocalClient` marker; this
    # overrides that exact instance's close to simulate the same close
    # failure a real session teardown could still hit.
    active.client.close = failing_close

    with pytest.raises(RuntimeError, match="^close_failed$"):
        await service.revoke(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
        )

    with pytest.raises(ValueError, match="^plugin_connection_authority_not_found$"):
        service.authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    after_failed_close = await service.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )
    assert after_failed_close.status == "installed_disconnected"
    assert calls == ["initialize", "capabilities"]
    active.client.close_error = None

    await service.revoke(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )
    disconnected = await service.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )

    assert disconnected.status == "installed_disconnected"
    assert calls == ["initialize", "capabilities"]


@pytest.mark.anyio
async def test_connection_is_not_connected_when_live_binding_compilation_is_empty(
    tmp_path: Path,
) -> None:
    service, broker, _installed = _remote_service(tmp_path, tools=())
    await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    prepared = await broker.complete(
        profile_id=PROFILE_ID,
        callback_uri=(
            "http://127.0.0.1/oauth/northwind-oauth/callback?code=x&state=callback-state"
        ),
    )

    with pytest.raises(ValueError, match="^plugin_capability_invalid$"):
        await service.activate_prepared(prepared)

    status = await service.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )
    assert status.status == "installed_disconnected"
    assert broker.committed is False


@pytest.mark.anyio
async def test_callback_probes_before_commit_then_publishes_exact_live_binding(
    tmp_path: Path,
) -> None:
    service, broker, installed = _remote_service(tmp_path)
    grant = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        scopes=("issues.write",),
        capability_ceiling=grant["capability_ceiling"],
        allowed_tool_schemas=grant["allowed_tool_schemas"],
        execution_policy=PluginExecutionPolicy.DIRECT_TURN,
    )
    assert start.status == "authorizing"

    status = await service.complete(
        start.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?code=x&state=callback-state",
    )

    assert status.status == "connected"
    assert status.provider_scopes == ("issues.write",)
    assert status.execution_policy is PluginExecutionPolicy.DIRECT_TURN
    assert status.capability_ceiling == ("tool:create_issue",)
    assert tuple(status.allowed_tool_schemas) == ("create_issue",)
    assert broker.prepared_calls == 2
    assert broker.transport_calls >= 1
    assert installed["plugin_registry"].get(TRACKER_PLUGIN).enabled is False


@pytest.mark.anyio
async def test_selected_capability_is_bound_through_oauth_and_runtime_authority(
    tmp_path: Path,
) -> None:
    service, _broker, installed = _remote_service(tmp_path)
    descriptor = descriptor_from_installed(
        installed["app_registry"],
        installed["package_registry"],
        TRACKER_APP,
    )
    schema_digest = descriptor.declared_capabilities["tool:create_issue"]

    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        scopes=full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)["scopes"],
        capability_ceiling=("tool:create_issue",),
        allowed_tool_schemas={"create_issue": schema_digest},
    )
    status = await service.complete(
        start.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?code=x&state=callback-state",
    )

    assert start.capability_ceiling == ("tool:create_issue",)
    assert start.allowed_tool_schemas == {"create_issue": schema_digest}
    assert status.capability_ceiling == ("tool:create_issue",)
    assert status.allowed_tool_schemas == {"create_issue": schema_digest}


@pytest.mark.anyio
async def test_prepared_activation_rejects_tampered_selected_context_before_commit(
    tmp_path: Path,
) -> None:
    service, broker, _installed = _remote_service(tmp_path)
    grant = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        scopes=("issues.write",),
        capability_ceiling=grant["capability_ceiling"],
        allowed_tool_schemas=grant["allowed_tool_schemas"],
        execution_policy=PluginExecutionPolicy.DIRECT_TURN,
    )
    prepared = await broker.complete(
        profile_id=PROFILE_ID,
        callback_uri=(
            "http://127.0.0.1/oauth/northwind-oauth/callback?code=x&state=callback-state"
        ),
    )
    drifted_context = dict(prepared.attempt_context)
    drifted_context["execution_policy"] = PluginExecutionPolicy.CONFIRM_EACH.value

    with pytest.raises(ValueError, match="^plugin_connection_drift$"):
        await service.activate_prepared(replace(prepared, attempt_context=drifted_context))

    assert broker.committed is False
    assert broker.prepared_calls == 0


@pytest.mark.anyio
async def test_status_is_read_only_and_execution_refresh_evicts_stale_tools(tmp_path: Path) -> None:
    service, broker, _installed, shared_client = _shared_client_remote_service(tmp_path)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        start.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?code=x&state=callback-state",
    )
    shared_client.tool_documents = ()

    status = await service.status(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)

    assert status.status == "connected"
    with pytest.raises(ValueError, match="^plugin_capability_invalid$"):
        await service.ensure_client(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
        )
    status = await service.status(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)
    assert status.status == "installed_disconnected"


@pytest.mark.anyio
async def test_connection_and_profile_drift_drop_connected_state(tmp_path: Path) -> None:
    service, broker, _installed = _remote_service(tmp_path)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        start.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?code=x&state=callback-state",
    )
    original = service._descriptor_provider
    service._descriptor_provider = lambda plugin_id, app_id: replace(
        original(plugin_id, app_id), capability_revision="9" * 64
    )

    status = await service.status(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)

    assert status.status == "installed_disconnected"
    with pytest.raises(ValueError, match="^plugin_connection_drift$"):
        await service.ensure_client(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
        )
    assert broker.committed is True


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        ("oauth_reauthorization_required", "reauthorization_required"),
        ("oauth_provider_unreachable", "connected"),
    ],
)
async def test_remote_authorization_failures_preserve_their_state(
    tmp_path: Path,
    error: str,
    expected: str,
) -> None:
    service, broker, _installed = _remote_service(tmp_path)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        start.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?code=x&state=callback-state",
    )
    broker.transport_error = error

    status = await service.status(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)
    assert status.status == "connected"
    with pytest.raises(ValueError, match=f"^{error}$"):
        await service.ensure_client(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
        )
    status = await service.status(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)
    assert status.status == expected
    if error == "oauth_provider_unreachable":
        broker.transport_error = None
        await service.prepare_authority_context(
            TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY
        )
    else:
        with pytest.raises(ValueError, match="^plugin_authorization_required$"):
            await service.ensure_client(
                TRACKER_PLUGIN,
                app_id=TRACKER_APP,
                account_key=ACCOUNT_KEY,
            )


@pytest.mark.anyio
async def test_authorization_failure_leaves_verified_package_installed_and_disabled(
    tmp_path: Path,
) -> None:
    service, broker, installed = _remote_service(tmp_path)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    registry = installed["plugin_registry"]
    registry.set_enabled(
        TRACKER_PLUGIN,
        True,
        expected_revision=registry.snapshot().revision,
    )
    broker.complete_error = "oauth_authorization_denied"

    with pytest.raises(ValueError, match="^oauth_authorization_denied$"):
        await service.complete(
            start.request_id or "",
            "http://127.0.0.1/oauth/northwind-oauth/callback?error=denied",
        )

    plugin = installed["plugin_registry"].get(TRACKER_PLUGIN)
    assert plugin.status == "installed"
    assert plugin.enabled is False
    status = await service.status(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)
    assert status.status == "installed_disconnected"


@pytest.mark.anyio
async def test_non_oauth_local_plugin_uses_only_worker_discovery_path(
    tmp_path: Path,
) -> None:
    installed = install_signed_northwind_plugins(tmp_path, peers=("calendar",))
    installed["package_registry"].set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=installed["package_registry"].snapshot().revision,
    )
    broker = _Broker()
    local_client = _Client(_tool_documents("calendar"))
    local_calls: list[str] = []

    async def local_connector(plugin_id: str, _record):
        local_calls.append(plugin_id)
        return local_client

    grants = _GrantService(broker)
    grants.authority_store = PluginAuthorityStore(
        path=tmp_path / "local-direct-authority.json"
    )
    worker = _DiscoveryWorker()
    service = PluginConnectionService(
        app_registry=installed["app_registry"],
        plugin_registry=installed["plugin_registry"],
        broker=broker,
        grant_service=grants,
        descriptor_provider=lambda _plugin_id, app_id: descriptor_from_installed(
            installed["app_registry"], installed["package_registry"], app_id
        ),
        requirement_provider=lambda _plugin_id, _record: (
            installed["plugin_registry"]
            .reverify_installed(CALENDAR_PLUGIN)
            .manifest.required_apps[0]
        ),
        offered_scopes_provider=lambda _plugin_id, _app_id: (_ for _ in ()).throw(
            AssertionError("local path requested oauth scopes")
        ),
        operation_submitter=_direct_submit,
        local_connector=local_connector,
        package_registry=installed["package_registry"],
        transport_authority=object(),
        execution_worker=worker,
    )

    start = await service.start(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, CALENDAR_PLUGIN, CALENDAR_APP),
    )
    status = await service.status(CALENDAR_PLUGIN, app_id=CALENDAR_APP, account_key=ACCOUNT_KEY)
    capability_id = str(local_client.tool_documents[0]["name"])
    preparation = await service.prepare_execution_worker(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        capability_kind="tool",
        capability_id=capability_id,
    )
    assert start.status == "connected"
    assert status.status == "connected"
    assert status.provider_scopes == ()
    assert local_calls == []
    assert broker.request is None
    assert preparation.credential == {}
    assert preparation.local_execution is not None
    assert preparation.descriptor.verified_launch_sha256 == (
        installed["package_registry"]
        .reverify_installed(CALENDAR_APP)
        .manifest.artifact_sha256
    )
    durable = service.authority_store.connection(
        CALENDAR_PLUGIN,
        CALENDAR_APP,
        ACCOUNT_DIGEST,
    )
    assert preparation.authority.connection_revision == durable.connection_revision
    assert preparation.authority.authority_revision == durable.revision


@pytest.mark.anyio
async def test_local_worker_authority_never_launches_provider_in_parent(
    tmp_path: Path,
) -> None:
    installed = install_signed_northwind_plugins(tmp_path, peers=("calendar",))
    installed["package_registry"].set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=installed["package_registry"].snapshot().revision,
    )
    broker = _Broker()
    grants = _GrantService(broker)
    grants.authority_store = PluginAuthorityStore(
        path=tmp_path / "local-worker-authority.json"
    )
    service = PluginConnectionService(
        app_registry=installed["app_registry"],
        plugin_registry=installed["plugin_registry"],
        package_registry=installed["package_registry"],
        broker=broker,
        grant_service=grants,
        descriptor_provider=lambda _plugin_id, app_id: descriptor_from_installed(
            installed["app_registry"], installed["package_registry"], app_id
        ),
        requirement_provider=lambda _plugin_id, _record: (
            installed["plugin_registry"]
            .reverify_installed(CALENDAR_PLUGIN)
            .manifest.required_apps[0]
        ),
        offered_scopes_provider=lambda _plugin_id, _app_id: (_ for _ in ()).throw(
            AssertionError("local worker authority requested oauth scopes")
        ),
        operation_submitter=_direct_submit,
        local_connector=None,
        transport_authority=object(),
        execution_worker=_DiscoveryWorker(),
    )

    started = await service.start(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, CALENDAR_PLUGIN, CALENDAR_APP),
    )
    preparation = await service.prepare_execution_worker(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        capability_kind="tool",
        capability_id=next(
            iter(
                descriptor_from_installed(
                    installed["app_registry"],
                    installed["package_registry"],
                    CALENDAR_APP,
                ).signed_capability_authority["tools"]
            )
        ),
    )

    assert started.status == "connected"
    assert preparation.credential == {}
    assert preparation.local_execution is not None
    assert broker.request is None


@pytest.mark.anyio
async def test_local_candidate_probe_uses_exact_candidate_worker_envelope(
    tmp_path: Path,
) -> None:
    installed = install_signed_northwind_plugins(tmp_path, peers=("calendar",))
    installed["package_registry"].set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=installed["package_registry"].snapshot().revision,
    )
    verified, result = stage_signed_northwind_candidate(
        tmp_path,
        installed,
        plugin_id=CALENDAR_PLUGIN,
        peer="calendar",
    )
    binding = result.record.app_binding
    assert binding is not None
    broker = _Broker()
    grants = _GrantService(broker)
    grants.authority_store = PluginAuthorityStore(
        path=tmp_path / "local-candidate-authority.json"
    )
    worker = _DiscoveryWorker()
    service = PluginConnectionService(
        app_registry=installed["app_registry"],
        plugin_registry=installed["plugin_registry"],
        package_registry=installed["package_registry"],
        broker=broker,
        grant_service=grants,
        descriptor_provider=lambda _plugin_id, app_id: descriptor_from_installed(
            installed["app_registry"], installed["package_registry"], app_id
        ),
        requirement_provider=lambda _plugin_id, _record: (
            installed["plugin_registry"]
            .reverify_installed(CALENDAR_PLUGIN)
            .manifest.required_apps[0]
        ),
        offered_scopes_provider=lambda _plugin_id, _app_id: (),
        operation_submitter=_direct_submit,
        transport_authority=object(),
        execution_worker=worker,
    )

    probe = await service.probe_candidate(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        candidate_verified=verified,
        candidate_binding=binding,
        account_key=ACCOUNT_KEY,
        expected_registry_revision=result.revision,
        **_full_candidate_grant(
            service,
            CALENDAR_PLUGIN,
            CALENDAR_APP,
            candidate_verified=verified,
            candidate_binding=binding,
            expected_registry_revision=result.revision,
        ),
    )

    assert probe.status == "connected"
    assert len(probe.connection_revision) == 64


@pytest.mark.anyio
async def test_non_oauth_local_plugin_disconnects_without_an_oauth_grant(
    tmp_path: Path,
) -> None:
    installed = install_signed_northwind_plugins(tmp_path, peers=("calendar",))
    installed["package_registry"].set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=installed["package_registry"].snapshot().revision,
    )
    broker = _Broker()
    grants = _GrantService(broker)
    grants.authority_store = PluginAuthorityStore(
        path=tmp_path / "local-disconnect-authority.json"
    )
    service = PluginConnectionService(
        app_registry=installed["app_registry"],
        plugin_registry=installed["plugin_registry"],
        broker=broker,
        grant_service=grants,
        descriptor_provider=lambda _plugin_id, app_id: descriptor_from_installed(
            installed["app_registry"], installed["package_registry"], app_id
        ),
        requirement_provider=lambda _plugin_id, _record: (
            installed["plugin_registry"]
            .reverify_installed(CALENDAR_PLUGIN)
            .manifest.required_apps[0]
        ),
        offered_scopes_provider=lambda _plugin_id, _app_id: (),
        operation_submitter=_direct_submit,
        package_registry=installed["package_registry"],
        transport_authority=object(),
        execution_worker=_DiscoveryWorker(),
    )
    await service.start(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, CALENDAR_PLUGIN, CALENDAR_APP),
    )

    await service.revoke(CALENDAR_PLUGIN, app_id=CALENDAR_APP, account_key=ACCOUNT_KEY)

    status = await service.status(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
    )
    # The worker owns the local provider client end to end; the parent never
    # holds a closeable client of its own to assert against (spec: "no
    # parent-process provider I/O").
    assert status.status == "installed_disconnected"


@pytest.mark.anyio
async def test_local_status_is_read_only_and_execution_refreshes_cleanly(
    tmp_path: Path,
) -> None:
    """A credentialless local connection's status is cached; execution refreshes it.

    The worker derives local discovery from the package's own signed
    capability authority (spec: locally reverified signed metadata only),
    not from a live-observed client, so a stale *live* capability set is no
    longer a reachable failure mode for a local connection once the
    execution worker is mandatory; `ensure_client` still performs the real
    live refresh rather than trusting the cached status.
    """

    installed = install_signed_northwind_plugins(tmp_path, peers=("calendar",))
    installed["package_registry"].set_enabled(
        CALENDAR_APP,
        True,
        expected_revision=installed["package_registry"].snapshot().revision,
    )
    broker = _Broker()
    service = PluginConnectionService(
        app_registry=installed["app_registry"],
        plugin_registry=installed["plugin_registry"],
        broker=broker,
        grant_service=_GrantService(broker),
        descriptor_provider=lambda _plugin_id, app_id: descriptor_from_installed(
            installed["app_registry"], installed["package_registry"], app_id
        ),
        requirement_provider=lambda _plugin_id, _record: (
            installed["plugin_registry"]
            .reverify_installed(CALENDAR_PLUGIN)
            .manifest.required_apps[0]
        ),
        offered_scopes_provider=lambda _plugin_id, _app_id: (),
        operation_submitter=_direct_submit,
        package_registry=installed["package_registry"],
        transport_authority=object(),
        execution_worker=_DiscoveryWorker(),
    )
    await service.start(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, CALENDAR_PLUGIN, CALENDAR_APP),
    )

    cached = await service.status(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
    )
    assert cached.status == "connected"

    # `ensure_client` performs the real live refresh (not the cached
    # status), and a worker-owned local connection has no client of its own
    # to hand back to the parent -- that is the correct, unchanged contract
    # (spec: "no parent-process provider I/O"), not a regression here.
    with pytest.raises(ValueError, match="^plugin_local_connection_unavailable$"):
        await service.ensure_client(
            CALENDAR_PLUGIN,
            app_id=CALENDAR_APP,
            account_key=ACCOUNT_KEY,
        )

    refreshed = await service.status(
        CALENDAR_PLUGIN,
        app_id=CALENDAR_APP,
        account_key=ACCOUNT_KEY,
    )
    assert refreshed.status == "connected"


@pytest.mark.anyio
async def test_failed_remote_oauth_never_falls_back_to_local_connector(tmp_path: Path) -> None:
    service, _broker, _installed = _remote_service(tmp_path)
    local_calls: list[str] = []

    async def local_connector(plugin_id: str, _record):
        local_calls.append(plugin_id)
        raise AssertionError("remote oauth used local fallback")

    service._profile_match_provider = None
    service._local_connector = local_connector

    with pytest.raises(ValueError, match="^oauth_profile_catalog_unavailable$"):
        await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
        )
    assert local_calls == []


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changed",
    ["scopes", "policy", "constraints", "context"],
)
async def test_connected_fast_path_retires_eligibility_for_any_activation_change(
    tmp_path: Path,
    changed: str,
) -> None:
    service, broker, _installed = _remote_service(tmp_path)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        start.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
    )
    key = (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    stale = service._sessions[key]
    kwargs: dict[str, object] = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    if changed == "scopes":
        kwargs["scopes"] = ("issues.write",)
    elif changed == "policy":
        kwargs["execution_policy"] = PluginExecutionPolicy.DIRECT_TURN
    elif changed == "constraints":
        kwargs["source_constraints"] = {"workspace": "signed"}
    else:
        original = service._descriptor_provider
        service._descriptor_provider = lambda plugin_id, app_id: replace(
            original(plugin_id, app_id),
            capability_revision="9" * 64,
        )

    restarted = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **kwargs,
    )

    assert restarted.status == "authorizing"
    # The worker owns the provider client end to end; the retired session's
    # eviction from `_sessions` below is the meaningful, still-observable
    # signal (spec: "no parent-process provider I/O").
    assert key not in service._sessions
    assert (
        service._grants.current(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key_sha256=ACCOUNT_DIGEST,
        )
        is None
    )
    assert broker.committed is True


@pytest.mark.anyio
async def test_connected_fast_path_reprobes_and_evicts_an_empty_live_snapshot(
    tmp_path: Path,
) -> None:
    service, _broker, _installed, shared_client = _shared_client_remote_service(tmp_path)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        start.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
    )
    key = (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    shared_client.tool_documents = ()

    with pytest.raises(ValueError, match="^plugin_capability_invalid$"):
        await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
        )

    assert key not in service._sessions


@pytest.mark.anyio
async def test_read_only_status_defers_stale_remote_eviction_to_execution(tmp_path: Path) -> None:
    service, _broker, _installed, shared_client = _shared_client_remote_service(tmp_path)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        start.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
    )
    key = (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    shared_client.tool_documents = ()

    status = await service.status(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)

    assert status.status == "connected"
    assert key in service._sessions
    with pytest.raises(ValueError, match="^plugin_capability_invalid$"):
        await service.ensure_client(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
        )
    assert key not in service._sessions
    assert (
        await service.status(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)
    ).status == "installed_disconnected"


@pytest.mark.anyio
async def test_prepared_probe_binds_the_generic_capability_surface(
    tmp_path: Path,
) -> None:
    service, broker, _installed = _remote_service(tmp_path)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )

    await service.complete(
        start.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
    )

    descriptor = service._descriptor(TRACKER_PLUGIN, TRACKER_APP)
    material = {
        "state_sha256": STATE,
        "descriptor": descriptor.to_dict(),
        "kind": "prepared_probe",
    }
    binding = broker.prepared_bindings[-1]
    assert binding.capability_binding_revision == sha256_hex(
        canonical_bytes({**material, "revision": "capabilities"})
    )
    assert binding.capability_binding_revision != sha256_hex(
        canonical_bytes({**material, "revision": "tools"})
    )


@pytest.mark.anyio
async def test_descriptor_drift_during_prepared_probe_never_commits(tmp_path: Path) -> None:
    service, broker, _installed = _remote_service(tmp_path)
    service._execution_worker = _ServiceClientDiscoveryWorker(service)
    original = service._descriptor_provider

    def drift() -> None:
        service._descriptor_provider = lambda plugin_id, app_id: replace(
            original(plugin_id, app_id),
            capability_revision="9" * 64,
        )

    service._client_factory = lambda _descriptor, _authorization: _Client(
        _tool_documents("tracker"),
        snapshot_hook=drift,
    )
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )

    with pytest.raises(ValueError, match="^plugin_connection_drift$"):
        await service.complete(
            start.request_id or "",
            "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
        )

    assert broker.committed is False
    assert broker.prepared_calls == 1


@pytest.mark.anyio
async def test_package_drift_during_prepared_probe_never_commits(tmp_path: Path) -> None:
    service, broker, installed = _remote_service(tmp_path)
    registry = installed["plugin_registry"]
    original_reverify = registry.reverify_installed
    drifted = False

    def reverify(plugin_id: str):
        verified = original_reverify(plugin_id)
        if not drifted:
            return verified
        return replace(verified, package_sha256="9" * 64)

    def drift() -> None:
        nonlocal drifted
        drifted = True

    registry.reverify_installed = reverify
    service._execution_worker = _ServiceClientDiscoveryWorker(service)
    service._client_factory = lambda _descriptor, _authorization: _Client(
        _tool_documents("tracker"),
        snapshot_hook=drift,
    )
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )

    with pytest.raises(ValueError, match="^plugin_connection_drift$"):
        await service.complete(
            start.request_id or "",
            "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
        )

    assert broker.committed is False
    assert broker.prepared_calls == 1


@pytest.mark.anyio
async def test_restart_recovers_nondefault_prepared_attempt_settings(tmp_path: Path) -> None:
    service, broker, installed = _remote_service(tmp_path)
    grant = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        scopes=("issues.write",),
        capability_ceiling=grant["capability_ceiling"],
        allowed_tool_schemas=grant["allowed_tool_schemas"],
        execution_policy=PluginExecutionPolicy.DIRECT_TURN,
        source_constraints={"workspace": "signed"},
    )
    prepared = await broker.complete(
        profile_id=PROFILE_ID,
        callback_uri=("http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state"),
    )
    broker.recovered = (prepared,)
    restarted, _broker, _installed = _remote_service(
        tmp_path,
        installed=installed,
        broker=broker,
        grants=service._grants,
    )

    status = await restarted.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )

    assert start.request_id == STATE
    # A recovered prepared attempt is finalizing, not yet connected: the signed
    # settings must survive the restart without the attempt being published as a
    # completed connection.
    assert status.status == "finalizing"
    assert status.provider_scopes == ("issues.write",)
    assert status.execution_policy is PluginExecutionPolicy.DIRECT_TURN
    assert status.source_constraints == {"workspace": "signed"}


@pytest.mark.anyio
async def test_restart_discards_terminal_prepared_attempt_instead_of_poisoning_status(
    tmp_path: Path,
) -> None:
    service, broker, installed = _remote_service(tmp_path)
    await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    prepared = await broker.complete(
        profile_id=PROFILE_ID,
        callback_uri=("http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state"),
    )
    broker.recovered = (prepared,)
    broker.committed_states.add(prepared.state_sha256)
    restarted, _broker, _installed = _remote_service(
        tmp_path,
        installed=installed,
        broker=broker,
        grants=service._grants,
    )

    status = await restarted.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )
    repeated = await restarted.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )

    assert status.status == repeated.status == "installed_disconnected"
    assert broker.discarded_prepared == [prepared.state_sha256]


@pytest.mark.anyio
async def test_disconnect_cancels_authorizing_remote_without_a_grant(tmp_path: Path) -> None:
    service, _broker, _installed = _remote_service(tmp_path)
    await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )

    await service.revoke(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )
    status = await service.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )

    assert status.status == "installed_disconnected"


@pytest.mark.anyio
async def test_credentialless_disconnect_does_not_require_the_oauth_vault(tmp_path: Path) -> None:
    service, broker, _installed = _remote_service(tmp_path, authentication_kind="none")
    await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )

    def locked_vault(*_args, **_kwargs):
        raise ValueError("oauth_vault_locked")

    broker.discard_connection_attempts = locked_vault
    await service.revoke(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)
    assert (await service.status(
        TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY,
    )).status == "installed_disconnected"


@pytest.mark.anyio
@pytest.mark.parametrize("prepared_before_restart", [False, True])
async def test_disconnect_consumes_unfinished_sign_in_across_restart(
    tmp_path: Path,
    prepared_before_restart: bool,
) -> None:
    profile = _real_profile()
    clock = _RealClock()
    protocol = _RealProtocol()
    vault = OAuthCredentialVault(
        tmp_path / "oauth-vault",
        key_provider=lambda: b"v" * 32,
        clock=clock,
    )
    broker = OAuthBroker(
        registry=_ExactProfileRegistry(profile),
        vault=vault,
        protocol=protocol,
        clock=clock,
    )
    service, _, installed = _remote_service(tmp_path, broker=broker)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    callback = _callback_uri(start, profile)
    if prepared_before_restart:
        await broker.complete(profile_id=PROFILE_ID, callback_uri=callback)
        # Reproduce a token received before a failed discovery, then left overnight.
        clock.value = protocol.exchange_expires_at + 1
    restarted, _, _ = _remote_service(
        tmp_path,
        installed=installed,
        broker=broker,
        grants=service._grants,
    )

    await restarted.revoke(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)

    status = await restarted.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )
    assert status.status == "installed_disconnected"
    assert broker.recover()[0] == ()
    with pytest.raises(ValueError, match="^oauth_callback_replayed$"):
        await broker.complete(profile_id=PROFILE_ID, callback_uri=callback)
    restarted_again, _, _ = _remote_service(
        tmp_path,
        installed=installed,
        broker=broker,
        grants=service._grants,
    )
    assert (
        await restarted_again.status(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
        )
    ).status == "installed_disconnected"
    new_start = await restarted_again.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(restarted_again, TRACKER_PLUGIN, TRACKER_APP),
    )
    assert new_start.status == "authorizing"
    assert new_start.request_id != start.request_id


@pytest.mark.anyio
async def test_two_same_profile_attempts_keep_settings_isolated_by_state(tmp_path: Path) -> None:
    service, _broker, _installed = _remote_service(tmp_path)
    grant = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    first = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        scopes=("issues.write",),
        capability_ceiling=grant["capability_ceiling"],
        allowed_tool_schemas=grant["allowed_tool_schemas"],
        execution_policy=PluginExecutionPolicy.DIRECT_TURN,
    )
    second = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key="second-account",
        scopes=("issues.read",),
        capability_ceiling=grant["capability_ceiling"],
        allowed_tool_schemas=grant["allowed_tool_schemas"],
        source_constraints={"tenant": "second"},
    )

    first_status = await service.complete(
        first.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
    )
    second_status = await service.complete(
        second.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state-two",
    )

    assert first.request_id == STATE
    assert second.request_id == STATE_TWO
    assert first_status.provider_scopes == ("issues.write",)
    assert first_status.execution_policy is PluginExecutionPolicy.DIRECT_TURN
    assert second_status.provider_scopes == ("issues.read",)
    assert second_status.source_constraints == {"tenant": "second"}


@pytest.mark.anyio
async def test_duplicate_callback_is_serialized_and_returns_current_activation(
    tmp_path: Path,
) -> None:
    service, broker, _installed = _remote_service(tmp_path)
    start = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )

    first, second = await asyncio.gather(
        service.complete(
            start.request_id or "",
            "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
        ),
        service.complete(
            start.request_id or "",
            "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
        ),
    )

    assert first == second
    assert first.status == "connected"
    assert broker.complete_calls == 1


@pytest.mark.anyio
@pytest.mark.parametrize("transition", ["revoke", "replacement"])
async def test_lifecycle_probe_serializes_revoke_or_session_replacement(
    tmp_path: Path,
    transition: str,
) -> None:
    """A concurrent revoke/restart must wait for an in-flight ensure_client probe.

    The worker owns the provider client end to end (spec: "no parent-process
    provider I/O"), so there is no parent-held session client left to pause
    or to observe closing.  The probe is paused inside the worker double's
    own live discovery call instead -- the discovery worker still runs
    through the caller's ``client_factory``, so pausing there exercises the
    same ``_lifecycle_lock`` serialization the coordinator depends on.
    """

    probe_entered = asyncio.Event()
    probe_release = asyncio.Event()

    async def pause_discovery() -> None:
        probe_entered.set()
        await probe_release.wait()

    shared_client = _Client(_tool_documents("tracker"))
    service, broker, _installed = _remote_service(
        tmp_path,
        client_factory=lambda _descriptor, _authorization: shared_client,
    )
    grant = full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP)
    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **grant,
    )
    await service.complete(
        started.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
    )
    # The initial connect above must run its own live discovery unpaused;
    # only the explicit `ensure_client` probe below should block.
    shared_client.snapshot_hook = pause_discovery
    key = (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    probe_task = asyncio.create_task(
        service.ensure_client(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)
    )
    await asyncio.wait_for(probe_entered.wait(), timeout=1)
    if transition == "revoke":
        transition_task = asyncio.create_task(
            service.revoke(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)
        )
    else:
        transition_task = asyncio.create_task(
            service.start(
                TRACKER_PLUGIN,
                app_id=TRACKER_APP,
                account_key=ACCOUNT_KEY,
                scopes=("issues.write",),
                capability_ceiling=grant["capability_ceiling"],
                allowed_tool_schemas=grant["allowed_tool_schemas"],
            )
        )
    await asyncio.sleep(0)

    assert transition_task.done() is False
    probe_release.set()
    result = await probe_task
    assert result is not None
    if transition == "revoke":
        assert (
            service._grants.current(
                TRACKER_PLUGIN,
                app_id=TRACKER_APP,
                account_key_sha256=ACCOUNT_DIGEST,
            )
            is None
        )
    transitioned = await transition_task
    if transition == "replacement":
        assert transitioned.status == "authorizing"
    else:
        status = await service.status(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)
        assert status.status == "installed_disconnected"
    assert key not in service._sessions


@pytest.mark.anyio
async def test_failed_session_close_leaves_durable_revoke_pending_for_restart(
    tmp_path: Path,
) -> None:
    service, broker, installed = _remote_service(tmp_path)
    started = await service.start(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
        **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
    )
    await service.complete(
        started.request_id or "",
        "http://127.0.0.1/oauth/northwind-oauth/callback?state=callback-state",
    )
    key = (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
    active = service._sessions[key]

    async def failing_close() -> None:
        raise RuntimeError("close_failed")

    # The worker owns the provider client end to end, so the parent-held
    # session client is always the `_WorkerOnlyLocalClient` marker; this
    # overrides that exact instance's close to simulate the same close
    # failure a real session teardown could still hit.
    active.client.close = failing_close

    with pytest.raises(RuntimeError, match="^close_failed$"):
        await service.revoke(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)

    assert key not in service._sessions
    assert (
        service._grants.current(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key_sha256=ACCOUNT_DIGEST,
        )
        is None
    )
    assert key in service._grants._pending
    restarted, _broker, _installed = _remote_service(
        tmp_path,
        installed=installed,
        broker=broker,
        grants=service._grants,
    )

    pending_status = await restarted.status(
        TRACKER_PLUGIN,
        app_id=TRACKER_APP,
        account_key=ACCOUNT_KEY,
    )

    assert pending_status.status == "revocation_pending"

    await restarted.revoke(TRACKER_PLUGIN, app_id=TRACKER_APP, account_key=ACCOUNT_KEY)

    assert service._grants._pending == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "authentication_kind",
    ("oauth_authorization_code_pkce", "oauth_device_code"),
    ids=("browser", "device"),
)
@pytest.mark.parametrize("candidate_kind", ("changed", "added"))
@pytest.mark.parametrize("predecessor_connected", (True, False), ids=("connected", "disconnected"))
async def test_real_signed_authenticated_candidate_stays_dormant_until_final_promotion(
    tmp_path: Path,
    authentication_kind: str,
    candidate_kind: str,
    predecessor_connected: bool,
) -> None:
    """Exercise browser OAuth with real registries, vault, grant store, and provisioner."""

    installed = install_signed_northwind_plugins(
        tmp_path,
        peers=("tracker",),
        tracker_authentication_kind=authentication_kind,
        tracker_remote_origin=f"{TRACKER_ORIGIN}/mcp/v1",
    )
    authorization_flow = (
        OAuthAuthorizationFlow.DEVICE_CODE
        if authentication_kind == "oauth_device_code"
        else OAuthAuthorizationFlow.AUTHORIZATION_CODE_PKCE
    )
    profile = _real_profile(
        resource_origin=f"{TRACKER_ORIGIN}/mcp/v1",
        authorization_flow=authorization_flow,
    )
    clock = _RealClock()
    protocol = _RealProtocol()
    vault = OAuthCredentialVault(
        tmp_path / "oauth-vault",
        key_provider=lambda: b"v" * 32,
        clock=clock,
    )
    broker = OAuthBroker(
        registry=_ExactProfileRegistry(profile),
        vault=vault,
        protocol=protocol,
        clock=clock,
    )
    permissions = SimpleNamespace(invalidate_plugin=lambda _plugin_id: 0)
    authority_store = PluginAuthorityStore(path=tmp_path / "authority.json")
    grants = PluginGrantService(
        broker=broker,
        authority_store=authority_store,
        permission_store=permissions,
        journal_path=tmp_path / "grants.json",
    )
    registry = installed["plugin_registry"]
    app_registry = installed["app_registry"]
    package_registry = installed["package_registry"]
    provisioner = installed["installer"].provisioner

    def live_descriptor(_plugin_id: str, app_id: str):
        record = app_registry.get(app_id)
        authority = signed_capability_authority_for_candidate(
            package_registry,
            record,
            registry.reverify_installed(TRACKER_PLUGIN),
        )
        return descriptor_for_record(record, signed_capability_authority=authority)

    service = PluginConnectionService(
        app_registry=app_registry,
        plugin_registry=registry,
        broker=broker,
        grant_service=grants,
        descriptor_provider=live_descriptor,
        candidate_descriptor_provider=lambda verified, binding, app_id: descriptor_for_record(
            provisioner.candidate_record(binding.dependency_receipt_id, app_id),
            signed_capability_authority=signed_capability_authority_for_candidate(
                package_registry,
                provisioner.candidate_record(binding.dependency_receipt_id, app_id),
                verified,
            ),
        ),
        candidate_record_provider=lambda _verified, binding, app_id: (
            provisioner.candidate_record(binding.dependency_receipt_id, app_id)
        ),
        requirement_provider=lambda _plugin_id, _record: registry.reverify_installed(
            TRACKER_PLUGIN
        ).manifest.required_apps[0],
        offered_scopes_provider=lambda _plugin_id, _app_id: SCOPES,
        profile_match_provider=_profile_match,
        operation_submitter=_direct_submit,
        client_factory=lambda _descriptor, _authorization: _Client(
            _tool_documents("tracker")
        ),
        execution_worker=_DiscoveryWorker(),
    )

    old_authority = None
    old_session = None
    if predecessor_connected:
        old_start = await service.start(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            **full_connection_grant(service, TRACKER_PLUGIN, TRACKER_APP),
        )
        if authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE:
            with pytest.raises(ValueError, match="oauth_device_authorization_pending"):
                await service.complete_device(old_start.request_id or "")
            clock.value += 5
            old_status = await service.complete_device(old_start.request_id or "")
            if old_status.status == "finalizing":
                old_status = await service.complete_device(old_start.request_id or "")
            assert old_status.status == "connected"
        else:
            await service.complete(
                old_start.request_id or "",
                _callback_uri(old_start, profile),
            )
        old_authority = authority_store.connection(
            TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST,
        )
        old_session = service._sessions[(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)]
    else:
        registry.set_enabled(
            TRACKER_PLUGIN, False, expected_revision=registry.snapshot().revision,
        )

    def assert_predecessor_unchanged() -> None:
        assert registry.get(TRACKER_PLUGIN).selected_version == "1.0.0"
        assert registry.get(TRACKER_PLUGIN).enabled is predecessor_connected
        if predecessor_connected:
            assert authority_store.connection(
                TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST,
            ) == old_authority
        else:
            with pytest.raises(ValueError, match="plugin_connection_authority_not_found"):
                authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
            assert grants.current(
                TRACKER_PLUGIN, app_id=TRACKER_APP, account_key_sha256=ACCOUNT_DIGEST,
            ) is None
        assert service._sessions.get((TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)) is old_session

    if authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE:
        protocol.device_poll_calls = 0

    candidate_app_id = (
        TRACKER_APP if candidate_kind == "changed" else "northwind.tracker-v2.mcp"
    )
    verified, result = stage_signed_northwind_candidate(
        tmp_path,
        installed,
        tracker_authentication_kind=authentication_kind,
        tracker_remote_origin=f"{TRACKER_ORIGIN}/mcp/v1",
        tracker_scopes=("issues.read", "issues.write", "issues.admin"),
        tracker_app_id=candidate_app_id,
    )
    binding = PluginAppBinding(
        required_app_ids=(candidate_app_id,),
        optional_app_ids=(),
        dependency_receipt_id=result.dependency_receipt.receipt_id,
        revision=sha256_hex(
            canonical_bytes(
                {
                    "required_app_ids": [candidate_app_id],
                    "optional_app_ids": [],
                    "dependency_receipt_id": result.dependency_receipt.receipt_id,
                }
            )
        ),
    )
    candidate_grant = _full_candidate_grant(
        service,
        TRACKER_PLUGIN,
        candidate_app_id,
        candidate_verified=verified,
        candidate_binding=binding,
        expected_registry_revision=result.revision,
    )
    blocked = registry.block(
        TRACKER_PLUGIN, "permission_review_required", expected_revision=result.revision,
    )
    with pytest.raises(ValueError, match="^plugin_update_failed$"):
        await service.start_candidate(
            TRACKER_PLUGIN,
            app_id=candidate_app_id,
            candidate_verified=verified,
            candidate_binding=binding,
            account_key=ACCOUNT_KEY,
            expected_registry_revision=blocked.revision,
            **candidate_grant,
        )
    unblocked = registry.clear_block(TRACKER_PLUGIN, expected_revision=blocked.revision)
    result = replace(result, revision=unblocked.revision)
    rejected_start = await service.start_candidate(
        TRACKER_PLUGIN,
        app_id=candidate_app_id,
        candidate_verified=verified,
        candidate_binding=binding,
        account_key=ACCOUNT_KEY,
        expected_registry_revision=result.revision,
        **candidate_grant,
    )
    if authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE:
        service.cancel_candidate(
            rejected_start.request_id or "",
            candidate_verified=verified,
            candidate_binding=binding,
        )
    else:
        with pytest.raises(ValueError, match="^oauth_authorization_denied$"):
            await service.complete_candidate(
                rejected_start.request_id or "",
                _callback_uri(rejected_start, profile, error="access_denied"),
                candidate_verified=verified,
                candidate_binding=binding,
                expected_registry_revision=result.revision,
            )
    assert_predecessor_unchanged()

    candidate_start = await service.start_candidate(
        TRACKER_PLUGIN,
        app_id=candidate_app_id,
        candidate_verified=verified,
        candidate_binding=binding,
        account_key=ACCOUNT_KEY,
        expected_registry_revision=result.revision,
        **candidate_grant,
    )

    if authorization_flow is OAuthAuthorizationFlow.DEVICE_CODE:
        with pytest.raises(ValueError, match="oauth_device_authorization_pending"):
            await service.complete_candidate_device(
                candidate_start.request_id or "",
                candidate_verified=verified,
                candidate_binding=binding,
                expected_registry_revision=result.revision,
            )
        clock.value += 5
        prepared = await service.complete_candidate_device(
            candidate_start.request_id or "",
            candidate_verified=verified,
            candidate_binding=binding,
            expected_registry_revision=result.revision,
        )
    else:
        prepared = await service.complete_candidate(
            candidate_start.request_id or "",
            _callback_uri(candidate_start, profile),
            candidate_verified=verified,
            candidate_binding=binding,
            expected_registry_revision=result.revision,
        )
    assert_predecessor_unchanged()

    promoted = registry.promote_candidate(
        TRACKER_PLUGIN,
        verified.manifest.plugin_version,
        expected_revision=result.revision,
        app_binding=binding,
    )
    provisioner.promote_candidate_records(
        result.dependency_receipt.receipt_id,
        expected_revision=app_registry.snapshot().revision,
    )
    connected = await service.activate_candidate(
        prepared,
        candidate_verified=verified,
        expected_registry_revision=promoted.revision,
    )
    if candidate_app_id != TRACKER_APP:
        await service.retire_predecessor(
            TRACKER_PLUGIN,
            app_id=TRACKER_APP,
            account_key=ACCOUNT_KEY,
            previous_record=result.record,
            expected_registry_revision=promoted.revision,
        )

    assert connected.status == "connected"
    assert registry.get(TRACKER_PLUGIN).selected_version == "2.0.0"
    new_authority = authority_store.connection(
        TRACKER_PLUGIN, candidate_app_id, ACCOUNT_DIGEST
    )
    if old_authority is not None:
        assert new_authority.connection_revision != old_authority.connection_revision
    assert service._sessions[
        (TRACKER_PLUGIN, candidate_app_id, ACCOUNT_DIGEST)
    ] is not old_session
    if candidate_app_id != TRACKER_APP:
        with pytest.raises(ValueError, match="plugin_connection_authority_not_found"):
            authority_store.connection(TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST)
        assert (TRACKER_PLUGIN, TRACKER_APP, ACCOUNT_DIGEST) not in service._sessions


# --- discovery-derived (locally sealed) capability authority ------------------


def _weather_discovery_binding(**overrides: object) -> CapabilityDiscoveryBinding:
    fields: dict[str, object] = {
        "plugin_id": "io.github.example.weather",
        "app_id": "io.github.example.weather",
        "publisher_id": "io.github.example",
        "account_key_hash": "a" * 64,
        "endpoint": "https://mcp.weather.example.com/mcp/v1",
        "protocol_version": "2026-07-28",
    }
    fields.update(overrides)
    return CapabilityDiscoveryBinding(**fields)


class _WeatherDiscoveryPeer:
    """A minimal restricted-discovery peer offering one unreviewed tool."""

    endpoint = "https://mcp.weather.example.com/mcp/v1"

    async def negotiate(self, operation: str) -> object:
        if operation == "initialize":
            return {
                "server_id": "io.github.example.weather",
                "server_version": "1.2.0",
                "capabilities": {
                    "tools": {"listChanged": False},
                    "prompts": {"listChanged": False},
                    "resources": {"listChanged": False},
                },
            }
        if operation == "tools/list":
            return [
                {
                    "name": "forecast",
                    "title": "Forecast",
                    "input_schema": {
                        "type": "object",
                        "properties": {},
                        "additionalProperties": False,
                    },
                }
            ]
        return []


@pytest.mark.anyio
async def test_locally_sealed_capability_authority_projects_the_descriptor_shape() -> None:
    """Catches the discovery-derived authority lane drifting from the signed loaders' shape.

    ``descriptor_for_record`` accepts one app's capability authority from a
    signed Omega-verified package today (``signed_capability_authority_from_package``,
    ``signed_capability_authority_for_candidate``). A publisher-verified or
    personal connection instead reviews and locally seals its authority
    through ``RestrictedPluginDiscovery`` (``mcp_platform.plugin_discovery``);
    this proves the projection lands in the exact same
    ``{"tools", "resources", "resource_templates", "prompts"}`` shape those
    two loaders already return.
    """

    trust_root = LocalTrustRoot.from_vault_key(b"k" * 32)
    discovery = RestrictedPluginDiscovery(_WeatherDiscoveryPeer(), trust_root=trust_root)
    review = await discovery.discover(_weather_discovery_binding())
    envelope = discovery.approve(review, ["tool:forecast"])

    authority = locally_sealed_capability_authority(envelope, trust_root)

    assert set(authority) == {"tools", "resources", "resource_templates", "prompts"}
    assert set(authority["tools"]) == {"forecast"}
    assert authority["tools"]["forecast"]["risk_class"] == "external_unreviewed"
    assert authority["resources"] == {}
    assert authority["resource_templates"] == {}
    assert authority["prompts"] == {}


def test_locally_sealed_capability_authority_rejects_anything_but_a_sealed_envelope() -> None:
    trust_root = LocalTrustRoot.from_vault_key(b"k" * 32)
    with pytest.raises(ValueError, match="plugin_catalog_authority_missing"):
        locally_sealed_capability_authority(object(), trust_root)


def test_locally_sealed_capability_authority_rejects_a_fabricated_envelope() -> None:
    """Catches a caller constructing ``LocalCapabilityEnvelope`` directly, bypassing approval.

    The plain constructor must refuse regardless of what fields are handed
    to it -- there is no way to mint a trusted envelope except through
    ``RestrictedPluginDiscovery.approve``.
    """

    with pytest.raises(plugin_discovery_module.PluginDiscoveryError):
        LocalCapabilityEnvelope(
            plugin_id="io.github.example.weather",
            app_id="io.github.example.weather",
            publisher_id="io.github.example",
            account_key_hash="a" * 64,
            endpoint="https://mcp.weather.example.com/mcp/v1",
            protocol_version="2026-07-28",
            server_id="io.github.example.weather",
            server_version="1.2.0",
            scopes=("tool:forecast",),
            capability_authority={
                "tools": {"forecast": {"title": "Forecast", "risk_class": "external_unreviewed"}},
                "resources": {},
                "resource_templates": {},
                "prompts": {},
            },
            capability_schema_sha256="a" * 64,
            mac="QQ==",
            mac_key_sha256="b" * 64,
        )


@pytest.mark.anyio
async def test_locally_sealed_capability_authority_rejects_a_tampered_mac() -> None:
    """Catches the bridge trusting an envelope's content without re-verifying its MAC.

    Even an envelope that legitimately carries the module's own construction
    seal must still fail the bridge if its ``capability_authority`` was
    swapped out from under a genuine ``mac``/``mac_key_sha256`` pair -- the
    seal proves provenance (it came from this module's own code), not that
    the content and the MAC actually agree. Reaching the private
    ``_sealed``/``_ENVELOPE_SEAL`` seam is legitimate white-box testing of
    ``mcp_platform.plugin_discovery``'s own module, not a boundary crossing.
    """

    trust_root = LocalTrustRoot.from_vault_key(b"k" * 32)
    discovery = RestrictedPluginDiscovery(_WeatherDiscoveryPeer(), trust_root=trust_root)
    review = await discovery.discover(_weather_discovery_binding())
    genuine = discovery.approve(review, ["tool:forecast"])

    tampered = plugin_discovery_module.LocalCapabilityEnvelope._sealed(
        plugin_id=genuine.plugin_id,
        app_id=genuine.app_id,
        publisher_id=genuine.publisher_id,
        account_key_hash=genuine.account_key_hash,
        endpoint=genuine.endpoint,
        protocol_version=genuine.protocol_version,
        server_id=genuine.server_id,
        server_version=genuine.server_version,
        scopes=genuine.scopes,
        capability_authority={
            "tools": {"evil": {"title": "Evil", "risk_class": "external_unreviewed"}},
            "resources": {},
            "resource_templates": {},
            "prompts": {},
        },
        capability_schema_sha256=genuine.capability_schema_sha256,
        mac=genuine.mac,
        mac_key_sha256=genuine.mac_key_sha256,
        seal=plugin_discovery_module._ENVELOPE_SEAL,
    )

    with pytest.raises(ValueError, match="plugin_catalog_authority_missing"):
        locally_sealed_capability_authority(tampered, trust_root)

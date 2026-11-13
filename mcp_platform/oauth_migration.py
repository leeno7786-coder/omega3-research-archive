"""Exact, one-time replacement migration for pre-broker OAuth state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from mcp_platform.oauth_vault import (
    OAuthCommittedGrant,
    OAuthCredentialVault,
    OAuthTokenPartition,
)


_ACTIONS = frozenset({"preserve", "delete_and_reauthorize"})


@dataclass(frozen=True, slots=True, repr=False)
class LegacyOAuthRecord:
    source_revision: str
    token_key: str
    plugin_id: str
    client_id: str
    authorization_issuer: str
    resource_origin: str
    redirect_uri: str
    account_partition: str
    scopes: tuple[str, ...]
    profile_revision: str
    plugin_manifest_revision: str
    app_binding_revision: str
    capability_authority_revision: str
    partition: OAuthTokenPartition
    grant: OAuthCommittedGrant

    def __repr__(self) -> str:
        return f"LegacyOAuthRecord(token_key={self.token_key!r}, plugin_id={self.plugin_id!r})"


@dataclass(frozen=True, slots=True, repr=False)
class OAuthMigrationExpectation:
    token_key: str
    client_id: str
    authorization_issuer: str
    resource_origin: str
    redirect_uri: str
    account_partition: str
    scopes: tuple[str, ...]
    profile_revision: str
    plugin_manifest_revision: str
    app_binding_revision: str
    capability_authority_revision: str

    def __repr__(self) -> str:
        return f"OAuthMigrationExpectation(token_key={self.token_key!r})"


@dataclass(frozen=True, slots=True, repr=False)
class OAuthMigrationEvaluation:
    action: str
    token_key: str
    plugin_id: str
    source_revision: str
    _record: LegacyOAuthRecord | None = None

    def __post_init__(self) -> None:
        if self.action not in _ACTIONS:
            raise ValueError("oauth_migration_evaluation_invalid")

    def __repr__(self) -> str:
        return (
            "OAuthMigrationEvaluation("
            f"action={self.action!r}, token_key={self.token_key!r}, "
            f"plugin_id={self.plugin_id!r})"
        )


@dataclass(frozen=True, slots=True)
class OAuthMigrationReceipt:
    migration_id: str
    preserved_token_keys: tuple[str, ...]
    deleted_token_keys: tuple[str, ...]
    disconnected_plugin_ids: tuple[str, ...]
    source_revisions: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: object) -> OAuthMigrationReceipt:
        if type(value) is not dict or set(value) != {
            "migration_id",
            "preserved_token_keys",
            "deleted_token_keys",
            "disconnected_plugin_ids",
            "source_revisions",
        }:
            raise ValueError("oauth_migration_receipt_invalid")
        try:
            receipt = cls(
                migration_id=value["migration_id"],
                preserved_token_keys=tuple(value["preserved_token_keys"]),
                deleted_token_keys=tuple(value["deleted_token_keys"]),
                disconnected_plugin_ids=tuple(value["disconnected_plugin_ids"]),
                source_revisions=tuple(value["source_revisions"]),
            )
        except (KeyError, TypeError):
            raise ValueError("oauth_migration_receipt_invalid") from None
        fields = (
            (receipt.migration_id,),
            receipt.preserved_token_keys,
            receipt.deleted_token_keys,
            receipt.disconnected_plugin_ids,
            receipt.source_revisions,
        )
        if any(
            type(item) is not str or not item or item.strip() != item
            for group in fields
            for item in group
        ):
            raise ValueError("oauth_migration_receipt_invalid")
        return receipt


class LegacyOAuthSource(Protocol):
    def records(self) -> tuple[LegacyOAuthRecord, ...]: ...

    def remove_committed(
        self,
        source_revisions: tuple[str, ...],
        *,
        disconnected_plugin_ids: tuple[str, ...],
    ) -> None: ...


ExpectedContextProvider = Callable[[LegacyOAuthRecord], OAuthMigrationExpectation | None]


def _stable_text(value: object) -> bool:
    return (
        type(value) is str
        and bool(value)
        and value.strip() == value
        and not any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _valid_scopes(value: object) -> bool:
    return (
        type(value) is tuple
        and bool(value)
        and len(value) == len(set(value))
        and all(_stable_text(item) for item in value)
    )


class OAuthBrokerMigration:
    """Import only records whose complete signed/live context remains identical."""

    MIGRATION_ID = "omega_oauth_broker_v1"

    def __init__(
        self,
        *,
        vault: OAuthCredentialVault,
        legacy: LegacyOAuthSource,
        expected: ExpectedContextProvider,
    ) -> None:
        if (
            type(vault) is not OAuthCredentialVault
            or not callable(getattr(legacy, "records", None))
            or not callable(getattr(legacy, "remove_committed", None))
            or not callable(expected)
        ):
            raise ValueError("oauth_migration_configuration_invalid")
        self._vault = vault
        self._legacy = legacy
        self._expected = expected

    @staticmethod
    def _record_shape_is_valid(record: LegacyOAuthRecord) -> bool:
        strings = (
            record.source_revision,
            record.token_key,
            record.plugin_id,
            record.client_id,
            record.authorization_issuer,
            record.resource_origin,
            record.redirect_uri,
            record.account_partition,
            record.profile_revision,
            record.plugin_manifest_revision,
            record.app_binding_revision,
            record.capability_authority_revision,
        )
        return (
            all(_stable_text(value) for value in strings)
            and _valid_scopes(record.scopes)
            and type(record.partition) is OAuthTokenPartition
            and type(record.grant) is OAuthCommittedGrant
        )

    @staticmethod
    def _exact_context(
        record: LegacyOAuthRecord,
        expected: OAuthMigrationExpectation,
    ) -> bool:
        if not _valid_scopes(expected.scopes):
            return False
        public_fields = (
            "token_key",
            "client_id",
            "authorization_issuer",
            "resource_origin",
            "redirect_uri",
            "account_partition",
            "profile_revision",
            "plugin_manifest_revision",
            "app_binding_revision",
            "capability_authority_revision",
        )
        if any(getattr(record, field) != getattr(expected, field) for field in public_fields):
            return False
        if set(record.scopes) != set(expected.scopes):
            return False
        partition = record.partition
        grant = record.grant
        return (
            partition.partition_key == record.token_key
            and partition.client_id == record.client_id
            and partition.authorization_issuer == record.authorization_issuer
            and partition.resource_origin == record.resource_origin
            and partition.account_partition == record.account_partition
            and partition.token.scopes == record.scopes
            and partition.profile_revision == record.profile_revision
            and grant.token_partition_key == record.token_key
            and grant.plugin_id == record.plugin_id
            and grant.profile_revision == record.profile_revision
            and grant.scopes == record.scopes
            and grant.capability_revision == record.capability_authority_revision
        )

    def evaluate(self, record: LegacyOAuthRecord) -> OAuthMigrationEvaluation:
        action = "delete_and_reauthorize"
        if type(record) is LegacyOAuthRecord and self._record_shape_is_valid(record):
            try:
                expected = self._expected(record)
            except Exception:
                expected = None
            if type(expected) is OAuthMigrationExpectation and self._exact_context(
                record, expected
            ):
                action = "preserve"
        token_key = record.token_key if type(record) is LegacyOAuthRecord else "invalid"
        plugin_id = record.plugin_id if type(record) is LegacyOAuthRecord else "invalid"
        source_revision = record.source_revision if type(record) is LegacyOAuthRecord else "invalid"
        return OAuthMigrationEvaluation(
            action,
            token_key,
            plugin_id,
            source_revision,
            record if action == "preserve" else None,
        )

    def run(self) -> OAuthMigrationReceipt:
        committed = self._vault.migration_receipt(self.MIGRATION_ID)
        if committed is None:
            try:
                records = self._legacy.records()
            except Exception as exc:
                raise ValueError("oauth_migration_source_unavailable") from exc
            if type(records) is not tuple or any(
                type(record) is not LegacyOAuthRecord for record in records
            ):
                raise ValueError("oauth_migration_source_unavailable")
            evaluated = tuple(self.evaluate(record) for record in records)
            if len({item.source_revision for item in evaluated}) != len(evaluated):
                raise ValueError("oauth_migration_source_unavailable")
            preserved = tuple(item for item in evaluated if item.action == "preserve")
            deleted = tuple(item for item in evaluated if item.action == "delete_and_reauthorize")
            pairs = tuple(
                (item._record.partition, item._record.grant)
                for item in preserved
                if item._record is not None
            )
            committed = self._vault.commit_migration(
                self.MIGRATION_ID,
                preserve=pairs,
                deleted_token_keys=tuple(sorted(item.token_key for item in deleted)),
                disconnected_plugin_ids=tuple(sorted({item.plugin_id for item in deleted})),
                source_revisions=tuple(sorted(item.source_revision for item in evaluated)),
            )
        receipt = OAuthMigrationReceipt.from_mapping(committed)
        try:
            self._legacy.remove_committed(
                receipt.source_revisions,
                disconnected_plugin_ids=receipt.disconnected_plugin_ids,
            )
        except Exception as exc:
            raise ValueError("oauth_migration_source_removal_failed") from exc
        return receipt


__all__ = [
    "LegacyOAuthRecord",
    "LegacyOAuthSource",
    "OAuthBrokerMigration",
    "OAuthMigrationEvaluation",
    "OAuthMigrationExpectation",
    "OAuthMigrationReceipt",
]

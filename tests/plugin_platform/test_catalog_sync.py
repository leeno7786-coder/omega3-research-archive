"""Bounded optional catalog synchronization that never empties the marketplace."""

from __future__ import annotations

import base64
from copy import deepcopy
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes
from plugin_platform.catalog_store import (
    CATALOG_FILE_NAME,
    CatalogStore,
    CatalogUnavailable,
)
from plugin_platform.catalog_sync import (
    MAX_CATALOG_PAYLOAD_BYTES,
    CatalogFetch,
    CatalogFetchError,
    CatalogFetchResponse,
    CatalogSynchronizer,
    CatalogSyncResult,
    CatalogSyncStatus,
)
from plugin_platform.trust import TrustedPublisherKeyRing
from tests.plugin_platform.support import (
    ephemeral_publisher_ring,
    valid_catalog_entry_document,
)

_PRIVATE_KEY, _RING = ephemeral_publisher_ring()


def _test_ring() -> TrustedPublisherKeyRing:
    """Return the ring admitting this module's ephemeral signing key."""
    return _RING


def _catalog_bytes(
    *,
    sequence: int,
    title: str = "Workspace",
    catalog_id: str = "test.catalog",
) -> bytes:
    """Return canonical signed catalog bytes for one exact sequence and content."""
    unsigned: dict[str, object] = {
        "schema_version": 2,
        "catalog_id": catalog_id,
        "publisher_id": "test.publisher",
        "sequence": sequence,
        "entries": [{**valid_catalog_entry_document(), "title": title}],
        "signing_key_id": "test-plugin-key",
    }
    signature = _PRIVATE_KEY.sign(canonical_bytes(unsigned))
    return canonical_bytes(
        {**deepcopy(unsigned), "signature": base64.b64encode(signature).decode("ascii")}
    )


def seeded_catalog_store(tmp_path: Path, *, sequence: int) -> CatalogStore:
    """Return a store whose synchronized snapshot already verifies at `sequence`."""
    root = tmp_path / "marketplace"
    root.mkdir(parents=True, exist_ok=True)
    (root / CATALOG_FILE_NAME).write_bytes(_catalog_bytes(sequence=sequence))
    return CatalogStore(
        root=root,
        bundled_path=tmp_path / "missing.json",
        key_ring=_test_ring(),
    )


def fetching(payload: bytes) -> CatalogFetch:
    """Return a fetch seam yielding exactly these bounded bytes."""

    def fetch() -> CatalogFetchResponse:
        return CatalogFetchResponse(payload=payload)

    return fetch


def failing(cause: str) -> CatalogFetch:
    """Return a fetch seam that fails with one stable transport cause."""

    def fetch() -> CatalogFetchResponse:
        raise CatalogFetchError(cause)

    return fetch


def test_failed_sync_preserves_last_verified_snapshot(tmp_path: Path) -> None:
    """Catches a failed synchronization erasing or emptying the retained snapshot."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    sync = CatalogSynchronizer(store=store, fetch=fetching(b"tampered"))
    result = sync.sync()
    assert result.status == "failed"
    assert result.error == "plugin_catalog_signature_invalid"
    assert store.current().sequence == 7


def test_successful_sync_promotes_the_new_snapshot_and_reports_updated(tmp_path: Path) -> None:
    """Catches a verified newer snapshot that is fetched but never retained."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    sync = CatalogSynchronizer(store=store, fetch=fetching(_catalog_bytes(sequence=8)))

    result = sync.sync()

    assert result.status == CatalogSyncStatus.UPDATED
    assert result.sequence == 8
    assert result.error is None
    assert result.revision == store.current().revision
    assert store.current().sequence == 8


def test_repeated_sync_of_the_identical_snapshot_reports_unchanged(tmp_path: Path) -> None:
    """Catches an unchanged snapshot being reported as a fresh marketplace update."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    sync = CatalogSynchronizer(store=store, fetch=fetching(_catalog_bytes(sequence=7)))

    result = sync.sync()

    assert result.status == CatalogSyncStatus.UNCHANGED
    assert result.sequence == 7
    assert result.error is None


def test_rollback_candidate_fails_and_preserves_the_snapshot(tmp_path: Path) -> None:
    """Catches synchronization accepting a replayed older signed snapshot."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    sync = CatalogSynchronizer(store=store, fetch=fetching(_catalog_bytes(sequence=6)))

    result = sync.sync()

    assert result.status == CatalogSyncStatus.FAILED
    assert result.error == "plugin_catalog_sequence_rollback"
    assert result.revision is None
    assert store.current().sequence == 7


def test_same_sequence_equivocation_fails_and_preserves_the_snapshot(tmp_path: Path) -> None:
    """Catches a second, different snapshot being admitted at one sequence."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    sync = CatalogSynchronizer(
        store=store, fetch=fetching(_catalog_bytes(sequence=7, title="Workspace Pro"))
    )

    result = sync.sync()

    assert result.status == CatalogSyncStatus.FAILED
    assert result.error == "plugin_catalog_sequence_equivocation"
    assert store.current().entries[0].title == "Workspace"


def test_oversized_response_is_refused_by_the_bounded_seam() -> None:
    """Catches an unbounded transport response entering the verification path."""
    with pytest.raises(ValueError, match="plugin_catalog_too_large"):
        CatalogFetchResponse(payload=b"x" * (MAX_CATALOG_PAYLOAD_BYTES + 1))


@pytest.mark.parametrize(
    "cause",
    [
        "plugin_catalog_too_large",
        "plugin_catalog_redirect_rejected",
        "plugin_catalog_timeout",
        "plugin_catalog_unreachable",
        "plugin_catalog_rejected",
    ],
)
def test_transport_failure_reports_its_stable_cause_and_preserves_the_snapshot(
    tmp_path: Path, cause: str
) -> None:
    """Catches transport failures collapsing into success or leaking raw exception text."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    sync = CatalogSynchronizer(store=store, fetch=failing(cause))

    result = sync.sync()

    assert result.status == CatalogSyncStatus.FAILED
    assert result.error == cause
    assert result.sequence is None
    assert store.current().sequence == 7


def test_sync_rejects_a_fetch_result_that_is_not_the_bounded_response_shape(
    tmp_path: Path,
) -> None:
    """Catches raw bytes bypassing the bounded response contract of the fetch seam."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    sync = CatalogSynchronizer(store=store, fetch=lambda: _catalog_bytes(sequence=8))

    result = sync.sync()

    assert result.status == CatalogSyncStatus.FAILED
    assert result.error == "plugin_catalog_fetch_invalid"
    assert store.current().sequence == 7


def test_sync_without_any_snapshot_reports_failed_and_leaves_the_catalog_unavailable(
    tmp_path: Path,
) -> None:
    """Catches a first-run failure fabricating an empty but successful marketplace."""
    store = CatalogStore(
        root=tmp_path / "marketplace",
        bundled_path=tmp_path / "missing.json",
        key_ring=_test_ring(),
    )
    sync = CatalogSynchronizer(store=store, fetch=failing("plugin_catalog_unreachable"))

    result = sync.sync()

    assert result.status == CatalogSyncStatus.FAILED
    assert result.error == "plugin_catalog_unreachable"
    with pytest.raises(CatalogUnavailable, match="plugin_catalog_unavailable"):
        store.current()


def test_first_successful_sync_without_a_prior_snapshot_reports_updated(tmp_path: Path) -> None:
    """Catches a first verified snapshot being refused for having no predecessor."""
    store = CatalogStore(
        root=tmp_path / "marketplace",
        bundled_path=tmp_path / "missing.json",
        key_ring=_test_ring(),
    )
    sync = CatalogSynchronizer(store=store, fetch=fetching(_catalog_bytes(sequence=5)))

    result = sync.sync()

    assert result.status == CatalogSyncStatus.UPDATED
    assert result.sequence == 5
    assert store.current().sequence == 5


@pytest.mark.parametrize("cause", ["", "catalog_timeout", "PLUGIN_CATALOG_TIMEOUT", "plugin", 7])
def test_catalog_fetch_error_requires_a_stable_plugin_catalog_cause(cause: object) -> None:
    """Catches raw exception text or ad-hoc strings entering the public cause contract."""
    with pytest.raises(ValueError, match="plugin_catalog_cause_invalid"):
        CatalogFetchError(cause)  # type: ignore[arg-type]


def test_sync_result_separates_outcome_from_cause() -> None:
    """Catches a result carrying both a promoted revision and a failure cause."""
    with pytest.raises(ValueError, match="plugin_catalog_sync_result_invalid"):
        CatalogSyncResult(
            status=CatalogSyncStatus.UPDATED,
            sequence=8,
            revision="a" * 64,
            error="plugin_catalog_timeout",
        )
    with pytest.raises(ValueError, match="plugin_catalog_sync_result_invalid"):
        CatalogSyncResult(status=CatalogSyncStatus.FAILED, sequence=8, revision="a" * 64)


def _bundled_only_store(tmp_path: Path, *, sequence: int) -> CatalogStore:
    """Return a store backed only by a bundled signed snapshot at `sequence`."""
    bundled = tmp_path / "bundled.json"
    bundled.write_bytes(_catalog_bytes(sequence=sequence))
    return CatalogStore(
        root=tmp_path / "marketplace",
        bundled_path=bundled,
        key_ring=_test_ring(),
    )


def test_first_sync_cannot_downgrade_below_the_bundled_snapshot(tmp_path: Path) -> None:
    """Catches a stale mirror pinning a fresh install below its shipped catalog."""
    store = _bundled_only_store(tmp_path, sequence=50)
    sync = CatalogSynchronizer(store=store, fetch=fetching(_catalog_bytes(sequence=5)))

    result = sync.sync()

    assert result.status == CatalogSyncStatus.FAILED
    assert result.error == "plugin_catalog_sequence_rollback"
    assert store.current().sequence == 50


def test_first_sync_past_the_bundled_snapshot_reports_updated(tmp_path: Path) -> None:
    """Catches the bundled rollback floor blocking a legitimate newer snapshot."""
    store = _bundled_only_store(tmp_path, sequence=50)
    sync = CatalogSynchronizer(store=store, fetch=fetching(_catalog_bytes(sequence=51)))

    result = sync.sync()

    assert result.status == CatalogSyncStatus.UPDATED
    assert result.sequence == 51
    assert store.current().sequence == 51

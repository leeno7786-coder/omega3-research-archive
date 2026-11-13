"""On-device retention and atomic promotion of verified catalog snapshots."""

from __future__ import annotations

import base64
import threading
from copy import deepcopy
from pathlib import Path

import pytest

from mcp_platform.canonical import canonical_bytes
from plugin_platform.catalog import MAX_CATALOG_BYTES
from plugin_platform.catalog_store import (
    CATALOG_FILE_NAME,
    CatalogStore,
    CatalogStoreError,
    CatalogUnavailable,
)
from plugin_platform.trust import TrustedPublisherKeyRing
from tests.plugin_platform.support import (
    ephemeral_publisher_ring,
    valid_catalog_entry_document,
)

_PRIVATE_KEY, _RING = ephemeral_publisher_ring()


def _test_ring() -> TrustedPublisherKeyRing:
    """Return the ring admitting this module's ephemeral signing key.

    Carries a leading underscore so the configured ``python_functions = test_*``
    discovery never collects a signing helper as a test case.
    """
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


def _bundled_store(tmp_path: Path, *, sequence: int) -> CatalogStore:
    """Return a store backed only by a bundled signed snapshot at `sequence`."""
    bundled = tmp_path / "bundled.json"
    bundled.write_bytes(_catalog_bytes(sequence=sequence))
    return CatalogStore(
        root=tmp_path / "marketplace",
        bundled_path=bundled,
        key_ring=_test_ring(),
    )


def _temporaries(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("catalog.json.*") if path.name.endswith(".tmp"))


def test_first_run_without_bundled_or_synced_catalog_fails_not_empty(tmp_path: Path) -> None:
    """Catches a fabricated empty marketplace standing in for a missing snapshot."""
    store = CatalogStore(
        root=tmp_path, bundled_path=tmp_path / "missing.json", key_ring=_test_ring()
    )
    with pytest.raises(CatalogUnavailable, match="plugin_catalog_unavailable"):
        store.current()


def test_current_serves_the_greater_sequence_when_the_synced_copy_leads(tmp_path: Path) -> None:
    """Catches a store that ignores a promoted snapshot and serves stale bundled bytes."""
    bundled = tmp_path / "bundled.json"
    bundled.write_bytes(_catalog_bytes(sequence=3))
    root = tmp_path / "marketplace"
    root.mkdir(parents=True)
    (root / CATALOG_FILE_NAME).write_bytes(_catalog_bytes(sequence=9))
    store = CatalogStore(root=root, bundled_path=bundled, key_ring=_test_ring())

    assert store.current().sequence == 9


def test_current_falls_back_to_bundled_when_the_synced_copy_is_a_partial_write(
    tmp_path: Path,
) -> None:
    """Catches a torn synchronized file being served, raising, or emptying the catalog."""
    bundled = tmp_path / "bundled.json"
    bundled.write_bytes(_catalog_bytes(sequence=4))
    root = tmp_path / "marketplace"
    root.mkdir(parents=True)
    (root / CATALOG_FILE_NAME).write_bytes(_catalog_bytes(sequence=9)[:64])
    store = CatalogStore(root=root, bundled_path=bundled, key_ring=_test_ring())

    current = store.current()

    assert current.sequence == 4
    assert current.entries


def test_promote_replaces_the_snapshot_and_survives_restart(tmp_path: Path) -> None:
    """Catches a promotion held only in memory or lost across process restart."""
    store = _bundled_store(tmp_path, sequence=8)
    promoted = store.promote(_catalog_bytes(sequence=9))

    assert promoted.sequence == 9
    restarted = CatalogStore(
        root=tmp_path / "marketplace",
        bundled_path=tmp_path / "bundled.json",
        key_ring=_test_ring(),
    )
    assert restarted.current().sequence == 9
    assert restarted.current().revision == promoted.revision
    assert _temporaries(tmp_path / "marketplace") == []


def test_promote_rejects_a_lower_sequence_rollback(tmp_path: Path) -> None:
    """Catches an attacker replaying an older signed snapshot over a newer one."""
    store = seeded_catalog_store(tmp_path, sequence=7)

    with pytest.raises(CatalogStoreError, match="plugin_catalog_sequence_rollback"):
        store.promote(_catalog_bytes(sequence=6))

    assert store.current().sequence == 7


def test_promote_rejects_same_sequence_equivocation(tmp_path: Path) -> None:
    """Catches two different signed snapshots being accepted at one sequence."""
    store = seeded_catalog_store(tmp_path, sequence=7)

    with pytest.raises(CatalogStoreError, match="plugin_catalog_sequence_equivocation"):
        store.promote(_catalog_bytes(sequence=7, title="Workspace Pro"))

    assert store.current().sequence == 7
    assert store.current().entries[0].title == "Workspace"


def test_promote_is_idempotent_for_the_identical_snapshot(tmp_path: Path) -> None:
    """Catches a re-promoted identical snapshot being treated as equivocation."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    before = store.current().revision

    promoted = store.promote(_catalog_bytes(sequence=7))

    assert promoted.revision == before


def test_promote_rejects_a_foreign_catalog_identity(tmp_path: Path) -> None:
    """Catches a different signed catalog identity displacing the retained one."""
    store = seeded_catalog_store(tmp_path, sequence=7)

    with pytest.raises(CatalogStoreError, match="plugin_catalog_identity_changed"):
        store.promote(_catalog_bytes(sequence=9, catalog_id="other.catalog"))

    assert store.current().sequence == 7


@pytest.mark.parametrize(
    "candidate",
    [b"tampered", b"", b"{}", b'{"schema_version": 2}'],
)
def test_promote_rejects_unauthentic_candidate_bytes(tmp_path: Path, candidate: bytes) -> None:
    """Catches unsigned, malformed, or truncated bytes reaching the retained snapshot."""
    store = seeded_catalog_store(tmp_path, sequence=7)

    with pytest.raises(CatalogStoreError, match="plugin_catalog_signature_invalid"):
        store.promote(candidate)

    assert store.current().sequence == 7


def test_promote_rejects_an_oversized_candidate(tmp_path: Path) -> None:
    """Catches an unbounded candidate payload being written before verification."""
    store = seeded_catalog_store(tmp_path, sequence=7)

    with pytest.raises(CatalogStoreError, match="plugin_catalog_too_large"):
        store.promote(b"x" * (MAX_CATALOG_BYTES + 1))

    assert store.current().sequence == 7


def test_failed_promotion_leaves_no_partial_write_or_temporary_residue(tmp_path: Path) -> None:
    """Catches a non-atomic promotion leaving a torn file or abandoned temporary."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    root = tmp_path / "marketplace"
    before = (root / CATALOG_FILE_NAME).read_bytes()

    with pytest.raises(CatalogStoreError):
        store.promote(b"tampered")
    with pytest.raises(CatalogStoreError):
        store.promote(_catalog_bytes(sequence=6))

    assert (root / CATALOG_FILE_NAME).read_bytes() == before
    assert _temporaries(root) == []


def test_promote_rejects_a_candidate_that_is_not_bytes(tmp_path: Path) -> None:
    """Catches text or arbitrary objects being written as a signed snapshot."""
    store = seeded_catalog_store(tmp_path, sequence=7)

    with pytest.raises(CatalogStoreError, match="plugin_catalog_signature_invalid"):
        store.promote("not bytes")  # type: ignore[arg-type]


def test_concurrent_promotion_admits_exactly_one_snapshot(tmp_path: Path) -> None:
    """Catches a lost update or torn snapshot when promotions race at one sequence."""
    store = seeded_catalog_store(tmp_path, sequence=7)
    candidates = [
        _catalog_bytes(sequence=8, title="Workspace A"),
        _catalog_bytes(sequence=8, title="Workspace B"),
    ]
    barrier = threading.Barrier(len(candidates))
    outcomes: list[object] = [None] * len(candidates)

    def promote(index: int) -> None:
        barrier.wait()
        try:
            outcomes[index] = store.promote(candidates[index]).revision
        except CatalogStoreError as exc:
            outcomes[index] = str(exc)

    threads = [threading.Thread(target=promote, args=(index,)) for index in range(len(candidates))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not thread.is_alive() for thread in threads)
    rejected = [item for item in outcomes if item == "plugin_catalog_sequence_equivocation"]
    accepted = [item for item in outcomes if item != "plugin_catalog_sequence_equivocation"]
    assert len(rejected) == 1
    assert len(accepted) == 1
    assert store.current().revision == accepted[0]
    assert _temporaries(tmp_path / "marketplace") == []


def test_store_rejects_a_key_ring_that_is_not_a_trusted_publisher_ring(tmp_path: Path) -> None:
    """Catches an unvalidated ring object being accepted as verification authority."""
    with pytest.raises(ValueError, match="plugin_catalog_store_invalid"):
        CatalogStore(
            root=tmp_path,
            bundled_path=tmp_path / "missing.json",
            key_ring=object(),  # type: ignore[arg-type]
        )


def test_bundled_snapshot_is_the_rollback_floor_on_first_sync(tmp_path: Path) -> None:
    """Catches a fresh install being pinned below the sequence it shipped with."""
    store = _bundled_store(tmp_path, sequence=50)

    with pytest.raises(CatalogStoreError, match="plugin_catalog_sequence_rollback"):
        store.promote(_catalog_bytes(sequence=5))

    assert store.current().sequence == 50


def test_first_sync_rejects_same_sequence_equivocation_against_the_bundled_floor(
    tmp_path: Path,
) -> None:
    """Catches a second signed snapshot displacing the bundled one at its sequence."""
    store = _bundled_store(tmp_path, sequence=50)

    with pytest.raises(CatalogStoreError, match="plugin_catalog_sequence_equivocation"):
        store.promote(_catalog_bytes(sequence=50, title="Workspace Pro"))

    assert store.current().entries[0].title == "Workspace"


def test_first_sync_rejects_a_foreign_identity_against_the_bundled_floor(tmp_path: Path) -> None:
    """Catches a different signed catalog identity displacing the shipped anchor."""
    store = _bundled_store(tmp_path, sequence=50)

    with pytest.raises(CatalogStoreError, match="plugin_catalog_identity_changed"):
        store.promote(_catalog_bytes(sequence=51, catalog_id="other.catalog"))

    assert store.current().sequence == 50


def test_promote_advances_past_the_bundled_floor_on_first_sync(tmp_path: Path) -> None:
    """Catches the rollback floor blocking a legitimate newer first synchronization."""
    store = _bundled_store(tmp_path, sequence=50)

    promoted = store.promote(_catalog_bytes(sequence=51))

    assert promoted.sequence == 51
    assert store.current().sequence == 51


def test_current_serves_the_greater_sequence_when_bundled_leads_the_synced_copy(
    tmp_path: Path,
) -> None:
    """Catches a stale synced snapshot masking a newer shipped bundled snapshot."""
    bundled = tmp_path / "bundled.json"
    bundled.write_bytes(_catalog_bytes(sequence=9))
    root = tmp_path / "marketplace"
    root.mkdir(parents=True)
    (root / CATALOG_FILE_NAME).write_bytes(_catalog_bytes(sequence=8))
    store = CatalogStore(root=root, bundled_path=bundled, key_ring=_test_ring())

    assert store.current().sequence == 9


def test_current_ignores_a_synced_snapshot_whose_identity_differs_from_bundled(
    tmp_path: Path,
) -> None:
    """Catches a foreign signed identity on disk displacing the shipped anchor."""
    bundled = tmp_path / "bundled.json"
    bundled.write_bytes(_catalog_bytes(sequence=9))
    root = tmp_path / "marketplace"
    root.mkdir(parents=True)
    (root / CATALOG_FILE_NAME).write_bytes(_catalog_bytes(sequence=40, catalog_id="other.catalog"))
    store = CatalogStore(root=root, bundled_path=bundled, key_ring=_test_ring())

    current = store.current()

    assert current.catalog_id == "test.catalog"
    assert current.sequence == 9

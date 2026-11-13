"""Bounded, content-addressed retention of imported plugin artifacts."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from mcp_platform.canonical import sha256_hex
from plugin_platform.artifact_store import (
    MAX_ARTIFACT_BYTES,
    STAGING_DIRECTORY_NAME,
    ArtifactStore,
    ArtifactStoreError,
    ArtifactStreamError,
    StagedArtifact,
)


_PACKAGE = b"package"
_PACKAGE_DIGEST = sha256_hex(_PACKAGE)


def _stored_files(root: Path) -> tuple[Path, ...]:
    """Return every file sitting in a content-address shard beneath this root."""
    if not root.is_dir():
        return ()
    return tuple(
        sorted(
            path
            for shard in root.iterdir()
            if shard.is_dir() and len(shard.name) == 2
            for path in shard.iterdir()
            if path.is_file()
        )
    )


def _counting_stream(chunks: tuple[bytes, ...], pulled: list[bytes]) -> Iterator[bytes]:
    """Yield these chunks while recording exactly which ones were consumed."""
    for chunk in chunks:
        pulled.append(chunk)
        yield chunk


def _forged_stage(sha256: str, byte_length: int, name: str) -> StagedArtifact:
    """Return a staging receipt built without its own constructor validation."""
    forged = object.__new__(StagedArtifact)
    object.__setattr__(forged, "sha256", sha256)
    object.__setattr__(forged, "byte_length", byte_length)
    object.__setattr__(forged, "name", name)
    return forged


def test_artifact_store_promotes_only_exact_content_digest(tmp_path: Path) -> None:
    """Catches promoting bytes the caller never proved are the pinned artifact."""
    store = ArtifactStore(tmp_path)
    expected = sha256_hex(b"package")
    stored = store.promote(store.stage([b"pack", b"age"], expected, 64))
    assert stored.path == tmp_path / expected[:2] / expected
    assert stored.path.read_bytes() == b"package"


def test_stage_refuses_a_stream_whose_content_digest_differs(tmp_path: Path) -> None:
    """Catches trusting a caller's declared digest instead of the streamed bytes."""
    store = ArtifactStore(tmp_path)

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage([b"pack", b"agf"], _PACKAGE_DIGEST, 64)

    assert str(exc_info.value) == "plugin_artifact_digest_mismatch"
    assert _stored_files(tmp_path) == ()


def test_stage_refuses_a_truncated_stream_that_ends_before_the_digest(
    tmp_path: Path,
) -> None:
    """Catches accepting a short read as a complete artifact."""
    store = ArtifactStore(tmp_path)

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage([b"pack"], _PACKAGE_DIGEST, 64)

    assert str(exc_info.value) == "plugin_artifact_digest_mismatch"
    assert not (tmp_path / _PACKAGE_DIGEST[:2] / _PACKAGE_DIGEST).exists()
    assert _stored_files(tmp_path) == ()


def test_stage_stops_before_consuming_or_writing_past_the_byte_limit(
    tmp_path: Path,
) -> None:
    """Catches a bound checked only after the whole stream was already absorbed."""
    store = ArtifactStore(tmp_path)
    pulled: list[bytes] = []
    stream = _counting_stream((b"a" * 4, b"b" * 4, b"c" * 4), pulled)

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage(stream, sha256_hex(b"a" * 4 + b"b" * 4 + b"c" * 4), 6)

    assert str(exc_info.value) == "plugin_artifact_too_large"
    assert pulled == [b"a" * 4, b"b" * 4]
    assert _stored_files(tmp_path) == ()


@pytest.mark.parametrize("max_bytes", [0, -1, MAX_ARTIFACT_BYTES + 1, True, 1.0])
def test_stage_refuses_a_bound_outside_the_closed_artifact_limit(
    tmp_path: Path, max_bytes: object
) -> None:
    """Catches letting a caller widen or disable the artifact size ceiling."""
    store = ArtifactStore(tmp_path)

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage([_PACKAGE], _PACKAGE_DIGEST, max_bytes)

    assert str(exc_info.value) == "plugin_artifact_bound_invalid"


@pytest.mark.parametrize(
    "digest",
    [
        "../../etc/passwd",
        f"..{os.sep}{'a' * 62}",
        _PACKAGE_DIGEST.upper(),
        _PACKAGE_DIGEST[:63],
        f"{_PACKAGE_DIGEST[:62]}/x",
        "",
    ],
)
def test_stage_refuses_a_digest_that_is_not_one_canonical_content_address(
    tmp_path: Path, digest: str
) -> None:
    """Catches deriving a store path from text that is not a bare SHA-256 digest."""
    store = ArtifactStore(tmp_path)

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage([_PACKAGE], digest, 64)

    assert str(exc_info.value) == "plugin_artifact_digest_invalid"
    assert _stored_files(tmp_path) == ()


@pytest.mark.parametrize("chunk", ["package", bytearray(b"package"), memoryview(b"pa"), None])
def test_stage_refuses_a_chunk_that_is_not_exact_bytes(
    tmp_path: Path, chunk: object
) -> None:
    """Catches hashing a text or buffer object the writer would encode differently."""
    store = ArtifactStore(tmp_path)

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage([chunk], _PACKAGE_DIGEST, 64)

    assert str(exc_info.value) == "plugin_artifact_stream_invalid"
    assert _stored_files(tmp_path) == ()


def test_promote_refuses_a_staging_name_that_escapes_the_staging_directory(
    tmp_path: Path,
) -> None:
    """Catches a promotion path built from an attacker-supplied staging name."""
    store = ArtifactStore(tmp_path)
    outside = tmp_path.parent / "outside.bin"
    outside.write_bytes(_PACKAGE)

    forged = _forged_stage(_PACKAGE_DIGEST, len(_PACKAGE), f"..{os.sep}..{os.sep}outside.bin")
    with pytest.raises(ArtifactStoreError) as exc_info:
        store.promote(forged)

    assert str(exc_info.value) == "plugin_artifact_staging_invalid"
    assert outside.read_bytes() == _PACKAGE
    assert _stored_files(tmp_path) == ()


def test_staged_receipt_rejects_its_own_escaping_name_at_construction(
    tmp_path: Path,
) -> None:
    """Catches a receipt type that only documents, rather than enforces, its grammar."""
    with pytest.raises(ArtifactStoreError):
        StagedArtifact(sha256=_PACKAGE_DIGEST, byte_length=7, name="../outside.bin")


def test_promoting_the_same_content_twice_keeps_one_shared_content_address(
    tmp_path: Path,
) -> None:
    """Catches a duplicate import clobbering, duplicating, or corrupting the artifact."""
    store = ArtifactStore(tmp_path)
    first = store.promote(store.stage([_PACKAGE], _PACKAGE_DIGEST, 64))
    second = store.promote(store.stage([b"pack", b"age"], _PACKAGE_DIGEST, 64))

    assert second.path == first.path
    assert second.sha256 == first.sha256 == _PACKAGE_DIGEST
    assert _stored_files(tmp_path) == (first.path,)
    assert first.path.read_bytes() == _PACKAGE


def test_promotion_leaves_no_staged_bytes_behind(tmp_path: Path) -> None:
    """Catches a staging area that accumulates a second executable copy per import."""
    store = ArtifactStore(tmp_path)
    store.promote(store.stage([_PACKAGE], _PACKAGE_DIGEST, 64))

    staging = tmp_path / STAGING_DIRECTORY_NAME
    assert not staging.exists() or tuple(staging.iterdir()) == ()


def test_staged_receipt_never_exposes_a_path_to_run_from(tmp_path: Path) -> None:
    """Catches handing callers a staging path they could execute before promotion."""
    store = ArtifactStore(tmp_path)
    staged = store.stage([_PACKAGE], _PACKAGE_DIGEST, 64)

    assert not hasattr(staged, "path")
    assert staged.name != staged.sha256
    assert staged.sha256 == _PACKAGE_DIGEST
    assert staged.byte_length == len(_PACKAGE)


def test_stage_rejects_a_reparse_point_standing_in_for_the_store_root(
    tmp_path: Path,
) -> None:
    """Catches following a swapped store root into a directory Omega does not own."""
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "artifacts"
    try:
        os.symlink(outside, root, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    store = ArtifactStore(root)
    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage([_PACKAGE], _PACKAGE_DIGEST, 64)

    assert str(exc_info.value) == "plugin_artifact_store_invalid"
    assert tuple(outside.iterdir()) == ()


def test_promote_rejects_a_reparse_point_standing_in_for_the_shard(
    tmp_path: Path,
) -> None:
    """Catches writing a promoted artifact through a swapped shard directory."""
    store = ArtifactStore(tmp_path)
    staged = store.stage([_PACKAGE], _PACKAGE_DIGEST, 64)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, tmp_path / _PACKAGE_DIGEST[:2], target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.promote(staged)

    assert str(exc_info.value) == "plugin_artifact_store_invalid"
    assert tuple(outside.iterdir()) == ()


def test_promote_refuses_staged_bytes_swapped_after_the_digest_was_proved(
    tmp_path: Path,
) -> None:
    """Catches promoting a staging receipt instead of re-proving the staged bytes."""
    store = ArtifactStore(tmp_path)
    staged = store.stage([_PACKAGE], _PACKAGE_DIGEST, 64)
    (tmp_path / STAGING_DIRECTORY_NAME / staged.name).write_bytes(b"replaced")

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.promote(staged)

    assert str(exc_info.value) == "plugin_artifact_digest_mismatch"
    assert _stored_files(tmp_path) == ()


def test_verify_reports_a_promoted_artifact_altered_on_disk(tmp_path: Path) -> None:
    """Catches trusting a content address without re-reading what it now names."""
    store = ArtifactStore(tmp_path)
    stored = store.promote(store.stage([_PACKAGE], _PACKAGE_DIGEST, 64))
    assert store.verify(stored) == stored

    stored.path.write_bytes(b"tampered")
    with pytest.raises(ArtifactStoreError) as exc_info:
        store.verify(stored)

    assert str(exc_info.value) == "plugin_artifact_digest_mismatch"


def test_promote_refuses_a_receipt_this_store_never_issued(tmp_path: Path) -> None:
    """Catches promoting a receipt whose staged file does not exist."""
    store = ArtifactStore(tmp_path)
    staged = store.stage([_PACKAGE], _PACKAGE_DIGEST, 64)
    (tmp_path / STAGING_DIRECTORY_NAME / staged.name).unlink()

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.promote(staged)

    assert str(exc_info.value) == "plugin_artifact_missing"
    assert _stored_files(tmp_path) == ()


def test_default_store_root_is_the_declared_plugin_artifacts_root() -> None:
    """Catches an artifact root invented beside, rather than under, plugin state."""
    from core import paths as core_paths

    assert core_paths.PLUGIN_ARTIFACTS_ROOT == core_paths.PLUGIN_STATE_ROOT / "artifacts"
    assert ArtifactStore().root == core_paths.PLUGIN_ARTIFACTS_ROOT.absolute()


def test_stage_reports_a_source_that_dies_midstream_as_a_stable_cause(
    tmp_path: Path,
) -> None:
    """Catches raw transport text escaping through a stream that fails mid-artifact."""

    def failing() -> Iterator[bytes]:
        yield b"pack"
        raise RuntimeError("connection reset by artifacts.example.test")

    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage(failing(), _PACKAGE_DIGEST, 64)

    assert str(exc_info.value) == "plugin_artifact_stream_invalid"
    assert "example.test" not in str(exc_info.value)
    assert _stored_files(tmp_path) == ()


def test_stage_refuses_a_source_that_is_not_iterable_at_all(tmp_path: Path) -> None:
    """Catches asking a non-stream object for bytes and reporting its own error."""
    store = ArtifactStore(tmp_path)

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage(object(), _PACKAGE_DIGEST, 64)  # type: ignore[arg-type]

    assert str(exc_info.value) == "plugin_artifact_stream_invalid"


def test_a_stream_failure_is_distinguishable_from_wrong_artifact_bytes(
    tmp_path: Path,
) -> None:
    """Catches collapsing an unavailable artifact and a wrong one into one verdict."""

    def failing() -> Iterator[bytes]:
        yield b"pack"
        raise RuntimeError("connection reset")

    store = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactStreamError):
        store.stage(failing(), _PACKAGE_DIGEST, 64)

    with pytest.raises(ArtifactStoreError) as exc_info:
        store.stage([b"different"], _PACKAGE_DIGEST, 64)

    assert not isinstance(exc_info.value, ArtifactStreamError)
    assert issubclass(ArtifactStreamError, ArtifactStoreError)


def test_stage_hashes_each_chunk_as_it_arrives_not_the_assembled_buffer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches computing the digest in one post-hoc pass over the assembled buffer.

    A genuine streaming hash calls ``update`` once per chunk as each one is
    pulled from the source. A post-hoc hash instead calls the ``hashlib.sha256``
    constructor exactly once, with the entire buffer already assembled, only
    after the whole stream has been absorbed. A spy standing in for
    ``hashlib.sha256`` records which one actually happened, so this fails on a
    store that hashes ``payload`` once at the end even though it still proves
    the correct digest.
    """
    from plugin_platform import artifact_store as artifact_store_module

    calls: list[bytes] = []
    real_sha256 = hashlib.sha256

    class _SpyHash:
        def __init__(self, data: bytes = b"") -> None:
            self._inner = real_sha256()
            if data:
                calls.append(data)
                self._inner.update(data)

        def update(self, data: bytes) -> None:
            calls.append(data)
            self._inner.update(data)

        def hexdigest(self) -> str:
            return self._inner.hexdigest()

    monkeypatch.setattr(artifact_store_module.hashlib, "sha256", _SpyHash)

    store = ArtifactStore(tmp_path)
    store.stage([b"pack", b"age"], _PACKAGE_DIGEST, 64)

    assert calls == [b"pack", b"age"]

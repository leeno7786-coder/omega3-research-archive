from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import torch

from core.paths import OMEGA_ROOT
from benchmarks.memory_organ.program1.ettin_fp32 import (
    ETTIN_DIM,
    ETTIN_HASHES,
    ETTIN_MAX_TOKENS,
    ETTIN_MODEL_ID,
    ETTIN_SNAPSHOT,
    EttinArtifactError,
    EttinExecutionError,
    mean_pool_and_normalize,
    verify_ettin_snapshot,
)

# The one authentic snapshot. Tests that need real weights skip when it is absent.
LIVE_SNAPSHOT = (
    OMEGA_ROOT
    / "memory"
    / "models"
    / "models--tasksource--ettin-17m-embed"
    / "snapshots"
    / ETTIN_SNAPSHOT
)


def make_fake_snapshot(root: Path) -> None:
    """Materialise all six pinned relative paths with non-pinned bytes."""
    for relative in ETTIN_HASHES:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"not the pinned artifact")


def test_snapshot_verification_refuses_any_hash_mismatch(tmp_path: Path) -> None:
    make_fake_snapshot(tmp_path)
    with pytest.raises(EttinArtifactError, match="model.safetensors"):
        verify_ettin_snapshot(tmp_path)


def test_snapshot_verification_requires_all_six_pinned_files(tmp_path: Path) -> None:
    with pytest.raises(EttinArtifactError, match="tokenizer.json"):
        verify_ettin_snapshot(tmp_path)


def test_pinned_constants_match_the_a1_amendment() -> None:
    assert ETTIN_MODEL_ID == "tasksource/ettin-17m-embed"
    assert ETTIN_SNAPSHOT == "6132e97e52834eb791fd9333049a1726d01fc898"
    assert ETTIN_DIM == 256
    assert ETTIN_MAX_TOKENS == 1024
    assert ETTIN_HASHES == {
        "model.safetensors": "42965efac128bdb06678f2ffbda2054bb31f286f68c10a5eea5796a170fd68b4",
        "tokenizer.json": "b18211964db7d0ae2a8d2c4a4c00e271c13905859726fe980622488d95db75af",
        "config.json": "afb59c561c4685b23b2e71753256f929fd0f79ea57d8429ee9c7feac10599bb0",
        "1_Pooling/config.json": "ace9f219846dfea1aad340dd7834ec03a05a32c5841738c383af4f41d59e92fc",
        "modules.json": "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
        "config_sentence_transformers.json": (
            "5bd47d0626eedaa34b22396a3dad81ebe0c257f17ffc77f97f0ac58bec4bbc86"
        ),
    }


def test_hashes_are_lowercase_sha256_hex() -> None:
    for relative, digest in ETTIN_HASHES.items():
        assert len(digest) == 64, relative
        assert digest == digest.lower(), relative
        int(digest, 16)  # raises ValueError if not hex


def test_verification_names_every_missing_file_not_merely_the_first(
    tmp_path: Path,
) -> None:
    """A snapshot missing several pinned files must report all of them."""
    (tmp_path / "config.json").write_bytes(b"x")
    with pytest.raises(EttinArtifactError) as excinfo:
        verify_ettin_snapshot(tmp_path)
    message = str(excinfo.value)
    for relative in ("model.safetensors", "tokenizer.json", "modules.json"):
        assert relative in message


def test_verification_refuses_a_dangling_symlink(tmp_path: Path) -> None:
    """HF snapshots are symlink farms; an unresolvable target must be refused."""
    for relative in ETTIN_HASHES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"placeholder")
    broken = tmp_path / "model.safetensors"
    broken.unlink()
    try:
        broken.symlink_to(tmp_path / "blobs" / "absent")
    except OSError as exc:
        pytest.skip(f"file symlinks unavailable: {exc}")
    with pytest.raises(EttinArtifactError, match="model.safetensors"):
        verify_ettin_snapshot(tmp_path)


def test_verification_refuses_a_directory_in_place_of_a_pinned_file(
    tmp_path: Path,
) -> None:
    for relative in ETTIN_HASHES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"placeholder")
    usurped = tmp_path / "tokenizer.json"
    usurped.unlink()
    usurped.mkdir()
    with pytest.raises(EttinArtifactError, match="tokenizer.json"):
        verify_ettin_snapshot(tmp_path)


def test_verification_accepts_a_snapshot_whose_bytes_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Monkeypatch the expected mapping; production constants stay untouched."""
    import benchmarks.memory_organ.program1.ettin_fp32 as module

    forged: dict[str, str] = {}
    for relative in ETTIN_HASHES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = f"payload for {relative}".encode()
        target.write_bytes(payload)
        forged[relative] = hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(module, "ETTIN_HASHES", forged)

    resolved = verify_ettin_snapshot(tmp_path)
    assert resolved == forged


def test_live_snapshot_satisfies_the_pinned_contract() -> None:
    if not LIVE_SNAPSHOT.exists():
        pytest.skip(f"pinned snapshot absent: {LIVE_SNAPSHOT}")
    assert verify_ettin_snapshot(LIVE_SNAPSHOT) == ETTIN_HASHES


def test_live_snapshot_config_declares_the_pinned_geometry() -> None:
    if not LIVE_SNAPSHOT.exists():
        pytest.skip(f"pinned snapshot absent: {LIVE_SNAPSHOT}")
    config = json.loads((LIVE_SNAPSHOT / "config.json").read_text())
    assert config["hidden_size"] == ETTIN_DIM
    assert config["model_type"] == "modernbert"


def test_mean_pool_ignores_padding_and_normalizes() -> None:
    hidden = torch.tensor([[[3.0, 0.0], [0.0, 4.0], [99.0, 99.0]]])
    mask = torch.tensor([[1, 1, 0]])
    pooled = mean_pool_and_normalize(hidden, mask)
    expected = torch.tensor([[1.5, 2.0]])
    expected = expected / torch.linalg.vector_norm(expected, dim=1, keepdim=True)
    assert torch.equal(pooled, expected)


def test_mean_pool_rejects_a_zero_token_mask() -> None:
    hidden = torch.zeros((1, 3, 2))
    mask = torch.zeros((1, 3), dtype=torch.long)
    with pytest.raises(EttinExecutionError, match="zero-token"):
        mean_pool_and_normalize(hidden, mask)


def test_mean_pool_rejects_a_zero_token_row_inside_a_batch() -> None:
    """One empty row must fail the batch rather than emit a NaN vector."""
    hidden = torch.ones((2, 3, 2))
    mask = torch.tensor([[1, 1, 0], [0, 0, 0]])
    with pytest.raises(EttinExecutionError, match="zero-token"):
        mean_pool_and_normalize(hidden, mask)


def test_mean_pool_returns_float32_even_from_float64_hidden_state() -> None:
    hidden = torch.ones((1, 2, 4), dtype=torch.float64)
    mask = torch.ones((1, 2), dtype=torch.long)
    assert mean_pool_and_normalize(hidden, mask).dtype is torch.float32


def test_mean_pool_batch_output_is_n_by_dim_and_unit_norm() -> None:
    torch.manual_seed(0)
    hidden = torch.randn(5, 7, ETTIN_DIM)
    mask = torch.ones((5, 7), dtype=torch.long)
    pooled = mean_pool_and_normalize(hidden, mask)
    assert pooled.shape == (5, ETTIN_DIM)
    norms = torch.linalg.vector_norm(pooled, dim=1)
    assert torch.allclose(norms, torch.ones(5), atol=1e-6)


def test_mean_pool_result_is_invariant_to_trailing_padding() -> None:
    """Padding width must not move the vector."""
    torch.manual_seed(1)
    real = torch.randn(1, 3, ETTIN_DIM)
    tight = mean_pool_and_normalize(real, torch.ones((1, 3), dtype=torch.long))
    padded_hidden = torch.cat([real, torch.randn(1, 4, ETTIN_DIM)], dim=1)
    padded_mask = torch.tensor([[1, 1, 1, 0, 0, 0, 0]])
    assert torch.allclose(tight, mean_pool_and_normalize(padded_hidden, padded_mask), atol=1e-6)

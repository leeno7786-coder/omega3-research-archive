"""The Ettin encoder seat — brain-resident FP32 embedding for the semantic plane.

This is the encoder `SemanticPlane` injects. Weights load in-process inside the
brain venv from a locally verified snapshot: no HTTP hop, no socket, no runtime
worker, no network at any point. The previous embed seat was a gateway service
on :52700, and every failure mode that cost us came from that hop; this one has
no hop to fail.

Two things this module adds over the frozen Program I encoder it is pinned to:

* **Resolution.** Program I took an explicit snapshot path because an experiment
  knows where its artifact is. A seat has to FIND its weights on a machine it
  was not built on, so the location is derived from the environment and never
  hardcoded. Nothing here contains an absolute path.
* **Lifecycle.** The weights are ~67 MB and loading them is the expensive part,
  so the seat is a lazy singleton: importing this module costs nothing, and the
  first `get_encoder()` pays once for the process.

Artifact identity is proven by SHA-256 over every pinned file *before* any model
or tokenizer is constructed, so a tampered or half-downloaded snapshot can never
reach Transformers. That ordering is the whole point and must not be inverted.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Mapping, Sequence

import numpy as np
import torch

ETTIN_MODEL_ID = "tasksource/ettin-17m-embed"
ETTIN_SNAPSHOT = "6132e97e52834eb791fd9333049a1726d01fc898"
ETTIN_DIM = 256
ETTIN_MAX_TOKENS = 1024

# HuggingFace's on-disk cache layout for this repo id.
ETTIN_CACHE_DIRNAME = "models--tasksource--ettin-17m-embed"

# Pinned by content, not by version string. A tag can be moved; a hash cannot.
# Identical to the frozen Program I table and pinned equal to it by test.
ETTIN_HASHES = {
    "model.safetensors": "42965efac128bdb06678f2ffbda2054bb31f286f68c10a5eea5796a170fd68b4",
    "tokenizer.json": "b18211964db7d0ae2a8d2c4a4c00e271c13905859726fe980622488d95db75af",
    "config.json": "afb59c561c4685b23b2e71753256f929fd0f79ea57d8429ee9c7feac10599bb0",
    "1_Pooling/config.json": "ace9f219846dfea1aad340dd7834ec03a05a32c5841738c383af4f41d59e92fc",
    "modules.json": "84e40c8e006c9b1d6c122e02cba9b02458120b5fb0c87b746c41e0207cf642cf",
    "config_sentence_transformers.json": (
        "5bd47d0626eedaa34b22396a3dad81ebe0c257f17ffc77f97f0ac58bec4bbc86"
    ),
}

SNAPSHOT_ENV = "OMEGA_ETTIN_SNAPSHOT"
MODELS_DIR_ENV = "OMEGA_MODELS_DIR"
ROOT_ENV = "OMEGA_ROOT"

_SNAPSHOT_TAIL = Path(ETTIN_CACHE_DIRNAME) / "snapshots" / ETTIN_SNAPSHOT
_READ_CHUNK = 1 << 20


class EttinArtifactError(RuntimeError):
    """The on-disk snapshot is not the pinned Ettin artifact."""


class EttinExecutionError(RuntimeError):
    """The pinned encoder could not execute the frozen FP32 contract."""


class EttinUnavailable(RuntimeError):
    """No pinned snapshot could be found anywhere the seat knows to look."""


# ── resolution ────────────────────────────────────────────────────────────────


def candidate_snapshots(env: Mapping[str, str] | None = None) -> list[Path]:
    """Every place the seat will look, in priority order.

    A pure function of ``env`` -- it touches no filesystem -- so the search order
    is testable on its own and cannot depend on what happens to exist on the
    machine running the test.
    """
    source = os.environ if env is None else env
    candidates: list[Path] = []

    explicit = source.get(SNAPSHOT_ENV)
    if explicit:
        # Used verbatim: an operator naming a path means that path, not a hint.
        candidates.append(Path(explicit))

    models_dir = source.get(MODELS_DIR_ENV)
    if models_dir:
        candidates.append(Path(models_dir) / _SNAPSHOT_TAIL)

    root = source.get(ROOT_ENV)
    if root:
        candidates.append(Path(root) / "memory" / "models" / _SNAPSHOT_TAIL)
    else:
        from core.paths import OMEGA_ROOT

        candidates.append(OMEGA_ROOT / "memory" / "models" / _SNAPSHOT_TAIL)

    seen: set[Path] = set()
    ordered: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return ordered


def _fault(relative: str, path: Path) -> str | None:
    """Return a structural fault for one pinned path, or None if it is readable.

    HuggingFace snapshots are symlink farms pointing into ``../../blobs``, so a
    dangling link is a realistic corruption mode -- and one that NTFS round-trips
    produce -- so it is reported distinctly from an absent entry.
    """
    if path.is_symlink() and not path.exists():
        try:
            target = path.readlink()
        except OSError:  # pragma: no cover - readlink on a live symlink
            target = Path("<unreadable>")
        return f"{relative}: symlink target does not resolve ({target})"
    if not path.exists():
        return f"{relative}: missing"
    if not path.is_file():
        return f"{relative}: not a regular file"
    return None


def _structurally_complete(snapshot: Path) -> bool:
    """Are all pinned files present? Cheap: no hashing, no reads."""
    return all(_fault(rel, snapshot / rel) is None for rel in ETTIN_HASHES)


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_READ_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def verify_ettin_snapshot(snapshot: Path) -> dict[str, str]:
    """Prove ``snapshot`` is the pinned Ettin artifact and return its digests.

    Every pinned file is checked and every fault is collected, so one failure
    reports the whole picture rather than only the first problem hit.
    """
    root = Path(snapshot)
    faults: list[str] = []
    resolved: dict[str, str] = {}

    for relative in sorted(ETTIN_HASHES):
        expected = ETTIN_HASHES[relative]
        path = root / relative

        fault = _fault(relative, path)
        if fault is not None:
            faults.append(fault)
            continue

        try:
            actual = _digest(path)
        except OSError as error:
            faults.append(f"{relative}: unreadable ({error.strerror})")
            continue

        if actual != expected:
            faults.append(f"{relative}: sha256 {actual} != pinned {expected}")
            continue

        resolved[relative] = actual

    if faults:
        detail = "\n  ".join(faults)
        raise EttinArtifactError(
            f"snapshot {root} does not match pinned {ETTIN_MODEL_ID}"
            f"@{ETTIN_SNAPSHOT}:\n  {detail}"
        )

    return resolved


def resolve_snapshot(env: Mapping[str, str] | None = None) -> Path:
    """Find the pinned snapshot, or say precisely where it looked.

    An explicitly named snapshot that fails verification RAISES rather than
    falling through to the next candidate. Falling through would let a planted
    directory silently redirect the seat to weights the operator did not name,
    which is exactly the substitution the hash pin exists to prevent.

    A candidate that is merely incomplete is a miss, not tampering -- that is
    what a half-finished download looks like -- so the search continues past it.
    """
    source = os.environ if env is None else env
    candidates = candidate_snapshots(env=source)

    if source.get(SNAPSHOT_ENV):
        explicit = candidates[0]
        verify_ettin_snapshot(explicit)
        return explicit

    for candidate in candidates:
        if not _structurally_complete(candidate):
            continue
        verify_ettin_snapshot(candidate)
        return candidate

    tried = "\n  ".join(str(candidate) for candidate in candidates)
    raise EttinUnavailable(
        f"no pinned {ETTIN_MODEL_ID}@{ETTIN_SNAPSHOT} snapshot found. Looked in:\n  {tried}\n"
        f"Set {SNAPSHOT_ENV} to a snapshot directory, or {MODELS_DIR_ENV}/{ROOT_ENV} "
        f"to a tree containing {_SNAPSHOT_TAIL}."
    )


# ── the frozen execution contract ─────────────────────────────────────────────


def mean_pool_and_normalize(
    last_hidden_state: torch.Tensor, attention_mask: torch.Tensor
) -> torch.Tensor:
    """The frozen pooling contract: masked mean, then L2 normalisation, in FP32.

    Padding positions contribute nothing, so a vector is independent of the batch
    padding width -- without that, a record's code would depend on which other
    records happened to be encoded alongside it, and the plane would stop being
    replayable. A zero-token row would divide by zero and yield NaN, so it is
    refused outright rather than propagated into the geometry.
    """
    mask = attention_mask.to(last_hidden_state.dtype).unsqueeze(-1)
    counts = mask.sum(dim=1)
    if torch.any(counts <= 0):
        raise EttinExecutionError("tokenizer produced a zero-token attention mask")
    pooled = (last_hidden_state * mask).sum(dim=1) / counts
    return torch.nn.functional.normalize(pooled, p=2, dim=1).to(torch.float32)


class EttinFP32Encoder:
    """Brain-resident FP32 Ettin encoder.

    Satisfies the encoder contract `SemanticPlane` expects:
    ``encode(texts, batch_size=...) -> float32 array of shape (n, 256)``.
    """

    def __init__(self, snapshot: Path, *, device: str = "cpu") -> None:
        from transformers import AutoModel, AutoTokenizer

        self.snapshot = Path(snapshot)
        self.device = device

        self.tokenizer = AutoTokenizer.from_pretrained(
            str(snapshot), local_files_only=True, trust_remote_code=False
        )
        self.model = AutoModel.from_pretrained(
            str(snapshot),
            local_files_only=True,
            trust_remote_code=False,
            # `dtype`, not the older `torch_dtype`. The frozen Program I module
            # still spells it the old way and that is deliberate -- it is parked
            # experiment code and is not edited for cosmetics. The two spellings
            # were proven equivalent on this pinned transformers (44 parameter
            # tensors and the forward output all bit-identical), and the live
            # parity test compares the two encoders' vectors directly, so the
            # difference cannot drift into a behavioural one unnoticed.
            dtype=torch.float32,
        )
        self.model.eval().to(device)
        torch.use_deterministic_algorithms(True)

        hidden_size = int(self.model.config.hidden_size)
        if hidden_size != ETTIN_DIM:
            raise EttinExecutionError(
                f"snapshot declares hidden_size {hidden_size}, pinned dimension is {ETTIN_DIM}"
            )
        dtypes = {parameter.dtype for parameter in self.model.parameters()}
        if dtypes != {torch.float32}:
            raise EttinExecutionError(
                f"model parameters are not pure FP32: {sorted(map(str, dtypes))}"
            )

    def encode(self, texts: Sequence[str], *, batch_size: int = 32) -> np.ndarray:
        """Encode ``texts`` verbatim into FP32[256] unit vectors.

        No prompt, instruction, or prefix is ever added: a stored record and a
        query must land in the same space, and a prefix on one side silently
        rotates it out of that space.
        """
        if batch_size < 1:
            raise EttinExecutionError(f"batch_size must be >= 1, got {batch_size}")
        items = list(texts)
        if any(not isinstance(text, str) for text in items):
            raise EttinExecutionError("encode() accepts str only")
        if not items:
            return np.zeros((0, ETTIN_DIM), dtype=np.float32)

        chunks: list[np.ndarray] = []
        for start in range(0, len(items), batch_size):
            batch = items[start : start + batch_size]
            encoded = self.tokenizer(
                batch,
                truncation=True,
                max_length=ETTIN_MAX_TOKENS,
                padding=True,
                return_tensors="pt",
            )
            encoded = {key: value.to(self.device) for key, value in encoded.items()}
            with torch.inference_mode():
                outputs = self.model(**encoded)
            pooled = mean_pool_and_normalize(
                outputs.last_hidden_state, encoded["attention_mask"]
            )
            chunks.append(pooled.detach().cpu().numpy().astype(np.float32, copy=False))

        vectors = np.concatenate(chunks, axis=0)
        if vectors.shape != (len(items), ETTIN_DIM):
            raise EttinExecutionError(
                f"encoder produced {vectors.shape}, expected {(len(items), ETTIN_DIM)}"
            )
        return vectors


def load_ettin_fp32(snapshot: Path, *, device: str = "cpu") -> EttinFP32Encoder:
    """Verify artifact identity, then construct the encoder. Never the reverse."""
    verify_ettin_snapshot(snapshot)
    return EttinFP32Encoder(Path(snapshot), device=device)


# ── the seat ──────────────────────────────────────────────────────────────────

_SEAT: dict[tuple[str, str], EttinFP32Encoder] = {}


def get_encoder(
    *, env: Mapping[str, str] | None = None, device: str = "cpu"
) -> EttinFP32Encoder:
    """The brain-resident seat: resolve once, load once, reuse for the process.

    Keyed by resolved snapshot and device so a test pointing at a different
    snapshot cannot be served a cached encoder built from another one.
    """
    snapshot = resolve_snapshot(env=env)
    key = (str(snapshot), device)
    encoder = _SEAT.get(key)
    if encoder is None:
        encoder = load_ettin_fp32(snapshot, device=device)
        _SEAT[key] = encoder
    return encoder


def reset_encoder_cache() -> None:
    """Drop every loaded encoder. For tests and for day-zero resets."""
    _SEAT.clear()


__all__ = [
    "ETTIN_CACHE_DIRNAME",
    "ETTIN_DIM",
    "ETTIN_HASHES",
    "ETTIN_MAX_TOKENS",
    "ETTIN_MODEL_ID",
    "ETTIN_SNAPSHOT",
    "EttinArtifactError",
    "EttinExecutionError",
    "EttinFP32Encoder",
    "EttinUnavailable",
    "candidate_snapshots",
    "get_encoder",
    "load_ettin_fp32",
    "mean_pool_and_normalize",
    "reset_encoder_cache",
    "resolve_snapshot",
    "verify_ettin_snapshot",
]

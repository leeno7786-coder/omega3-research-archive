"""Evaluation-only in-process EmbeddingGemma — no HTTP round-trips.

This benchmark owns its local INT8 ONNX artifact and does not use Omega's
runtime or product semantic-perception route (fixed [1,256] input,
'sentence_embedding' output) directly in the eval process.

Provider preference mirrors the model's own reference runner: VitisAI (NPU)
when the installed onnxruntime carries it, else CPU. Deterministic: same
text -> same vector within a provider.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np


def _model_dir() -> Path:
    """Resolve the benchmark's independent embedding artifact.

    Derived from OMEGA_ROOT rather than written absolute: this tree ships on a
    portable SSD, and Windows assigns drive letters by attach order rather than
    by identity, so a literal silently resolves onto whatever disk holds it.
    """
    env = os.environ.get("OMEGA_EMBED_MODEL_DIR", "").strip()
    if env:
        return Path(env)
    from core.paths import RUNTIME_MODELS_ROOT

    return Path(RUNTIME_MODELS_ROOT) / "embeddings" / "embeddinggemma-300m-npu2-int8"


MODEL_DIR = _model_dir()
SEQ = 256  # quantized with a fixed [1, 256] input shape
PAD_ID = 0  # Gemma <pad>

_SESSION = None
_TOKENIZER = None
_OUT_IDX = None


def available() -> bool:
    return (MODEL_DIR / "model.onnx").exists() and (MODEL_DIR / "tokenizer.json").exists()


def _load():
    global _SESSION, _TOKENIZER, _OUT_IDX
    if _SESSION is not None:
        return
    import onnxruntime as ort
    from tokenizers import Tokenizer

    _TOKENIZER = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
    _TOKENIZER.enable_truncation(max_length=SEQ)
    _TOKENIZER.enable_padding(length=SEQ, pad_id=PAD_ID, pad_token="<pad>")

    preferred = ["VitisAIExecutionProvider", "CPUExecutionProvider"]
    avail = ort.get_available_providers()
    providers = [p for p in preferred if p in avail] or ["CPUExecutionProvider"]
    popts = []
    for p in providers:
        if p == "VitisAIExecutionProvider":
            popts.append({"cache_dir": str(MODEL_DIR / "vaip_cache"), "cache_key": "embgemma"})
        else:
            popts.append({})
    sess_opts = ort.SessionOptions()
    sess_opts.log_severity_level = 3
    _SESSION = ort.InferenceSession(
        str(MODEL_DIR / "model.onnx"),
        sess_options=sess_opts,
        providers=providers,
        provider_options=popts,
    )
    _OUT_IDX = [o.name for o in _SESSION.get_outputs()].index("sentence_embedding")


def active_provider() -> str:
    _load()
    return _SESSION.get_providers()[0]


_WORKERS = 4


def embed_texts(texts: list) -> list:
    """L2-normalized 768-d vectors, one per text. Fixed-shape model → one
    in-process run per text, parallelized across a small thread pool (ORT
    sessions are thread-safe and release the GIL during compute)."""
    _load()
    encs = _TOKENIZER.encode_batch([str(t or "") for t in texts])

    def _one(enc):
        ids = np.asarray(enc.ids, dtype=np.int64).reshape(1, SEQ)
        mask = np.asarray(enc.attention_mask, dtype=np.int64).reshape(1, SEQ)
        vec = np.asarray(_SESSION.run(None, {"input_ids": ids, "attention_mask": mask})[_OUT_IDX])[
            0
        ].astype(np.float32)
        norm = float(np.linalg.norm(vec)) or 1.0
        return (vec / norm).tolist()

    if len(encs) <= 2:
        return [_one(enc) for enc in encs]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        return list(pool.map(_one, encs))


if __name__ == "__main__":
    import sys
    import time

    texts = [
        "I graduated with a degree in Business Administration number %d." % i for i in range(32)
    ]
    t0 = time.perf_counter()
    vecs = embed_texts(texts[:4])
    warm = time.perf_counter() - t0
    t0 = time.perf_counter()
    vecs = embed_texts(texts)
    dt = time.perf_counter() - t0
    print(
        json.dumps(
            {
                "provider": active_provider(),
                "warmup_4": round(warm, 2),
                "batch_32_s": round(dt, 2),
                "texts_per_s": round(32 / dt, 1),
                "dim": len(vecs[0]),
            }
        )
    )
    sys.exit(0)

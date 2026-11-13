"""
Regression: loader.identify() must match brain anchor_store when runtime
returns an embedding but no name (cold runtime _face_db).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402

import runtime.face.recognition as loader_mod  # noqa: E402


class _FakeRecognizer:
    def __init__(self, ret):
        self._ret = ret

    def load(self):
        return {"ok": True}

    def unload(self):
        return None

    def identify(self, image_bytes):
        return self._ret


def _identify_with(ret) -> dict:
    from unittest.mock import patch

    recognizer = _FakeRecognizer(ret)
    with (
        patch("runtime.face.recognizer.get_recognizer", return_value=recognizer),
    ):
        return loader_mod.identify(b"\xff\xd8\xff")


def _isolate_store(tmp_path):
    import runtime.face.anchor_store as astore_mod

    path = tmp_path / "face_anchors.json"
    astore_mod._singleton = astore_mod.AnchorStore(path=path)
    return astore_mod.get_anchor_store()


def test_anchor_fallback_when_runtime_has_embedding_only(tmp_path):
    store = _isolate_store(tmp_path)
    emb = np.random.randn(512).astype(np.float32)
    emb = emb / (np.linalg.norm(emb) + 1e-8)
    store.enroll("Noah", emb)

    runtime_ret = {
        "ok": True,
        "name": None,
        "confidence": 0.0,
        "embedding": emb.tolist(),
        "bbox": [10, 10, 100, 100],
    }
    out = _identify_with(runtime_ret)
    assert out.get("recognized_name") == "Noah"
    assert out.get("recognition_source") == "anchor_store"
    assert float(out.get("confidence") or 0) >= 0.4


def test_anchor_fallback_without_bbox_but_embedding(tmp_path):
    store = _isolate_store(tmp_path)
    emb = np.ones(512, dtype=np.float32) * 0.01
    emb = emb / (np.linalg.norm(emb) + 1e-8)
    store.enroll("Operator", emb)

    runtime_ret = {
        "ok": True,
        "name": None,
        "confidence": 0.0,
        "embedding": emb.tolist(),
    }
    out = _identify_with(runtime_ret)
    assert out.get("recognized_name") == "Operator"

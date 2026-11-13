"""Face enrollment admits durable work before the presence cache changes."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest


@pytest.fixture()
def isolated_anchor_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import runtime.face.anchor_store as astore

    anchor_path = tmp_path / "face_anchors.json"
    astore._singleton = None  # noqa: SLF001
    store = astore.AnchorStore(anchor_path)
    monkeypatch.setattr(astore, "get_anchor_store", lambda: store)
    return store


def test_enroll_queues_face_memory_before_presence_learns_it(isolated_anchor_store):
    emb = np.random.RandomState(7).randn(512).astype(np.float32)
    ident = {
        "ok": True,
        "degraded": False,
        "embedding": emb.tolist(),
        "bbox": [0, 0, 10, 10],
        "name": None,
        "confidence": 0.0,
    }

    from runtime.face import recognition as face_rec

    with (
        patch.object(face_rec, "_identify_impl", return_value=ident),
    ):
        result = face_rec.enroll(b"jpeg-bytes", "Noah")

    assert result["ok"] is True
    assert result["queued"] and result["cycle_id"]
    assert isolated_anchor_store.names() == []

    from sensory.operator_presence import enrolled_operator_name

    assert enrolled_operator_name() is None
    from pressure_loop.cognition_queue import lease_next_input

    assert lease_next_input(0).metadata["visual_intake"]["face"]["embedding"] == emb.tolist()

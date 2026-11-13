"""Day-zero factory reset — vision lobe + operator face_images fully cleared."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.day_zero_reset as day_zero_reset  # noqa: E402


def test_wipe_guardian_state_completely_removes_face_images(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "guardian" / "state"
    face_images = state_root / "face_images" / "operator"
    face_images.mkdir(parents=True)
    (face_images / "selfie.jpg").write_bytes(b"jpeg")

    monkeypatch.setattr(day_zero_reset, "GUARDIAN_STATE_ROOT", state_root)

    result = day_zero_reset.wipe_guardian_state_completely()

    assert not (state_root / "face_images").exists()
    assert result["removed_dirs"] == 1


def test_wipe_corpus_removes_vision_json_and_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    iso_root = tmp_path / "omega"
    images = iso_root / "memory" / "700_vision" / "images"
    embeddings = iso_root / "memory" / "700_vision" / "face_embeddings"
    images.mkdir(parents=True)
    embeddings.mkdir(parents=True)
    (images / "thumb.png").write_bytes(b"png")
    (embeddings / "face-1.embedding.json").write_text("{}", encoding="utf-8")
    (embeddings / "snap.webp").write_bytes(b"webp")

    monkeypatch.setattr(day_zero_reset, "OMEGA_ROOT", iso_root)

    removed = day_zero_reset.wipe_corpus()

    assert not (images / "thumb.png").exists()
    assert not (embeddings / "snap.webp").exists()
    assert not (embeddings / "face-1.embedding.json").exists()
    assert removed == 3

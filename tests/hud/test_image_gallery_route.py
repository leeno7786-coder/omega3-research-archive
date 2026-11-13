"""HUD Created-Images gallery DISPLAY end — RED-first against the live
``GET /api/images`` FastAPI route (claim-11, §0.3).

The render+store rail (``runtime/image_store.persist_generated_image``) is
gated elsewhere; this pins the *display* end the HUD gallery rests on:
``GET /api/images`` lists images that were persisted to the canonical store,
rewrites each to a browser ``/api/images/<date>/<name>`` URL, and NEVER leaks
the raw filesystem path to the client.

Hermetic: the image store reads/writes via the module global
``runtime.image_store.IMAGE_ROOT`` (both ``persist_generated_image`` and
``list_generated_images`` reference it), so monkeypatching that single global
to a ``tmp_path`` directory fully isolates the store — nothing touches the
real ``guardian/state/images`` tree.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hud.server import create_app  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_image_store(tmp_path, monkeypatch):
    """Point the canonical image store at a tmp dir (hermetic)."""
    import runtime.image_store as image_store

    monkeypatch.setattr(image_store, "IMAGE_ROOT", tmp_path / "images")
    yield


def _client() -> TestClient:
    return TestClient(create_app())


def _seed_image(prompt: str, *, when: datetime) -> Path:
    """Seed one image into the isolated store via the canonical writer.

    Returns the on-disk PATH (the store deals in paths, not bytes, once
    written) so the test can verify the route's date/name URL mapping.
    """
    from runtime.image_store import persist_generated_image

    # A minimal valid PNG signature is enough — the store writes bytes
    # verbatim and the route only lists/serves by name, never decodes.
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    return persist_generated_image(png_bytes, prompt, when=when)


def test_gallery_lists_seeded_image_with_browser_url():
    """A persisted image appears in GET /api/images with the date/name URL."""
    when = datetime(2026, 6, 12, 14, 30, 5, tzinfo=timezone.utc)
    path = _seed_image("a serene mountain lake", when=when)

    # The seed actually landed in the (isolated) store on the expected day.
    assert path.exists()
    assert path.parent.name == "2026-06-12"

    response = _client().get("/api/images")
    assert response.status_code == 200
    payload = response.json()

    assert payload["ok"] is True
    assert payload["count"] == 1
    images = payload["images"]
    assert len(images) == 1

    item = images[0]
    # The display end maps the stored file to a browser URL: configured VALUE.
    assert item["date"] == "2026-06-12"
    assert item["name"] == path.name
    assert item["url"] == f"/api/images/2026-06-12/{path.name}"


def test_gallery_does_not_leak_filesystem_path():
    """FAIL-MODE: the raw fs ``path`` is popped — never leaked to the client.

    ``list_generated_images`` returns ``path``; the route MUST strip it. If a
    regression removes the ``it.pop("path", None)``, the absolute disk path
    leaks to the browser — this pins that it does not."""
    when = datetime(2026, 6, 12, 9, 0, 0, tzinfo=timezone.utc)
    path = _seed_image("leak check", when=when)

    item = _client().get("/api/images").json()["images"][0]

    assert "path" not in item
    # And specifically the real on-disk location is absent from every value.
    assert str(path) not in item.values()


def test_gallery_empty_when_store_empty():
    """FAIL-MODE (other gate direction): no images persisted -> empty list,
    not a 500 / not a stale real-store listing. Proves the isolation holds
    and the route reports zero rather than fabricating entries."""
    response = _client().get("/api/images")
    assert response.status_code == 200
    payload = response.json()

    assert payload["ok"] is True
    assert payload["count"] == 0
    assert payload["images"] == []


def test_gallery_orders_newest_first():
    """Two images across days list newest-first (the gallery's stated order)."""
    older = _seed_image("older one", when=datetime(2026, 6, 10, 8, 0, 0, tzinfo=timezone.utc))
    newer = _seed_image("newer one", when=datetime(2026, 6, 12, 8, 0, 0, tzinfo=timezone.utc))

    images = _client().get("/api/images").json()["images"]
    assert [it["name"] for it in images] == [newer.name, older.name]

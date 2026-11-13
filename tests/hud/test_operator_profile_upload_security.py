"""Operator profile upload must reject non-image payloads."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hud.server import create_app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def test_profile_upload_rejects_html_disguised_as_png(client: TestClient):
    payload = b"<html><body><script>alert(1)</script></body></html>"
    response = client.post(
        "/api/settings/operator-profile/upload-picture",
        files={"file": ("evil.png", payload, "image/png")},
    )
    assert response.status_code == 400
    assert "image" in response.json()["detail"].lower()

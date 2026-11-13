"""HUD serves hostable legal pages for Google OAuth consent URLs."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from hud.server import create_app

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
PRIVACY = ROOT / "docs" / "legal" / "privacy-policy.md"


def test_privacy_policy_source_exists_and_names_omega():
    text = PRIVACY.read_text(encoding="utf-8")
    assert "Omega 3.0" in text
    assert "AES-256-GCM" in text
    assert "SourceLedger" in text
    assert "myaccount.google.com/connections" in text


def test_get_legal_privacy_returns_html_from_source():
    client = TestClient(create_app())
    response = client.get("/legal/privacy")

    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    body = response.text
    assert "Omega 3.0" in body
    assert "Privacy Policy" in body or "privacy" in body.lower()
    assert "AES-256-GCM" in body
    assert "SourceLedger" in body

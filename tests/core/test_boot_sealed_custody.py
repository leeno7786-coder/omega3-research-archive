"""Boot names a locked sealed vault before any consumer trips over it."""

from __future__ import annotations

from pathlib import Path

import pytest

import boot
from core.required_substrate import RequiredSubstrateError
from mcp_platform import sealed_secrets


@pytest.fixture
def authorization_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "authorization"
    root.mkdir()
    monkeypatch.setattr("core.paths.MCP_AUTHORIZATION_ROOT", root)
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_ENV, raising=False)
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_FILE_ENV, raising=False)
    return root


def test_no_sealed_key_needs_no_passphrase(authorization_root: Path) -> None:
    report: dict[str, object] = {}

    boot._boot_sealed_custody(report)

    assert report["custody"] == "unbound"
    (authorization_root / "oauth-vault-key.dpapi").write_bytes(b"opaque")
    boot._boot_sealed_custody(report)
    assert report["custody"] == "desktop"


def test_sealed_key_without_a_passphrase_fails_closed_with_the_real_cause(
    authorization_root: Path,
) -> None:
    sealed_secrets.write_sealed_file(
        authorization_root / sealed_secrets.VAULT_KEY_FILENAME,
        b"k" * 32,
        "portable-pw",
        purpose=sealed_secrets.VAULT_KEY_PURPOSE,
    )

    with pytest.raises(RequiredSubstrateError) as exc_info:
        boot._boot_sealed_custody({})

    assert exc_info.value.component == "custody"
    assert exc_info.value.reason == "sealed_passphrase_required"
    assert sealed_secrets.PASSPHRASE_ENV in str(exc_info.value)


def test_sealed_key_opens_with_the_configured_passphrase_and_locks_with_another(
    authorization_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sealed_secrets.write_sealed_file(
        authorization_root / sealed_secrets.VAULT_KEY_FILENAME,
        b"k" * 32,
        "portable-pw",
        purpose=sealed_secrets.VAULT_KEY_PURPOSE,
    )
    report: dict[str, object] = {}

    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "portable-pw")
    boot._boot_sealed_custody(report)
    assert report["custody"] == "sealed"

    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "wrong")
    with pytest.raises(RequiredSubstrateError) as exc_info:
        boot._boot_sealed_custody({})
    assert exc_info.value.reason == "sealed_secret_locked"

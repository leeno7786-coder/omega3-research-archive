"""Passphrase-sealed custody: portable, fail-closed, purpose-bound."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mcp_platform import sealed_secrets
from mcp_platform.oauth_vault import OAuthClientCredential, OAuthCredentialVault
from mcp_platform.sealed_secrets import (
    PortableOAuthKeyProvider,
    SealedOAuthKeyProvider,
    SealedSecretError,
    passphrase_from_environment,
    read_sealed_seed,
    seal,
    unseal,
    write_sealed_file,
)

_SECRET = bytes(range(32))
_PURPOSE = "omega.test-seed"


@pytest.fixture(autouse=True)
def _no_ambient_passphrase(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_ENV, raising=False)
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_FILE_ENV, raising=False)


def test_seal_round_trips_and_never_stores_plaintext() -> None:
    blob = seal(_SECRET, "correct horse", purpose=_PURPOSE)

    assert _SECRET not in blob
    document = json.loads(blob)
    assert document["format"] == sealed_secrets.FORMAT
    assert document["purpose"] == _PURPOSE
    assert document["kdf"] == {"name": "scrypt", "n": 2**15, "r": 8, "p": 1}
    assert unseal(blob, "correct horse", purpose=_PURPOSE) == _SECRET


def test_two_seals_of_the_same_secret_differ_and_both_open() -> None:
    first = seal(_SECRET, "pw", purpose=_PURPOSE)
    second = seal(_SECRET, "pw", purpose=_PURPOSE)

    assert first != second
    assert unseal(first, "pw", purpose=_PURPOSE) == unseal(second, "pw", purpose=_PURPOSE)


def test_wrong_passphrase_tampering_and_purpose_mismatch_all_lock() -> None:
    blob = seal(_SECRET, "pw", purpose=_PURPOSE)

    with pytest.raises(SealedSecretError, match="^sealed_secret_locked$"):
        unseal(blob, "pw2", purpose=_PURPOSE)
    with pytest.raises(SealedSecretError, match="^sealed_secret_purpose_mismatch$"):
        unseal(blob, "pw", purpose="omega.other")

    document = json.loads(blob)
    tampered = dict(document, purpose=_PURPOSE)
    raw = bytearray(sealed_secrets._unb64(document["ciphertext"]))
    raw[0] ^= 0x01
    tampered["ciphertext"] = sealed_secrets._b64(bytes(raw))
    with pytest.raises(SealedSecretError, match="^sealed_secret_locked$"):
        unseal(json.dumps(tampered).encode(), "pw", purpose=_PURPOSE)

    for broken in (b"", b"not json", b"[]", json.dumps({"format": "x"}).encode()):
        with pytest.raises(SealedSecretError, match="^sealed_secret_invalid$"):
            unseal(broken, "pw", purpose=_PURPOSE)
    huge_n = dict(document, kdf={"name": "scrypt", "n": 2**30, "r": 8, "p": 1})
    with pytest.raises(SealedSecretError, match="^sealed_secret_invalid$"):
        unseal(json.dumps(huge_n).encode(), "pw", purpose=_PURPOSE)


def test_write_sealed_file_is_exclusive_and_verifies(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "seed.sealed"

    write_sealed_file(target, _SECRET, "pw", purpose=_PURPOSE)

    assert target.is_file()
    with pytest.raises(SealedSecretError, match="^sealed_secret_exists$"):
        write_sealed_file(target, b"other", "pw", purpose=_PURPOSE)
    assert unseal(target.read_bytes(), "pw", purpose=_PURPOSE) == _SECRET


def test_passphrase_comes_from_env_then_file_then_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert passphrase_from_environment() is None

    secret_file = tmp_path / "passphrase.txt"
    secret_file.write_text("from-file\n", encoding="utf-8")
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_FILE_ENV, str(secret_file))
    assert passphrase_from_environment() == "from-file"

    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "from-env")
    assert passphrase_from_environment() == "from-env"

    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "")
    with pytest.raises(SealedSecretError, match="^sealed_passphrase_invalid$"):
        passphrase_from_environment()

    monkeypatch.delenv(sealed_secrets.PASSPHRASE_ENV)
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_FILE_ENV, str(tmp_path / "absent"))
    with pytest.raises(SealedSecretError, match="^sealed_passphrase_unreadable$"):
        passphrase_from_environment()


def test_read_sealed_seed_requires_exactly_32_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "pw")
    good = tmp_path / "good.sealed"
    short = tmp_path / "short.sealed"
    write_sealed_file(good, _SECRET, "pw", purpose=_PURPOSE)
    write_sealed_file(short, _SECRET[:31], "pw", purpose=_PURPOSE)

    assert read_sealed_seed(good, purpose=_PURPOSE) == _SECRET
    with pytest.raises(SealedSecretError, match="^sealed_secret_invalid$"):
        read_sealed_seed(short, purpose=_PURPOSE)
    with pytest.raises(SealedSecretError, match="^sealed_secret_unreadable$"):
        read_sealed_seed(tmp_path / "absent.sealed", purpose=_PURPOSE)

    monkeypatch.delenv(sealed_secrets.PASSPHRASE_ENV)
    with pytest.raises(SealedSecretError, match="^sealed_passphrase_required$"):
        read_sealed_seed(good, purpose=_PURPOSE)


def test_sealed_vault_key_is_born_once_and_reopens_across_machines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sealed key depends on the passphrase alone, never on this host."""
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "portable-pw")
    vault_path = tmp_path / "oauth-vault.json"
    provider = SealedOAuthKeyProvider(tmp_path, vault_path)

    key = provider()

    assert len(key) == 32
    assert (tmp_path / "oauth-vault-key.sealed").is_file()
    assert key not in (tmp_path / "oauth-vault-key.sealed").read_bytes()
    assert SealedOAuthKeyProvider(tmp_path, vault_path)() == key
    # Another machine = a fresh process with only the drive and the passphrase.
    assert unseal(
        (tmp_path / "oauth-vault-key.sealed").read_bytes(),
        "portable-pw",
        purpose=sealed_secrets.VAULT_KEY_PURPOSE,
    ) == key

    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "wrong")
    with pytest.raises(ValueError, match="^oauth_vault_locked$"):
        SealedOAuthKeyProvider(tmp_path, vault_path)()
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_ENV)
    with pytest.raises(ValueError, match="^oauth_vault_locked$"):
        SealedOAuthKeyProvider(tmp_path, vault_path)()


def test_sealed_provider_never_births_a_second_key_over_an_existing_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "pw")
    (tmp_path / "oauth-vault.json").write_bytes(b"{}")

    with pytest.raises(ValueError, match="^oauth_vault_locked$"):
        SealedOAuthKeyProvider(tmp_path, tmp_path / "oauth-vault.json")()
    assert not (tmp_path / "oauth-vault-key.sealed").exists()


def test_portable_provider_prefers_sealed_then_desktop_then_births_by_passphrase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "oauth-vault.json"
    desktop_calls: list[str] = []

    class _Desktop:
        path = tmp_path / "oauth-vault-key.dpapi"

        def __call__(self) -> bytes:
            desktop_calls.append("desktop")
            return b"d" * 32

    # No passphrase, nothing on disk: the desktop custody births as before.
    provider = PortableOAuthKeyProvider(tmp_path, vault_path, desktop_provider=_Desktop())
    assert provider() == b"d" * 32
    assert desktop_calls == ["desktop"]

    # A passphrase and no existing vault: a sealed key is born, DPAPI untouched.
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "pw")
    provider = PortableOAuthKeyProvider(tmp_path, vault_path, desktop_provider=_Desktop())
    sealed_key = provider()
    assert (tmp_path / "oauth-vault-key.sealed").is_file()
    assert desktop_calls == ["desktop"]

    # An existing DPAPI file with no sealed twin keeps using the desktop custody.
    (tmp_path / "oauth-vault-key.sealed").unlink()
    _Desktop.path.write_bytes(b"opaque")
    assert PortableOAuthKeyProvider(tmp_path, vault_path, desktop_provider=_Desktop())() == b"d" * 32

    # Once the sealed twin exists it wins even with the DPAPI file still present.
    write_sealed_file(
        tmp_path / "oauth-vault-key.sealed", sealed_key, "pw", purpose=sealed_secrets.VAULT_KEY_PURPOSE
    )
    assert PortableOAuthKeyProvider(tmp_path, vault_path, desktop_provider=_Desktop())() == sealed_key


def test_default_vault_custody_is_sealed_when_a_passphrase_is_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "portable-pw")
    credential = OAuthClientCredential(client_id="public-id", client_secret="private-value")

    OAuthCredentialVault(tmp_path).store_credential(
        "oauth-client/provider/secret", credential, expected_revision=None
    )

    assert (tmp_path / "oauth-vault-key.sealed").is_file()
    assert not (tmp_path / "oauth-vault-key.dpapi").exists()
    assert b"private-value" not in (tmp_path / "oauth-vault-key.sealed").read_bytes()
    assert OAuthCredentialVault(tmp_path).load_credential("oauth-client/provider/secret") == (
        0,
        credential,
    )
    monkeypatch.delenv(sealed_secrets.PASSPHRASE_ENV)
    with pytest.raises(ValueError, match="^oauth_vault_locked$"):
        OAuthCredentialVault(tmp_path).load_credential("oauth-client/provider/secret")


@pytest.mark.skipif(os.name != "nt", reason="the DPAPI side of the move needs Windows")
def test_reseal_vault_key_moves_dpapi_custody_into_the_sealed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault_path = tmp_path / "oauth-vault.json"
    credential = OAuthClientCredential(client_id="public-id", client_secret="private-value")
    OAuthCredentialVault(tmp_path).store_credential(
        "oauth-client/provider/secret", credential, expected_revision=None
    )
    assert (tmp_path / "oauth-vault-key.dpapi").is_file()

    sealed_path = sealed_secrets.reseal_vault_key(tmp_path, vault_path, "pw")

    assert sealed_path == tmp_path / "oauth-vault-key.sealed"
    monkeypatch.setenv(sealed_secrets.PASSPHRASE_ENV, "pw")
    assert OAuthCredentialVault(tmp_path).load_credential("oauth-client/provider/secret") == (
        0,
        credential,
    )
    (tmp_path / "oauth-vault-key.dpapi").unlink()
    assert OAuthCredentialVault(tmp_path).load_credential("oauth-client/provider/secret") == (
        0,
        credential,
    )

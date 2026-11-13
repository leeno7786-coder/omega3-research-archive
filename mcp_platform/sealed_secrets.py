"""Passphrase-sealed, machine-independent custody for Omega's private material.

Omega is portable: the whole install travels on one drive between machines,
so a key that only *this* Windows user on *this* machine can open (DPAPI) is
the wrong custody for anything that must survive a move. This module seals a
small secret (an AES vault key, an Ed25519 signing seed) under a passphrase
the operator carries in their head or in their own secret manager:

* file format ``omega.sealed.v1`` -- JSON, versioned, self-describing KDF
  parameters, AES-256-GCM with the sealed ``purpose`` bound as associated
  data, so a vault-key file can never be presented as a signing seed;
* key derivation scrypt (N=2**15, r=8, p=1) over a random 16-byte salt;
* the passphrase comes from ``OMEGA_SEAL_PASSPHRASE`` or, for unattended
  hosts, the file named by ``OMEGA_SEAL_PASSPHRASE_FILE``. Nothing in the
  repository, the drive, or the environment layout implies the passphrase.

Failure is closed and attributable: every unreadable, foreign, tampered or
locked file raises ``ValueError("sealed_secret_locked")`` (or a more specific
``sealed_secret_*`` reason) and never a partial key.
"""

from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from core import paths as core_paths

FORMAT = "omega.sealed.v1"
PASSPHRASE_ENV = "OMEGA_SEAL_PASSPHRASE"
PASSPHRASE_FILE_ENV = "OMEGA_SEAL_PASSPHRASE_FILE"

VAULT_KEY_PURPOSE = "omega.oauth-vault-key"
FEATURED_SIGNING_SEED_PURPOSE = "omega.featured-marketplace-ed25519-seed"
OAUTH_PROFILE_SIGNING_SEED_PURPOSE = "omega.oauth-profiles-ed25519-seed"

# Release signing seeds are NOT runtime state: a day-zero reset clears
# ``guardian/state`` on purpose, and putting irreplaceable signing material
# inside it destroyed both seeds on 2026-09-06. They live beside that tree, on
# the drive (so they travel with a portable install), never inside it.
RELEASE_SECRETS_ROOT = core_paths.OMEGA_ROOT / "guardian" / "release-secrets"
FEATURED_SIGNING_SEED_PATH = RELEASE_SECRETS_ROOT / "featured-marketplace-ed25519.sealed"
OAUTH_PROFILE_SIGNING_SEED_PATH = RELEASE_SECRETS_ROOT / "oauth-profiles-ed25519.sealed"
VAULT_KEY_FILENAME = "oauth-vault-key.sealed"

_KEY_BYTES = 32
_SALT_BYTES = 16
_NONCE_BYTES = 12
_MAX_SEALED_FILE_BYTES = 4096
_MAX_PASSPHRASE_BYTES = 1024
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_MAX_SCRYPT_N = 2**20


class SealedSecretError(ValueError):
    """A sealed file could not be produced or opened; ``str(exc)`` is the reason."""


def passphrase_from_environment() -> str | None:
    """Return the operator passphrase, or ``None`` when no unlock material is configured.

    ``OMEGA_SEAL_PASSPHRASE`` wins; otherwise the file named by
    ``OMEGA_SEAL_PASSPHRASE_FILE`` is read (first line, trailing newline
    stripped). A configured-but-unreadable file is a hard failure, never a
    silent fall-through to "no passphrase".
    """

    direct = os.environ.get(PASSPHRASE_ENV)
    if direct is not None:
        if not direct or len(direct.encode("utf-8")) > _MAX_PASSPHRASE_BYTES:
            raise SealedSecretError("sealed_passphrase_invalid")
        return direct
    file_name = os.environ.get(PASSPHRASE_FILE_ENV)
    if not file_name:
        return None
    try:
        raw = Path(file_name).read_bytes()
    except OSError:
        raise SealedSecretError("sealed_passphrase_unreadable") from None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise SealedSecretError("sealed_passphrase_invalid") from None
    text = text.split("\n", 1)[0].rstrip("\r")
    if not text or len(text.encode("utf-8")) > _MAX_PASSPHRASE_BYTES:
        raise SealedSecretError("sealed_passphrase_invalid")
    return text


def require_passphrase() -> str:
    passphrase = passphrase_from_environment()
    if passphrase is None:
        raise SealedSecretError("sealed_passphrase_required")
    return passphrase


def _derive(passphrase: str, salt: bytes, *, n: int, r: int, p: int) -> bytes:
    return Scrypt(salt=salt, length=_KEY_BYTES, n=n, r=r, p=p).derive(passphrase.encode("utf-8"))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value: object, *, size: int | None = None) -> bytes:
    if type(value) is not str:
        raise SealedSecretError("sealed_secret_invalid")
    try:
        raw = base64.b64decode(value, validate=True)
    except (TypeError, ValueError):
        raise SealedSecretError("sealed_secret_invalid") from None
    if _b64(raw) != value or (size is not None and len(raw) != size):
        raise SealedSecretError("sealed_secret_invalid")
    return raw


def seal(secret: bytes, passphrase: str, *, purpose: str) -> bytes:
    """Return the sealed file bytes for ``secret`` bound to ``purpose``."""

    if type(secret) is not bytes or not secret or len(secret) > 1024:
        raise SealedSecretError("sealed_secret_invalid")
    if type(purpose) is not str or not purpose or not purpose.isascii():
        raise SealedSecretError("sealed_secret_invalid")
    if type(passphrase) is not str or not passphrase:
        raise SealedSecretError("sealed_passphrase_invalid")
    salt = os.urandom(_SALT_BYTES)
    nonce = os.urandom(_NONCE_BYTES)
    key = _derive(passphrase, salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
    ciphertext = AESGCM(key).encrypt(nonce, secret, _associated_data(purpose))
    document = {
        "format": FORMAT,
        "purpose": purpose,
        "kdf": {"name": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P},
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "ciphertext": _b64(ciphertext),
    }
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _associated_data(purpose: str) -> bytes:
    return f"{FORMAT}:{purpose}".encode("ascii")


def unseal(blob: bytes, passphrase: str, *, purpose: str) -> bytes:
    """Open a sealed file. Wrong passphrase, wrong purpose and tampering all lock."""

    if type(blob) is not bytes or not blob or len(blob) > _MAX_SEALED_FILE_BYTES:
        raise SealedSecretError("sealed_secret_invalid")
    try:
        document = json.loads(blob.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise SealedSecretError("sealed_secret_invalid") from None
    if type(document) is not dict or document.get("format") != FORMAT:
        raise SealedSecretError("sealed_secret_invalid")
    if document.get("purpose") != purpose:
        raise SealedSecretError("sealed_secret_purpose_mismatch")
    kdf = document.get("kdf")
    if (
        type(kdf) is not dict
        or kdf.get("name") != "scrypt"
        or any(type(kdf.get(name)) is not int or kdf[name] <= 0 for name in ("n", "r", "p"))
        or kdf["n"] > _MAX_SCRYPT_N
        or kdf["n"] & (kdf["n"] - 1)
        or kdf["r"] > 32
        or kdf["p"] > 16
    ):
        raise SealedSecretError("sealed_secret_invalid")
    salt = _unb64(document.get("salt"))
    nonce = _unb64(document.get("nonce"), size=_NONCE_BYTES)
    ciphertext = _unb64(document.get("ciphertext"))
    if len(salt) < 8:
        raise SealedSecretError("sealed_secret_invalid")
    if type(passphrase) is not str or not passphrase:
        raise SealedSecretError("sealed_passphrase_required")
    key = _derive(passphrase, salt, n=kdf["n"], r=kdf["r"], p=kdf["p"])
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, _associated_data(purpose))
    except InvalidTag:
        raise SealedSecretError("sealed_secret_locked") from None


def write_sealed_file(path: Path, secret: bytes, passphrase: str, *, purpose: str) -> None:
    """Create ``path`` exclusively (never overwrite), fsync it, and verify it reopens."""

    target = Path(path)
    blob = seal(secret, passphrase, purpose=purpose)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
    except FileExistsError:
        raise SealedSecretError("sealed_secret_exists") from None
    except OSError:
        raise SealedSecretError("sealed_secret_write_failed") from None
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        if unseal(target.read_bytes(), passphrase, purpose=purpose) != secret:
            raise SealedSecretError("sealed_secret_write_failed")
    except BaseException:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def read_sealed_file(path: Path, *, purpose: str, passphrase: str | None = None) -> bytes:
    """Open the sealed file at ``path`` with the operator passphrase."""

    try:
        blob = Path(path).read_bytes()
    except FileNotFoundError:
        raise SealedSecretError("sealed_secret_unreadable") from None
    except OSError:
        raise SealedSecretError("sealed_secret_unreadable") from None
    if passphrase is None:
        passphrase = require_passphrase()
    return unseal(blob, passphrase, purpose=purpose)


def read_sealed_seed(path: Path, *, purpose: str) -> bytes:
    """A sealed Ed25519 seed / AES key: exactly 32 bytes or locked."""

    secret = read_sealed_file(path, purpose=purpose)
    if len(secret) != _KEY_BYTES:
        raise SealedSecretError("sealed_secret_invalid")
    return secret


class SealedOAuthKeyProvider:
    """The portable OAuth vault key: ``<root>/oauth-vault-key.sealed`` under the passphrase.

    Same 32-byte contract as ``DesktopOAuthKeyProvider``. Loads the sealed key
    when the file exists; births one when neither the file nor the vault exists
    and a passphrase is configured; otherwise fails closed with
    ``oauth_vault_locked`` so a moved drive without its passphrase never
    silently starts a second, empty vault.
    """

    def __init__(self, root: Path, vault_path: Path) -> None:
        self.root = Path(root)
        self.vault_path = Path(vault_path)
        self.path = self.root / VAULT_KEY_FILENAME

    def __call__(self) -> bytes:
        try:
            passphrase = passphrase_from_environment()
        except SealedSecretError as exc:
            raise ValueError("oauth_vault_locked") from exc
        if self.path.exists():
            if passphrase is None:
                raise ValueError("oauth_vault_locked")
            try:
                key = read_sealed_file(self.path, purpose=VAULT_KEY_PURPOSE, passphrase=passphrase)
            except SealedSecretError as exc:
                raise ValueError("oauth_vault_locked") from exc
            if len(key) != _KEY_BYTES:
                raise ValueError("oauth_vault_locked")
            return key
        if passphrase is None or self.vault_path.exists():
            raise ValueError("oauth_vault_locked")
        key = os.urandom(_KEY_BYTES)
        try:
            write_sealed_file(self.path, key, passphrase, purpose=VAULT_KEY_PURPOSE)
        except SealedSecretError as exc:
            if str(exc) == "sealed_secret_exists":
                return self()
            raise ValueError("oauth_vault_locked") from exc
        return key


class PortableOAuthKeyProvider:
    """Sealed custody first, this machine's DPAPI custody as the fallback.

    Resolution order for an existing installation: a sealed key file, then a
    DPAPI-wrapped key file, then the legacy ledger-derived key. A brand-new
    vault is born sealed when a passphrase is configured and DPAPI-wrapped
    otherwise, so a desktop without a passphrase keeps working exactly as
    before while a portable install never grows a machine-bound key.
    """

    def __init__(
        self,
        root: Path,
        vault_path: Path,
        *,
        desktop_provider: Callable[[], bytes] | None = None,
    ) -> None:
        self.root = Path(root)
        self.vault_path = Path(vault_path)
        self.sealed = SealedOAuthKeyProvider(self.root, self.vault_path)
        if desktop_provider is None:
            from mcp_platform.desktop_secrets import DesktopOAuthKeyProvider

            desktop_provider = DesktopOAuthKeyProvider(self.root, self.vault_path)
        self.desktop = desktop_provider

    def __call__(self) -> bytes:
        if self.sealed.path.exists():
            return self.sealed()
        desktop_path = getattr(self.desktop, "path", None)
        if desktop_path is not None and Path(desktop_path).exists():
            return self.desktop()
        try:
            passphrase = passphrase_from_environment()
        except SealedSecretError as exc:
            raise ValueError("oauth_vault_locked") from exc
        if passphrase is not None and not self.vault_path.exists():
            return self.sealed()
        return self.desktop()


def reseal_vault_key(root: Path, vault_path: Path, passphrase: str) -> Path:
    """Move an existing DPAPI-held vault key into sealed custody on this machine.

    Opens the DPAPI file (only this Windows user can), writes the sealed twin,
    and leaves the DPAPI file in place: the sealed file wins from now on and
    the operator deletes the DPAPI file once the move is verified.
    """

    from mcp_platform.desktop_secrets import DesktopOAuthKeyProvider

    key = DesktopOAuthKeyProvider(root, vault_path)()
    sealed_path = Path(root) / VAULT_KEY_FILENAME
    write_sealed_file(sealed_path, key, passphrase, purpose=VAULT_KEY_PURPOSE)
    return sealed_path


__all__ = [
    "FEATURED_SIGNING_SEED_PATH",
    "FEATURED_SIGNING_SEED_PURPOSE",
    "FORMAT",
    "OAUTH_PROFILE_SIGNING_SEED_PATH",
    "OAUTH_PROFILE_SIGNING_SEED_PURPOSE",
    "PASSPHRASE_ENV",
    "PASSPHRASE_FILE_ENV",
    "PortableOAuthKeyProvider",
    "RELEASE_SECRETS_ROOT",
    "SealedOAuthKeyProvider",
    "SealedSecretError",
    "VAULT_KEY_PURPOSE",
    "passphrase_from_environment",
    "read_sealed_file",
    "read_sealed_seed",
    "require_passphrase",
    "reseal_vault_key",
    "seal",
    "unseal",
    "write_sealed_file",
]

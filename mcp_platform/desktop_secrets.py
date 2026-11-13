"""Windows user-bound custody for Omega's local OAuth vault key."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from core import paths as core_paths

_KEY_BYTES = 32
_KEY_INFO = b"omega-mcp-authorization-aes256-v1"
_DESCRIPTION = "Omega OAuth vault key v1"


class _DataBlob(ctypes.Structure):
    _fields_ = (("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte)))


def _blob(value: bytes) -> tuple[_DataBlob, object]:
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    return _DataBlob(len(value), buffer), buffer


def _dpapi_protect(value: bytes) -> bytes:
    if os.name != "nt":
        raise ValueError("oauth_vault_locked")
    source, source_buffer = _blob(value)
    protected = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        wintypes.LPCWSTR,
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptProtectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not crypt32.CryptProtectData(
        ctypes.byref(source),
        _DESCRIPTION,
        None,
        None,
        None,
        0,
        ctypes.byref(protected),
    ):
        raise ValueError("oauth_vault_locked")
    del source_buffer
    try:
        return ctypes.string_at(protected.data, protected.size)
    finally:
        kernel32.LocalFree(protected.data)


def _dpapi_unprotect(value: bytes) -> bytes:
    if os.name != "nt":
        raise ValueError("oauth_vault_locked")
    source, source_buffer = _blob(value)
    clear = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptUnprotectData.argtypes = (
        ctypes.POINTER(_DataBlob),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(_DataBlob),
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(_DataBlob),
    )
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    description = wintypes.LPWSTR()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(source),
        ctypes.byref(description),
        None,
        None,
        None,
        0,
        ctypes.byref(clear),
    ):
        raise ValueError("oauth_vault_locked")
    del source_buffer
    try:
        result = ctypes.string_at(clear.data, clear.size)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(clear.data)
    if len(result) != _KEY_BYTES:
        raise ValueError("oauth_vault_locked")
    return result


def _legacy_key() -> bytes | None:
    try:
        seed = core_paths.GUARDIAN_LEDGER_KEY_PATH.read_bytes()
    except OSError:
        return None
    if len(seed) != _KEY_BYTES:
        raise ValueError("oauth_vault_locked")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=None,
        info=_KEY_INFO,
    ).derive(seed)


class DesktopOAuthKeyProvider:
    """Load or birth one DPAPI-wrapped key without exposing plaintext on disk."""

    def __init__(self, root: Path, vault_path: Path) -> None:
        self.root = Path(root)
        self.vault_path = Path(vault_path)
        self.path = self.root / "oauth-vault-key.dpapi"

    def __call__(self) -> bytes:
        try:
            protected = self.path.read_bytes()
        except FileNotFoundError:
            protected = None
        except OSError as exc:
            raise ValueError("oauth_vault_locked") from exc
        if protected is not None:
            try:
                return _dpapi_unprotect(protected)
            except (OSError, ValueError) as exc:
                raise ValueError("oauth_vault_locked") from exc

        legacy = _legacy_key()
        if self.vault_path.exists():
            if legacy is None:
                raise ValueError("oauth_vault_locked")
            return legacy

        key = os.urandom(_KEY_BYTES)
        if len(key) != _KEY_BYTES:
            raise ValueError("oauth_vault_locked")
        protected = _dpapi_protect(key)
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
                0o600,
            )
        except FileExistsError:
            return _dpapi_unprotect(self.path.read_bytes())
        except OSError as exc:
            raise ValueError("oauth_vault_locked") from exc
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as handle:
                handle.write(protected)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        if _dpapi_unprotect(self.path.read_bytes()) != key:
            raise ValueError("oauth_vault_locked")
        return key


__all__ = ["DesktopOAuthKeyProvider"]

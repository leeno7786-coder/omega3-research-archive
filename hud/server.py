"""
HUD FastAPI app — serves omega_interface.html + JSON telemetry.

Routes:
    GET  /                       -> hud/omega_interface.html
    GET  /healthz                -> {"status": "ok", "ts": ...}
    GET  /api/status             -> top status bar (5 pills)
    GET  /api/boot/state         -> boot banner state
    GET  /api/panel/{topic}      -> detail panel content
                                    topic in {brain, memory, omega,
                                              autoloop, dev}

Architecture:
    - GET / serves hud/omega_interface.html (single-file operator surface).
    - /api/omega/* paths mirror /api/* for compatibility with the archived SPA.
    - Telemetry functions in hud.telemetry are the SINGLE indirection
      between the FastAPI surface and Omega internals — easy to mock
      for tests + easy to swap backing implementations panel-by-panel.
    - Google Fonts remain CDN-hosted where referenced in omega_interface.html.
    - CORS is intentionally NOT configured: the HUD is same-origin;
      any cross-origin client is suspicious in this trust model.
"""

from __future__ import annotations

import asyncio
import math
import json as _json
import hashlib
import logging
import os
import re
import secrets
import sys as _sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi import (
    FastAPI,
    HTTPException,
    Request,
    Body,
    UploadFile,
    File,
    Form,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    StreamingResponse,
    FileResponse,
    Response,
)
from starlette.concurrency import run_in_threadpool

from pressure_loop.cognition_queue import next_arrival_seq, reserve_operator_arrival, abandon_arrival_reservation
from typing import Any, Dict, List, Optional

from core.paths import OMEGA_ROOT, GUARDIAN_STATE_ROOT, STAGING_ROOT, portable_python_path
from hud import telemetry, events as hud_events, settings as hud_settings, qwen_skills
from hud.omega_api_mirror import OmegaSpaApiPrefixMiddleware
from hud import chat_history as hud_chat_history
from mcp_platform.external_switch import (
    ExternalMcpSwitchConflict,
    ExternalMcpSwitchUnreadable,
)
from mcp_platform.host import McpHostControlError
from mcp_platform.registry import (
    McpPackageNotInstalled,
    McpRegistryConflict,
    McpRegistryUnreadable,
)
from mcp_platform.service import (
    mcp_status,
    set_external_mcp_enabled,
    set_mcp_package_enabled,
)
from mcp_platform.plugin_lifecycle import (
    MAX_CATALOG_PAGE_SIZE,
    PluginUpdatePending,
    get_default_plugin_lifecycle_service,
)
from mcp_platform.canonical import canonical_bytes, sha256_hex
from mcp_platform.plugin_directory import PluginDirectoryError, PluginDirectoryService
from mcp_platform.plugin_permissions import get_default_plugin_permission_store
from pressure_loop.operator_chat import handle_operator_chat
from pressure_loop.plugin_continuations import (
    PluginPermissionDecision,
    enqueue_plugin_permission_decision,
)


# Upload staging — under guardian/state/ so it survives boots and
# tools can discover it via the single-socket convention. Uploads
# land in subdirs by kind (docs/, images/, other/) for filtering.
_UPLOAD_ROOT = GUARDIAN_STATE_ROOT / "uploads"
_UPLOAD_KINDS = {
    "image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"},
    "doc": {
        ".txt",
        ".md",
        ".pdf",
        ".docx",
        ".rtf",
        ".html",
        ".json",
        ".csv",
        ".log",
        ".yaml",
        ".yml",
    },
}


def _chat_question_id(payload: Dict[str, Any]) -> Optional[str]:
    """Return an optional exact alignment-question identifier."""
    if "question_id" not in payload:
        return None
    value = payload["question_id"]
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("question_id must be a non-empty exact string")
    return value


def _chat_cycle_id(payload: Dict[str, Any]) -> str:
    """Return one canonical correlation id for the operator's whole turn.

    The live HUD mints the id before sending so it can bind the thinking block
    directly to the eventual response without a separate acknowledgement
    frame. Non-browser socket clients may omit it and receive a server-minted
    id on the final reply.
    """
    if "cycle_id" not in payload or payload.get("cycle_id") is None:
        return str(uuid4())
    value = payload.get("cycle_id")
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("cycle_id must be a canonical UUID")
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError, TypeError):
        raise ValueError("cycle_id must be a canonical UUID") from None
    canonical = str(parsed)
    if parsed.version != 4 or canonical != value.lower():
        raise ValueError("cycle_id must be a canonical UUID")
    return canonical


# Hard upload-size ceiling. Larger uploads are rejected so a runaway
# drop can't fill the disk in a demo. 25 MB is plenty for docs + pics.
_UPLOAD_MAX_BYTES = 25 * 1024 * 1024
_STT_MAX_BYTES = 12 * 1024 * 1024
_CHAT_WS_MAX_BYTES = 1_048_576
_MCP_REVISION_RE = re.compile(r"[0-9a-f]{64}\Z")
_MCP_SERVER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*\Z")
_PLUGIN_REQUEST_ID_RE = re.compile(r"[0-9a-f]{32,128}\Z")


def _temporary_upload_path(suffix: str) -> Path:
    """Allocate short-lived upload storage inside the portable product tree."""
    root = STAGING_ROOT / "hud-uploads"
    root.mkdir(parents=True, exist_ok=True)
    clean_suffix = str(suffix or ".bin")
    if not clean_suffix.startswith("."):
        clean_suffix = f".{clean_suffix}"
    return root / f"upload-{secrets.token_hex(16)}{clean_suffix}"


#: A signed OAuth profile id, as it appears in the redirect path. Spelled once
#: because the callback both ADMITS on it and logs on it, and two copies of a
#: pattern are how the two ends drift apart.
def _raising_site(error: BaseException) -> str:
    """Where in Omega's own source this was raised, as ``file.py:line in func``.

    The innermost frame of the traceback is the `raise` statement itself, which
    is what a reason shared by nineteen call sites cannot say. Only Omega's
    source names appear -- never the exception message, which is the one part a
    provider could have written.
    """
    frame = None
    traceback = error.__traceback__
    while traceback is not None:
        frame = traceback
        traceback = traceback.tb_next
    if frame is None:
        return "no traceback"
    code = frame.tb_frame.f_code
    return f"{os.path.basename(code.co_filename)}:{frame.tb_lineno} in {code.co_name}"


_OAUTH_PROFILE_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?\Z")
# The closed setup-action set hud/ui-src/core/plugin-setup.js drives (task-5
# controller ruling 1): every PluginAuthenticationKind
# (plugin_platform/contracts.py) maps to exactly one of these -- a generic
# client never brands or guesses the flow, it only executes whichever action
# the server names for the plugin's `next_required_app_id`.
_PLUGIN_SETUP_ACTION_BY_AUTHENTICATION_KIND = {
    "none": "none",
    "oauth_authorization_code_pkce": "browser",
    "oauth_client_metadata_document": "browser",
    "oauth_device_code": "device",
    "api_key": "secret_form",
    "bearer_token": "secret_form",
    "basic": "secret_form",
    "declared_secret_fields": "secret_form",
}
_PLUGIN_DECISION_KEYS = frozenset({"expected_revision"})
_PLUGIN_PERMISSION_ERRORS = {
    "plugin_permission_expired": 409,
    "plugin_permission_replayed": 409,
    "plugin_permission_invalidated": 409,
    "plugin_permission_cancelled": 409,
    "plugin_permission_revision_conflict": 409,
    "plugin_permission_already_decided": 409,
    "plugin_permission_decision_not_queued": 409,
    "plugin_permission_request_not_found": 404,
    "plugin_permission_request_invalid": 422,
    "plugin_permission_decision_invalid": 422,
    "plugin_permission_lineage_invalid": 409,
    "plugin_permission_lineage_conflict": 409,
    "plugin_authority_revision_conflict": 409,
    "plugin_scopes_invalid": 422,
    "plugin_capability_scope_invalid": 422,
    "plugin_execution_policy_invalid": 422,
    "plugin_connection_disconnected": 409,
}
_MCP_HUD_DEFAULT_HOST = "127.0.0.1"
_MCP_HUD_DEFAULT_PORT = 13310
_MCP_HUD_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_PLUGIN_CATALOG_VERSION_RE = re.compile(
    r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?\Z"
)


class _McpMutationRejected(ValueError):
    """Content-free rejection with the transport status chosen at the boundary."""

    def __init__(self, status_code: int) -> None:
        super().__init__("invalid_mcp_control_request")
        self.status_code = status_code


def _catalog_plugin_dict(plugin: object) -> dict[str, object]:
    if type(plugin) is dict:
        return dict(plugin)
    if is_dataclass(plugin) and not isinstance(plugin, type):
        return asdict(plugin)
    projector = getattr(plugin, "to_dict", None)
    if callable(projector):
        payload = projector()
        if type(payload) is dict:
            return payload
    raise ValueError("plugin_catalog_signature_invalid")


def _http_origin(
    scheme: str, hostname: str | None, port: int | None
) -> tuple[str, str, int] | None:
    scheme = scheme.lower()
    if scheme not in {"http", "https"} or not hostname:
        return None
    return (scheme, hostname.lower(), port or (443 if scheme == "https" else 80))


def _mcp_browser_source_allowed(request: Request, source: str, *, referer: bool) -> bool:
    """Bind a browser mutation source to the configured local HUD authority."""
    try:
        parsed = urlsplit(source)
        if parsed.username is not None or parsed.password is not None:
            return False
        if not referer and (parsed.path or parsed.query or parsed.fragment):
            return False
        if referer and parsed.fragment:
            return False
        configured_host = (
            (os.environ.get("OMEGA_HUD_HOST") or _MCP_HUD_DEFAULT_HOST).strip().lower()
        )
        if configured_host.startswith("[") and configured_host.endswith("]"):
            configured_host = configured_host[1:-1]
        configured_port = int(os.environ.get("OMEGA_HUD_PORT") or _MCP_HUD_DEFAULT_PORT)
        source_origin = _http_origin(parsed.scheme, parsed.hostname, parsed.port)
        request_scheme = request.url.scheme.lower()
    except (TypeError, ValueError):
        return False
    return bool(
        configured_host in _MCP_HUD_LOOPBACK_HOSTS
        and 1 <= configured_port <= 65535
        and source_origin == (request_scheme, configured_host, configured_port)
    )


def _websocket_source_allowed(websocket: WebSocket) -> bool:
    """Reject browser cross-site WebSocket handshakes to the local HUD.

    Non-browser clients commonly omit ``Origin`` and remain supported. Browsers
    always send it for a WebSocket opened by a page, so an explicit foreign
    origin is a CSWSH attempt and must not reach ``accept()``.
    """
    headers = getattr(websocket, "headers", {})
    origin = headers.get("origin")
    if origin is None:
        return True
    try:
        parsed = urlsplit(origin)
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.path or parsed.query or parsed.fragment:
            return False
        configured_host = (
            (os.environ.get("OMEGA_HUD_HOST") or _MCP_HUD_DEFAULT_HOST).strip().lower()
        )
        if configured_host.startswith("[") and configured_host.endswith("]"):
            configured_host = configured_host[1:-1]
        configured_port = int(os.environ.get("OMEGA_HUD_PORT") or _MCP_HUD_DEFAULT_PORT)
        websocket_url = getattr(websocket, "url", None)
        websocket_scheme = str(getattr(websocket_url, "scheme", "ws") or "ws")
        expected_scheme = "https" if websocket_scheme.lower() == "wss" else "http"
        source_origin = _http_origin(parsed.scheme, parsed.hostname, parsed.port)
    except (TypeError, ValueError):
        return False
    return bool(
        configured_host in _MCP_HUD_LOOPBACK_HOSTS
        and 1 <= configured_port <= 65535
        and source_origin == (expected_scheme, configured_host, configured_port)
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate_json_key")
        value[key] = item
    return value


async def _strict_mutation_json(request: Request) -> dict[str, Any]:
    """Accept same-origin JSON with exactly the two user-owned MCP controls."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise _McpMutationRejected(415)

    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin is not None:
        if not _mcp_browser_source_allowed(request, origin, referer=False):
            raise _McpMutationRejected(403)
    elif referer is not None and not _mcp_browser_source_allowed(
        request,
        referer,
        referer=True,
    ):
        raise _McpMutationRejected(403)

    try:
        raw = _json.loads(
            (await request.body()).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise _McpMutationRejected(422) from exc
    if type(raw) is not dict or set(raw) != {"enabled", "revision"}:
        raise _McpMutationRejected(422)
    if type(raw["enabled"]) is not bool:
        raise _McpMutationRejected(422)
    revision = raw["revision"]
    if type(revision) is not str or not _MCP_REVISION_RE.fullmatch(revision):
        raise _McpMutationRejected(422)
    return raw


def _mcp_json_error(error: Exception) -> JSONResponse:
    """Map stable platform failures without reflecting exception content."""
    if isinstance(error, ExternalMcpSwitchConflict):
        return JSONResponse(
            {"ok": False, "error": "external_mcp_switch_conflict"},
            status_code=409,
        )
    if isinstance(error, McpRegistryConflict):
        return JSONResponse(
            {"ok": False, "error": "mcp_registry_conflict"},
            status_code=409,
        )
    if isinstance(error, McpPackageNotInstalled):
        return JSONResponse(
            {"ok": False, "error": "mcp_package_not_installed"},
            status_code=404,
        )
    if isinstance(error, ExternalMcpSwitchUnreadable):
        stable_error = "external_mcp_switch_unreadable"
    elif isinstance(error, McpRegistryUnreadable):
        stable_error = "mcp_registry_unreadable"
    elif isinstance(error, McpHostControlError):
        stable_error = "mcp_control_reconcile_failed"
    else:
        stable_error = "mcp_control_unavailable"
    return JSONResponse({"ok": False, "error": stable_error}, status_code=503)


def _plugin_revision_conflict() -> JSONResponse:
    """A client-held lifecycle/catalog revision no longer matches the server's
    current one. Every permission-bearing mutation (review, install, update)
    rejects with this stable, content-free cause rather than proceeding
    against possibly-stale assumptions; the panel re-fetches and re-renders
    rather than retrying blindly (see task-4 controller ruling 2).
    """
    return JSONResponse({"ok": False, "error": "plugin_revision_conflict"}, status_code=409)


def _plugin_lifecycle_error(error: Exception) -> JSONResponse:
    """Expose only provider-neutral, stable lifecycle boundary failures."""

    reason = str(error).split(":", 1)[0].strip()
    if reason in {
        "plugin_authority_revision_conflict",
        "plugin_grant_revision_conflict",
        "plugin_registry_conflict",
        "mcp_app_registry_conflict",
        "plugin_lifecycle_revision_conflict",
    }:
        reason = "plugin_permission_changed"
    status_by_reason = {
        "plugin_catalog_entry_unavailable": 404,
        "plugin_not_installed": 404,
        "plugin_app_not_installed": 404,
        "plugin_catalog_unavailable": 503,
        "plugin_catalog_signature_invalid": 503,
        "plugin_catalog_identity_ambiguous": 503,
        "plugin_package_unavailable": 503,
        "plugin_package_integrity_invalid": 409,
        "mcp_app_registry_unreadable": 409,
        "plugin_registry_unreadable": 409,
        "plugin_registry_reconciliation_failed": 409,
        "plugin_legacy_recovery_required": 409,
        "plugin_source_untrusted": 409,
        "plugin_runtime_unsupported": 409,
        "plugin_install_review_changed": 409,
        "plugin_update_not_available": 409,
        "plugin_update_required": 409,
        "plugin_install_confirmation_mismatch": 409,
        "plugin_setup_required": 409,
        "plugin_setup_not_required": 409,
        "plugin_app_selection_required": 409,
        "plugin_auth_profile_unavailable": 409,
        "plugin_auth_denied": 409,
        "plugin_auth_expired": 409,
        "plugin_auth_provider_unreachable": 503,
        "plugin_secret_required": 409,
        "plugin_capability_invalid": 409,
        "plugin_permission_changed": 409,
        # A connection form choice the operator can correct and resubmit.
        "plugin_execution_policy_invalid": 422,
        "plugin_scopes_invalid": 422,
        "plugin_capability_scope_invalid": 422,
        "plugin_offline": 503,
        "plugin_revocation_pending": 409,
        "plugin_uninstall_incomplete": 409,
        "plugin_lifecycle_operation_in_progress": 409,
        "plugin_account_unconfigured": 409,
        "oauth_profile_catalog_unavailable": 503,
        "oauth_profile_unavailable": 409,
        "oauth_vault_locked": 409,
        "oauth_issuer_mismatch": 409,
        "oauth_device_authorization_pending": 202,
        "oauth_device_slow_down": 202,
        "oauth_device_poll_too_soon": 202,
        "oauth_device_authorization_expired": 409,
        "oauth_authorization_denied": 409,
        "oauth_token_partition_conflict": 409,
        "oauth_revocation_pending": 409,
        "oauth_revocation_unsupported": 409,
        "plugin_authorization_required": 409,
        "plugin_catalog_authority_missing": 409,
        "plugin_connection_state_invalid": 409,
        "plugin_transport_unavailable": 503,
        "oauth_provider_unreachable": 503,
        "oauth_provider_timeout": 503,
        # The provider answered and refused. Omega is fine, so saying its
        # plugin service is unavailable and asking for a HUD restart sends the
        # operator to fix the one thing that is not broken.
        "plugin_operation_failed": 502,
        "oauth_provider_rejected": 502,
        # Both were falling through to `plugin_lifecycle_unavailable`, which is
        # the exact misdirection the comment above describes, and both reach the
        # operator in practice: `oauth_client_invalid` is monday.com refusing an
        # exchange its own registration gave Omega no secret to authenticate,
        # and `oauth_scope_mismatch` is a provider granting less than it was
        # asked for. Neither is a reason to restart anything.
        "oauth_client_invalid": 502,
        "oauth_scope_mismatch": 502,
    }
    status = status_by_reason.get(reason)
    if status is None:
        # An unmapped cause reaches the operator as "the plugin service is
        # unavailable", which names nothing and points at the one thing that is
        # usually fine. This mapper serves about twenty routes and logged
        # nothing, so a real failure -- Gmail and Linear both refusing an update
        # on 2026-09-08 -- left no trace at all. The exception TYPE and the
        # Omega source line that raised it are named; the message is not, since
        # an unmapped one is not known to be Omega's own vocabulary.
        log.warning(
            "plugin lifecycle: unmapped cause (%s raised at %s)",
            type(error).__name__,
            _raising_site(error),
        )
        return JSONResponse(
            {"ok": False, "error": "plugin_lifecycle_unavailable"},
            status_code=503,
        )
    return JSONResponse({"ok": False, "error": reason}, status_code=status)


def _plugin_directory_error(error: Exception) -> JSONResponse:
    """Expose only the generic directory's closed, content-free failures."""
    reason = str(error).split(":", 1)[0].strip()
    if reason in {
        "plugin_directory_entry_unavailable",
        "plugin_not_installed",
    }:
        return JSONResponse({"ok": False, "error": reason}, status_code=404)
    if reason in {
        "plugin_connection_required",
        "plugin_interaction_unavailable",
    }:
        return JSONResponse({"ok": False, "error": reason}, status_code=409)
    if reason.startswith(("plugin_", "oauth_")):
        return _plugin_lifecycle_error(error)
    return JSONResponse(
        {"ok": False, "error": "plugin_directory_unavailable"},
        status_code=503,
    )


def _chat_plugin_selector(
    directory: object | None,
    payload: dict[str, Any],
) -> dict[str, str] | None:
    """Resolve an unprivileged browser selection against current server authority."""
    text = str(payload.get("text") or payload.get("message") or "").strip()
    slash_match = re.match(r"^/plugin:([a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?)(?:\s|$)", text, re.IGNORECASE)
    has_plugin_id = "plugin_id" in payload or slash_match is not None
    if not has_plugin_id:
        return None
    if payload.get("plugin_operation") is not None:
        raise PluginDirectoryError("plugin_chat_selector_invalid")
    if directory is None:
        raise PluginDirectoryError("plugin_directory_unavailable")
    plugin_id = payload.get("plugin_id") or (slash_match.group(1) if slash_match else None)
    if type(plugin_id) is not str:
        raise PluginDirectoryError("plugin_chat_selector_invalid")
    selector = directory.chat_selector(plugin_id)
    if type(selector) is not dict or set(selector) != {"plugin_id"}:
        raise PluginDirectoryError("plugin_chat_selector_invalid")
    plugin_id = selector.get("plugin_id")
    if type(plugin_id) is not str or not plugin_id:
        raise PluginDirectoryError("plugin_chat_selector_invalid")
    return {"plugin_id": plugin_id}


async def _strict_plugin_install_json(request: Request) -> dict[str, str]:
    """Accept one exact same-origin review-digest confirmation plus the
    client's held lifecycle revision (checked by the route caller against
    ``app.state.plugin_lifecycle.revision`` -- see task-4 controller ruling 2;
    a stale revision returns ``plugin_revision_conflict`` rather than
    proceeding)."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise _McpMutationRejected(415)
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin is not None:
        if not _mcp_browser_source_allowed(request, origin, referer=False):
            raise _McpMutationRejected(403)
    elif referer is not None and not _mcp_browser_source_allowed(
        request,
        referer,
        referer=True,
    ):
        raise _McpMutationRejected(403)
    try:
        raw = _json.loads(
            (await request.body()).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise _McpMutationRejected(422) from exc
    if type(raw) is not dict or set(raw) != {"confirmation", "revision"}:
        raise _McpMutationRejected(422)
    confirmation = raw["confirmation"]
    if type(confirmation) is not str or not _MCP_REVISION_RE.fullmatch(confirmation):
        raise _McpMutationRejected(422)
    revision = raw["revision"]
    if type(revision) is not str or not _MCP_REVISION_RE.fullmatch(revision):
        raise _McpMutationRejected(422)
    return {"confirmation": confirmation, "revision": revision}


async def _strict_plugin_update_json(request: Request) -> dict[str, str]:
    """Accept one exact same-origin candidate version, its review-digest
    confirmation, and the client's held lifecycle revision (see
    ``_strict_plugin_install_json``)."""
    raw = await _strict_closed_json(
        request,
        allowed=frozenset({"confirmation", "version", "revision"}),
        required=frozenset({"confirmation", "version", "revision"}),
    )
    confirmation = raw["confirmation"]
    version = raw["version"]
    revision = raw["revision"]
    if type(confirmation) is not str or not _MCP_REVISION_RE.fullmatch(confirmation):
        raise _McpMutationRejected(422)
    if type(version) is not str or _PLUGIN_CATALOG_VERSION_RE.fullmatch(version) is None:
        raise _McpMutationRejected(422)
    if type(revision) is not str or not _MCP_REVISION_RE.fullmatch(revision):
        raise _McpMutationRejected(422)
    return {"confirmation": confirmation, "version": version, "revision": revision}


async def _strict_device_poll_json(request: Request) -> str:
    raw = await _strict_closed_json(
        request,
        allowed=frozenset({"request_id"}),
        required=frozenset({"request_id"}),
    )
    request_id = raw["request_id"]
    if type(request_id) is not str or re.fullmatch(r"[0-9a-f]{64}", request_id) is None:
        raise _McpMutationRejected(422)
    return request_id


async def _strict_plugin_operation_json(request: Request) -> dict[str, object]:
    """Parse one bounded, explicit plugin operation at the HUD boundary."""
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise _McpMutationRejected(415)
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin is not None:
        if not _mcp_browser_source_allowed(request, origin, referer=False):
            raise _McpMutationRejected(403)
    elif referer is not None and not _mcp_browser_source_allowed(
        request,
        referer,
        referer=True,
    ):
        raise _McpMutationRejected(403)
    body = await request.body()
    if len(body) > 1_048_576:
        raise _McpMutationRejected(413)
    try:
        raw = _json.loads(
            body.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise _McpMutationRejected(422) from exc
    if type(raw) is not dict:
        raise _McpMutationRejected(422)
    allowed = {
        "plugin_id",
        "app_id",
        "capability",
        "arguments",
        "confirmation",
        "authority_revision",
    }
    if set(raw) - allowed or not {"plugin_id", "app_id", "capability", "arguments"} <= set(raw):
        raise _McpMutationRejected(422)
    if type(raw["arguments"]) is not dict:
        raise _McpMutationRejected(422)
    for field in ("plugin_id", "app_id", "capability"):
        value = raw[field]
        if type(value) is not str or not value or value.strip() != value:
            raise _McpMutationRejected(422)
    confirmation = raw.get("confirmation")
    if confirmation is not None and (
        type(confirmation) is not str or not _MCP_REVISION_RE.fullmatch(confirmation)
    ):
        raise _McpMutationRejected(422)
    authority_revision = raw.get("authority_revision")
    if authority_revision is not None and (
        type(authority_revision) is not str or not _MCP_REVISION_RE.fullmatch(authority_revision)
    ):
        raise _McpMutationRejected(422)
    return raw


def _plugin_operation_digest(body: dict[str, object]) -> str:
    return sha256_hex(
        canonical_bytes(
            {
                "plugin_id": body["plugin_id"],
                "app_id": body["app_id"],
                "capability": body["capability"],
                "arguments": body["arguments"],
            }
        )
    )


def _reject_cross_origin_mutation(request: Request) -> None:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin is not None:
        if not _mcp_browser_source_allowed(request, origin, referer=False):
            raise _McpMutationRejected(403)
    elif referer is not None and not _mcp_browser_source_allowed(
        request,
        referer,
        referer=True,
    ):
        raise _McpMutationRejected(403)


async def _strict_closed_json(
    request: Request,
    *,
    allowed: frozenset[str],
    required: frozenset[str],
) -> dict[str, object]:
    media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise _McpMutationRejected(415)
    _reject_cross_origin_mutation(request)
    try:
        raw = _json.loads(
            (await request.body()).decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (UnicodeDecodeError, ValueError) as exc:
        raise _McpMutationRejected(422) from exc
    if (
        type(raw) is not dict
        or set(raw) - set(allowed)
        or not required <= set(raw)
    ):
        raise _McpMutationRejected(422)
    return raw


def _plugin_permission_http_error(error: Exception) -> JSONResponse:
    reason = str(error).split(":", 1)[0].strip()
    status = _PLUGIN_PERMISSION_ERRORS.get(reason)
    if status is None:
        return JSONResponse(
            {"ok": False, "error": "plugin_permission_unavailable"}, status_code=503
        )
    return JSONResponse({"ok": False, "error": reason}, status_code=status)


def _permission_hud_dict(item: object) -> dict[str, object]:
    projector = getattr(item, "to_hud_dict", None)
    if not callable(projector):
        raise ValueError("plugin_permission_request_invalid")
    payload = projector()
    if (
        type(payload) is not dict
        or set(payload) != {"request_id", "revision", "display"}
        or type(payload["request_id"]) is not str
        or type(payload["revision"]) is not str
        or type(payload["display"]) is not dict
    ):
        raise ValueError("plugin_permission_request_invalid")
    return payload


def _queue_permission_decision(
    request_id: str, expected_revision: str, decision: str
) -> dict[str, object]:
    if not _PLUGIN_REQUEST_ID_RE.fullmatch(request_id):
        raise ValueError("plugin_permission_request_invalid")
    if type(expected_revision) is not str or not _MCP_REVISION_RE.fullmatch(expected_revision):
        raise _McpMutationRejected(422)
    store = get_default_plugin_permission_store()
    lineage = store.resolve_continuation_lineage(request_id, expected_revision)
    arrival_seq = next_arrival_seq()
    queued = enqueue_plugin_permission_decision(
        PluginPermissionDecision(
            request_id=request_id,
            expected_revision=expected_revision,
            decision=decision,  # type: ignore[arg-type]
        ),
        arrival_seq=arrival_seq,
    )
    if not queued:
        raise ValueError("plugin_permission_decision_not_queued")
    return {
        "ok": True,
        "status": "queued",
        "request_id": request_id,
        "task_id": lineage.task_id,
    }


async def _read_upload_bytes_capped(upload: UploadFile, *, max_bytes: int) -> bytes:
    written = 0
    chunks: list[bytes] = []
    while True:
        chunk = await upload.read(64 * 1024)
        if not chunk:
            break
        written += len(chunk)
        if written > max_bytes:
            raise HTTPException(
                status_code=413,
                detail=f"upload exceeds {max_bytes} bytes",
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _classify_upload(filename: str) -> str:
    suffix = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    for kind, exts in _UPLOAD_KINDS.items():
        if suffix in exts:
            return kind
    return "other"


def _verify_image_bytes(data: bytes) -> None:
    """Decode image header bytes — do not trust filename or Content-Type."""
    try:
        import cv2
        import numpy as np

        arr = np.frombuffer(data, dtype=np.uint8)
        if cv2.imdecode(arr, cv2.IMREAD_UNCHANGED) is None:
            raise ValueError("payload is not a decodable image")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid image payload") from exc


# Image generation output root — mirrored from tools/image_gen.py so
# we can serve the files via HTTP without importing the tool at module
# load time. Files land at guardian/state/images/<YYYY-MM-DD>/<name>.png
_IMAGE_ROOT = GUARDIAN_STATE_ROOT / "images"
_DOCUMENT_ROOT = GUARDIAN_STATE_ROOT / "documents"


def _face_runtime_unavailable_response(
    *,
    phase: str,
    reason: str = "",
) -> JSONResponse:
    """Operator-actionable response when the brain-local face bundle is down."""
    try:
        from runtime.face.recognition import face_status

        status = face_status()
    except Exception:
        status = {}
    base = "brain-local-cpu"
    detail = (reason or "runtime face pipeline is not reachable").strip()
    error = (
        f"{detail}. Restore the brain-side CPU face models under "
        f"{status.get('asset_root') or 'models/onnxs/face'} and retry."
    )
    try:
        from guardian.terminal_feedback import emit_phase

        emit_phase(
            "hud",
            phase,
            status="degraded",
            summary=f"{phase} degraded — face runtime unavailable",
            component="hud.server",
            failure_class="face_runtime_unavailable",
            runtime=base,
            reason=detail[:120],
        )
    except Exception as _e:
        log.debug("%s face-runtime emit_phase failed: %s", phase, _e)
    return JSONResponse(
        {
            "ok": False,
            "error": error,
            "reason": "face_runtime_unavailable",
            "runtime": base,
        }
    )


def _require_face_runtime(*, phase: str) -> JSONResponse | None:
    """Return a JSON error when the brain-local face bundle is unavailable."""
    from runtime.face.recognition import face_status

    status = face_status()
    if status.get("face_ready"):
        return None
    return _face_runtime_unavailable_response(
        phase=phase,
        reason=str(status.get("reason") or "brain-local CPU face models unavailable"),
    )


def _attach_image_urls(result: Dict[str, Any]) -> Dict[str, Any]:
    r"""Mutate the image_gen result dict in place to add `url` (and
    `urls` for multi-image runs) — relative HTTP paths the browser
    can resolve. Raw filesystem paths in `path` / `all_paths` are
    preserved for any consumer that needs them.

    The mapping: guardian/state/images/<date>/<name>.png
                 -> /api/images/<date>/<name>.png
    """

    def _to_url(fs_path: str) -> str:
        try:
            p = Path(fs_path).resolve()
            rel = p.relative_to(_IMAGE_ROOT.resolve())
            # rel is "<date>/<name>.png" with OS separators; force forward slashes
            return "/api/images/" + rel.as_posix()
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "image_url_resolution_failed",
                ok=False,
                error=str(exc),
            )
            return ""

    if isinstance(result, dict):
        primary = result.get("path")
        if primary:
            url = _to_url(primary)
            if url:
                result["url"] = url
        if isinstance(result.get("all_paths"), list):
            urls = [_to_url(p) for p in result["all_paths"]]
            result["urls"] = [u for u in urls if u]
    return result


def _attach_document_url(result: Dict[str, Any]) -> Dict[str, Any]:
    """Mutate document artifact dict in place to add `url` for browser download."""

    def _to_url(fs_path: str) -> str:
        try:
            p = Path(fs_path).resolve()
            rel = p.relative_to(_DOCUMENT_ROOT.resolve())
            return "/api/documents/" + rel.as_posix()
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "document_url_resolution_failed",
                ok=False,
                error=str(exc),
            )
            return ""

    if isinstance(result, dict):
        primary = result.get("path")
        if primary:
            url = _to_url(primary)
            if url:
                result["url"] = url
    return result


def _browser_artifacts(record: Dict[str, Any]) -> tuple[dict | None, dict | None]:
    """Copy stored artifacts and add browser-safe URLs without mutating the ledger."""
    image = dict(record.get("image") or {}) or None
    document = dict(record.get("document") or {}) or None
    if image is not None and image.get("path"):
        _attach_image_urls(image)
    if document is not None and document.get("path"):
        _attach_document_url(document)
    return image, document


def _safe_upload_name(filename: str) -> str:
    """Strip path traversal + non-portable chars; keep extension."""
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base)[:128] or "upload"
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    return f"{ts}_{uuid4().hex}_{base}"


log = logging.getLogger("omega.hud.server")
_presence_runtime_absent_logged = False


# Operator HTML surface — single-file bundle at hud/omega_interface.html.
_HUD_HTML_PATH = OMEGA_ROOT / "hud" / "omega_interface.html"
_PRIVACY_POLICY_PATH = OMEGA_ROOT / "docs" / "legal" / "privacy-policy.md"


def _markdown_file_to_simple_html(path: Path, *, page_title: str) -> str:
    """Render a small legal markdown file to a single HTML page.

    Fail closed if the source is missing or empty — Google consent URLs must not
    resolve to a blank success page.
    """
    import html as html_lib

    if not path.is_file():
        raise FileNotFoundError(f"legal source missing: {path}")
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        raise ValueError(f"legal source empty: {path}")

    blocks: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if not paragraph:
            return
        text = " ".join(paragraph)
        blocks.append(f"<p>{html_lib.escape(text)}</p>")
        paragraph.clear()

    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            continue
        if stripped.startswith("# "):
            flush_paragraph()
            blocks.append(f"<h1>{html_lib.escape(stripped[2:].strip())}</h1>")
            continue
        if stripped.startswith("## "):
            flush_paragraph()
            blocks.append(f"<h2>{html_lib.escape(stripped[3:].strip())}</h2>")
            continue
        if stripped.startswith("### "):
            flush_paragraph()
            blocks.append(f"<h3>{html_lib.escape(stripped[4:].strip())}</h3>")
            continue
        if stripped.startswith("|") and "|" in stripped[1:]:
            flush_paragraph()
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            if all(set(c) <= set("-: ") for c in cells):
                continue
            row = "".join(f"<td>{html_lib.escape(c)}</td>" for c in cells)
            blocks.append(f"<tr>{row}</tr>")
            continue
        if stripped.startswith("- "):
            flush_paragraph()
            blocks.append(f"<li>{html_lib.escape(stripped[2:].strip())}</li>")
            continue
        paragraph.append(stripped)
    flush_paragraph()

    body_parts: list[str] = []
    list_open = False
    table_open = False
    for block in blocks:
        if block.startswith("<li>"):
            if table_open:
                body_parts.append("</table>")
                table_open = False
            if not list_open:
                body_parts.append("<ul>")
                list_open = True
            body_parts.append(block)
            continue
        if block.startswith("<tr>"):
            if list_open:
                body_parts.append("</ul>")
                list_open = False
            if not table_open:
                body_parts.append("<table>")
                table_open = True
            body_parts.append(block)
            continue
        if list_open:
            body_parts.append("</ul>")
            list_open = False
        if table_open:
            body_parts.append("</table>")
            table_open = False
        body_parts.append(block)
    if list_open:
        body_parts.append("</ul>")
    if table_open:
        body_parts.append("</table>")

    title = html_lib.escape(page_title)
    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
        f'<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f"<title>{title}</title>"
        "<style>"
        "body{margin:0;font:16px/1.55 Georgia,'Times New Roman',serif;"
        "background:linear-gradient(160deg,#0b1220,#142033 45%,#1a2a22);"
        "color:#f4f0e6;}"
        "main{max-width:42rem;margin:0 auto;padding:2.5rem 1.25rem 4rem;}"
        "h1,h2,h3{font-weight:650;letter-spacing:0.01em;}"
        "h1{font-size:1.85rem;margin:0 0 1rem;}"
        "h2{font-size:1.25rem;margin:2rem 0 0.75rem;}"
        "h3{font-size:1.05rem;margin:1.5rem 0 0.5rem;}"
        "p,li{color:#e6e0d2;}"
        "a{color:#9ec5b0;}"
        "table{width:100%;border-collapse:collapse;margin:1rem 0;font-size:0.92rem;}"
        "td{border:1px solid #2f4a3c;padding:0.45rem 0.55rem;vertical-align:top;}"
        "ul{padding-left:1.2rem;}"
        "</style></head><body><main>" + "".join(body_parts) + "</main></body></html>"
    )


def _hud_html_present() -> bool:
    return _HUD_HTML_PATH.is_file()


# Map panel topic strings -> telemetry functions. The dict is the
# canonical list of detail panels the HUD knows about. New panels add
# one entry.
_PANEL_HANDLERS = {
    "brain": telemetry.panel_brain,
    "memory": telemetry.panel_memory,
    "omega": telemetry.panel_omega,
    "meta": telemetry.panel_meta,
    "global": telemetry.panel_global,
    "autoloop": telemetry.panel_autoloop,
    "identity": telemetry.panel_identity,
    "deepsleep": telemetry.panel_deepsleep,
    "settings": telemetry.panel_settings,
}

# P3-S03 — Per-lobe display metadata for the HUD memory node graph.
#
# Hand-mirrored from Kimi's `generateMockTree` mock in
# omega_interface.html so mock-mode and live-mode render the same
# colors/types/labels. The `type` field is a PRESENTATION tag (style
# hint for the graph node), NOT a canonical doctrine classification —
# see memory/dewey_map.py for the canonical ENTITY_BEARING_LOBES /
# SCRIPTED_LOBES sets.
#
# DRIFT GUARD: scripts/symphony_boot_cascade.py
# `_run_scenario_memory_graph_api_contract` regex-parses the HUD's
# mock lobe entries and asserts they match this dict element-by-
# element. A future edit to either side that diverges will fail the
# scenario. Module-level (not closure-scoped) so the scenario can
# import it.
_MEMORY_LOBE_DISPLAY: Dict[int, Dict[str, Any]] = {
    0: {"label": "Identity", "coord": "000", "type": "scripted"},
    1: {"label": "Personas", "coord": "100", "type": "entity"},
    2: {"label": "Knowledge", "coord": "200", "type": "setfit"},
    3: {"label": "Reasoning", "coord": "300", "type": "scripted"},
    4: {"label": "Situational", "coord": "400", "type": "entity"},
    5: {"label": "Tools", "coord": "500", "type": "scripted"},
    6: {"label": "Journal", "coord": "600", "type": "scripted"},
    7: {"label": "Vision", "coord": "700", "type": "scripted"},
    8: {"label": "Coding", "coord": "800", "type": "setfit"},
    9: {"label": "Temporal", "coord": "900", "type": "scripted"},
    10: {"label": "Workflows", "coord": "1000", "type": "setfit"},
    11: {"label": "Dev", "coord": "1100", "type": "scripted"},
}


def _resolve_upload_ref(upload_ref: str) -> Optional[Path]:
    """Resolve an operator-supplied upload ref STRICTLY under _UPLOAD_ROOT.

    Accepts the manifest url form (/api/uploads/<kind>/<name>), a
    <kind>/<name> relative ref, or a bare filename (tried under image/ and
    images/). Containment-checked — traversal refs resolve to None."""
    from pressure_loop.operator_vision import resolve_upload_ref

    return resolve_upload_ref(upload_ref, upload_root=_UPLOAD_ROOT)


def _chat_upload_refs(payload: Dict[str, Any]) -> list[str]:
    """Collect upload refs from the composer payload in stable order."""
    refs: list[str] = []
    first = payload.get("upload_ref")
    if isinstance(first, str) and first.strip():
        refs.append(first.strip())
    uploads = payload.get("uploads")
    if isinstance(uploads, list):
        for item in uploads:
            if not isinstance(item, dict):
                continue
            ref = item.get("upload_ref") or item.get("url")
            if isinstance(ref, str) and ref.strip() and ref.strip() not in refs:
                refs.append(ref.strip())
    return refs


def _answer_who_is_this_from_upload(upload_ref: str, text: str) -> Optional[Dict[str, Any]]:
    """Deterministic who-is-this for a chat-attached image (vision spec §3).

    Returns a chat-shaped response dict, an explicit failure, or None when
    the input does not request image identification."""
    from pressure_loop.operator_vision import answer_who_is_this_from_upload

    result = answer_who_is_this_from_upload(
        upload_ref,
        text,
        upload_root=_UPLOAD_ROOT,
    )
    if result is None:
        return None
    if result.get("ok") is False:
        return result
    return {
        "ok": True,
        "message": result["message"],
        "route": {"action": result["kind"]},
        "recognized_name": result.get("recognized_name"),
        "confidence": result.get("confidence"),
    }


def create_app() -> FastAPI:
    """Build the FastAPI app. Factory pattern so tests can build a
    fresh app without import-time side effects."""
    app = FastAPI(
        title="Omega 3.0 HUD",
        description="Bridge between the cognitive substrate and the operator surface",
        version="0.1.0",
        docs_url=None,  # disable /docs (operator surface, not API consumer surface)
        redoc_url=None,
    )
    app.state.background_tasks = set()
    # One process-owned lifecycle service is the source of truth for the
    # HUD directory, install registry, OAuth sessions, and AUTO runtime.
    #
    # Failure-soft on purpose. This runs at module import, and the spawner
    # treats an import failure as required substrate, so letting it raise ended
    # the whole boot for one plugin: an interrupted update whose sign-in had
    # been started reconciles through the connection service, which needs the
    # default MCP host, and refuses when composed before that host is running.
    # Losing the plugin surface is bad; losing chat, memory, telemetry and the
    # only surface that could recover the plugin is worse. Every plugin route
    # already answers `plugin_lifecycle_unavailable` when the service is not
    # there, which is exactly what a degraded surface should say.
    try:
        app.state.plugin_lifecycle = get_default_plugin_lifecycle_service()
    except Exception as exc:  # noqa: BLE001 — the HUD must still come up
        log.error(
            "plugin lifecycle unavailable, HUD starting with the plugin surface"
            " degraded (%s raised at %s)",
            type(exc).__name__,
            _raising_site(exc),
        )
        app.state.plugin_lifecycle = None
    plugin_registry = getattr(app.state.plugin_lifecycle, "plugin_registry", None)
    app.state.plugin_directory = (
        PluginDirectoryService(
            sources=(app.state.plugin_lifecycle.catalog_source(),),
            plugin_registry=plugin_registry,
        )
        if plugin_registry is not None
        else None
    )

    def _schedule_background(awaitable: Any, *, name: str) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable, name=name)
        app.state.background_tasks.add(task)

        def _finish(completed: asyncio.Task[Any]) -> None:
            app.state.background_tasks.discard(completed)
            if completed.cancelled():
                return
            try:
                completed.result()
            except Exception as exc:
                log.warning("background task %s failed: %s", name, exc)

        task.add_done_callback(_finish)
        return task

    @app.middleware("http")
    async def add_cache_control_header(request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/assets/hud"):
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return response

    # Presence is operator-initiated (QUARANTINE: the webcam is operator-owned —
    # see hud/README.md + sensory/operator_presence.py). The HUD does NOT
    # auto-start a webcam presence watcher on boot; PRESENCE_STATE latches via
    # the operator-run presence surface / explicit face enroll. (The old
    # core.hud_presence_watcher startup hook imported a module that no longer
    # exists and silently no-opped, so it was removed.)

    @app.get("/", response_class=HTMLResponse)
    async def root() -> HTMLResponse:
        """Serve hud/omega_interface.html."""
        if not _hud_html_present():
            log.error("HUD HTML missing at %s", _HUD_HTML_PATH)
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "root",
                    status="fail",
                    summary="HUD HTML missing — operator web surface unservable",
                    component="hud.server",
                    failure_class="hud_html_missing",
                )
            except Exception as _e:
                log.debug("root emit_phase failed: %s", _e)
            raise HTTPException(
                status_code=500,
                detail=f"HUD HTML missing at {_HUD_HTML_PATH}",
            )
        try:
            html = _HUD_HTML_PATH.read_text(encoding="utf-8")
        except Exception as exc:
            log.error("HUD HTML read failed: %s", exc)
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "root",
                    status="fail",
                    summary="HUD HTML read failed — operator web surface unservable",
                    component="hud.server",
                    failure_class="hud_html_read_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("root emit_phase failed: %s", _e)
            raise HTTPException(status_code=500, detail=f"read failed: {exc}")
        return HTMLResponse(content=html)

    @app.get("/legal/privacy", response_class=HTMLResponse)
    async def legal_privacy() -> HTMLResponse:
        """Public privacy policy copy for Google OAuth consent URLs."""
        try:
            html = _markdown_file_to_simple_html(
                _PRIVACY_POLICY_PATH,
                page_title="Omega 3.0 Privacy Policy",
            )
        except (FileNotFoundError, ValueError, OSError) as exc:
            log.error("privacy policy unavailable: %s", exc)
            raise HTTPException(
                status_code=500,
                detail="privacy policy source is unavailable",
            ) from exc
        return HTMLResponse(content=html)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:
        """Liveness probe. Cheap — used by hud.standalone to confirm
        the server bound before opening the browser."""
        return JSONResponse(
            {
                "status": "ok",
                "ts": datetime.now(timezone.utc).isoformat(),
                "html_present": _hud_html_present(),
            }
        )

    @app.get("/api/status")
    async def api_status() -> JSONResponse:
        """Top status bar — legacy pill cells + SPA ``substrate`` glyphs."""
        pills = await run_in_threadpool(telemetry.panel_status)
        payload = dict(pills)
        payload["substrate"] = telemetry.panel_status_spa_substrate(pills)
        payload["offline"] = False
        return JSONResponse(payload)

    @app.get("/api/boot/state")
    async def api_boot_state() -> JSONResponse:
        """Boot banner state. HUD-3 will populate."""
        return JSONResponse(telemetry.panel_boot_state())

    @app.get("/api/panel/{topic}")
    async def api_panel(topic: str) -> JSONResponse:
        """Detail panel content. Topic must be one of the 6 known panels."""
        handler = _PANEL_HANDLERS.get(topic)
        if handler is None:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "api_panel",
                    status="fail",
                    summary=f"unknown panel topic '{topic}' requested",
                    component="hud.server",
                    failure_class="unknown_panel_topic",
                )
            except Exception as _e:
                log.debug("api_panel emit_phase failed: %s", _e)
            raise HTTPException(
                status_code=404,
                detail=f"unknown panel '{topic}'; valid: {sorted(_PANEL_HANDLERS.keys())}",
            )
        return JSONResponse(await run_in_threadpool(handler))

    @app.get("/api/telemetry/snapshot")
    async def api_telemetry_snapshot() -> JSONResponse:
        """Unified telemetry snapshot — patch the HUD's Telemetry state
        deep-merges into its existing object. Each subsystem subtree
        is best-effort independent. HUD-4..9 wiring."""
        return JSONResponse(await run_in_threadpool(telemetry.telemetry_snapshot))

    @app.get("/api/telemetry/turn-latency")
    async def api_telemetry_turn_latency(limit: int = 25) -> JSONResponse:
        """How long recent operator turns actually took, from the marks the paths record.

        Diagnostic only — a bounded in-memory ring, nothing persisted, nothing gated. It exists
        so a latency claim can be checked instead of asserted: the demo bar names a number
        (median time-to-first-audio under 2 s while two lanes are leased) and until these marks
        existed nothing in the system measured it. Every figure carries its observation count,
        and a phase nobody marked reads as unknown rather than as zero.
        """
        from pressure_loop import cognition_queue, turn_latency

        bounded = max(1, min(int(limit), turn_latency.RING_SIZE))
        return JSONResponse({
            "summary": turn_latency.summary(),
            "turns": turn_latency.recent(limit=bounded),
            # Why a turn is waiting rather than thinking: an earlier arrival still in ingress
            # holds the line for every pipeline, and that hold used to be invisible.
            "ingress_barrier": cognition_queue.ingress_barrier_state(),
        })

    @app.get("/api/telemetry/pipelines")
    async def api_telemetry_pipelines() -> JSONResponse:
        """Uncached physical execution-pipeline telemetry.

        The unified snapshot above keeps one "current" episode, because it was
        built when there was one cascade. Four concurrent physical pipelines
        make that reader bounce: send a chat while another pipeline is mid-build
        and the panel remains parked on that episode, so the chat's ignition and cascade
        never appear (operator 2026-08-14).

        This poll answers "what is EVERY lane doing" — newest ignition per lane
        from the audit ring, joined with the durable lane envelopes for current
        state. Separate from the snapshot on purpose: it is uncached, because a
        lane can open, run and close inside the snapshot's cache window.
        """
        return JSONResponse(await run_in_threadpool(telemetry.pipeline_telemetry))

    # =================================================================
    # Signed MCP connectivity — content-free status + exact user controls
    # =================================================================
    @app.get("/api/mcp/status")
    async def api_mcp_status() -> JSONResponse:
        """Return the public host's content-free switch/package projection."""
        try:
            status = await run_in_threadpool(mcp_status)
        except Exception as exc:
            return _mcp_json_error(exc)
        return JSONResponse({"ok": True, **status})

    @app.post("/api/mcp/external-switch")
    async def api_mcp_external_switch(request: Request) -> JSONResponse:
        """Set only the hard External MCP switch through its public owner."""
        try:
            body = await _strict_mutation_json(request)
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_mcp_control_request"},
                status_code=exc.status_code,
            )
        try:
            result = await set_external_mcp_enabled(
                body["enabled"],
                body["revision"],
            )
        except Exception as exc:
            return _mcp_json_error(exc)
        return JSONResponse({"ok": True, **result})

    @app.post("/api/mcp/packages/{server_id}/enabled")
    async def api_mcp_package_enabled(
        server_id: str,
        request: Request,
    ) -> JSONResponse:
        """Toggle one installed signed package through revision-safe host control."""
        if not _MCP_SERVER_ID_RE.fullmatch(server_id):
            return JSONResponse(
                {"ok": False, "error": "invalid_mcp_control_request"},
                status_code=422,
            )
        try:
            body = await _strict_mutation_json(request)
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_mcp_control_request"},
                status_code=exc.status_code,
            )
        try:
            result = await set_mcp_package_enabled(
                server_id,
                body["enabled"],
                body["revision"],
            )
        except Exception as exc:
            return _mcp_json_error(exc)
        return JSONResponse({"ok": True, **result})

    # =================================================================
    # Provider-neutral plugin lifecycle API
    # =================================================================
    @app.get("/api/plugins/catalog")
    async def api_plugin_catalog(request: Request) -> JSONResponse:
        """Validate and delegate: every search/merge/pagination decision is
        ``PluginLifecycleService.search_catalog()``'s alone."""

        allowed = {"q", "category", "trust", "state", "cursor", "page_size"}
        query_items = tuple(request.query_params.multi_items())
        query_keys = tuple(key for key, _value in query_items)
        if set(query_keys) - allowed or len(query_keys) != len(set(query_keys)):
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=422,
            )
        raw_page_size = request.query_params.get("page_size", "50")
        if (
            re.fullmatch(r"[1-9][0-9]{0,2}", raw_page_size) is None
            or int(raw_page_size) > MAX_CATALOG_PAGE_SIZE
        ):
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=422,
            )
        page_size = int(raw_page_size)
        q = request.query_params.get("q") or ""
        filters: dict[str, str] = {}
        for key in ("category", "trust", "state"):
            value = request.query_params.get(key)
            if value:
                filters[key] = value
        cursor = request.query_params.get("cursor") or None
        try:
            page = await run_in_threadpool(
                lambda: app.state.plugin_lifecycle.search_catalog(
                    q, filters, cursor, page_size
                )
            )
        except Exception as exc:
            reason = str(exc).split(":", 1)[0].strip()
            if reason == "plugin_catalog_signature_invalid":
                return JSONResponse(
                    {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                    status_code=422,
                )
            if reason == "plugin_catalog_cursor_invalid":
                return JSONResponse({"ok": False, "error": reason}, status_code=422)
            if reason == "plugin_catalog_cursor_stale":
                return JSONResponse({"ok": False, "error": reason}, status_code=409)
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                "status": page.status,
                "plugins": [dict(item) for item in page.items],
                "next_cursor": page.next_cursor,
                "total_plugins": page.total,
            }
        )

    @app.get("/api/plugins/catalog/{plugin_id}")
    async def api_plugin_catalog_entry(plugin_id: str) -> JSONResponse:
        """Return one verified catalog entry's public details.

        Base fields come straight from the verified catalog entry lookup
        (title, summary, publisher, trust tier, version, categories,
        distribution kind/origin, offline capability, support/source URLs,
        release history). Two further sections are merged in only
        best-effort -- a hiccup in either omits just its own keys, never the
        whole response, matching /api/plugins/installed's established
        reverify_installed fallback handling (see task-3-report.md):

        - ``source_kind``/``publisher_display_name`` from the merged plugin
          directory (the same provider-neutral projection the catalog list
          route already renders cards from);
        - ``installed_authority`` -- this plugin's currently active
          capabilities, per-app requested provider scopes/authentication
          kind, and network/filesystem boundaries, from its already
          re-verified sealed manifest, present only when installed. Never a
          claim about an unreviewed candidate version; never a package
          download; never secrets or seal material.
        """
        try:
            snapshot = await run_in_threadpool(
                app.state.plugin_lifecycle.catalog_source().snapshot
            )
            matches = tuple(
                plugin for plugin in snapshot.plugins if plugin.plugin_id == plugin_id
            )
            if len(matches) != 1:
                raise ValueError("plugin_catalog_entry_unavailable")
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        payload = _catalog_plugin_dict(matches[0])
        try:
            directory_snapshot = await run_in_threadpool(
                app.state.plugin_directory.list_directory
            )
            directory_matches = tuple(
                entry
                for entry in directory_snapshot.plugins
                if entry.plugin_id == plugin_id
            )
            if len(directory_matches) == 1:
                directory_entry = directory_matches[0].to_dict()
                payload["source_kind"] = directory_entry["source_kind"]
                payload["publisher_display_name"] = directory_entry[
                    "publisher_display_name"
                ]
        except Exception:
            pass
        try:
            verified = app.state.plugin_lifecycle.plugin_registry.reverify_installed(
                plugin_id
            )
            payload["installed_authority"] = PluginDirectoryService.installed_plugin_authority(
                verified
            )
        except Exception:
            pass
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                "plugin": payload,
            }
        )

    @app.get("/api/plugins/{plugin_id}/update-review")
    async def api_plugin_update_review(plugin_id: str, request: Request) -> JSONResponse:
        """Return the complete identity/capability/permission diff between
        the installed version and one exact candidate catalog version --
        validation-only, delegating entirely to
        ``PluginLifecycleService.update_review()``. Requires the client's
        held revision, exactly like review/install/update (task-4 fix round
        1, finding 2): a diff is never shown against catalog/lifecycle state
        the client no longer holds, and a stale revision returns
        ``plugin_revision_conflict`` rather than a diff computed against
        moving ground.
        """
        allowed = {"version", "revision"}
        query_keys = set(request.query_params)
        if query_keys != allowed:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=422,
            )
        version = request.query_params.get("version")
        revision = request.query_params.get("revision")
        if (
            type(version) is not str
            or _PLUGIN_CATALOG_VERSION_RE.fullmatch(version) is None
            or type(revision) is not str
            or _MCP_REVISION_RE.fullmatch(revision) is None
        ):
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=422,
            )
        if revision != app.state.plugin_lifecycle.revision:
            return _plugin_revision_conflict()
        try:
            review = await run_in_threadpool(
                lambda: app.state.plugin_lifecycle.update_review(plugin_id, version)
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                "review": review.to_dict(),
            }
        )

    @app.get("/api/plugins/installed")
    async def api_plugins_installed() -> JSONResponse:
        try:
            installed_provider_identity: dict[str, dict[str, object]] = {}
            plugin_directory = app.state.plugin_directory
            if plugin_directory is not None:
                try:
                    directory_snapshot = await run_in_threadpool(
                        plugin_directory.list_directory
                    )
                    for entry in directory_snapshot.plugins:
                        document = entry.to_dict()
                        plugin_id = document.get("plugin_id")
                        if type(plugin_id) is not str:
                            continue
                        installed_provider_identity[plugin_id] = {
                            field: document[field]
                            for field in (
                                "publisher_id",
                                "publisher_display_name",
                                "provider_id",
                                "provenance",
                            )
                            if field in document
                        }
                except Exception as exc:  # noqa: BLE001 - identity is display metadata
                    log.warning(
                        "installed listing: provider identity unavailable (%s at %s)",
                        type(exc).__name__,
                        _raising_site(exc),
                    )
            records = app.state.plugin_lifecycle.plugin_registry.snapshot().records
            snapshots = []
            for record in records:
                try:
                    status = await app.state.plugin_lifecycle.status(record.plugin_id)
                except Exception:
                    # One unreadable plugin must not cost the operator the whole
                    # panel -- including the Uninstall button that would clear
                    # it. This is the same rule the reverify call below already
                    # follows, and `status` sat outside it: a retired provider
                    # whose package was still installed took down the listing.
                    log.warning(
                        "installed listing: status failed for one plugin (%s at %s)",
                        type(_sys.exc_info()[1]).__name__,
                        _raising_site(_sys.exc_info()[1]),
                    )
                    snapshots.append(
                        {
                            "plugin_id": record.plugin_id,
                            "state": "blocked",
                            "error_code": "plugin_state_unreadable",
                            "package_version": record.selected_version,
                            "enabled": record.enabled,
                            "apps": [],
                            "next_required_app_id": None,
                        }
                    )
                    continue
                snapshot = status.to_dict()
                # Best-effort: an installed plugin's own sealed manifest names
                # it (title/summary/trust_tier) independent of whether it is
                # also present in any currently synced catalog source or page
                # (see mcp_platform/plugin_directory.py's
                # installed_plugin_summary). A verification hiccup for this
                # one plugin only omits these three keys -- it never fails
                # the whole installed listing, matching list_directory()'s
                # own fallback-entry handling for the same reverify call.
                try:
                    verified = app.state.plugin_lifecycle.plugin_registry.reverify_installed(
                        record.plugin_id
                    )
                    snapshot.update(PluginDirectoryService.installed_plugin_summary(verified))
                except Exception:
                    pass
                snapshot.update(installed_provider_identity.get(record.plugin_id, {}))
                snapshots.append(snapshot)
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                "plugins": snapshots,
            }
        )

    @app.post("/api/plugins/{plugin_id}/review")
    async def api_plugin_review(plugin_id: str, request: Request) -> JSONResponse:
        """Return a confirmation-bound review from the verified catalog."""
        try:
            body = await _strict_closed_json(
                request,
                allowed=frozenset({"revision"}),
                required=frozenset({"revision"}),
            )
            revision = body["revision"]
            if type(revision) is not str or _MCP_REVISION_RE.fullmatch(revision) is None:
                raise _McpMutationRejected(422)
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        if revision != app.state.plugin_lifecycle.revision:
            return _plugin_revision_conflict()
        try:
            review = await run_in_threadpool(
                lambda: app.state.plugin_lifecycle.review(plugin_id)
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        payload = review.to_dict()
        payload["sha256"] = review.sha256
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                "review": payload,
            }
        )

    @app.post("/api/plugins/{plugin_id}/install")
    async def api_plugin_install(plugin_id: str, request: Request) -> JSONResponse:
        """Install one reviewed plugin through its admitted signed source."""
        try:
            body = await _strict_plugin_install_json(request)
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        if body["revision"] != app.state.plugin_lifecycle.revision:
            return _plugin_revision_conflict()
        try:
            result = await run_in_threadpool(
                lambda: app.state.plugin_lifecycle.install(
                    plugin_id,
                    confirmation=body["confirmation"],
                )
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                "plugin_id": result.record.plugin_id,
                "registry_revision": result.revision,
                "dependency_receipt_id": result.dependency_receipt.receipt_id,
            }
        )

    @app.post("/api/plugins/{plugin_id}/try-in-chat")
    async def api_plugin_try_in_chat(plugin_id: str, request: Request) -> JSONResponse:
        """Return a non-executing introduction from current signed plugin metadata."""
        try:
            await _strict_closed_json(
                request,
                allowed=frozenset(),
                required=frozenset(),
            )
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        try:
            handoff = await run_in_threadpool(
                lambda: app.state.plugin_lifecycle.try_in_chat(plugin_id)
            )
        except Exception as exc:
            # try_in_chat delegates to PluginDirectoryService.try_in_chat_handoff,
            # which can raise the generic directory's own closed causes
            # (plugin_connection_required, plugin_interaction_unavailable,
            # plugin_directory_entry_unavailable) in addition to lifecycle
            # ones -- route through _plugin_directory_error so those reach
            # the client verbatim with their proper status codes instead of
            # collapsing to a generic 503 (controller ruling 1).
            return _plugin_directory_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **handoff.to_public_dict(),
            }
        )

    @app.post("/api/plugins/{plugin_id}/connection/start")
    async def api_plugin_connection_start(
        plugin_id: str,
        request: Request,
    ) -> JSONResponse:
        """Start one exact app connection through the lifecycle service."""
        try:
            body = await _strict_closed_json(
                request,
                allowed=frozenset({"app_id", "revision", "settings"}),
                required=frozenset({"revision"}),
            )
            app_id = body.get("app_id")
            if app_id is not None and (
                type(app_id) is not str
                or not app_id
                or app_id.strip() != app_id
            ):
                raise _McpMutationRejected(422)
            revision = body["revision"]
            if (
                type(revision) is not str
                or re.fullmatch(r"[0-9a-f]{64}", revision) is None
            ):
                raise _McpMutationRejected(422)
            settings = body.get("settings")
            if app_id is not None and type(settings) is not dict:
                raise _McpMutationRejected(422)
            if app_id is None and settings is not None:
                raise _McpMutationRejected(422)
            start = await app.state.plugin_lifecycle.start_connection(
                plugin_id,
                app_id,
                revision=revision,
                settings=settings,
            )
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **start.to_dict(),
            }
        )

    @app.post("/api/plugins/{plugin_id}/connection/poll")
    async def api_plugin_device_connection_poll(
        plugin_id: str,
        request: Request,
    ) -> JSONResponse:
        """Poll one encrypted Device Flow attempt without accepting provider data."""

        try:
            request_id = await _strict_device_poll_json(request)
            status = await app.state.plugin_lifecycle.poll_connection(request_id)
            if getattr(status, "plugin_id", None) != plugin_id:
                raise ValueError("plugin_authorization_request_invalid")
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        except Exception as exc:
            reason = str(exc)
            if reason in {
                "oauth_device_authorization_pending",
                "oauth_device_slow_down",
                "oauth_device_poll_too_soon",
            }:
                return JSONResponse(
                    {
                        "ok": True,
                        "revision": app.state.plugin_lifecycle.revision,
                        "status": "authorizing",
                        "request_id": request_id,
                        "retry_after_seconds": 5,
                    },
                    status_code=202,
                )
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **status.to_dict(),
            }
        )

    @app.get("/api/plugins/{plugin_id}/connection")
    async def api_plugin_connection_status(plugin_id: str) -> JSONResponse:
        """Validation-only per-app setup routing (task-5 controller ruling 1).

        Delegates entirely to ``PluginLifecycleService.status()`` -- the same
        lifecycle snapshot shape ``/connection/start`` and ``/connection/poll``
        already return -- then adds the one field the generic client needs to
        pick a driver without ever branching on a provider name:
        ``setup_action``, derived from ``next_required_app_id``'s own declared
        ``PluginAuthenticationKind`` (read from the already re-verified sealed
        manifest, exactly the way ``installed_plugin_authority`` already does
        for the details route -- never a fresh package download or a new
        trust decision). For a ``secret_form`` action, the declared fields'
        non-secret metadata (``field_id``/``label``/``secret``/``required``)
        is included so the client can render real controls; no secret value
        ever exists at this point, so none is ever returned.

        A snapshot that is not itself `connected` but names no
        `next_required_app_id` (blocked/offline/permission_changed/etc. with
        no incomplete required app) still reports `setup_action: 'connected'`
        -- there is no app left for the generic setup driver to act on, and
        the panel's existing per-state UI (not this driver) already owns
        rendering that case.
        """
        try:
            snapshot = await app.state.plugin_lifecycle.status(plugin_id)
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        payload = snapshot.to_dict()
        next_app_id = payload["next_required_app_id"]
        if next_app_id is None:
            payload["setup_action"] = "connected"
        else:
            try:
                # The requirement comes from the manifest this setup authorizes: a
                # pending update's candidate (which may add the app, or change how
                # it signs in), otherwise the installed version.
                requirement = app.state.plugin_lifecycle.setup_requirement(plugin_id, next_app_id)
                authentication = requirement.authentication
                setup_action = _PLUGIN_SETUP_ACTION_BY_AUTHENTICATION_KIND.get(
                    authentication.kind.value
                )
                if setup_action is None:
                    raise ValueError("plugin_connection_state_invalid")
                payload["setup_action"] = setup_action
                if setup_action == "secret_form":
                    payload["secret_fields"] = [
                        field.to_dict() for field in authentication.secret_fields
                    ]
            except Exception:
                # Any hiccup deriving setup_action (an unreadable manifest, a
                # missing requirement) is reported as this route's own stable
                # cause -- never the raw underlying reason, which could be an
                # arbitrary internal exception string with no client mapping.
                return _plugin_lifecycle_error(ValueError("plugin_connection_state_invalid"))
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **payload,
            }
        )

    @app.get("/oauth/{profile_id}/callback")
    async def api_oauth_callback(profile_id: str, request: Request) -> HTMLResponse:
        """Complete one signed-profile redirect without reflecting credentials."""
        try:
            if _OAUTH_PROFILE_ID_RE.fullmatch(profile_id) is None:
                raise ValueError("oauth_profile_invalid")
            status = await app.state.plugin_lifecycle.complete_connection(
                str(request.url)
            )
            plugin_id = getattr(status, "plugin_id", None)
            if type(plugin_id) is not str or not plugin_id:
                raise ValueError("plugin_authorization_request_invalid")
            canonical_state = getattr(status, "state", None)
            canonical_state = getattr(canonical_state, "value", canonical_state)
            if canonical_state not in {"connected", "setup_required"}:
                raise ValueError("plugin_connection_state_invalid")
            next_required_app_id = getattr(status, "next_required_app_id", None)
            if canonical_state == "setup_required" and (
                type(next_required_app_id) is not str or not next_required_app_id
            ):
                raise ValueError("plugin_connection_state_invalid")
        except Exception as exc:
            reason = str(exc)
            if not re.fullmatch(r"[a-z0-9_]{1,64}", reason):
                reason = type(exc).__name__
            # Named, because a bare reason is unattributable when a dozen
            # sign-ins are in flight. Re-checked rather than trusted: this runs
            # in the handler for a path segment whose validation may be what
            # just failed.
            named = profile_id if _OAUTH_PROFILE_ID_RE.fullmatch(profile_id) else "unnamed profile"
            # The site too: a reason like `plugin_operation_failed` is raised
            # from nineteen places and on its own names none of them.
            log.warning(
                "OAuth callback failed for %s: %s (%s raised at %s)",
                named,
                reason,
                type(exc).__name__,
                _raising_site(exc),
            )
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "hud",
                    "oauth_callback",
                    status="fail",
                    ok=False,
                    summary=reason,
                    reason=reason,
                    profile_id=named,
                    error_type=type(exc).__name__,
                    raising_site=_raising_site(exc),
                )
            except Exception:
                pass
            request_id = None
            try:
                resolved = app.state.plugin_lifecycle.connection_service.callback_request_id(
                    str(request.url)
                )
                if type(resolved) is str and re.fullmatch(r"[a-f0-9]{64}", resolved):
                    request_id = resolved
            except Exception:
                # An unidentifiable callback cannot wake any pending attempt.
                pass
            expired = reason == "oauth_transaction_expired"
            error_code = "plugin_auth_expired" if expired else "plugin_authorization_failed"
            failure_message = _json.dumps({
                "type": "omega:plugin-authorization-failed",
                "requestId": request_id,
                "errorCode": error_code,
            })
            failure_text = (
                "This sign-in expired. Return to Omega and start the connection again."
                if expired else "Omega could not complete this sign-in. Return to settings to retry."
            )
            return HTMLResponse(
                '<!doctype html><meta charset="utf-8"><title>Omega authorization</title>'
                f"<p>{failure_text}</p>"
                '<p><a href="/#settings/plugins">Return to Omega plugin settings</a></p>'
                "<script>(() => {"
                f"const message = {failure_message};"
                "try { if (message.requestId && window.opener && !window.opener.closed) {"
                "window.opener.postMessage(message, window.location.origin);"
                "window.opener.focus(); window.setTimeout(() => window.close(), 80);"
                "} } catch (_) {}"
                "})();</script>",
                status_code=400,
                headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
            )
        # This page is deliberately static: never reflect OAuth code/state/issuer
        # values into the browser document.  The popup was opened by the HUD
        # without `noopener` so the same-origin callback can notify its opener,
        # which refreshes the plugin settings view before this window closes.
        plugin_id_js = _json.dumps(plugin_id)
        connected = canonical_state == "connected"
        message_type_js = _json.dumps(
            "omega:plugin-connected"
            if connected
            else "omega:plugin-setup-required"
        )
        next_app_js = _json.dumps(next_required_app_id)
        status_text = (
            "Omega is connected. Returning to settings…"
            if connected
            else "Additional plugin setup is required. Returning to settings…"
        )
        return HTMLResponse(
            "<!doctype html>"
            '<html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer">'
            "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'\">"
            "<title>Omega authorization complete</title></head><body>"
            f"<p>{status_text}</p>"
            '<p><a href="/#settings/plugins">Return to Omega plugin settings</a></p>'
            "<script>"
            "(() => {"
            "  const returnUrl = new URL('/#settings/plugins', window.location.origin).href;"
            f"  const message = {{type: {message_type_js}, pluginId: {plugin_id_js}, nextAppId: {next_app_js}}};"
            "  try {"
            "    if (window.opener && !window.opener.closed) {"
            "      window.opener.postMessage(message, window.location.origin);"
            "      window.opener.location.replace(returnUrl);"
            "      window.opener.focus();"
            "    } else {"
            "      window.location.replace(returnUrl);"
            "      return;"
            "    }"
            "  } catch (_) {}"
            "  window.setTimeout(() => {"
            "    try { window.close(); } catch (_) {}"
            "    window.setTimeout(() => {"
            "      if (!window.closed) window.location.replace(returnUrl);"
            "    }, 120);"
            "  }, 80);"
            "})();"
            "</script></body></html>",
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.post("/api/plugins/{plugin_id}/disconnect")
    async def api_plugin_disconnect(
        plugin_id: str,
        request: Request,
    ) -> JSONResponse:
        """Disconnect every configured app while keeping the package installed.

        Requires and checks the client's held lifecycle revision (task-6
        controller ruling 2, mirroring /review's own {"revision"}-only body):
        a stale revision returns plugin_revision_conflict before ever
        reaching the lifecycle service -- disconnect removes connection
        authority only, it never removes the installed card.
        """
        try:
            body = await _strict_closed_json(
                request,
                allowed=frozenset({"revision"}),
                required=frozenset({"revision"}),
            )
            revision = body["revision"]
            if type(revision) is not str or _MCP_REVISION_RE.fullmatch(revision) is None:
                raise _McpMutationRejected(422)
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        if revision != app.state.plugin_lifecycle.revision:
            return _plugin_revision_conflict()
        try:
            snapshot = await app.state.plugin_lifecycle.disconnect(plugin_id)
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **snapshot.to_dict(),
            }
        )

    @app.post("/api/plugins/{plugin_id}/uninstall")
    async def api_plugin_uninstall(
        plugin_id: str,
        request: Request,
    ) -> JSONResponse:
        """Disconnect and remove one plugin from this portable install.

        Requires and checks the client's held lifecycle revision (task-6
        controller ruling 2), same shape as /disconnect above -- a stale
        revision returns plugin_revision_conflict before ever removing
        anything.
        """
        try:
            body = await _strict_closed_json(
                request,
                allowed=frozenset({"revision"}),
                required=frozenset({"revision"}),
            )
            revision = body["revision"]
            if type(revision) is not str or _MCP_REVISION_RE.fullmatch(revision) is None:
                raise _McpMutationRejected(422)
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        if revision != app.state.plugin_lifecycle.revision:
            return _plugin_revision_conflict()
        try:
            await app.state.plugin_lifecycle.uninstall(plugin_id)
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                "status": "uninstalled",
                "plugin_id": plugin_id,
            }
        )

    @app.post("/api/plugins/{plugin_id}/update")
    async def api_plugin_update(plugin_id: str, request: Request) -> JSONResponse:
        try:
            body = await _strict_plugin_update_json(request)
            if body["revision"] != app.state.plugin_lifecycle.revision:
                return _plugin_revision_conflict()
            result = await run_in_threadpool(
                lambda: app.state.plugin_lifecycle.update(
                    plugin_id,
                    body["version"],
                    body["confirmation"],
                )
            )
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        if type(result) is PluginUpdatePending:
            return JSONResponse(
                {
                    "ok": True,
                    "revision": app.state.plugin_lifecycle.revision,
                    **result.to_dict(),
                },
                status_code=202,
            )
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **result.to_dict(),
            }
        )

    @app.post("/api/plugins/catalog/sync")
    async def api_plugin_catalog_sync(request: Request) -> JSONResponse:
        """Fetch, verify, and atomically promote a newer signed snapshot.

        Never installs, updates, or enables any plugin -- only the retained
        catalog snapshot changes.
        """
        try:
            _reject_cross_origin_mutation(request)
            result = await run_in_threadpool(app.state.plugin_lifecycle.sync_catalog)
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                "status": result.status.value,
                "sequence": result.sequence,
                "catalog_revision": result.revision,
                "error": result.error,
            }
        )

    @app.get("/api/plugins/updates")
    async def api_plugin_updates_status() -> JSONResponse:
        """Return sanitized update policy/status telemetry."""
        service = app.state.plugin_lifecycle
        if service is None:
            return JSONResponse({"ok": False, "error": "plugin_lifecycle_unavailable"}, status_code=503)
        try:
            policy = await run_in_threadpool(lambda: service.update_policy.to_dict())
            status = dict(getattr(app.state, "plugin_update_status", {}))
            last = await run_in_threadpool(
                lambda: getattr(service, "last_update_check", None)
            )
            # A startup boundary commonly returns ``not_due``; it must not
            # hide a durable candidate/error result from the previous run.
            if last is not None and (
                not status
                or status.get("checked_at") is None
            ):
                status = last.to_dict()
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse({"ok": True, "policy": policy, "status": status})

    @app.post("/api/plugins/updates/check")
    async def api_plugin_updates_check(request: Request) -> JSONResponse:
        """Request one manual lifecycle-owned check; never schedule polling."""
        try:
            _reject_cross_origin_mutation(request)
            service = app.state.plugin_lifecycle
            if service is None:
                return JSONResponse(
                    {"ok": False, "error": "plugin_lifecycle_unavailable"},
                    status_code=503,
                )
            result = await run_in_threadpool(lambda: service.check_updates(force=True))
            app.state.plugin_update_status = result.to_dict()
            return JSONResponse({"ok": True, "status": result.to_dict()})
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)

    @app.post("/api/plugins/personal/remote")
    async def api_plugin_personal_remote(request: Request) -> JSONResponse:
        try:
            body = await _strict_closed_json(
                request,
                allowed=frozenset({"source_url"}),
                required=frozenset({"source_url"}),
            )
            source_url = body["source_url"]
            if (
                type(source_url) is not str
                or not source_url
                or source_url != source_url.strip()
                or len(source_url) > 2_048
            ):
                raise _McpMutationRejected(422)
            parsed_source = urlsplit(source_url)
            if (
                parsed_source.scheme != "https"
                or not parsed_source.hostname
                or parsed_source.username is not None
                or parsed_source.password is not None
                or parsed_source.fragment
            ):
                raise _McpMutationRejected(422)
            result = await run_in_threadpool(
                lambda: app.state.plugin_lifecycle.add_personal_remote(
                    source_url
                )
            )
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **result,
            }
        )

    @app.post("/api/plugins/personal/import")
    async def api_plugin_personal_import(request: Request) -> JSONResponse:
        try:
            body = await _strict_closed_json(
                request,
                allowed=frozenset({"upload_id"}),
                required=frozenset({"upload_id"}),
            )
            upload_id = body["upload_id"]
            if type(upload_id) is not str or not _PLUGIN_REQUEST_ID_RE.fullmatch(upload_id):
                raise _McpMutationRejected(422)
            result = await run_in_threadpool(
                lambda: app.state.plugin_lifecycle.import_personal(upload_id)
            )
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **result,
            }
        )

    @app.post("/api/plugins/operate")
    async def api_plugin_operate(request: Request) -> JSONResponse:
        """Queue one structured plugin operation through the Pressure ingress."""
        try:
            body = await _strict_plugin_operation_json(request)
            queued = await run_in_threadpool(
                handle_operator_chat,
                "Run the approved plugin operation.",
                directive_intent="PLUGIN_OPERATION",
                plugin_operation=body,
            )
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_operation"},
                status_code=exc.status_code,
            )
        except (TypeError, ValueError):
            return JSONResponse({"ok": False, "error": "invalid_plugin_operation"}, status_code=422)
        except Exception:
            return JSONResponse({"ok": False, "error": "plugin_operation_failed"}, status_code=503)
        if not isinstance(queued, dict) or not queued.get("ok"):
            return JSONResponse({"ok": False, "error": "plugin_operation_failed"}, status_code=503)
        return JSONResponse(
            {"ok": True, "queued": True, "cycle_id": str(queued.get("cycle_id") or "")},
            status_code=202,
        )

    @app.get("/api/plugins/permissions/pending")
    async def api_plugin_permissions_pending() -> JSONResponse:
        """Return opaque HUD permission cards; never include secrets or plan bodies."""
        try:
            store = get_default_plugin_permission_store()
            pending = await run_in_threadpool(store.pending)
            requests = [_permission_hud_dict(item) for item in pending]
        except Exception as exc:
            return _plugin_permission_http_error(exc)
        return JSONResponse({"ok": True, "requests": requests})

    @app.post("/api/plugins/permissions/{request_id}/approve")
    async def api_plugin_permission_approve(request_id: str, request: Request) -> JSONResponse:
        """Queue an Allow decision. Never approve a grant, open an episode, or call a provider."""
        try:
            body = await _strict_closed_json(
                request,
                allowed=_PLUGIN_DECISION_KEYS,
                required=_PLUGIN_DECISION_KEYS,
            )
            payload = _queue_permission_decision(
                request_id,
                str(body["expected_revision"]),
                "allow",
            )
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_permission_request"},
                status_code=exc.status_code,
            )
        except ValueError as exc:
            return _plugin_permission_http_error(exc)
        return JSONResponse(payload)

    @app.post("/api/plugins/permissions/{request_id}/cancel")
    async def api_plugin_permission_cancel(request_id: str, request: Request) -> JSONResponse:
        """Queue a Cancel decision. Never invoke a provider or mutate the grant store."""
        try:
            body = await _strict_closed_json(
                request,
                allowed=_PLUGIN_DECISION_KEYS,
                required=_PLUGIN_DECISION_KEYS,
            )
            payload = _queue_permission_decision(
                request_id,
                str(body["expected_revision"]),
                "cancel",
            )
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_permission_request"},
                status_code=exc.status_code,
            )
        except ValueError as exc:
            return _plugin_permission_http_error(exc)
        return JSONResponse(payload)

    @app.get("/api/plugins/{plugin_id}/permissions")
    async def api_plugin_permissions(plugin_id: str, request: Request) -> JSONResponse:
        """Return exact provider and Omega capability settings."""
        try:
            if set(request.query_params) - {"app_id"}:
                raise _McpMutationRejected(422)
            app_id = request.query_params.get("app_id")
            if app_id is not None and (not app_id or app_id != app_id.strip()):
                raise _McpMutationRejected(422)
            payload = await app.state.plugin_lifecycle.permissions(
                plugin_id,
                app_id,
            )
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        public = payload.to_dict() if hasattr(payload, "to_dict") else payload
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **public,
            }
        )

    @app.put("/api/plugins/{plugin_id}/permissions")
    async def api_plugin_permissions_put(
        plugin_id: str,
        request: Request,
    ) -> JSONResponse:
        """Persist exact offered settings and reconnect when authority changes.

        Requires and checks the client's held lifecycle revision at the
        route level (task-6 controller ruling 2, mirroring /review's own
        precheck): a stale revision returns plugin_revision_conflict
        immediately, before ever computing a settings diff against moving
        ground. PluginLifecycleService.set_permissions still re-checks the
        revision itself around its own internal await (a race-condition
        guard for a concurrent mutation during this request, mapped to the
        existing plugin_permission_changed cause) -- this route-level check
        only covers the common already-stale case earlier and with the
        ruling's own required cause.
        """
        try:
            body = await _strict_closed_json(
                request,
                allowed=frozenset({"revision", "settings", "app_id"}),
                required=frozenset({"revision", "settings"}),
            )
            revision = body["revision"]
            if type(revision) is not str or _MCP_REVISION_RE.fullmatch(revision) is None:
                raise _McpMutationRejected(422)
            if type(body["settings"]) is not dict:
                raise _McpMutationRejected(422)
            app_id = body.get("app_id")
            if app_id is not None and (
                type(app_id) is not str or not app_id or app_id != app_id.strip()
            ):
                raise _McpMutationRejected(422)
        except _McpMutationRejected as exc:
            return JSONResponse(
                {"ok": False, "error": "invalid_plugin_lifecycle_request"},
                status_code=exc.status_code,
            )
        if revision != app.state.plugin_lifecycle.revision:
            return _plugin_revision_conflict()
        try:
            payload = await app.state.plugin_lifecycle.set_permissions(
                plugin_id,
                revision=revision,
                settings=body["settings"],
                app_id=app_id,
            )
        except Exception as exc:
            return _plugin_lifecycle_error(exc)
        public = payload.to_dict() if hasattr(payload, "to_dict") else payload
        return JSONResponse(
            {
                "ok": True,
                "revision": app.state.plugin_lifecycle.revision,
                **public,
            }
        )

    # =================================================================
    # Operator identity — dynamic display name for HUD personalization
    # =================================================================
    @app.get("/api/operator/identity")
    async def api_operator_identity() -> JSONResponse:
        """Return the operator's display name + relationship from memory.

        Reads `hud.operator_identity.operator_context()` which looks up
        the operator persona (lobe 1, coord [1,0], person_id="operator")
        written by the Settings tab's operator-profile save. Returns
        empty strings until the operator fills out the Settings tab —
        the HUD's frontend falls back to a neutral "operator" display.

        This endpoint exists so the HUD can render personalized greetings
        without HARDCODING any specific name. The system's IDENTITY of
        creator (immutable in omega_identity.json + creator_noah.json)
        is unaffected — that's identity-substrate, not operator-display.
        """
        try:
            from hud.operator_identity import is_onboarded, operator_context

            ctx = operator_context()
            return JSONResponse(
                {
                    "ok": True,
                    "name": ctx.get("name", "") or "",
                    "relationship": ctx.get("relationship", "") or "",
                    # onboarded -> the HUD locks the profile form + suppresses the onboarding prompt.
                    "onboarded": is_onboarded(),
                }
            )
        except Exception as exc:
            log.warning("operator identity read failed: %s", exc)
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "operator_identity",
                    status="degraded",
                    summary="operator identity read failed — HUD falls back to neutral display",
                    component="hud.server",
                    failure_class="operator_identity_read_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("operator_identity emit_phase failed: %s", _e)
            return JSONResponse(
                {
                    "ok": False,
                    "name": "",
                    "relationship": "",
                    "onboarded": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    # =================================================================
    # Settings HUD screen — operator profile + HUD appearance
    # =================================================================
    @app.get("/api/settings/functions")
    async def api_settings_functions() -> JSONResponse:
        """Return the settings function shelf shown by the HUD."""
        return JSONResponse({"ok": True, "functions": hud_settings.settings_functions()})

    @app.get("/api/settings/trusted-folders")
    async def api_settings_trusted_folders_get() -> JSONResponse:
        """Return operator-granted roots and the currently selected root."""
        from core.trusted_folders import active_trusted_folder, load_trusted_folders

        try:
            folders = load_trusted_folders()
            active = active_trusted_folder()
            return JSONResponse(
                {
                    "ok": True,
                    "folders": [folder.to_dict() for folder in folders],
                    "active": str(active) if active else None,
                    "required": os.environ.get("OMEGA_TRUSTED_FOLDERS_REQUIRED", "")
                    .strip()
                    .lower()
                    in {"1", "true", "yes", "on"},
                }
            )
        except Exception as exc:  # noqa: BLE001 - settings read must remain attributable
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"}, status_code=500
            )

    @app.post("/api/settings/trusted-folders")
    async def api_settings_trusted_folders_post(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Grant, revoke, or select a trusted folder from the Settings panel."""
        from core.trusted_folders import (
            activate_grant_if_unset,
            active_trusted_folder,
            grant_trusted_folder,
            revoke_trusted_folder,
            set_active_trusted_folder,
        )

        body = payload or {}
        action = str(body.get("action") or "").strip().casefold()
        path = str(body.get("path") or "").strip()
        if action not in {"grant", "revoke", "select"}:
            raise HTTPException(status_code=400, detail="action must be grant, revoke, or select")
        if not path:
            raise HTTPException(status_code=400, detail="path is required")
        try:
            if action == "grant":
                folder = grant_trusted_folder(path)
                active_after_grant = activate_grant_if_unset(folder)
                result = {"folder": folder.to_dict(), "active": str(active_after_grant)}
            elif action == "revoke":
                result = {"removed": revoke_trusted_folder(path)}
            else:
                result = {"active": str(set_active_trusted_folder(path))}
            active = active_trusted_folder()
            result.update({"ok": True, "active": str(active) if active else None})
            return JSONResponse(result)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/api/settings/occupation-options")
    async def api_settings_occupation_options() -> JSONResponse:
        """Occupation dropdown (with lobe routing) + per-occupation subcategory
        hints for the first-wake operator onboarding."""
        from hud.operator_onboarding import occupation_options

        return JSONResponse({"ok": True, "occupations": occupation_options()})

    @app.get("/api/settings/hud-theme")
    async def api_settings_hud_theme_get() -> JSONResponse:
        """Return persisted HUD appearance settings and allowed options."""
        return JSONResponse(hud_settings.load_hud_theme_settings())

    @app.post("/api/settings/hud-theme")
    async def api_settings_hud_theme_post(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Persist operator HUD appearance preferences under Guardian state."""
        from guardian.terminal_feedback import emit_phase

        try:
            result = hud_settings.save_hud_theme_settings(payload or {})
        except ValueError as exc:
            emit_phase(
                "hud",
                "settings_hud_theme",
                status="fail",
                summary=f"bad payload: {exc}",
                error_kind="ValueError",
            )
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            emit_phase(
                "hud",
                "settings_hud_theme",
                status="fail",
                summary=f"write raised {type(exc).__name__}",
                error_kind=type(exc).__name__,
                error=str(exc),
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

        settings = result.get("settings") or {}
        emit_phase(
            "hud",
            "settings_hud_theme",
            status="ok",
            summary=(
                f"theme={settings.get('theme')} density={settings.get('density')} "
                f"motion={settings.get('motion')} glow={settings.get('glow')}"
            ),
            accepted_fields=result.get("accepted_fields") or [],
            theme=settings.get("theme"),
            density=settings.get("density"),
            motion=settings.get("motion"),
            glow=settings.get("glow"),
            panel_depth=settings.get("panel_depth"),
        )
        return JSONResponse(result)

    # P4 provider-consolidation: the /api/settings/openrouter GET/POST pair is DELETED with the
    # HUD "owl" section. Agent provider choice, endpoints, secrets, and generation tunables are
    # owned by the runtime brain seat; live chat is the sole agent surface.

    @app.get("/api/settings/cloud-providers")
    async def api_settings_cloud_providers() -> JSONResponse:
        """Return the cloud-only connection cards without any credential."""
        from runtime.cloud_providers import public_cloud_providers

        return JSONResponse({"ok": True, "providers": public_cloud_providers()})

    @app.post("/api/settings/mind-backend/test")
    async def api_settings_mind_backend_test(
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Probe a selected cloud seat and return its available model ids."""
        from runtime import brain_cloud_config
        from runtime.cloud_providers import CloudProviderError, discover_cloud_models

        provider = str(payload.get("provider") or "").strip().lower()
        api_key = str(payload.get("api_key") or "").strip()
        base_url = str(payload.get("base_url") or "").strip() or None
        try:
            if not api_key:
                api_key = brain_cloud_config.provider_api_key(provider)
            models = discover_cloud_models(provider, api_key, base_url)
        except CloudProviderError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except brain_cloud_config.MindBackendConfigError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        return JSONResponse({"ok": True, "provider": provider, "models": models})

    @app.post("/api/settings/mind-backend/key")
    async def api_settings_mind_backend_key(
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Explicitly save one provider key to the encrypted portable vault."""
        from runtime import brain_cloud_config

        try:
            result = brain_cloud_config.save_provider_api_key(
                str(payload.get("provider") or ""),
                str(payload.get("api_key") or ""),
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        except Exception as exc:  # noqa: BLE001 - settings boundary must return stable JSON
            log.warning("mind connector key save failed: %s", exc)
            return JSONResponse(
                {"ok": False, "error": "mind_connector_key_save_failed"},
                status_code=503,
            )
        return JSONResponse({"ok": True, **result})

    @app.get("/api/settings/mind-backend")
    async def api_settings_mind_backend_get() -> JSONResponse:
        """Return the configured backend-neutral mind connector without secrets."""
        from runtime import brain_cloud_config

        return JSONResponse({"ok": True, "connector": brain_cloud_config.public_state()})

    @app.post("/api/settings/mind-backend")
    async def api_settings_mind_backend_post(
        payload: Dict[str, Any] = Body(...),
    ) -> JSONResponse:
        """Persist one mind connector; plaintext credentials never enter mind state."""
        from runtime import brain_cloud_config

        try:
            connector = brain_cloud_config.configure_connector(payload or {})
        except ValueError as exc:
            return JSONResponse(
                {"ok": False, "error": str(exc)},
                status_code=422,
            )
        except Exception as exc:  # noqa: BLE001 - settings boundary must return stable JSON
            log.warning("mind connector save failed: %s", exc)
            return JSONResponse(
                {"ok": False, "error": "mind_connector_save_failed"},
                status_code=503,
            )
        return JSONResponse({"ok": True, "connector": connector})

    @app.get("/api/settings/voice")
    async def api_settings_voice_get() -> JSONResponse:
        """Return persisted voice & speech settings and allowed options."""
        return JSONResponse(hud_settings.load_voice_settings())

    @app.post("/api/settings/voice")
    async def api_settings_voice_post(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Persist operator voice & speech preferences under Guardian state."""
        from guardian.terminal_feedback import emit_phase

        try:
            result = hud_settings.save_voice_settings(payload or {})
        except ValueError as exc:
            emit_phase(
                "hud",
                "settings_voice",
                status="fail",
                summary=f"bad payload: {exc}",
                error_kind="ValueError",
            )
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            emit_phase(
                "hud",
                "settings_voice",
                status="fail",
                summary=f"write raised {type(exc).__name__}",
                error_kind=type(exc).__name__,
                error=str(exc),
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

        settings = result.get("settings") or {}
        emit_phase(
            "hud",
            "settings_voice",
            status="ok",
            summary=(
                f"tts_voice={settings.get('tts_voice')} "
                f"tts_speed={settings.get('tts_speed')} "
                f"stt_language={settings.get('stt_language')} "
                f"stt_model={settings.get('stt_model')}"
            ),
            accepted_fields=result.get("accepted_fields") or [],
            tts_voice=settings.get("tts_voice"),
            stt_language=settings.get("stt_language"),
            stt_model=settings.get("stt_model"),
        )
        return JSONResponse(result)

    @app.post("/api/settings/operator-profile")
    async def api_settings_operator_profile(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Persist operator personal info into the operator persona namespace.

        Body accepts fields such as name, preferred_name, birthday, location,
        occupation, relationship, and notes, plus a subcategories list. On save,
        raw ingress is recorded and Guardian-screened before persona fields and
        operator-linked work topics are projected into canonical memory. The first
        LEARN episode asks Omega foldback to expand those topics into child gaps.
        """
        from guardian.terminal_feedback import emit_phase

        try:
            result = hud_settings.save_operator_profile(payload or {})
        except ValueError as exc:
            emit_phase(
                "hud",
                "settings_operator_profile",
                status="fail",
                summary=f"bad payload: {exc}",
                error_kind="ValueError",
            )
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:
            emit_phase(
                "hud",
                "settings_operator_profile",
                status="fail",
                summary=f"write raised {type(exc).__name__}",
                error_kind=type(exc).__name__,
                error=str(exc),
            )
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

        emit_phase(
            "hud",
            "settings_operator_profile",
            status="ok" if result.get("ok") else "fail",
            summary=(
                f"operator profile fields={len(result.get('accepted_fields') or [])} "
                f"status={(result.get('memory') or {}).get('status')}"
            ),
            accepted_fields=result.get("accepted_fields") or [],
            persona_id=result.get("persona_id"),
            memory_status=(result.get("memory") or {}).get("status"),
            memory_path=(result.get("memory") or {}).get("path"),
        )
        return JSONResponse(result)

    @app.post("/api/settings/operator-profile/upload-picture")
    async def api_settings_operator_profile_upload_picture(
        file: UploadFile = File(...),
    ) -> JSONResponse:
        """Upload operator profile picture from file.

        Accepts an image file, stores it under guardian/state/uploads/images/,
        and returns the path for use in the operator profile.
        """
        if not file:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "settings_operator_profile_upload_picture",
                    status="fail",
                    summary="upload rejected — no file in request",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("settings_operator_profile_upload_picture emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="no file")

        # Classify as image
        kind = _classify_upload(file.filename or "upload.bin")
        if kind != "image":
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "settings_operator_profile_upload_picture",
                    status="fail",
                    summary=f"upload rejected — not an image (kind={kind})",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("settings_operator_profile_upload_picture emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="file must be an image")

        target_dir = _UPLOAD_ROOT / kind
        target_dir.mkdir(parents=True, exist_ok=True)
        stored_name = _safe_upload_name(file.filename or "upload.bin")
        target_path = target_dir / stored_name

        try:
            image_bytes = await _read_upload_bytes_capped(file, max_bytes=_UPLOAD_MAX_BYTES)
        except HTTPException:
            raise
        if not image_bytes:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "settings_operator_profile_upload_picture",
                    status="fail",
                    summary="upload rejected — empty file",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("settings_operator_profile_upload_picture emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="empty file")

        _verify_image_bytes(image_bytes)

        try:
            target_path.write_bytes(image_bytes)
            written = len(image_bytes)
        except Exception as exc:
            if target_path.exists():
                target_path.unlink(missing_ok=True)
            log.exception("settings_operator_profile_upload_picture failed")
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "settings_operator_profile_upload_picture",
                    status="fail",
                    summary="operator picture upload failed",
                    component="hud.server",
                    failure_class="upload_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("settings_operator_profile_upload_picture emit_phase failed: %s", _e)
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

        # Convert to relative HTTP path
        http_path = f"/api/uploads/{kind}/{stored_name}"

        try:
            from guardian.terminal_feedback import emit_phase

            emit_phase(
                "hud",
                "settings_operator_profile_upload_picture",
                status="ok",
                summary=f"operator picture uploaded: {file.filename}",
                filename=file.filename,
                size=written,
                path=http_path,
            )
        except Exception as _e:
            log.debug("settings_operator_profile_upload_picture emit_phase failed: %s", _e)

        return JSONResponse(
            {
                "ok": True,
                "filename": file.filename,
                "size": written,
                "path": http_path,
                "local_path": str(target_path),
            }
        )

    @app.get("/api/settings/operator-profile/picture")
    async def api_settings_operator_profile_get_picture() -> JSONResponse:
        """Get the current operator profile picture.

        Returns the latest operator face image path if available.
        """
        try:
            from hud.operator_identity import latest_operator_profile_fields
            from core.paths import GUARDIAN_STATE_ROOT

            fields = latest_operator_profile_fields()
            picture_path = fields.get("picture_path", "")

            # Check if there's a face memory for the operator
            face_image_path = None
            try:
                face_dir = GUARDIAN_STATE_ROOT / "face_images" / "operator"
                if face_dir.exists():
                    images = list(face_dir.glob("*.png")) + list(face_dir.glob("*.jpg"))
                    if images:
                        # Get the most recent image
                        latest_image = max(images, key=lambda p: p.stat().st_mtime)
                        face_image_path = f"/api/face-images/operator/{latest_image.name}"
            except Exception:
                pass

            return JSONResponse(
                {
                    "ok": True,
                    "picture_path": picture_path,
                    "face_image_path": face_image_path,
                    "has_picture": bool(picture_path or face_image_path),
                }
            )
        except Exception as exc:
            log.exception("settings_operator_profile_get_picture failed")
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    @app.get("/api/face-images/{person_id}/{filename}")
    async def api_face_image(person_id: str, filename: str) -> FileResponse:
        """Serve face images from guardian/state/face_images/."""
        from core.hud_path_guard import resolve_face_image_path

        try:
            file_path = resolve_face_image_path(person_id, filename)
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid face image path")

        if not file_path.exists():
            raise HTTPException(status_code=404, detail="face image not found")

        return FileResponse(file_path)

    @app.post("/api/shutdown")
    async def api_system_shutdown(payload: Dict[str, Any] | None = Body(None)) -> JSONResponse:
        """Trigger graceful system shutdown via the shutdown script."""
        import subprocess
        from core.paths import OMEGA_ROOT

        log.info("HUD requested system shutdown via shutdown.py")
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if _sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_PROCESS_GROUP
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            # POSIX: the shutdown script must outlive the HUD it kills — its
            # own session, so the teardown's process-group sweep spares it.
            kwargs["start_new_session"] = True

        subprocess.Popen(
            [str(portable_python_path(OMEGA_ROOT)), str(OMEGA_ROOT / "scripts" / "shutdown.py")],
            cwd=str(OMEGA_ROOT),
            **kwargs,
        )
        return JSONResponse(
            {
                "ok": True,
                "message": "Shutdown script activated.",
            }
        )

    @app.get("/api/events")
    async def api_events(request: Request) -> StreamingResponse:
        """SSE event stream — live phase/signal/memory events from
        guardian.terminal_feedback. Client passes Last-Event-ID header
        to resume from a specific seq; without it, sends the current
        backlog plus all new events.

        Frontend uses `new EventSource('/api/events')` then listens
        on event types 'phase' | 'signal' | 'memory'. HUD-10 wiring.
        """
        # Make sure the subscriber is installed (idempotent — safe to
        # call on every connection in case server.py was imported
        # before terminal_feedback was ready).
        hud_events.install()

        # Resume from the client's last-seen seq if provided
        last_id_hdr = request.headers.get("last-event-id")
        try:
            start_seq = int(last_id_hdr) if last_id_hdr else 0
        except ValueError:
            start_seq = 0

        return StreamingResponse(
            hud_events.sse_stream(
                start_seq=start_seq,
                is_disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",  # disable nginx buffering if present
                "Connection": "keep-alive",
            },
        )

    @app.get("/api/skills")
    async def api_skills_list() -> JSONResponse:
        """Return live built-ins, local skills, and installed plugins for chat."""
        try:
            directory = app.state.plugin_directory
            installed = directory.list_directory().plugins
            payload = qwen_skills.skill_payload(plugins=(item.to_dict() for item in installed))
        except Exception:
            # A catalog outage must not hide the local skill and permission
            # commands. Uninstalled or unreadable plugins simply disappear.
            payload = qwen_skills.skill_payload()
        return JSONResponse(payload)
    # Install the events subscriber once at app construction so events
    # buffer from the moment Omega boots, not just when /api/events
    # gets its first connection. Idempotent.
    hud_events.install()

    # =================================================================
    # HUD-11: Vision identify (POST a base64 image -> face recognition)
    # =================================================================
    @app.post("/api/vision/identify")
    async def api_vision_identify(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Identify face(s) in a base64-encoded image. The browser
        captures via getUserMedia after Guardian consent (UI-side flow);
        this endpoint trusts that consent occurred and runs the
        face-recognition loader against the image.

        Body: {"image_b64": "<JPEG/PNG base64 without data: prefix>"}
        Response: {ok, faces:[{bbox, embedding_id?, recognized_name?}], elapsed_seconds}
        """
        b64 = payload.get("image_b64") or ""
        if not isinstance(b64, str) or not b64:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_identify",
                    status="fail",
                    summary="vision identify rejected — image_b64 missing",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_identify emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="image_b64 required")
        import time as _time
        import base64 as _b64

        t0 = _time.time()
        try:
            raw = _b64.b64decode(b64.split(",", 1)[-1])  # tolerate "data:image/png;base64,..."
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_identify",
                    status="fail",
                    summary="vision identify rejected — image_b64 decode failed",
                    component="hud.server",
                    failure_class="image_decode_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_identify emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail=f"image_b64 decode failed: {exc}")
        try:
            from runtime.face.recognition import identify as _face_identify

            result = _face_identify(raw)
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_identify",
                    status="fail",
                    summary="face identify raised — recognition failed",
                    component="hud.server",
                    failure_class="face_identify_raised",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_identify emit_phase failed: %s", _e)
            return JSONResponse(
                {
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_seconds": _time.time() - t0,
                }
            )
        if not isinstance(result, dict):
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_identify",
                    status="fail",
                    summary="face loader returned non-dict result",
                    component="hud.server",
                    failure_class="face_loader_bad_result",
                )
            except Exception as _e:
                log.debug("vision_identify emit_phase failed: %s", _e)
            return JSONResponse(
                {
                    "ok": False,
                    "error": "face loader returned non-dict result",
                    "elapsed_seconds": _time.time() - t0,
                }
            )
        # Recognition could not run (runtime unreachable / ok=false).
        # Surface this distinctly — NOT as a false "no face detected"
        # (audit line 101a: the down-state was byte-identical to a
        # healthy no-match, so support/operator could not tell infra
        # failure from a genuine no-match).
        if result.get("degraded"):
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_identify",
                    status="degraded",
                    summary="face runtime unavailable — recognition could not run",
                    component="hud.server",
                    failure_class="face_runtime_unavailable",
                )
            except Exception as _e:
                log.debug("vision_identify emit_phase failed: %s", _e)
            return JSONResponse(
                {
                    "ok": False,
                    "degraded": True,
                    "error": result.get("error")
                    or "face runtime unavailable — recognition could not run",
                    "reason": result.get("reason", "face_runtime_unavailable"),
                    "faces": [],
                    "face_count": 0,
                    "recognized_name": None,
                    "confidence": 0.0,
                    "elapsed_seconds": _time.time() - t0,
                }
            )
        # Translate runtime contract -> browser/FaceWatch contract:
        #   runtime returns {name, confidence, bbox, ...[, reason="no_face_detected"]}
        #   browser expects  {recognized_name, confidence, face_count, faces:[...]}
        bbox = result.get("bbox")
        emb = result.get("embedding")
        has_emb = isinstance(emb, list) and len(emb) >= 8
        face_detected = result.get("reason") != "no_face_detected" and (
            bbox is not None or int(result.get("face_count") or 0) > 0 or has_emb
        )
        face_count = int(result.get("face_count") or (1 if face_detected else 0))
        recognized_name = result.get("recognized_name") or result.get("name")
        # Brain-side fallback: if runtime returned an embedding but no
        # match (cold-boot _face_db is empty), check the brain-side
        # anchor_store (warmed from vision lobe (7,1) at boot per
        # core.face_recall_warmup). This is the cold-boot recognition
        # bridge — vision-lobe-persisted faces match on first capture
        # without needing a runtime restart side-channel.
        if face_detected and not recognized_name and isinstance(result.get("embedding"), list):
            try:
                from runtime.face.anchor_store import get_anchor_store

                _anchor = get_anchor_store().identify(result["embedding"])
                if _anchor.get("recognized_name"):
                    recognized_name = _anchor["recognized_name"]
                    result["confidence"] = _anchor["confidence"]
                    result["candidates"] = _anchor.get("candidates", [])
                    result["recognition_source"] = "anchor_store_warmup"
                    try:
                        from guardian.terminal_feedback import emit_data

                        emit_data(
                            "hud",
                            "anchor_identify",
                            ok=True,
                            recognized_name=recognized_name,
                            source="anchor_store_warmup",
                        )
                    except Exception as _e:
                        log.debug("anchor_identify emit_data failed: %s", _e)
            except Exception as _anc_exc:
                log.debug("anchor_store fallback failed: %s", _anc_exc)
        face_rows: list[dict] = []
        if face_count > 0:
            row = {
                "bbox": bbox,
                "score": result.get("score") or result.get("confidence"),
                "recognized_name": recognized_name,
                "confidence": result.get("confidence", 0.0),
                "candidates": result.get("candidates", []),
                "threshold": result.get("threshold"),
            }
            if isinstance(result.get("embedding"), list) and result["embedding"]:
                row["embedding"] = result["embedding"]
            face_rows.append(row)
        # "Who is this?" (vision spec §3): a recognized face pulls the person's
        # persona facts so the answer is "that's Noah — <what Omega knows>",
        # not just a name. Persona lane is lobe 1 (recallable); lobe-7 stays
        # query-closed. Failure-soft: identity alone still returns.
        persona_facts: list[str] = []
        if recognized_name:
            try:
                from core.persona_router import recall_for_person

                for _row in (recall_for_person(recognized_name) or [])[:6]:
                    _p = _row.get("payload") if isinstance(_row, dict) else None
                    _txt = str((_p or {}).get("text") or (_p or {}).get("summary") or "").strip()
                    if _txt:
                        persona_facts.append(_txt[:200])
            except Exception:
                log.debug("identify persona recall failed (non-fatal)", exc_info=True)
        payload_out = {
            **result,
            "ok": bool(result.get("ok")),
            "face_count": face_count,
            "recognized_name": recognized_name,
            "confidence": result.get("confidence", 0.0),
            "faces": face_rows,
            "persona_facts": persona_facts,
            "elapsed_seconds": _time.time() - t0,
        }
        try:
            from guardian.terminal_feedback import emit_phase

            emit_phase(
                "hud",
                "vision_identify",
                status="ok",
                summary="face identify completed",
                component="hud.server",
                face_count=face_count,
                recognized=bool(recognized_name),
            )
        except Exception as _e:
            log.debug("vision_identify emit_phase failed: %s", _e)
        return JSONResponse(payload_out)

    @app.get("/api/vision/status")
    async def api_vision_status() -> JSONResponse:
        """Report expected face model paths and current anchor count."""
        try:
            from runtime.face.recognition import face_status

            return JSONResponse(face_status())
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_status",
                    status="degraded",
                    summary="vision status probe failed — face model status unavailable",
                    component="hud.server",
                    failure_class="vision_status_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_status emit_phase failed: %s", _e)
            return JSONResponse(
                {"ok": False, "error_type": type(exc).__name__, "models": [], "missing": []}
            )

    # =================================================================
    # FACE-2: Vision enroll + anchors list (Windows-Hello-style)
    # =================================================================
    @app.post("/api/vision/enroll")
    async def api_vision_enroll(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Enroll a face under a name. Camera capture happens browser-
        side (FaceWatch); this endpoint trusts that consent occurred
        and that the operator just spoke 'Omega remember me as <name>'.

        Body: {"image_b64": "<JPEG/PNG base64>", "name": "<operator name>"}
        Response: {ok, queued, cycle_id, name, bbox} on admission
                  {ok:false, error} on no-face / bad-input
        """
        b64 = payload.get("image_b64") or ""
        name = payload.get("name") or ""
        if not isinstance(b64, str) or not b64:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_enroll",
                    status="fail",
                    summary="vision enroll rejected — image_b64 missing",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_enroll emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="image_b64 required")
        if not isinstance(name, str) or not name.strip():
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_enroll",
                    status="fail",
                    summary="vision enroll rejected — name missing",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_enroll emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="name required")
        blocked = _require_face_runtime(phase="vision_enroll")
        if blocked is not None:
            return blocked
        import base64 as _b64

        try:
            raw = _b64.b64decode(b64.split(",", 1)[-1])
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_enroll",
                    status="fail",
                    summary="vision enroll rejected — image_b64 decode failed",
                    component="hud.server",
                    failure_class="image_decode_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_enroll emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail=f"image_b64 decode failed: {exc}")
        try:
            from runtime.face.recognition import enroll as _face_enroll

            result = _face_enroll(raw, name.strip())
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_enroll",
                    status="fail",
                    summary="face enroll raised — enrollment failed",
                    component="hud.server",
                    failure_class="face_enroll_raised",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_enroll emit_phase failed: %s", _e)
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        if isinstance(result, dict) and not result.get("ok"):
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_enroll",
                    status="fail",
                    summary="face enroll failed — backend returned ok=false",
                    component="hud.server",
                    failure_class="face_enroll_failed",
                    error=str(result.get("error") or result.get("reason") or "")[:120],
                )
            except Exception as _e:
                log.debug("vision_enroll emit_phase failed: %s", _e)
        return JSONResponse(result, status_code=202 if result.get("queued") else 200)

    # =================================================================
    # FACE-5: Operator answers "I see a new face. Who is this?" prompt.
    # Persists to vision lobe (7,1) face_embeddings + creates persona
    # at (1,0,person_id) + bidirectional links so future identify
    # matches surface full context, not just a name.
    # Per project_face_persistence_via_vision_lobe.
    # =================================================================
    @app.post("/api/vision/identify-upload")
    async def api_vision_identify_upload(file: UploadFile = File(...)) -> JSONResponse:
        """Upload a face image file and identify who it is.

        Simple "who is this" flow: upload image → recognize → return name.
        """
        # Read uploaded file (size-capped)
        image_bytes = await _read_upload_bytes_capped(file, max_bytes=_UPLOAD_MAX_BYTES)
        if not image_bytes:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_identify_upload",
                    status="fail",
                    summary="vision identify-upload rejected — empty file",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_identify_upload emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="empty file")

        # Run face recognition
        try:
            from runtime.face.recognition import identify as _face_identify

            result = _face_identify(image_bytes)
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_identify_upload",
                    status="fail",
                    summary="vision identify-upload raised — recognition failed",
                    component="hud.server",
                    failure_class="face_identify_raised",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_identify_upload emit_phase failed: %s", _e)
            return JSONResponse(
                {"ok": False, "error": f"face recognition failed: {type(exc).__name__}: {exc}"}
            )

        if not isinstance(result, dict):
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_identify_upload",
                    status="fail",
                    summary="vision identify-upload failed — invalid result type",
                    component="hud.server",
                    failure_class="face_loader_bad_result",
                )
            except Exception as _e:
                log.debug("vision_identify_upload emit_phase failed: %s", _e)
            return JSONResponse({"ok": False, "error": "face recognition returned invalid result"})

        if result.get("degraded"):
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_identify_upload",
                    status="degraded",
                    summary="vision identify-upload degraded — face runtime unavailable",
                    component="hud.server",
                    failure_class="face_runtime_unavailable",
                )
            except Exception as _e:
                log.debug("vision_identify_upload emit_phase failed: %s", _e)
            return JSONResponse(
                {
                    "ok": False,
                    "error": "face runtime unavailable - cannot identify",
                    "degraded": True,
                }
            )

        try:
            from guardian.terminal_feedback import emit_phase

            emit_phase(
                "hud",
                "vision_identify_upload",
                status="ok",
                summary="vision identify-upload completed",
                component="hud.server",
                recognized=bool(result.get("recognized_name")),
            )
        except Exception as _e:
            log.debug("vision_identify_upload emit_phase failed: %s", _e)
        return JSONResponse(
            {
                "ok": True,
                "recognized_name": result.get("recognized_name"),
                "confidence": result.get("confidence", 0.0),
                "face_count": result.get("face_count", 0),
                "candidates": result.get("candidates", []),
                "threshold": result.get("threshold", 0.4),
            }
        )

    @app.post("/api/vision/upload-face")
    async def api_vision_upload_face(
        file: UploadFile = File(...), name: str = Form(...), context: str = Form("")
    ) -> JSONResponse:
        """Upload a face image file, run recognition, and store embedding.

        Simplified enrollment flow: upload image → recognize → store embedding.
        No webcam required. Works with any face image file.

        Form fields:
        - file: image file (JPEG/PNG)
        - name: person name (required)
        - context: optional context/description
        """
        if not name or not name.strip():
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_upload_face",
                    status="fail",
                    summary="vision upload-face rejected — name missing",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_upload_face emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="name required")

        # Read uploaded file (size-capped)
        image_bytes = await _read_upload_bytes_capped(file, max_bytes=_UPLOAD_MAX_BYTES)
        if not image_bytes:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_upload_face",
                    status="fail",
                    summary="vision upload-face rejected — empty file",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_upload_face emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="empty file")

        blocked = _require_face_runtime(phase="vision_upload_face")
        if blocked is not None:
            return blocked

        # Run face recognition to get embedding
        try:
            from runtime.face.recognition import identify as _face_identify

            ident = _face_identify(image_bytes)
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_upload_face",
                    status="fail",
                    summary="vision upload-face raised — recognition failed",
                    component="hud.server",
                    failure_class="face_identify_raised",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_upload_face emit_phase failed: %s", _e)
            return JSONResponse(
                {"ok": False, "error": f"face recognition failed: {type(exc).__name__}: {exc}"}
            )

        if not isinstance(ident, dict) or ident.get("degraded"):
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_upload_face",
                    status="degraded",
                    summary="vision upload-face degraded — face runtime unavailable",
                    component="hud.server",
                    failure_class="face_runtime_unavailable",
                )
            except Exception as _e:
                log.debug("vision_upload_face emit_phase failed: %s", _e)
            return JSONResponse(
                {"ok": False, "error": "face runtime unavailable - cannot process image"}
            )

        embedding = ident.get("embedding")
        if not (isinstance(embedding, list) and len(embedding) == 512):
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_upload_face",
                    status="fail",
                    summary="vision upload-face failed — no face detected",
                    component="hud.server",
                    failure_class="no_face_detected",
                )
            except Exception as _e:
                log.debug("vision_upload_face emit_phase failed: %s", _e)
            return JSONResponse(
                {"ok": False, "error": "no face detected in image or embedding generation failed"}
            )

        # Convert image to base64 for storage
        import base64 as _b64

        try:
            image_b64 = _b64.b64encode(image_bytes).decode("utf-8")
        except Exception as exc:
            image_b64 = None
            try:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "hud",
                    "vision_upload_b64_failed",
                    status="degraded",
                    ok=False,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_upload_face b64 emit_data failed: %s", _e)

        # Call the existing remember-face logic with the derived embedding
        return await api_vision_remember_face(
            {
                "name": name.strip(),
                "context": context.strip(),
                "embedding": embedding,
                "bbox": ident.get("bbox"),
                "image_b64": image_b64,
            }
        )

    @app.post("/api/vision/remember-face")
    async def api_vision_remember_face(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        name = (payload.get("name") or "").strip()
        context = (payload.get("context") or "").strip()
        embedding = payload.get("embedding")
        bbox = payload.get("bbox")
        image_b64 = payload.get("image_b64") or None
        if not name:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_remember_face",
                    status="fail",
                    summary="remember-face rejected — name missing",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_remember_face emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="name required")
        if (not isinstance(embedding, list) or not embedding) and image_b64:
            blocked = _require_face_runtime(phase="vision_remember_face")
            if blocked is not None:
                return blocked
            import base64 as _b64_derive

            try:
                raw_ident = _b64_derive.b64decode(
                    image_b64.split(",", 1)[-1] if isinstance(image_b64, str) else b"",
                )
                if raw_ident:
                    from runtime.face.recognition import identify as _face_identify_derive

                    ident = _face_identify_derive(raw_ident)
                    emb = ident.get("embedding") if isinstance(ident, dict) else None
                    if isinstance(emb, list) and emb:
                        embedding = emb
                        if bbox is None and ident.get("bbox") is not None:
                            bbox = ident.get("bbox")
            except Exception as _derive_exc:
                log.debug("remember-face embedding derive from image_b64 failed: %s", _derive_exc)
        if isinstance(embedding, list) and embedding and not image_b64:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_remember_face",
                    status="fail",
                    summary="remember-face rejected — raw client embedding",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_remember_face emit_phase failed: %s", _e)
            raise HTTPException(
                status_code=400,
                detail="image_b64 required — server derives embedding from image",
            )
        if not isinstance(embedding, list) or not embedding:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_remember_face",
                    status="fail",
                    summary="remember-face rejected — embedding missing/empty",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_remember_face emit_phase failed: %s", _e)
            raise HTTPException(
                status_code=400,
                detail=(
                    "embedding required (non-empty float list), or supply "
                    "image_b64 so the server can derive it from /face/identify"
                ),
            )
        try:
            embedding = [float(x) for x in embedding]
        except (TypeError, ValueError) as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_remember_face",
                    status="fail",
                    summary="remember-face rejected — embedding values non-numeric",
                    component="hud.server",
                    failure_class="bad_request",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_remember_face emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail=f"embedding values must be numeric: {exc}")
        # Transport admits the complete payload; only pressure-owned AUTO may
        # interpret it or put it in episode SRAM. No canonical sidecar writes.
        from pressure_loop.operator_vision import queue_face_memory

        try:
            result = queue_face_memory(name=name, context=context, embedding=embedding,
                                       bbox=bbox, image_b64=image_b64)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return JSONResponse(
            {**result, "name": name, "vision_coord": [7, 1],
             "face_status": "queued" if result.get("queued") else "rejected"},
            status_code=202 if result.get("queued") else 400,
        )

    @app.get("/api/vision/anchors")
    async def api_vision_anchors() -> JSONResponse:
        """List the names of enrolled operators. Embeddings are NEVER
        exposed via the HUD (they're a biometric — Guardian-protected).
        """
        try:
            from runtime.face.recognition import anchor_thumbnail_bytes, known_names

            names = known_names()
            results = []
            for name in names:
                # P3-S08 cure: route existence check through the same
                # whitelist+resolve-under-root validator as the read
                # endpoint. Any pre-cure anchor whose name now fails
                # the whitelist surfaces as a null thumbnail_url
                # (no surprise — the read endpoint would 404 too).
                try:
                    has_thumb = anchor_thumbnail_bytes(name) is not None
                except ValueError:
                    has_thumb = False
                results.append(
                    {
                        "name": name,
                        "thumbnail_url": f"/api/vision/anchor-thumbnail/{name}"
                        if has_thumb
                        else None,
                    }
                )
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_anchors",
                    status="degraded",
                    summary="vision anchors list failed — enrolled names unavailable",
                    component="hud.server",
                    failure_class="vision_anchors_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_anchors emit_phase failed: %s", _e)
            return JSONResponse(
                {"ok": False, "error_type": type(exc).__name__, "anchors": [], "count": 0}
            )
        return JSONResponse({"ok": True, "names": names, "anchors": results, "count": len(results)})

    @app.get("/api/vision/anchor-thumbnail/{name}")
    async def api_vision_anchor_thumbnail(name: str):
        """Serve the latest canonical face capture without writing a thumbnail."""
        from fastapi.responses import Response
        from runtime.face.recognition import anchor_thumbnail_bytes

        try:
            capture = anchor_thumbnail_bytes(name)
        except ValueError:
            # Single 404 for both "rejected name" and "missing file" —
            # closes the enumeration oracle: an attacker can't tell
            # blocked names apart from non-existent ones.
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_anchor_thumbnail",
                    status="fail",
                    summary="vision anchor-thumbnail rejected — invalid name",
                    component="hud.server",
                    failure_class="bad_request",
                    anchor_name=name,
                )
            except Exception as _e:
                log.debug("vision_anchor_thumbnail emit_phase failed: %s", _e)
            raise HTTPException(status_code=404, detail="thumbnail not found")
        if capture is None:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_anchor_thumbnail",
                    status="fail",
                    summary="vision anchor-thumbnail not found",
                    component="hud.server",
                    failure_class="thumbnail_not_found",
                    anchor_name=name,
                )
            except Exception as _e:
                log.debug("vision_anchor_thumbnail emit_phase failed: %s", _e)
            raise HTTPException(status_code=404, detail="thumbnail not found")
        from io import BytesIO
        from PIL import Image

        with Image.open(BytesIO(capture)) as image:
            media_type = Image.MIME.get(image.format, "application/octet-stream")
        return Response(capture, media_type=media_type)

    @app.post("/api/vision/forget")
    async def api_vision_forget(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Forget a previously-enrolled identity. Operator-initiated
        only — there's no UI button by default; voice command 'Omega
        forget <name>' is the intended invocation."""
        name = payload.get("name") or ""
        if not isinstance(name, str) or not name.strip():
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_forget",
                    status="fail",
                    summary="vision forget rejected — name missing",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("vision_forget emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="name required")
        try:
            from runtime.face.recognition import forget as _face_forget

            forgot = _face_forget(name.strip())
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "vision_forget",
                    status="fail",
                    summary="vision forget raised — identity not removed",
                    component="hud.server",
                    failure_class="vision_forget_raised",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("vision_forget emit_phase failed: %s", _e)
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return JSONResponse({"ok": True, "name": name.strip(), "removed": forgot})

    # =================================================================
    # P3-S03 — Memory Node Graph API (read-only)
    #
    # Consumed by the HUD settings panel's "memory" tab (node-graph
    # visualization, see hud/omega_interface.html OmegaMemoryGraph
    # engine). Three endpoints expose the on-disk Dewey memory tree
    # (memory/<X_lobe>/<Y_subdomain>/<...>.json) for visual inspection:
    #
    #   GET /api/memory/lobes?counts=true
    #     -> { "X,Y": <int count>, ... } for static taxonomy coords,
    #     plus "X,Y:dirname" entries when live/dynamic folders collide
    #     with a legacy Y. Recursive count of *.json files under each
    #     actual subdomain folder.
    #
    #   GET /api/memory/list?lobe=X&subdomain=Y
    #     -> [{ name, path, size, timestamp, strength }] for every
    #     *.json file under memory/<X_lobe>/<Y_subdomain>/. Omit
    #     ``lobe`` for SPA initial poll (returns []). Omit
    #     ``subdomain`` to aggregate all subdomains in one lobe.
    #     ``path`` is relative to the subdomain dir — clients pass
    #     it back to /api/memory/read. ``strength`` is looked up via
    #     core.strength_index (filename-stem as memory_id); falls
    #     back to None if not in the index.
    #
    #   GET /api/memory/read?path=...&lobe=X&subdomain=Y&subdomain_dir=dirname
    #     -> file content as text/plain. Path-traversal guarded:
    #     resolved full path must lie under the resolved subdomain dir.
    #     `subdomain_dir` is preferred when present so dynamic Y
    #     folders and static coords do not collide. Size capped at
    #     _MEMORY_READ_MAX_BYTES so a misbehaving JSON file can't
    #     OOM the browser.
    #
    # Read-only by design — no write/delete surface here. Memory
    # mutations go through gates/ + memory.api as before. This
    # surface exists ONLY for operator-visible inspection through
    # the HUD node graph.
    # =================================================================
    _MEMORY_READ_MAX_BYTES = 256 * 1024  # 256K; engine.js truncates client-side at 2K
    _MEMORY_ROOT = OMEGA_ROOT / "memory"

    def _coerce_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _safe_memory_dir_segment(value: Any) -> str:
        segment = str(value or "").strip()
        if not segment or segment in {".", ".."}:
            return ""
        if "/" in segment or "\\" in segment:
            return ""
        return segment

    def _lobe_memory_dir(lobe_x: int) -> Path | None:
        from memory.dewey_map import LOBE_MAP

        lobe_dir = LOBE_MAP.get(int(lobe_x))
        if not lobe_dir:
            return None
        return _MEMORY_ROOT / lobe_dir

    def _path_inside(base_path: Path, child_path: Path) -> bool:
        try:
            child_path.resolve().relative_to(base_path.resolve())
            return True
        except (OSError, ValueError):
            return False

    def _dynamic_subdomain_index() -> Dict[str, Any]:
        idx_path = OMEGA_ROOT / "guardian" / "state" / "dynamic_y_index.json"
        if not idx_path.exists():
            return {}
        try:
            data = _json.loads(idx_path.read_text(encoding="utf-8"))
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "dynamic_y_index_read_failed",
                ok=False,
                error=str(exc),
            )
            return {}
        return data if isinstance(data, dict) else {}

    def _read_memory_file_meta(path: Path) -> Dict[str, Any]:
        try:
            if path.stat().st_size > _MEMORY_READ_MAX_BYTES:
                return {}
            data = _json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}

    def _memory_meta_coord_y(meta: Dict[str, Any]) -> int | None:
        coord = meta.get("coord")
        if isinstance(coord, (list, tuple)) and len(coord) >= 2:
            return _coerce_int(coord[1], default=-1)
        if isinstance(coord, str):
            parts = coord.split(",")
            if len(parts) >= 2:
                return _coerce_int(parts[1], default=-1)
        return None

    def _infer_subdomain_y_from_files(sub_path: Path) -> int | None:
        try:
            files = sorted(sub_path.rglob("*.json"))
        except OSError:
            return None
        for file_path in files[:50]:
            y = _memory_meta_coord_y(_read_memory_file_meta(file_path))
            if y is not None and y >= 0:
                return y
        return None

    def _subdomain_path_from_segment(
        lobe_x: int,
        subdomain_dir: Any,
    ) -> Path | None:
        lobe_path = _lobe_memory_dir(lobe_x)
        segment = _safe_memory_dir_segment(subdomain_dir)
        if lobe_path is None or not segment:
            return None
        candidate = lobe_path / segment
        if not _path_inside(lobe_path, candidate):
            return None
        return candidate

    def _memory_subdomain_specs_for_lobe(lobe_x: int) -> List[Dict[str, Any]]:
        from memory.dewey_map import LOBE_MAP, SUBDOMAIN_MAP

        lobe_x = int(lobe_x)
        lobe_dir = LOBE_MAP.get(lobe_x)
        if not lobe_dir:
            return []

        specs_by_dir: Dict[str, Dict[str, Any]] = {}

        def add_spec(
            *,
            dirname: Any,
            subdomain_y: Any,
            label: Any = "",
            source: str,
            dynamic: bool = False,
            parent: Any = "",
        ) -> None:
            safe_dir = _safe_memory_dir_segment(dirname)
            if not safe_dir:
                return
            spec = specs_by_dir.get(safe_dir)
            y = _coerce_int(subdomain_y, default=0)
            if spec is None:
                specs_by_dir[safe_dir] = {
                    "dirname": safe_dir,
                    "label": str(label or safe_dir),
                    "subdomain": y,
                    "source": source,
                    "sources": [source],
                    "dynamic": bool(dynamic),
                    "parent": str(parent or ""),
                    "path": _MEMORY_ROOT / lobe_dir / safe_dir,
                }
                return
            if source not in spec["sources"]:
                spec["sources"].append(source)
            spec["dynamic"] = bool(spec.get("dynamic")) or bool(dynamic)
            if dynamic:
                spec["subdomain"] = y
            if label and spec.get("label") == spec.get("dirname"):
                spec["label"] = str(label)
            if parent and not spec.get("parent"):
                spec["parent"] = str(parent)

        for (x, y), subdomain_name in sorted(SUBDOMAIN_MAP.items()):
            if x == lobe_x:
                add_spec(
                    dirname=subdomain_name,
                    subdomain_y=y,
                    label=subdomain_name,
                    source="static",
                )

        dyn_lobe = (_dynamic_subdomain_index().get("lobes") or {}).get(str(lobe_x)) or {}
        dyn_subs = dyn_lobe.get("subdomains") or {}
        if isinstance(dyn_subs, dict):
            for dirname, meta in sorted(dyn_subs.items()):
                meta = meta if isinstance(meta, dict) else {}
                add_spec(
                    dirname=dirname,
                    subdomain_y=meta.get("y", 0),
                    label=meta.get("label") or dirname,
                    source="dynamic_index",
                    dynamic=True,
                    parent=meta.get("parent", ""),
                )

        lobe_path = _MEMORY_ROOT / lobe_dir
        try:
            actual_dirs = (
                sorted(
                    (p for p in lobe_path.iterdir() if p.is_dir()),
                    key=lambda p: p.name.lower(),
                )
                if lobe_path.exists()
                else []
            )
        except OSError:
            actual_dirs = []
        for actual_dir in actual_dirs:
            existing = specs_by_dir.get(actual_dir.name)
            inferred_y = (
                existing.get("subdomain")
                if existing is not None
                else _infer_subdomain_y_from_files(actual_dir)
            )
            add_spec(
                dirname=actual_dir.name,
                subdomain_y=0 if inferred_y is None else inferred_y,
                label=actual_dir.name,
                source="filesystem",
            )

        return sorted(
            specs_by_dir.values(),
            key=lambda spec: (
                _coerce_int(spec.get("subdomain"), default=0),
                str(spec.get("label") or "").lower(),
                str(spec.get("dirname") or "").lower(),
            ),
        )

    def _spec_has_live_subdomain_dir(spec: Dict[str, Any]) -> bool:
        sub_path = spec.get("path")
        if not isinstance(sub_path, Path):
            return False
        try:
            return sub_path.exists() and sub_path.is_dir()
        except OSError:
            return False

    def _memory_graph_subdomain_specs_for_lobe(
        lobe_x: int,
    ) -> List[Dict[str, Any]]:
        """Return only the subdomains the settings graph should render.

        The HUD memory graph is a live view over the on-disk memory tree, so it
        should not pre-populate every static taxonomy bucket. Keep the fixed lobe
        shell, but only show subdomains that either exist on disk already or were
        dynamically allocated by the live memory system.
        """
        visible_specs: List[Dict[str, Any]] = []
        for spec in _memory_subdomain_specs_for_lobe(lobe_x):
            if _spec_has_live_subdomain_dir(spec) or bool(spec.get("dynamic")):
                visible_specs.append(spec)
        return visible_specs

    def _resolve_subdomain_dir(
        lobe_x: int,
        subdomain_y: int,
        subdomain_dir: str = "",
    ):
        """Resolve a memory subdomain. ``subdomain_dir`` is the precise
        folder identity used by the live graph; legacy callers can still
        resolve by (X, Y)."""
        if subdomain_dir:
            return _subdomain_path_from_segment(lobe_x, subdomain_dir)
        for spec in _memory_subdomain_specs_for_lobe(int(lobe_x)):
            if _coerce_int(spec.get("subdomain"), default=-9999) == int(subdomain_y):
                return spec.get("path")
        return None

    # _MEMORY_LOBE_DISPLAY is now defined at module scope (above
    # create_app) so the boot-cascade drift-guard scenario can import
    # it. Closure-scoped definition is preserved as a reference via
    # the module-level constant.

    def _count_memory_files_in_path(sub_path: Path | None) -> int:
        if sub_path is None:
            return 0
        if not sub_path.exists() or not sub_path.is_dir():
            return 0
        try:
            return sum(1 for _ in sub_path.rglob("*.json"))
        except OSError:
            return 0

    def _count_subdomain_files(
        lobe_x: int,
        subdomain_y: int,
        subdomain_dir: str = "",
    ) -> int:
        sub_path = _resolve_subdomain_dir(lobe_x, subdomain_y, subdomain_dir)
        return _count_memory_files_in_path(sub_path)

    def _memory_node_key(*parts: Any) -> str:
        raw = "::".join(str(part) for part in parts)
        digest = hashlib.sha1(raw.encode("utf-8", errors="replace")).hexdigest()[:12]
        safe = "".join(ch if ch.isalnum() else "_" for ch in raw).strip("_")
        return f"{safe[-80:] or 'node'}_{digest}"

    def _memory_file_strength(memory_id: str, si: Any) -> float | None:
        if si is None:
            return None
        try:
            strength = si.get_strength(memory_id)
            return float(strength) if strength > 0.0 else None
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "strength_index_read_failed",
                ok=False,
                error=str(exc),
            )
            return None

    def _memory_node_count(node: Dict[str, Any]) -> int:
        try:
            return int(node.get("count") or 0)
        except (TypeError, ValueError):
            return 0

    def _build_memory_fs_children(
        base_path: Path,
        current_path: Path,
        lobe_x: int,
        subdomain_y: int,
        subdomain_dir: str,
        si: Any,
    ) -> List[Dict[str, Any]]:
        try:
            entries = sorted(
                current_path.iterdir(),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except OSError:
            return []
        children: List[Dict[str, Any]] = []
        for entry in entries:
            try:
                rel = entry.relative_to(base_path).as_posix()
            except ValueError:
                continue
            if entry.is_dir():
                dir_children = _build_memory_fs_children(
                    base_path, entry, lobe_x, subdomain_y, subdomain_dir, si
                )
                count = sum(_memory_node_count(child) for child in dir_children)
                children.append(
                    {
                        "id": (
                            f"dir_{lobe_x}_{subdomain_y}_{_memory_node_key(subdomain_dir, rel)}"
                        ),
                        "label": entry.name,
                        "coord": f"{lobe_x},{subdomain_y}/{rel}",
                        "color": lobe_x,
                        "type": "directory",
                        "node_kind": "directory",
                        "path": rel,
                        "subdomain_dir": subdomain_dir,
                        "children": dir_children,
                        "count": count,
                        "entry_count": count,
                    }
                )
                continue
            if not entry.is_file() or entry.suffix.lower() != ".json":
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            memory_id = entry.stem
            meta = _read_memory_file_meta(entry)
            label = str(meta.get("title") or meta.get("summary") or entry.name)
            meta_y = _memory_meta_coord_y(meta)
            node_y = subdomain_y if meta_y is None else meta_y
            timestamp = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
            node: Dict[str, Any] = {
                "id": (f"mem_{lobe_x}_{subdomain_y}_{_memory_node_key(subdomain_dir, rel)}"),
                "label": label,
                "file_name": entry.name,
                "coord": f"{lobe_x},{node_y}/{rel}",
                "color": lobe_x,
                "type": "memory",
                "node_kind": "file",
                "path": rel,
                "read_path": rel,
                "lobe": lobe_x,
                "subdomain": node_y,
                "subdomain_dir": subdomain_dir,
                "memory_id": memory_id,
                "size": int(stat.st_size),
                "timestamp": timestamp,
                "children": [],
                "count": 1,
                "entry_count": 1,
            }
            strength = _memory_file_strength(memory_id, si)
            if strength is not None:
                node["strength"] = strength
            children.append(node)
        return children

    @app.get("/api/memory/lobes")
    def api_memory_lobes(
        counts: bool = False,
        expand: str = "",
        taxonomy: bool = False,
    ) -> JSONResponse:
        """Memory tree introspection. Query modes:

        (default)         → SPA lobe map ``{lobes: [{id,name,count}, ...]}``
        ?taxonomy=1       → static taxonomy {coord -> {lobe, subdomain}}
        ?counts=true      → flat {"X,Y": <int>} per-subdomain file counts
        ?expand=all       → recursive tree for settings memory graph
        """
        try:
            from memory.dewey_map import SUBDOMAIN_MAP, LOBE_MAP
        except Exception as exc:
            # Fail-CLOSED to 500 so the HUD's `!r.ok` fallback-to-mock
            # branch fires. A 200 + {"ok": false} payload would slip
            # past the JS fetch check and hit buildLayoutFromTree with
            # a malformed tree.
            return JSONResponse(
                {"ok": False, "error": f"dewey_map import: {exc}"},
                status_code=500,
            )

        all_lobes = sorted(
            {
                *LOBE_MAP.keys(),
                *(x for (x, _y) in SUBDOMAIN_MAP.keys()),
            }
        )

        # ?expand=all — tree shape consumed by the HUD memory graph.
        if expand == "all":
            try:
                from memory.strength_index import get_strength_index

                si = get_strength_index()
            except Exception:
                si = None
            lobe_nodes: List[Dict[str, Any]] = []
            for lobe_x in all_lobes:
                display = _MEMORY_LOBE_DISPLAY.get(
                    lobe_x,
                    {
                        "label": LOBE_MAP.get(lobe_x, str(lobe_x)),
                        "coord": str(lobe_x * 100).zfill(3),
                        "type": "scripted",
                    },
                )
                children: List[Dict[str, Any]] = []
                lobe_total = 0
                for spec in _memory_graph_subdomain_specs_for_lobe(lobe_x):
                    y = _coerce_int(spec.get("subdomain"), default=0)
                    subdomain_name = str(spec.get("label") or spec["dirname"])
                    subdomain_dir = str(spec.get("dirname") or subdomain_name)
                    sub_path = spec.get("path")
                    fs_children: List[Dict[str, Any]] = []
                    if sub_path is not None and sub_path.exists() and sub_path.is_dir():
                        fs_children = _build_memory_fs_children(
                            sub_path, sub_path, lobe_x, y, subdomain_dir, si
                        )
                    n = sum(_memory_node_count(child) for child in fs_children)
                    lobe_total += n
                    children.append(
                        {
                            "id": (f"sub_{lobe_x}_{y}_{_memory_node_key(subdomain_dir)}"),
                            "label": subdomain_name,
                            "coord": f"{lobe_x},{y}",
                            "color": lobe_x,
                            "type": display["type"],
                            "node_kind": "subdomain",
                            "lobe": lobe_x,
                            "subdomain": y,
                            "subdomain_dir": subdomain_dir,
                            "dynamic": bool(spec.get("dynamic")),
                            "source": spec.get("source"),
                            "sources": spec.get("sources") or [spec.get("source")],
                            "parent": spec.get("parent") or "",
                            "children": fs_children,
                            "count": n,
                            "entry_count": n,
                        }
                    )
                lobe_nodes.append(
                    {
                        "id": f"lobe_{display['coord']}",
                        "label": display["label"],
                        "coord": display["coord"],
                        "color": lobe_x,
                        "type": display["type"],
                        "node_kind": "lobe",
                        "children": children,
                        "count": lobe_total,
                        "entry_count": lobe_total,
                    }
                )
            tree = {
                "id": "root",
                "label": "Memory Core",
                "coord": "",
                "color": -1,
                "type": "root",
                "node_kind": "root",
                "children": lobe_nodes,
                "count": sum(node["count"] for node in lobe_nodes),
                "entry_count": sum(node["count"] for node in lobe_nodes),
            }
            return JSONResponse(tree)

        # ?counts=true — flat per-subdomain counts (engine.js contract).
        if counts:
            result: Dict[str, int] = {}
            seen_legacy: set[str] = set()
            for lobe_x in all_lobes:
                for spec in _memory_subdomain_specs_for_lobe(lobe_x):
                    y = _coerce_int(spec.get("subdomain"), default=0)
                    dirname = str(spec.get("dirname") or "")
                    count = _count_memory_files_in_path(spec.get("path"))
                    legacy_key = f"{lobe_x},{y}"
                    if legacy_key not in seen_legacy:
                        result[legacy_key] = count
                        seen_legacy.add(legacy_key)
                    elif dirname:
                        result[f"{legacy_key}:{dirname}"] = count
            return JSONResponse(result)

        # Default — SPA memory panel lobe map with per-lobe counts.
        # Legacy taxonomy remains at ``?taxonomy=1``; tree graph uses
        # ``?expand=all``.
        if taxonomy:
            taxonomy_map: Dict[str, Dict[str, Any]] = {}
            seen_legacy: set[str] = set()
            for lobe_x in all_lobes:
                for spec in _memory_subdomain_specs_for_lobe(lobe_x):
                    y = _coerce_int(spec.get("subdomain"), default=0)
                    dirname = str(spec.get("dirname") or "")
                    legacy_key = f"{lobe_x},{y}"
                    key = legacy_key if legacy_key not in seen_legacy else f"{legacy_key}:{dirname}"
                    seen_legacy.add(legacy_key)
                    taxonomy_map[key] = {
                        "lobe": LOBE_MAP.get(lobe_x, ""),
                        "subdomain": str(spec.get("label") or dirname),
                        "subdomain_dir": dirname,
                        "dynamic": bool(spec.get("dynamic")),
                        "sources": spec.get("sources") or [spec.get("source")],
                    }
            return JSONResponse({"ok": True, "taxonomy": taxonomy_map})

        lobes: List[Dict[str, Any]] = []
        lobe_totals: Dict[int, int] = {}
        for lobe_x in all_lobes:
            total = sum(
                _count_memory_files_in_path(spec.get("path"))
                for spec in _memory_subdomain_specs_for_lobe(lobe_x)
            )
            lobe_totals[lobe_x] = total
        for lobe_x in sorted(lobe_totals.keys()):
            display = _MEMORY_LOBE_DISPLAY.get(
                lobe_x,
                {
                    "label": LOBE_MAP.get(lobe_x, str(lobe_x)),
                    "coord": str(lobe_x * 100).zfill(3),
                    "type": "scripted",
                },
            )
            count = int(lobe_totals.get(lobe_x, 0))
            lobes.append(
                {
                    "id": str(lobe_x),
                    "name": display["label"],
                    "label": display["label"],
                    "coord": display.get("coord", str(lobe_x)),
                    "count": count,
                    "size": count,
                }
            )
        return JSONResponse({"ok": True, "lobes": lobes})

    def _list_memory_subdomain_files(
        sub_path: Path,
        *,
        si: Any,
        lobe_x: int | None = None,
        subdomain_y: int | None = None,
        subdomain_dir: str = "",
    ) -> List[Dict[str, Any]]:
        """List *.json files under one subdomain directory."""
        if not sub_path.exists() or not sub_path.is_dir():
            return []
        files: List[Dict[str, Any]] = []
        for p in sorted(sub_path.rglob("*.json")):
            try:
                stat = p.stat()
            except OSError:
                continue
            memory_id = p.stem
            strength = None
            if si is not None:
                try:
                    s = si.get_strength(memory_id)
                    strength = float(s) if s > 0.0 else None
                except Exception:
                    strength = None
            try:
                rel = p.relative_to(sub_path)
            except ValueError:
                rel = p.name
            files.append(
                {
                    "name": p.name,
                    "path": str(rel).replace("\\", "/"),
                    "size": int(stat.st_size),
                    "timestamp": stat.st_mtime,
                    "strength": strength,
                    "lobe": lobe_x,
                    "subdomain": subdomain_y,
                    "subdomain_dir": subdomain_dir,
                }
            )
        return files

    @app.get("/api/memory/list")
    def api_memory_list(
        lobe: str = "",
        subdomain: str = "",
        subdomain_dir: str = "",
    ) -> JSONResponse:
        """List *.json files under memory/<X_lobe>/<Y_subdomain>/.

        SPA contract: React polls without ``lobe`` on first paint and
        expects HTTP 200 + a JSON array (empty when unfiltered).
        When only ``lobe`` is set, aggregate across all subdomains in
        that lobe. With both ``lobe`` and ``subdomain``, list one
        subdomain (legacy contract). Unknown coords still 404.
        """
        lobe_s = (lobe or "").strip()
        subdomain_s = (subdomain or "").strip()
        if not lobe_s:
            return JSONResponse([])

        try:
            lobe_x = int(lobe_s)
        except (TypeError, ValueError):
            return JSONResponse([])

        try:
            from memory.strength_index import get_strength_index

            si = get_strength_index()
        except Exception:
            si = None

        if not subdomain_s:
            files: List[Dict[str, Any]] = []
            try:
                for spec in _memory_subdomain_specs_for_lobe(lobe_x):
                    y = _coerce_int(spec.get("subdomain"), default=0)
                    dirname = str(spec.get("dirname") or "")
                    sub_path = spec.get("path")
                    if sub_path is None:
                        continue
                    files.extend(
                        _list_memory_subdomain_files(
                            sub_path,
                            si=si,
                            lobe_x=lobe_x,
                            subdomain_y=y,
                            subdomain_dir=dirname,
                        )
                    )
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"listing failed: {exc}")
            return JSONResponse(files)

        try:
            subdomain_y = int(subdomain_s)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="lobe and subdomain must be integers",
            )
        sub_path = _resolve_subdomain_dir(lobe_x, subdomain_y, subdomain_dir=subdomain_dir)
        if sub_path is None:
            raise HTTPException(
                status_code=404,
                detail=f"no subdomain at ({lobe_x}, {subdomain_y})",
            )
        try:
            files = _list_memory_subdomain_files(
                sub_path,
                si=si,
                lobe_x=lobe_x,
                subdomain_y=subdomain_y,
                subdomain_dir=(_safe_memory_dir_segment(subdomain_dir) or sub_path.name),
            )
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"listing failed: {exc}")
        return JSONResponse(files)

    @app.get("/api/memory/read")
    def api_memory_read(
        path: str = "",
        lobe: str = "",
        subdomain: str = "",
        subdomain_dir: str = "",
    ):
        """Read a memory file's content. Path-traversal guarded: the
        resolved absolute path MUST sit under memory/<X_lobe>/
        <Y_subdomain>/. Returns content as text/plain (the engine
        prefers text and treats JSON as opaque text).
        """
        if not path:
            raise HTTPException(status_code=400, detail="path required")
        try:
            lobe_x = int(lobe)
            subdomain_y = int(subdomain)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="lobe and subdomain must be integers",
            )
        sub_path = _resolve_subdomain_dir(lobe_x, subdomain_y, subdomain_dir=subdomain_dir)
        if sub_path is None:
            raise HTTPException(
                status_code=404,
                detail=f"no subdomain at ({lobe_x}, {subdomain_y})",
            )
        # Path-traversal guard: take the path as-given (may contain a
        # relative sub-path for nested entity-bearing dirs like
        # person_stub/<person_id>/<id>.json or knowledge clusters), but
        # resolve against the subdomain dir and verify the result is
        # CONTAINED in the subdomain dir. Reject otherwise.
        candidate = (sub_path / path).resolve()
        try:
            candidate.relative_to(sub_path.resolve())
        except ValueError:
            raise HTTPException(
                status_code=403,
                detail="path escapes subdomain root",
            )
        if not candidate.exists() or not candidate.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        try:
            size = candidate.stat().st_size
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"stat failed: {exc}")
        if size > _MEMORY_READ_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(f"file too large ({size} bytes; max {_MEMORY_READ_MAX_BYTES})"),
            )
        try:
            content = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"read failed: {exc}")
        return PlainTextResponse(content)

    # =================================================================
    # P3-S04 — v2 substrate telemetry endpoints (read-only)
    #
    # Per HUD_PILL_TELEMETRY_V2.md §5. Each endpoint exposes a v2
    # substrate primitive the HUD's panels need but didn't have an
    # HTTP surface for yet. All are SYNC handlers (FastAPI dispatches
    # to thread pool — no blocking the event loop, P3-S03 review
    # finding banked).
    # =================================================================

    @app.get("/api/pressure-loop/state")
    def api_pressure_loop_state() -> JSONResponse:
        """Pressure-loop pause primitive state (§5.1 of telemetry spec).
        Polled at 2s by autoloop/dev/deepsleep pills. Returns
        `{paused: bool, owners: [str]}`.
        """
        try:
            from pressure_loop.pressure_loop_pause import pressure_loop_pause_state

            return JSONResponse(pressure_loop_pause_state())
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error_type": type(exc).__name__, "paused": False, "owners": []},
                status_code=500,
            )

    @app.get("/api/broker/snapshot")
    def api_broker_snapshot() -> JSONResponse:
        """All 11 runtime capabilities at once (§5.3). Polled at 5s
        by memory/identity/browser/settings/dev panels for capability
        dots. React SPA expects ``capabilities`` as a JSON array
        (``.slice()`` in the dev sigil); keyed lookup remains under
        ``capabilities_by_name``.
        """
        try:
            from runtime.runtime_capability_broker import (
                runtime_capability_snapshot_dict,
            )

            caps_by_name = runtime_capability_snapshot_dict()
            return JSONResponse(
                {
                    "capabilities": list(caps_by_name.values()),
                    "capabilities_by_name": caps_by_name,
                }
            )
        except Exception as exc:
            return JSONResponse(
                {
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "capabilities": [],
                    "capabilities_by_name": {},
                },
                status_code=500,
            )

    @app.get("/api/presence/state")
    def api_presence_state() -> JSONResponse:
        """Operator presence + watcher liveness (§5.2). Polled at 2s
        by identity pill + new g-presence glyph. Returns
        `{state, enrolled_operator_name, last_heartbeat_iso,
        watcher_stale}`. `state` ∈ {PRESENT, ABSENT, UNKNOWN}; UNKNOWN
        is fail-CLOSED per watcher doctrine.

        P3-S04: depends on `PRESENCE_WATCHER_HEARTBEAT` side-channel
        written by `sensory.operator_presence.run_watcher` each tick
        (added in the same slice as this endpoint).

        Failure semantics: ANY exception (orchestrator-not-yet-bound,
        shared_state read failure, presence_is_stale crash) resolves
        to `state=UNKNOWN` at HTTP 200. Unlike `/api/memory/lobes`
        (which 500s so HUD can fall back to mock), presence state
        has NO meaningful fallback — UNKNOWN is the canonical fail-
        CLOSED return value and the HUD MUST be able to render it
        as a real state. Returning 500 would put the identity pill
        in a "broken endpoint" mode instead of the correct fail-
        CLOSED state.
        """
        global _presence_runtime_absent_logged
        state = "UNKNOWN"
        enrolled = None
        heartbeat_iso = ""
        stale = False
        error_detail = None
        try:
            from sensory.operator_presence import (
                read_presence_from_shared_state,
                enrolled_operator_name,
                presence_is_stale,
            )
            from core.symphony_time import now_wall
            from datetime import datetime, timezone
            from pressure_loop.runtime import try_get_default_runtime

            runtime = try_get_default_runtime()
            if runtime is None:
                if not _presence_runtime_absent_logged:
                    log.debug(
                        "api_presence_state: no PressureRuntime — "
                        "fail-closed UNKNOWN (expected for --no-boot)",
                    )
                    _presence_runtime_absent_logged = True
            else:
                ss = runtime.shared_state
                state = read_presence_from_shared_state(ss) or "UNKNOWN"
                try:
                    enrolled = enrolled_operator_name()
                except Exception as exc:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "hud",
                        "enrolled_operator_name_failed",
                        ok=False,
                        error=str(exc),
                    )
                    enrolled = None
                try:
                    from core.shared_state_keys import (
                        PRESENCE_WATCHER_HEARTBEAT,
                    )

                    heartbeat_iso = ss.read(PRESENCE_WATCHER_HEARTBEAT) or ""
                except Exception as exc:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "hud",
                        "heartbeat_read_failed",
                        ok=False,
                        error=str(exc),
                    )
                    heartbeat_iso = ""
                try:
                    # presence_is_stale signature: keyword-only `now` after
                    # the `*` separator. Positional pass would raise TypeError
                    # (banked P3-S04 review finding — original implementation
                    # passed positionally; outer try/except silently swallowed
                    # it → watcher_stale was always False, defeating the whole
                    # heartbeat side-channel).
                    now_dt = datetime.fromtimestamp(
                        now_wall(),
                        tz=timezone.utc,
                    )
                    stale = presence_is_stale(
                        heartbeat_iso,
                        now=now_dt,
                        max_age_seconds=90,
                    )
                except Exception as exc:
                    from guardian.terminal_feedback import emit_data

                    emit_data(
                        "hud",
                        "presence_staleness_failed",
                        ok=False,
                        error=str(exc),
                    )
                    stale = False
        except Exception as exc:
            # Unexpected import/read failure — fail-CLOSED at JSON.
            # Surface error TYPE only (not str(exc)) to avoid leaking
            # internal paths / config fragments through an unauth'd
            # endpoint (P3-S04 review finding I4).
            log.warning(
                "api_presence_state: presence read failed (%s)",
                exc,
            )
            error_detail = type(exc).__name__
        thumb_url = None
        if enrolled:
            # P3-S08 cure: same resolve-under-root path as the listing
            # endpoint. Pre-cure anchors with names failing the new
            # whitelist surface a null thumbnail_url instead of a
            # link the read endpoint would 404 on.
            from runtime.face.recognition import anchor_thumbnail_bytes

            try:
                if anchor_thumbnail_bytes(enrolled) is not None:
                    thumb_url = f"/api/vision/anchor-thumbnail/{enrolled}"
            except ValueError:
                thumb_url = None

        payload = {
            "state": state,
            "enrolled_operator_name": enrolled,
            "enrolled_operator_thumbnail_url": thumb_url,
            "last_heartbeat_iso": heartbeat_iso,
            "watcher_stale": stale,
        }
        if error_detail:
            payload["error"] = error_detail
        return JSONResponse(payload)

    @app.get("/api/runtime/health")
    def api_runtime_health() -> JSONResponse:
        """Raw runtime health body for the settings panel (§5.6).
        Polled at 10s. Returns the Omega local-services
        full `/health` payload (capabilities + models_loaded +
        engine name + face contract). Runtime-down responses stay HTTP
        200 so the SPA can render offline state without browser error
        noise during HUD-only visual QA.
        """
        try:
            from runtime.client import get_runtime_client

            body = get_runtime_client().health_body() or {}
            if not body:
                return JSONResponse(
                    {"ok": False, "status": "down", "error": "runtime health body empty"},
                )
            return JSONResponse(body)
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "status": "down", "error": f"{type(exc).__name__}: {exc}"},
            )

    @app.get("/api/cuedesk/last_tick")
    def api_cuedesk_last_tick() -> JSONResponse:
        """Snapshot of the last pressure-tick result (§5.4 legacy route).

        Polled at 2s by autoloop + brain panels to render
        'what just happened' (suppress_reason / opened_by /
        pause_owners). Reads the v2 pressure-loop audit ring +
        shared_state tick counter (CueDesk is deleted; pressure loop
        is the canonical tick source).

        Fail-CLOSED-at-JSON pattern (matches /api/presence/state):
        ALWAYS returns 200, even when PressureRuntime isn't bound yet
        or no tick has fired. The HUD must be able to render a
        "no tick yet" empty state without seeing an HTTP error.
        """
        snapshot = None
        error_type = None
        try:
            snapshot = telemetry.last_pressure_tick_snapshot()
        except Exception as exc:
            log.warning(
                "api_cuedesk_last_tick: read failed (%s)",
                exc,
            )
            error_type = type(exc).__name__
        payload: Dict[str, Any] = {"tick": snapshot}
        if error_type:
            payload["error_type"] = error_type
        return JSONResponse(payload)

    @app.get("/api/response/state")
    def api_response_state() -> JSONResponse:
        """Current asynchronous response-rail health and backlog snapshot."""
        try:
            from pressure_loop.operator_chat_responses import list_pending as operator_pending
            from pressure_loop.proactive_chat_messages import list_pending as proactive_pending

            operator_count = len(operator_pending())
            proactive_count = len(proactive_pending())
            return JSONResponse(
                {
                    "ok": True,
                    "degraded": False,
                    "rail": "event_driven",
                    "gate": "ready",
                    "pending_operator_responses": operator_count,
                    "pending_proactive_responses": proactive_count,
                    "pending_total": operator_count + proactive_count,
                }
            )
        except Exception as exc:
            log.warning("api_response_state: rail read failed (%s)", exc)
            return JSONResponse(
                {
                    "ok": False,
                    "degraded": True,
                    "rail": "event_driven",
                    "gate": "unavailable",
                    "error_type": type(exc).__name__,
                }
            )

    @app.get("/api/global-operator-outreach/active")
    def api_global_operator_outreach_active() -> JSONResponse:
        """The one pending system query (text app), if any.

        Legacy outreach shape for HUD polls; canonical delivery is the
        system-messages badge + panel (``pressure_loop.system_messages``).
        """
        try:
            from pressure_loop.system_messages import current_outreach

            record = current_outreach()
        except Exception as exc:
            return JSONResponse(
                {
                    "active": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                status_code=500,
            )
        if not record:
            return JSONResponse({"active": False})
        question_text = str(record.get("question_text") or "").strip()
        return JSONResponse(
            {
                "active": bool(question_text),
                "outreach_id": str(record.get("outreach_id") or ""),
                "question_id": str(record.get("question_id") or ""),
                "question_text": question_text,
                "outreach_status": str(record.get("outreach_status") or ""),
                "act": str(record.get("act") or ""),
                "status": str(record.get("status") or ""),
            }
        )

    # =================================================================
    # TTS-1: Runtime voice — /api/tts/speak (text -> WAV) and /api/tts/stream
    # =================================================================
    @app.post("/api/tts/speak")
    async def api_tts_speak(payload: Dict[str, Any] = Body(...)):
        """Synthesize text via the configured runtime TTS voice.

        Returns a WAV blob the frontend plays
        through HTML Audio — bypassing Web Speech API entirely so we
        don't double up with Windows SAPI voices.

        Body: {"text": "<utterance>"}
        Response: audio/wav with the full RIFF header.
        """
        text = payload.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "tts_speak",
                    status="fail",
                    summary="tts speak rejected — text missing",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("tts_speak emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="text required")
        try:
            from runtime.tts import synthesize, is_available

            if not await run_in_threadpool(is_available):
                try:
                    from guardian.terminal_feedback import emit_phase

                    emit_phase(
                        "hud",
                        "tts_speak",
                        status="degraded",
                        summary="tts speak unavailable — TTS runtime not ready",
                        component="hud.server",
                        failure_class="tts_model_missing",
                    )
                except Exception as _e:
                    log.debug("tts_speak emit_phase failed: %s", _e)
                raise HTTPException(status_code=503, detail="TTS runtime unavailable")
            voice_cfg = hud_settings.load_voice_settings().get("settings", {})
            tts_voice = str(payload.get("voice") or voice_cfg.get("tts_voice") or "Aiden")
            try:
                raw_speed = payload.get("speed")
                if raw_speed is None and payload.get("length_scale") is not None:
                    try:
                        raw_speed = 1.0 / float(payload.get("length_scale"))
                    except (TypeError, ValueError, ZeroDivisionError):
                        raw_speed = 1.0
                if raw_speed is None:
                    raw_speed = payload.get("rate")
                if raw_speed is None:
                    raw_speed = voice_cfg.get("tts_speed", 1.0)
                tts_speed = float(raw_speed)
            except (TypeError, ValueError):
                tts_speed = 1.0
            if tts_speed <= 0:
                tts_speed = 1.0
            try:
                tts_pitch = float(
                    payload.get("pitch")
                    if payload.get("pitch") is not None
                    else voice_cfg.get("tts_pitch", 1.0)
                )
            except (TypeError, ValueError):
                tts_pitch = 1.0
            if tts_pitch <= 0:
                tts_pitch = 1.0

            # The service retains the `length_scale` field name and passes its
            # value to the speech engine as `speed`. Pitch shifting then changes
            # playback speed by `tts_pitch`, so compensate with
            # engine speed = requested speed / pitch.
            length_scale = tts_speed / tts_pitch

            wav = await run_in_threadpool(
                synthesize,
                text.strip(),
                voice=tts_voice,
                length_scale=length_scale,
                pitch=tts_pitch,
            )
        except HTTPException:
            raise
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "tts_speak",
                    status="fail",
                    summary="tts synthesis raised — speech not produced",
                    component="hud.server",
                    failure_class="tts_synthesis_raised",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("tts_speak emit_phase failed: %s", _e)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
        if not wav:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "tts_speak",
                    status="fail",
                    summary="tts synthesis produced empty audio",
                    component="hud.server",
                    failure_class="tts_empty_synthesis",
                )
            except Exception as _e:
                log.debug("tts_speak emit_phase failed: %s", _e)
            raise HTTPException(status_code=500, detail="empty synthesis")
        return Response(content=wav, media_type="audio/wav", headers={"Cache-Control": "no-store"})

    @app.post("/api/tts/stream")
    async def api_tts_stream(payload: Dict[str, Any] = Body(...)):
        """Stream raw s16le 24 kHz mono PCM from the runtime TTS lane.

        This is the low-latency companion to ``/api/tts/speak``.  The existing
        WAV route remains the compatibility path for HTML Audio; hands-free
        clients can consume this endpoint incrementally and cancel by closing
        the response.  Pitch is intentionally not applied here because a
        streaming PCM response has no safe whole-buffer pitch transform.
        """
        text = payload.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text required")
        try:
            from runtime.tts import is_available, synthesize_stream

            if not await run_in_threadpool(is_available):
                raise HTTPException(status_code=503, detail="tts runtime unavailable")
            voice_cfg = hud_settings.load_voice_settings().get("settings", {})
            tts_voice = str(payload.get("voice") or voice_cfg.get("tts_voice") or "Aiden")
            try:
                raw_speed = payload.get("speed")
                if raw_speed is None and payload.get("length_scale") is not None:
                    try:
                        raw_speed = 1.0 / float(payload.get("length_scale"))
                    except (TypeError, ValueError, ZeroDivisionError):
                        raw_speed = 1.0
                if raw_speed is None:
                    raw_speed = payload.get("rate")
                if raw_speed is None:
                    raw_speed = voice_cfg.get("tts_speed", 1.0)
                tts_speed = float(raw_speed)
            except (TypeError, ValueError):
                tts_speed = 1.0
            if tts_speed <= 0:
                tts_speed = 1.0
            stream = await run_in_threadpool(
                synthesize_stream,
                text.strip(),
                voice=tts_voice,
                length_scale=tts_speed,
            )
        except HTTPException:
            raise
        except Exception as exc:
            log.warning("tts stream setup failed: %s", exc, exc_info=True)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}") from exc

        from runtime.tts_stream import PCMResponse

        return PCMResponse(
            stream,
            headers={
                "Cache-Control": "no-store",
                "X-Omega-TTS-Sample-Rate": "24000",
                "X-Omega-TTS-Channels": "1",
                "X-Omega-TTS-Sample-Width": "2",
                "X-Omega-TTS-Pitch-Applied": "false",
            },
        )

    @app.get("/api/tts/status")
    async def api_tts_status() -> JSONResponse:
        """Diagnostic — is the portable TTS service reachable?"""
        try:
            from runtime.tts import status as _tts_status

            return JSONResponse(await run_in_threadpool(_tts_status))
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "tts_status",
                    status="degraded",
                    summary="tts status probe failed — voice model status unavailable",
                    component="hud.server",
                    failure_class="tts_status_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("tts_status emit_phase failed: %s", _e)
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    @app.post("/api/tts/warmup")
    async def api_tts_warmup() -> JSONResponse:
        """Eager-load the selected TTS engine before boot narration."""
        try:
            import asyncio
            from runtime.tts import warmup as _tts_warmup

            ok = await asyncio.to_thread(_tts_warmup)
            return JSONResponse({"ok": bool(ok)})
        except Exception as exc:
            return JSONResponse(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                status_code=500,
            )

    # =================================================================
    # STT-1: Parakeet voice input — /api/stt/transcribe (audio -> text)
    # =================================================================
    @app.get("/api/stt/status")
    async def api_stt_status() -> JSONResponse:
        """Diagnostic — is the portable Parakeet service reachable?"""
        try:
            from runtime.stt import status as _stt_status

            return JSONResponse(_stt_status())
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "stt_status",
                    status="degraded",
                    summary="stt status probe failed — Parakeet service status unavailable",
                    component="hud.server",
                    failure_class="stt_status_failed",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("stt_status emit_phase failed: %s", _e)
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    @app.post("/api/stt/transcribe")
    async def api_stt_transcribe(
        audio: UploadFile = File(...),
        language: str = Form("auto"),
        task: Optional[str] = Form(None),
    ) -> JSONResponse:
        """Transcribe one complete browser audio blob via Whisper.

        The frontend stops and recreates MediaRecorder for each window so
        every upload is a full container, not an undecodable continuation
        fragment.
        """
        data = await audio.read(_STT_MAX_BYTES + 1)
        if not data:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "stt_transcribe",
                    status="fail",
                    summary="stt transcribe rejected — audio missing",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("stt_transcribe emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="audio required")
        if len(data) > _STT_MAX_BYTES:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "stt_transcribe",
                    status="fail",
                    summary="stt transcribe rejected — audio too large",
                    component="hud.server",
                    failure_class="payload_too_large",
                )
            except Exception as _e:
                log.debug("stt_transcribe emit_phase failed: %s", _e)
            raise HTTPException(status_code=413, detail="audio too large")

        def _transcribe_blob_sync():
            from runtime.stt import (
                infer_audio_format,
                is_available as _stt_available,
                transcribe_with_segments,
            )

            if not _stt_available():
                try:
                    from guardian.terminal_feedback import emit_phase

                    emit_phase(
                        "hud",
                        "stt_transcribe",
                        status="degraded",
                        summary="stt transcribe unavailable — whisper checkpoint missing",
                        component="hud.server",
                        failure_class="stt_model_missing",
                    )
                except Exception as _e:
                    log.debug("stt_transcribe emit_phase failed: %s", _e)
                raise HTTPException(status_code=503, detail="whisper checkpoint missing")
            suffix = Path(audio.filename or "audio.webm").suffix or ".webm"
            if len(suffix) > 12 or any(ch in suffix for ch in "\\/:"):
                suffix = ".webm"
            tmp_path = None
            try:
                tmp_path = _temporary_upload_path(suffix)
                tmp_path.write_bytes(data)
                audio_format = infer_audio_format(tmp_path)
                result = transcribe_with_segments(
                    tmp_path,
                    language=language or "auto",
                    audio_format=audio_format,
                    task=task,
                )
                return result
            finally:
                if tmp_path is not None:
                    try:
                        tmp_path.unlink(missing_ok=True)
                    except Exception:
                        pass

        try:
            result = await run_in_threadpool(_transcribe_blob_sync)
        except HTTPException:
            raise
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "stt_transcribe",
                    status="fail",
                    summary="stt transcription raised — audio not transcribed",
                    component="hud.server",
                    failure_class="stt_transcribe_raised",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("stt_transcribe emit_phase failed: %s", _e)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
        segments_out = []
        for seg in result.segments:
            if isinstance(seg, dict):
                segments_out.append(
                    {
                        "text": seg.get("text", ""),
                        "start": seg.get("start", 0.0),
                        "end": seg.get("end", 0.0),
                    }
                )
            else:
                segments_out.append(
                    {
                        "text": getattr(seg, "text", ""),
                        "start": getattr(seg, "start", 0.0),
                        "end": getattr(seg, "end", 0.0),
                    }
                )
        return JSONResponse(
            {
                "ok": True,
                "text": result.text,
                "segments": segments_out,
                "detected_language": result.detected_language,
                "language_probability": result.language_probability,
                "duration": result.duration,
            }
        )

    # =================================================================
    # WIRE-6: Operator-driven tool synthesis request
    # =================================================================
    @app.post("/api/omega/request_tool")
    async def api_omega_request_tool(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Operator-initiated tool synthesis request.

        Body: {"description": "natural-language tool description"}
        Effect: creates a CuriosityEngineEntry with recommended_path=
            tool_creation + trigger_source="operator". Omega Phase 4
            drains eligible entries through synthesize_from_curiosity
            after health and dedup gates pass.
        """
        desc = (payload.get("description") or "").strip()
        if not desc or len(desc) < 4:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "request_tool",
                    status="fail",
                    summary="request_tool rejected — description missing/too short",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("request_tool emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="description required (min 4 chars)")
        try:
            from schema.curiosity_engine import CuriosityEngineEntry
            from hud import curiosity_store
            from datetime import datetime, timezone
            import uuid as _uuid
            from guardian.terminal_feedback import emit_phase

            cur = CuriosityEngineEntry(
                cycle_id="operator-request",
                curiosity_id=f"cur-op-{_uuid.uuid4().hex[:8]}",
                created_at=datetime.now(timezone.utc).isoformat(),
                trigger_source="operator",
                gap_type="capability",
                problem_statement=desc[:300],
                tension_score=0.7,
                resolution_value=0.8,
                urgency_score=0.7,
                persistence_score=0.6,
                resource_cost_estimate=0.5,
                recommended_path="tool_creation",
                should_pursue=True,
                status="open",
                requires_operator_input=False,
            )
            path = curiosity_store.persist(cur)
            emit_phase(
                "hud",
                "request_tool",
                status="ok",
                summary=f"operator requested tool: {desc[:80]!r}",
                curiosity_id=cur.curiosity_id,
                description=desc[:300],
            )
        except Exception as exc:
            from guardian.terminal_feedback import emit_phase as _emit

            _emit(
                "hud",
                "request_tool",
                status="fail",
                summary=f"persist failed: {type(exc).__name__}",
                error_kind=type(exc).__name__,
                error=str(exc),
            )
            return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        return JSONResponse(
            {
                "ok": True,
                "curiosity_id": cur.curiosity_id,
                "description": desc[:300],
                "next": "Omega Phase 4 dispatches eligible tool_creation "
                "curiosities through synthesize_from_curiosity "
                "after health and dedup gates pass",
                "path": str(path),
            }
        )

    # =================================================================
    # DOC-3: 9-principle Tool-Synthesis Doctrine audit
    # =================================================================
    # Audit walks live system state (sandbox bomb, chain inspection,
    # registry scan, etc.) so it's expensive — cache for 60s. The HUD
    # polls every few seconds; we don't want a sandbox process kicked
    # off on every poll.
    _audit_cache: Dict[str, Any] = {"ts": 0.0, "report": None}

    @app.get("/api/omega/doctrine_audit")
    async def api_omega_doctrine_audit(force: bool = False) -> JSONResponse:
        """Return the latest 9-principle doctrine compliance report.

        Query: ?force=true bypasses cache (operator drilldown).
        """
        import time as _time

        now = _time.time()
        cached = _audit_cache.get("report")
        ttl_ok = (now - float(_audit_cache.get("ts") or 0.0)) < 60.0
        if cached is not None and ttl_ok and not force:
            return JSONResponse({"cached": True, **cached})
        # 2026-05-14: core.doctrine_audit retired alongside the 9-point
        # Tool Synthesis Doctrine in the cognitive-tool-layer rebuild.
        # Endpoint preserved as a stub so HUD calls don't 500; will be
        # rebuilt when the new doctrine surface lands.
        return JSONResponse(
            {
                "ok": True,
                "retired": True,
                "reason": "doctrine_audit retired with cognitive-tool-layer rebuild",
                "cached": False,
            }
        )

    # =================================================================
    # IMG-2: Serve generated images from guardian/state/images/
    # =================================================================
    @app.get("/api/images/{date}/{name}")
    async def api_images_get(date: str, name: str):
        """Serve a generated image. Restricted to guardian/state/images/.
        Path-traversal attempts (../, absolute paths) rejected."""
        # Defensive: only YYYY-MM-DD-style date dirs + plain filename
        import re as _re

        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "images_get",
                    status="fail",
                    summary="images get rejected — invalid date segment",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("images_get emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="invalid date")
        if "/" in name or "\\" in name or ".." in name:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "images_get",
                    status="fail",
                    summary="images get rejected — path-traversal pattern in name",
                    component="hud.server",
                    failure_class="path_traversal_attempt",
                )
            except Exception as _e:
                log.debug("images_get emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="invalid name")
        path = _IMAGE_ROOT / date / name
        try:
            resolved = path.resolve()
            if _IMAGE_ROOT.resolve() not in resolved.parents:
                try:
                    from guardian.terminal_feedback import emit_phase

                    emit_phase(
                        "hud",
                        "images_get",
                        status="fail",
                        summary="images get rejected — resolved path escapes images root",
                        component="hud.server",
                        failure_class="path_traversal_attempt",
                    )
                except Exception as _e:
                    log.debug("images_get emit_phase failed: %s", _e)
                raise HTTPException(status_code=400, detail="path escapes images root")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found")
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "images_get",
                ok=True,
                date=date,
                name=name,
            )
        except Exception as _e:
            log.debug("images_get emit_data failed: %s", _e)
        return FileResponse(str(path), media_type="image/png")

    @app.get("/api/images")
    async def api_images_list(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
        """List recent generated image artifacts (newest first).

        Each item carries a browser URL; raw filesystem paths are not leaked to
        the client. The HUD Settings surface does not expose an image library.
        """
        from runtime.image_store import list_generated_images

        items = list_generated_images(limit=limit)
        for it in items:
            it["url"] = f"/api/images/{it['date']}/{it['name']}"
            it.pop("path", None)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "images_list",
                ok=True,
                count=len(items),
                limit=limit,
            )
        except Exception as _e:
            log.debug("images_list emit_data failed: %s", _e)
        return JSONResponse({"ok": True, "count": len(items), "images": items})

    @app.get("/api/documents/{date}/{name}")
    async def api_documents_get(date: str, name: str):
        """Serve a generated document with Content-Disposition: attachment."""
        import re as _re

        if not _re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "documents_get",
                    status="fail",
                    summary="documents get rejected — invalid date segment",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("documents_get emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="invalid date")
        if "/" in name or "\\" in name or ".." in name:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "documents_get",
                    status="fail",
                    summary="documents get rejected — path-traversal pattern in name",
                    component="hud.server",
                    failure_class="path_traversal_attempt",
                )
            except Exception as _e:
                log.debug("documents_get emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="invalid name")
        path = _DOCUMENT_ROOT / date / name
        try:
            resolved = path.resolve()
            if _DOCUMENT_ROOT.resolve() not in resolved.parents:
                try:
                    from guardian.terminal_feedback import emit_phase

                    emit_phase(
                        "hud",
                        "documents_get",
                        status="fail",
                        summary="documents get rejected — resolved path escapes documents root",
                        component="hud.server",
                        failure_class="path_traversal_attempt",
                    )
                except Exception as _e:
                    log.debug("documents_get emit_phase failed: %s", _e)
                raise HTTPException(status_code=400, detail="path escapes documents root")
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="not found")
        if not path.exists() or not path.is_file():
            raise HTTPException(status_code=404, detail="not found")
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "documents_get",
                ok=True,
                date=date,
                name=name,
            )
        except Exception as _e:
            log.debug("documents_get emit_data failed: %s", _e)
        return FileResponse(
            str(path),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{name}"'},
        )

    @app.get("/api/documents")
    async def api_documents_list(limit: int = Query(default=50, ge=1, le=500)) -> JSONResponse:
        """List recent generated documents for Settings -> Created Documents."""
        from runtime.document_store import list_documents

        items = list_documents(limit=limit)
        for it in items:
            it["url"] = f"/api/documents/{it['date']}/{it['name']}"
            it.pop("path", None)
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "documents_list",
                ok=True,
                count=len(items),
                limit=limit,
            )
        except Exception as _e:
            log.debug("documents_list emit_data failed: %s", _e)
        return JSONResponse({"ok": True, "count": len(items), "documents": items})

    # =================================================================
    # UPL-1: Upload endpoints (multipart docs + images)
    # =================================================================
    @app.post("/api/upload")
    async def api_upload(files: List[UploadFile] = File(...)) -> JSONResponse:
        """Accept one or more files via multipart/form-data. Each file
        is classified (image/doc/other), stored under
        guardian/state/staging/uploads/<kind>/<ts>_<name>, and returned
        in a manifest the frontend renders inline.
        """
        if not files:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "upload",
                    status="fail",
                    summary="upload rejected — no files in request",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("upload emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="no files")
        results: list[dict] = []
        for f in files:
            kind = _classify_upload(f.filename or "upload.bin")
            target_dir = _UPLOAD_ROOT / kind
            target_dir.mkdir(parents=True, exist_ok=True)
            stored_name = _safe_upload_name(f.filename or "upload.bin")
            target_path = target_dir / stored_name
            # Stream to disk in chunks; abort if size exceeds cap.
            written = 0
            try:
                with target_path.open("xb") as out:
                    while True:
                        chunk = await f.read(64 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > _UPLOAD_MAX_BYTES:
                            out.close()
                            target_path.unlink(missing_ok=True)
                            try:
                                from guardian.terminal_feedback import emit_phase

                                emit_phase(
                                    "hud",
                                    "upload",
                                    status="fail",
                                    summary="upload aborted — file exceeds size cap",
                                    component="hud.server",
                                    failure_class="upload_size_cap_exceeded",
                                    upload_kind=kind,
                                )
                            except Exception as _e:
                                log.debug("upload emit_phase failed: %s", _e)
                            results.append(
                                {
                                    "ok": False,
                                    "filename": f.filename,
                                    "error": f"exceeds {_UPLOAD_MAX_BYTES} bytes",
                                }
                            )
                            written = -1
                            break
                        out.write(chunk)
            except Exception as exc:
                target_path.unlink(missing_ok=True)
                try:
                    from guardian.terminal_feedback import emit_phase

                    emit_phase(
                        "hud",
                        "upload",
                        status="fail",
                        summary="upload failed — write to disk raised",
                        component="hud.server",
                        failure_class="upload_write_failed",
                        upload_kind=kind,
                        error_type=type(exc).__name__,
                        error=str(exc)[:120],
                    )
                except Exception as _e:
                    log.debug("upload emit_phase failed: %s", _e)
                results.append(
                    {"ok": False, "filename": f.filename, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue
            if written < 0:
                continue
            results.append(
                {
                    "ok": True,
                    "filename": f.filename,
                    "stored_as": stored_name,
                    "kind": kind,
                    "size": written,
                    "url": f"/api/uploads/{kind}/{stored_name}",
                }
            )
        return JSONResponse({"ok": all(r.get("ok") for r in results), "files": results})

    @app.get("/api/uploads")
    async def api_uploads_list() -> JSONResponse:
        """List all staged uploads with metadata. Frontend uses this
        to populate the upload tab without re-walking the disk."""
        items: list[dict] = []
        if _UPLOAD_ROOT.exists():
            for kind_dir in sorted(_UPLOAD_ROOT.iterdir()):
                if not kind_dir.is_dir():
                    continue
                for fp in sorted(kind_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                    if not fp.is_file():
                        continue
                    items.append(
                        {
                            "kind": kind_dir.name,
                            "stored_as": fp.name,
                            "size": fp.stat().st_size,
                            "mtime": fp.stat().st_mtime,
                            "url": f"/api/uploads/{kind_dir.name}/{fp.name}",
                        }
                    )
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "uploads_list",
                ok=True,
                count=len(items),
            )
        except Exception as _e:
            log.debug("uploads_list emit_data failed: %s", _e)
        return JSONResponse({"ok": True, "count": len(items), "files": items})

    @app.get("/api/uploads/{kind}/{name}")
    async def api_uploads_get(kind: str, name: str):
        """Serve a stored upload. Restricted to the staging tree —
        rejects path-traversal attempts."""
        if kind not in {"image", "doc", "other"}:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "uploads_get",
                    status="fail",
                    summary="uploads get rejected — unknown kind",
                    component="hud.server",
                    failure_class="unknown_kind",
                    upload_kind=kind,
                )
            except Exception as _e:
                log.debug("uploads_get emit_phase failed: %s", _e)
            raise HTTPException(status_code=404, detail="unknown kind")
        # Defensive: refuse anything that's not a plain filename.
        if "/" in name or "\\" in name or ".." in name:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "uploads_get",
                    status="fail",
                    summary="uploads get rejected — path-traversal pattern in name",
                    component="hud.server",
                    failure_class="path_traversal_attempt",
                )
            except Exception as _e:
                log.debug("uploads_get emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="invalid name")
        path = _UPLOAD_ROOT / kind / name
        try:
            resolved = path.resolve()
            if _UPLOAD_ROOT.resolve() not in resolved.parents:
                try:
                    from guardian.terminal_feedback import emit_phase

                    emit_phase(
                        "hud",
                        "uploads_get",
                        status="fail",
                        summary="uploads get rejected — resolved path escapes staging",
                        component="hud.server",
                        failure_class="path_traversal_attempt",
                    )
                except Exception as _e:
                    log.debug("uploads_get emit_phase failed: %s", _e)
                raise HTTPException(status_code=400, detail="path escapes staging")
        except FileNotFoundError:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "uploads_get",
                    status="fail",
                    summary="uploads get failed — file not found",
                    component="hud.server",
                    failure_class="not_found",
                    upload_kind=kind,
                    upload_name=name,
                )
            except Exception as _e:
                log.debug("uploads_get emit_phase failed: %s", _e)
            raise HTTPException(status_code=404, detail="not found")
        if not path.exists() or not path.is_file():
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "uploads_get",
                    status="fail",
                    summary="uploads get failed — file not found",
                    component="hud.server",
                    failure_class="not_found",
                    upload_kind=kind,
                    upload_name=name,
                )
            except Exception as _e:
                log.debug("uploads_get emit_phase failed: %s", _e)
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(str(path))

    @app.delete("/api/uploads/{kind}/{name}")
    async def api_uploads_delete(kind: str, name: str) -> JSONResponse:
        """Remove a single staged upload (operator-initiated cleanup)."""
        if kind not in {"image", "doc", "other"}:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "uploads_delete",
                    status="fail",
                    summary="uploads delete rejected — unknown kind",
                    component="hud.server",
                    failure_class="unknown_kind",
                    upload_kind=kind,
                )
            except Exception as _e:
                log.debug("uploads_delete emit_phase failed: %s", _e)
            raise HTTPException(status_code=404, detail="unknown kind")
        if "/" in name or "\\" in name or ".." in name:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "uploads_delete",
                    status="fail",
                    summary="uploads delete rejected — path-traversal pattern in name",
                    component="hud.server",
                    failure_class="path_traversal_attempt",
                )
            except Exception as _e:
                log.debug("uploads_delete emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="invalid name")
        path = _UPLOAD_ROOT / kind / name
        if path.exists() and path.is_file():
            try:
                path.unlink()
                return JSONResponse({"ok": True, "removed": True})
            except Exception as exc:
                try:
                    from guardian.terminal_feedback import emit_phase

                    emit_phase(
                        "hud",
                        "uploads_delete",
                        status="fail",
                        summary="uploads delete failed — unlink raised",
                        component="hud.server",
                        failure_class="upload_unlink_failed",
                        upload_kind=kind,
                        error_type=type(exc).__name__,
                        error=str(exc)[:120],
                    )
                except Exception as _e:
                    log.debug("uploads_delete emit_phase failed: %s", _e)
                return JSONResponse({"ok": False, "error": f"{type(exc).__name__}: {exc}"})
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "hud",
                "uploads_delete",
                ok=True,
                removed=False,
                upload_kind=kind,
                upload_name=name,
            )
        except Exception as _e:
            log.debug("uploads_delete emit_data failed: %s", _e)
        return JSONResponse({"ok": True, "removed": False})

    # =================================================================
    # Operator chat transport to the shared queue (AGENTS.md §4.0)
    # =================================================================
    @app.websocket("/omega/chat")
    async def ws_operator_chat(websocket: WebSocket) -> None:
        """Duplex chat: the turn goes up, the finished reply comes back down.

        TRANSPORT IS NOT IGNITION (§4.0). This carries a turn TO the gate —
        `handle_operator_chat` enqueues it into the durable line exactly as the
        POST route does — and carries the reply back. It never evaluates
        pressure and never opens an episode. A transport with its own road to
        ignition is a second gate, which is precisely what the retired
        retired SharedState ingress mirror was.

        This replaces polling `/api/chat/responses` on `cycle_id`. That poll is
        why speech could not begin until a reply was fully assembled AND a tick
        happened to catch it — the latency was the seam, not the models. The
        POST route stays for non-socket clients and the durable response rail
        remains the source of truth, so a client that reconnects can still fetch
        an answer it missed.

        Served same-origin, so it needs no port of its own. The client's old
        `ws://localhost:13317/omega/bridge` target never had a server behind it;
        the HUD binds 13310 by default (`OMEGA_HUD_PORT`).

        Live agent activity is carried by this chat response rail.
        """
        from hud import chat_socket

        if not _websocket_source_allowed(websocket):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        loop = asyncio.get_running_loop()
        outbox: asyncio.Queue = asyncio.Queue()
        mine = chat_socket.JobFollowing()

        def _on_reply(payload: Dict[str, Any]) -> None:
            # Runs on the CASCADE thread. Hand off to the loop; never block it.
            routed = mine.route(payload)
            if routed is None:
                return
            loop.call_soon_threadsafe(outbox.put_nowait, routed)

        def _queue_control(payload: Dict[str, Any]) -> None:
            chat_socket.publish_control(
                payload, lambda event: loop.call_soon_threadsafe(outbox.put_nowait, event)
            )

        token = chat_socket.subscribe(_on_reply)

        # Replies this socket wrote and the page has not yet acknowledged. A written frame
        # may never render (the page closed or failed), so only the page's `chat.ack` for
        # that exact cycle marks it delivered; until then the orphan sweep can recover it.
        sent_replies: set[str] = set()

        async def _pump() -> None:
            while True:
                payload = await outbox.get()
                await websocket.send_json(payload)
                if str(payload.get("kind") or "") == "chat.reply":
                    cycle_id = str(payload.get("cycle_id") or "")
                    sent_replies.add(cycle_id)

        async def _receive() -> None:
            while True:
                message = await websocket.receive_json()
                if not isinstance(message, dict):
                    continue
                try:
                    if len(_json.dumps(message, ensure_ascii=False).encode("utf-8")) > _CHAT_WS_MAX_BYTES:
                        _queue_control(
                            {
                                "kind": "chat.rejected",
                                "cycle_id": str(message.get("cycle_id") or ""),
                                "error": "chat message exceeds size limit",
                            }
                        )
                        continue
                except (TypeError, ValueError, UnicodeEncodeError):
                    _queue_control(
                        {
                            "kind": "chat.rejected",
                            "cycle_id": str(message.get("cycle_id") or ""),
                            "error": "chat message is not valid JSON data",
                        }
                    )
                    continue
                kind = str(message.get("kind") or "chat.user_message")
                if kind == "ping":
                    await websocket.send_json({"kind": "pong"})
                    continue
                if kind == "chat.ack":
                    acked = str(message.get("cycle_id") or "")
                    if acked in sent_replies:
                        from pressure_loop.operator_chat_responses import mark_delivered

                        sent_replies.discard(acked)
                        mark_delivered(acked)
                    continue
                if kind == "chat.interrupt":
                    # Voice barge-in belongs to chat's existing response rail.
                    # It is not a new turn and never enters pressure.
                    from pressure_loop.operator_chat_responses import request_interrupt

                    interrupt_cycle = str(message.get("cycle_id") or "")
                    accepted = request_interrupt(interrupt_cycle)
                    _queue_control(
                        {"kind": "chat.interrupted", "cycle_id": interrupt_cycle, "ok": bool(accepted)}
                    )
                    continue
                if kind != "chat.user_message":
                    continue
                text = message.get("text") or message.get("message") or ""
                cycle_id = ""
                chat_kwargs = {}
                try:
                    cycle_id = _chat_cycle_id(message)
                    # Register ownership BEFORE admission. Enqueue wakes the
                    # pressure loop, so AUTO can publish a fast result while
                    # handle_operator_chat is still returning to this task.
                    # Registering afterwards loses that result permanently.
                    mine.add(cycle_id)
                    question_id = _chat_question_id(message)
                    chat_kwargs: Dict[str, Any] = {
                        "text": str(text).strip(),
                        "cycle_id": cycle_id,
                        # Stamped HERE, at the entry boundary, before the
                        # threadpool hop below — every later operation has
                        # variable latency, so this preserves send order.
                        "arrival_seq": reserve_operator_arrival(cycle_id),
                        "arrival_reserved": True,
                    }
                    if question_id is not None:
                        chat_kwargs["question_id"] = question_id
                    for src, dst in (
                        ("intent", "directive_intent"),
                        ("upload_ref", "upload_ref"),
                        ("format", "format"),
                        ("template", "template"),
                    ):
                        value = message.get(src)
                        if value:
                            chat_kwargs[dst] = (
                                str(value).strip().upper()
                                if dst == "directive_intent"
                                else str(value)
                            )
                    upload_refs = _chat_upload_refs(message)
                    if upload_refs:
                        chat_kwargs["upload_refs"] = upload_refs
                    plugin_selector = await run_in_threadpool(
                        lambda: _chat_plugin_selector(
                            app.state.plugin_directory,
                            message,
                        )
                    )
                    plugin_operation = message.get("plugin_operation")
                    if plugin_selector is not None:
                        chat_kwargs["plugin_operation"] = plugin_selector
                    elif plugin_operation is not None:
                        if not isinstance(plugin_operation, dict):
                            raise ValueError("plugin_operation must be an object")
                        chat_kwargs["plugin_operation"] = dict(plugin_operation)
                    from pressure_loop.operator_chat import handle_operator_chat

                    result = await run_in_threadpool(lambda: handle_operator_chat(**chat_kwargs))
                except Exception as exc:
                    mine.discard(cycle_id)
                    _queue_control(
                        {
                            "kind": "chat.rejected",
                            "cycle_id": cycle_id,
                            "error": str(exc) or exc.__class__.__name__,
                        }
                    )
                    continue
                finally:
                    if "arrival_seq" in chat_kwargs:
                        abandon_arrival_reservation(chat_kwargs["arrival_seq"], cycle_id=cycle_id)
                result_cycle_id = str((result or {}).get("cycle_id") or "")
                if result_cycle_id != cycle_id:
                    mine.discard(cycle_id)
                    _queue_control(
                        {
                            "kind": "chat.rejected",
                            "cycle_id": cycle_id,
                            "error": "chat_cycle_mismatch",
                        }
                    )
                    continue
                if (result or {}).get("queued") is False:
                    immediate = {
                        "kind": "chat.reply",
                        "cycle_id": cycle_id,
                        "episode_id": "",
                        "text": str((result or {}).get("message") or ""),
                        "delivery": dict((result or {}).get("delivery") or {}),
                        "route": dict((result or {}).get("route") or {}),
                        "prompt_pack": dict((result or {}).get("prompt_pack") or {}),
                    }
                    if isinstance((result or {}).get("chat_permission"), dict):
                        immediate["chat_permission"] = dict(result["chat_permission"])
                    _queue_control(immediate)
                    mine.discard(cycle_id)
                    continue
                if not (result or {}).get("ok"):
                    mine.discard(cycle_id)
                    _queue_control(
                        {
                            "kind": "chat.rejected",
                            "cycle_id": cycle_id,
                            "error": str((result or {}).get("error") or "chat_refused"),
                        }
                    )
        pump = asyncio.create_task(_pump())
        receiver = asyncio.create_task(_receive())
        try:
            done, _ = await asyncio.wait({pump, receiver}, return_when=asyncio.FIRST_COMPLETED)
            # Prefer the sender's cause if both tasks completed together. A failed
            # send or durable ACK must never leave an open, receive-only socket.
            if pump in done:
                await pump
            await receiver
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            receiver.cancel()
            log.debug("operator chat socket closed on error", exc_info=True)
            try:
                await websocket.close(code=1011)
            except Exception as close_error:
                exc.add_note(f"chat socket close also failed: {close_error!r}")
                log.debug("chat socket error close failed", exc_info=True)
        finally:
            chat_socket.unsubscribe(token)
            pump.cancel()
            receiver.cancel()
            await asyncio.gather(pump, receiver, return_exceptions=True)

    @app.post("/api/chat/barge-in")
    async def api_chat_barge_in(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Stop the SPEECH of ``cycle_id`` — voice barge-in, and nothing else.

        The non-socket twin of the ``chat.interrupt`` frame, and deliberately NOT
        ``/api/chat/interrupt``: that one means "stop the work" (Esc and the stop control,
        operator 2026-09-15). Speaking over Omega must have one consequence whether or not the
        chat socket happened to be open, so the fallback path raises the speech verb only: the
        lane keeps running, the visible work frames keep flowing, and the answer still arrives.
        Body: {"cycle_id": str}.
        """
        from pressure_loop.operator_chat_responses import request_interrupt

        cycle_id = str(payload.get("cycle_id") or "").strip()
        if not cycle_id:
            raise HTTPException(status_code=400, detail="cycle_id required")
        accepted = request_interrupt(cycle_id)
        return JSONResponse({"ok": bool(accepted), "cycle_id": cycle_id, "scope": "speech"})

    @app.post("/api/chat/interrupt")
    async def api_chat_interrupt(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Stop the turn answering ``cycle_id`` (barge-in / stop button).

        The non-socket twin of the ``chat.interrupt`` frame. Idempotent: the
        loop polls the request between passes and tool calls; a turn that has
        already answered simply never reads it. Body: {"cycle_id": str}.
        """
        from pressure_loop.operator_chat import halt_operator_work

        cycle_id = str(payload.get("cycle_id") or "").strip()
        if not cycle_id:
            raise HTTPException(status_code=400, detail="cycle_id required")
        # Also stops the agent jobs behind the turn: a build spans batches, so interrupting
        # one turn left it running (operator 2026-09-15; Esc and the stop control land here).
        halted = halt_operator_work(cycle_id)
        # A stop whose durable record failed is reported as degraded rather than as a plain
        # success: the HUD must be able to tell the operator (T12, review 2026-09-18).
        return JSONResponse({"ok": True, "cycle_id": cycle_id,
                             "stopped_jobs": halted["stopped_jobs"],
                             "stop_recorded": bool(halted.get("stop_recorded", True)),
                             "unresolved_stops": list(halted.get("unresolved_stops") or ())})

    @app.post("/api/agent/halt")
    async def api_agent_halt(payload: Dict[str, Any] = Body(default={})) -> JSONResponse:
        """Halt the build in progress — the Esc key's route (operator 2026-09-15).

        Takes the cycles the HUD is waiting on, if any, and stops the turn in flight plus the
        agent jobs behind it. With no cycle named it still stops every running job: Esc means
        stop, and the operator may press it with no bubble open.
        Body: {"cycle_ids": [str]} (optional).
        """
        from pressure_loop.operator_chat import halt_operator_work

        raw = payload.get("cycle_ids") if isinstance(payload, dict) else None
        cycles = [str(item).strip() for item in raw or () if str(item or "").strip()] or [""]
        stopped: list[str] = []
        unresolved: list[str] = []
        for cycle in cycles:
            halted = halt_operator_work(cycle)
            for job_id in halted["stopped_jobs"]:
                if job_id not in stopped:
                    stopped.append(job_id)
            for turn in halted.get("unresolved_stops") or ():
                if turn not in unresolved:
                    unresolved.append(turn)
        # Esc reports the same degraded outcome as any other stop path (T12).
        return JSONResponse({"ok": True, "cycles": cycles, "stopped_jobs": stopped,
                             "stop_recorded": not unresolved, "unresolved_stops": unresolved})

    # =================================================================
    # HUD-13: Chat composer (POST text -> pressure-led cognition chain)
    # =================================================================
    @app.post("/api/chat")
    async def api_chat(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        """Submit operator text to the current pressure-led cognition chain.

        The backend normalizes text into an operator contributor, opens a
        single episode through pressure evaluation, runs Global A -> Meta A ->
        AUTO -> Meta B -> Global B, then returns AUTO/Global response artifacts.

        Body: {"text": str, "message": str (alias), "face_recognition": dict (optional)}
        Response: response result dict
        """
        text = payload.get("text") or payload.get("message") or ""
        if not isinstance(text, str) or not text.strip():
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "chat",
                    status="fail",
                    summary="chat rejected — text missing",
                    component="hud.server",
                    failure_class="bad_request",
                )
            except Exception as _e:
                log.debug("chat emit_phase failed: %s", _e)
            raise HTTPException(status_code=400, detail="text required")
        # Explicit commands (deterministic dispatch) route through Global A
        # without word-parsing. Optional `weight` lets a caller raise pressure.
        # Thread these only when present so plain chat keeps its simple call.
        intent = payload.get("intent") or payload.get("command")
        weight = payload.get("weight")
        face_recognition = payload.get("face_recognition")
        doc_format = payload.get("format")
        doc_template = payload.get("template")
        upload_ref = payload.get("upload_ref")
        upload_refs = _chat_upload_refs(payload)
        plugin_operation = payload.get("plugin_operation")
        if plugin_operation is not None and not isinstance(plugin_operation, dict):
            raise HTTPException(status_code=422, detail="plugin_operation must be an object")
        try:
            question_id = _chat_question_id(payload)
            cycle_id = _chat_cycle_id(payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Stamp SEND order here, on the event loop, BEFORE the threadpool
        # dispatch below. Everything past this point runs concurrently and does
        # variable-latency work (SourceLedger write, Guardian screen, a
        # classifier call with a 15 s timeout) before it reaches the queue, so
        # a stamp taken any later records completion order rather than arrival
        # order — and the operator's line would silently re-sort itself
        # (AGENTS.md §4.0; audit §1.4).
        chat_kwargs: Dict[str, Any] = {
            "text": text.strip(),
            "cycle_id": cycle_id,
        }
        if question_id is not None:
            chat_kwargs["question_id"] = question_id
        if intent:
            chat_kwargs["directive_intent"] = str(intent).strip().upper()
        if weight is not None:
            if isinstance(weight, bool):
                raise HTTPException(status_code=422, detail="weight must be a finite number")
            try:
                normalized_weight = float(weight)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=422,
                    detail="weight must be a finite number",
                ) from exc
            if not math.isfinite(normalized_weight):
                raise HTTPException(status_code=422, detail="weight must be a finite number")
            chat_kwargs["weight"] = normalized_weight
        if face_recognition and isinstance(face_recognition, dict):
            chat_kwargs["face_recognition"] = face_recognition
        if doc_format:
            chat_kwargs["format"] = str(doc_format).lower()
        if doc_template:
            chat_kwargs["template"] = str(doc_template)
        if upload_ref:
            chat_kwargs["upload_ref"] = str(upload_ref)
        if upload_refs:
            chat_kwargs["upload_refs"] = upload_refs
        if plugin_operation is not None:
            chat_kwargs["plugin_operation"] = dict(plugin_operation)
        chat_kwargs["arrival_seq"] = reserve_operator_arrival(cycle_id)
        chat_kwargs["arrival_reserved"] = True
        try:
            from pressure_loop.operator_chat import handle_operator_chat

            plugin_selector = await run_in_threadpool(
                lambda: _chat_plugin_selector(app.state.plugin_directory, payload)
            )
            if plugin_selector is not None:
                chat_kwargs["plugin_operation"] = plugin_selector
            result = await run_in_threadpool(lambda: handle_operator_chat(**chat_kwargs))
        except PluginDirectoryError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "chat",
                    status="fail",
                    summary="chat failed — cognition chain raised",
                    component="hud.server",
                    failure_class="chat_chain_raised",
                    error_type=type(exc).__name__,
                    error=str(exc)[:120],
                )
            except Exception as _e:
                log.debug("chat emit_phase failed: %s", _e)
            return JSONResponse(
                {"ok": False, "message": "", "error": f"{type(exc).__name__}: {exc}"}
            )
        finally:
            abandon_arrival_reservation(chat_kwargs["arrival_seq"], cycle_id=cycle_id)

        # Rewrite any image artifact path -> /api/images URL so the chat feed
        # can render it inline (raw filesystem paths are useless to JS).
        img = (result or {}).get("image")
        if isinstance(img, dict) and img.get("path"):
            _attach_image_urls(img)

        doc = (result or {}).get("document")
        if isinstance(doc, dict) and doc.get("path"):
            _attach_document_url(doc)

        # Surface runtime fallback explicitly. The current chat path returns
        # `rendered_by="auto_promptpack"` for real AUTO-generated responses;
        # keep this compatibility branch for any caller that still stamps a
        # fallback delivery.
        delivery = (result or {}).get("delivery") or {}
        rendered_by = (delivery.get("rendered_by") or "").lower()
        if rendered_by == "fallback":
            try:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "chat",
                    status="degraded",
                    summary="chat degraded — voice_box unreachable, fallback rendered",
                    component="hud.server",
                    failure_class="voice_box_unreachable",
                    cycle_id=(result or {}).get("cycle_id"),
                )
            except Exception as _e:
                log.debug("chat emit_phase failed: %s", _e)
            return JSONResponse(
                {
                    **result,
                    "ok": False,
                    "error": (
                        "runtime unreachable — Omega local services are not "
                        "serving generation on :11434. Start Omega from the "
                        "portable SSD entrypoint and retry."
                    ),
                }
            )
        chat_ok = bool((result or {}).get("ok"))
        try:
            if chat_ok:
                from guardian.terminal_feedback import emit_data

                emit_data(
                    "hud",
                    "chat",
                    ok=True,
                    episode_id=(result or {}).get("episode_id"),
                    intent=chat_kwargs.get("directive_intent"),
                )
            else:
                from guardian.terminal_feedback import emit_phase

                emit_phase(
                    "hud",
                    "chat",
                    status="fail",
                    summary="chat completed with episode failure",
                    component="hud.server",
                    failure_class="chat_episode_failed",
                    episode_id=(result or {}).get("episode_id"),
                    intent=chat_kwargs.get("directive_intent"),
                    error=str((result or {}).get("error") or "")[:120],
                )
        except Exception as _e:
            log.debug("chat outcome emit failed: %s", _e)
        if chat_ok:
            try:
                hud_chat_history.record_chat_result(
                    result if isinstance(result, dict) else {},
                    operator_text=text.strip(),
                )
            except Exception as _hist_exc:
                log.debug("chat history persist failed: %s", _hist_exc)
        response_status = (result or {}).get("status_code", 200)
        if type(response_status) is not int or not 100 <= response_status <= 599:
            raise RuntimeError("chat result status_code must be an HTTP status integer")
        return JSONResponse(result, status_code=response_status)

    @app.get("/api/chat/history")
    def api_chat_history() -> JSONResponse:
        """Operator chat transcript for the React HUD feed."""
        return JSONResponse(hud_chat_history.list_messages())

    @app.get("/api/chat/responses")
    def api_chat_responses(cycle_id: str = "") -> JSONResponse:
        """Poll for the system's async answer to a QUEUED operator chat turn.

        When the operator sends while an autonomous episode runs, /api/chat
        returns ``queued`` with the originating ``cycle_id``. The HUD polls here
        on that cycle_id; once the next episode answers, the response is returned
        shaped for ``renderChatResult`` and marked delivered (shows once)."""
        from pressure_loop.operator_chat_responses import get_response, mark_delivered

        cid = (cycle_id or "").strip()
        if not cid:
            return JSONResponse({"found": False})
        rec = get_response(cid)
        if rec is None:
            return JSONResponse({"found": False})
        mark_delivered(cid)
        text = str(rec.get("text") or "")
        delivery = dict(rec.get("delivery") or {})
        route = dict(rec.get("route") or {})
        image, document = _browser_artifacts(rec)
        assignment = rec.get("workspace_assignment")
        payload = {
            "found": True,
            "ok": not bool(delivery.get("failed")),
            "cycle_id": cid,
            "message": text,
            "image": image,
            "document": document,
            "delivery": (delivery if delivery else {"text": text, "rendered_by": "omega_async"}),
            "route": (
                route
                if route
                else {"opened_by": "operator", "intent": "RESPOND", "action": "RESPONSE"}
            ),
            "prompt_pack": dict(rec.get("prompt_pack") or {}),
            "thinking": dict(rec.get("thinking") or {}),
        }
        for key in ("pipeline_id", "job_id", "work_kind"):
            if key in rec:
                payload[key] = rec[key]
        if isinstance(assignment, dict) and assignment:
            payload["workspace_assignment"] = dict(assignment)
        lane_work = rec.get("lane_work")
        if isinstance(lane_work, dict):
            payload["lane_work"] = dict(lane_work)
        question = rec.get("operator_question")
        if isinstance(question, dict) and question:
            payload["operator_question"] = dict(question)
        from hud.chat_socket import plugin_permission_hud_payload

        permission = plugin_permission_hud_payload(rec)
        if permission is not None:
            payload["plugin_permission_request"] = permission
        return JSONResponse(payload)

    @app.get("/api/chat/updates")
    def api_chat_lane_updates(cycle_id: str = "", after_seq: int = 0) -> JSONResponse:
        """Read durable specialist updates correlated to an original chat turn."""
        from pressure_loop.operator_chat_responses import get_lane_updates

        cid = (cycle_id or "").strip()
        if not cid:
            return JSONResponse({"found": False})
        record = get_lane_updates(cid, after_seq=after_seq)
        if record is None:
            return JSONResponse({"found": False})
        return JSONResponse({"found": True, **record})

    @app.post("/api/chat/updates/ack")
    async def api_chat_lane_updates_ack(request: Request) -> JSONResponse:
        """Persist the cursor after the HUD has painted correlated lane updates."""
        try:
            body = await request.json()
        except (ValueError, TypeError):
            return JSONResponse({"ok": False, "error": "invalid_lane_update_ack"}, status_code=422)
        if not isinstance(body, dict) or set(body) != {"cycle_id", "through_seq"}:
            return JSONResponse({"ok": False, "error": "invalid_lane_update_ack"}, status_code=422)
        from pressure_loop.operator_chat_responses import acknowledge_lane_updates

        try:
            sequence = acknowledge_lane_updates(
                str(body.get("cycle_id") or ""), through_seq=body.get("through_seq")
            )
        except ValueError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=422)
        return JSONResponse({"ok": True, "acknowledged_seq": sequence})

    @app.get("/api/chat/active-lanes")
    def api_chat_active_lanes() -> JSONResponse:
        """List admitted specialist work so a reloaded HUD can resume update polling."""
        from pressure_loop.operator_chat_responses import list_active_lane_work

        return JSONResponse({"lanes": list_active_lane_work()})

    @app.get("/api/chat/responses/orphans")
    def api_chat_responses_orphans() -> JSONResponse:
        """Sweep rail: undelivered async answers whose bubble poll never claimed them.

        The per-cycle poll depends on an in-browser registration (lost on HUD
        reload / hidden tab / a send path with no thinking block). Anything
        pending past the age floor is returned once and marked delivered, so a
        generated reply ALWAYS folds through to the chat window (operator ruling
        2026-07-19: answers to proactive queries ride the regular line)."""
        from pressure_loop.operator_chat_responses import list_undelivered, mark_delivered

        rows = []
        for rec in list_undelivered(min_age_s=10.0):
            cid = str(rec.get("cycle_id") or "")
            if not cid:
                continue
            mark_delivered(cid)
            text = str(rec.get("text") or "")
            delivery = dict(rec.get("delivery") or {})
            route = dict(rec.get("route") or {})
            image, document = _browser_artifacts(rec)
            payload = {
                    "found": True,
                    "ok": not bool(delivery.get("failed")),
                    "cycle_id": cid,
                    "message": text,
                    "image": image,
                    "document": document,
                    "delivery": delivery
                    if delivery
                    else {"text": text, "rendered_by": "omega_async"},
                    "route": route
                    if route
                    else {"opened_by": "operator", "intent": "RESPOND", "action": "RESPONSE"},
                    "prompt_pack": dict(rec.get("prompt_pack") or {}),
                    "thinking": dict(rec.get("thinking") or {}),
                    **(
                        {"workspace_assignment": dict(rec["workspace_assignment"])}
                        if isinstance(rec.get("workspace_assignment"), dict)
                        and rec.get("workspace_assignment")
                        else {}
                    ),
                }
            for key in ("pipeline_id", "job_id", "work_kind"):
                if key in rec:
                    payload[key] = rec[key]
            question = rec.get("operator_question")
            if isinstance(question, dict) and question:
                payload["operator_question"] = dict(question)
            lane_work = rec.get("lane_work")
            if isinstance(lane_work, dict):
                payload["lane_work"] = dict(lane_work)
            rows.append(payload)
        return JSONResponse({"responses": rows, "count": len(rows)})

    @app.get("/api/chat/proactive")
    def api_chat_proactive() -> JSONResponse:
        """Poll for autonomous PROACTIVE_RESPONSE updates for the side pinboard.

        Returns pending messages only. The HUD marks each delivered via
        ``POST /api/chat/proactive/{message_id}/delivered`` after rendering."""
        from pressure_loop.proactive_chat_messages import list_pending

        pending = list_pending()
        if not pending:
            return JSONResponse({"messages": [], "count": 0})
        messages = []
        for rec in pending:
            msg_id = str(rec.get("id") or "")
            if not msg_id:
                continue
            messages.append(
                {
                    "id": msg_id,
                    "message": str(rec.get("text") or ""),
                    "episode_id": str(rec.get("episode_id") or ""),
                    "opened_by": str(rec.get("opened_by") or ""),
                    "intent": str(rec.get("intent") or "PROACTIVE_RESPONSE"),
                    "ts": rec.get("ts"),
                    "kind": str(rec.get("kind") or "NOTICE"),
                    # The notice's own payload travels with it. Dropped until 2026-09-22, which
                    # silently broke the boot greeting: the producer marked it speak_always and this
                    # projection threw the flag away, so the client saw a plain notice and the voice
                    # pill suppressed it. A format written on one side and parsed on the other needs
                    # to survive the transport between them (AGENTS.md §2.1).
                    "payload": dict(rec.get("payload") or {}),
                    "delivery": {
                        "text": str(rec.get("text") or ""),
                        "rendered_by": "proactive_autonomous",
                    },
                }
            )
        return JSONResponse({"messages": messages, "count": len(messages)})

    @app.post("/api/chat/proactive/{message_id}/delivered")
    def api_chat_proactive_delivered(message_id: str) -> JSONResponse:
        """Ack a proactive notice after its HUD speech request completes.

        Marks the line delivered so the speech path stops re-announcing it. The
        notice remains pinned until the operator clears it and remains durable in
        proactive Settings history afterward. It is deliberately NOT written to
        the main chat_history ring, which would replay it into the conversation.
        """
        from pressure_loop.proactive_chat_messages import mark_delivered

        mid = (message_id or "").strip()
        if not mid:
            raise HTTPException(status_code=400, detail="message_id required")
        rec = mark_delivered(mid)
        if rec is None:
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse({"ok": True, "message": rec})

    @app.get("/api/proactive/notices")
    def api_proactive_notices() -> JSONResponse:
        """Informational notices currently pinned in the right drawer."""
        from pressure_loop.proactive_chat_messages import list_pinned

        messages = []
        for rec in list_pinned():
            message_id = str(rec.get("id") or "")
            if not message_id:
                continue
            text = str(rec.get("text") or "")
            messages.append(
                {
                    "id": message_id,
                    "message": text,
                    "status": str(rec.get("status") or ""),
                    "episode_id": str(rec.get("episode_id") or ""),
                    "opened_by": str(rec.get("opened_by") or ""),
                    "intent": str(rec.get("intent") or "PROACTIVE_RESPONSE"),
                    "ts": rec.get("ts"),
                    # Keep producer flags attached to the notice the pinboard
                    # actually polls. Boot greeting speech options are part of
                    # this transport contract, not metadata for another route.
                    "payload": dict(rec.get("payload") or {}),
                    "delivery": {
                        "text": text,
                        "rendered_by": "proactive_autonomous",
                    },
                }
            )
        return JSONResponse({"messages": messages, "count": len(messages)})

    @app.post("/api/proactive/notices/clear")
    def api_proactive_notices_clear() -> JSONResponse:
        """Unpin informational notices without deleting their history."""
        from pressure_loop.proactive_chat_messages import clear_pinned_notices

        return JSONResponse({"ok": True, "cleared": clear_pinned_notices()})

    @app.get("/api/proactive/history")
    def api_proactive_history() -> JSONResponse:
        """Read-only questions, results, notices, and operator responses."""
        from pressure_loop.proactive_history import build_proactive_history

        entries = build_proactive_history()
        return JSONResponse({"entries": entries, "count": len(entries)})

    # Text app — system messages (badge + panel API)
    # =================================================================
    @app.get("/api/system-messages")
    def api_system_messages_list() -> JSONResponse:
        from pressure_loop.system_messages import list_messages, unread_count

        return JSONResponse({"messages": list_messages(), "unread": unread_count()})

    @app.post("/api/system-messages/{message_id}/respond")
    def api_system_messages_respond(
        message_id: str, payload: Dict[str, Any] = Body(...)
    ) -> JSONResponse:
        text = payload.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            raise HTTPException(status_code=400, detail="text required")
        clean = text.strip()
        # The operator's own verdict on a presented result, when they used the control that states
        # one. Checked here, before anything is written or queued: an unrecognised verdict is
        # refused rather than dropped (memory/operator_feedback.py).
        from memory.operator_feedback import checked_verdict

        try:
            verdict = checked_verdict(payload.get("verdict"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        cycle_id = str(uuid4())
        try:
            from memory.source_ledger.store import record_operator_chat_source

            source_citation = record_operator_chat_source(clean, cycle_id=cycle_id)
        except Exception as exc:
            log.warning("proactive reply SourceLedger capture failed: %s", exc)
            return JSONResponse(
                {"ok": False, "error": "source_ledger_unavailable"},
                status_code=503,
            )
        from guardian.external_ingress import screen_text

        screened, guardian_decision = screen_text(
            clean,
            source="operator_chat",
            field="proactive_response",
        )
        if guardian_decision != "allow":
            return JSONResponse(
                {
                    "ok": False,
                    "error": (
                        "guardian_quarantined"
                        if guardian_decision == "quarantine"
                        else "guardian_withheld"
                    ),
                },
                status_code=403,
            )
        from pressure_loop.system_messages import respond_to_message

        updated = respond_to_message(
            message_id,
            screened,
            cycle_id=cycle_id,
            source_citation=source_citation,
            verdict=verdict,
        )
        if updated is None:
            raise HTTPException(status_code=404, detail="not found")
        return JSONResponse({"ok": True, "message": updated})

    @app.get("/api/idle-learning")
    async def api_idle_learning_get() -> JSONResponse:
        """Durable operator toggle for the idle-learning timer (cockpit pill)."""
        from pressure_loop.idle_wake import operator_idle_learning_enabled

        return JSONResponse({"ok": True, "enabled": operator_idle_learning_enabled()})

    @app.post("/api/idle-learning")
    async def api_idle_learning_set(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
        from pressure_loop.idle_wake import set_operator_idle_learning

        enabled = payload.get("enabled")
        if type(enabled) is not bool:
            raise HTTPException(
                status_code=422,
                detail="enabled must be a boolean",
            )
        result = set_operator_idle_learning(enabled)
        return JSONResponse({"ok": True, **result})

    @app.on_event("startup")
    async def _check_plugin_updates_on_startup() -> None:
        """Let the lifecycle owner perform one bounded due check.

        The HUD owns no timer or polling loop; this hook is the single startup
        handoff and the service itself decides whether work is due.
        """
        service = app.state.plugin_lifecycle
        if service is None:
            return
        try:
            result = await asyncio.to_thread(service.check_updates)
            app.state.plugin_update_status = result.to_dict()
        except Exception as exc:  # noqa: BLE001 - telemetry must not block HUD boot
            log.debug("plugin update startup check unavailable: %s", type(exc).__name__)

    @app.on_event("startup")
    async def _warmup_tts_on_startup() -> None:
        try:
            from runtime.tts import warmup as _tts_warmup

            _schedule_background(
                asyncio.to_thread(_tts_warmup),
                name="hud-tts-warmup",
            )
        except Exception as exc:
            log.debug("tts warmup skipped: %s", exc)

    @app.on_event("startup")
    async def _warm_face_anchors_on_startup() -> None:
        # Cold-boot face recall (vision spec slice 1): rebuild any anchors
        # missing from the store from the vision-lobe sidecars — the reader
        # the sidecar writer always promised. Idempotent; never raises.
        try:
            from core.face_recall_warmup import warm_anchor_store

            _schedule_background(
                asyncio.to_thread(warm_anchor_store),
                name="hud-face-anchor-warmup",
            )
        except Exception as exc:
            log.debug("face recall warmup skipped: %s", exc)

    @app.on_event("shutdown")
    async def _stop_background_tasks() -> None:
        tasks = tuple(app.state.background_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        try:
            await asyncio.to_thread(app.state.plugin_lifecycle.close)
        except Exception:
            log.debug("plugin lifecycle service close skipped", exc_info=True)
        try:
            from runtime.face.lifecycle import release_face_models

            await asyncio.to_thread(release_face_models)
        except Exception:
            log.warning("face model release failed during HUD shutdown", exc_info=True)

    return OmegaSpaApiPrefixMiddleware(app)


# Module-level instance for `uvicorn hud.server:app` invocation.
app = create_app()

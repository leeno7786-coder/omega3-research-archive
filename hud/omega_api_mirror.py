"""Rewrite /api/omega/* SPA fetch paths to live /api/* handlers."""

from __future__ import annotations

from typing import Callable, Awaitable

# Routes that are natively implemented under /api/omega/ (do not rewrite).
_NATIVE_OMEGA_SEGMENTS = frozenset({"request_tool", "doctrine_audit"})


class OmegaSpaApiPrefixMiddleware:
    """Map React HUD fetch paths onto the existing FastAPI surface."""

    def __init__(self, app: Callable[..., Awaitable[None]]) -> None:
        self.inner = app

    def __getattr__(self, name: str):  # noqa: ANN001
        return getattr(self.inner, name)

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope.get("type") == "http":
            path = scope.get("path") or ""
            if path.startswith("/api/omega/"):
                remainder = path[len("/api/omega/") :]
                head = remainder.split("/", 1)[0] if remainder else ""
                if head not in _NATIVE_OMEGA_SEGMENTS:
                    new_path = "/api/" + remainder
                    scope = dict(scope)
                    scope["path"] = new_path
                    scope["raw_path"] = new_path.encode("utf-8")
        await self.inner(scope, receive, send)

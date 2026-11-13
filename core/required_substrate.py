"""Attributable failures for work that Omega cannot truthfully skip."""

from __future__ import annotations


class RequiredSubstrateError(RuntimeError):
    """A required substrate operation failed before it could commit success."""

    def __init__(
        self,
        component: str,
        operation: str,
        *,
        reason: str,
        detail: str = "",
    ) -> None:
        self.component = str(component or "unknown")
        self.operation = str(operation or "unknown")
        self.reason = str(reason or "unknown")
        self.detail = str(detail or "")
        message = f"{self.component}.{self.operation} failed: {self.reason}"
        if self.detail:
            message += f" ({self.detail})"
        super().__init__(message)

    @classmethod
    def from_exception(
        cls,
        component: str,
        operation: str,
        exc: BaseException,
    ) -> "RequiredSubstrateError":
        """Preserve an already-attributed failure or classify a raw cause."""
        if isinstance(exc, cls):
            return exc
        return cls(
            component,
            operation,
            reason=type(exc).__name__,
            detail=str(exc),
        )

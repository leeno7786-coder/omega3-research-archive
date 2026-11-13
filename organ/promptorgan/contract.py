"""PromptOrgan contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class PromptContext:
    """Immutable host-owned input for one prompt pack."""

    kind: str
    question: str = ""
    data: Any = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PhrasedRequest:
    """A complete model request assembled by one prompt pack."""

    pack: str
    version: str
    kind: str
    system: str
    user: str
    response_format: Mapping[str, Any] | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


class PromptPack(Protocol):
    name: str
    version: str
    priority: int

    def claims(self, context: PromptContext) -> bool: ...

    def build(self, context: PromptContext) -> PhrasedRequest: ...

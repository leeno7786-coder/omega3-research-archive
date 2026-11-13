"""The single prompt ownership layer for Omega3.0.

PromptOrgan chooses a deterministic prompt pack. Packs own the model-facing
selection and assembly boundary; typed domain modules may remain the canonical
owners of schemas, evidence contracts, and authority-bound prompt payloads.
Callers own only the host facts passed into the pack. MCP execution, authority,
and tool handlers remain outside this package.
"""

from __future__ import annotations

from .contract import PromptContext, PhrasedRequest, PromptPack
from .registry import discover
from .router import phrase, route

__all__ = [
    "PromptContext",
    "PhrasedRequest",
    "PromptPack",
    "discover",
    "phrase",
    "route",
]

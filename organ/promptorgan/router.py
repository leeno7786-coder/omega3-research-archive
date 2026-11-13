"""Deterministic PromptOrgan routing."""

from __future__ import annotations

from typing import Iterable

from .contract import PromptContext, PromptPack, PhrasedRequest
from .registry import discover


def route(
    context: PromptContext,
    packs: Iterable[PromptPack] | None = None,
) -> PromptPack:
    candidates = tuple(packs) if packs is not None else discover()
    for pack in candidates:
        if pack.claims(context):
            return pack
    raise LookupError(f"no PromptOrgan pack claims kind={context.kind!r}")


def phrase(
    context: PromptContext,
    packs: Iterable[PromptPack] | None = None,
) -> PhrasedRequest:
    pack = route(context, packs)
    request = pack.build(context)
    if request.pack != pack.name:
        raise ValueError(
            f"PromptOrgan pack {pack.name!r} returned request for {request.pack!r}"
        )
    return request

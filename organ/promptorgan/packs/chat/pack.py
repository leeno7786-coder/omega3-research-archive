"""Deterministic chat answer prompts."""

from __future__ import annotations

from collections.abc import Mapping

from ...contract import PromptContext, PhrasedRequest


NAME = "chat"
VERSION = "1.0.0"


def _data(context: PromptContext) -> Mapping[str, object]:
    return context.data if isinstance(context.data, Mapping) else {}


class ChatPack:
    name = NAME
    version = VERSION
    priority = 100

    def claims(self, context: PromptContext) -> bool:
        if context.kind != "chat":
            return False
        from ..web_search.pack import is_web_search_request

        data = _data(context)
        if data.get("web_research"):
            return False
        return not is_web_search_request(context.question)

    def build(self, context: PromptContext) -> PhrasedRequest:
        from .answer import (
            answer_system_prompt,
            build_answer_prompt,
            resolve_register,
        )
        from auto_loop.scaffold.grammar import analyze

        data = _data(context)
        hits = data.get("hits")
        if not isinstance(hits, Mapping):
            hits = {}
        shape = analyze(context.question)
        register = str(data.get("register") or "").strip().lower()
        if not register:
            register = resolve_register(
                chat_intent=str(data.get("chat_intent") or ""),
                prompt_type=str(data.get("prompt_type") or ""),
                shape=shape,
            )
        answer = build_answer_prompt(
            context.question,
            hits,
            memory_required=bool(data.get("memory_required")),
            register=register,
            multiturn=bool(data.get("multiturn")),
        )
        context_blocks = tuple(
            str(block).strip()
            for block in (data.get("context_blocks") or ())
            if str(block).strip()
        )
        if context_blocks:
            answer = "\n\n".join((*context_blocks, answer))
        temporal_line = str(data.get("temporal_line") or "").strip()
        if temporal_line:
            answer = f"{temporal_line}\n\n{answer}"
        return PhrasedRequest(
            pack=NAME,
            version=VERSION,
            kind=context.kind,
            system=answer_system_prompt(register),
            user=answer,
            metadata={"register": register},
        )


PACK = ChatPack()

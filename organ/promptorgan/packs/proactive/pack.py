"""Prompt pack for cognition-initiated operator updates."""

from __future__ import annotations

from ...contract import PromptContext, PhrasedRequest


class ProactivePack:
    name = "proactive"
    version = "1.0.0"
    priority = 100

    def claims(self, context: PromptContext) -> bool:
        return context.kind == "proactive"

    def build(self, context: PromptContext) -> PhrasedRequest:
        from ..chat.pack import PACK as chat_pack

        base = chat_pack.build(
            PromptContext(
                kind="chat",
                question=context.question,
                data=context.data,
                metadata=context.metadata,
            )
        )
        system = (
            f"{base.system}\n\nThis is a cognition-initiated proactive update, not an "
            "answer to a waiting chat bubble. State the grounded update directly and do not "
            "claim that work happened unless the supplied execution evidence supports it."
        )
        return PhrasedRequest(
            pack=self.name,
            version=self.version,
            kind=context.kind,
            system=system,
            user=base.user,
            metadata={**dict(base.metadata), "delivery_channel": "proactive"},
        )


PACK = ProactivePack()

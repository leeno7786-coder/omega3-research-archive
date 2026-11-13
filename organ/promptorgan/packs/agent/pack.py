"""Prompt pack for bounded agentic work."""

from __future__ import annotations

from collections.abc import Mapping

from ...contract import PromptContext, PhrasedRequest


NAME = "agent"
VERSION = "1.0.0"


def _value(data: object, key: str, default: object = None) -> object:
    if isinstance(data, Mapping):
        return data.get(key, default)
    return getattr(data, key, default)


class AgentPack:
    name = NAME
    version = VERSION
    priority = 100

    def claims(self, context: PromptContext) -> bool:
        return context.kind == "agent"

    def build(self, context: PromptContext) -> PhrasedRequest:
        data = context.data
        directive = _value(data, "directive", "")
        if not str(directive or "").strip():
            from schema.qwen_skill_dispatch import build_directive

            directive = build_directive(data)
        agent_type = str(
            _value(data, "agent_type", "")
            or _value(data, "execution_kind", "")
            or _value(data, "skill_class", "")
            or "bounded"
        ).strip()
        system = f"AGENT TYPE: {agent_type}\n\n{str(directive)}"
        return PhrasedRequest(
            pack=NAME,
            version=VERSION,
            kind=context.kind,
            system=system,
            user=str(context.question or _value(data, "objective", "")),
            metadata={"agent_type": agent_type},
        )


PACK = AgentPack()

"""Prompt pack for an explicitly routed workspace inspection request."""

from __future__ import annotations

from ...contract import PromptContext, PhrasedRequest
from auto_loop.promptpack.operator_intent import is_workspace_inspection_request


NAME = "workspace"
VERSION = "1.0.0"

_WORKSPACE_SYSTEM = (
    "This turn explicitly requests a read-only inspection of the operator's "
    "active trusted folder. Call list_directory before answering. Use only the "
    "returned directory evidence; do not answer from memory as if the folder "
    "had been inspected. Recalled conversation is not evidence for this turn. "
    "The host enforces the trusted-folder boundary."
)


class WorkspacePack:
    name = NAME
    version = VERSION
    priority = 85

    def claims(self, context: PromptContext) -> bool:
        return (
            context.kind == "chat"
            and isinstance(context.data, dict)
            and str(context.data.get("operator_route") or "").strip().lower() == "agent"
            and is_workspace_inspection_request(context.question)
        )

    def build(self, context: PromptContext) -> PhrasedRequest:
        from ..chat.pack import PACK as chat_pack

        base = chat_pack.build(context)
        return PhrasedRequest(
            pack=NAME,
            version=VERSION,
            kind=context.kind,
            system=f"{base.system}\n\n{_WORKSPACE_SYSTEM}",
            user=(
                "WORKSPACE INSPECTION REQUEST\n"
                "Use list_directory now, then report the observed entries.\n\n"
                + base.user
            ),
            metadata={
                **dict(base.metadata),
                "force_tool": "list_directory",
                "operator_route": "agent",
            },
        )


PACK = WorkspacePack()

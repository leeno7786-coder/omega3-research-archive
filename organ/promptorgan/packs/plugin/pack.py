"""MCP plugin planning prompt pack.

This pack can propose a plugin operation, but it cannot execute one. The MCP
authority gateway and server remain the only execution owners.
"""

from __future__ import annotations

from collections.abc import Mapping

from ...contract import PromptContext, PhrasedRequest


NAME = "plugin"
VERSION = "1.0.0"


class PluginPack:
    name = NAME
    version = VERSION
    priority = 100

    def claims(self, context: PromptContext) -> bool:
        return context.kind == "plugin"

    def build(self, context: PromptContext) -> PhrasedRequest:
        from mcp_platform.inference.prompts import MCP_SYSTEM_KERNEL

        data = context.data if isinstance(context.data, Mapping) else {}
        prompt = data.get("prompt")
        response_format = data.get("response_format")
        if prompt is None or response_format is None:
            from mcp_platform.plugin_planning import build_plugin_planner_prompt

            prompt, response_format = build_plugin_planner_prompt(
                data["objective"],
                data["candidates"],
                data.get("prior_steps") or (),
                context=data["context"],
                metadata=data["metadata"],
            )
        return PhrasedRequest(
            pack=NAME,
            version=VERSION,
            kind=context.kind,
            system=(
                f"{MCP_SYSTEM_KERNEL}\n"
                "Choose only from the supplied signed MCP candidates and return a proposal; "
                "the host performs authority checks and the MCP server performs execution."
            ),
            user=str(prompt),
            response_format=response_format,
        )


PACK = PluginPack()

"""Station marker for action-specific Omega foldbacks."""

from __future__ import annotations

from ...contract import PromptContext, PhrasedRequest


class MetaOmegaPack:
    name = "meta_omega"
    version = "1.0.0"
    priority = 9000

    def claims(self, context: PromptContext) -> bool:
        return False

    def build(self, context: PromptContext) -> PhrasedRequest:
        raise RuntimeError("meta_omega is station-invoked; use foldback_spec_for")


PACK = MetaOmegaPack()

"""Model-family controls for the LM Studio chat-template boundary.

LM Studio owns and renders each model's tokenizer-provided prompt template. This
registry only supplies family-specific request controls that Omega must add to
the OpenAI-compatible request (turn endings and supported Jinja variables).
Unknown families stay server-managed instead of receiving guessed tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class ModelTemplateProfile:
    """Request controls for a known model family's bundled LM Studio template."""

    family: str
    end_token: str
    tool_end_tokens: tuple[str, ...] = ()
    supports_template_kwargs: bool = False


_GEMMA4 = ModelTemplateProfile(
    family="gemma4",
    end_token="<turn|>",
    tool_end_tokens=("<|tool_response>",),
    supports_template_kwargs=True,
)
_GEMMA_LEGACY = ModelTemplateProfile(family="gemma", end_token="<end_of_turn>")
_QWEN3 = ModelTemplateProfile(
    family="qwen3",
    end_token="<|im_end|>",
    supports_template_kwargs=True,
)
_QWEN_LEGACY = ModelTemplateProfile(family="qwen", end_token="<|im_end|>")


def resolve_lmstudio_template_profile(model: str) -> Optional[ModelTemplateProfile]:
    """Resolve known Gemma and Qwen IDs without depending on provider casing/path.

    LM Studio IDs commonly include publisher prefixes, and may use either a dash
    or no separator between family and version. The rest of the prompt rendering
    remains with LM Studio's model metadata.
    """
    normalized = str(model or "").strip().lower().replace("_", "-")
    gemma = re.search(r"(?<![a-z0-9])gemma-?(\d+)(?:[.-]|\b)", normalized)
    if gemma:
        version = int(gemma.group(1))
        if version == 4:
            return _GEMMA4
        if version in {2, 3}:
            return _GEMMA_LEGACY

    qwen = re.search(r"(?<![a-z0-9])qwen-?(\d+)(?:[.-]|\b)", normalized)
    if qwen:
        version = int(qwen.group(1))
        if version == 3:
            return _QWEN3
        if version == 2:
            return _QWEN_LEGACY
    return None

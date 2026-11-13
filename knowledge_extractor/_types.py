"""Knowledge extractor contracts — Source input and KnowledgeArtifact output."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Source:
    kind: str  # web_result | file | pdf | video_transcript | tool_output
    content: str
    title: Optional[str] = None
    source_ref: Optional[str] = None
    meta: dict = field(default_factory=dict)


@dataclass(frozen=True)
class KnowledgeArtifact:
    domain: str
    concepts: tuple[str, ...]
    facts: tuple[str, ...]
    relationships: tuple[dict, ...]
    mastery_tags: tuple[str, ...]
    confidence: float
    source_ref: Optional[str] = None
    source_kind: str = ""
    artifact_id: str = ""

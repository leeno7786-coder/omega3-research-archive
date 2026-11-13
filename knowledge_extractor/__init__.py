"""Knowledge extractor — raw content to structured KnowledgeArtifact (deterministic)."""

from knowledge_extractor._types import KnowledgeArtifact, Source
from knowledge_extractor.pipeline import extract

__all__ = ["extract", "KnowledgeArtifact", "Source"]

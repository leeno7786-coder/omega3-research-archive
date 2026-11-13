"""Knowledge extractor pipeline — pure sequencer (ingest → derive → store)."""

from __future__ import annotations

from guardian.terminal_feedback import emit_data, phase_span

from knowledge_extractor import derive, ingest, store
from knowledge_extractor._types import KnowledgeArtifact, Source


def extract(source: Source) -> KnowledgeArtifact:
    with phase_span(
        "knowledge",
        "extract",
        source_kind=str(source.kind or ""),
        source_ref=str(source.source_ref or ""),
    ):
        with phase_span(
            "knowledge",
            "ingest",
            source_kind=str(source.kind or ""),
        ):
            normalized = ingest.run(source)
        with phase_span(
            "knowledge",
            "derive",
            source_kind=str(normalized.get("source_kind") or ""),
            content_length=len(str(normalized.get("text") or "")),
        ):
            fields = derive.run(normalized)
        artifact = KnowledgeArtifact(
            domain=fields["domain"],
            concepts=tuple(fields["concepts"]),
            facts=tuple(fields["facts"]),
            relationships=tuple(fields["relationships"]),
            mastery_tags=tuple(fields["mastery_tags"]),
            confidence=float(fields["confidence"]),
            source_ref=normalized.get("source_ref"),
            source_kind=str(normalized.get("source_kind") or ""),
            artifact_id=str(fields["artifact_id"]),
        )
        with phase_span(
            "knowledge",
            "store",
            artifact_id=artifact.artifact_id,
            domain=artifact.domain,
        ):
            store.run(artifact)
        try:
            emit_data(
                "knowledge",
                "knowledge_artifact",
                summary=f"domain={artifact.domain}",
                artifact_id=artifact.artifact_id,
                domain=artifact.domain,
                confidence=artifact.confidence,
            )
        except Exception:
            pass
    return artifact

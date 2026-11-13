"""Document output and verified delivery artifacts in the episode's existing intake lane."""

from __future__ import annotations

import hashlib
from typing import Any

from memory.auto_evidence import attempt_recorder, new_attempt_token
from memory.episode_events import record_event


def capture_document_source(episode_id: str, *, source: dict[str, Any]) -> str | None:
    """Record the upload's identity and screen decision, retaining no uploaded text."""
    if attempt_recorder(episode_id, lane="document") is None:
        return None
    event = record_event(
        episode_id,
        operation_id=f"document-source:{episode_id}:{new_attempt_token()}",
        phase="auto",
        event_kind="auto.document_source",
        payload={"source": source},
    )
    return event.event_id


def capture_generated_document(
    episode_id: str,
    *,
    markdown: str,
    title: str,
    template: str,
    fmt: str,
    source: dict[str, Any] | None,
    prompt: str,
    request_digest: str,
    source_event_id: str | None = None,
    generation_error: str | None = None,
) -> str | None:
    """Retain the generated document before rendering can fail, without retaining the upload.

    The document is model output, not a statement that its contents have been verified. The
    source receipt identifies the extracted input by digest; its private body is not copied.
    Like other AUTO capture producers, direct callers without an episode own no memory write.
    """
    if attempt_recorder(episode_id, lane="document") is None:
        return None
    event = record_event(
        episode_id,
        operation_id=f"document-generated:{episode_id}:{new_attempt_token()}",
        phase="auto",
        event_kind="auto.document_generated",
        evidence_class="model_conclusion",
        evidence_refs=(source_event_id,) if source_event_id else (),
        payload={
            "markdown": markdown,
            "markdown_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
            "title": title,
            "template": template,
            "format": fmt,
            "source": source,
            "operator_prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "request_digest": request_digest,
            "generation_status": "failed" if generation_error is not None else "succeeded",
            "failure_reason": generation_error,
        },
    )
    return event.event_id


def capture_verified_document(
    episode_id: str, *, artifact: dict[str, Any], generated_event_id: str | None,
) -> None:
    """Record the host's readback proof, tied to the document it rendered."""
    if attempt_recorder(episode_id, lane="document") is None:
        return
    if not generated_event_id:
        raise ValueError("a verified document requires its generated-document event")
    record_event(
        episode_id,
        operation_id=f"document-verified:{episode_id}:{generated_event_id}",
        phase="auto",
        event_kind="auto.document_artifact",
        evidence_class="verification_result",
        evidence_refs=(generated_event_id,),
        payload={"artifact": artifact, "generated_event_id": generated_event_id,
                 "verification": "persisted bytes match rendered bytes"},
    )

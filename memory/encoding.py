"""
Memory atomic citizenship - post-commit memory pipeline
========================================================

Every Guardian-approved memory write must complete the native projection
pipeline or roll back. Semantic perception is rebuilt from canonical truth by
the resident Ettin retina at the projection boundary; this module does not
compute or persist dense embeddings.

The post-commit pipeline is:

    1. canonical commit            <- memory.engine.validate_write.atomic_write
    2. index update                <- same
    3. native projection admission <- no durable dense vector
    4. persistence anchor          <- memory.strength_index
    5. reflection eligibility      <- memory.queues.enqueue_reflection_eligible
    6. deepsleep candidacy         <- memory.queues.enqueue_deepsleep_candidate

Caller (memory.promotion.commit_promotion / commit_flagged) wraps the result
and rolls back the canonical write when ``ok`` is false.
"""

from __future__ import annotations

import logging
import re as _re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

log = logging.getLogger("omega.memory.encoding")

IDENTITY_LOBE = 0


def _is_identity_write(memory_dict: Dict[str, Any]) -> bool:
    """Identity writes live in lobe 0 (``000_identity``)."""
    coord = memory_dict.get("coord") or []
    return bool(coord) and int(coord[0]) == IDENTITY_LOBE


@dataclass
class EncodingResult:
    """Outcome of the post-commit native projection pipeline."""

    ok: bool
    failed_step: Optional[str] = None
    failure_reason: Optional[str] = None
    is_identity_write: bool = False
    steps_completed: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


# Trait records are filesystem-only citizenship. Their pattern-voting system
# never belonged in semantic projection.
NON_SEMANTIC_TYPES = frozenset({"emergent_trait", "trait_observation"})


def encode_committed_memory(
    memory_dict: Dict[str, Any],
    final_path: str,
    *,
    skip_embed: bool = False,
) -> EncodingResult:
    """Admit a committed memory to native projection and durable queues.

    ``skip_embed`` remains accepted while intake callers converge; it has no
    effect because dense embedding is no longer a write-time operation.
    """
    del skip_embed
    is_identity = _is_identity_write(memory_dict)
    memory_id = memory_dict.get("id") or ""
    coord = memory_dict.get("coord") or []
    result = EncodingResult(ok=True, is_identity_write=is_identity)

    if str(memory_dict.get("type") or "").strip().lower() in NON_SEMANTIC_TYPES:
        result.steps_completed.append("non_semantic_bypass")
        return _bind_claim_receipt(memory_dict, final_path, result)

    result.steps_completed.append("native_projection_eligible")

    try:
        from memory.strength_index import ensure_strength_index

        si = ensure_strength_index()
        write_strength = float((memory_dict.get("provenance") or {}).get("confidence", 0.5))
        si.register(memory_id, write_strength)
    except Exception as exc:
        return _fail(result, "persistence_anchor", f"StrengthIndex.register raised: {exc}")
    result.steps_completed.append("persistence_anchor")

    try:
        from memory.queues import enqueue_reflection_eligible
    except Exception as exc:
        return _fail(result, "import_queues", f"could not import queues: {exc}")
    if not enqueue_reflection_eligible(memory_id, list(coord)):
        log.warning(
            "encoding: reflection eligibility queue append failed for %s "
            "(best-effort until L11 consumer exists)",
            memory_id,
        )
    else:
        result.steps_completed.append("reflection_eligibility")
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "encoding_reflection_queue",
                summary=f"queued memory_id={memory_id}",
                memory_id=memory_id,
                ok=True,
            )
        except Exception:
            pass

    try:
        from memory.queues import enqueue_deepsleep_candidate
    except Exception as exc:
        return _fail(result, "import_queues", f"could not import queues: {exc}")
    if not enqueue_deepsleep_candidate(memory_id, list(coord)):
        return _fail(result, "deepsleep_candidacy", "queue append failed (jsonl write error)")
    result.steps_completed.append("deepsleep_candidacy")
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "encoding_deepsleep_queue",
            summary=f"queued memory_id={memory_id}",
            memory_id=memory_id,
            ok=True,
        )
    except Exception:
        pass

    result = _bind_claim_receipt(memory_dict, final_path, result)
    if not result.ok:
        return result

    log.info(
        "encoding ok memory_id=%s identity=%s steps=%s",
        memory_id,
        is_identity,
        result.steps_completed,
    )
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "encode_committed_memory",
            summary=f"ok={result.ok} steps={len(result.steps_completed)}",
            memory_id=memory_id,
            steps_completed=list(result.steps_completed),
            ok=result.ok,
            failed_step=result.failed_step,
            is_identity_write=is_identity,
        )
    except Exception:
        pass
    return result


def _bind_claim_receipt(memory_dict: Dict[str, Any], final_path: str,
                        result: EncodingResult) -> EncodingResult:
    """Claim integrity applies to filesystem-only traits as well as recall records."""
    provenance = memory_dict.get("provenance")
    provenance = dict(provenance) if isinstance(provenance, dict) else {}
    if not provenance.get("source_id"):
        return result
    provenance["projection_receipt_required"] = True
    memory_dict["provenance"] = provenance
    try:
        from memory.engine.validate_write import atomic_write
        from memory.projection_receipt import memory_root_for_canonical_path, write_projection_receipt

        atomic_write(memory_dict, update_index=False)
        write_projection_receipt(memory_dict, root=memory_root_for_canonical_path(final_path))
    except Exception as exc:
        return _fail(result, "projection_receipt", f"projection receipt write failed: {exc}")
    result.steps_completed.append("projection_receipt")
    return result


def _fail(result: EncodingResult, step: str, reason: str) -> EncodingResult:
    """Mark and emit a failed encoding result."""
    result.ok = False
    result.failed_step = step
    result.failure_reason = reason
    result.errors.append(f"{step}: {reason}")
    log.error("encoding FAIL step=%s identity=%s reason=%s", step, result.is_identity_write, reason)
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "memory",
            "encode_committed_memory",
            status="fail",
            summary=f"failed_step={step}",
            steps_completed=list(result.steps_completed),
            ok=False,
            failed_step=step,
            is_identity_write=result.is_identity_write,
        )
    except Exception:
        pass
    return result


# Rule-frame and stance helpers remain local to memory encoding so the
# contradiction tests do not reintroduce a dense semantic dependency.
_RULE_NEG = _re.compile(
    r"\b(never|must not|may not|do not|don'?t|forbidden|prohibited|no longer)\b"
)
_RULE_POS = _re.compile(r"\b(must|always|shall|should|required|mandatory|non-negotiable)\b")
_RULE_STOP = frozenset(
    {
        "you",
        "must",
        "never",
        "always",
        "shall",
        "should",
        "not",
        "do",
        "don't",
        "the",
        "a",
        "an",
        "to",
        "with",
        "before",
        "after",
        "while",
        "when",
        "of",
        "on",
        "in",
        "at",
        "for",
        "and",
        "or",
        "is",
        "are",
        "be",
        "it",
        "this",
        "that",
        "required",
        "mandatory",
        "forbidden",
        "prohibited",
        "may",
    }
)


def _rule_frame(text: str) -> tuple[str, frozenset[str]] | None:
    """Normalize one rule into polarity and content tokens."""
    value = str(text or "").strip().lower()
    if not value:
        return None
    negative = _RULE_NEG.search(value)
    positive = _RULE_POS.search(value)
    if negative is None and positive is None:
        return None
    modality = "-" if negative is not None else "+"
    anchor = negative or positive
    tokens = frozenset(
        word
        for word in _re.findall(r"[a-z][a-z'-]+", value[anchor.end() :])
        if word not in _RULE_STOP
    )
    if not tokens:
        tokens = frozenset(
            word for word in _re.findall(r"[a-z][a-z'-]+", value) if word not in _RULE_STOP
        )
    return modality, tokens


def _rules_compatible(text_a: str, text_b: str) -> bool | None:
    """Return False only for opposing modalities on the same action."""
    frame_a, frame_b = _rule_frame(text_a), _rule_frame(text_b)
    if frame_a is None or frame_b is None:
        return None
    modality_a, action_a = frame_a
    modality_b, action_b = frame_b
    shared = action_a & action_b
    same_action = len(shared) >= 2 or (
        len(shared) == 1 and (len(action_a) <= 2 or len(action_b) <= 2)
    )
    return not (same_action and modality_a != modality_b)


_STANCE_NEG = _re.compile(
    r"\b(not|never|no longer|don'?t|doesn'?t|isn'?t|aren'?t|won'?t|cannot|"
    r"can'?t|refuse[sd]?|reject(?:s|ed)?|avoid(?:s|ed)?|stop(?:s|ped)?)\b"
)


def _content_tokens(text: str) -> frozenset[str]:
    return frozenset(
        word
        for word in _re.findall(r"[a-z][a-z'-]+", str(text or "").lower())
        if word not in _RULE_STOP
    )


def _stance_opposed(text_a: str, text_b: str) -> bool:
    """Detect one-sided negation over at least two shared content tokens."""
    negative_a = bool(_STANCE_NEG.search(str(text_a or "").lower()))
    negative_b = bool(_STANCE_NEG.search(str(text_b or "").lower()))
    if negative_a == negative_b:
        return False
    return len(_content_tokens(text_a) & _content_tokens(text_b)) >= 2


def _identity_facet(memory_id: str) -> Optional[str]:
    """Return the facet for a trait-family memory id, if present."""
    parts = str(memory_id or "").split(".")
    if len(parts) >= 3 and parts[0] in {"trait", "trait-observation"}:
        return parts[1] or None
    return None

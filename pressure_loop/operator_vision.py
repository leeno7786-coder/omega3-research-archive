"""Deterministic vision handling for Guardian-approved operator chat.

This module deliberately lives below the HUD.  The web surface only accepts
and queues chat; image interpretation happens after the text ingress has been
captured in the SourceLedger and allowed by Guardian.
"""

from __future__ import annotations

import logging
import base64
import hashlib
import json
import math
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from core.paths import GUARDIAN_STATE_ROOT


log = logging.getLogger(__name__)

UPLOAD_ROOT = GUARDIAN_STATE_ROOT / "uploads"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
WHO_IS_THIS_RE = re.compile(
    r"\bwho(?:'s|\s+is|\s+are)\s+(?:this|that|these|it|in\s+the\s+(?:photo|picture|image))\b",
    re.IGNORECASE,
)


def prepare_visual_intake(payload, *, upload_refs=()) -> dict[str, Any]:
    """Validate JSON-safe transport data without interpreting or remembering it."""
    if payload is not None and not isinstance(payload, dict):
        raise ValueError("visual intake must be an object")
    result = {}
    refs = list(dict.fromkeys(str(ref) for ref in upload_refs if ref))
    if refs:
        result["upload_refs"] = refs
    face = (payload or {}).get("face")
    if face is not None:
        if not isinstance(face, dict):
            raise ValueError("face intake must be an object")
        from guardian.external_ingress import screen_text

        screened = {}
        for field in ("name", "context"):
            value = str(face.get(field) or "").strip()
            if value:
                value, decision = screen_text(value, source="face_memory", field=field)
                if decision != "allow":
                    raise ValueError(f"face {field} was {decision} by Guardian")
            screened[field] = value
        if not screened["name"]:
            raise ValueError("face name required")
        embedding = face.get("embedding")
        if not isinstance(embedding, list) or len(embedding) != 512:
            raise ValueError("face embedding must contain 512 finite numbers")
        embedding = [float(value) for value in embedding]
        if not all(math.isfinite(value) for value in embedding):
            raise ValueError("face embedding must contain 512 finite numbers")
        raw_b64 = str(face.get("image_b64") or "").split(",", 1)[-1]
        if not raw_b64 or not base64.b64decode(raw_b64, validate=True):
            raise ValueError("face image required")
        result["face"] = {**screened, "embedding": embedding,
                          "bbox": face.get("bbox"), "image_b64": raw_b64}
    return result


def queue_face_memory(*, name, embedding, image_b64, context="", bbox=None):
    """The single face enrollment admission path for HUD and diagnostics."""
    from pressure_loop.operator_chat import handle_operator_chat

    return handle_operator_chat(
        f"Remember this face as {name}. {context}".strip(), directive_intent="RESPOND",
        visual_intake={"face": {"name": name, "context": context, "embedding": embedding,
                                "bbox": bbox, "image_b64": image_b64}},
    )


def build_face_memory_envelope(*, name, persona_coord, embedding, context="",
                               bbox=None, image_b64=None, cycle_id="", source_citation=None):
    """Retain biometric evidence inside the canonical enrichment whitelist."""
    from memory.api import CanonicalEnvelope

    record_cycle = f"face-remember-{cycle_id or uuid.uuid4().hex}"
    face = {"name": name, "person_id": persona_coord[2], "embedding": embedding,
            "embedding_dim": len(embedding), "operator_context": context or None,
            "bbox": bbox, "image_b64": image_b64}
    return CanonicalEnvelope(
        cycle_id=record_cycle, coord=[7, 1, 0], type="face_memory",
        id="7.01.face_" + hashlib.sha256(record_cycle.encode()).hexdigest()[:24],
        payload={"title": f"face:{name}", "summary": f"Face memory for {name}.",
                 "text": f"Face memory for {name}. {context}".strip(),
                 "subtopic": "face_memory", "tags": ["face", "vision", "persona_link"],
                 "enrichment": {"face_memory": face},
                 "links": [{"kind": "persona_link", "person_id": persona_coord[2],
                            "person_name": name, "coord": list(persona_coord)}],
                 **dict(source_citation or {})},
        source="operator_vision.auto", confidence=0.7,
        provenance=["hud.api_vision_remember_face"],
    )


def execute_visual_intake(payload, *, text, cycle_id, episode_id, source_citation=None):
    """AUTO visual action; successful and failed work become owned episode content."""
    from memory import episode_sram
    from memory.api import CanonicalEnvelope, intake_memory, intake_receipt_accepted

    if episode_sram.bound_episode_id() != episode_id:
        raise RuntimeError("visual intake requires the current episode SRAM owner")
    outcomes = []
    response = None
    face = payload.get("face")
    if face is not None:
        try:
            from core.persona_router import route_persona, add_memory_for_person
            from sensory.vision_ingest import ingest_image_description

            persona_coord = route_persona(face["name"])
            env = build_face_memory_envelope(
                name=face["name"], persona_coord=persona_coord, embedding=face["embedding"],
                context=face.get("context", ""), bbox=face.get("bbox"),
                image_b64=face["image_b64"], cycle_id=cycle_id,
                source_citation=source_citation,
            )
            receipt = intake_memory(env, propose=False, bypass_guardian=True, skip_embed=True)
            if not intake_receipt_accepted(receipt):
                raise RuntimeError(f"face memory rejected: {receipt.message}")
            persona_ref = add_memory_for_person(
                face["name"], {"text": f"Face image supplied for {face['name']}.",
                               "tags": ["face_link", "vision"],
                               "enrichment": {"face_cycle_id": env.cycle_id},
                               **dict(source_citation or {})},
                type="persona_face_link", cycle_id=f"{env.cycle_id}-persona", human_sourced=True,
            )
            if not persona_ref:
                raise RuntimeError("face persona link was not admitted")
            outcomes.append({"ok": True, "outcome": "face_admitted", "name": face["name"]})
            description = ingest_image_description(
                base64.b64decode(face["image_b64"], validate=True), origin="face_memory",
                name=face["name"], episode_id=episode_id,
            )
            outcomes.append(description)
            response = {"message": f"Captured face memory for {face['name']} for this episode's memory consolidation.",
                        "kind": "face_memory_admitted"}
        except Exception as exc:
            outcomes.append({"ok": False, "outcome": "face_intake_failed",
                             "error": f"{type(exc).__name__}: {exc}"})
    for ref in payload.get("upload_refs") or []:
        try:
            outcome = ingest_uploaded_image(ref, cycle_id=episode_id)
            if outcome is not None:
                outcomes.append(outcome)
        except Exception as exc:
            outcomes.append({"ok": False, "outcome": "ingest_failed", "upload_ref": ref,
                             "error": f"{type(exc).__name__}: {exc}"})
    if not face and payload.get("upload_refs") and not any(not item.get("ok") for item in outcomes):
        response = answer_who_is_this_from_upload(payload["upload_refs"][0], text)
        if response is not None and response.get("ok") is False:
            outcomes.append(response)
            response = None
    report = {"ok": all(item.get("ok") for item in outcomes), "cycle_id": cycle_id,
              "episode_id": episode_id, "outcomes": outcomes, "response": response}
    receipt = intake_memory(CanonicalEnvelope(
        cycle_id=f"visual-result-{cycle_id}", coord=[6, 0, 0], type="episode",
        id="6.00.visual_" + hashlib.sha256(cycle_id.encode()).hexdigest()[:24],
        payload={"text": json.dumps(report, ensure_ascii=False), "title": "Visual intake outcome",
                 "enrichment": {"visual_intake": report}}, source="operator_vision.auto",
    ), propose=False, bypass_guardian=True, skip_embed=True)
    if not intake_receipt_accepted(receipt):
        raise RuntimeError(f"visual outcome record rejected: {receipt.message}")
    return report


def resolve_upload_ref(upload_ref: str, *, upload_root: Path = UPLOAD_ROOT) -> Optional[Path]:
    """Resolve a staged upload while refusing paths outside ``upload_root``."""
    ref = str(upload_ref or "").strip().replace("\\", "/")
    for prefix in ("/api/uploads/", "api/uploads/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix) :]
            break
    if not ref:
        return None

    root = upload_root.resolve()
    candidates = [ref] if "/" in ref else [f"image/{ref}", f"images/{ref}", ref]
    for rel in candidates:
        try:
            candidate = (upload_root / rel).resolve()
        except OSError:
            continue
        if candidate != root and root not in candidate.parents:
            continue
        if candidate.is_file():
            return candidate
    return None


def ingest_uploaded_image(
    upload_ref: str,
    *,
    cycle_id: str = "",
    upload_root: Path = UPLOAD_ROOT,
) -> Optional[Dict[str, Any]]:
    """Send one staged image through the visual-memory ingest pipeline.

    Documents return None; an unavailable image or runtime returns an explicit
    failure with its cause for AUTO to capture and resolve to the operator.
    """
    path = resolve_upload_ref(upload_ref, upload_root=upload_root)
    if path is None and Path(upload_ref).suffix.lower() in IMAGE_SUFFIXES:
        raise FileNotFoundError(f"staged image is unavailable: {upload_ref}")
    if path is None or path.suffix.lower() not in IMAGE_SUFFIXES:
        return None
    try:
        from sensory.vision_ingest import ingest_image_description

        return ingest_image_description(
            path.read_bytes(),
            origin="upload",
            image_path=str(upload_ref),
            episode_id=str(cycle_id or ""),
        )
    except Exception as exc:
        return {
            "ok": False,
            "outcome": "ingest_failed",
            "origin": "upload",
            "upload_ref": str(upload_ref),
            "error": f"{type(exc).__name__}: {exc}",
        }


def answer_who_is_this_from_upload(
    upload_ref: str,
    text: str,
    *,
    upload_root: Path = UPLOAD_ROOT,
) -> Optional[Dict[str, Any]]:
    """Return deterministic face-identification evidence for an allowed chat turn.

    ``None`` means this input is not the operation. Failure carries an explicit
    cause for AUTO to append to its visual outcome and episode SRAM.
    """
    try:
        if not WHO_IS_THIS_RE.search(str(text or "")):
            return None
        path = resolve_upload_ref(upload_ref, upload_root=upload_root)
        if path is None or path.suffix.lower() not in IMAGE_SUFFIXES:
            return None

        from runtime.face.recognition import identify as face_identify

        result = face_identify(path.read_bytes())
        if not isinstance(result, dict) or result.get("ok") is False or result.get("degraded"):
            reason = (result.get("reason") or result.get("error") or "invalid face result"
                      if isinstance(result, dict) else "invalid face result")
            raise RuntimeError(f"face identification failed: {reason}")
        name = result.get("recognized_name") or result.get("name")
        face_seen = bool(
            result.get("face_count")
            or result.get("face_detected")
            or name
            or isinstance(result.get("embedding"), list)
        )
        if not name and isinstance(result.get("embedding"), list) and result["embedding"]:
            from runtime.face.anchor_store import get_anchor_store

            anchor = get_anchor_store().identify(result["embedding"])
            if anchor.get("recognized_name"):
                name = anchor["recognized_name"]
                result["confidence"] = anchor.get("confidence")

        if name:
            confidence = int(round(float(result.get("confidence") or 0.0) * 100))
            facts: list[str] = []
            try:
                from core.persona_router import recall_for_person

                for row in (recall_for_person(str(name)) or [])[:3]:
                    payload = row.get("payload") if isinstance(row, dict) else None
                    fact = str(
                        (payload or {}).get("text") or (payload or {}).get("summary") or ""
                    ).strip()
                    if fact:
                        facts.append(fact[:200])
            except Exception as exc:
                raise RuntimeError(f"face persona recall failed: {exc}") from exc
            message = f"That's {name} ({confidence}% match)."
            if facts:
                message += " " + " · ".join(facts)
        elif face_seen:
            message = (
                "I don't recognize this face yet — want me to remember them? "
                "Tell me their name (or use the camera flyout's save) and I will."
            )
        else:
            message = "I don't see a face in that image."

        return {
            "message": message,
            "kind": "VISION_IDENTIFY",
            "recognized_name": name,
            "confidence": result.get("confidence"),
        }
    except Exception as exc:
        return {"ok": False, "outcome": "face_identification_failed",
                "upload_ref": str(upload_ref), "error": f"{type(exc).__name__}: {exc}"}

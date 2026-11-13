"""Vision pipeline A — semantic image ingest (VISION_PERCEPTION_PIPELINE §3).

One deliberate image (webcam shot, upload, or Omega's own generated output)
becomes a screened text description in the vision lobe, and — when a person
is named — a recallable persona note, so "what does Noah look like" answers
through the persona lane while lobe 7 keeps holding the artifacts.

RATIFIED SENSORY ORDER (same as the fire loop / sensory sync):
  1. VLM describe (selected offline/cloud mind seat; injectable for tests) —
     a describe failure aborts pipeline A loudly; pipeline B is unaffected.
  2. SourceLedger artifact FIRST (raw description + provenance) — MANDATORY:
     no ledger, no content (withheld entirely).
  3. Guardian external-ingress screen taint-by-default — the description
     derives from an image the model READ; it is untrusted content.
  4. Memory gates: one lobe-7 record [7,2,0] scene_descriptions (canonical
     record holds the SCREENED text + pointers, never bytes) + optionally a
     persona note (lobe 1, recallable) linking the description to the person.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("omega.sensory.vision_ingest")

SCENE_COORD = [7, 2, 0]  # 700_vision / scene_descriptions

_DESCRIBE_PROMPT = (
    "Describe this image factually in 2-4 sentences: subjects, setting, "
    "notable objects, any visible text. No speculation about identity."
)


def _default_describe(image_bytes: bytes) -> Optional[str]:
    from runtime.vision import describe_image

    return describe_image(image_bytes, prompt=_DESCRIBE_PROMPT)


def ingest_image_description(
    image_bytes: bytes,
    *,
    origin: str,
    image_path: str = "",
    name: str = "",
    episode_id: str = "",
    root: Optional[Path] = None,
    describe_fn: Optional[Callable[[bytes], Optional[str]]] = None,
) -> Dict[str, Any]:
    """Run pipeline A on one image. Returns a JSON-safe outcome report.

    ``origin``: "face_memory" | "upload" | "generated". ``name`` (optional)
    links the description to that person's persona namespace. Never raises —
    every failure mode degrades to {"ok": False, "outcome": ...}."""
    sha = hashlib.sha256(image_bytes or b"").hexdigest()
    base = {"ok": False, "sha256": sha, "origin": str(origin or "")}
    if not image_bytes:
        return {**base, "outcome": "no_image"}

    # 1 — describe (injectable; failure aborts A loudly, never raises out).
    try:
        describe = describe_fn if describe_fn is not None else _default_describe
        description = str(describe(image_bytes) or "").strip()
    except Exception as exc:  # noqa: BLE001 — caller records the failure in its episode
        log.warning("vision describe failed (%s)", origin, exc_info=True)
        return {**base, "outcome": "describe_failed", "error": f"{type(exc).__name__}: {exc}"}
    if not description:
        return {**base, "outcome": "describe_empty"}

    # 2 — SourceLedger raw artifact (MANDATORY for content).
    try:
        from memory.source_ledger.store import record_source

        artifact = record_source(
            description,
            source_type=f"vision_{origin}" if origin else "vision_describe",
            origin_id=f"img@{sha[:16]}",
            root=root,
        )
    except Exception as exc:  # noqa: BLE001 — no ledger, no content
        log.warning("vision description withheld — source ledger write failed", exc_info=True)
        return {**base, "outcome": "withheld", "error": f"{type(exc).__name__}: {exc}"}

    # 3 — Guardian screen (taint-by-default).
    from guardian.external_ingress import screen_receipt

    screened = screen_receipt({"description": description}, source="vision_describe")
    guardian_block = dict(screened.pop("guardian", {}) or {})
    safe_description = str(screened.get("description") or "")

    # 4 — memory gates: lobe-7 scene record.
    try:
        citation = dict(artifact.citation() or {})
    except Exception:  # noqa: BLE001 — citation enriches, never blocks
        citation = {}
    from memory.api import CanonicalEnvelope, intake_memory, intake_receipt_accepted

    cycle_id = f"vision-desc-{sha[:16]}"
    label = str(name or origin or "image")
    env = CanonicalEnvelope(
        cycle_id=cycle_id,
        coord=list(SCENE_COORD),
        type="vision",
        payload={
            "title": f"scene: {label}"[:120],
            "summary": safe_description[:300],
            "text": safe_description,
            "subtopic": "scene_descriptions",
            "tags": ["vision", "scene", str(origin or "image")],
            "enrichment": {
                "origin": str(origin or ""),
                "image_path": str(image_path or ""),
                "image_sha256": sha,
                "episode_id": str(episode_id or ""),
                "person_name": str(name or ""),
                "guardian_quarantined": int(guardian_block.get("quarantined") or 0),
                "canonical": True,
            },
            **citation,
        },
        source="vision:describe",
    )
    receipt = intake_memory(env, propose=False, bypass_guardian=True, skip_embed=True)
    accepted = intake_receipt_accepted(receipt)
    if not accepted:
        log.warning(
            "vision scene record rejected by the gates (%s): %s",
            cycle_id,
            getattr(receipt, "message", ""),
        )

    # 5 — persona linkage (recallable lane): a named image's SCREENED
    # description becomes a note in the person's namespace, so persona recall
    # answers "what do they look like" without touching lobe-7 query recall.
    persona_note = None
    if name and accepted:
        try:
            from core.persona_router import add_memory_for_person

            persona_note = add_memory_for_person(
                name,
                {
                    "text": f"Visual: {safe_description}"[:600],
                    "vision_ref": cycle_id,
                    "image_sha256": sha,
                },
                type="visual_description",
                cycle_id=f"{cycle_id}-persona",
            )
        except Exception as exc:  # noqa: BLE001 — retain the partial success and its cause
            return {**base, "outcome": "persona_link_failed", "cycle_id": cycle_id,
                    "error": f"{type(exc).__name__}: {exc}",
                    "record_path": str(getattr(receipt, "path", "") or "")}

    return {
        **base,
        "ok": accepted,
        "outcome": "ingested" if accepted else "gate_rejected",
        "cycle_id": cycle_id,
        "description": safe_description,
        "record_path": str(getattr(receipt, "path", "") or ""),
        "persona_note": persona_note,
        "guardian_quarantined": int(guardian_block.get("quarantined") or 0),
    }


def describe_generated_image(image_path: str, *, episode_id: str = "") -> Dict[str, Any]:
    """IMAGE_CREATION hook: Omega remembers what it makes. Never raises."""
    try:
        raw = Path(image_path).read_bytes()
    except OSError:
        log.warning("generated image unreadable for ingest: %s", image_path)
        return {"ok": False, "outcome": "unreadable", "sha256": "", "origin": "generated"}
    return ingest_image_description(
        raw, origin="generated", image_path=str(image_path), episode_id=episode_id
    )

# Vision Perception Pipeline — Live Vision Organ

**Status:** IMPLEMENTED for deliberate face/image intake — CPU face inference,
provider-neutral scene description, lobe-7 writes, persona linkage, and
generated-image description are present in the current source tree.
**Reviewed:** 2026-09-10
**Authority:** `sensory/vision_ingest.py`, `pressure_loop/operator_vision.py`,
`runtime/vision.py`, `runtime/face/`, and `hud/server.py`.

**Recall boundary:** the structural ruling in `memory/dewey_map.py`
(`NON_RECALLABLE_LOBES = {6, 7}`) remains in force. Lobe 7 is ingest-open and
general-query-recall-closed. This pipeline supplies dedicated face identity and
persona-linkage behavior; it does not make lobe-7 scene records generally
recallable.

---

## 1. Rulings (binding)

1. **No production face watch. No production presence watch.** Ambient
   webcam/presence latching is not a production trigger. Every image enters by
   a deliberate, user-initiated act — an explicit camera capture or a file
   upload. The operator-run presence test remains diagnostic only. The legacy
   `FaceWatch` block in `hud/ui-src/core/vision-runtime.js` is retained but has
   no production starter; the quarantined presence watcher stays unwired.
2. **The goal:** the user sends Omega a shot with a **name** (+ optional
   info). Omega saves it to memory AND maps the face. Later the user sends a
   fresh pic and asks "who is this?" — Omega recalls the person through the
   facial models. Personas Omega already builds from conversation gain
   **visual perception**.
3. **Two fan-out ingest pipelines from one submission**, linked to the
   persona by name (§3).
4. **Models:** the selected offline or cloud multimodal mind seat handles
   scene description through the provider-neutral adapter. Face detection and
   embeddings use the repository's bundled CPU ONNX models. The configured
   image tool is `flux2_klein`; its deployment-owned authoring/runtime details
   are not a repository path. No new caption models. The ONNX CLIP-class embedding encoder
   is **deferred** — people + descriptions need no vector index; it returns
   when semantic image search ("find that diagram") is wanted.

## 2. Existing Assets (Verified 2026-09-10)

| Asset | Where | State |
|---|---|---|
| Face detect/embed/landmarks (SCRFD + ArcFace 512-d) | CPU ONNX via `runtime/face/{recognizer,recognition,anchor_store,lifecycle}.py` and `models/onnxs/face/` | LIVE |
| Anchor store (name → embedding, cosine id, 0.4 threshold) | `guardian/state/face_anchors.json` | LIVE, plaintext |
| HUD entry surfaces | chat camera flyout (`omega-ui.js` "face memory · webcam" → `/api/vision/upload-face`); settings "face memory" card (name + info + photo) | LIVE |
| Lobe-7 write path + persona back-link | `hud/server.py` `_build_face_memory_envelope` → `[7,1,0]` + sidecar + anchor enroll | LIVE |
| VLM describe | `runtime/vision.py` → selected offline LM Studio or configured cloud multimodal seat | LIVE for chat uploads and `vision_reasoning_tool` |
| Image generation | `image_tool` → configured `flux2_klein` seat → `runtime/image_store.py` → `guardian/state/images/<date>/` | LIVE; generated output is sent to the description hook |
| `remember_vision` / `LANE_VISION` / `modality="vision"` | MRIL routing exists, but `NON_RECALLABLE_LOBES` still closes general lobe-7 recall | REGISTERED / RECALL DEFERRED |
| `core.face_recall_warmup` | sidecar reader + AnchorStore restore | LIVE |

## 3. The contract

### One submission, two pipelines, one linkage

```
user submission = {image, name?, info?, intent: remember | ask}
        │
         ├── PIPELINE A — semantic (live fan-out)
         │     runtime.vision → description text
         │     → SourceLedger artifact (raw description + provenance)
         │     → Guardian external-ingress screen (taint-by-default: the
         │       description derives from an image the model READ — treat as
         │       untrusted content, same as calendar titles)
         │     → lobe-7 record [7,2,0] scene_descriptions
         │       (payload: screened description, image path pointer, hashes)
        │
         ├── PIPELINE B — biometric (live)
         │     face models: detect → quality/score → 512-d embedding
         │     → anchor store enroll under name (running-mean, capped)
         │     → thumbnail under memory/700_vision/images/
         │     → lobe-7 record [7,1,0] face_memory + embedding sidecar under
         │       memory/700_vision/face_embeddings/ (canonical records do not
         │       hold embedding bytes)
         │
         └── LINKAGE (live records, target block deferred)
               a named face receives a persona-face link record in lobe 1;
               a named scene receives a persona visual-description note.
               The current source does not mutate one `visual` enrichment
               block on a persona record. The persona lane is truth for
               identity; lobe 7 holds artifacts; AnchorStore holds the
               biometric index.
```

Info text ("relationship, role, notes") rides the EXISTING persona intake —
it is operator-authored conversational fact, not image-derived content, so
it flows the normal persona/memory gates, not the vision screen.

### "Who is this?" (intent: ask)

```
photo → `pressure_loop/operator_vision.py` → face models → AnchorStore match
  ├─ hit (≥ 0.4): person name → persona recall (lobe 1 — fully recallable)
  │    Answer: "That's <name> — <persona facts>", with confidence.
  └─ miss: honest unknown + offer to enroll ("I don't know this face —
       want me to remember them? Give me a name.")
```

**Boundary preserved:** lobe 7 stays OUT of general query recall. The current
identity answer uses the face anchor and persona graph; a dedicated scene
linkage/recency reader is not yet implemented. No change to
`NON_RECALLABLE_LOBES`.

### Generated-image ingest (same pipeline A)

`IMAGE_CREATION` results run pipeline A on completion (describe → raw ledger →
screen → [7,2,0], tagged `origin: generated`, linked to the originating
episode) — Omega remembers what it makes. Uploads (non-face) may enter the
same way from the existing upload surface.

## 4. Failure modes (fail-closed)

- No face detected on `remember` with a name → refuse enrollment, say so. A
  separately submitted non-face image can still use the semantic upload path.
- The current recognizer selects its largest detected face; a separate
  multi-face refusal rule is **not implemented** and remains a hardening item.
- VLM describe fails → pipeline B proceeds; description ref absent; loud log.
- SourceLedger write fails → pipeline A WITHHELD entirely (ratified sensory
  order); pipeline B unaffected (biometric artifacts are not cognitive text).
- AnchorStore currently uses the 0.4 threshold. There is no separate
  threshold-plus-margin ambiguous band in the live source; do not claim that
  behavior until it is implemented and verified.

## 5. Current and Deferred Work

**Implemented in the current source:**

1. CPU face detect/embed/enroll/identify through `runtime/face/`.
2. Face lobe-7 records, embedding sidecars, thumbnails, AnchorStore
   persistence, and `core.face_recall_warmup` on HUD startup.
3. Semantic image ingest through `sensory.vision_ingest`: raw SourceLedger
   record, Guardian screen, `[7,2,0]` scene record, and optional persona note.
4. Explicit HUD upload/settings routes and the chat "who is this" path through
   `pressure_loop/operator_vision.py`.
5. Generated-image description after the live AUTO `IMAGE_CREATION` path.

**Deferred or not implemented:**

- General lobe-7 scene recall. `remember_vision` is registered and routed, but
  the general recall boundary still denies lobe 7.
- A single persona `visual` enrichment block and ID/recency scene reader.
- Multi-face enrollment refusal and an ambiguous-match margin beyond the 0.4
  AnchorStore threshold.
- An ONNX image-embedding encoder/vector index for semantic image search and
  any DeepSleep re-caption pass.

## 6. Open decisions

- **Face data at rest** (operator ruling pending): recommendation — encrypt
  anchors + embedding sidecars (AES-256-GCM vault pattern, as tokens/send
  previews); display thumbnails stay plaintext behind the HUD path guard.
- Presence watcher and legacy FaceWatch code removal timing (prune versus leave
  quarantined); neither is a production trigger.

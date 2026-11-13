# Face Model Integration Map

**Purpose:** Complete mapping of how face models are used and called through the Omega system.

**Date:** 2026-06-08  
**Branch:** main

---

## Overview

The face recognition system integrates with Omega at multiple layers:
1. **HUD Layer:** Webcam capture and file upload interfaces
2. **API Layer:** Server endpoints for enrollment and recognition
3. **Recognition Layer:** Runtime service (ArcFace) + brain anchor fallback
4. **Storage Layer:** Multiple persistence locations for embeddings
5. **Model Layer:** Omega's cognitive model can be aware of face recognition results
6. **Memory Layer:** Vision lobe integration for recall and context

---

## Face Model Call Chain

### 1. Runtime Service (Out-of-Process)
**Location:** `http://127.0.0.1:11434` (omega-portable-lite)

**Model:** ArcFace (512-dim face embeddings)

**Endpoints:**
- `POST /face/identify` - Detect face, generate embedding, match against runtime DB
- `POST /face/enroll` - Add face to runtime DB

**Client:** `runtime/client.py:RuntimeClient`

**Call Path:**
```
runtime/client.py:RuntimeClient.face_identify(image_bytes)
  → HTTP POST http://127.0.0.1:11434/face/identify
  → omega-runtime ArcFace model
  → Returns {name, confidence, bbox, embedding, face_count, reason}
```

**When Called:**
- HUD settings panel enrollment (`POST /api/vision/upload-face`)
- HUD settings panel identification (`POST /api/vision/identify-upload`)
- Legacy enrollment (`POST /api/vision/enroll`)
- Face identification (`POST /api/vision/identify`)

---

### 2. Brain-Side Recognition Module
**File:** `runtime/face/recognition.py`

**Functions:**

#### `identify(image_bytes: bytes) -> dict`
**Purpose:** Identify face with runtime + brain anchor fallback

**Call Path:**
```
identify(image_bytes)
  → get_runtime_client().face_identify(image_bytes)
  → omega-runtime HTTP call
  → _apply_brain_anchor_fallback(result)
  → anchor_store.identify(embedding) if runtime has no match
  → Returns {name, recognized_name, confidence, bbox, embedding, ...}
```

**When Called:**
- HUD server: `api_vision_identify()`
- HUD server: `api_vision_identify_upload()`
- HUD server: `api_vision_enroll()` (to get embedding before enrollment)

#### `enroll(image_bytes: bytes, name: str) -> dict`
**Purpose:** Enroll face in runtime and brain anchor store

**Call Path:**
```
enroll(image_bytes, name)
  → identify(image_bytes) to get embedding
  → get_runtime_client().face_enroll(image_bytes, canonical_name)
  → persist_brain_anchor(canonical_name, embedding)
  → anchor_store.enroll(name, embedding)
  → Save thumbnail to guardian/state/face_thumbnails/{name}.png
  → Returns {ok, name, sample_count, anchored_at, bbox, ...}
```

**When Called:**
- HUD server: `api_vision_enroll()` (legacy webcam enrollment)

#### `persist_brain_anchor(name: str, embedding: list) -> dict`
**Purpose:** Write embedding to brain-side anchor store

**Call Path:**
```
persist_brain_anchor(name, embedding)
  → anchor_store.enroll(name, np.array(embedding))
  → Write to guardian/state/face_anchors.json
  → Returns {name, sample_count, anchored_at}
```

**When Called:**
- HUD server: `api_vision_remember_face()` (after vision lobe write)
- HUD server: `api_vision_upload-face()` (via remember-face)
- Runtime recognition: `enroll()` (after runtime enrollment)

---

### 3. Anchor Store (Brain-Side Persistence)
**File:** `runtime/face/anchor_store.py`

**Storage:** `guardian/state/face_anchors.json`

**Functions:**

#### `enroll(name: str, embedding: np.ndarray) -> dict`
**Purpose:** Add or update anchor with running mean

**Call Path:**
```
enroll(name, embedding)
  → Normalize embedding to unit vector
  → If name exists: merge with running mean (capped at 5 samples)
  → If name new: store fresh embedding
  → Atomic write via temp file rename
  → Returns {name, sample_count, anchored_at}
```

**When Called:**
- Runtime recognition: `persist_brain_anchor()`
- Boot warmup: `core.face_recall_warmup()` (from vision lobe)

#### `identify(embedding: np.ndarray) -> dict`
**Purpose:** Find best match via cosine similarity

**Call Path:**
```
identify(embedding)
  → Normalize query embedding
  → For each stored anchor: compute cosine similarity
  → Sort by confidence descending
  → Return top match if confidence >= 0.4
  → Returns {recognized_name, confidence, candidates, threshold}
```

**When Called:**
- Runtime recognition: `_apply_brain_anchor_fallback()`
- HUD server: `api_vision_identify()` (cold-boot fallback)
- Boot warmup: (not called directly, used for persistence)

---

### 4. HUD Server API
**File:** `hud/server.py`

#### Enrollment Endpoints

**`POST /api/vision/upload-face`** (NEW)
**Purpose:** Upload face image → recognize → store embedding

**Call Path:**
```
POST /api/vision/upload-face (file, name, context)
  → Read image_bytes from UploadFile
  → runtime.face.recognition.identify(image_bytes)
  → Get 512-dim embedding
  → Convert to base64
  → api_vision_remember_face({name, context, embedding, bbox, image_b64})
  → Write to vision lobe (7,1)
  → Write sidecar to memory/700_vision/face_embeddings/
  → Write persona back-link (1,0,person_id)
  → persist_brain_anchor(name, embedding)
  → ensure_presence_watcher_running()
  → Returns {ok, name, person_id, vision_coord, ...}
```

**When Called:**
- HUD settings panel: "upload & recognize face" button

**`POST /api/vision/remember-face`**
**Purpose:** Store face with pre-computed embedding

**Call Path:**
```
POST /api/vision/remember-face (name, context, embedding, bbox, image_b64)
  → route_persona(name) → (1,0,person_id)
  → Write vision-lobe entry (7,1) with embedding
  → Write sidecar to memory/700_vision/face_embeddings/
  → Write persona back-link (1,0,person_id)
  → persist_brain_anchor(name, embedding)
  → ensure_presence_watcher_running()
  → Returns {ok, name, person_id, vision_coord, ...}
```

**When Called:**
- HUD server: `api_vision_upload-face()` (after recognition)
- Legacy: FaceWatch enrollment (retired)

**`POST /api/vision/enroll`** (LEGACY)
**Purpose:** Webcam enrollment bypassing vision lobe

**Call Path:**
```
POST /api/vision/enroll (image_b64, name)
  → Decode image
  → runtime.face.recognition.enroll(image_bytes, name)
  → ensure_presence_watcher_running()
  → Returns {ok, name, sample_count, anchored_at, bbox, ...}
```

**When Called:**
- Legacy FaceWatch (retired)

#### Recognition Endpoints

**`POST /api/vision/identify-upload`** (NEW)
**Purpose:** Upload face image → return recognized name

**Call Path:**
```
POST /api/vision/identify-upload (file)
  → Read image_bytes from UploadFile
  → runtime.face.recognition.identify(image_bytes)
  → Returns {ok, recognized_name, confidence, candidates, threshold}
```

**When Called:**
- HUD settings panel: "who is this?" button

**`POST /api/vision/identify`**
**Purpose:** Identify face in base64 image

**Call Path:**
```
POST /api/vision/identify (image_b64)
  → Decode image
  → runtime.face.recognition.identify(image_bytes)
  → Brain anchor fallback if runtime has no match
  → Translate runtime contract to browser contract
  → Returns {ok, face_count, recognized_name, confidence, faces, ...}
```

**When Called:**
- Legacy FaceWatch (retired)
- Browser face identification (if re-enabled)

#### Query Endpoints

**`GET /api/vision/anchors`**
**Purpose:** List enrolled faces with thumbnails

**Call Path:**
```
GET /api/vision/anchors
  → runtime.face.recognition.known_names()
  → For each name: check thumbnail existence
  → Returns {ok, anchors: [{name, thumbnail_url}]}
```

**When Called:**
- HUD settings panel: "refresh names" button

**`GET /api/vision/status`**
**Purpose:** Check face model availability

**Call Path:**
```
GET /api/vision/status
  → runtime.face.recognition.face_status()
  → Returns {ok, runtime, anchor_count, known_names}
```

**When Called:**
- HUD presence check (if re-enabled)

---

### 5. Vision Lobe Integration
**Location:** `memory/700_vision/face_embeddings/`

**Purpose:** Persistent face memories with full context and persona links

**Schema:**
```json
{
  "name": "alice",
  "person_id": "alice_abc123",
  "embedding": [512 floats],
  "embedding_dim": 512,
  "bbox": [x,y,w,h],
  "image_b64": "base64 JPEG",
  "cycle_id": "face-remember-abc123",
  "vision_path": "memory/700_vision/face_embeddings/..."
}
```

**Call Path:**
```
HUD server: api_vision_remember_face()
  → Write sidecar to memory/700_vision/face_embeddings/{cycle_id}.embedding.json
  → Write canonical memory entry (7,1) with type: "face_memory"
  → Write persona back-link (1,0,person_id)
```

**When Called:**
- HUD server: `api_vision_remember_face()`
- HUD server: `api_vision_upload-face()` (via remember-face)

**Boot Warmup:**
```
core.face_recall_warmup()
  → Walk memory/700_vision/face_embeddings/*.embedding.json
  → For each sidecar: anchor_store.enroll(name, embedding)
  → Populate guardian/state/face_anchors.json from vision lobe
```

---

### 6. Model Integration (Current State)

**Current Limitation:** Omega's cognitive model is **NOT** directly aware of face recognition results. Face recognition is a separate system that stores embeddings, but the model doesn't receive face recognition context in its prompts.

**Current Flow:**
```
User uploads face → Recognition → Storage
  ↓
Model is NOT informed of face recognition results
  ↓
Model cannot say "I recognize you, Alice"
```

**What's Missing:**
1. Face recognition results need to be injected into model prompts
2. Model needs context about who is currently present
3. Chat panel needs to capture faces and pass recognition to model
4. Model needs to be able to reference face memories in its responses

---

## Integration Points for Model Awareness

### 1. Presence System Integration
**File:** `sensory/operator_presence.py`

**Current State:**
- `enrolled_operator_name()` returns current enrolled operator name
- Face enrollment triggers `ensure_presence_watcher_running()`
- Presence watcher uses face recognition for operator detection

**Missing:**
- Model prompts don't include presence context
- Chat system doesn't query presence before model calls

**Proposed Integration:**
```
Chat → Check presence → If face recognized → Inject into model prompt
  → "Operator present: Alice (confidence: 0.85)"
```

### 2. Chat Panel Integration
**File:** `hud/omega_interface.html`

**Current State:**
- Chat panel has text input only
- No webcam capture in main panel
- Face recognition only in settings panel

**Missing:**
- Webcam toggle in chat panel
- "Take picture" button
- Face capture → recognition → model prompt flow

**Proposed Integration:**
```
Chat panel webcam toggle
  → User clicks "take picture"
  → Capture frame → POST /api/vision/identify-upload
  → If recognized: inject into chat as system message
  → Model sees: "Face recognized: Alice (confidence: 0.82)"
  → Model can respond: "Hello Alice, I recognize you"
```

### 3. Memory Tool Integration
**Files:** `auto_loop/mril_tools/`

**Current State:**
- MRIL tools can query face memories via subdomain routing
- Model can use `remember_face` tool to store faces
- Model can use `recall_face` tool to retrieve faces

**Missing:**
- Model doesn't automatically use face context
- No automatic face-to-persona linking in model prompts

**Proposed Integration:**
```
Model prompt includes:
  - Current recognized faces (if any)
  - Enrolled operator name
  - Recent face recognitions from chat
```

---

## Complete Face Model Call Tree

```
USER ACTION
  ↓
HUD Chat Panel (webcam capture)
  ↓
POST /api/vision/identify-upload (file)
  ↓
runtime.face.recognition.identify(image_bytes)
  ↓
runtime.client.face_identify(image_bytes)
  ↓
HTTP POST http://127.0.0.1:11434/face/identify
  ↓
omega-runtime ArcFace model
  ↓
Returns {name, confidence, bbox, embedding, face_count}
  ↓
_apply_brain_anchor_fallback(result)
  ↓
anchor_store.identify(embedding) if runtime has no match
  ↓
Cosine similarity vs guardian/state/face_anchors.json
  ↓
Returns {recognized_name, confidence, candidates}
  ↓
HUD receives recognition result
  ↓
INJECT INTO MODEL PROMPT (MISSING)
  ↓
Model sees: "Face recognized: Alice (confidence: 0.82)"
  ↓
Model responds: "Hello Alice, I recognize you"
```

---

## Storage Flow

```
FACE ENROLLMENT
  ↓
POST /api/vision/upload-face (file, name, context)
  ↓
runtime.face.recognition.identify(image_bytes)
  ↓
Get 512-dim embedding
  ↓
api_vision_remember_face({name, context, embedding, bbox, image_b64})
  ↓
Write to THREE locations:
  1. Vision lobe (7,1) - canonical memory entry
  2. Sidecar: memory/700_vision/face_embeddings/{cycle_id}.embedding.json
  3. Brain anchor: guardian/state/face_anchors.json
  4. Persona back-link: memory/100_personas/{person_id}/
  5. Thumbnail: guardian/state/face_thumbnails/{name}.png
  6. Runtime DB: omega-runtime internal (via face_enroll)
  ↓
MULTI-LAYER PERSISTENCE FOR REDUNDANCY
```

---

## Boot-Time Warmup

```
boot.py:run_continuous()
  ↓
core.face_recall_warmup()
  ↓
Walk memory/700_vision/face_embeddings/*.embedding.json
  ↓
For each sidecar:
  - Read name, person_id, embedding
  - anchor_store.enroll(name, embedding)
  ↓
guardian/state/face_anchors.json populated
  ↓
Cold-boot recognition available without runtime restart
```

---

## Recognition Threshold Contract

**Critical:** Both brain-side and runtime-side must use the same threshold (0.4).

**Locations:**
- Brain: `runtime.face.anchor_store.RECOGNITION_THRESHOLD = 0.4`
- Runtime: `omega-runtime _face_db_match = 0.4`

**Why:** Cold-boot recognition (brain) and warm recognition (runtime) must agree on identity for the same face.

**Harmonization:** 2026-05-14 (build_log 0117)

---

## Summary

**Current State:**
- Face models work correctly for enrollment and recognition
- Multi-layer storage ensures persistence
- Recognition works via runtime + brain anchor fallback
- **BUT:** Model is NOT aware of face recognition results

**Missing Integration:**
1. Chat panel webcam capture
2. Face recognition → model prompt injection
3. Model awareness of current recognized faces
4. Automatic face-to-persona context in model responses

**Required for Full Integration:**
1. Add webcam toggle to chat panel
2. Add "take picture" button to chat panel
3. Integrate face recognition into chat flow
4. Inject recognition results into model prompts
5. Add face context to memory tool results
6. Enable model to reference face memories in responses

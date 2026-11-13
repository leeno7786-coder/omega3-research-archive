# Face Recognition System Pipeline Map

**Purpose:** Complete mapping of face recognition from HUD settings panel through runtime models, storage, and recall.

**Date:** 2026-06-08  
**Branch:** main

---

## Overview

The face recognition system enables Omega to:
1. **Enroll faces** via webcam capture from the HUD settings panel
2. **Store face embeddings** in multiple locations for redundancy
3. **Recognize faces** in real-time via browser capture or runtime service
4. **Recall faces by name** through memory integration with persona system

---

## Pipeline Components

### 1. HUD Settings Panel (Browser)
**File:** `hud/omega_interface.html`

**Entry Point:** Settings → Operator Profile → Face Memory section

**UI Elements:**
- "add named face memory" button
- "refresh names" button  
- "start recognition watcher" button
- Status display: "idle · models not loaded" / "active · watching"

**JavaScript Objects:**
- `window.OmegaEnrollment.prompt(mode, options)` - enrollment prompt overlay
- `window.FaceWatch` - webcam capture and face enrollment manager

**Flow:**
```
User clicks "add named face memory"
  → OmegaEnrollment.prompt('operator_profile')
  → FaceWatch.rememberMe(name, {context})
  → Browser webcam capture (getUserMedia)
  → POST /api/vision/remember-face
```

---

### 2. HUD Server API
**File:** `hud/server.py`

#### Endpoint: `POST /api/vision/remember-face`
**Purpose:** Enroll a face with name and context, persist to vision lobe and anchor store

**Request Body:**
```json
{
  "name": "string (required)",
  "context": "string (optional)",
  "embedding": "[512 floats] (optional, derived from image_b64 if missing)",
  "bbox": "[x,y,w,h] (optional)",
  "image_b64": "base64 JPEG/PNG (optional)"
}
```

**Processing Steps:**
1. Validate name required
2. If embedding missing but image_b64 present → derive via `/face/identify`
3. Create persona coord via `route_persona(name)` → `(1,0,person_id)`
4. Write vision-lobe face entry to `(7,1)` with:
   - `type: "face_memory"`
   - `payload: {name, person_id, embedding, embedding_dim, operator_context, bbox, image_b64, tags, links}`
   - `links: [{kind: "persona_link", person_id, person_name, coord}]`
5. Write sidecar to `memory/700_vision/face_embeddings/{cycle_id}.embedding.json` (cold-boot warmup)
6. Create persona back-link at `(1,0,person_id)` pointing to face memory
7. Enroll in brain anchor store via `persist_brain_anchor(name, embedding)`
8. Start presence watcher

**Response:**
```json
{
  "ok": true,
  "name": "string",
  "person_id": "string",
  "persona_coord": [1,0,person_id],
  "vision_coord": [7,1],
  "face_status": "staged",
  "face_path": "memory/700_vision/face_embeddings/...",
  "persona_ref": "memory/100_personas/..."
}
```

#### Endpoint: `POST /api/vision/identify`
**Purpose:** Identify face(s) in a base64-encoded image

**Request Body:**
```json
{
  "image_b64": "base64 JPEG/PNG (required)"
}
```

**Processing Steps:**
1. Decode base64 image
2. Call `runtime.face.recognition.identify(raw)` → runtime service
3. If runtime returns embedding but no match (cold boot), check brain anchor store
4. Translate runtime contract to browser contract

**Response:**
```json
{
  "ok": true,
  "face_count": 1,
  "recognized_name": "string or null",
  "confidence": 0.0-1.0,
  "faces": [{
    "bbox": [x,y,w,h],
    "score": 0.0-1.0,
    "recognized_name": "string or null",
    "confidence": 0.0-1.0,
    "candidates": [{name, confidence}],
    "threshold": 0.4,
    "embedding": [512 floats]
  }],
  "elapsed_seconds": 0.123
}
```

#### Endpoint: `POST /api/vision/enroll`
**Purpose:** Legacy enrollment endpoint (bypasses vision lobe, direct anchor store)

**Request Body:**
```json
{
  "image_b64": "base64 JPEG/PNG (required)",
  "name": "string (required)"
}
```

**Processing Steps:**
1. Decode image
2. Call `runtime.face.recognition.enroll(raw, name)`
3. Start presence watcher

#### Endpoint: `GET /api/vision/status`
**Purpose:** Check face model availability and known names

**Response:**
```json
{
  "ok": true,
  "runtime": "http://127.0.0.1:11434",
  "anchor_count": 5,
  "known_names": ["alice", "bob", "operator"]
}
```

---

### 3. Face Recognition Module
**File:** `runtime/face/recognition.py`

#### Function: `identify(image_bytes: bytes) -> dict`
**Purpose:** Identify face in image via runtime service with brain anchor fallback

**Flow:**
```
image_bytes
  → get_runtime_client().face_identify(image_bytes)
  → omega-runtime HTTP POST /face/identify
  → returns {name, confidence, bbox, embedding, face_count, reason}
  → _apply_brain_anchor_fallback(result)
  → if no runtime match but embedding present:
      → anchor_store.identify(embedding)
      → cosine similarity vs all stored embeddings
      → best match above 0.4 threshold wins
  → return {name, recognized_name, confidence, bbox, embedding, ...}
```

**Degraded Mode:** If runtime unavailable → returns `{degraded: true, reason: "face_runtime_unavailable"}`

#### Function: `enroll(image_bytes: bytes, name: str) -> dict`
**Purpose:** Enroll face in runtime and brain anchor store

**Flow:**
```
image_bytes + name
  → identify(image_bytes) to get embedding
  → get_runtime_client().face_enroll(image_bytes, canonical_name)
  → persist_brain_anchor(canonical_name, embedding)
  → save thumbnail to guardian/state/face_thumbnails/{name}.png
  → return {ok, name, sample_count, anchored_at, bbox, runtime_enrolled}
```

**Running Mean:** Repeated enrollments average new embedding into old (capped at 5 samples)

#### Function: `persist_brain_anchor(name: str, embedding: list) -> dict`
**Purpose:** Write embedding to brain-side anchor store

**Flow:**
```
name + [512 floats]
  → anchor_store.enroll(name, np.array(embedding))
  → guardian/state/face_anchors.json updated
  → return {name, sample_count, anchored_at}
```

#### Function: `forget(name: str) -> bool`
**Purpose:** Remove face from anchor store and thumbnail

**Flow:**
```
name
  → anchor_store.forget(name)
  → delete guardian/state/face_thumbnails/{name}.png
  → return true/false
```

#### Function: `face_status() -> dict`
**Purpose:** Runtime health and known names

**Response:**
```json
{
  "ok": true,
  "runtime": "http://127.0.0.1:11434",
  "anchor_count": 5,
  "known_names": ["alice", "bob", "operator"]
}
```

---

### 4. Anchor Store (Brain-Side Persistence)
**File:** `runtime/face/anchor_store.py`

**Storage Location:** `guardian/state/face_anchors.json`

**Schema:**
```json
{
  "alice": {
    "embedding": [512 floats],
    "anchored_at": "2026-06-08T12:00:00Z",
    "updated_at": "2026-06-08T12:05:00Z",
    "sample_count": 3
  },
  "bob": {
    "embedding": [512 floats],
    "anchored_at": "2026-06-08T11:30:00Z",
    "updated_at": "2026-06-08T11:30:00Z",
    "sample_count": 1
  }
}
```

#### Method: `enroll(name: str, embedding: np.ndarray) -> dict`
**Purpose:** Add or update anchor with running mean

**Algorithm:**
1. Normalize embedding to unit vector
2. If name exists:
   - Load old embedding and sample_count
   - Merge: `(old * samples + new) / (samples + 1)`
   - Renormalize
   - Cap samples at 5
   - Update `updated_at`
3. If name new:
   - Store embedding
   - Set `anchored_at` and `updated_at`
   - Set `sample_count = 1`
4. Atomic write via temp file rename

#### Method: `identify(embedding: np.ndarray) -> dict`
**Purpose:** Find best match via cosine similarity

**Algorithm:**
1. Normalize query embedding
2. For each stored anchor:
   - Compute cosine similarity: `dot(query, stored)`
   - Score all anchors
3. Sort by confidence descending
4. Return top match if `confidence >= 0.4` else `null`

**Response:**
```json
{
  "recognized_name": "alice" or null,
  "confidence": 0.72,
  "candidates": [
    {"name": "alice", "confidence": 0.72},
    {"name": "bob", "confidence": 0.35}
  ],
  "threshold": 0.4
}
```

**Recognition Threshold:** `RECOGNITION_THRESHOLD = 0.4`
- Same-identity ArcFace embeddings: 0.55-0.85
- Cross-identity: 0.05-0.30
- **Critical:** Must match omega-runtime `_face_db_match` threshold (cross-process contract)

---

### 5. Omega Runtime Service (Out-of-Process)
**Location:** `http://127.0.0.1:11434` (omega-portable-lite)

**Endpoints:**
- `POST /face/identify` - Detect and embed faces, match against runtime face DB
- `POST /face/enroll` - Add face to runtime face DB

**Model:** ArcFace (512-dim embeddings)

**Runtime Face DB:** Internal to omega-runtime process (not brain-side)

**Client:** `runtime/client.py:RuntimeClient.face_identify()` / `face_enroll()`

---

### 6. Storage Locations

#### Primary Storage (Brain-Side)
**Location:** `guardian/state/face_anchors.json`

**Purpose:** Cold-boot recognition bridge, persistence across runtime restarts

**Content:** Name → {embedding, anchored_at, updated_at, sample_count}

**Writer:** `runtime.face.anchor_store.AnchorStore`

**Reader:** `runtime.face.recognition.identify()` (fallback), `core.face_recall_warmup` (boot)

#### Vision Lobe (Memory System)
**Location:** `memory/700_vision/face_embeddings/{cycle_id}.embedding.json`

**Purpose:** Persistent face memories with full context, persona links

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

**Writer:** `hud/server.py:api_vision_remember_face()`

**Reader:** `core.face_recall_warmup` (boot warmup), memory API queries

**Canonical Memory Entry:** `(7,1)` with `type: "face_memory"` and persona links

#### Persona Back-Links
**Location:** `memory/100_personas/{person_id}/linked_face:{name}.json`

**Purpose:** Discoverable faces via persona search

**Schema:**
```json
{
  "title": "linked_face:alice",
  "summary": "Face memory anchored for alice.",
  "tags": ["face_link", "vision"],
  "links": [{
    "kind": "linked_face",
    "vision_coord": [7,1],
    "vision_path": "memory/700_vision/face_embeddings/..."
  }]
}
```

**Writer:** `hud.server.py:api_vision_remember_face()`

**Reader:** `core.persona_router`, memory API `search_by_decimal((1,0,person_id))`

#### Face Thumbnails
**Location:** `guardian/state/face_thumbnails/{name}.png`

**Purpose:** Visual reference for enrolled faces

**Writer:** `runtime.face.recognition.enroll()`

**Reader:** HUD face memory card display

#### Runtime Face DB
**Location:** Internal to omega-runtime process

**Purpose:** Warm recognition (runtime already loaded)

**Writer:** `runtime.client.face_enroll()`

**Reader:** `runtime.client.face_identify()`

---

## Complete Enrollment Pipeline

```
User Action (HUD Settings)
  ↓
Click "add named face memory"
  ↓
OmegaEnrollment.prompt('operator_profile')
  ↓
Enter name + context → submit
  ↓
FaceWatch.rememberMe(name, {context})
  ↓
Browser webcam capture (getUserMedia)
  ↓
Canvas capture → toDataURL('image/jpeg')
  ↓
POST /api/vision/remember-face
  {
    name: "alice",
    context: "colleague from work",
    image_b64: "data:image/jpeg;base64,..."
  }
  ↓
hud.server:api_vision_remember_face()
  ↓
If embedding missing → derive via runtime.face.recognition.identify()
  ↓
route_persona("alice") → (1,0,"alice_abc123")
  ↓
Write vision-lobe entry (7,1):
  - type: "face_memory"
  - embedding: [512 floats]
  - person_id: "alice_abc123"
  - links: [{kind: "persona_link", person_id: "alice_abc123"}]
  ↓
Write sidecar: memory/700_vision/face_embeddings/{cycle_id}.embedding.json
  ↓
Write persona back-link (1,0,"alice_abc123"):
  - type: "persona_face_link"
  - links: [{kind: "linked_face", vision_coord: [7,1]}]
  ↓
persist_brain_anchor("alice", embedding)
  ↓
anchor_store.enroll("alice", embedding)
  ↓
Write guardian/state/face_anchors.json
  ↓
runtime.client.face_enroll(image_bytes, "alice")
  ↓
Write omega-runtime face DB
  ↓
Save thumbnail: guardian/state/face_thumbnails/alice.png
  ↓
ensure_presence_watcher_running()
  ↓
Response: {ok: true, name: "alice", person_id: "alice_abc123", ...}
  ↓
HUD updates face memory card with enrolled name
```

---

## Complete Recognition Pipeline

### Hot Path (Runtime Available)
```
Browser capture (FaceWatch or getUserMedia)
  ↓
toDataURL('image/jpeg')
  ↓
POST /api/vision/identify {image_b64: "..."}
  ↓
hud.server:api_vision_identify()
  ↓
runtime.face.recognition.identify(image_bytes)
  ↓
runtime.client.face_identify(image_bytes)
  ↓
POST http://127.0.0.1:11434/face/identify
  ↓
omega-runtime:
  - Detect face (ArcFace)
  - Generate 512-dim embedding
  - Match against runtime face DB
  - Return {name, confidence, bbox, embedding, face_count}
  ↓
If runtime returns name → done
  ↓
If runtime returns embedding but no name (cold boot):
  ↓
_apply_brain_anchor_fallback()
  ↓
anchor_store.identify(embedding)
  ↓
Cosine similarity vs guardian/state/face_anchors.json
  ↓
Best match if confidence >= 0.4
  ↓
Return {recognized_name: "alice", confidence: 0.72, ...}
  ↓
HUD displays recognized name in FaceWatch
```

### Cold Path (Runtime Unavailable)
```
Browser capture
  ↓
POST /api/vision/identify
  ↓
runtime.face.recognition.identify()
  ↓
runtime client returns {degraded: true, reason: "face_runtime_unavailable"}
  ↓
HUD shows degraded state
  ↓
Recognition only available via brain anchor store
  (requires pre-enrolled faces in guardian/state/face_anchors.json)
```

---

## Boot-Time Warmup

**File:** `core.face_recall_warmup` (referenced in code, implementation location varies)

**Purpose:** Load vision-lobe face embeddings into brain anchor store at boot

**Flow:**
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
guardian/state/face_anchors.json populated from vision lobe
  ↓
Cold-boot recognition available without runtime restart
```

---

## Face Recall by Name

### Memory API Query
```
memory.api.read_memory(
  coord=[7,1],
  filter={"type": "face_memory", "name": "alice"}
)
  ↓
Returns face memory entry with:
  - embedding
  - person_id
  - links to persona
  - image_b64 (thumbnail)
```

### Persona-Linked Query
```
memory.api.search_by_decimal((1,0,"alice_abc123"))
  ↓
Returns persona entries including:
  - linked_face entries
  - links to vision lobe (7,1)
  - full face context
```

### Direct Anchor Store Lookup
```
runtime.face.anchor_store.get_anchor_store().names()
  ↓
Returns ["alice", "bob", "operator"]
  ↓
Can retrieve specific anchor data via identify() with known embedding
```

---

## Integration Points

### Presence System
**File:** `sensory/operator_presence.py`

**Integration:**
- `enrolled_operator_name()` - returns current enrolled operator name
- Face enrollment triggers `ensure_presence_watcher_running()`
- Presence watcher uses face recognition for operator detection

### Memory System
**Files:** `memory/api.py`, `core/persona_router.py`

**Integration:**
- Face memories stored at `(7,1)` with persona links
- Persona back-links at `(1,0,person_id)` for discoverability
- MRIL tools can query face memories via subdomain routing
- Vision enrichment uses face recognition for context

### Runtime Client
**File:** `runtime/client.py`

**Integration:**
- `RuntimeClient.face_identify(image_bytes)` - HTTP to omega-runtime
- `RuntimeClient.face_enroll(image_bytes, name)` - HTTP to omega-runtime
- Health check via `RuntimeClient.health()`

---

## Recognition Threshold Contract

**Critical Cross-Process Contract:**

Both brain-side (`runtime.face.anchor_store.RECOGNITION_THRESHOLD = 0.4`) and runtime-side (`omega-runtime _face_db_match = 0.4`) must use the **same threshold**.

**Why:** Cold-boot recognition (brain anchor store) and warm recognition (runtime) must agree on identity for the same face. If thresholds drift, the same face could be recognized as "alice" by brain and "unknown" by runtime → silent split.

**Harmonization Date:** 2026-05-14 (build_log 0117 "Threshold harmonization")

**Future Work:** Live drift enforcement (brain reads runtime's effective threshold at warmup) - tracked in SESSION_FOLLOWUPS_2026-05-18.

---

## Failure Modes

### Runtime Unavailable
- **Symptom:** `face_identify()` returns `{degraded: true, reason: "face_runtime_unavailable"}`
- **Fallback:** Brain anchor store still works for pre-enrolled faces
- **Recovery:** Restart omega-runtime or use cold path only

### No Face Detected
- **Symptom:** `identify()` returns `{reason: "no_face_detected", face_count: 0}`
- **Cause:** No face in image, poor lighting, bad angle
- **Recovery:** Recapture with better conditions

### No Anchors Stored
- **Symptom:** `identify()` returns `{recognized_name: null, reason: "no_anchors"}`
- **Cause:** No faces enrolled yet
- **Recovery:** Enroll faces via settings panel

### Threshold Mismatch
- **Symptom:** Same face recognized differently by brain vs runtime
- **Cause:** Threshold drift between processes
- **Recovery:** Harmonize thresholds in both codebases

---

## Security & Privacy

**Consent Model:**
- Browser capture requires explicit user action (click "add named face memory")
- HUD server trusts that consent occurred (no additional consent check)
- Guardian consent gates apply to memory writes

**Data Storage:**
- Face embeddings stored locally (guardian/state, memory/)
- No cloud transmission of face data
- Thumbnails stored locally only

**Access Control:**
- Face data stored in guardian-state (guardian-sole-writer doctrine)
- Memory API enforces persona isolation
- No cross-operator face sharing

---

## Testing

**Unit Tests:**
- `tests/runtime/test_face_loader_anchor_fallback.py` - anchor fallback logic
- `tests/runtime/test_face_threshold_runtime_contract.py` - threshold contract

**Integration Tests:**
- HUD settings panel enrollment flow
- FaceWatch recognition loop
- Boot-time warmup from vision lobe

**Manual Testing:**
1. Enroll face via settings panel
2. Verify anchor store updated
3. Verify vision lobe entry created
4. Verify persona back-link created
5. Test recognition via FaceWatch
6. Test cold-boot recognition (runtime stopped)
7. Verify threshold consistency

---

## Summary

The face recognition system is a multi-layer pipeline:

1. **UI Layer:** HUD settings panel with FaceWatch enrollment
2. **API Layer:** HUD server endpoints for identify/enroll/remember-face
3. **Recognition Layer:** Runtime service (ArcFace) + brain anchor fallback
4. **Storage Layer:** 
   - Brain anchor store (guardian/state/face_anchors.json)
   - Vision lobe (memory/700_vision/face_embeddings/)
   - Persona back-links (memory/100_personas/)
   - Thumbnails (guardian/state/face_thumbnails/)
   - Runtime face DB (omega-runtime internal)
5. **Integration Layer:** Presence system, memory API, persona router

**Key Design Decisions:**
- Dual storage (runtime + brain) for cold-boot resilience
- Vision lobe integration for memory/persona convergence
- Running mean enrollment (capped at 5 samples) for improved accuracy
- Threshold contract for cross-process consistency
- Failure-soft degraded mode when runtime unavailable

**Recall by Name:**
- Memory API queries at `(7,1)` for face memories
- Persona queries at `(1,0,person_id)` for linked faces
- Anchor store direct lookup for enrolled names
- All paths converge on 512-dim ArcFace embeddings with 0.4 threshold

# runtime/face — brain-side CPU face inference + memory

**What it is** — The brain-side face recognition system. Face inference
(detect / recognize / embed) runs locally through CPU ONNX Runtime. DME owns
the NPU independently; the face path never selects or claims an NPU provider.
The package also owns the persistent name→embedding anchor store, lazy session
residency, and the public identify/enroll/forget API.

The live face path is brain-local. The generic runtime HTTP client is not the
owner of face inference; callers use this package's API directly.

**Key files**
- `recognizer.py` — the CPU SCRFD + ArcFace ONNX engine. It loads
  `models/onnxs/face/det_10g.onnx` and `w600k_r50.onnx` with
  `CPUExecutionProvider`, aligns the largest detected face, emits a 512-d
  embedding, and matches it against the brain anchor store.
- `recognition.py` — the load-bearing public API: `identify(image_bytes)`,
  `enroll(image_bytes, name)`, `forget(name)`, `known_names()`,
  `face_status()`, and `anchor_thumbnail_bytes(name)`. Enrollment queues the
  complete face observation through the ordinary operator ingress gate;
  AUTO admits it to episode SRAM and DeepSleep persists the canonical record.
  Thumbnail reads decode the canonical capture without writing another file.
  Every inference call wraps itself in a
  `face_inference_session()` bracket (from `lifecycle.py`) so face weights
  are resident only during active use. After a local inference, the result is
  cross-checked against the local `anchor_store` (`_apply_brain_anchor_fallback`)
  and the winning identity wins by cosine confidence.
- `lifecycle.py` — on-demand model residency for the local CPU process.
  `face_inference_session()` is a reference-counted context manager: the
  first enter calls `ensure_face_models_loaded()`; the last exit calls
  `release_face_models()` — CPU sessions are released immediately after
  inference. `face_model_ids()` reads the `face_tool` socket
  from the policy registry. Raises `RuntimeError("face_models_unavailable")`
  if models cannot be loaded, which callers convert to a degraded result.
- `anchor_store.py` — JSON-backed `name → {embedding:[512 floats],
  anchored_at, updated_at, sample_count}` map at
  `guardian/state/face_anchors.json`. Identification = cosine similarity
  (threshold `RECOGNITION_THRESHOLD = 0.4`, harmonized with the runtime's
  own cutoff) vs every stored embedding; best match above threshold wins,
  else `recognized_name: None`. `project_canonical` rebuilds each canonical
  identity in capture-time order, keeping record IDs with its running mean.
  Publishing the mean, count and IDs uses one temp-file rename. Repeated
  warmup and interrupted acknowledgement recovery do not double-count samples.
- `__init__.py` — re-exports the public symbols from `recognition.py`:
  `anchor_thumbnail_bytes`, `enroll`, `face_status`, `forget`, `identify`,
  and `known_names`.

The `.onnx` model files live under `models/onnxs/face/` as content-addressed
product assets, not Python source.

**Entry points / how it's used** — Import directly from the package:

```python
from runtime.face import identify, enroll, forget, known_names, face_status
result = identify(image_bytes)   # -> dict with recognized_name, confidence, …
```

Model loading is implemented by `recognizer.py`; there is no webcam-capture
path inside this package, so image bytes arrive from callers (upload, sensory
feed, or another operator surface).

**Depends on / depended-on-by** — Depends on `onnxruntime`, OpenCV, NumPy,
`pressure_loop.policy` (face model IDs via `runtime_tool_socket`),
`guardian/state/face_anchors.json` (anchor persistence — data, brain-side
by design), and `core.paths` (`MEMORY_ROOT`, `GUARDIAN_STATE_ROOT`).
Used by the vision / persona convergence wiring
and any caller that needs face identity; see `sensory/README.md`.

**Gotchas / doctrine**
- **Identity only, not a system lock:** enroll + identify attach names to
  embeddings for HUD / vision / memory. Operator presence on the pill is
  observability; it does not gate episode open or proactive speech.
- **Lazy load, immediate release:** face weights are never resident at idle.
  `face_inference_session()` is the sole bracket; callers must not load
  face sessions directly.
- **Anchor store is a derived projection** — canonical face payloads live in
  Dewey memory. DeepSleep refreshes the cache only after a successful canonical
  write; a refresh failure keeps the durable intake pending for retry.
- **Threshold is brain-owned:** `RECOGNITION_THRESHOLD` (0.4) in
  `anchor_store.py` is the single local face-match cutoff used by warm and
  cold-boot paths.
- Model unavailability returns an explicit failed result. Queue and projection
  failures preserve their cause; they cannot report successful enrollment.

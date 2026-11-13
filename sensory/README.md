# sensory - presence and visual intake boundary

`sensory/` owns small, deterministic perception adapters that sit between
operator input and the cognition queue. It does not own cognition, model-seat
selection, or memory writes.

## Live Modules

| Module | Role |
|---|---|
| `operator_presence.py` | Presence hysteresis, shared-state reads, and explicit operator-run presence observation. |
| `vision_ingest.py` | Scene-description intake for uploads, face-memory images, and generated images; it preserves vision-memory references and optional persona links. |
| `__init__.py` | Package marker only. |

Audio is not owned by this package. The live HUD audio routes are
`hud/server.py` -> `runtime.stt` and `runtime.tts`; the former
`sensory/voice_io.py` wrapper is absent and must not be recreated.

## Entry Points

- `sensory.operator_presence.PresenceMachine` is used by the HUD presence
  surface and its tests. The watcher is operator-run; boot does not start a
  webcam loop automatically.
- `sensory.operator_presence.read_presence_from_shared_state()` is read by
  `hud/server.py`, `hud/telemetry.py`, and `pressure_loop/pressure_field.py`.
- `sensory.vision_ingest.ingest_image_description()` is called by the
  upload/vision lane and routes scene description through `runtime.vision`.
- Face enrollment and recognition themselves are owned by `runtime.face`; the
  vision ingest module only stores the screened description and optional persona
  reference for a named image.
- Uploaded images and face artifacts remain under the portable state/staging
  roots resolved by `core.paths`; the module does not write arbitrary host paths.

## Ownership Boundaries

- `runtime/face/` owns brain-local CPU ONNX face sessions. Face inference never
  claims the DME NPU.
- `runtime/vision.py` owns provider-neutral scene description. It chooses the
  configured offline or cloud mind seat; `sensory/` only supplies the intake
  request and preserves the resulting reference.
- `memory/` owns canonical writes and recall. Vision memory is admitted through
  the normal memory contracts and uses modality `vision`.
- `hud/` owns HTTP routes, upload staging, and operator controls.

## Observability

Sensory operations emit through the Guardian feedback/data channels where the
owning module declares an event:

| Operation | Owner |
|---|---|
| Presence state transitions | `omega.sensory.presence_state` |
| Operator-run presence watcher | `omega.sensory.presence_watcher` |
| Vision intake and face handoff | `sensory.vision_ingest` plus the runtime face/vision events |

## Gotchas

- Presence is a downstream behavioral signal, not a second cognition ingress.
- Vision uploads must preserve the original image reference and the modality
  metadata needed for later lobe-7 recall.
- No module in `sensory/` may select a language model, bypass the queue, or
  write canonical memory directly.
- Use the current runtime clients for voice. Do not add a compatibility wrapper
  for the removed `sensory.voice_io` path.

# runtime — mind ↔ Omega local-services boundary

**Python (this repo loads):**

- `client.py` — thin HTTP client to the configured local-services process
  (`:11434`), plus the shared generate/voice/vision transport surface
- `runtime_capability_broker.py` — Preflight gating for all model calls (relocated from `core/`)
- `socket.py` — declarative sockets from `config/runtime.yaml`
- `brain_local.py`, `brain_cloud.py`, `mind_connector.py` — backend-neutral
  adapters for the operator-selected mind seat
- `stt.py`, `tts.py` — voice helpers (HTTP only)
- `qwen_tts.py` — runtime-owned Qwen3-TTS GGML sidecar supervisor;
  the child process owns the model context so a native backend failure cannot
  take down cognition or the HUD
- `tool_dispatch.py` — Deterministic tool dispatch surface (relocated from `core/`)
- `runtime_tool_dispatch.py` — Tool dispatch runtime integration (relocated from `core/`)
- `gpu_lease.py` — Process-local GPU-LLM lease; prevents hotswap thrash when a build holds the GPU slot (flag `OMEGA_GPU_BUILD_LEASE`, default ON)
- `face/recognition.py`, `face/recognizer.py`, `face/anchor_store.py` —
  brain-local CPU face inference and anchors; this is not a runtime HTTP model
  seat

## Current State / Legacy Findings

The current runtime surface is the thin transport/provider boundary, local
modality service adapter, model-control helpers, skill catalog/loader, and
brain-local face package described below. Some compatibility helpers remain
for tests or migration tooling but are not architecture ownership.

### Live modules

The runtime boundary is heavily exercised by cognition, execution, and the HUD:

- `client.py` — core HTTP client; called by `boot.py`, `cognition_chain.py`, all `omega_loop/*` runners, `auto_loop/execute/run.py`, `pressure_loop/`, `memory/dme/matmul_engine.py`, `hud/server.py`, `hud/telemetry.py`, and more. Memory semantic perception is owned by the in-brain Ettin retina, not this client.
- `socket.py` — declarative sockets; `invoke_runtime_generate` is used by the
  cognition, AUTO, Omega, pressure, and HUD paths; `invoke_runtime_tool` is
  used by `runtime_tool_dispatch.py`.
- `runtime_capability_broker.py` — preflight gating; called by `auto_loop/execute/run.py`, `sensory/operator_presence.py`, `hud/server.py`, `hud/telemetry.py`, `runtime/socket.py`, `runtime/face/recognition.py`, tests.
- `runtime_tool_dispatch.py` — runtime-backed Meta tool dispatch; called by `auto_loop/execute/run.py`.
- `model_control.py` — model handoff helpers; called by `boot.py`, `auto_loop/skill/run.py`, `hud/server.py`, `runtime/face/lifecycle.py`.
- `gpu_lease.py` — process-local GPU lease; called by `runtime/client.py`, `runtime/model_control.py`, `pressure_loop/operator_chat.py`, `pressure_loop/idle_wake.py`, tests.
- `qwen_skills.py` — historical-named SKILL.md catalog reader; called by
  `runtime/execution_skills.py`, `memory/capability_catalog.py`,
  `meta_loop/skill/run.py`, migration scripts, and tests.
- `execution_skills.py` — execution-skill loader; called by `auto_loop/skill/run.py`, `meta_loop/skill/run.py`, `runtime/user_execution_skills.py`, migration scripts, tests.
- `user_execution_skills.py` — user-skill trust policy; called by `meta_loop/skill/run.py` and tests.
- `stt.py`, `tts.py` — voice I/O; called by `hud/server.py` and `hud/telemetry.py`.
- `image_store.py` — generated-image persistence; called by `auto_loop/execute/run.py`, `hud/server.py`.
- `document_upload.py`, `document_templates.py`, `document_renderer.py`, `document_store.py` — document create pipeline; called by `auto_loop/execute/run.py`, `hud/server.py`, tests.
- `brain_cloud.py`, `brain_cloud_config.py`, `mind_connector.py` — backend-neutral mind connector; dynamically invoked by `runtime/client.py` for a configured cloud/external seat.
- `face/recognition.py`, `face/anchor_store.py`, `face/lifecycle.py` — face identification/enrollment; called by `sensory/operator_presence.py` (for `enrolled_operator_name`), `hud/server.py`, `hud/presence_test.py`, tests.

### Dead / stale / inert findings

1. **Historical model artifacts**
   Older classifier/model artifact directories are not inputs to the live
   runtime path. Current memory routing is deterministic and current semantic
   perception is the in-process binary retina.

2. **Dead helpers in otherwise-live modules**
   - `runtime/socket.py::invoke_runtime_service` / `resolve_runtime_service` — defined but never called.
   - `runtime/tool_dispatch.py::overlay_tool_call_args`, `overlay_web_fetch_args`, `model_query_from_args`, `stable_pick` — only referenced by docs/tests; production uses `deterministic_args_from_order` only.
   - Historical CLI handoff helpers remain only where tests or migration code
     still import them; they are not the live cognition route.
    - Some provider-state helpers remain only for compatibility and migration
      tooling; they are not the live cognition route.

### Docs / reality discrepancies

- The module list above separates the live transport/adapters from historical
  helpers; the historical model artifacts are not runtime inputs.

### `client.py` — Generate path + chat_template_kwargs

The runtime client posts to `/generate` on the local-services process (`:11434`)
when the selected mind seat is local. The mind connector can instead route
generation to the configured cloud provider; both adapters return the same
gateway-shaped body. All generate variants (`generate`, `generate_completion`,
`generate_stream`) thread the permitted request context through the body.

**Key parameters (mind-side):**
- `enable_thinking` — merged into `chat_template_kwargs` if the caller did not explicitly set it (`client.py:483-484`). Default `False` for structured paths; Omega opts back in to `True`.
- `stop` — list of stop strings passed directly in the payload when a caller
  supplies one.
- `timeout` — per-call override; Omega uses 900s.

**Mind path:**
- The local-services process owns Parakeet, TTS, and DME; it does not choose
  the language-model seat.
- LM Studio is launched from the SSD when the operator selects the local seat.
- Cloud requests use the provider and API key saved by the portable setup wizard.
- The response may carry a `reasoning` field, which `reason()` captures (`omega_loop/reason/run.py:96`).

**Failure behavior:** if the selected mind or local-services process is unavailable, the request returns an explicit failure. There is no silent in-process model fallback.

**Weights (local-services loads, mind does not):**

The portable layout carries modality assets in `Omega/models/` for DME,
Parakeet, and face recognition, plus the Qwen3-TTS payload at the sibling path
`../models/qwen3-tts` declared by `config/tts.yaml` and `models/manifest.json`.
Language-model weights are not a product default; a local model or cloud
provider/model is selected through deployment state and the mind connector.

Mind config only names **slots** in `config/runtime.yaml`; provider and model
selection are saved by the portable launcher.

### TTS engines and streaming

`config/tts.yaml` selects `engine: qwen` and `device: Vulkan0`. When the qwentts.cpp executable,
the Qwen talker GGUF, and the 12 Hz codec GGUF are all present, the runtime
starts the sidecar and reports `qwen3-tts` in `/health` and `/api/tts/status`.
Missing assets or a failed Vulkan load make TTS unavailable. The default Qwen root is
`../models/qwen3-tts`, which keeps the large payload at the SSD root beside an
`Omega3.0` checkout; `OMEGA_QWEN_TTS_ROOT` can override it for another portable
layout.

`POST /synthesise/stream` and `POST /api/tts/stream` expose the common low-
latency contract: raw signed 16-bit little-endian, mono PCM at 24 kHz. The
legacy `/synthesise` and `/api/tts/speak` WAV routes remain available for HTML
Audio and clients that need a complete RIFF response. Closing a stream cancels
the qwentts.cpp request through both async HTTP relays, including before the
first audio byte. Errors before audio return 502; errors after audio abort the
stream rather than send clean EOF. The HUD drops late PCM and text after a
barge-in and never retries a failed live sentence as WAV.

Boot loads the Qwen talker and codec, consumes a PCM warmup utterance, and runs
a one-second Parakeet transcription before the gateway starts serving. A failed
warmup prevents readiness and releases the constructed services. Both native
workers retain their models until gateway shutdown; there is no idle eviction.
`/health` exposes `resident` and `warmed` for both speech capabilities, and
`/api/tts/status` exposes those fields for Qwen. The HUD warmup request only
checks this receipt, so enabling voice does not queue another dummy utterance.

## Observability

All runtime operations emit via `guardian.terminal_feedback` (failure-soft, never halts cognition) with matching `guardian.data_logger.emit_event` JSONL durability where noted.

| Operation | Hook | Durable JSONL subsystem |
|-----------|------|------------------------|
| client health / generation | `emit_inference` | `omega.runtime.inference` |
| client generate | `emit_inference` | `omega.runtime.inference` |
| socket generate / tool | `emit_inference` | `omega.runtime.socket.*` |
| model_control load / unload / ensure / prepare / release | `emit_data` | `omega.runtime.model_control` |
| image_store persist / list | `emit_data` | `omega.runtime.image_store` |
| runtime_tool_dispatch | `emit_data` | `omega.runtime.tool_dispatch` |
| stt / tts | `emit_inference` | `omega.runtime.stt`, `omega.runtime.tts` |
| face recognition / anchor_store | `emit_inference` + `emit_data` | `omega.runtime.face.*` |
| runtime_capability_broker | `phase_span` + `emit_data` | `omega.runtime.capability` |

> Failure-log coverage: `docs/architecture/FAILURE_LOG_COVERAGE.md`

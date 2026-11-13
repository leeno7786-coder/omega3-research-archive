# Omega Runtime ↔ Brain Contract

**Status:** historical runtime design; superseded by `AGENTS.md` and the live
`runtime/` client/broker modules.
**Date:** 2026-06-16; residency and answer-composer amendment 2026-07-30.
**Audience:** the runtime rebuild (custom GPU/NPU binaries — llama.cpp + VitisAI/ONNX + stable-diffusion.cpp).

This spec records what the retired brain/qwen-bridge split required from the
model runtime on `127.0.0.1:11434`. It is retained for migration provenance,
not as an implementation authority. The live callers are `runtime/client.py`,
`runtime/model_control.py`, `boot.py`, `hud/server.py`, and
`runtime/face/lifecycle.py`; current ownership is defined by `AGENTS.md`.

> **Historical note.** There is no `/rerank` endpoint and no `rerank_model`. The old
> `embedding_gemma` `/embed` route is also retired; semantic perception now lives in
> the in-process Ettin retina. (The brain's *recall arbitration* — `reranker_engine`
> / `rerank_score` in `memory.api` — is internal deterministic scoring and is NOT part of this
> contract; it does not call the runtime.) Remove from the runtime: `endpoints.rerank` +
> `settings.orchestration.rerank_model` (omega3_models.json), `/rerank` + `RerankRequest` +
> `resolve_rerank_model_id` (routes_ops.py / endpoint_policy.py / orchestrator.py), and the
> `engine.rerank` methods.

---

## 0. Transport & base-URL resolution

- **Bind:** `127.0.0.1:11434` (loopback only).
- **Brain base-URL precedence** (`runtime/client.resolve_runtime_base_url`):
  1. `$OMEGA_RUNTIME_URL` (if set), else
  2. `config/runtime.yaml` `endpoint`, else
  3. `http://127.0.0.1:11434` (default).
- All requests use a `(connect, read)` timeout tuple: **connect capped at `min(5s, op_timeout)`**,
  read = the per-op budget below. The runtime must accept a connection fast even while a model loads.

## 1. Universal invariants (every cognitive endpoint)

1. **Fail-soft envelope.** Every POST returns JSON with a top-level **`ok: bool`**. `ok:false`
   (or a 5xx, or a transport error) makes the brain degrade gracefully (returns `None` /
   `classify`→`coord [5,0]` / silence). `ok:false` SHOULD carry `reason: str` (+ optional `detail`).
   5xx → brain reads `None`; 4xx → logged, body still parsed.
2. **`/health` is the local-runtime liveness + boot gate** (see §2). Local boot probes transport
   only and leaves the response model lazy; a missing model/load failure surfaces on first inference.
3. **Binary payloads are base64**: request `audio_b64` / `image_b64` / `file_b64`; response
   `audio_b64` / `image_b64` (brain base64-decodes to bytes).
4. **`chat_template_kwargs.enable_thinking`** is ALWAYS sent on `/generate*` and defaults to
   **`false`** ("reasoning off" doctrine — avoids `<think>` blocks burning the timeout). Honor it.
5. **Runtime owns sampler defaults.** The brain omits `temperature/top_p/top_k/max_tokens/stop`
   unless explicitly set; the runtime supplies canonical per-model defaults from its registry.
6. **`image/generate` omits `seed` entirely** when unset (do NOT require it; do not 422 on absent seed).

## 2. `GET /health` — liveness + boot gate

```
GET /health  →  200 {"status": "ok", "face": {"recognition_threshold": <float>}, ...}
```
- **`status == "ok"`** (exact string) is REQUIRED for the brain to consider the runtime live.
  Read by `RuntimeClient.health()` (cached 1.5s) AND `model_control._probe_runtime_reachable`
  (2s timeout, the boot gate `probe_cognition_runtime`).
- `face.recognition_threshold` (float) is optional; read for the `/face/identify` cosine cutoff.
- Also expected (HUD, non-fatal): `GET /telemetry`, `GET /detect`, `GET /models`.

---

## 3. Cognitive endpoints (brain → runtime)

These are the **native** routes. The model is resolved by a **fixed endpoint name baked into
each handler** (the request body carries NO `model` field) via the endpoint→model map (§5).

### 3.1 `POST /generate` — text (→ `gemma_e4b`)
```
req : {prompt:str, [max_tokens, temperature, top_p, top_k, stop:[str], system:str,
       tools:[dict], chat_template_kwargs:{enable_thinking:bool=false}]}
resp: {ok:bool, text:str, [reasoning, tokens_in, tokens_out, latency_ms,
       finish_reason:"stop"|"tool_calls"|"error", model_id, tool_calls:[dict], reason, detail]}
```
- Brain reads `text` (and `tool_calls`/`finish_reason` via `generate_completion`). 60s budget.
- **Native tool calling**: when `tools` provided and the model emits calls, return
  `finish_reason:"tool_calls"` + `tool_calls:[...]`.

### 3.2 `POST /generate/stream` — SSE token stream (→ `gemma_e4b`)
Same request (no `tools`). **SSE framing** (`event:` + `data:` JSON):
- `event: token` → `data: {token:str, ...}` (brain yields `data.token`)
- `event: done`  → `data: {finish_reason, tokens_out, latency_ms}` (stop)
- `event: error` → `data: {ok:false, reason, detail}` (brain raises `RuntimeStreamError` —
  this is how an empty completion is distinguished from a server error). Non-200 → empty iterator.

### 3.3 `POST /embed` — embeddings (→ `embedding_gemma`)
```
req : {text:str}                 # single
   or {texts:[str]}              # batch (same path, different key)
resp: {ok:bool, embedding:[float] | embeddings:[[float]], [dim, latency_ms], [reason]}
```
- `embedding_gemma` = EmbeddingGemma 300M ONNX q4, **NPU (VitisAI) → CPU fallback**. 5s budget.
- **This is the ONLY embedding path. No rerank.**

### 3.4 `POST /classify` — hierarchical SetFit lobe→subdomain (→ `setfit_*`)
```
req : {text:str, [force_lobe:int(0-11)]}
resp: {ok:bool, coord:[lobe:int, subdomain:int], label:str, confidence:float,
       degraded:bool, stages:{stage1:{...}, stage2:{...}}, latency_ms:float, [reason]}
```
- **Stage 1**: `setfit_lobe` SetFit classifier → `[lobe, 0]`.
- **Stage 2**: per-lobe specialist, chosen by lobe id:
  `{0: setfit_identity, 2: setfit_knowledge, 4: setfit_situational, 8: setfit_coding, 10: setfit_workflows}`.
  Lobes with no specialist return the stage-1 coord `[lobe, 0]`.
- `force_lobe=N` skips stage 1 and runs only lobe N's specialist.
- On any failure the brain defaults to **`coord [5, 0]`** — so the runtime SHOULD too (`fallback_coord`).
- All SetFit models are **CPU, pinned, auto-loaded at boot** (§5).

### 3.5 `POST /vision/describe` — VLM describe/VQA (→ `cosmos_reason2b`)
```
req : {image_b64:str, [video_b64, images_b64:[str], frame_count:int=6,
       prompt:str="Describe what you see.", system, max_tokens, temperature,
       image_token_budget:int∈{70,140,280,560,1120}=280]}
resp: {ok:bool, text:str, [reasoning, frames_used, tokens_out, latency_ms, reason, detail]}
```
- `cosmos_reason2b` = Cosmos-Reason2-2B GGUF **+ `mmproj-Cosmos-Reason2-2B-F16.gguf`** (multimodal).
  GPU hotswap actor, **`evict_after_use`** (§6). 30s budget. Default system = registry `vision_system`.

### 3.6 `POST /image/generate` — text-to-image (→ `sd_turbo`)
```
req : {prompt:str, [negative_prompt:str="", width:int, height:int, steps:int,
       seed:int (OMITTED when unset), cfg_scale:float, sampler:str]}
resp: {ok:bool, image_b64:str(PNG), [seed_used:int, format:"png", latency_ms], [reason]}
```
- `sd_turbo` = SD-Turbo Q8_0 `ggml_sd` (stable-diffusion.cpp). GPU, hotswap actor + heavy victim +
  `evict_after_use` (§6). 120s budget. Registry defaults: 512×512, steps 12, cfg 1.2, euler.

### 3.7 Audio / face / document (complete the surface)
| Route | Model | Request → Response (brain reads) | Budget |
|---|---|---|---|
| `POST /transcribe` | `whisper_small_en` | `{audio_b64, format="wav", language="auto", vad_filter=true}` → `{ok, text, language}`. Benign `reason` (no_speech/silence/vad/…) → brain treats as empty text, not error. | 30s |
| `POST /transcribe/stream` | `whisper_small_en` | SSE `word`/`error`/`done` | 30s |
| `POST /synthesise` | `jarvis_piper` | `{text, voice="jarvis", length_scale=1.0}` → `{ok, audio_b64, sample_rate, duration_s}` | 10s |
| `POST /voice/command` | `jackrong_qwen` + `whisper_small_en` | `{audio_b64, format, [tools, system, max_tokens, temperature]}` → `{ok, transcript, intent, tool_calls, text, language}`. Transcribe→generate in one call. | 60s |
| `POST /face/identify` | pipeline `[face_det_10g, face_w600k_r50]` | `{image_b64, format="jpeg"}` → `{ok, name|null, confidence, bbox, landmarks, [embedding(512)]}` | 10s |
| `POST /face/enroll` | pipeline `[det, arc]` | `{name, image_b64}` → `{ok}` | 10s |
| `POST /face/landmarks` | `face_det_10g` + (`face_2d106det`\|`face_1k3d68`) | `{image_b64, kind="2d106"\|"3d68"}` → `{ok, landmarks, kind}` | 10s |
| `POST /face/genderage` | `face_genderage` (+det) | `{image_b64}` → `{ok, gender, age}` | 10s |
| `POST /face/load` / `POST /face/unload` | group `face` | no body → `{ok, group:"face", loaded\|unloaded:[...]}` (bundle warm/free) | — |
| `POST /document/extract` | `document_extract` (builtin) | `{file_b64, filename, format="auto"}` → `{ok, text, format, page_count, metadata}` | 60s |

### 3.8 Compat surfaces (optional but expected by the bridge/HUD)
- **Ollama**: `GET /api/version|/api/tags|/api/ps`, `POST /api/show|/api/chat|/api/generate|/api/embed|/api/embeddings`.
  `resolve_ollama_model` maps aliases (`qwen3.5*`→`jackrong_qwen`, strip `:latest`); default chat = `jackrong_qwen`.
- **OpenAI v1**: `GET /v1/models[/{id}]`, `POST /v1/chat/completions` (**the qwen bridge `:8092`
  uses this** — multimodal-aware, SSE `chat.completion.chunk` + `[DONE]`), `POST /v1/embeddings`,
  `POST /v1/images/generations`, `POST /v1/classify`, `POST /v1/face/{embed,verify,genderage}`.
  `/v1/chat/completions` default model = `jackrong_qwen`. **No `/v1/rerank`.**

---

## 4. Model control & orchestration (brain → runtime)

The brain's `model_control.load_model`/`unload_model` are **route-agnostic** — they try a ladder
and accept the **first** route that doesn't 404/405/4xx. **The runtime only needs to implement
ONE load + ONE unload route** (any from the ladder). `context_size` is threaded as `num_ctx`
(best-effort, ignorable).

**Load ladder (first-ok-wins):**
```
POST /admin/models/{model}/load {}              POST /models/load {model|model_id}
POST /admin/load {model_id}                     POST /load {model|model_id}
POST /models/{model}/load {}
# fallbacks (warmup, 90s):
POST /api/generate {model, prompt:"OK", stream:false, keep_alive:"30m", options:{num_predict:1}}
POST /v1/chat/completions {model, messages:[{role:"user",content:"Reply OK."}], max_tokens:1, stream:false}
```
**Unload ladder:** the `/…/unload` mirror, plus `POST /api/generate {model, prompt:"", keep_alive:0}`
(Ollama-style eviction).

**Orchestration (admin, under `/admin/orchestration`):** `GET /state`, `GET /endpoints`,
`GET|PATCH /models/{id}/config`, `POST /models/{id}/load`, `POST /models/{id}/unload`,
`POST /hotswap {load, unload:[...], warmup}`, `POST /endpoints/{ep}/remap {model_id}`,
`POST /shutdown`, `POST /shutdown/force`.

**Brain lifecycle calls that exercise the above:**
- `probe_cognition_runtime()` — local **boot gate**: `/health` only; no response model load.
- `ensure_cognition_runtime()` — explicit loadability/warmup helper for callers that truly need it;
  `keep_resident=False` verifies and releases the seat.
- `prepare_dev_pill_runtime()` → unload `gemma_e4b`, load `jackrong_qwen` (APPLY/dev handoff).
- `release_dev_pill_runtime()` → unload `jackrong_qwen` (gemma lazy-reloads on next `/generate`).
- `ensure_face_models_loaded()` / `release_face_models()` → `/face/load` bundle (or per-model ladder).

---

## 5. Model registry & endpoint bindings

Endpoint → model (single unless noted). **No `rerank` row.**

| Endpoint | Model | Chip / backend |
|---|---|---|
| `generate` (+ default_generate) | **`gemma_e4b`** | GPU (llama.cpp Vulkan/ROCm) |
| `embed` | **`embedding_gemma`** | NPU (VitisAI) → CPU |
| `classify` | retired fallback route (`[5,0]`) | host |
| `intent` | **`intent_classifier`** | CPU |
| `vision_describe` | **`cosmos_reason2b`** (+mmproj) | GPU |
| `image_generate` | **`sd_turbo`** | GPU (sd.cpp) |
| `transcribe` | `whisper_small_en` | CPU |
| `synthesise` | `jarvis_piper` | CPU |
| `voice_command` | `jackrong_qwen` (+`whisper_small_en`) | GPU + CPU |
| `face_identify`/`face_enroll` | pipeline `[face_det_10g, face_w600k_r50]` | NPU → CPU |
| `face_landmarks` | `face_det_10g` + `face_2d106det`/`face_1k3d68` | NPU → CPU |
| `face_genderage` | `face_genderage` (+det) | NPU → CPU |
| `document_extract` | `document_extract` (builtin, no weights) | CPU |

`settings.orchestration`: `default_chat_model = gemma_e4b`, `default_generate_model = gemma_e4b`.
(Delete `rerank_model`.) `settings`: `max_concurrent_models = 4`, `eviction.idle_minutes = 10`.

### 5.1 Per-model lifecycle matrix

| Model | format / chip | load policy | residency | swap role | eviction |
|---|---|---|---|---|---|
| **gemma_e4b** | gguf / GPU | **lazy; first response use** | logical `RESIDENT_LLM`, pinned once loaded | swap **victim**; loading it evicts active hotswap actors | no boot load and no automatic post-swap restore while `auto_load:false`; next response reloads it |
| **scaffold_composer** | gguf / GPU | lazy; immediately before response inference | not pinned | **hotswap actor** → evicts gemma, then is evicted when gemma answers | one-shot by the adjacent response-seat load |
| **jackrong_qwen** | gguf+mmproj / GPU | lazy (register-only at boot) | not pinned | **hotswap actor** (`hotswap`+`cli_model`) → evicts gemma on load | **no `evict_after_use`** (persistent); idle/LRU eligible |
| **cosmos_reason2b** | gguf+mmproj / GPU | lazy | not pinned | **hotswap actor** | **`evict_after_use`** → unload + reload gemma |
| **sd_turbo** | ggml_sd / GPU | lazy | not pinned | **hotswap actor + heavy victim** | **`evict_after_use`**; also force-evicted to free VRAM for any LLM load |
| **embedding_gemma** | onnx / NPU→CPU | gateway fallback is lazy; Omega's in-process Harrier lane is managed separately | gateway entry not pinned | none | gateway idle/LRU eligible |
| **intent_classifier / dme_overlap_matmul** | setfit CPU / onnx NPU→CPU | auto-load at runtime boot | always resident | none | never |
| **jarvis_piper** | piper / CPU | **lazy** (auto_load:false) then **pinned** | always_resident once loaded | none | never (once loaded) |
| **whisper_small_en** | ct2 / CPU | **lazy** then **pinned** | always_resident once loaded | none | never (once loaded) |
| **face_det_10g / w600k_r50 / 2d106det / 1k3d68 / genderage** | onnx / NPU→CPU | auto_load at boot | **NOT pinned** | none | **idle/LRU evictable**, lazy-reload on next face request |
| **document_extract** | builtin / CPU | lazy (no weights, max_mem 0) | not pinned | none | harmless |

> Only **gemma_e4b, jackrong_qwen, cosmos_reason2b** are GPU GGUF LLMs (llama-worker eligible).
> **sd_turbo is `ggml_sd` → in-process only** (NOT a llama-server worker). The face/embed ONNX
> models target the **NPU (VitisAI EP)** with CPU fallback.

---

## 6. Swapping logic (only ONE GPU GGUF LLM resident at a time)

`RESIDENT_LLM = $OMEGA_RESIDENT_LLM or "gemma_e4b"`. Predicates:
- `is_resident_model(id)` = `id == RESIDENT_LLM` or `flags.resident_llm`.
- `is_hotswap_model(id)` = `id != RESIDENT_LLM` and (`flags.hotswap` or `flags.cli_model`).
- `swap_victims(id)` = `[]` if resident or not-hotswap; else `[RESIDENT_LLM] + flags.hotswap_unload` (deduped).

**`prepare_hotswap(target)`** runs at the top of every load (lazy first-request AND explicit):
1. If `target` IS the resident → unload every currently-loaded hotswap model (loading gemma back evicts qwen/cosmos), return.
2. Else unload each loaded victim (i.e. unload `gemma_e4b`).
3. Belt-and-suspenders: if target is hotswap, unload any *other* loaded GPU-GGUF LLM (only one GPU LLM at a time).
4. `release_igpu_for_hotswap(keep=target)` — free integrated-GPU workers.

**`restore_resident(swapped)`** (for `evict_after_use` models — cosmos, sd_turbo): unload the
one-shot model, then reload `gemma_e4b` only when `RESIDENT_LLM.auto_load` is true. With the current
`auto_load:false` policy, the seat stays free until the next response request loads gemma lazily.

**`free_vram_for_llm_load`** (separate from hotswap): before loading any GGUF LLM, force-unload any
loaded model in heavy groups `{vision, image, sd, diffusion, image_gen}` or format `ggml_sd`
(i.e. sd_turbo), skipping pinned entries.

## 7. Lazyload logic

`auto_load:false` models are **register-only at boot** (`tier:"lazy"` — a registry row + memory
reservation + resolved plan; no weights, no worker, no warmup). On first request the route resolves
the endpoint → `ensure_engine_registered(model_id)`:
1. `prepare_hotswap(model_id)` (so a lazy hotswap model evicts gemma first),
2. return early if already loaded,
3. if llama-pool enabled AND worker-eligible → spawn/reuse a llama-server worker (free VRAM + retry once),
4. else in-process `engine.load_model(...)`,
5. `ops.load_model` + `rm.touch`.
Pipelines (face) load every member; classify loads the lobe model + every specialist.

## 8. Eviction (three independent mechanisms)

1. **`evict_after_use`** (one-shot): unload the one-shot model after use; gemma is restored only when its `auto_load` policy allows it.
2. **Idle** (60s background sweep): unload non-pinned models idle ≥ `idle_minutes` (10). `rm.touch` resets the clock.
3. **LRU / capacity**: before a new load, if non-pinned loaded count > `max_concurrent_models` (4), unload oldest non-pinned.

**Pinned** means either the registry's logical default-generate resident (`gemma_e4b`) or an
explicit `always_resident` flag. The current boot-autoload policy set is the intent classifier and
DME overlap runtime component; Omega's Harrier embedding lane is in-process and outside this registry.

## 9. llama.cpp worker model (THE rebuild's core fix)

GPU GGUF LLMs (`gemma_e4b`, `jackrong_qwen`, `cosmos_reason2b`) run via an **out-of-process
`llama-server` worker**, gateway-proxied:
- **Binary**: `$LLAMA_SERVER_BIN` or harvested; `enabled()` when a binary resolves and
  `$OMEGA_GATEWAY_GGUF_WORKERS` allows (`auto`/`1`/`true`).
- **Port**: deterministic dynamic = `32800 + (abs(hash(model_id)) % 2000)`.
- **Spawn**: `llama-server --model <gguf> --host 127.0.0.1 --port N <loader_params> [--mmproj <abs>]`,
  `CREATE_NO_WINDOW`, `cwd = binary dir` (so `ggml-*.dll` resolve); poll `http://127.0.0.1:N/health` ≤90s.
- **Proxy**: gateway forwards `POST /v1/chat/completions` (+ SSE `data:` … `[DONE]`); normalizes
  `content` vs `reasoning_content` channels (Gemma/Qwen reasoning split).
- **Eligibility**: `format ∈ {gguf, ggml}`. **`sd_turbo` (`ggml_sd`) is NOT eligible → in-process.**

> ⚠️ **The bug this rebuild fixes:** the prior `llama-server` was built/launched **without real GPU
> offload** — every model reported `resolved_accelerator: cpu` / `CPUExecutionProvider`, dumping
> weights into VRAM but running inference on the CPU. The rebuilt llama.cpp server **MUST** be built
> with a working GPU backend (Vulkan or ROCm/HIP for the AMD Radeon iGPU) and honor `n_gpu_layers: -1`
> so gemma/qwen/cosmos actually run on the GPU. Likewise the ONNX EP for embed/face must resolve
> **VitisAI (XDNA2 NPU)** before CPU. Verify post-build via `GET /v1/models` →
> `resolved_accelerator` ≠ `cpu` for the GPU/NPU models.

## 10. Backend targets (the rebuild's runtime servers)

| Engine | Models | Acceleration |
|---|---|---|
| **llama.cpp server** (GGUF, GPU) | gemma_e4b, jackrong_qwen, cosmos_reason2b (+mmproj) | Vulkan or ROCm/HIP; `n_gpu_layers:-1`, flash_attn, q4 KV |
| **stable-diffusion.cpp** (in-process) | sd_turbo | GPU (cuda/vulkan/directml → cpu) |
| **ONNX Runtime** (in-process) | embedding_gemma, face_* | **VitisAI (NPU)** → CPU |
| **SetFit** (in-process, CPU) | setfit_lobe + 5 specialists | CPU |
| **faster-whisper / piper** (in-process, CPU) | whisper_small_en, jarvis_piper | CPU |

---

## 11. APPLY ↔ hotswap integration (once the runtime is up)

When the brain runs an autonomous APPLY, it must yield the GPU to qwen and reclaim it after:
- before dispatch: `prepare_dev_pill_runtime()` → unload `gemma_e4b`, load `jackrong_qwen`.
- dispatch: the qwen bridge (`:8092`) → runtime `/v1/chat/completions` (model `jackrong_qwen`).
- after: `release_dev_pill_runtime()` → unload `jackrong_qwen`; `gemma_e4b` lazy-reloads on the next `/generate`.

The runtime's own `prepare_hotswap`/`restore_resident` (§6) is the safety net so only one GPU LLM is
ever resident — but the brain drives the prepare/release around the APPLY explicitly. **Clean teardown
matters**: the runtime must not orphan a `llama-server` worker holding VRAM when its parent dies
(the orphan-reaper / `reap_stale_subprocess_ports` on boot covers restarts; a true process-tree kill
of workers on shutdown is the belt-and-suspenders).

---

### Quick conformance checklist for the rebuilt runtime
- [ ] `GET /health` → `{"status":"ok", "face":{"recognition_threshold":…}}`
- [ ] `POST /generate` (+`/generate/stream` SSE token/done/error) → gemma_e4b, **on GPU**
- [ ] `POST /embed` (text + texts) → embedding_gemma, **on NPU**
- [ ] `POST /classify` retired fallback and `POST /classify/intent` → intent_classifier
- [ ] `POST /vision/describe` → cosmos_reason2b (+mmproj), evict_after_use
- [ ] `POST /image/generate` → sd_turbo, evict_after_use, seed-optional
- [ ] audio/face/document routes (§3.7)
- [ ] one load + one unload route from the §4 ladder; `/v1/chat/completions` for the bridge
- [ ] residency: DME + intent autoload; gemma/composer/qwen/cosmos/sd load lazily; only ONE GPU LLM at a time
- [ ] lazyload + evict_after_use + idle(10m) + LRU(max 4) + pinning per §5.1/§7/§8
- [ ] **NO `/rerank`, no `rerank_model`**
- [ ] `GET /v1/models` shows `resolved_accelerator` ≠ `cpu` for GPU/NPU models

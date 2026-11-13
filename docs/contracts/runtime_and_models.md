# Runtime & Models

> How brain talks to models, where the runtime lives, and the lifecycle that ties them together.
>
> **Last sync with reality:** 2026-05-09

> **Historical sections below.** This document predates the `runtime/` client
> relocation, the Ettin retina, the PromptOrgan surface, and the OpenRouter
> cognition seat. It is retained for migration provenance only. The live
> contract is `AGENTS.md` plus the modules named in the section below; deleted
> `brain/*` and `/embed` paths must not be reintroduced from this document.

**See Also:**
- [`README.md`](README.md) — Contracts directory index
- [`00_cognitive_doctrine.md`](00_cognitive_doctrine.md) — Cognitive doctrine
- [`01_schema_contract.md`](01_schema_contract.md) — Schema contracts
- [`../architecture/AUTO_INTEGRATION_SURFACE.md`](../architecture/AUTO_INTEGRATION_SURFACE.md) — Auto integration surface
- [AGENTS.md](../../AGENTS.md) — governing runtime and portability contract

---

## Current Live Contract

- Cognition reaches the local runtime through `runtime.client.RuntimeClient` and
  `runtime.client.get_runtime_client()`.
- The default local endpoint is `http://127.0.0.1:11434`; OpenRouter cognition
  is opt-in through `OMEGA_LLM_BACKEND=openrouter` and the process environment.
- The runtime owns model loading and residency. This repository is an HTTP
  client and orchestration layer; it does not load the cognition model locally.
- Runtime-backed capabilities currently include generation, STT, TTS, voice,
  vision, face, image, and document extraction. Product embedding is not a
  runtime capability: semantic perception is the in-process Ettin retina in
  `memory/retina.py`.
- The active skill executor uses native runtime tool calls through
  `auto_loop/execute/brain_skill_executor.py`; the deleted Python Qwen bridge
  is not a supported route.

---

## Historical Architecture (Superseded)

Brain is a thin cognition substrate. **All model calls go through ONE HTTP socket** to the Omega Portable runtime, which lives in a sibling repo at `F:\omega-portable-lite\`.

```
F:\Omega3.0\          ← cognition (brain, autonomous, meta, omega, memory, guardian, sensory)
   │
   │  HTTP, 127.0.0.1:11434
   ▼
F:\omega-portable-lite\   ← runtime (model loaders, inference engines, sensory IO bins)
   ├── omega-runtime/         FastAPI server: /generate /embed /classify /transcribe /synthesise /image_generate ...
   │   ├── core/              Python orchestrator + engine_bindings
   │   └── omega3/            Native C++ engine (Phase 4 in flight, see below)
   ├── models/                All GGUF / ONNX / safetensors / SetFit weights
   ├── runtimes/              Pre-built llama.cpp Vulkan + HIP-Radeon binaries
   ├── handoff/               Phase 4 architecture notes for omega3.cpp
   ├── start.bat              Boot the runtime
   └── install.py             Bootstrap the lite venv
```

The brain side reaches it via `brain/runtime_client.py` (`get_runtime_client().generate(...)`, `.embed(...)`, etc). Every brain phase uses this client; nothing in this repo opens a model file directly.

---

## Historical Standalone Runtimes

These were standalone third-party servers that brain used to talk to directly. The directories still exist under `runtime/` for now (model files, configs, training scripts) but the **runners are dead** — replaced by routes inside the omega-portable-lite runtime.

| Was | Status | Now |
|---|---|---|
| LMS llama-server on port 49234 (voice_box) | **RETIRED as standalone** | `/generate` route in omega-runtime |
| FastFlowLM on port 52625 (embedding_gemma) | **RETIRED as standalone** | `/embed` route in omega-runtime |
| Lemonade on port 13305 (image_gen) | **RETIRED as standalone** | `/image_generate` route in omega-runtime |
| BGE Reranker (in-process) | Still in-process | Same, but loaded via runtime |
| Whisper / Jarvis TTS / Face / SetFit / NPU | On-demand | Same model files, loaded by runtime |

Doctrinal anchor: `MEMORY.md → project_omega_runtime_one_substrate` (2026-05-08 architectural lock):
> Lemonade / FastFlowLM / LMS as third-party servers RETIRED — model files survive, runners change.

---

## Historical Operational Stack

The runtime currently uses **llama-cpp-python with a Vulkan source build** for the voice_box LLM path. ~30 tok/s on AMD Radeon 860M (Strix Point). This is a temporary bridge.

In parallel, **omega3.cpp** (sovereign C++ engine, no vendor wrapping) is being built at `F:\omega-portable-lite\omega-runtime\omega3\` to replace llama-cpp-python entirely. Once forward-pass-correct, the runtime flips over and the llama.cpp dependency is deleted.

Phase 4 forward-pass rewrite is in flight. Status as of 2026-05-09:
- Lazy F32 dequant landed (load 17 min → 15 sec)
- Q6_K column extractor for PLI landed
- Real tokens out of the engine (no longer `<pad><pad>...`)
- `verify_against_safetensors.py` harness being built on a remote MI300X droplet (240 GB RAM)
- KV sharing for layers 15-34 deferred until correctness verified
- Quantized matmul kernels pending (eliminate F32 expansion entirely)

Architecture details for omega3.cpp live in the handoff bundle: `F:\omega-portable-lite\handoff\gemma-4-E4B\PHASE4_ARCHITECTURE_NOTES.md`.

---

## Historical Model Call Examples

```python
from brain.runtime_client import get_runtime_client

rt = get_runtime_client()

# Generate (LLM)
text = rt.generate(
    prompt="...",
    max_tokens=4096,
    temperature=0.7,
    top_p=0.95,
    chat_template_kwargs={"enable_thinking": False},  # for strict-JSON paths
)

# Embed (memory write step 3, contradiction precheck, recall similarity)
vec = rt.embed("text to embed")

# Classify (intent / lobe / dewey routing)
label = rt.classify(text, classifier="lobe")
```

For LLM-style cognition, the older `brain.phases._model_client.call_model_kind(...)` is still the public surface — it now delegates to `runtime_client.generate()` internally. Same call sites, single runtime endpoint underneath.

---

## Historical Runtime Directory Notes

Vestigial. Files survive but the per-subsystem servers don't run anymore. The directory will be cleaned up once the omega-portable-lite runtime fully owns every code path that touches these subsystems.

```
runtime/
├── bge_reranker/     # ONNX cross-encoder (in-process, still loaded directly)
├── face/             # Face recognition (model files; loader stub)
├── fastflowlm/       # EmbeddingGemma (model files only — server retired)
├── jarvis_tts/       # Piper TTS (model files; runtime owns playback now)
├── lemonade/         # Image gen (server retired; runtime owns image_generate)
├── lms/              # llama-server config (server retired; runtime owns /generate)
├── npu_cache/        # NPU caching (still used)
├── npu_config/       # NPU configuration
├── setfit/           # Classifier training scripts (still used to TRAIN; serving is in runtime)
└── whisper/          # STT (model files; runtime owns /transcribe)
```

---

## Historical Model Lifecycle (`core/model_lifecycle.py`)

Still extant for the few in-process models (BGE reranker, SetFit at training time). Most lifecycle work has migrated into the omega-portable-lite runtime, which owns its own loader/sub-env orchestration.

Mode swap (`MODE_COGNITION` ↔ `MODE_IMAGE_GEN`) is now serialized inside the runtime, not in this repo.

---

## Historical Model Router (`core/model_router.py`)

Still resolves model_kind → ModelDescriptor for the few non-runtime kinds (image_gen direct, deterministic engines). For the LLM kinds (`voice_box`, `omega_tiny_cognitive`), the descriptor is mostly informational now — actual call always goes through `runtime_client`.

Constraints still enforced:
- `brain.respond` MUST use `voice_box`
- Deterministic phases cannot use generative models
- Enforced by `is_phase_kind_legal()`

---

## Historical Health Probe

```python
from brain.runtime_client import get_runtime_client
rt = get_runtime_client()
print(rt.health())  # {"ok": true, "model_loaded": true, ...}
```

Or directly:
```bash
curl http://127.0.0.1:11434/health
```

Used in `run_continuous()` post-boot substrate health summary.

---

## Historical Pointers

- Brain↔runtime client: `brain/runtime_client.py`
- Model client compat shim: `brain/phases/_model_client.py`
- Old descriptor router: `core/model_router.py`
- The runtime itself: `F:\omega-portable-lite\omega-runtime\`
- Phase 4 architecture doctrine: `F:\omega-portable-lite\handoff\gemma-4-E4B\PHASE4_ARCHITECTURE_NOTES.md`
- Runtime-related doctrine in MEMORY.md:
  - `project_omega_runtime_one_substrate` (the architectural lock)
  - `project_omega3_cpp_engine_vision` (sovereign C++ engine plan)
  - `project_omega3_native_engine_phase4_progress` (latest engine status)
  - `project_omega_lite_gpu_acceleration` (Vulkan source build details)
  - `project_voice_audio_consolidation` (post-Phase-4 voice cycle plan)

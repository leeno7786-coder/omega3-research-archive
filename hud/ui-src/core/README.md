# Core modules (`hud/ui-src/core/`)

Shared HUD runtime extracted incrementally from `omega_interface.html`.

| Module | Status | Notes |
| --- | --- | --- |
| `boot.js` | **bundled** | Boot sequence, markdown renderer, feed preamble, ChatBridge mock |
| `telemetry.js` | **bundled** | Singleton + panel re-render subscriber; wraps `openPanel` |
| `chat-feed.js` | **bundled** | Feed helpers, thinking blocks, queued response polling |
| `chat-wiring.js` | **bundled** | ChatBridge POST override → `/api/chat` (LIVE WIRING IIFE) |
| `live-wiring.js` | **bundled** | Clock/voice pills, SSE, atmosphere, vision/Build patches, composer (sectioned) |
| `canvas-live.js` | **bundled** | Operator identity, cycle wheel, and recall snapshot refresh |
| `poll-status.js` | **bundled** | Status + boot banner polls; `pollStatus`, `pollBootState`, `applyLiveBootStrings` |
| `poll-telemetry.js` | **bundled** | Unified telemetry snapshot + diagnostics polls; feeds `Telemetry.update()` |
| `poll-v2.js` | **bundled** | v2 substrate endpoint polls (2s/5s/10s cadences); `pollV2Fast/Medium/Slow`, `_setPillBadge` |
| `polling.js` | **bundled** | Poll loop orchestrator — starts tiers (2s/5s/10s/15s), calls handlers from the three poll modules above |
| `omega-ui.js` | **bundled** | Composer action row (Create + Upload buttons), skill picker, OmegaUI helpers (LIVE WIRING IIFE) |
| `panel-fx.js` | **bundled** | Panel visual effects — particle network background, sparklines, badges (LIVE WIRING IIFE) |
| `memory-graph.js` | **bundled** | Interactive force-layout memory graph; `MemoryGraph.init/destroy/loadData/setSearch`; fetches `/api/memory/lobes` |
| `canvas.js` | **bundled** | Panel infra, `_td*` widgets, brain renderers |
| `renderers-dashboards.js` | **bundled** | Memory/omega/autoloop/identity/deepsleep dashboards |
| `canvas-heroes.js` | **bundled** | Respiratory field, trajectory star map, consolidation flow |
| `voice-runtime.js` | **bundled** | Voice IIFE, Kokoro TTS, Parakeet STT transport, wake routing (sectioned) |
| `vision-runtime.js` | **bundled** | Vision capture/consent, FaceWatch (sectioned) |
| `canvas-panels.js` | **bundled** | Panel-local canvas renderers (presence orb, memory canvas, …) |

## Build

| Bundle | Injection markers | Script block |
| --- | --- | --- |
| `boot` | `INJECT_BOOT_*` | First script — before chat-feed |
| `live-chrome` | `INJECT_LIVE_CHROME_*` | First script — after chat-feed |
| `register` | `INJECT_REGISTER_*` | First script — before canvas-live |
| `canvas-live` | `INJECT_CANVAS_LIVE_*` | First script — before telemetry |
| `telemetry` | `INJECT_TELEMETRY_*` | First script |
| `voice-runtime` | `INJECT_VOICE_RUNTIME_*` | First script — after telemetry |
| `vision-runtime` | `INJECT_VISION_RUNTIME_*` | First script — after voice-runtime |
| `chat-feed` | `INJECT_CHAT_FEED_*` | First script |
| `canvas` | `INJECT_CANVAS_*` | First script — includes heroes |
| `canvas-panels` | `INJECT_CANVAS_PANELS_*` | First script — after panels |
| `live-composer` | `INJECT_LIVE_COMPOSER_*` | First script — after canvas-panels |
| `live-sse` | `INJECT_LIVE_SSE_*` | HUD LIVE WIRING script |
| `live-atmos` | `INJECT_LIVE_ATMOS_*` | HUD LIVE WIRING IIFE (before core polling) |
| `core` (polling) | `INJECT_CORE_*` | HUD LIVE WIRING IIFE |
| `chat-wiring` | `INJECT_CHAT_WIRING_*` | HUD LIVE WIRING IIFE |
| `omega-ui` | `INJECT_OMEGA_UI_*` | First script |
| `panel-fx` | `INJECT_PANEL_FX_*` | First script |
| `memory-graph` | `INJECT_MEMORY_GRAPH_*` | First script |
| `live-vision` | `INJECT_LIVE_VISION_*` | HUD LIVE WIRING IIFE (after chat-wiring) |
| `vision-facewatch` | `INJECT_VISION_FACEWATCH_*` | HUD LIVE WIRING script (FaceWatch IIFE) |
| `voice-kokoro` | `INJECT_VOICE_KOKORO_*` | HUD LIVE WIRING script |
| `voice-stt` | `INJECT_VOICE_STT_*` | HUD LIVE WIRING script |
| `voice-wake` | `INJECT_VOICE_WAKE_*` | HUD LIVE WIRING script |

`live-wiring.js` and `voice-runtime.js` / `vision-runtime.js` use `// SECTION:name` markers; `build_hud.py` splices each section into its inject slot.

Run `.\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py` after edits.

## Module contract

- **Canvas / heroes / canvas-live** — plain functions in the first script scope; depend on `_escapeHtml` in the monolith shell.
- **Core (poll-status / poll-telemetry / poll-v2 / polling / chat-wiring / live-wiring sections)** — fragments spliced into the LIVE WIRING IIFE; poll handlers are exposed on `window.*` so `polling.js` can call across the splice boundary.

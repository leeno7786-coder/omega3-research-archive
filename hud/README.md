# hud — the operator web surface for the Omega 3.0 substrate

> **Status (2026-06-10):** HUD is **restored and live** on the v2 rebuild path. It runs in-process via `boot.py`, not as a separate process. Local single-operator platform — **no auth middleware** on mutating routes (bind `127.0.0.1` by default).

**What it is** — `hud/` is the FastAPI web application that gives the operator a window into the live cognitive substrate. It serves **`omega_interface.html`** at `GET /` plus a JSON/SSE telemetry API. The HUD is **read-only by default**: it polls Omega subsystem state and renders it. The only interactive surfaces are the explicit exceptions — operator chat, settings, and opt-in face memory enrollment — and they route through the existing `pressure_loop` / Guardian / `memory.api` paths, never bypassing doctrine.

---

## Key files / areas

| File | Role |
| ---- | ---- |
| [`server.py`](server.py) | The FastAPI app. `create_app()` builds the status, panel, telemetry, settings, vision, image, speech, upload, and chat routes; `GET /` serves `hud/omega_interface.html`. Presence is operator-initiated — the HUD does NOT auto-start a webcam presence watcher on boot. |
| [`spawner.py`](spawner.py) | **Canonical launch path.** `boot.py` calls `spawn_hud_in_thread()` to run the HUD as a **daemon thread inside the boot process** — same interpreter, no IPC, same-origin so browser CORS works. Watchdog restarts up to 5 times with exponential backoff. |
|| [`operator_identity.py`](operator_identity.py) | Operator display context. Relocated from `core/`. |
|| [`curiosity_store.py`](curiosity_store.py) | Persistent open-questions journal. Relocated from `core/`. |
| [`standalone.py`](standalone.py) | Secondary launcher — `.\\.venv-brain\\Scripts\\python.exe -m hud.standalone`. Boots Omega (or `--no-boot`) then runs uvicorn on port 13310. `--open-browser` (used by `Omega.exe`/`hud-only.bat`) prepares the SSD-local Edge debugger profile and opens the HUD after the port binds. Optional `--https`. `--presence-test` serves the S2 surface instead of the full HUD (implies `--no-boot`). |
| [`presence_test.py`](presence_test.py) | **S2 operator-run live test surface** (`.\\.venv-brain\\Scripts\\python.exe -m hud.standalone --presence-test`). Thin glue over the **real** `sensory.operator_presence.PresenceMachine` + the **real** runtime face models. Browser-webcam frames replace the server cv2 loop. Robust per-sample enroll. Pure diagnostic: no Omega boot, no cognitive loop. |
| [`telemetry.py`](telemetry.py) | Read-only adapters: Omega subsystem state → JSON shapes the frontend renders. Process-wide cached cadence scheduler (HUD polls ~2s; rebuilding per-call was log/CPU spam). Audit-ring reads use a single capture + **1.5s TTL cache** (`AuditRingSnapshot`) to avoid amplification. Best-effort: degrades to shape-correct placeholders, never 500s the panel. Reads `pressure_loop.runtime`, `RECENT_LAYER_STATES`, and `sensory.operator_presence`. |
| [`settings.py`](settings.py) | Thin command layer for operator settings (HUD theme, voice & speech, operator profile). Normalizes payloads; durable writes go to `guardian/state/hud/*.json` / `memory.api`. |
| `live chat` | The sole agent execution surface. Tool calls, skill dispatch, and responses stay on AUTO's chat rail. |
| [`events.py`](events.py) | SSE event-stream feeder behind `GET /api/events`. |
| [`omega_interface.html`](omega_interface.html) | **Built operator surface** — edit `hud/ui-src/`, then run `.\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py` to assemble this single-file bundle. |

---

## Entry points / how it's used

**Canonical (production / operator):**

```powershell
.\\.venv-brain\\Scripts\\python.exe boot.py
```

Brings up the substrate AND the HUD (daemon thread via `spawner.py`) on `http://127.0.0.1:13310`. Browse there. Same-origin = `/api/*` fetches work. Override with env: `OMEGA_HUD_HOST`, `OMEGA_HUD_PORT`, `OMEGA_HUD_DISABLE=1`.

**Standalone (visual inspection — substrate paused):**

```powershell
.\\.venv-brain\\Scripts\\python.exe -m hud.standalone               # boot paused + HUD (live memory/panels)
.\\.venv-brain\\Scripts\\python.exe -m hud.standalone --no-boot     # HUD only — empty placeholders
.\\.venv-brain\\Scripts\\python.exe -m hud.standalone --port 13311
.\\.venv-brain\\Scripts\\python.exe -m hud.standalone --open-browser  # portable Edge debugger + HUD
.\\.venv-brain\\Scripts\\python.exe -m hud.standalone --https       # certs at guardian/state/tls/server.{crt,key}
```

Or the batch wrapper:

```powershell
scripts\hud-only.bat            # routes through Omega.exe, http :13310
scripts\hud-only.bat 13311
scripts\hud-only.bat 13310 --https
scripts\hud-only.bat 13310 --no-boot   # empty placeholders only
```

**Graceful stop:**

```powershell
.\\.venv-brain\\Scripts\\python.exe scripts\\shutdown.py
```

Reads `guardian/state/boot.lock` (`core/boot_lock.py`), verifies the recorded PID is a live `boot.py` in this repo, sends a graceful stop signal (SIGINT / CTRL_BREAK then escalate), and clears the lock. Model runtime (`:11434`) is managed separately.

**Routes (all defined inside `create_app()` in `server.py`):**

- `GET /` → `hud/omega_interface.html`; 500 if the file is missing.
- `GET /healthz` → liveness probe; reports `html_present`.
- `GET /api/status` → top status bar (5 pills: voice, identity, guardian, memory, l0/semantic core).
- `GET /api/boot/state` → boot banner state.
- `GET /api/panel/{topic}` → detail panel; `topic` ∈ `{brain, memory, identity, omega, autoloop, deepsleep, meta, global, settings}`.
- `GET /api/telemetry/snapshot` → unified telemetry blob the frontend deep-merges (~2s poll).
- `GET /api/events` → Server-Sent Events stream (`events.py`).
- `POST /api/chat` → canonical operator text entry. Calls `pressure_loop.operator_chat.handle_operator_chat`, which normalizes text into an operator contributor, opens a pressure-led episode, and returns AUTO/Global response artifacts plus prompt metadata. **Refuses** when the pressure loop is paused — operator input must not bypass pressure/action setup.
- `GET /api/global-operator-outreach/active` → the one pending system query (text app), if any, from `pressure_loop.system_messages.current_outreach()` (canonical delivery is the system-messages badge + panel).
- `GET /api/presence/state` → `PRESENCE_STATE` + watcher heartbeat.
- `POST /api/guardian/{approve,deny,escalate}`, `GET /api/guardian/inspect` → Guardian approval surface.
- `GET|POST /api/settings/*` → HUD theme, voice & speech (TTS/STT configuration), and operator profile.
- `/api/system-messages` (`GET`, `POST /{id}/respond`, `DELETE /{id}`) → the text app: system queries to the operator (badge + panel). `respond` feeds the reply back through the operator input queue box as `operator_response`; `delete` is pressure-loss.
- `/api/vision/*`, `/api/image/generate`, `/api/tts/*`, `/api/stt/*` → multimodal surfaces.
- `/api/upload`, `/api/uploads/*`, `/api/images/*` → file staging (under `guardian/state/uploads`, 25 MB cap) + generated-image serving.
- `GET /api/skills` → read-only `SKILL.md` inventory for the live-chat slash-command picker.

**Face memory enrollment:** Settings → operator profile owns face enrollment. Clicking “add named face memory” captures one visible webcam shot, asks for a name/context, and posts to `POST /api/vision/remember-face`, which writes a `face_memory` to vision lobe `(7,1)` plus the persona backlink. The HUD no longer starts FaceWatch, polls vision status, or opens face enrollment during first boot; local face models load only after this explicit settings action or an explicit recognition command.

**Live-chat agent surface:**
Agent execution stays on the shared chat/SSE rails. The HUD has no standalone terminal, agent window, websocket bridge, or retirement shim for those surfaces.

The chat bar's slash picker is sourced from `/api/skills`: local valid skills,
the built-in permission commands, and installed plugin IDs only. A catalog
candidate is not exposed as a chat command until it is installed. Marketplace
"Try in chat" inserts the same `/plugin:<id>` command; there is no plugin button
or connected-plugin chip in the composer.

Chat execution modes are changed in the composer with `/read-only`,
`/allow-edits`, `/always-allow`, or `/mode`. The mode gates ordinary agent
tools and chat-originated skills; hard workspace, self-modification, command,
and signed-plugin authority checks remain in force.

---

## Lifecycle (in-process spawn)

`boot.py:run_continuous()` (`boot.py:238`):
1. Acquires boot lock (`core/boot_lock.py:acquire`) — kills any prior substrate first, then takes the lock.
2. Calls `boot()` — warms runtime, memory, guardian.
3. Calls `hud.spawner.spawn_hud_in_thread()` (`boot.py:327`) AFTER boot, BEFORE `run_pressure_tick_loop`.
4. Runs the pressure tick loop (`pressure_loop.loop.run_pressure_tick_loop`).
5. In `finally`, calls `shutdown_system()` (`boot.py:384`) — sets `_explicit_shutdown_requested=True` so the watchdog does not restart.

**Spawner rules (`hud/spawner.py:126`):**
- The HUD runs in a **daemon thread**, not a subprocess. This shares the same Python interpreter so the FastAPI app talks to the live `PressureRuntime` + `SharedState` without IPC.
- Failure to start the HUD does NOT abort the cognitive substrate. The substrate is the load-bearing system; the HUD is the operator surface.
- Port + host configurable via env vars (`OMEGA_HUD_HOST`, `OMEGA_HUD_PORT`) — defaults to `127.0.0.1:13310`.
- `OMEGA_HUD_DISABLE=1` skips spawning entirely (for headless CI runs or when running standalone).
- Watchdog: if uvicorn returns unexpectedly (not operator-requested shutdown), the spawner restarts up to 5 times with exponential backoff (2s, 4s, 8s, 16s, 32s).

---

## Visual surface (as-built 2026-06-29)

**Under-chat nav pills** are intentionally minimal. The operator-facing rail keeps
`help`, `settings`, and `shutdown`. The former cognition panel launchers
(`brain`, `memory`, `identity`, `omega`, `autoloop`, `deepsleep`) are removed
from the rail and from voice/typed panel routing. Their backend telemetry
adapters and `/api/panel/{topic}` JSON routes remain for compatibility.

**Removed surfaces (historical UI cleanup):**
- **Top `.vpill` strip** — bespoke visual pills v1 (gauges/orbs/graphs/arcs as header row) removed.
- **Guardian pill + panel** — removed from nav and panel registry. Guardian's backend substrate remains; only this HUD surface is unwired.
- **Autonomous machine-prose feed lines** — `.activity`, `.objective`, `.voice-state`, `.greet`, `.ready` removed from chat feed. Conversation (`user`/`assistant` turns), reasoning blocks, error lines, image cards, and operator-action feedback lines are preserved.

**Data feed:**
- `hud/telemetry.py` panel adapters read the current pressure/runtime and
  presence surfaces directly.
- Reads `pressure_loop.runtime` + `RECENT_LAYER_STATES` + `sensory.operator_presence`.
- Fail-closed to clean degraded JSON (HTTP 200) when the runtime is absent.

**Settings panel (as-built 2026-06-08):**

The Settings panel provides three active configuration surfaces:

| Settings section | Fields | Persistence |
|----------------|--------|-------------|
| **Operator profile** | name, preferred name, birthday, pronouns, location, timezone, role, working on now, tone preference, schedule pattern, notes | `memory/100_personas/operator` via `memory.api` |
| **Themes** | theme (8 options: industrial, paper, night garden, high contrast, ocean deep, sunset amber, arctic mist, midnight violet), density, motion, glow, panel depth | `guardian/state/hud/theme_settings.json` |
| **Voice & speech** | TTS enabled, Qwen CustomVoice speaker (default: Aiden), speed, pitch; STT enabled, language, model; wake word enabled, wake word phrase; auto-speak, voice activity detection, noise cancellation | `guardian/state/hud/voice_settings.json` |

**Voice toggle integration:**
- The voice pill in the chat bar respects the `stt_enabled` setting — if disabled, the pill shows feedback and refuses to activate
- When voice settings are saved, if STT is disabled and the voice pill is active, it automatically deactivates
- TTS synthesis respects the `tts_enabled`, `tts_speed`, and `tts_pitch` settings
- STT recognition respects the `stt_enabled` and `stt_language` settings

---

## Observability

All HUD backend operations emit via `guardian.terminal_feedback` (failure-soft, never halts cognition) with matching `guardian.data_logger.emit_event` JSONL durability where noted.

| Operation | Hook | Durable JSONL subsystem |
|-----------|------|------------------------|
| Settings routes | `emit_phase` | `omega.hud.settings` |
| Backend dev helpers (launch / shutdown / rollback) | `emit_phase` | `omega.hud.dev_pill` |
| Vision (identify / status / enroll / remember-face / capture / anchors / forget) | `emit_phase` + `emit_inference` + `emit_data` | `omega.hud.vision` |
| Image generate | `emit_phase` + `emit_inference` + `emit_data` | `omega.hud.image_generate` |
| /api/images/list + /api/images/get | `emit_phase` | `omega.hud.images_list`, `omega.hud.images_get` |
| /api/chat | `emit_phase` + `emit_data` | `omega.hud.chat` |
| /api/events SSE stream | `emit_data` | `omega.hud.events_stream` |
| Face sidecar write | `emit_data` | `omega.hud.face_sidecar_write` |
| Persona back-link | `emit_memory` | `omega.hud.persona_backlink` |
| TTS / STT routes | `emit_phase` | `omega.hud.tts`, `omega.hud.stt` |
| spawner.py | `emit_phase` | `omega.hud.spawner` |
| telemetry.py | `emit_data` | `omega.hud.telemetry` |
| curiosity_store.py | `emit_phase` | `omega.hud.curiosity` |

> Failure-log coverage: `docs/architecture/FAILURE_LOG_COVERAGE.md`

## Depends on / depended-on-by

**Depends on:**

- `core.paths` — `OMEGA_ROOT`, `GUARDIAN_STATE_ROOT` (HTML path, upload/image roots).
- `pressure_loop.operator_chat` — `/api/chat` dispatches user text into the current pressure-led episode chain.
- `pressure_loop.runtime` — v2 telemetry reads PressureRuntime + SharedState directly.
- `pressure_loop.cycle_cadence` — cached scheduler used by telemetry pressure signals.
- `memory.api` / Guardian state — durable writes for settings, identity, credentials, uploads (under `guardian/state/`, `guardian/logs/`).
- Live chat/AUTO — the sole agent execution and tool-dispatch surface.
- `uvicorn`, `fastapi` — the server runtime.

**Depended-on-by:**

- `boot.py` → `hud.spawner.spawn_hud_in_thread()` (the canonical launch path).
- The operator, via the browser at `http://127.0.0.1:13310`.

---

## Security (local operator)

- **No HUD auth layer** — by operator directive this is a localhost single-operator surface. Hardening for non-localhost deploy is out of scope unless explicitly requested.
- **Path guards** (`core/hud_path_guard.py`) confine uploads, generated-image serving, face-image paths, and dev rollback targets to repo-root subtrees.
- **Face upload limits** on `POST /api/vision/remember-face` — MIME/size validation before write.
- **Cognition lock** — HUD chat and the pressure tick share `core/cognition_lock.py` around `ignite_episode` so cascade state cannot interleave.

---

## Gotchas / doctrine

- **Frontend build:** `hud/omega_interface.html` is the served bundle. The source of truth is `hud/ui-src/`; run `.\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py` after source edits and `.\\.venv-brain\\Scripts\\python.exe hud\\scripts\\build_hud.py --check` before committing. `GET /` serves the bundle directly and reports a missing bundle as an error.
- **The launcher moved to `scripts/` (2026-05-17).** Use `scripts\hud-only.bat`, not a repo-root `hud-only.bat`.
- **Read-only by default.** Telemetry adapters must never mutate Omega state; interactive surfaces are the explicit, doctrine-routed exceptions only.
- **Same-origin only.** CORS is intentionally *not* configured. Opening the HTML via `file://` breaks every `/api/*` fetch (origin `null`) — always reach the HUD through `http://127.0.0.1:13310`, which is why `boot.py` spawns it in-process.
- **Telemetry is degraded under `--no-boot`** — placeholders, no live subsystem state. Only `.\\.venv-brain\\Scripts\\python.exe boot.py` (or `hud.standalone` without `--no-boot`) yields real data.
- **Agent execution is not a separate bridge.** Inspect live-chat response frames and AUTO tool telemetry for execution state.
- **HTTPS** needs certs at `guardian/state/tls/server.{crt,key}` before
  `--https`; certificate provisioning is outside the HUD bundle.
- Cycle-cadence scheduler is process-wide cached on purpose — see `docs/contracts/` for the FINAL VERDICT cure invariants the cadence/cognition authority boundaries derive from.
- **Do NOT use retired HUD browser-agent UI paths for new work.** See `AGENTS.md` §Agent context memory.

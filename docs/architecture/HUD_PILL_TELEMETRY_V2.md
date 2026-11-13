# HUD Pill Telemetry — v2 Symphony Wiring Reference

**Date:** 2026-05-27 (line-number resync + heartbeat status update + CueDesk suppress wiring flip)
**Branch:** `main` (tip `ba9a42a`)
**Audience:** Kimi K2.6 agent swarm updating HUD pills to reflect v2 substrate
**Companion docs:** `docs/architecture/SLICE_BOARD.md`, `docs/architecture/SYMPHONY_FULL_INTEGRATION_STATUS.md`

> **Historical design reference.** This May 2026 proposal names retired
> `core.response_engine`, `core.runtime_capability_broker`, `brain/*`, and the
> `/embed` capability. It is retained for provenance; the live HUD/runtime
> owners are `hud/server.py`, `hud/telemetry.py`, and `runtime/`.

---

## 0. Context — what v2 changed (read first)

The v2 symphony substrate (P2-S17 → P3-S02, May 2026) introduced new gates, primitives, and broker surfaces that **the HUD does not yet reflect**. This document maps each HUD surface to the v2 telemetry it should poll.

**v2 surfaces the HUD must now expose:**

1. **Pressure-loop pause** (`core/pressure_loop_pause.py`) — owner-set pause primitive. Dev pill, autonomy controls, and any future "yield substrate" feature route through this. Replaces legacy `pause_default_autonomy` for the dev pill.
2. **Operator presence gate** (`core/operator_presence.py` + `core/response_engine.py:should_speak_now`) — `PRESENCE_STATE` in SharedState (PRESENT / ABSENT / UNKNOWN) is now the master gate for proactive speech. Face module DOWN → UNKNOWN → holds (fail-CLOSED).
3. **Runtime capability broker** (`core/runtime_capability_broker.py`) — 11 capabilities each with `(state, route, reason)`: LLM/CODING_MODEL, EMBED, CLASSIFY, FACE, VOICE, STT, TTS, IMAGE, DOCUMENT_PARSING, SPECIALIST_VISION, SPECIALIST_VIDEO. Every cognitive call preflights through it.
4. **CueDesk suppress-reason taxonomy** (`core/cue_desk.py:tick`) — when no movement dispatches, the reason is structured (`pressure_loop_paused`, `presence_gate_held`, `autonomy_paused`, `omega_lock_held`).
5. **Response engine signals** — `response.presence_gate.held`, `response.dispatch.result`, `response.decision.curiosity`, `response.decision.system_state.count`, `response.sweep.dispatched_count` all land in `feedback_audit.jsonl`.

---

## 1. The two pill classes in the HUD

| Class | Where | Count | Behavior | File |
|---|---|---|---|---|
| **Navigation pills** | Top row, `.pills` container | 11 | Click → open panel; no polling | `hud/omega_interface.html:3559-3571` |
| **Status glyphs** | Below pills, `.governance-posture` row | 5 | Poll `/api/status` every 2s; color-coded state | `hud/omega_interface.html:3495-3509` |
| Voice control widget | Bottom-left | 1 | Event-driven (button + watchdog), not polled | `hud/omega_interface.html:3553-3555` |

> The Kimi swarm should treat these as **two separate problems**:
> - Navigation pills: add per-pill v2 state badges (small glyph adjacent to the pill label) that poll a per-pill endpoint
> - Status glyphs: extend the existing `/api/status` payload (or add a v2 sibling endpoint) to cover the broker's 11 capabilities + presence + pressure-loop

---

## 2. Current state — what each surface polls TODAY

### 2.1 Status glyphs (already polling)

All five poll `/api/status` every 2s via `startPolling()` (`hud/omega_interface.html:8790-8827`, `9097-9105`).

Response shape: `{ <glyph_id>: { state, label, detail }, ... }`
State enum (CSS class mapping at `hud/omega_interface.html:8798-8802`):
- `live` → green
- `loading` → gray (`dim`)
- `degraded` → amber (`warn`)
- `down` → amber (`warn`)
- `copper` → reserved, unused

| Glyph DOM id | Label shown | Field read | Backend handler |
|---|---|---|---|
| `g-voice` | "voice" | `status.voice` | `hud/telemetry.py:panel_status()` |
| `g-identity` | "identity" | `status.identity` | same |
| `g-guardian` | "guardian" | `status.guardian` | same |
| `g-memory` | "memory" | `status.memory` | same |
| `g-l0` | "semantic core" | `status.l0` | same |

### 2.2 Navigation pills (current panel-open behavior)

| Pill | DOM | Panel handler | What it currently shows |
|---|---|---|---|
| brain | `.pill[data-panel="brain"]` | `panel_brain()` | cycle + recent layers |
| memory | `[data-panel="memory"]` | `panel_memory()` | recall trace, top recall, anchors |
| guardian | `[data-panel="guardian"]` | `panel_guardian()` | posture, mode, integrity, approvals |
| identity | `[data-panel="identity"]` | `panel_identity()` | charter, operators, self-vector, voice |
| omega | `[data-panel="omega"]` | `panel_omega()` | 8-section identity + governance |
| autoloop | `[data-panel="autoloop"]` | `panel_autoloop()` | pressure, cadence, curiosity |
| deepsleep | `[data-panel="deepsleep"]` | `panel_deepsleep()` | phases 1-4 rollup, 8s poll while open |
| browser | (calls `browserEnter()`) | `hud_browser_agent.*` | sandbox, action queue, inbox |
| terminal | `[data-panel="terminal_agent"]` | `panel_terminal_agent()` | live run, params, skills, outcomes |
| settings | `[data-panel="settings"]` | (local UI only) | theme, browser-agent config |
| dev | `terminalEnter()` | WebSocket `:8091/ws/cli` | omega_cli output stream |

### 2.3 Polling cadence summary (current)

| Endpoint | Method | Cadence | Driver | File:line |
|---|---|---|---|---|
| `/api/status` | GET | 2s | startPolling | `omega_interface.html:9097-9105` |
| `/api/boot/state` | GET | 2s | startPolling | same |
| `/api/telemetry/snapshot` | GET | 2s | startPolling | same |
| `/api/events` | SSE | continuous | EventSource | `omega_interface.html:9033-9044` |
| `/api/browser-agent/inbox?limit=12` | GET | 8s (when browser mode active) | `browserAgentRefresh` | `omega_interface.html:7749` |
| `/api/panel/deepsleep` | GET | 8s (when panel open) | `_refreshDeepsleepPanel` | `omega_interface.html:4259-4265` |
| `/api/vision/status` | GET | 15s | `pollDiagnostics` | `omega_interface.html:9083` |
| `/api/vision/anchors` | GET | 15s | same | `9084` |
| `/api/tts/status` | GET | 15s | same | `9085` |
| `/api/stt/status` | GET | 15s | same | `9086` |
| `ws://localhost:8091/ws/cli` | WS | continuous (dev mode) | `devConnect` | `omega_interface.html:8067-8098` |

---

## 3. v2 telemetry sources — what to poll for each pill

For each pill, columns are:
- **v2 signal** — the substrate concept
- **Where it lives** — source file + function
- **Already wired?** — Y if an HTTP endpoint already exposes it, N if a new endpoint must be added
- **Endpoint** — existing or proposed path
- **Polled fields** — JSON keys to read
- **State semantics** — enum + color mapping
- **Cadence** — recommended poll interval

### 3.1 `brain` pill

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| Current cycle, recent layers | `hud/telemetry.py:panel_brain()` (line 2451) | Y | `GET /api/panel/brain` | `cycle.cycle_id`, `cycle.source`, `cycle.layer`, `cycle.pressure`, `layers[]` (tail 6) | n/a (display only) | 2s (already via snapshot) |
| **Last pressure pulse** | `core/pressure_field.py` → cycle.pressure | Y (in snapshot) | `GET /api/telemetry/snapshot` → `cycle.pressure` | scalar 0..1 | low<0.3 green / 0.3-0.7 amber / >0.7 red | 2s |
| **CueDesk last suppress reason** | `core/cue_desk.py:tick` → `LAST_CUEDESK_TICK` (P3-S06) | **Y** (wired at `hud/telemetry.py:2527-2535`) | `GET /api/panel/brain` → `{suppress_reason, opened_by}` and `GET /api/cuedesk/last_tick` (panel snapshot) | `suppress_reason`, `opened_by`, `pause_owners[]` | proceed=green / pressure_loop_paused=blue / presence_gate_held=amber / autonomy_paused=gray / p2_s2_no_action_observe=teal (P3-S23) | 2s |

**Pill badge proposal:** small dot next to "brain" label colored by `cycle.pressure` band. Tooltip shows last suppress_reason if non-empty.

### 3.2 `memory` pill

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| Recall trace, top recall, anchors | `hud/telemetry.py:panel_memory()` (line 2557) | Y | `GET /api/panel/memory` | `recall_trace`, `top_recall`, `persistence_anchors` | n/a | 2s via snapshot |
| **EMBED capability state** | `core/runtime_capability_broker.py:check_runtime_capability(EMBED)` | **N** (needs broker endpoint) | propose `GET /api/broker/snapshot` → `embed.{state, route, reason}` | `state`, `route`, `reason` | available=green / degraded=amber / down=red | 5s |
| Pending approvals (memory promotions) | `hud/telemetry.py:panel_memory()` | Y | same | `pending_approvals[]` count | 0=green / >0=amber | 2s |

**Pill badge proposal:** badge shows count of pending memory promotions; color flips to amber when EMBED is degraded (recall falls back to lexical-only).

### 3.3 `guardian` pill

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| Posture, mode, HMAC integrity | `hud/telemetry.py:panel_guardian()` (line 2563) | Y | `GET /api/panel/guardian` | `posture`, `mode`, `integrity`, `pending_approvals`, `interventions` | n/a | 2s via snapshot |
| Pending staging refs | same | Y | same → `pending_approvals[]` | count + ref ids | 0=green / >0=amber | 2s |
| Recent interventions | same | Y | same → `interventions[]` | count last N minutes | 0=green / >0=red | 2s |

**Already-good source.** No new wiring needed; this pill is already v2-compatible.

### 3.4 `identity` pill

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| Charter, operators, self-vector, voice | `hud/telemetry.py:panel_identity()` (line 2911) | Y | `GET /api/panel/identity` | `charter`, `operators`, `self_vector`, `voice` | n/a | on open |
| **Operator presence state** (NEW — P3-S02) | `core/operator_presence.py:read_presence_from_shared_state(ss)` | **N** | propose `GET /api/presence/state` → `{state, last_heartbeat_iso, watcher_alive, enrolled_operator_name}` | `state` ∈ {PRESENT, ABSENT, UNKNOWN} | PRESENT=green / ABSENT=amber / UNKNOWN=red (fail-CLOSED) | 2s |
| **FACE capability state** | broker → `FACE` | **N** (broker endpoint) | `GET /api/broker/snapshot` → `face.{state, route, reason}` | same | available=green / degraded=amber / down=red | 5s |
| Identity drift score | `core/loop_adapters.py` signal `identity.self_vector_updated` | Indirect (events SSE) | `/api/events` filter `name=identity.self_vector_updated` | value scalar | <0.05=green / 0.05-0.20=amber / >0.20=red | event-driven |
| Anchor count, known names | `hud/server.py:/api/vision/anchors` (line 2287) | Y | `GET /api/vision/anchors` | `count`, `names[]` | n/a | 30s |

**Pill badge proposal:** badge shows presence state glyph (•=PRESENT, ○=ABSENT, ?=UNKNOWN). Tooltip shows enrolled operator name + last heartbeat age.

### 3.5 `omega` pill

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| Full 8-section omega state | `hud/telemetry.py:panel_omega()` (line 2569; `_telemetry_omega` at line 1272) | Y | `GET /api/panel/omega` | `objective`, `online_days`, `coherence`, `traits`, `contradiction`, `gaps`, `capability_timeline`, `trajectory`, `tool_synthesis` | n/a | on open + 2s via snapshot |
| **LAST_OMEGA_TRAJECTORY** (v2-resilient) | SharedState canonical key | Y (in snapshot) | snapshot → `omega.trajectory` | latest trajectory record | n/a | 2s |
| **LAST_OMEGA_PROPOSALS** (new v2) | SharedState canonical key | Y (in snapshot) | snapshot → `omega.proposals[]` | proposal count | 0=gray / >0=blue (active) | 2s |
| Tool synthesis queue depth | `hud/telemetry.py:_tool_synthesis_summary()` (line 1337) | Y | snapshot → `omega.tool_synthesis.{registry, queue, recent, dev_invocations}` | queue depth | 0=green / >0=blue | 2s |
| Doctrine audit | `hud/server.py:/api/omega/doctrine_audit` (line 2724) | Y (stub) | `GET /api/omega/doctrine_audit` | `retired?`, `reason?` | retired=red / else=green | 60s |

**Pill badge proposal:** badge shows tool-synthesis queue depth as a small numeric counter when >0.

### 3.6 `autoloop` pill

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| Pressure, cadence, curiosity | `hud/telemetry.py:_telemetry_autoloop()` (line 1998; routed via `/api/panel/autoloop`) | Y | `GET /api/panel/autoloop` | `pressure`, `cadence`, `curiosity` | n/a | 2s via snapshot |
| **Pressure-loop pause state** (NEW) | `core/pressure_loop_pause.py:pressure_loop_pause_state()` | **N** | propose `GET /api/pressure-loop/state` → `{paused, owners[]}` | `paused` bool, `owners[]` | paused=blue / running=green | 2s |
| Open curiosities | `panel_autoloop()` | Y | same | `curiosity.open_count` | 0-5=green / 5-15=amber / >15=red | 2s |
| **CueDesk suppress reason** | `core/cue_desk.py:tick` | **N** | propose `GET /api/cuedesk/last_tick` → `{suppress_reason, opened_by, idle_probes_run}` | `suppress_reason` | n/a (informational) | 2s |
| Last `response.sweep.dispatched_count` | `core/response_engine.py:1053` signal | Indirect | `/api/events` filter `name=response.sweep.dispatched_count` | value scalar | n/a | event-driven |

**Pill badge proposal:** when paused, show pause glyph (⏸) colored by who's pausing (dev_pill=blue). When running, show last sweep dispatched count or "—" if zero.

### 3.7 `deepsleep` pill

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| Full 4-phase rollup | `hud/telemetry.py:panel_deepsleep()` (line 2677) | Y | `GET /api/panel/deepsleep` | `phase1..4`, `upstream_telemetry`, `phase_states[]`, `current_phase`, `next_cycle_eta` (P3-S18/S19) | active phase highlighted | 8s while panel open |
| **Pressure-loop pause** (deepsleep schedules when substrate quiet) | `pressure_loop_pause_state()` | **N** | `GET /api/pressure-loop/state` | `paused`, `owners[]` | n/a | 2s |
| Next-phase ETA | `panel_deepsleep()` → upstream_telemetry | Y | same | `next_phase_at` | <60s=blue / >60s=gray | 8s |

**Pill badge proposal:** badge shows currently-active phase number (1-4) or "—" if not in deepsleep.

### 3.8 `browser` pill

Already well-instrumented; no v2 changes required for substrate telemetry.

> ⚠️ **2026-05-27 audit drift flag.** `hud/omega_interface.html:11510-11562` re-introduced [Approve][Deny][Escalate][Inspect] verb buttons in the browser_agent panel for pending approvals ("Migrated from panelGuardian … visibility alone is observation; verbs make it command"). This contradicts [[feedback_guardian_outside_poisoning_only]] and the P3-S11 doctrine that retired the Approval Stack hero. Open decision: either strip the verbs (read-only observatory rows) or carve out a clearly-labeled separate panel and update `HUD_PANEL_CONTENT_V2 §2.3` to spec the exception. Tracked as a follow-up slice; do not extend the verb surface in the meantime.

Endpoints:
- `GET /api/browser-agent/status` (line 552)
- `GET /api/browser-agent/inbox?limit=12` (line 557) — 8s poll
- `GET /api/browser-agent/sandbox` (line 733)
- `GET /api/browser-agent/credentials` (line 562)

**One v2-relevant addition:** browser action execution preflights through broker (`CODING_MODEL` capability for analysis, `SPECIALIST_VISION` for OCR). Surface broker state when in flight:

| v2 signal | Source | Wired? | Endpoint | Fields | States |
|---|---|---|---|---|---|
| Coding/vision capability for browser actions | broker | **N** (broker endpoint) | `GET /api/broker/snapshot` → `coding_model.state`, `specialist_vision.state` | state per cap | available=green else amber |

### 3.9 `terminal` pill (terminal_agent)

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| Live run, params, outcomes | `hud/telemetry.py:panel_terminal_agent()` (line 2657) | Y | `GET /api/panel/terminal_agent` + `GET /api/terminal/state` (line 1671) | full snapshot | n/a | 2s via snapshot |
| Active skills | `terminal_agent/hud_bridge.py` (line ~150+, see active_skills set) | Y | `GET /api/terminal/skills` (line 1729) | `available[]`, `active[]`, `commands[]` | active=patina-green / inactive=faded | on-demand |
| Skills mutations | `/api/terminal/skills/{name}/{action}` (lines 1741-1756) | Y | POST/DELETE | per-action response | n/a | on-demand |

**No v2 changes required.** Skills surface was just landed (commits referenced in `[[project_skills_hud_frontend_complete]]`).

### 3.10 `settings` pill

Already covers HUD theme + browser-agent + operator-profile via `hud/server.py:370-465`. No v2 changes required for substrate telemetry.

**Optional addition:** broker capability config visibility — surface which capabilities the runtime advertises as available so operator can see what hardware/models are resident.

| v2 signal | Source | Wired? | Endpoint |
|---|---|---|---|
| Capability inventory | broker → `health_body.capabilities` | **N** | propose `GET /api/runtime/health` |

### 3.11 `dev` pill (dev mode)

This pill is **the most v2-impacted** because it's the operator's primary lever to pause the substrate.

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| **Pressure-loop pause state** | `core/pressure_loop_pause.py:pressure_loop_pause_state()` | **N** | propose `GET /api/dev/pause_state` → `{paused, owners[]}` | `paused`, `owners[]` (e.g. `["dev_pill"]`) | paused-by-dev=blue / paused-other=amber / running=green | 2s |
| Pause/resume mutators | `POST /api/dev/launch` (line 1052), `POST /api/dev/shutdown` (line 1109) | Y | already in place | called when pill engaged/disengaged | n/a | on-click |
| omega_cli WS stream | `ws://localhost:8091/ws/cli` | Y | event-driven | reasoning, content, ledger, tool_call, tool_result | n/a | continuous |
| **Voice backend descriptor** (v2 handoff) | `hud/omega_cli_launch.py:ensure_omega_cli_server` + `OMEGA_CLI_BACKEND_OVERRIDE` env | Y (server-side) | included in launch handshake | `backend_override` | n/a | one-shot at launch |
| CueDesk section 0a short-circuit confirmation | `core/cue_desk.py:tick` → `suppress_reason="pressure_loop_paused"` | **N** | derived from `/api/dev/pause_state` + audit feed | n/a | n/a | 2s |

**Pill badge proposal:** the **dev pill itself** should pulse a small indicator when active (`paused=true && "dev_pill" in owners`). When other owners hold pause (future feature), show amber with tooltip listing owners.

### 3.12 Voice control widget (bottom-left)

| v2 signal | Source | Wired? | Endpoint | Fields | States | Cadence |
|---|---|---|---|---|---|---|
| STT availability | broker → `STT` capability | Y (indirect via `/api/stt/status`) | `GET /api/stt/status` | `ok`, `available` | available=green / down=red | 15s |
| TTS availability | broker → `TTS` capability | Y (indirect via `/api/tts/status`) | `GET /api/tts/status` | `ok`, `available` | same | 15s |
| Listening / idle / error | local JS state | n/a | n/a | UI-driven | listening=cyan / error=red / idle=dim | event |
| **Voice backend override** (dev pill handoff) | env `OMEGA_CLI_BACKEND_OVERRIDE` | **N** (currently invisible to operator) | propose surface in settings | descriptor + base_url | n/a | n/a |

---

## 4. Status glyphs — v2 extensions

The five existing glyphs already cover the right macro-subsystems but their backing logic should be updated to v2 sources:

### 4.1 Existing glyph remappings

| Glyph | Current source | v2 source (recommended) |
|---|---|---|
| `g-voice` | local checks | broker `VOICE` ∨ (`STT` ∧ `TTS`) capabilities |
| `g-identity` | face/anchor probe | broker `FACE` ∧ `read_presence_from_shared_state(ss) != UNKNOWN` |
| `g-guardian` | guardian posture probe | unchanged (already v2-good) |
| `g-memory` | recall probe | broker `EMBED` ∧ memory.api liveness |
| `g-l0` (semantic core) | embedding probe | broker `EMBED` directly |

### 4.2 New glyphs to add

Recommended additions to surface v2-critical state at-a-glance:

| Proposed glyph | Label | Source | Endpoint | States |
|---|---|---|---|---|
| `g-presence` | "presence" | `read_presence_from_shared_state(ss)` | proposed `GET /api/presence/state` | PRESENT=green / ABSENT=amber / UNKNOWN=red |
| `g-pressure-loop` | "pressure" | `pressure_loop_pause_state()` | proposed `GET /api/pressure-loop/state` | running=green / paused-dev=blue / paused-other=amber |
| `g-runtime` | "runtime" | broker `LLM`/`CODING_MODEL` | proposed `GET /api/broker/snapshot` → `coding_model.state` | available=green / degraded=amber / down=red |
| `g-classify` | "classify" | broker `CLASSIFY` | same → `classify.state` | same |

Or, simpler: keep the existing 5 glyphs and expose **all 11 broker capabilities** in a single new panel reachable from the `omega` or new `runtime` pill.

---

## 5. Endpoints to ADD (the work list)

These are the v2 surfaces the substrate already exposes programmatically but the HUD server hasn't published. Each is a small handler in `hud/server.py`.

### 5.1 `GET /api/pressure-loop/state`

**Purpose:** poll pressure-loop pause state for autoloop/dev/deepsleep pills.

**Implementation sketch:**
```python
@app.get("/api/pressure-loop/state")
async def _pressure_loop_state():
    from core.pressure_loop_pause import pressure_loop_pause_state
    return JSONResponse(pressure_loop_pause_state())
```

**Response:**
```json
{ "paused": false, "owners": [] }
```
or
```json
{ "paused": true, "owners": ["dev_pill"] }
```

**Cadence:** 2s. **Cost:** trivial (lock + set copy).

---

### 5.2 `GET /api/presence/state`

**Purpose:** poll operator presence for identity pill + new `g-presence` glyph + response-engine pill.

**Implementation sketch:**
```python
@app.get("/api/presence/state")
async def _presence_state():
    from core.operator_presence import (
        read_presence_from_shared_state,
        enrolled_operator_name,
        presence_is_stale,
    )
    ss = _shared_state()  # existing accessor
    state = read_presence_from_shared_state(ss)
    enrolled = enrolled_operator_name()
    # last heartbeat from shared state side-channel
    heartbeat_iso = ss.read("PRESENCE_WATCHER_HEARTBEAT", "")
    stale = presence_is_stale(heartbeat_iso, _utcnow(), max_age_seconds=90)
    return JSONResponse({
        "state": state,  # PRESENT | ABSENT | UNKNOWN
        "enrolled_operator_name": enrolled,
        "last_heartbeat_iso": heartbeat_iso,
        "watcher_stale": stale,
    })
```

**Response:**
```json
{
  "state": "PRESENT",
  "enrolled_operator_name": "leeno",
  "last_heartbeat_iso": "2026-05-26T14:23:45.123Z",
  "watcher_stale": false
}
```

**Cadence:** 2s. **Notes:** `PRESENCE_WATCHER_HEARTBEAT` side-channel is **WIRED** at `core/operator_presence.py:423-433` (writes `now_fn().isoformat()` through the canonical key from `core/shared_state_keys.py` on every successful watcher pass; failure-soft on shared_state errors). The full `state` + `last_heartbeat_iso` + `watcher_stale` payload is available today.

---

### 5.3 `GET /api/broker/snapshot`

**Purpose:** poll all 11 runtime capabilities at once. Powers any pill that depends on a runtime capability.

**Implementation sketch:**
```python
@app.get("/api/broker/snapshot")
async def _broker_snapshot():
    from core.runtime_capability_broker import runtime_capability_snapshot_dict
    return JSONResponse({"capabilities": runtime_capability_snapshot_dict()})
```

**Response:**
```json
{
  "capabilities": {
    "coding_model": {
      "state": "available",
      "route": "proceed",
      "reason": "",
      "endpoint": "/generate",
      "client_method": "generate_completion",
      "alternate_capability": null
    },
    "embed": { "state": "available", "route": "proceed", ... },
    "classify": { "state": "available", "route": "proceed", ... },
    "face": { "state": "available", "route": "proceed", ... },
    "voice": { ... },
    "image": { ... },
    "stt": { ... },
    "tts": { ... },
    "document_parsing": { ... },
    "specialist_vision": { ... },
    "specialist_video": {
      "state": "degraded",
      "route": "alternate_capability",
      "reason": "client_method_missing",
      "alternate_capability": "specialist_vision"
    }
  }
}
```

**Cadence:** 5s (or piggyback on `/api/status` extension — see §5.7). **Cost:** one runtime `/health` call per snapshot (~10ms). The broker's `_read_health_body` already caches via the runtime client.

---

### 5.4 `GET /api/cuedesk/last_tick`

**Purpose:** expose CueDesk suppress-reason + opened_by + chain trace for autoloop pill.

**Implementation sketch:** CueDesk needs a small side-channel (last_tick_result dict) since `tick()` doesn't currently publish to shared_state. Either:
- Add `LAST_CUEDESK_TICK` canonical key (classification: `tick_boundary_projection`), or
- Read from the tail of `feedback_audit.jsonl` filtered by `loop=cuedesk`.

**Response:**
```json
{
  "last_tick_at": "2026-05-26T14:23:45Z",
  "suppress_reason": "pressure_loop_paused",
  "opened_by": null,
  "pause_owners": ["dev_pill"],
  "idle_probes_run": 0
}
```

**Cadence:** 2s.

---

### 5.5 `GET /api/response/state`

**Purpose:** surface proactive-speech gate state for an operator-facing "talkativity" indicator.

**Implementation sketch:**
```python
@app.get("/api/response/state")
async def _response_state():
    from core.response_engine import _talkativity_state_snapshot  # add this
    return JSONResponse(_talkativity_state_snapshot())
```

**Response:**
```json
{
  "presence_gate": "PRESENT",
  "cooldown_per_act_s": 30.0,
  "cooldown_per_curiosity_s": 90.0,
  "talkativity_window_s": 60.0,
  "talkativity_used": 1,
  "talkativity_cap": 3,
  "last_dispatch_by_act": {
    "ask_question": "2026-05-26T14:23:00Z"
  }
}
```

**Cadence:** 5s.

---

### 5.6 `GET /api/runtime/health`

**Purpose:** expose the runtime's full health body (capability flags + model inventory) for the settings panel or a new "runtime" pill.

**Implementation sketch:**
```python
@app.get("/api/runtime/health")
async def _runtime_health():
    from brain.runtime_client import get_runtime_client
    client = get_runtime_client()
    body = client.health_body() or {}
    return JSONResponse(body)
```

**Response:** depends on omega3.cpp runtime; expect `{ status, capabilities: { embed, classify, face, ... }, models_loaded: [...], gpu_resident_mb, ... }`.

**Cadence:** 10s.

---

### 5.7 Extending `/api/status` (alternative to new endpoints)

Rather than 5+ new endpoints, the simpler patch is to **extend `panel_status()` in `hud/telemetry.py`** to add v2 entries to the existing payload:

```python
{
  "voice": { "state": "live", ... },        # existing
  "identity": { "state": "live", ... },     # existing
  "guardian": { "state": "live", ... },     # existing
  "memory": { "state": "live", ... },       # existing
  "l0": { "state": "live", ... },           # existing
  # v2 additions:
  "presence": { "state": "live" | "degraded" | "down", "label": "presence", "detail": "PRESENT (leeno)" },
  "pressure_loop": { "state": "live" | "loading", "label": "pressure", "detail": "paused by dev_pill" },
  "embed": { "state": "live" | "degraded" | "down", "label": "embed", "detail": "available @ 11434" },
  "classify": { "state": ..., "label": "classify", ... },
  "face": { "state": ..., "label": "face", ... },
  "coding_model": { "state": ..., "label": "llm", ... }
}
```

Then the HUD's existing 2s `/api/status` poll covers everything; only the front-end needs to render the new glyphs.

**Recommended approach for the swarm:** start with §5.7 (extend `/api/status`) for the at-a-glance glyph row. Add §5.1, §5.2, §5.3 separately so panel-detail views can render owner lists, broker reasons, and capability detail.

---

## 6. Signal event catalog (for `/api/events` SSE consumers)

The HUD's existing SSE stream at `/api/events` (`hud/server.py:1636`) already carries all of these. The swarm should add per-pill filters so each pill can subscribe to its relevant signals.

### 6.1 v2-new signals (already emitting)

| Signal name | Emitter | File:line | Fields | Use in HUD |
|---|---|---|---|---|
| `response.presence_gate.held` | response_engine | `core/response_engine.py:490` | `act, source, source_id, presence_state` | counter on identity pill; tooltip "N proactive speeches held due to absent operator" |
| `response.dispatch.result` | response_engine | `core/response_engine.py:568,617,695` | `act, cycle_id, source, source_id` value=0/1 | success/suppression ratio on autoloop pill |
| `response.decision.curiosity` | response_engine | `core/response_engine.py:172,204` | `curiosity_id, act, trigger_source, gap_type` | activity pulse on autoloop pill |
| `response.decision.system_state.count` | response_engine | `core/response_engine.py:349` | `stress_level, aged_open_curiosities, identity_drift_score` | last-batch indicator |
| `response.sweep.dispatched_count` | response_engine | `core/response_engine.py:1053` | `candidates, limit` | per-epoch sweep number on autoloop pill |
| `auto.cycle.{dopamine,plasticity,strength,stress}` | loop_adapters | `core/loop_adapters.py:336,339,342,345` | per-cycle scalars | autoloop pill spark chart |
| `cycle.pressure` (scalar, NOT an SSE signal) | snapshot only | exposed via `/api/telemetry/snapshot` → `cycle.pressure` | per-tick scalar | autoloop pill pressure-band dot — POLL the snapshot, not SSE (no `auto.cycle.pressure` signal emits today; pressure is computed in `core/pressure_field.py` and surfaces via `cycle.pressure` in the telemetry snapshot) |
| `curiosity.decision.should_pursue` | curiosity_engine | `core/curiosity_engine.py:417,432` | `should_pursue` 0/1 | curiosity activity on autoloop |
| `curiosity.route.refused` | curiosity_engine | `core/curiosity_engine.py:487,498,507,511` | reason | refusal counter |
| `identity.self_vector_updated` | loop_adapters | `core/loop_adapters.py` | drift magnitude | identity pill drift indicator |
| `memory.write.summary` | memory.api | (memory write paths) | counts | memory pill activity pulse |
| `memory.arbitration.skipped` | memory.api | (BGE arbitration) | reason | memory pill amber pulse when skipping arbitration |
| `global.grounding.malformed_records` | global loop | failure signal | count | guardian pill warn |
| `global.grounding.pressure_malformed` | global loop | failure signal | count | guardian pill warn |
| `loop_adapters.goal_rejected` | loop_adapters | line 849 | reason | global/meta pill warn |
| `*.failure` (10+ canonical failure names) | various loops | each via emit_signal failure handlers | error | guardian pill red-pulse |

#### 6.1.1 P3-S22 / S24 emit surface (audit-truth breadcrumbs)

| Phase / signal | Emitter | File:line | Fields | Use in HUD |
|---|---|---|---|---|
| `emit_phase("global_action", "pack_mint_*")` (learning_debt / curiosity_gap) | symphony_global_action | `core/symphony_global_action.py:1008, 1112` | `effective_dominant, source_goal_id, mastery_gap, alignment` | autoloop / omega — every minted LAW-grounded goal leaves a breadcrumb so the operator can confirm the cold-start cure fired |
| `emit_phase("global_b", "law_filter")` | symphony_runner | `core/symphony_runner.py` (around `is_grounded_learning_goal` pass) | `goal_count_pre, goal_count_post, rejected_reasons[]` | global / omega — visibility into which committed goals survived the filter |
| `emit_phase("symphony_runner", "ask_suppression")` (`ensure_open_question` / `mark_evidence_terminal_handled`) | symphony_runner | `core/symphony_runner.py` | `outstanding_id, action, terminal_reason` | brain / autoloop — explains why a tick produced no executive movement when one was expected |
| `emit_phase("cue_desk", "preflight_bail")` | cue_desk | `core/cue_desk.py` (around `build_global_action_pack` + NO_ACTION_OBSERVE bail) | `action_signal, episode_id, opened_by` | autoloop / brain — distinguishes healthy idle from frozen-loop pathology (P3-S23) |
| `emit_phase("orchestrator", "tick_state_snapshot", status="ok")` + `emit_phase("orchestrator", "tick_save", status="ok")` | boot loop | `boot.py` (post-tick), via cue_desk | `episode_id, epoch` | runbook §7 expectation — every clean tick leaves the heartbeat pair |
| `emit_phase("episode_wal", "restore_into_substrate")` (drive-through) | episode_wal | `core/episode_wal.py:79` | `episode_id, drive_state` | observability for the WAL-restore drive cure (P3-S24) — distinguishes restored-stalled from restored-driven |
| `emit_signal("neuromod.spike.persist")` | symphony_neuromod | `core/symphony_neuromod.py:382` | autonomic spike payload | autoloop spike indicator |

All emit sites carry P2-S19-pattern refractory windows so a high-rate substrate doesn't flood the audit log.

#### 6.1.2 P3-S25 — in-process audit-events ring (no SSE, no disk-tail)

`guardian/terminal_feedback.py` now exposes a process-local `_RECENT_EVENTS_RING` (deque, `maxlen=5000`) populated by `_publish_event` after the disk audit write lands. Panels consume it via:

```python
from guardian.terminal_feedback import recent_audit_events

events = recent_audit_events(
    predicate=lambda r: r.get("name") == "memory.write.summary",
    limit=10,
    newest_first=True,
)
```

This is the per-emit live-feed channel for sources too high-frequency to push through SharedState on every event (4 of the 6 disk-tail violations cured in P3-S25 use it: `_telemetry_memory.recent_writes`, `_telemetry_guardian.auto_decisions`, `_telemetry_autoloop` autonomic+suppress histogram, `panel_identity.recentEvents`). Doctrine: audit log is source of truth, ring is live feed — the ring must never carry events that didn't reach the forensic record (`disk_committed` gate in `_publish_event`).

### 6.2 v2-new phase emissions (broker)

| Phase | Emitter | File:line | Fields | Use in HUD |
|---|---|---|---|---|
| `broker:<operation>:ok\|degraded` | runtime_capability_broker | `core/runtime_capability_broker.py:414` | `capability, route, state, reason, alternate_capability, cycle_id` | per-operation broker heartbeat — feeds runtime/g-runtime glyph |
| `operator_presence:PRESENCE_ENTER\|PRESENCE_EXIT\|PRESENCE_WATCHER_STALE` | operator_presence | `core/operator_presence.py:222-233` | `confidence, name, last_state` | identity pill state transitions; SSE-driven update |
| `terminal:<phase>:<status>` | terminal_agent.hud_bridge | `terminal_agent/hud_bridge.py:87` | varies | terminal pill activity |

### 6.3 SSE consumption pattern (per pill)

```js
const es = new EventSource('/api/events');
es.addEventListener('signal', (ev) => {
    const data = JSON.parse(ev.data);
    if (data.name === 'response.presence_gate.held') {
        identityPill.bumpHeldCount();
    } else if (data.name?.startsWith('auto.cycle.')) {
        autoloopPill.updateSpark(data.name, data.value);
    }
    // etc.
});
es.addEventListener('phase', (ev) => {
    const data = JSON.parse(ev.data);
    if (data.loop === 'operator_presence') {
        identityPill.onPresenceEvent(data);
    } else if (data.loop === 'broker') {
        runtimeGlyph.onBrokerHeartbeat(data);
    }
});
```

---

## 7. SharedState fields the HUD can read

Reference list — fields the orchestrator's SharedState bus carries that pills may want to project. Source: `core/shared_state_keys.py` (KEY_REGISTRY).

| Key | Classification | Writer | Pill that should read |
|---|---|---|---|
| `LAST_BRAIN_RESPONSE` | durable_projection | brain loop | brain |
| `PRESENCE_STATE` | ephemeral_runtime | operator_presence watcher | identity, dev (gate confirmation) |
| `PRESENCE_WATCHER_HEARTBEAT` | ephemeral_runtime | operator_presence watcher (`core/operator_presence.py:423-433`, written on every successful watcher pass) | identity (stale-watcher detection) |
| `RECENT_OMEGA_EPOCHS` | ephemeral_runtime | omega phase_4_commit auditor (P3-S16; ring of last 20 epochs) | omega (Trajectory Star Map) |
| `RECENT_DEEPSLEEP_CYCLES` (P3-S25) | ephemeral_runtime | deepsleep audit_logger (`metacognition/.../audit_logger.py:log_cycle`; ring of last 5 cycles, published AFTER disk write to `deep_sleep_audit.jsonl`) | deepsleep (Consolidation Flow) |
| `RECENT_GUARDIAN_INTERVENTIONS` (P3-S25) | ephemeral_runtime | `guardian/decision.py:_emit_intervention` (ring of last 50 records, published AFTER disk write to `guardian_interventions.jsonl`) | guardian (Activity Stream interventions card) |
| `RECENT_LAYER_STATES` (P3-S28) | ephemeral_runtime | `core/layer_state.py:emit_layer_state` (ring of last 40 records, published AFTER disk write to `layer_state.jsonl`) | brain (cycle hero + recent layers), memory (recall trace via `brain.retrieve`), guardian (response mode via `brain.translate`), omega (contradiction load via `brain.retrieve`) |
| `RECENT_SELF_VECTORS` (P3-S28) | ephemeral_runtime | `memory/self_vector.py:record_self_vector` (ring of last 20 records, published AFTER disk write to `self_vector_history.jsonl`) | omega (coherence + drift), identity (self-vector trajectory + traits) |
| `LAST_AUTO_CYCLE` | durable_projection | autonomous loop | autoloop |
| `LAST_GLOBAL_OBSERVATIONS` | durable_projection | global loop | omega |
| `LAST_GLOBAL_GOALS` | durable_projection | global loop | omega |
| `LAST_META_ORDERS` | durable_projection | meta loop | omega |
| `LAST_DEEPSLEEP_CONSOLIDATION` | durable_projection | deepsleep | deepsleep |
| `LAST_OMEGA_EVALUATION` | durable_projection | omega loop | omega |
| `LAST_OMEGA_PROPOSALS` | durable_projection | omega loop | omega |
| `LAST_OMEGA_TRAJECTORY` | durable_projection | omega loop | omega |
| `LAST_META_CYCLE` | durable_projection | meta loop | omega, global |
| `LAST_CURIOSITY_CYCLE` | durable_projection | autonomous | autoloop |
| `PENDING_CURIOSITY_RESOLUTIONS` | tick_boundary_projection | autonomous | autoloop |
| `OUTSTANDING_USER_QUESTION` | durable_projection | brain | brain |
| `CONVERSATION_STATE` | durable_projection | brain | brain |
| `OPERATOR_DIRECTIVE` (S6) | tick_boundary_projection | operator receiver | brain, autoloop |
| `LAST_NEUROMOD_SPIKE` (S9) | tick_boundary_projection | symphony_neuromod | autoloop |

**HUD access pattern:** these are accessed transitively through `/api/telemetry/snapshot` which already deep-merges them into the HUD's `Telemetry` singleton. Direct reads would be a SharedState accessor inside a panel handler — most pills don't need this.

---

## 8. State enum & color mapping (cross-pill consistency)

To keep the swarm coherent across pills, use these mappings everywhere:

### 8.1 Broker capability state

| `state` | Color | CSS class | Meaning |
|---|---|---|---|
| `available` | green | `.glyph.live` | runtime up, capability ready |
| `degraded` | amber | `.glyph.warn` | runtime reachable, capability unavailable or method missing |
| `down` | red | `.glyph.error` (new) | runtime unreachable or unhealthy |

### 8.2 Broker route

| `route` | Color | Display |
|---|---|---|
| `proceed` | green | (call runtime) |
| `response_hold` | amber | (record degraded receipt, do not call) |
| `alternate_capability` | blue | (fall back to alternate — show alternate name) |

### 8.3 Presence state

| `state` | Color | Glyph |
|---|---|---|
| `PRESENT` | green | • (filled dot) |
| `ABSENT` | amber | ○ (open circle) |
| `UNKNOWN` | red | ? |

### 8.4 Pressure-loop pause

| State | Color | Glyph |
|---|---|---|
| running (no pause) | green | (none / hidden) |
| paused by `dev_pill` only | blue | ⏸ |
| paused by other owner | amber | ⏸ with owner tooltip |
| paused by multiple | amber | ⏸ with owner count badge |

### 8.5 CueDesk suppress reasons

| `suppress_reason` | Color | Tooltip |
|---|---|---|
| `""` (proceeded) | green | "movement dispatched" |
| `pressure_loop_paused` | blue | "paused by {owners}" |
| `presence_gate_held` | amber | "operator presence={state}" |
| `autonomy_paused` | gray | "legacy autonomy hold" |
| `omega_lock_held` | purple | "omega lock active" |
| `p2_s2_no_action_observe` (P3-S23) | teal | "preflight bail — pure-observe idle tick, chain runner skipped" |

---

## 9. Recommended implementation order for the swarm

If working in parallel waves:

**Wave 1 (backend wiring — no UI):**
1. Add `/api/pressure-loop/state` (§5.1) — trivial, unblocks dev/autoloop/deepsleep
2. Add `/api/presence/state` (§5.2) — small side-channel work in `operator_presence.run_watcher` to also write `PRESENCE_WATCHER_HEARTBEAT`
3. Add `/api/broker/snapshot` (§5.3) — wraps existing `runtime_capability_snapshot_dict()`
4. Extend `panel_status()` per §5.7 to add presence/pressure_loop/embed/classify/face/coding_model entries to `/api/status`

**Wave 2 (front-end glyphs):**
5. Add `g-presence` glyph row (or repurpose `g-identity` to combine)
6. Add `g-pressure-loop` glyph
7. Add `g-runtime` (coding_model) glyph
8. (Optional) `g-embed`, `g-classify` glyphs OR fold into a single "substrate" glyph

**Wave 3 (per-pill v2 badges):**
9. `dev` pill — pulse indicator when `paused && "dev_pill" in owners`
10. `identity` pill — presence-state glyph adjacent to label
11. `autoloop` pill — pause/sweep indicator
12. `omega` pill — tool-synthesis queue depth badge

**Wave 4 (event-driven enrichment):**
13. Subscribe each pill to relevant `/api/events` signals per §6
14. Add audit-stream filter to events panel for the new v2 signal names

**Wave 5 (deferred):**
15. `/api/cuedesk/last_tick` (§5.4) once a `LAST_CUEDESK_TICK` key is added to canonical SharedState
16. `/api/response/state` (§5.5) with snapshot helper in `core/response_engine`
17. `/api/runtime/health` (§5.6) for settings/runtime pill detail

---

## 10. Quick-reference cheat sheet

```text
NAVIGATION PILL    NEW v2 BADGE              SOURCE                                     ENDPOINT (proposed)
─────────────────  ────────────────────────  ─────────────────────────────────────────  ────────────────────────────────
brain              pressure-band dot          cycle.pressure                             /api/telemetry/snapshot (existing)
memory             EMBED state dot            broker.embed.state                         /api/broker/snapshot
guardian           (no change)                                                           /api/panel/guardian
identity           presence-state glyph       PRESENCE_STATE + FACE broker               /api/presence/state + /api/broker/snapshot
omega              tool-synth queue count     omega.tool_synthesis.queue                 /api/telemetry/snapshot
autoloop           pause glyph + sweep count  pressure_loop_pause_state + signals        /api/pressure-loop/state + SSE
deepsleep          active phase number        panel_deepsleep.phase                      /api/panel/deepsleep (existing)
browser            (RETIRED 2026-06-02)       n/a                                        n/a
terminal           skills count (existing)                                               /api/terminal/skills (existing)
settings           (no change)                                                           /api/settings/* (existing)
dev                pause-pulse when active    pressure_loop_pause_state.owners           /api/pressure-loop/state

STATUS GLYPH       v2 SOURCE                  ENDPOINT
─────────────────  ────────────────────────  ────────────────────────────────
g-voice            broker VOICE+STT+TTS       /api/status (extended)
g-identity         broker FACE + PRESENCE     /api/status (extended)
g-guardian         (unchanged)                /api/status
g-memory           broker EMBED + memory      /api/status (extended)
g-l0               broker EMBED               /api/status (extended)
g-presence (NEW)   PRESENCE_STATE             /api/status (extended) or /api/presence/state
g-pressure (NEW)   pressure_loop_pause_state  /api/status (extended) or /api/pressure-loop/state
g-runtime (NEW)    broker CODING_MODEL        /api/status (extended) or /api/broker/snapshot
```

---

## 11. References

- `core/pressure_loop_pause.py` — pause primitive (S17+, [[project_dev_pill_pressure_loop_pause]])
- `core/operator_presence.py` — presence machine (P3-S02)
- `core/response_engine.py` — should_speak_now presence gate (P3-S02, [[project_p3_s02_passed_pushed]])
- `core/runtime_capability_broker.py` — broker + 11 capabilities (P3-S01, [[project_p3_s01_passed_pushed]])
- `core/cue_desk.py` — pressure-gated conductor with section 0a short-circuit
- `core/shared_state_keys.py` — canonical key registry + classifications
- `core/semantic_state_engine.py` — L0 embedding substrate (EMBED capability consumer)
- `brain/runtime_client.py` — runtime client + health body
- `hud/server.py` — current FastAPI surface
- `hud/telemetry.py` — `panel_status()` and panel handlers
- `hud/omega_interface.html` — front-end pills, glyphs, polling loop
- `terminal_agent/hud_bridge.py` — dev pill / terminal agent bridge (currently pauses orchestrator autonomy, NOT pressure loop — see §3.11 note)

---

**End of document.**

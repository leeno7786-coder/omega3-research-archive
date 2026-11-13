# HUD Panel Content Design — v2 Symphony

**Date:** 2026-05-26
**Branch:** `main`
**Audience:** Kimi K2.6 agent swarm building modernized panel UIs
**Companion docs:** `docs/architecture/HUD_PILL_TELEMETRY_V2.md` (telemetry sources), `docs/architecture/SYMPHONY_FULL_INTEGRATION_STATUS.md` (substrate status)

This document specifies **what each of the 11 navigation panels should display** when opened. The telemetry sources, endpoints, and polling cadences are already enumerated in `HUD_PILL_TELEMETRY_V2.md` — this doc is purely about **layout, content, and information hierarchy** so the agent swarm can build the panels as self-contained units.

---

## 0. Design principles (cross-cutting)

These five rules apply to every panel. Apply them ruthlessly.

1. **One hero per panel.** The first thing the operator sees when opening a panel must answer the single question that panel exists to answer. ("What's the brain doing right now?" "Is the operator present?" "What's pending approval?")
2. **Why over what.** Every suppressed/blocked/held state must surface the human-readable *reason*, never just a state code. `suppress_reason="pressure_loop_paused"` becomes "Substrate paused by dev pill" with tooltip "operator engaged dev mode at 14:23:01".
3. **Live tail at the bottom.** Each panel ends with a filtered `/api/events` SSE feed (last ~10 entries) showing the emit_signal/emit_phase events relevant to that subsystem. This is the panel's "vitals monitor."
4. **Inline action where it belongs.** Approval queues, skill toggles, face forget, pause/resume — every actionable state shows its action button on the same row. No "click to open another modal to act."
5. **Empty states are educational.** When a panel has no data (no pending approvals, no open curiosities, no contradictions), say *why that's good* — "All staging refs cleared" / "Operator is present and proactive speech is permitted." Don't show a blank.

**Visual cohesion (cross-panel):**

- **Color palette is fixed** per `HUD_PILL_TELEMETRY_V2.md §8`:
  - green = healthy / proceed / present
  - amber = degraded / held / absent
  - red = down / failure / unknown-fail-closed
  - blue = paused-by-operator / informational
  - gray = idle / loading / not applicable
  - purple = omega lock / governance escalation
- **Density:** Mac-Finder-comfortable, not Bloomberg-terminal. Whitespace between cards, but cards themselves can be dense.
- **Numeric formatting:** scalars to 2 decimal places, percentages with `%`, durations as `12.3s` / `4m 12s` / `2h 45m`, timestamps as relative ("23s ago", "4m ago") with hover-tooltip for ISO 8601.
- **Sparklines** for any time-series scalar (pressure, dopamine, drift, confidence). 60s window default; click to expand to 5min/1hr/24hr.

---

## 1. Cross-panel components (reusable building blocks)

Kimi should build these once and reuse:

### 1.1 `<v2-state-badge>`

Color-coded pill with state text. Inputs: `state`, `tooltip`, optional `meta` (count or value).

```
[ ● PRESENT (0.94 conf) ]   ← green
[ ◐ DEGRADED (broker hold) ]  ← amber
[ ● PAUSED (dev_pill) ]      ← blue
```

### 1.2 `<v2-sparkline>`

60s rolling window, 1-pixel-per-second. Inputs: `signal_name`, `current_value`, optional `band` (color thresholds).

Subscribes to `/api/events` filtered to the named signal; appends `value` on each event. Renders SVG path.

### 1.3 `<v2-approval-row>`

Single row for any pending-approval queue. Inputs: `ref`, `summary`, `kind`, `created_at`, `actions` (approve/deny/escalate buttons).

Used in: `guardian` (staging refs), `browser` (action requests), `memory` (promotions), `terminal` (trust_required).

### 1.4 `<v2-signal-tail>`

Last N (default 10) signals/phases for a given filter. Inputs: `filter` (loop name or signal-name prefix), `limit`.

Reverse-chronological. Each row: timestamp · loop · phase/signal name · value · narrative (truncated, hover for full).

### 1.5 `<v2-coord-chip>`

Tiny chip showing a memory coordinate `[X, Y, Z]` with lobe-name tooltip.

```
[ 1·0 persona ]   [ 2·3·z Z-routed ]   [ 7·1 vision ]
```

### 1.6 `<v2-broker-dot>`

Single-letter capability dot. Hover shows full status (state + route + reason). Used in headers to indicate which capabilities the panel depends on.

```
E·C·F·L   ← Embed, Classify, Face, LLM (all green)
E·c·F·L   ← Classify degraded (lowercase + amber)
```

### 1.7 `<v2-camera-thumb>`

Small (96×96) face anchor thumbnail. Used in identity panel + browser sandbox + presence gate live view.

---

## 2. Panel-by-panel content specs

Each panel section below has:

- **Hero** — the top-of-panel element
- **Cards** — secondary widgets, in display order
- **Live tail** — what the signal feed filters to
- **Interactions** — operator actions inline in this panel
- **Empty states** — what to show when nothing's happening
- **Data sources** — references back to `HUD_PILL_TELEMETRY_V2.md` sections

---

## 2.1 `brain` panel — "What is Omega thinking right now?"

This is the **cognitive primary** panel. Most-opened. Most-watched.

> **P3-S10 (2026-05-26):** Layer-3 Wave-1C rebuild landed. The panel now renders the **Cycle Wheel** (§9.2 signature visual — SVG, 280px diameter, 11 segments one per layer registry entry, active segment highlighted, rotating spotlight arc traces cadence, pressure halo around the wheel with band-color tint (lo/med/hi from §8.1 thresholds), contributor satellites orbiting at radius proportional to weight) above a 5-card grid (conversation state / cycle traversal / cuedesk last tick / top contributors / recent layers).
>
> Backend (`hud/telemetry.py:panel_brain`) extended with six new top-level fields: `cycle_layers[]` (the layer registry exposed as `{idx, key, label}` so the renderer doesn't hardcode the table), `cycle_active_index` (parsed from `cycle.layer`'s leading integer), `contributors[]` (top 6 pressure signals, same shape as autoloop), `suppress_reason` + `opened_by` (from `LAST_CUEDESK_TICK` shared-state snapshot), and `conversation{outstanding_question, last_response_excerpt, cycle_counter, last_user_turn_at}` from shared state.
>
> When `suppress_reason` is non-empty the wheel freezes mid-rotation (`data-suppressed="true"` flips `animation-play-state: paused` on the spotlight; center text replaces cycle id with the human-readable suppress reason). Suppress-state and pressure-band changes flip `data-suppressed`/`data-band` on the wrapper via `_refreshCycleWheelState()` wired into `pollV2Fast` — reuses the `system.v2.cuedesk_last_tick` stream already published by the P3-S06 wiring foundation.
>
> Particle ignition / comet on dispatch (spec §9.2 final animation hook) deferred — render hook `window.OmegaBrain.igniteDispatch(...)` not yet wired to SSE; the visual passes the cascade as-is.

### Hero
**Current cycle card** — large, centered.
- Cycle ID (mono font)
- Layer position dial (current of N layers; visual "where in the cycle are we")
- Pressure scalar with sparkline (60s) + contributors breakdown (top 3 contributors: e.g., "curiosity_gap 0.42, operator_intent 0.28, contradiction 0.15")
- LIVE flag (green) or stale-indicator if cycle hasn't ticked in > 8s

**Source:** `/api/telemetry/snapshot` → `cycle.{cycle_id, source, layer, pressure}` (2s)

### Cards

**Card A: Conversation state**
- Outstanding user question (if any) — bold, top
- Last brain response excerpt (~150 chars, hover for full)
- Conversation cycle counter
- Last user turn timestamp ("23s ago")

**Source:** `OUTSTANDING_USER_QUESTION`, `LAST_BRAIN_RESPONSE`, `CONVERSATION_STATE` via snapshot.

**Card B: Last response decision**
- Dialogue act picked (e.g., "ask_question", "share_observation", "silence")
- Trigger source (curiosity_id or system_state)
- **Was it dispatched or held?** — if held, show reason (presence_gate / cooldown / talkativity / suppressed by broker hold)
- Time since last dispatch per act type (refractory windows visualized as fading dots)

**Source:** `/api/events` filter `name=response.decision.*` + `name=response.dispatch.result` + `name=response.presence_gate.held`

**Card C: Presence + speech gate**
- Presence state badge (`<v2-state-badge>`)
- "Proactive speech: PERMITTED / HELD" — derived from presence + cooldown state
- If HELD: which gate is holding (presence / cooldown_per_act / cooldown_per_curiosity / talkativity)
- Mini-graph: held vs dispatched count (last hour)

**Source:** `/api/presence/state` (proposed) + `response.presence_gate.held` signal stream.

**Card D: CueDesk last tick**
- `suppress_reason` (color from `HUD_PILL_TELEMETRY_V2 §8.5` — including `p2_s2_no_action_observe` (teal) added in P3-S23 for the preflight NO_ACTION_OBSERVE bail, which is now the most common idle-tick suppress reason on a quiet substrate)
- `opened_by` contributor (if movement dispatched)
- Idle probes run this tick (count)
- Pause owners (if paused)

**Source:** `LAST_CUEDESK_TICK` SharedState (P3-S06) — wired into `panel_brain` at `hud/telemetry.py:2527-2535`; `/api/cuedesk/last_tick` snapshot endpoint.

**Card E: Recent layers**
- Tail of last 6 `layer_state` records
- Each row: layer index · cycle_id · source · pressure · timestamp
- Click row → highlight that cycle's events in the live tail below

**Source:** `/api/telemetry/snapshot` → `layers[]`

### Live tail
Filter: loops `brain | response | curiosity`. Last 10 entries.

### Interactions
- Click cycle ID → copy to clipboard
- Click contributor in pressure breakdown → opens autoloop panel scoped to that contributor
- Click outstanding user question → focus chat input

### Empty states
- No cycle yet: "Substrate booting — first tick in N seconds" with phase indicator
- Substrate paused: prominent blue banner "Pressure loop paused by {owners} — brain frozen at cycle {id}. {N} pending decisions will resume when released."
- No outstanding question: "Conversation idle — no question pending."

---

## 2.2 `memory` panel — "What does Omega know, and what's pending to remember?"

> **P3-S21 (2026-05-27):** The settings → memory_graph sub-panel (Kimi's drop-in interactive node graph, separate from this panel's hero) is now plugged into the live backend. `MemoryGraph.init` used to render a hardcoded `generateMockTree()`; the settings-integration shim now also fetches `/api/memory/lobes?counts=true`, stamps per-coord write counts onto the canonical taxonomy, polls every 10s while the view is mounted, and subscribes to `memory.write.summary` SSE events for immediate refresh on commit. Polling + SSE listening fire ONLY while the view is open per the live-feed doctrine — `destroyMemoryGraph` tears them down. See SLICE_BOARD P3-S21 + cascade scenario `memory_graph_realtime_v1`.
>
> **P3-S14 (2026-05-27):** Layer-3 Wave-2C rebuild landed. The panel now renders the **Recall Snapshot hero** (top-K results with similarity + coord chip + gist; recall mode badge — `semantic` patina / `lexical_fallback` copper — that flips amber when broker EMBED is degraded; EMBED + CLASSIFY state badges; recall trace mini-readout) above a 5-card grid: pending promotions (near-empty under P3-S11 doctrine) / recent memory writes / persistence anchors (top 6 from StrengthIndex) / L0 substrate state / **Lobe Activity heatmap** (10×10 SVG grid colored by last-hour write density, 6 tier gradient dim→ember).
>
> The full **§9.1 "Bloom"** (animated lobe field with pulse-on-write, co-recall edges drawn between nodes that share top-K hits, drifting particle background representing embedding-space density) is **deferred to a follow-up slice**. This slice ships the Bloom-lite: a static 10×10 heatmap that gives the operator the same per-lobe write-density read at-a-glance without the animation complexity. The full Bloom remains the long-arc visual ambition for the memory panel.
>
> Backend (`hud/telemetry.py:_telemetry_memory`) extended with 7 new top-level fields: `recent_writes[]` (last 10 `memory.write.summary` signals), `lobe_activity{}` (coord-key → write count, last hour), `lobe_activity_total`, `l0_state{embed, classify}`, `recall_mode` ("semantic" | "lexical_fallback"), `persistence_anchors[]` (top-8 by `write_strength` from StrengthIndex), `pending_promotions[]` (forensic surface; near-empty under doctrine). Tail window 256KB (banked P3-S08 reviewer lesson).

### Hero
**Recall snapshot card** — what was most recently recalled.
- Top recall query (last user input or autonomous trigger)
- Top-K results (default 5) as rows: similarity score · coord chip · excerpt (~100 chars)
- Recall mode (semantic / lexical-fallback) — **amber if EMBED is degraded and fell back to lexical**

**Source:** `/api/panel/memory` (already wired) — augment with broker EMBED state from `/api/broker/snapshot`.

### Cards

**Card A: Pending memory promotions**
- Approval queue using `<v2-approval-row>`
- Each row: staging ref · kind (insight / contradiction / trait / etc.) · created_at · approve/deny/escalate buttons
- Empty state: "No memory promotions pending — Guardian channel clear."

**Source:** `panel_memory.pending_approvals` (already in snapshot) + `POST /api/guardian/approve|deny|escalate`

**Card B: Recent memory writes**
- Last 10 write operations as compact rows: timestamp · op (write/update/promote) · coord chip · candidate count · narrative
- Color: green=write, blue=update, purple=promotion, amber=arbitration_skipped

**Source:** `/api/events` filter `name=memory.write.*` and `name=memory.arbitration.*`

**Card C: Lobe activity heatmap**
- 10×10 grid (X coord × Y coord) showing recent write density per lobe
- Cell color = write count in last hour (gradient: dim → bright)
- Hover cell → tooltip with lobe name + write count + last write timestamp
- Click cell → filter live tail to that coord

**Source:** Aggregate from `memory.write.summary` signals (group by coord). Could also be served from a new `/api/memory/lobe-activity` endpoint.

**Card D: Persistence anchors (StrengthIndex)**
- Top 8 strongest memory anchors as rows: strength score · coord · summary (~80 chars)
- This is the "what Omega refuses to forget" view.

**Source:** `panel_memory.persistence_anchors` (already in snapshot)

**Card E: L0 substrate state**
- EMBED capability badge (`<v2-broker-dot>`)
- Last embedding latency (from broker heartbeat)
- Last classification coord (CLASSIFY broker)
- L0 dimension currently in use (128/256/512/768 — MRL)

**Source:** `/api/broker/snapshot` → `embed`, `classify`

### Live tail
Filter: loops `memory | global` + signal names starting `memory.*`. Last 10.

### Interactions
- Inline approve/deny/escalate on promotions
- Click coord chip in any row → opens a coord-scoped detail (could be modal or fly-out)
- Click recall result → show full memory record

### Empty states
- No recall yet: "No recall queries this session."
- No promotions: "Memory channel clear — no pending promotions."
- EMBED down: red banner "L0 substrate offline — recall falling back to lexical only. Recommendations and identity stability are degraded."

---

## 2.3 `guardian` panel — "Is Omega's boundary defense healthy?"

> **P3-S11 (2026-05-27) — doctrine reframe.** Guardian's role is **outside-poisoning defense**, not memory gatekeeping. Omega auto-commits its own consolidations (DeepSleep writes directly via `write_memory(bypass_guardian=True)`; the 120s sleep cycle handles consolidation, dedup, garbage-prune, and orphaned-staging sweep autonomously). Guardian's verify chain still runs on memory derived from conversation (poisoning defense against adversarial user input) and on tool synthesis (Guardian's 4-gate auto-approval cycle). The operator-facing approval queue is a near-empty forensic widget — if items accumulate there it means Guardian flagged genuine outside corruption, not Omega's own cognition.
>
> **P3-S12 (2026-05-27) — tool doctrine lock.** Tool synthesis auto-approves through `guardian.tool_authority.authorize_tool`'s 5-phase pipeline (INSPECT → SANDBOX → DECIDE → WRITE → AUDIT). The 4-gate sandbox tests every synthesized tool with synthesized arguments under a hard timeout; PASS auto-writes the canonical schema + cognitive lobe entry, FAIL audits + drops the spec. **Tools NEVER appear in `panel_guardian.pending_approvals`.** Cascade scenario `tool_authority_doctrine_v1` locks the no-operator-gate invariant; phase_4_commit emits enter/exit/fail breadcrumbs around every `authorize_tool(spec)` call so silent failures surface in `feedback_audit.jsonl` even if the dedicated `tool_authority.jsonl` audit doesn't get written.
>
> **The §9.7 "Approval Stack" hero is anti-pattern under this doctrine.** A queue that demands operator approval for Omega's own memory consolidations contradicts the memory-first autonomous-system design — "the user has to constantly switch tabs to keep approving everything and that is systems death." It is retired (see §9.7 below) and the original Card A "primary action surface" framing is reframed below to a small forensic surface.

This panel is governance-vitals at-a-glance. Optimize for **fast read**, not fast action.

### Hero
**Posture + integrity card** — the "governance vitals."
- Posture mode (e.g., "supervised / autonomous / dev")
- HMAC integrity check status (green/red)
- Pending approval count — **large number, color-coded** (0=green, 1-3=amber, >3=red)
- Recent intervention count (last hour)

**Source:** `/api/panel/guardian` (already wired)

### Cards

**Card A: Auto-approval activity feed** *(re-scoped P3-S13)*
- This card is the **drilldown for the Activity Stream hero** (§9.7) — same data, expanded view.
- Three subsections, one per decision-kind: tool decisions / memory writes / boundary blocks. Each shows the last 5 unified rows from `panel_guardian.auto_decisions`.
- Boundary blocks card stays empty in normal operation ("channel clear · no outside corruption flagged"). When it fills, Guardian caught actual poisoning attempts the auto-pipeline rejected — operator can drill into the audit detail.
- Memory promotions from DeepSleep DO appear here (as `memory` rows) — but as ACTIVITY, not approval items. They auto-committed by the time you read them.

**Source:** `panel_guardian.auto_decisions` (P3-S13 unified stream) + `panel_guardian.interventions` (existing intervention records).

**Card B: Recent interventions timeline**
- Last 10 governance interventions (Guardian-issued holds, rollbacks, escalations) as vertical timeline
- Each: timestamp · intervention type · target ref · operator who approved/escalated

**Source:** `panel_guardian.interventions[]`

**Card C: Doctrine compliance**
- 9-principle compliance dashboard (one row per principle)
- Each row: principle name · status (compliant/warned/retired) · last check timestamp
- If any retired: red callout at top

**Source:** `/api/omega/doctrine_audit` (60s cached)

**Card D: Action approvals (browser agent)**
- Pending browser action requests requiring operator approval
- Each row: request_id · origin · action_type · field/url · approve/deny buttons
- Critical when browser sandbox is active — these are sensitive (form fills, destructive actions)

**Source:** `/api/browser-agent/inbox?limit=12` filtered to `requires_operator_approval=true`

**Card E: Site-policy receipts**
- Recent non-executing site-policy receipts (last 5)
- Each: receipt_id · url · status · timestamp

**Source:** `/api/browser-agent/site-policy/receipt` POST history (need a list endpoint) — for now, derive from `/api/events` filter `phase=guardian.site_policy`

### Live tail
Filter: loops `guardian | governance | broker` (degraded only). Last 10.

### Interactions
- Bulk-approve / bulk-deny buttons at top of approval card (with confirm)
- Click ref → `/api/guardian/inspect?ref=<ref>` to load full payload in modal
- Click intervention → drill into the originating signal

### Empty states
- No approvals: large green check "Governance channel clear" — operator can relax
- Posture changed: amber banner explaining new posture and what it permits

---

## 2.4 `identity` panel — "Who is Omega seeing, and who is Omega becoming?"

⭐ **The signature panel.** This is where v2's face/presence work pays off visually. Build this one ambitious.

> **P3-S08 (2026-05-26):** Layer-3 Wave-1A rebuild landed. The panel now renders the **Presence Orb** (§9.3 signature visual — SVG, 320px, tints by presence state, embeds enrolled face thumbnails on an inner ring, orbits trait moons on the outer ring, halo breathes, drift ring spins when self-vector velocity > 0.01) above a six-card grid (operators / self-vector / traits / voice / closed-world / recent events).
>
> Backend (`hud/telemetry.py:panel_identity`) extended with three new top-level keys (`traits`, `closedWorld`, `recentEvents`) and a `thumbnail_url` per-operator field; face thumbnails are served from `MEMORY_ROOT/700_vision/images` (Dewey vision lobe coord 7,0) via `/api/vision/anchor-thumbnail/{name}`. `closedWorld.sealed` derives from `mode == "production" AND verify_ok AND ledger_ok`. `recentEvents` tails `feedback_audit.jsonl` and filters to `identity.*` / `response.presence_gate.*` / presence / face / anchor signals + phases.
>
> The orb consumes presence state from `Telemetry.system.v2.presence` (kept current by the P3-S06 fast/medium polling); presence-state changes flip `data-state` on the orb wrapper via `_refreshPresenceOrbState()` (wired into `pollV2Fast` + `pollV2Medium`) — CSS tints take over without a full panel re-render. Boot-cascade scenario `identity_panel_orb_v3` validates all of the above.

### Hero
**Live presence card** — split layout.

**Left half:**
- Live presence state badge (large)
- Confidence dial (0..1) with current value
- Below dial: "Operator: {enrolled_name} · last seen {relative}"
- Acquire/release threshold marks on dial (so operator sees hysteresis bands)

**Right half:**
- Live face anchor thumbnail of currently-recognized operator (or placeholder if ABSENT)
- "Frame {N} of acquire window" — counts up to acquire_frames during transition

**Source:** `/api/presence/state` (proposed) + `/api/vision/anchors` + occasional `/api/vision/identify` from cached frame.

### Cards

**Card A: Enrolled operators**
- Grid of face anchor thumbnails (one per enrolled name)
- Below each: name · enrollment date · "currently recognized" badge if matches
- Per-row actions: `forget` button (calls `/api/vision/forget`)
- "+ Enroll new" button → opens enroll flow (webcam capture → `/api/vision/enroll`)

**Source:** `/api/vision/anchors` (already wired) + `/api/vision/remember-face` for new enrollment

**Card B: Self-vector + drift**
- Current self-vector summary (top traits)
- Drift sparkline (60s default — extend to 24h on click)
- Latest drift magnitude with color (>0.20 = red, 0.05-0.20 = amber, <0.05 = green)
- "Last drift event: {when} — {magnitude}"

**Source:** `panel_identity.self_vector` (already in snapshot) + `/api/events` filter `name=identity.self_vector_updated`

**Card C: Traits + coherence**
- Top 5 traits with strength bars
- Coherence score (single number, color-coded)
- "Contradiction watch: {N} open" — links to omega panel contradiction section

**Source:** `panel_omega` → `traits`, `coherence`, `contradiction` (yes, identity reads from omega summary; that's intentional — these are the operator-facing identity facets)

**Card D: Voice signature**
- Current TTS backend (e.g., "Piper en-gb-x-rp 22050Hz")
- STT backend (e.g., "Whisper small.en INT8")
- Voice intake state (listening / idle)
- **Voice backend override (if dev pill engaged):** show override descriptor → indicates dev agent is using different voice config

**Source:** `panel_identity.voice` (already in snapshot) + `/api/tts/status` + `/api/stt/status` + env `OMEGA_CLI_BACKEND_OVERRIDE` (needs new endpoint to expose)

**Card E: Closed-world enrollment status**
- Single state line at bottom: "Operator enrollment: {name} (closed-world)" green, or "No enrollment — proactive speech disabled" red
- Tooltip: "Omega's no-enroll-no-proactive rule: presence can never latch PRESENT without an enrolled operator name."

**Source:** `enrolled_operator_name()` via `/api/presence/state`

**Card F: Recent presence events**
- Last 5 PRESENCE_ENTER / PRESENCE_EXIT / PRESENCE_WATCHER_STALE events
- Each row: timestamp · event · confidence · summary

**Source:** `/api/events` filter `loop=operator_presence`

### Live tail
Filter: loops `operator_presence | identity` + signal `identity.*`. Last 10.

### Interactions
- Enroll new face (modal with webcam capture)
- Forget face (confirm dialog)
- Click drift event → show contextual cycle info

### Empty states
- No enrollment: prominent red "No operator enrolled — closed-world presence cannot engage. Enroll your face to unlock proactive speech." with big "Enroll" CTA
- ABSENT: amber banner "Operator not visible — proactive speech held. Reactive paths still active."
- UNKNOWN (face module down): red banner "Face module unavailable — holding last known state. Investigate runtime."

---

## 2.5 `omega` panel — "Where is Omega heading, what does she lack, and what is she synthesizing?"

> **P3-S15 (2026-05-27):** Layer-3 Wave-3A rebuild landed. The panel now renders the **Trajectory Star Map** (§9.5 signature visual — SVG constellation of the last 20 epochs from `guardian/logs/omega_audit.jsonl`, stars positioned along a chronological rail left=oldest/right=newest, size scales with coherence + activity volume, color from direction tag — `stable` cool-blue / `changing` warm-orange / `contradiction_rich` red / `consolidation` gold). Current epoch is the brightest star with a 2.6s pulse keyframe. Edges connect consecutive epochs via gradient `<linearGradient>` strokes fading between epoch colors. Open contradictions float as red `✱` asterisks near the current star; queued tool-synthesis requests render as small comet lines entering from the right edge toward the current epoch.
>
> Backend (`hud/telemetry.py:_telemetry_omega`) extended with `recent_epochs[]` — tails `guardian/logs/omega_audit.jsonl` for last 20 epoch records, computes per-epoch `coherence` (applied / total) and a `direction` tag derived from the apply:reject ratio (contradiction_rich when rejected ≥ 3 AND rejected > applied; consolidation when applied ≥ 5 AND rejected == 0; changing on mixed activity; stable on low overall activity).
>
> Hero followed by a compact "becoming" strip (objective + online + posture + coherence one-liner) and 5 cards via `_idCard`: traits + deltas (top 6) / open contradictions / capability gaps / tool synthesis queue summary / DeepSleep journal stats.

The **meta-cognitive view** — what omega knows about itself.

### Hero
**Objective + trajectory card** — split layout.

**Left:**
- Current objective (top-level mission statement, ~150 chars)
- Online days counter (large)
- Coherence score (0..1, with band color)

**Right:**
- Trajectory summary (`LAST_OMEGA_TRAJECTORY`) — last 3 epochs as compact tiles
- Each tile: epoch index · directional pressure summary · timestamp
- Click tile → expand into a multi-epoch trajectory view

**Source:** `panel_omega.{objective, online_days, coherence, trajectory}` (already in snapshot)

### Cards

**Card A: Traits (full)**
- All traits with strength bars + recent delta arrows (↑/↓ since last meta-cycle)
- Hover trait → narrative explanation

**Source:** `panel_omega.traits`

**Card B: Open contradictions**
- List of currently-open contradictions
- Each: contradiction summary · trait_a vs trait_b · age · cycle_id when surfaced
- Empty: "No open contradictions — internal consistency stable."

**Source:** `panel_omega.contradiction.open[]`

**Card C: Capability gaps**
- What omega has flagged as missing (e.g., "I cannot do real-time video analysis", "I lack access to X tool")
- Click gap → opens tool-synthesis flow

**Source:** `panel_omega.gaps`

**Card D: Tool synthesis queue** ⭐
- Queue tile: pending count · running count · recently-completed count
- Recent invocations table: tool name · request source · status · latency
- "+ Request tool synthesis" button → opens form (calls `/api/omega/request_tool`)

**Source:** `panel_omega.tool_synthesis` (already in snapshot) + `/api/omega/request_tool` for new requests

**Card E: Recent proposals**
- `LAST_OMEGA_PROPOSALS` — what omega has proposed to do in recent cycles
- Each: proposal id · summary · status (proposed/accepted/declined) · cycle_id

**Source:** snapshot → `omega.proposals[]`

**Card E2: Pack-minted LAW-grounded goals (P3-S22)**
- `LAST_GLOBAL_GOALS` — committed goals from the global action-pack mint layer (filtered through `is_grounded_learning_goal`)
- Each row: source contributor (`learning_debt` / `curiosity_gap`) · goal summary · `mastery_gap` · trajectory alignment status (`aligned` / `unaligned` / `no_alignment_evidence`) · priority (medium / high — promoted to high on active alignment)
- Empty: "Substrate idle — no LAW-grounded goals minted this epoch. Cold-start cure dormant by design (no learning_debt / curiosity_gap dominant)."
- Doctrinal note: minted at the pack layer (not via the dead phase-1→2→3 mill); single-goal cap per dominant contributor; minter mirrors `read_curiosity_gap`'s ASK/HOLD exclusion to prevent double-routing.

**Source:** snapshot → `omega.committed_goals[]` / `omega.phases.phase_3` / `omega.phases.phase_4_emission.primary` / `omega.telemetry` (projected by `run_global_b_movement`).

**Card F: Last meta cycle**
- `LAST_META_CYCLE` final verdict — what meta loop decided last
- Outcome status · timestamp · summary

**Source:** snapshot → `meta` slice

**Card G: Journal recent entries**
- Last 5 omega journal entries
- Each: timestamp · entry excerpt · associated cycle

**Source:** `panel_omega.journal` — read-only

**Card H: Capability timeline**
- Horizontal timeline showing when capabilities came online / went offline
- Tick marks for each capability state change

**Source:** `panel_omega.capability_timeline`

**Card I: Doctrine compliance summary**
- One-line per principle (9 of them) with state
- Click → opens full doctrine_audit view (Guardian panel has the detailed view; this is the omega-facing summary)

**Source:** `/api/omega/doctrine_audit`

### Live tail
Filter: loops `omega | meta | global`. Last 15 (omega is the slow-pulse loop — fewer events).

### Interactions
- Request tool synthesis (modal form)
- Mark contradiction as resolved (writes to memory) — guarded by Guardian approval
- Click trajectory tile → expand multi-epoch view

### Empty states
- No proposals: "Omega quiet — no proposals this epoch."
- No tool synthesis: "No tools synthesized this session — operator can request one via the button."

---

## 2.6 `autoloop` panel — "What is the substrate's autonomic state?"

The **vitals dashboard.** Lots of sparklines. Lots of color.

> **P3-S09 (2026-05-26):** Layer-3 Wave-1B rebuild landed. The panel now renders the **Respiratory Field** (§9.4 signature visual — SVG, full-width hero, breathing pressure dial at the center of an orbital field of curiosity particles, 4 vertical autonomic sparklines beneath, 7-segment suppress-histogram stripe at the top, full pause overlay with owners listed when `pause_state.paused`) above a 4-card grid (open curiosities / cadence + contributors / pressure-loop pause / last cycle + suppress histogram totals).
>
> Backend (`hud/telemetry.py:_telemetry_autoloop`) extended with four new top-level fields: `autonomic{dopamine[], plasticity[], strength[], stress[]}` (last 30 samples per signal, tailed from `feedback_audit.jsonl`), `suppress_histogram{proceeded, pressure_loop_paused, presence_gate_held, autonomy_paused, omega_lock_held, below_threshold, other, total}` (last-hour rollup of `loop=cue_desk` `phase=tick` records), `pause_state{paused, owners[]}` (via `core.pressure_loop_pause.pressure_loop_pause_state()`), and `last_cycle_at` ISO timestamp. Tail window bumped from 64KB → 256KB per banked P3-S08 reviewer lesson.
>
> The dial's breathing animation runs at the cadence interval (`--rf-breathe-duration` CSS variable from `cadence.currentInterval`); when paused, animation-play-state freezes mid-cycle and the entire field desaturates. Curiosity particles orbit at periods varied by urgency (high urgency = faster orbit); particle radius shrinks inward as pressure rises. Pause-state changes flip `data-paused` on the wrapper via `_refreshRespiratoryPauseState()` wired into `pollV2Fast` — same lightweight pattern as the orb's `_refreshPresenceOrbState`.
>
> Scope: this slice ships 4 of the 8 spec cards (B/C subset + E/F-equivalents folded into cadence+contributors); cards A and D are absorbed into the hero (autonomic dashboard + suppress histogram are the hero's lower strip and top stripe); cards G (sweep results) and H (idle probe findings) deferred to a follow-up slice. Particle ignition on dispatch (spec §9.4) deferred — render hook `window.OmegaRespiratory.igniteParticle(name)` not yet wired to SSE; the visual passes the cascade as-is.

### Hero
**Pressure dial + pause state** — split.

**Left (large):**
- Pressure dial (0..1) — large, prominent, color-banded
- Below dial: 60s pressure sparkline
- Contributors as small bars: top 5 pressure contributors with weights

**Right:**
- Pressure-loop pause badge (`<v2-state-badge>`) — owners listed
- Cycle cadence: current Hz vs target Hz
- Last cycle elapsed
- "Substrate: LIVE / PAUSED" mega-indicator

**Source:** `/api/telemetry/snapshot` → `cycle.pressure` + `/api/pressure-loop/state` + `panel_autoloop.cadence`

### Cards

**Card A: Autonomic dashboard** ⭐ (the showpiece)
- 4 sparklines side-by-side, color-coded:
  - Dopamine (signal `auto.cycle.dopamine` or `autonomic.dopamine`)
  - Plasticity (`auto.cycle.plasticity`)
  - Strength (`auto.cycle.strength`)
  - Stress (`auto.cycle.stress`)
- Each sparkline: 60s window, current value labeled, threshold bands
- Hover → expand to 5min view

**Source:** `/api/events` filter signal names above

**Card B: Open curiosities**
- Tile grid of open curiosity entries (up to 12 visible, "+N more" indicator)
- Each tile: curiosity_id · gap_type · age · trigger_source
- Color: fresh=green, aging=amber, stale=red
- Click tile → drill into the curiosity record

**Source:** `panel_autoloop.curiosity.open[]`

**Card C: Recent decisions**
- Last 10 response engine decisions
- Each row: timestamp · act · trigger_source · dispatched? (green check / amber held) · reason if held

**Source:** `/api/events` filter signal `response.decision.*` and `response.dispatch.result` and `response.presence_gate.held`

**Card D: CueDesk suppress histogram**
- Stacked bar (last hour) showing why CueDesk didn't dispatch:
  - pressure_loop_paused (blue)
  - presence_gate_held (amber)
  - autonomy_paused (gray)
  - omega_lock_held (purple)
  - p2_s2_no_action_observe (teal) — P3-S23 preflight bail; expected to dominate on a quiet substrate (every pure-observe idle tick lands here)
  - below_threshold (dim gray) — aggregate pressure < 0.55
  - proceeded (green)
- Total tick count below

**Source:** `panel_autoloop.suppress_histogram` (P3-S09, last-hour rollup of `loop=cue_desk` `phase=tick` records from `feedback_audit.jsonl`).

**Card E: Operator directive (S6)**
- Current `OPERATOR_DIRECTIVE` value with TTL countdown bar
- If empty: "No active directive — substrate is in default-mode."
- If active: directive text · expires in {time}

**Source:** `OPERATOR_DIRECTIVE` from snapshot

**Card F: Neuromod spike (S9)**
- `NEUROMOD_SPIKE_PROJECTION` scalar with sparkline
- Body-state spike indicator (visual: pulse animation when > threshold)

**Source:** `NEUROMOD_SPIKE_PROJECTION` from snapshot + `/api/events` signal `neuromod.*`

**Card G: Recent sweep results**
- Last 5 response sweeps: timestamp · dispatched count · candidates · limit
- Trend line: dispatched/candidates ratio (success rate)

**Source:** `/api/events` filter signal `response.sweep.dispatched_count`

**Card H: Idle probe findings**
- Last 5 idle episodic probes that returned findings
- Each: probe class · finding summary · timestamp

**Source:** `panel_autoloop.idle_probes` (need to expose from `CueDesk.curiosity_substrate_tick` return dict — currently not in panel)

### Live tail
Filter: loops `autonomous | meta | curiosity | cuedesk | response`. Last 15.

### Interactions
- Pause/resume buttons (operator-initiated pressure-loop pause — different from dev pill)
  - Tooltip: "Manually pause cognition for {reason}"
  - Calls `POST /api/orchestrator/agent-engagement` with operator-set owner
- Click curiosity tile → drill-in
- Click sparkline → expand to 1hr view

### Empty states
- Paused: large blue overlay "Substrate paused by {owners}. All sparklines frozen at last value. {N} pending decisions queued."
- No curiosities: "Substrate calm — no open curiosity gaps."

---

## 2.7 `deepsleep` panel — "What is Omega consolidating?"

> **P3-S18 (2026-05-27):** Layer-3 Wave-3B rebuild landed. The panel now renders the **Consolidation Flow** (§9.6 signature visual — SVG, 4 phase nodes laid out horizontally P1 → P2 → P3 → P4, each colored per spec (intake persona-warm / reactivation trait-gold / reconciliation contradiction-amber / persistence trajectory-blue). Pending = outlined dim, completed = solid with check mark, active = pulsing solid. Connecting dashed lines between nodes "march" (animated stroke-dashoffset) when both endpoint nodes are completed. Output summaries render below each node (e.g., P3 shows "N contradictions · M forgot"; P4 shows "N promoted · M pruned").
>
> Backend (`hud/telemetry.py:panel_deepsleep`) extended with 3 new top-level fields: `phase_states[]` (4 entries with `phase` / `name` / `status` / `summary`), `current_phase` (active phase number 1-4 or None), `next_cycle_eta` (relative age string, e.g., "last cycle 21m ago" — substantive ETA-to-next-cycle requires a live scheduler signal which doesn't yet exist; current implementation reports last-cycle age).
>
> Hero followed by a compact cycle-header strip (id + audit + timestamp + age) and 5 cards via `_idCard`: phase 1 intake / phase 2 reactivation / phase 3 reconciliation / phase 4 persistence / engine + cascade. Each card tones values by outcome (sealed when phase produced output, broken when guardian_review_required, etc.).
>
> **Deferred:** The full §9.6 "Flowing particles between nodes" (particle streams visualizing records flowing between phases, with particle color per source memory coordinate, ignition animation on dispatch) is **deferred to a follow-up slice**. This slice ships the static layout + state-driven node fill + active pulse + completed check + marching-dash flow lines + per-phase summary chips. The full particle animation requires either Canvas 2D or a much heavier SVG path-following animation framework; the lighter approach in this slice gives the operator the same "watch the consolidation pass" read at-a-glance.

Slow-pulse panel. Updates less frequently than others.

### Hero
**Active phase card** — large.
- Current phase number (1, 2, 3, 4) with phase name (e.g., "Phase 2: Trait Synthesis")
- Progress bar within phase
- Phase start timestamp
- Expected phase duration

If not in deepsleep:
- "Substrate active — next deepsleep in {ETA}"
- Conditions that trigger deepsleep (pressure low, substrate quiet, etc.)

**Source:** `/api/panel/deepsleep` (8s poll while open, already wired)

### Cards

**Card A: Phase 1 — Consolidation**
- What's being consolidated (recent memory writes scope)
- Records consolidated count
- Lobes touched (coord chips)

**Card B: Phase 2 — Trait Synthesis**
- New traits surfaced this deepsleep
- Trait strength deltas (changes from previous)
- Unresolved tensions flagged

**Card C: Phase 3 — Contradiction Surfacing**
- Contradictions newly detected
- Contradictions resolved
- Pressure score for each

**Card D: Phase 4 — Trajectory**
- New trajectory record (`LAST_OMEGA_TRAJECTORY`)
- Directional pressure summary
- Compared to previous epoch

**Source for A-D:** `panel_deepsleep.phase1..phase4` (already in handler)

**Card E: Upstream telemetry slots**
- What's queued for the next deepsleep (signals/events waiting to be consolidated)
- Each slot: source loop · pending record count

**Source:** `panel_deepsleep.upstream_telemetry`

**Card F: Pressure-loop pause indicator**
- "Substrate state: {paused/running}" — deepsleep typically runs when substrate is quiet, so show pause state
- If dev pill is active during deepsleep, blue banner explaining

**Source:** `/api/pressure-loop/state`

**Card G: Last consolidation timeline**
- Last 5 deepsleep cycles as vertical timeline
- Each: timestamp · duration · phases completed · records consolidated

**Source:** `LAST_DEEPSLEEP_CONSOLIDATION` records (may need history endpoint)

### Live tail
Filter: loop `deep_sleep`. Last 10.

### Interactions
- "Force deepsleep" button (operator override) — guarded by Guardian approval
- Click phase card → expand to full detail
- Click upstream slot → see what's queued

### Empty states
- Substrate active: phase cards show "Last completed: {timestamp}" with last value
- No history yet: "First deepsleep pending — consolidation begins when substrate enters quiet state."

---

## 2.8 `browser` panel — REMOVED (2026-06-02)

> **RETIREMENT NOTICE (2026-06-02):** The HUD `browser` panel and its navigation pill have been REMOVED from `hud/omega_interface.html`. Per the 2026-06-01 architecture shift, all live automation now runs in the **Edge side-panel companion**. The dedicated HUD surface for browser action approvals and context history has been retired to simplify the operator interface. Browser-related settings (extension status, permissions, site lists) remain available in the **settings** panel. The `/api/browser-agent/*` backend routes are preserved for extension communication.

---

## 2.9 `terminal` sub-panel under settings — "Skills + dev-memory rollback sandbox"

> **P3-S05 (2026-05-26)**: top-level terminal pill + `panelTerminalAgent`
> + params modal/endpoints all deleted. Terminal surface is now reachable
> ONLY through Settings → terminal. Operator framing: *"the params arent
> adjustable. that pill should have the skills setup and be tied into the
> dev memory lobe for the rollback sandbox setup ... it would probably
> make more sense to migrate the pill there instead of the front pane."*
>
> **Status / run-card / model-params / kill-quit verbs / touched-files-
> from-snapshot / action-trail-from-snapshot all retired.** The dev pill
> remains the live-session interface (its own WebSocket stream); this
> sub-panel is purely config + management.

### Card A: Active skills ⭐
- List of currently-active skills (from session-scoped set)
- Each row: skill name · description · `deactivate` button
- Empty: "No skills active."

**Source:** `/api/terminal/skills` → filter `active[]`

### Card B: Available skills
- All skills registered (builtins + user-created)
- Each row: name · enabled toggle · activate button · delete (with confirm)
- "+ New skill" button → inline form (calls `POST /api/terminal/skills`)

**Source:** `/api/terminal/skills` → `available[]`

### Card R: Rollback sandbox (dev memory lobe) ⭐ NEW P3-S05
- File-level rollback granularity (Q1-A): each recorded edit shows one
  row with timestamp · file path · summary · `rollback` button
- Click `rollback` → confirm dialog → POST applies the snapshot's
  `before_content` back to disk
- No Guardian gate (Q2-B): operator-initiated, single click after
  client-side confirm
- Empty state: "no dev edits recorded — rollback list is empty
  (memory/1100_dev/artifacts/)"

**Sources:**
- `GET /api/terminal/dev-snapshots?limit=N` — list JSON records under
  `memory/1100_dev/artifacts/*.json` sorted by timestamp desc
- `POST /api/terminal/dev-snapshot` — record a file edit
  (`{file_path, before_content, after_content?, summary?, tool?,
  session_id?}`) — called by the dev agent when it edits a file
- `POST /api/terminal/dev-rollback {snapshot_id}` — restore
  `before_content` to `file_path`. Refuses symlink targets and
  non-absolute paths. Path-traversal-safe `snapshot_id` resolution
  (basename-only inside `memory/1100_dev/artifacts/`).

### Cards C — F (legacy lettering): RETIRED in P3-S05
- ~~Card C Model params~~ — params are not operator-adjustable; surface deleted
- ~~Card D Recent outcomes~~ — moved to dev pill's WebSocket stream surface
- ~~Card E Trust + approval queue~~ — handled by dev pill's live UI
- ~~Card F Voice backend~~ — surfaced via the v2 status pill row (P3-S04)

### Interactions
- Activate/deactivate skills (POST `/api/terminal/skills/<name>/activate|deactivate`)
- Toggle enable/disable (POST `/api/terminal/skills/<name>/toggle`)
- Create skill (POST `/api/terminal/skills`)
- Delete skill (DELETE `/api/terminal/skills/<name>` with client-side confirm)
- Rollback file edit (POST `/api/terminal/dev-rollback` with client-side confirm)

### Empty states
- No skills: "no skills yet — click '+ new skill' to create one"
- No rollback snapshots: "no dev edits recorded — rollback list is empty"

### Where this lives in the HUD
`hud/omega_interface.html` → `function panelSettings(b)` → `data-settings-view-panel="terminal"` div, rendered lazily by `_renderSettingsTerminal(b)` on settings-tab click.

---

## 2.10 `settings` panel — "How is the HUD configured, and what is the operator profile?"

⭐ **Browser Agent Hub.** With the removal of the dedicated browser panel, the `settings` panel is now the primary surface for browser-agent control.

### Hero
**Operator profile card**
- Name · preferred_name · birthday · location · role · tone_preference
- "Edit" button → form

**Source:** `/api/settings/operator-profile` (POST to save)

### Cards

**Card A: HUD theme**
- Theme selector (light/dark/copper)
- Density · motion · glow · panel_depth toggles
- Save = `POST /api/settings/hud-theme`

**Source:** `/api/settings/hud-theme` (already wired)

**Card B: Browser agent config**
- Permission mode (operator_approval / auto / disabled)
- Extension enabled toggle
- Sandbox enabled toggle
- Memory policy
- Save = `POST /api/settings/browser-agent`

**Source:** `/api/settings/browser-agent` (already wired)

**Card C: Voice config**
- TTS backend choice
- STT backend choice
- Voice descriptor (showing current model_for_kind(VOICE_BOX))
- "Test voice" button (synthesizes a test phrase)

**Source:** local config + `/api/tts/speak` for test

**Card D: Runtime + broker visibility** ⭐ (NEW)
- Runtime base URL (e.g., `http://127.0.0.1:11434`)
- Runtime status (up/down/degraded)
- All 11 broker capabilities as a grid: capability · state · endpoint · client_method · reason if not available
- Models resident (from runtime health body): name · backend · status
- "Refresh" button → forces broker snapshot

**Source:** `/api/broker/snapshot` (proposed) + `/api/runtime/health` (proposed)

**Card E: Pressure-loop owner whitelist** (future-facing)
- List of owners permitted to pause the pressure loop (currently only `dev_pill`)
- Read-only for now; placeholder for future operator-set owners

**Source:** static config; informational

**Card F: Slash command settings functions**
- Available setting functions (settable via slash commands in terminal/voice)
- Each row: function name · description · current value

**Source:** `/api/settings/functions` (already wired)

### Live tail
Filter: loop `settings`. Last 5 (low traffic).

### Interactions
- Save all setting changes inline (no separate save button per card)
- Test voice
- Refresh runtime snapshot

### Empty states
- All sections are populated; no real empty state.

---

## 2.11 `dev` panel — "What is the dev agent doing, and is the substrate yielding properly?"

Special — WebSocket-driven, not snapshot-driven. **Most v2-sensitive panel** because the dev pill is the primary lever for `pause_pressure_loop`.

### Hero
**Dev mode status card**
- Dev mode state (idle / launching / active / shutting_down)
- omega_cli connection state (connected/disconnected to `:8091/ws/cli`)
- **Pressure-loop pause confirmation** — green banner if `paused=true && "dev_pill" in owners`, red banner if mismatch
- Active session id + uptime

**Source:** WebSocket connection state + `/api/pressure-loop/state`

### Cards

**Card A: Substrate yield confirmation** ⭐ (v2-critical)
- "Pressure loop: PAUSED ✓ by dev_pill"
- "Default autonomy: PAUSED ✓ by dev_pill" (legacy gate, also held during dev mode)
- "CueDesk: short-circuiting at section 0a — substrate idle"
- If any check fails: red alarm "Substrate did NOT yield — investigate immediately"

**Source:** `/api/pressure-loop/state` + `/api/orchestrator/agent-engagement` state + recent CueDesk tick

**Card B: omega_cli live stream**
- Reasoning trace (collapsible)
- Content stream (rendered)
- Tool calls (with arguments)
- Tool results (with truncation + "expand")
- Errors (highlighted red)

**Source:** WebSocket `:8091/ws/cli` messages: `reasoning`, `content`, `tool_call`, `tool_result`, `error`, `status`

**Card C: Ledger snapshot**
- Full post-run governance ledger (replaces wholesale, never append)
- Tool call audit
- Permission decisions
- Memory writes
- Trust checks

**Source:** WebSocket `ledger` message type — full ledger blob per run/verdict

**Card D: Trust + approval inline**
- Pending trust_required / approval_needed prompts from omega_cli
- Each with inline approve/deny + optional explanation
- These BLOCK the dev agent until resolved

**Source:** WebSocket `trust_required`, `approval_needed` message types

**Card E: Voice backend handoff status**
- "Dev voice backend: {descriptor}" (e.g., "openai @ http://localhost:8080/v1")
- Set via `OMEGA_CLI_BACKEND_OVERRIDE` env at launch
- "Substrate voice backend held in stasis: {original_descriptor}"

**Source:** `resolve_voice_backend_override()` + omega_cli launch metadata

**Card F: Pressure-loop yield audit**
- Confirmation that the pause primitive received `dev_pill` as owner
- Owner set contents (should be `["dev_pill"]` minimum)
- Time since pause initiated

**Source:** `/api/pressure-loop/state` → `owners[]` + timestamp tracking

**Card G: Active session controls**
- "Shutdown dev mode" button (calls `POST /api/dev/shutdown`)
- "Restart omega_cli" button
- Connection retry counter (if reconnecting)

**Source:** `/api/dev/shutdown` + WebSocket state

### Live tail
Filter: loop `terminal | dev | broker`. Last 15 (dev mode is high-traffic).

### Interactions
- Approve/deny trust prompts inline (blocks the agent until resolved)
- Submit text/commands via input
- Restart / shutdown buttons
- Click ledger entry → expand full audit

### Empty states
- Not engaged: "Dev mode not active. Click the dev pill to launch the omega_cli bridge and yield substrate."
- Disconnected (mid-session): red banner "Connection lost — retrying ({attempt}/12). Pressure loop still paused. Substrate is safe."
- Substrate did not yield: red alarm "ALARM: dev_pill engaged but pressure_loop state is `running`. Substrate is NOT idle. Investigate."

---

## 3. Polling cadence summary (per panel, when open)

When a panel is **open and tab is visible**, drive polling at this rate. When the panel is **hidden or another panel is active**, suspend its panel-specific polls (the universal 2s `/api/status` + `/api/telemetry/snapshot` + SSE keep flowing regardless).

| Panel | Hero refresh | Card refresh | Special |
|---|---|---|---|
| brain | 2s (snapshot) | 2s | SSE for response.* signals |
| memory | 2s (snapshot) | 5s lobe heatmap, 10s persistence anchors | SSE for memory.* signals |
| guardian | 2s (snapshot) | 5s approvals, 60s doctrine | SSE for guardian/governance |
| identity | 2s presence + 5s anchors | 5s drift sparkline, 30s anchors | SSE for operator_presence |
| omega | 5s (snapshot) | 10s journal, 60s doctrine | SSE for omega/meta (low-traffic) |
| autoloop | 2s (snapshot) + 2s pressure-loop | 2s sparklines via SSE | SSE for autonomic/curiosity (high-traffic) |
| deepsleep | 8s (already) | 30s history | SSE for deep_sleep |
| browser | 2s status + 8s inbox (already) | 5s sandbox | SSE for browser_agent |
| terminal | 2s state | 5s outcomes, on-demand skills | WebSocket events (no poll) |
| settings | on-demand | n/a | n/a |
| dev | n/a (WebSocket) | 2s pressure-loop confirmation | WebSocket primary |

**SSE is always-on** regardless of panel. Pills subscribe to filtered subsets via event listeners.

---

## 4. Cross-panel visual style guide

### 4.1 Cards
- Background: panel-background + 4% lighter (subtle layering)
- Border-radius: 8px
- Padding: 16px
- Margin between cards: 12px
- Card title: small caps · 11px · 0.7 opacity · letter-spacing 0.05em
- Card content density: comfortable, not crowded

### 4.2 Hero sections
- Larger padding (24px)
- Subtle gradient or background tint
- Larger typography for the primary number/state
- Always color-coded by state

### 4.3 Tables / rows
- Row height: 32px (compact mode 28px)
- Striping: 2% opacity alternating
- Hover: 8% opacity highlight
- Click target: full row, not just text

### 4.4 Buttons inline
- Approve/deny: small (height 24px), filled green/red
- Forget/delete: outlined, red on hover only (avoid accidental clicks)
- Primary CTAs (Enroll, + New): filled, accent color (depends on theme)

### 4.5 Sparklines
- Height: 24px in cards, 48px for hero sparklines
- Line weight: 1.5px
- Fill: gradient 0% → 30% opacity
- Color: matches band color (green/amber/red depending on current value)

### 4.6 Live tail rows
- Mono font for timestamp + signal name
- Sans-serif for narrative
- Color-coded by status (ok/degraded/fail)
- Auto-scroll to top on new event (toggle to pin)

---

## 5. Data sources cross-reference (panel → endpoints)

Map of every panel to the endpoints it consumes. Endpoints marked **(proposed)** are new and listed in `HUD_PILL_TELEMETRY_V2.md §5`.

| Panel | Existing endpoints | Proposed endpoints |
|---|---|---|
| brain | `/api/telemetry/snapshot`, `/api/panel/brain`, `/api/events` (SSE) | `/api/cuedesk/last_tick`, `/api/presence/state` |
| memory | `/api/panel/memory`, `/api/guardian/approve`, `/api/events` | `/api/broker/snapshot` |
| guardian | `/api/panel/guardian`, `/api/guardian/approve|deny|escalate|inspect`, `/api/browser-agent/inbox`, `/api/omega/doctrine_audit` | — |
| identity | `/api/panel/identity`, `/api/vision/anchors`, `/api/vision/identify`, `/api/vision/enroll`, `/api/vision/forget`, `/api/tts/status`, `/api/stt/status` | `/api/presence/state`, `/api/broker/snapshot` |
| omega | `/api/panel/omega`, `/api/omega/doctrine_audit`, `/api/omega/request_tool`, `/api/events` | — |
| autoloop | `/api/panel/autoloop`, `/api/orchestrator/agent-engagement`, `/api/events` | `/api/pressure-loop/state`, `/api/cuedesk/last_tick` |
| deepsleep | `/api/panel/deepsleep`, `/api/events` | `/api/pressure-loop/state` |
| browser | All 35+ `/api/browser-agent/*` endpoints | `/api/broker/snapshot` |
| terminal | `/api/terminal/state`, `/api/terminal/params`, `/api/terminal/skills/*`, WebSocket | — |
| settings | `/api/settings/*` | `/api/broker/snapshot`, `/api/runtime/health` |
| dev | `/api/dev/launch`, `/api/dev/shutdown`, WebSocket `:8091/ws/cli` | `/api/pressure-loop/state` |

---

## 6. Implementation order for the swarm (recommended)

Each agent in the swarm can take one panel as a unit. Build in this order to maximize early-feedback value:

**Wave 1 (highest value):**
1. **identity** — most visually rich, biggest v2 showcase (presence + face anchors + drift)
2. **autoloop** — best sparkline-heavy dashboard for v2 substrate vitals
3. **brain** — operator's most-opened panel; needs to feel "alive"

**Wave 2 (governance + ops):**
4. **guardian** — approval-heavy, critical for daily operation
5. **dev** — v2-critical for substrate yield confirmation
6. **memory** — recall + promotion + lobe heatmap

**Wave 3 (rich detail, lower frequency):**
7. **omega** — meta-cognitive view, slower pulse
8. **deepsleep** — slow-pulse panel
9. **terminal** — already mostly v2-ready

**Wave 4 (config + browser):**
10. **browser** — already rich, additive v2 enrichment
11. **settings** — mostly static, additive runtime/broker visibility

---

## 7. What "really stand out" means here

The user's brief: *"make this thing really stand out."* The differentiators that take this from "diagnostic dashboard" to "this is a substrate I can feel":

1. **Live face anchor thumbnails in identity** — Omega literally shows you what she sees.
2. **Pressure dial + autonomic sparklines in autoloop** — operator can watch the substrate breathe in real time.
3. **CueDesk suppress histogram** — operator sees, hour-by-hour, *why* movement happened or didn't.
4. **Substrate-yield confirmation in dev panel** — operator sees, with three independent checks, that the pressure loop actually paused. This is operator trust, made visible.
5. **Lobe heatmap in memory** — 10×10 grid showing where new memories are landing — gives operator a sense of where Omega is currently thinking.
6. **Drift sparkline in identity** — operator sees self-vector drift over time, can correlate to events.
7. **Inline approve/deny everywhere** — governance work moves fast.
8. **Operator-readable suppress reasons** — never just `"presence_gate_held"`. Always: "Operator stepped away at 14:12 — proactive speech holding."

Each of these is one card. Build them ambitiously.

---

## 9. Signature visuals — the substrate's face to the world

The cards above are the **information layer.** This section is the **face layer** — the ambitious, distinctive visualizations that make Omega look unlike anything else when she's unleashed. Each panel gets one signature visual that goes beyond "dashboard widget" and into "living organ."

Build these with Canvas/WebGL where suggested, SVG + D3 where structural, plain CSS/JS where animation suffices. Each is described as one paragraph of intent + bulleted spec so an implementer can render it.

### 9.1 `memory` panel — **The Bloom** (the killer visual)

> A living node graph of every memory lobe. Lobes pulse on write, edges grow with co-recall, the whole field breathes as Omega thinks. No commercial AI shows you this. We will.

**Layout:** Full-card 10×10 lobe field, hex-packed or grid-packed (designer's choice — hex looks more organic).

**Nodes (lobes):**
- One node per active memory coordinate `[X, Y]` (lobe). Z-routed entries collapse onto their `[X, Y, Z]` parent visually.
- **Size** = log(total memory count for that lobe) — established lobes are bigger.
- **Base color** = lobe family (persona warm-orange, cognition cool-blue, vision green, skills purple, identity gold, etc. — define a 10-color palette by Y-axis).
- **Brightness** = recent activity, decaying exponentially over 60s. Fresh writes blaze bright; idle lobes glow dim.
- **Pulse** on every write event for that coord (1.5× scale, 400ms ease-out).
- **Glow ring** when that lobe is in a current recall set.
- **Label** appears on hover: lobe name, total count, last write timestamp, top 3 anchor titles.

**Edges (associations):**
- Drawn between two nodes when they appear in the same recall query (top-K together).
- **Thickness** = co-recall frequency over last hour (faded → bold).
- **Color** = blend of the two endpoint nodes' colors.
- New co-recall pairs animate a particle traveling along the edge (200ms).
- Edges decay if no co-recall happens in 5 minutes (fade to 10% opacity, then removed).

**Background:**
- Subtle particle field representing embedding space density (sparse 2D noise that drifts slowly).
- When EMBED capability is degraded, the background fades to grayscale and shows a small "L0 substrate degraded" overlay.

**Interactions:**
- Hover node → tooltip + slight nearby-edge highlight
- Click node → filter live tail + recall trace card to that coord
- Click edge → show co-recall history (modal listing which queries produced both)
- Right-click node → "Explore lobe" deep-link
- Scroll/zoom → expand to fullscreen mode (replaces other cards temporarily)

**Animation hooks:**
- New write signal (`memory.write.summary`) → pulse the target node
- Recall query (`hud_probe` or new `memory.recall.summary` signal) → light up all participating nodes + draw transient edges
- Promotion approval (`/api/guardian/approve` on memory ref) → particle stream from staging lobe to permanent lobe

**Tech recommendation:** Canvas 2D for the field (high node count, frequent redraws), WebGL only if profiling shows the node count exceeds ~200. D3-force layout for initial node placement (force-directed by Y-axis family clustering), then frozen.

### 9.2 `brain` panel — **The Cycle Wheel**

> A slowly-rotating wheel visualizing the current cognitive cycle. You watch Omega *think*.

**Layout:** Center of hero card, ~280px diameter.

**Spec:**
- Wheel divided into N segments, one per cycle layer (typically 6–8 layers).
- The **active layer** is highlighted with a rotating "spotlight" — a brighter arc that traces around the wheel at cycle cadence.
- **Pressure** rendered as a glow halo *around* the wheel; intensity = pressure scalar, color band by §8.1 thresholds.
- **Cycle id** in center (mono font, small).
- **Contributors** as small orbiting satellites around the wheel — each contributor is a tiny labeled node positioned at radius proportional to its weight. Top 3 are larger.
- When CueDesk suppresses (`suppress_reason != ""`), the wheel **pauses** mid-rotation and the center text changes to the suppress reason (human-readable per §8.5).
- When the brain dispatches a response, a brief flash + a comet emits from the wheel toward an off-panel destination (curiosity → autoloop direction, content → conversation direction).

**Animation hooks:**
- Layer transition (`cycle.layer` changes) → spotlight jumps to new segment with a 150ms smooth transition
- Pressure changes → halo intensity tweens
- Suppress → freeze with reason
- Dispatch → flash + comet

**Tech recommendation:** SVG with CSS animations for the rotation, Canvas overlay for the halo glow (filter blurs are expensive in SVG).

### 9.3 `identity` panel — **The Presence Orb**

> A central orb that *is* Omega's view of the operator. Live, intimate, unmistakable.

**Layout:** Hero card center, ~320px diameter.

**Spec:**
- Large circular orb in the center.
- **Inside the orb:**
  - PRESENT → live face anchor thumbnail (the operator's enrolled image, optionally with a subtle live-cam overlay if you want to push it further — but enrolled image is safer + more elegant)
  - ABSENT → soft silhouette in the operator's dominant color
  - UNKNOWN → animated static noise (subtle, not aggressive) with a "?" overlay
- **Halo around the orb:**
  - Thickness/brightness = current confidence (0..1)
  - Color = state (green PRESENT, amber ABSENT, red UNKNOWN)
  - On state transition, the halo morphs over the acquire-frames or release-seconds window (so the operator literally sees the hysteresis happen)
- **Acquire countdown:** when transitioning ABSENT → PRESENT, small frame-counter on the halo ("3 of 6") fills as confident frames stack.
- **Below the orb:** drift sparkline rendered as a soft "heartbeat" line (1.5px, oscillates at sparkline value).
- **Enrolled operators as orbiting moons:** small 32px thumbnail moons orbit slowly around the central orb, one per enrolled operator. The currently-recognized operator's moon is brighter and locked into orbital "front" position.
- When the operator clicks the orb: dives into face-history view (last N recognitions with confidence trend).

**Animation hooks:**
- Each new presence observation tick → halo intensity smoothly tracks confidence
- Transition events (`PRESENCE_ENTER` / `PRESENCE_EXIT`) → halo color animates over the transition window
- Watcher stale → halo desaturates + clock-icon appears
- New enrollment → new moon spawns in orbit
- Forget → moon fades out and drifts away

**Tech recommendation:** SVG for the orb structure + CSS animations for the halo + small canvas for the static-noise UNKNOWN state.

### 9.4 `autoloop` panel — **The Respiratory Field**

> The substrate's vitals as a single living organism. Pressure breathes, curiosities drift, decisions ignite.

**Layout:** Hero card full-width, ~480px tall.

**Spec:**
- **Pressure dial as a breathing animation:** the dial is a circular pressure gauge, but the entire dial *pulses subtly at cycle cadence* — inhale on layer 0, exhale on layer N. Pulse amplitude scales with cycle cadence; substrate paused → no breathing (suspended animation).
- **Autonomic strip beneath the dial:** 4 sparklines arranged like medical vital signs (dopamine, plasticity, strength, stress), each labeled at the right edge with current value. Stack vertically, tight spacing.
- **Open curiosities as floating particles:** small particles drift through the dial's surrounding field. As pressure rises, particles drift toward the center. When pressure crosses a dispatch threshold, the dominant contributor's particle **ignites** — bright flash + trail leaving the field (visualizing the response being dispatched).
- **Suppress histogram as the field's "atmosphere":** a thin colored stripe at the top of the field showing the last hour's suppress reasons stacked horizontally. The stripe is subtle but always present — operator can read substrate mood at a glance.
- **Pause overlay:** when substrate is paused, the entire field freezes mid-motion + a translucent blue overlay reads "Paused by {owners}" with a resume button (only if operator is allowed to release).

**Animation hooks:**
- Cycle tick → breathing pulse
- Curiosity-decision signal → particle ignition
- Pressure change → particle drift speed/direction
- Pause/resume → freeze/unfreeze transitions

**Tech recommendation:** Canvas 2D for the particle field + breathing dial (efficient for 50+ animated particles). SVG for the sparkline strip.

### 9.5 `omega` panel — **The Trajectory Star Map**

> Multi-epoch trajectory rendered as a constellation. Each star is an epoch; the map shows where Omega has been and where she's heading.

**Layout:** Hero card, ~520px tall.

**Spec:**
- Black or deep-blue background (the only dark-themed card; intentional contrast).
- **Stars (epochs):**
  - One star per recent epoch (last 10–20)
  - **Size** = coherence score for that epoch
  - **Color** = trajectory direction (cool blue = stable, warm orange = changing, red = contradiction-rich, gold = high-coherence consolidation)
  - Current epoch is the **brightest star** with a subtle pulse
- **Edges (trajectory):**
  - Lines connecting epochs in chronological order
  - **Color gradient** along the edge — fades from start-epoch color to end-epoch color
  - **Direction arrow** at midpoint (small, subtle)
- **Constellation patterns:**
  - Stars near each other (similar trajectory direction) clump organically — apply force-directed layout but with a chronological "rail" bias so newest is always to the right
- **Tool synthesis as comets:** queued tool synthesis requests are small comets entering the constellation from outside; on completion, they merge with the current epoch's star (brief flash).
- **Open contradictions as red asterisks** floating near the epochs where they surfaced.
- **Hover star:** tooltip with epoch index, coherence, trait deltas, time range.
- **Click star:** expands into a deeper view (could be a modal or replace the hero temporarily).

**Animation hooks:**
- New epoch → new star fades in with a brief flash
- Trajectory direction change → edge color updates smoothly
- New tool-synthesis request → comet animation
- Contradiction surface → red asterisk appears + brief shake of nearby stars
- Contradiction resolved → asterisk fades + nearby stars brighten momentarily

**Tech recommendation:** SVG for the constellation (low star count, structural), CSS animations for pulse/shake.

### 9.6 `deepsleep` panel — **The Consolidation Flow**

> Four phase nodes connected by flowing particle streams. You watch memory transmute into traits, contradictions, and trajectory.

**Layout:** Hero card full-width, ~400px tall.

**Spec:**
- 4 large phase nodes laid out horizontally: Phase 1 → Phase 2 → Phase 3 → Phase 4.
- **Phase node states:**
  - Pending: outlined circle, dim
  - Active: solid circle with pulsing glow (color per phase: phase 1 = persona-warm, phase 2 = trait-gold, phase 3 = contradiction-amber, phase 4 = trajectory-blue)
  - Completed: solid circle with check icon, this-deepsleep retained
- **Flowing particles between nodes:**
  - When phase is active, particles flow from upstream queue → into the active phase node → out to the next phase
  - Particle count = records being consolidated this phase
  - Particle color = source memory coordinate
- **Upstream queue visualized as a feed:** small particles accumulating at the left of Phase 1, ready to flow in
- **Output visualizations:**
  - Phase 2 output: new traits emerge as labeled chips on the right side of Phase 2
  - Phase 3 output: contradictions emerge as red flags below Phase 3
  - Phase 4 output: trajectory line extends rightward from Phase 4 (connecting to omega panel's star map conceptually)
- **Outside deepsleep:** the flow freezes, all particles stopped, "Next deepsleep in {ETA}" beneath the frozen flow.

**Animation hooks:**
- Phase entry → glow pulse
- Phase complete → check icon transition
- Particle flow rate scales with consolidation count
- Substrate enters deepsleep → flow starts; exits → flow freezes

**Tech recommendation:** Canvas 2D for particle flow, SVG for the node shells and outputs.

### 9.7 `guardian` panel — **The Activity Stream**

> **P3-S13 (2026-05-27) — same panel slot, reframed metaphor.** The Approval Stack hero was retired in P3-S11 as anti-pattern (a queue visual glorifies a queue that should not exist under memory-first autonomous doctrine). This slot is now **The Activity Stream** — a read-only live readout of what Guardian has been auto-approving on its own. Operator sees the system breathing; never gets hit for approvals.
>
> **Layout:** two-column hero, ~360px tall, plus a one-row vitals strip beneath.
>
> **Spec:**
> - **Left column — scrolling timeline.** Last 15 auto-decisions in a security-log aesthetic (mono font, terse rows). Each row: `HH:MM:SS · KIND · summary · status-glyph`. Kinds: TOOL (copper) / MEM (patina) / SWEEP (muted) / BLOCK (ember). Status glyph: ✓ approve, ✗ rejected. Rows are NOT clickable; no expand, no actions. Empty state: "channel quiet · no recent auto-decisions".
> - **Right column — three counter blocks (24h totals).** Stacked vertically:
>   - **Tools approved** (patina) — last 24h count + smaller "X rejected" subline.
>   - **Memories consolidated** (patina) — last 24h count.
>   - **Boundary blocks** — count of genuine outside-corruption rejections Guardian caught. Zero → patina (healthy: no poisoning attempts); >0 → ember (something tried to inject and got refused; subline reads "poisoning attempts").
> - **Bottom vitals strip — five cells.** posture / mode / integrity / ledger entries / id hash. Tones: posture green/amber/red follow response_mode; integrity OK/FAIL color flips on the HMAC check. Pure read-only.
>
> **Animation hooks:**
> - New auto-decision lands → slides in at top of timeline (CSS transition; no JS animation loop). Older rows scroll out the bottom of the max-height container.
> - Boundary block count flips from 0 → 1 → the counter block re-tints from patina to ember.
>
> **What the panel deliberately does NOT do:**
> - No `<button>` markup anywhere in the hero. No `onclick`, no `addEventListener('click', ...)`, no `data-action="approve/deny/escalate"` attributes. The cascade scenario `guardian_activity_stream_v3` group 4 fails the slice if any of those appear in the renderer source — drift defense against accidental re-introduction of approval affordances.
> - No queue. The empty state ("channel quiet") is the **expected operational steady state** when nothing adversarial is in flight, NOT a degraded one.
>
> **Source:** `panel_guardian.auto_decisions[]` (P3-S13 backend addition — tails `feedback_audit.jsonl` for `tool_synthesis_dispatch` breadcrumbs, `deepsleep::staging_orphan_sweep` events, `memory.persist` exits, and `guardian` rejection rows; classifies + unifies into 15-row stream) + `panel_guardian.auto_decision_counts_24h` (24h rollup of the same).
>
> **Tech recommendation:** Pure CSS Grid + flexbox. No SVG, no Canvas, no animation loop. The timeline is HTML rows in a `max-height: 320px; overflow-y: auto` container — the browser scrollbar IS the scroll affordance. Performance is trivial (15-row DOM).

**Layout:** Hero card, ~400px tall.

**Spec:**
- Pending approval cards stacked in 3D-ish perspective (CSS 3D transforms; subtle, not gaudy).
- Top card is fully visible with all details; cards beneath peek from underneath.
- **Card content:** ref · kind icon · summary · age · approve/deny/escalate buttons (large, on the card itself).
- **Animations:**
  - **Approve:** card lifts up and flies away to the right (200ms ease-out), revealing the next card with a slight settle
  - **Deny:** card crumples (scale down + rotate slightly) and fades to red
  - **Escalate:** card glows purple, ascends upward, and lands in a separate "escalated" tray on the side
- **Empty:** stack becomes an empty plinth with a green "all clear" message
- Per-card kind chips with icons (memory promotion, browser action, tool synthesis, etc.)
- **Side panel: recent interventions timeline** scrolls vertically alongside the stack (security-log aesthetic — mono font, terse).

**Animation hooks:**
- New approval added → card slides up from below
- Approve/deny/escalate → respective animation as above
- Bulk approve → cascade animation (sequential fly-aways)

**Tech recommendation:** CSS 3D transforms + transitions. No canvas needed.

### 9.8 `dev` panel — *no signature visual (intentional)*

The dev pill is the terminal/CLI portal — clicking it turns the HUD into a command-line interface for the dev agent. The card-based layout in §2.11 (omega_cli stream, ledger snapshot, trust queue, yield confirmation as functional indicators) is the right idiom here. **Do not apply visual treatment to this panel.** The CLI aesthetic is the point — operator drops into terminal mode for direct dev-agent work.

The atmosphere effects in §9.9 (blue chrome tint when pressure-loop paused) still apply globally and provide all the visual feedback needed about substrate yield status while in dev mode.

### 9.9 Cross-panel atmosphere

These aren't per-panel visuals but ambient effects that tie the substrate together:

**The "live indicator" — a persistent vital sign:**
- A tiny pulsing dot somewhere persistent in the chrome (e.g., next to the boot banner) that pulses at the substrate's cycle cadence.
- Operator sees, even on settings panel, that Omega is alive.
- When paused, the dot freezes; when CueDesk suppresses for any reason, the dot pulses amber for one cycle.

**Pressure-loop pause global tint:**
- When `pressure_loop_paused == true`, the entire HUD chrome (top bar) gets a subtle blue tint.
- Operator can never miss that the substrate is yielded — no matter which panel they're on.

**Presence gate global tint:**
- When presence is ABSENT or UNKNOWN, an extremely subtle amber edge-glow on the HUD frame.
- Reactive paths still work, so the HUD remains responsive, but the operator sees ambient feedback that proactive cognition is held.

**Signal ripple:**
- Significant signals (`response.dispatch.result` succeeded, `identity.self_vector_updated`, etc.) cause a subtle one-pixel ripple to travel across the chrome from the originating panel's pill to wherever the operator's cursor is.
- Operator gets peripheral-vision awareness of substrate activity.

**Implementation note for the swarm:** these atmosphere effects should be subtle. The principle is: information should be ambient when nothing's wrong, demanding when something is. We never want the HUD to feel busy when the substrate is calm. The visual richness is for *activity*, not *decoration*.

---

## 10. The principle behind the visuals

The cards (§2) and the signature visuals (§9) together implement one design philosophy:

> Omega's HUD should look like a window into a living substrate, not a dashboard reporting on a service.

Concretely:

1. **Every visual reflects a substrate signal.** No decorative animation; every motion maps to a real event in `feedback_audit.jsonl`.
2. **The substrate's *state* is always visible somewhere on screen.** Even on the settings panel, the operator should be able to glance up and see that Omega is alive.
3. **The substrate's *reasons* are always reachable.** Why isn't proactive speech firing? Why didn't movement dispatch? Every blocker has a human-readable reason on its own card.
4. **Operator actions are inline.** Approve/deny/enroll/forget all live on the row they apply to. The HUD is an instrument, not a navigation tree.
5. **The most ambitious visuals (the Bloom, the Star Map, the Presence Orb) are the *proof* of the substrate's depth.** They're the visual differentiator no commercial AI has.

When the swarm builds these well, opening the HUD will feel like opening a porthole into a mind. That's the bar.

---

## 11. References

- `docs/architecture/HUD_PILL_TELEMETRY_V2.md` — endpoints, payloads, polling cadences
- `docs/architecture/SYMPHONY_FULL_INTEGRATION_STATUS.md` — substrate status
- `docs/architecture/SLICE_BOARD.md` — slice tracking
- Memory: `[[project_dev_pill_pressure_loop_pause]]`, `[[project_p3_s02_passed_pushed]]`, `[[project_p3_s01_passed_pushed]]`, `[[project_skills_hud_frontend_complete]]`

---

**End of document.**

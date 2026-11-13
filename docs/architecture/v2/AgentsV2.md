# Omega 3.0 — Agent Instructions (V2 Live System)

**Default for all agents.** Enforced via [`AGENTS.md`](AGENTS.md) (redirect), [`CLAUDE.md`](CLAUDE.md), and Cursor rule `.cursor/rules/agents-v2-default.mdc` (`alwaysApply: true`).

**Branch:** `main`  
**Authority for agents:** **this file + live code**. Verify every claim against the repo before changing behavior.

**Spec anchor (architecture intent only):** [`docs/architecture/OMEGA 3.x ARCHITECTURE CORRECTION.txt`](docs/architecture/OMEGA%203.x%20ARCHITECTURE%20CORRECTION.txt)

---

## Read this first

Omega 3.x is **one cognition loop** running today:

```text
boot.py
  → PressureRuntime (episode store + shared_state)
  → pressure_loop.loop (15s ticks)
       → gather_contributors → evaluate → EpisodeIgnition?
       → cognition_chain.ignite_episode (on ignition)
            → open episode
            → Global A → Meta A → AUTO → Meta B → Global B   (always, no route fork)
            → per-episode DeepSleep journal write
            → close episode → reflection window
            → at 10 episodes: Omega window → DeepSleep window → archive + reseed
```

There is **no** central orchestrator, **no** CueDesk tick, **no** ignition route selector, **no** outlet placeholder layer. Pressure ignites; the executive cascade runs; the episode closes.

---

## Symphony phase work is DONE — do not use it

The pre-rebuild **Symphony v0.2 phase / SWING / slice-board / CueDesk** era is **finished and retired from the live path**. Treat it as **historical documentation only**.

### Do NOT implement, wire, or resurrect

| Retired concept | Why |
|-----------------|-----|
| `CueDesk`, `symphony_runner`, `run_symphony_through_deepsleep` | Replaced by `boot.py` + `pressure_loop.loop` + `cognition_chain.py` |
| `build_global_action_pack`, `symphony_global_action`, global `act` / `action_signal` / `outcome_kind` packs | Replaced by slice loops: `ActionGoal` + `ExecutionActionPack` on the episode |
| `decide_ignition`, `IgnitionRoute`, `IgnitionRecord`, `episode.ignition` | Deleted; no first-movement route fork in v2 |
| `episode.response_hold`, `episode.omega_need`, outlet placeholder writers | Deleted; no non-executive short-circuit close path |
| `run_response_hold_movement`, `response_engine` outlet dispatch | Not on live path; do not reintroduce |
| `OMEGA_PRELUDE` / `RESPONSE_HOLD` as **episode routing** | Not v2 behavior; every ignition runs the executive cascade |
| Phase 3 Symphony integration docs as **build authority** | See firewall list below |
| SWING §S1–S18 packets, `SLICE_BOARD.md` movement IDs, jobsite handoffs as **implementation specs** | Rebuild complete; use live maps only for archaeology |

### Do NOT import from live paths

Never add imports that pull legacy conductors back into boot or the pressure tick:

- `core/cue_desk.py`, `core/symphony_runner.py` (if present on disk — dormant)
- Any module whose purpose is “run symphony through deepsleep” or “tick slice N front” outside `pressure_loop` + `cognition_chain`

`boot.py` explicitly does **not** boot BrainLoop or the legacy orchestrator. Keep it that way.

### Historical docs — reference only, not build specs

Do **not** treat these as tasks to implement or gaps to close:

- `docs/architecture/SLICE_BOARD.md`
- `docs/architecture/SYMPHONY_FULL_INTEGRATION_STATUS.md`
- `docs/architecture/JOBSITE.md` and `docs/_archive/architecture/handoffs/*`
- `docs/_archive/architecture/slices/PACKET_P2_S4.md` (outlet packs)
- `docs/architecture/DEFERRED_INTEGRATION.md` (legacy backlog — not a todo list for v2)
- `docs/architecture/FULL_ALIGNMENT_STATUS.md` (audit snapshot; verify against code)
- `docs/architecture/symphony_episode_schema.json` fields `ignition`, `response_hold`, `omega_need` — **schema drift**; live episode shape is `pressure_loop/episode.py`

When a historical doc conflicts with `boot.py`, `cognition_chain.py`, or the `*_loop/` packages, **the code wins**.

---

## What IS live (integration surfaces)

### Boot and runtime

| Component | Role |
|-----------|------|
| `boot.py` | Substrate boot, HUD spawn, `ensure_cognition_runtime()`, starts pressure loop |
| `pressure_loop/runtime.py` | `PressureRuntime` — episode store + `SharedState` |
| `pressure_loop/loop.py` | Sole tick driver; hands off to `cognition_chain.ignite_episode` |
| `runtime/client.py` + `runtime/model_control.py` | Local inference at `:11434` (`gemma_e4b`) |
| `runtime/runtime_capability_broker.py` | **Capability preflight only** — `proceed` / `response_hold` / `alternate_capability` when runtime is down. This is **not** episode routing. |

### Cognition cascade (every ignited episode)

| Step | Module | Episode slot |
|------|--------|--------------|
| Global A | `global_loop/loop.py` | `episode.global_["action_goal"]` |
| Meta A | `meta_loop/loop.py` | `episode.meta["execution_action_pack"]` |
| AUTO | `auto_loop/auto.py` | `episode.auto` |
| Meta B + Global B | `meta_loop/loop.py`, `global_loop/loop.py` | meta reflection + global outcome |
| Per-episode DeepSleep | `deepsleep_loop/loop.py` via `cognition_chain._run_per_episode_journal_write` | journal + consolidation |
| Close | `pressure_loop/episode.py` `EpisodeStore.close()` | enriched body → reflection window |

**Model-running layers:** AUTO and Omega (window only). Meta B, Global B, per-episode DeepSleep phases are deterministic unless they call bounded subsystems.

### Windowed reflection (Model C)

- `pressure_loop/reflection_window.py` — `WINDOW_SIZE = 10`
- `EpisodeStore.close()` appends enriched body via `pressure_loop/episode_journal.enriched_episode_from_episode`
- `cognition_chain._maybe_run_window_reflection` — Omega → DeepSleep window → `archive_and_reset_window`
- Omega writes reflected trajectory/episodes to **lobe 9** (`omega_loop/package/run.py`)
- Pressure feedback closure: Omega `pressure_feedback` → DeepSleep reseed → `pressure_loop/pressure.py` `stage_reseed` / `stage_reseed_suppress`

### Operator surface

- HUD in-process: `hud/spawner.py`, `:13310`, daemon from `boot.py`
- Shutdown: `scripts/shutdown.py`
- Dev pill pauses pressure loop (`pressure_loop/pressure_loop_pause.py`) — required for shared runtime with TS CLI bridge

### Policy gates

Live fronts are toggled in `core/policy/symphony.yaml` under `pressure.slice*_front` (all `true` in production). **These flags name the v2 slice loops, not Symphony phase packets.**

---

## Canonical types (v2)

```text
Contributor(source, type, weight, metadata)
EpisodeIgnition(episode_id, aggregate_pressure, opened_by, dominant_contributor, contributors, trajectory_snapshot)
ActionGoal(episode_id, intent, target, confidence, reason)
ExecutionActionPack  → episode.meta["execution_action_pack"]
```

**ALLOWED_INTENTS (exactly 7):** `RESPOND`, `PROACTIVE_RESPONSE`, `RESOLVE_QUERY`, `LEARN`, `USE_TOOL`, `IMAGE_CREATE`, `IDLE` — `SELF_INQUIRY` was removed (collapsed to `IDLE`; the idle-wake awakening owns reflection now)

Intent is chosen in **Global A** from pressure + episode context. Meta A plans execution. There is no separate “global action pack” layer.

---

## Episode as carrier

`pressure_loop/episode.py` — `CognitiveEpisode` is the single in-flight truth object.

Live slots: `pressure`, `temporal`, `learning_state`, `global_`, `meta`, `auto`, `omega`, `brain_turns`, `sensory`, `cascade_status`.

**Not live:** `ignition`, `response_hold`, `omega_need` (removed).

Persistence:

- Per-episode journal JSONL — `pressure_loop/episode_journal.archive_episode`
- Reflection window — `guardian/state/reflection_window.json`
- Durable writes — `pressure_loop/durable_write.py` (fsync before next ignition)

---

## Fail-hard vs failure-soft

| Path | Rule |
|------|------|
| Cognition cascade (Global A → … → close) | **Fail-hard** — exceptions halt the tick |
| Substrate boot (HUD, memory warmup, guardian reload) | **Failure-soft** — warn and continue |
| `runtime_capability_broker` hold | Skips inference for that capability; does not abort the episode |

Operator directive: cognition crashes the system; substrate degrades gracefully.

---

## Hard rules for agents

1. **Pathspec commits only** — never `git add -A` / `git add .`
2. **Never** `git stash` / `checkout` / `reset` / `restore` unless the operator explicitly orders it
3. **Operator-gated push** — commit locally; push when authorized
4. **Minimal diffs** — match patterns in neighboring code; do not reintroduce retired abstractions
5. **Do not modify** `docs/_archive/build_log/` entries
6. **Do not run git mutations** unless explicitly asked
7. **Kill legacy on contact** — if you touch dormant Symphony conductor code, delete or firewall it; do not wire it back
8. After code changes: `graphify update .` when `graphify-out/graph.json` exists

---

## Testing

```powershell
$env:PYTEST_ADDOPTS=''
.venv-brain\Scripts\python.exe -m pytest tests/core/test_cognition_loop.py -o addopts=
```

- Substrate: `.venv-brain` for pytest/ruff
- Smoke boot: `python boot.py --once --json`
- Cognition debug: `guardian/telemetry/terminal.log`, `guardian/telemetry/infra.log`
- Watch scripts: `scripts/monitor_boot_pacing.py`, `scripts/watch_window_reflection.py`, `scripts/watch_pacing.py`

Prefer tests that assert **live module behavior** (`cognition_chain`, `*_loop/`, `pressure_loop/`) over regression suites that encode pre-rebuild Symphony wiring.

---

## Project spine (live code)

```text
boot.py
cognition_chain.py
pressure_loop/          # tick, contributors, pressure, episode store, reflection window
global_loop/
meta_loop/
auto_loop/
omega_loop/
deepsleep_loop/
memory/
runtime/
guardian/
hud/
core/                   # policy, pacing, temporal, shared_state
autonomic/              # neuromod
config/
tests/
```

---

## Roles + coordination

| Role | Responsibility |
|------|----------------|
| **Operator** | Spec authority, sign-off, push gate |
| **Claude** | Architecture, verification, adversarial tests |
| **Cursor** | Implementation from live spec + this doc |
| **Devin** | Cold maps and audits against **live** code |

**Agent bus:** `.agents/bus.jsonl` · `scripts/agent_bus.py`

**graphify:** `graphify query "<question>"` when `graphify-out/graph.json` exists (see root `CLAUDE.md`).

---

## Useful live maps (not phase packets)

| Topic | Doc |
|-------|-----|
| Minimal boot | `docs/architecture/MINIMAL_BOOT_SURFACE.md` |
| Windowed reflection | `docs/architecture/MODEL_C_WINDOWED_REFLECTION_MAP.md` |
| AUTO integration | `docs/architecture/AUTO_INTEGRATION_SURFACE.md` |
| Slice built maps | `docs/architecture/SLICE_*_BUILT_MAP.md`, `SLICE_*_*_MAP.md` |

---

## Operator preferences (learned)

- Pressure loop pause: **1s** between ticks when cognition pacing is on (`loop_pause_seconds` in `core/policy/symphony.yaml`); cascade runs at full speed (no in-phase pauses)
- Fallback tick cadence when pacing off: **15s** (`tick_interval_seconds`); `OMEGA_COGNITION_PACING=0` disables loop pause
- Post-ignition cascade runs **synchronously** inside the tick until episode close
- Curiosity remains causal for learning pressure; mastery influences framing only
- Face/presence: `assume_operator_present` for dev; do not gate cognition on presence alone
- MRIL v2: deterministic lobe + subdomain routing in AUTO (`auto_loop/mril_tools/`)

---

## Summary for new agents

1. **The rebuild is shipped.** You are maintaining and extending the v2 loop, not continuing Symphony phases.
2. **One path:** pressure → ignite → executive cascade → close → (maybe) window Omega.
3. **Ignore outlet packs, ignition routes, and CueDesk docs** unless explicitly deleting or archiving them.
4. **When in doubt, read** `boot.py`, `cognition_chain.py`, `pressure_loop/loop.py`, and the relevant `*_loop/loop.py`.

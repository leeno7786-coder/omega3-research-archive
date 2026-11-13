# core — the shared infrastructure spine every subsystem crosses through

**What it is** — `core/` is Omega 3.0's deterministic, layer-crossing infrastructure spine.
Anything that sits *between* subsystems lives here: filesystem path resolution,
shared state, boot lock, compat shims, and temporal authority. The rule is
one-directional: nothing in `core/` knows about the cognitive loops — the loops
import `core/`, never the reverse.

**Doctrine (post-cleanup):** `core/` = Omega-core routing/boot/substrate **only**.
All former cognitive-infrastructure modules have been relocated to the subsystems
that own them (see the move table below).

## Key files / areas

**Path + write doctrine**
- [`core/paths.py`](paths.py) → single source of truth for filesystem paths. `OMEGA_ROOT` is the repo root; everything derives from it.

**Shared state + inter-loop wire**
- [`core/shared_state.py`](shared_state.py) + [`core/shared_state_keys.py`](shared_state_keys.py) → the inter-loop data passing surface. Thread-safe key→value map; snapshots are independent.

**Boot + substrate**
- [`core/boot_lock.py`](boot_lock.py) → single-instance enforcement (`BootAlreadyRunning`).
- [`core/symphony_time.py`](symphony_time.py) → wall-clock timing primitives.

**Policy**
- [`policy/symphony.policy.yaml`](../policy/symphony.policy.yaml) → runtime flags controlling slice fronts, conductor mode, feature toggles.

**Boot lifecycle**
- [`core/boot_lifecycle.py`](boot_lifecycle.py) → ordered start manifest (`BOOT_COMPONENTS`) and idempotent reverse teardown. Writes `guardian/state/boot.manifest.json`; restores/persists substrate state (shared_state + pressure_field snapshot) across reboots. Called from `boot.py` signal hooks and `scripts/shutdown.py`.

**Cognition orchestration**
- [`core/cognition_lock.py`](cognition_lock.py) → substrate-seam lock used for
  admission and terminus state. The ingress gate routes work into four durable
  semantic FIFOs, permanently bound to physical pipelines 0–3; model-pass work
  is not a monolithic core-owned runner.
- [`core/cognition_pacing.py`](cognition_pacing.py) → wall-clock pacing for the pressure loop and cascade handoffs. Inter-phase pause comes from `pressure.cognition_pacing` policy; disable at runtime with `OMEGA_COGNITION_PACING=0`.
- [`core/pipelines.py`](pipelines.py) and [`core/pipeline_state.py`](pipeline_state.py) → permanent Chat/Browser/Agent/Window pipeline bindings and lifecycle state for lane-local queue ownership.
- [`core/episode_temporal.py`](episode_temporal.py) → compact, JSON-safe temporal envelope captured at episode open (`capture_temporal_context`). Supplies tick / phase / cycle / epoch context to every organ; routed through `core.symphony_time` as the timing authority.

**Config bundles**
- [`core/directives_config.py`](directives_config.py) → governed directive-routing specs (intent → route + runtime_capability map). Default intents: `IMAGE_CREATE`, `DOCUMENT_CREATE`.
- [`core/memory_config.py`](memory_config.py) → governed memory-cortex tunables: promotion thresholds (observation→episodic→semantic), demotion, confidence decay, and contradiction resolution order.
- [`core/memory_contradiction.py`](memory_contradiction.py) → deterministic contradiction adjudication using `memory_config` rules (`confidence_then_evidence_then_recency`).
- [`core/skill_execution_config.py`](skill_execution_config.py) → governed Phase 6 skill-execution settings: USE_SKILL routing toggle (`OMEGA_USE_SKILL_ROUTING`), user-skill trust policy, bundled/user skill roots, global forbidden tools.
- [`core/strength_index_config.py`](strength_index_config.py) → governed strength-index recency windows (write / recall / hot / cold age in hours), read from `config/strength_index` bundle.

**Build / workspace surfaces**
- [`core/build_verification.py`](build_verification.py) → deterministic fold-back battery (`verify_build_against_request`). Called from `cognition_chain.py` fold-back sites and `omega_loop/episode/packet.py`.
- [`core/dev_rollback_capture.py`](dev_rollback_capture.py) → captures autonomous build file snapshots into the dev-lobe rollback sandbox.
- [`core/workspace_port.py`](workspace_port.py) → ports completed/stopped builds from workspace to operator directory (`record_port`, `execute_port`, `take_port`, `reconcile_stale_port`, `sweep_workspace_for_fresh_build`). Called from `cognition_chain.py`, `pressure_loop/practice_mint.py`, and `boot.py`.

**Runtime surfaces**
- [`core/hud_path_guard.py`](hud_path_guard.py) → path-validation helpers for HUD-facing file operations: `validate_path_segment`, `resolve_repo_path` (repo-jailed), `resolve_face_image_path`.
- [`core/mind_state_surface.py`](mind_state_surface.py) → canonical definition of Omega's live, gitignored mind-state surface: the 12 memory lobes + `memory/indexes`, `guardian/state`, `deepsleep_loop/state`, `pressure_loop/state`. Used by `guardian.mind_checkpoint` for rollback scope.
- [`core/persona_router.py`](persona_router.py) → conservative whole-name detection and per-person memory routing under lobe 1 (`memory/100_personas/person_stub/<person_id>/`). Every recognized non-operator person gets one canonical profile-root stub before facts and relationships attach to their tree. Maintains a durable `guardian/state/persona_index.json` catalog; exposes `detect_persons`, `route_persona`, `ensure_persona_profile`, `add_memory_for_person`, `link_persons`, `recall_for_person`.

## Historical relocations (receiving subsystem)

| Old `core/` path | New path | Receiving subsystem |
|------------------|----------|---------------------|
| `symphony_neuromod.py` | `autonomic/symphony_neuromod.py` | autonomic |
| `cognitive_churn.py` | `autonomic/cognitive_churn.py` | autonomic |
| `semantic_state_engine.py` | `memory/retina.py` | memory (Ettin semantic perception) |
| `symphony_strength.py` | `memory/symphony_strength.py` | memory |
| `runtime_capability_broker.py` | `runtime/runtime_capability_broker.py` | runtime |
| `model_router.py` | `runtime/model_control.py` | runtime |
| `tool_dispatch.py` | `runtime/tool_dispatch.py` | runtime |
| `runtime_tool_dispatch.py` | `runtime/runtime_tool_dispatch.py` | runtime |
| `operator_presence.py` | `sensory/operator_presence.py` | sensory |
| `operator_identity.py` | `hud/operator_identity.py` | hud |
| `curiosity_store.py` | `hud/curiosity_store.py` | hud |
| `data_logger.py` | `guardian/data_logger.py` | guardian |
| `operator_pressure.py` | `pressure_loop/operator_pressure.py` | pressure_loop |
| `pressure_loop_pause.py` | `pressure_loop/pressure_loop_pause.py` | pressure_loop |
| `symphony_signals.py` | `pressure_loop/symphony_signals.py` | pressure_loop |
| `symphony_learning_state.py` | `pressure_loop/symphony_learning_state.py` | pressure_loop |
| `symphony_probes.py` | `pressure_loop/symphony_probes.py` | pressure_loop |
| `open_questions.py` | `pressure_loop/open_questions.py` | pressure_loop |
| `cycle_cadence.py` | `pressure_loop/cycle_cadence.py` | pressure_loop |
| `schema/*` | `schema/*` (top-level) | schema |

## Current State / Legacy Findings

As of the latest codebase audit:

| Metric | Count |
|---|---|
| Python files currently present | 29 |
| Package role | shared routing, boot, state, configuration, and substrate helpers |

**Every file currently in `core/` is wired into production or test code.** Dead material is almost entirely *references to modules that no longer exist in `core/`* rather than unused files inside `core/`.

### Audit boundary

`core/` is not the home of cognition phases, model routing, memory engines, or
HUD business logic. Those responsibilities belong to their owning packages;
`core/` supplies only cross-cutting substrate services and configuration readers.

## Entry points / how it's used

- **Boot:** `boot.py` imports `core.paths.OMEGA_ROOT` and `core.boot_lock`
- **Inter-loop exchange:** loops read/write `SharedState` via the `PressureRuntime` instance
- **Config:** `policy/symphony.policy.yaml` is the runtime override authority for all slice gates and pressure tunables

## Depends on / depended-on-by

- **Depends on:** `config.loader` and the governed YAML bundles under `config/`
  and `policy/`
- **Depended-on-by:** every subsystem — `pressure_loop/`, `global_loop/`, `meta_loop/`, `auto_loop/`, `omega_loop/`, `deepsleep_loop/`, `memory/`, `guardian/`, `boot.py`, `hud/`, and operator surfaces all import `core/`. `core/` imports none of them at module load (lazy local imports break the few necessary cycles).

## Gotchas / doctrine

- **State-root doctrine:** every live write lands under `memory/` or
  `guardian/`. Code constants point at `guardian/logs/` and `guardian/state/`;
  historical state-root names are not runtime paths.
- **Single authorities (must not be redefined elsewhere):** `paths.py`, `shared_state.py`. Path resolution and shared-state routing flow only through these modules.
- **See contracts** under [`docs/contracts/`](../docs/contracts/) before changing any contract surface — downstream readers break silently on drift.

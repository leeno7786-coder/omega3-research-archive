# autonomic — the biological-signal (neuromodulator) layer

**What it is** — `autonomic/` computes Omega's biological signals: per-cycle
neuromodulator scores that observe how a cognitive cycle went, plus the
wall-clock substrate that grounds every layer's notion of "now". The two
core signal modules (`symphony_neuromod.py`, `cognitive_churn.py`) and the
temporal grounding module (`temporal_grounding/`) are strictly
**observational** — they are written to memory and emitted to telemetry but
do **not** steer execution. Meta never interprets them; only the GLOBAL
layer reflects on their trends. The timekeeping signals (`temporal_grounding`)
are pure functions of wall time — no scheduling, no inference, no policy.

**Historical retirement note:** Earlier scoring packages and the former session-only
temporal module were removed during the rebuild. The live signal computation is in
`symphony_neuromod.py`; `cognitive_churn.py` is owned by this package. The current
temporal subpackage contains only the durable lifetime-clock persistence helper.

## Current State / Legacy Findings

The current live surface is `symphony_neuromod.py`, `cognitive_churn.py`,
`_history.py`, `neuromod_config.py`, `neuromod_reinforce.py`,
`pathway_credit.py`, and `temporal_grounding/temporal_persistence.py`. The two
package initializers are markers; there is no live phase-clock implementation in
this package.

**Live neuromod stack:** `symphony_neuromod.py`, `cognitive_churn.py`, `_history.py`, `neuromod_config.py`, `neuromod_reinforce.py`, and `pathway_credit.py` all have production callers.

**Known dead / legacy seams:**

- The former session-only temporal-grounding implementation is historical; real
  episode timing authority lives in `core/episode_temporal.py` and
  `core/symphony_time.py`.
- Several exported helpers in `_history.py` are dead: `recent_cycles_for_domain()`, `extract_cycle_kinds()`, `extract_stress_levels()`, `extract_plasticity_strengths()`, `last_stress_level()`, `last_consecutive_chronic()`.
- `symphony_neuromod.emit_neuromod_terminal()` is exported/documented but never called.
- `cognitive_churn.export_last_churn_for_snapshot()` has no callers.
- In `neuromod_config.py`, only the **dopamine accessors** are on the live path (`dopamine_limits()`, `dopamine_thresholds()`, `dopamine_reinforce_scale()`). The former plasticity/stress/strength config surfaces were removed.
- `StrengthIndex` is owned by `memory/strength_index.py`; it is not an
  autonomic module.


**Key files / areas (as-built)**

**Live modules:**

- `autonomic/symphony_neuromod.py` — RPE formula, salience latch,
  closed-body spike. Pure never-raise functions.
  - `compute_episode_neuromod(auto, baseline)` — signed dopamine, |RPE|
    plasticity, stress, serotonin, strength (`:256-327`)
  - `compute_closed_body_spike(neuromod)` — cross-tick arousal scalar (`:329-345`)
  - `compute_memory_salience(neuromod)` — encoding scale + tau factor (`:106-129`)
  - `set_current_memory_salience()` / `current_memory_salience()` — ambient
    latch for memory writes (`:131-142`)
- `autonomic/cognitive_churn.py` — Saturation detection, dopamine plateau,
  habituation signals. Evaluates whether a domain or goal has been
  over-repeated and should be attenuated.
- `autonomic/temporal_grounding/temporal_persistence.py` — durable lifetime
  clock (`record_boot`, `checkpoint`, `record_shutdown`) under `guardian/state/`.
  Per-episode timing context is produced by `core/episode_temporal.py` and
  `core/symphony_time.py`.
- `autonomic/_history.py` — Reads recent `cycle_event` records from the temporal
  lobe (lobe 9) via `memory.gates.lobe_gates.TemporalGate`. **Live helpers:**
  `recent_cycles()`, `extract_dopamine_outcomes()`. **Test-only:**
  `consecutive_failures()` (sole caller is `tests/core/test_audit_substrate_medium.py:105`;
  zero production callers per the 2026-06-23 substrate audit).
  **Dead helpers** (no callers): `recent_cycles_for_domain()`,
  `extract_cycle_kinds()`, `extract_stress_levels()`,
  `extract_plasticity_strengths()`, `last_stress_level()`,
  `last_consecutive_chronic()`.
- `autonomic/neuromod_config.py` — failure-soft, cached accessors for the live
  dopamine config (`dopamine_limits()`, `dopamine_thresholds()`, and
  `dopamine_reinforce_scale()`).
- `autonomic/neuromod_reinforce.py` — `reinforce_recalled_memories(episode,
  neuromod)` credit-assigns the cycle's dopamine RPE to the recalled engrams
  via `StrengthIndex.reinforce`; flag-gated `OMEGA_DOPAMINE_REINFORCE`
  (default OFF); called from `cognition_chain._start_auto`.

**Historical deleted modules (not on disk):**

- ~~`dopamine/autonomic_module.py`~~ — RPE computation moved to
  `autonomic/symphony_neuromod.py:256-327`.
- ~~`plasticity/autonomic_module.py`~~ — Three-factor encoding gate moved
  to `autonomic/symphony_neuromod.py`.
- ~~`strength_index/autonomic_module.py`~~ — Cycle-agnostic robustness
  snapshot moved to `memory/strength_index.py`.
- ~~`stress/autonomic_module.py`~~ — HPA-axis integrator moved to
  `autonomic/symphony_neuromod.py`.
- ~~`_memory_stats.py`~~ — Removed; `memory.strength_index` now serves
  the robustness snapshot directly.

`autonomic/__init__.py` is documentation only — there is no package-level
export surface.

**Entry points / how it's used**

The autonomic modules are **callable libraries** that compute biological
signals. Each module exposes a single public entry function.

**Current integration:** Autonomic signals feed the pressure loop through
the advisory tonic-body path: `autonomic/symphony_neuromod.py` normalizes
dopamine/plasticity/strength/stress into a cross-tick body-state signal,
which `pressure_loop/pressure_field.py` reads during contributor evaluation
(dopamine gamma vigor, serotonin patience damp). `cognitive_churn.py`
attenuates curiosity-gap / learning-debt when a domain is habituated.
`temporal_persistence.py` is wired to boot lifecycle persistence; live episode
timing context is produced by `core/episode_temporal.py` and
`core/symphony_time.py`.

**Depends on / depended-on-by**

- **Depends on:** `config/dopamine.yaml` for the live dopamine tunables;
  strength-index recency is owned by `core/strength_index_config.py` and
  memory, while episode timing is owned by `core/episode_temporal.py` and
  `core/symphony_time.py`;
  `guardian.terminal_feedback` (`emit_phase`/`emit_signal`);
  `memory.gates.lobe_gates.TemporalGate`, `memory.engine.walker`,
  `memory.strength_index` (via the two `_*` helpers).
- **Depended-on-by:** `cognition_chain._start_auto` (neuromod compute + latch);
  `pressure_loop/pressure_field.py` (advisory tonic-state read);
  `memory/strength_index.py` (salience latch sample);
  tests in `tests/autonomic/`.
   *Timing context comes from `core/episode_temporal.py` and
   `core/symphony_time.py`; the lifetime clock is persisted by
   `temporal_persistence.py`.*

## Observability

All autonomic operations emit via `guardian.terminal_feedback` (failure-soft, never halts cognition) with matching `guardian.data_logger.emit_event` JSONL durability where noted.

| Operation | Hook | Durable JSONL subsystem |
|-----------|------|------------------------|
| compute_episode_neuromod | `emit_data` + `emit_phase` + `emit_signal` | `omega.autonomic.neuromod` |
| compute_memory_salience | `emit_data` | `omega.autonomic.salience` |
| compute_closed_body_spike | `emit_signal` | `omega.autonomic.spike` |
| cognitive_churn internals | `emit_phase` + `emit_data` | `omega.autonomic.churn` |
| temporal lifetime persistence | `emit_data` | `omega.autonomic.temporal` |
| _history reads | `emit_data` | `omega.autonomic.history` |

> Full coverage map: `docs/architecture/FAILURE_LOG_COVERAGE.md`

**Gotchas / doctrine**

- **Observational only.** No module influences execution; meta never
  interprets a signal; only GLOBAL reflects on trends. (Stress *modulates*
  dopamine/plasticity — but that modulation is applied inside those
  modules, never as an execution gate.)
- **Failures are data.** Every cycle produces every signal — there is no
  "skip autonomic on failure" path. `META_SILENCE` is intentional idle
  (neutral signal, neither reward nor penalty), not failure — see the
  inline `project_idle_is_data_doctrine` references.
- **History goes through lobe 9 only.** Cross-cycle reads flow through
  `_history.py`; modules never do inline file/recall I/O. Records without
  a `score.kind == "cycle_event"` block are skipped (no signal history).
- **Strength is state, not output.** strength_index reads memory state via
  `memory.strength_index` (coord index + robustness tracker); it is the
  only autonomic signal that touches memory state directly. The other
  signals operate on cycle telemetry. (`_memory_stats.py` is deleted —
  `memory.strength_index` now serves the robustness snapshot.)
- **Config-driven, schema-shared.** Thresholds/weights/windows live in
  `config/<signal>.yaml`; modules read config and nothing else does.
  Schemas live in `schema/`; consumers import, never redefine.
- **Timekeeping is separate from signal computation.** Episode timing comes
  from `core/episode_temporal.py` and `core/symphony_time.py`; the lifetime
  clock persists boot, checkpoint, and shutdown totals under `guardian/state/`.
- Relevant real contracts: [`docs/contracts/04_scoring_contract.md`](../docs/contracts/04_scoring_contract.md),
  [`docs/contracts/01_schema_contract.md`](../docs/contracts/01_schema_contract.md),
  [`docs/contracts/00_cognitive_doctrine.md`](../docs/contracts/00_cognitive_doctrine.md).
  (Note: `docs/contracts/DOCTRINE_V2.md` is phantom doctrine — do not
  treat it as authoritative.)

# policy — per-subsystem policy contracts

YAML policy contracts for each subsystem live in this directory.

**Two files are live runtime value-sources:**
- `symphony.policy.yaml` — cascade conductor policy, read by `pressure_loop/policy.py` as the runtime overlay for `config/*.yaml` bases.
- `shared_state.policy.yaml` — persistence contract, read by `guardian/state_persistence.py`.

**The remaining `*.policy.yaml` files are governance/behavioral-contract artifacts.** They are loaded opaquely by `config.loader.load_policy_contract()` to satisfy the every-stem invariant, but their values are **not consumed at runtime**. Live tunables for those subsystems come from `config/<stem>.yaml` (with `symphony.policy.yaml` overlay where applicable).

**The every-stem invariant (2026-07-04):** every governed config stem ships the complete triple — `config/<stem>.yaml` + `schema/<stem>.schema.yaml` (registered in `SCHEMA_ROOT_BY_STEM`, validated at load) + `policy/<stem>.policy.yaml` (loadable via `load_policy_contract`). Enforced by `tests/config/test_config_governance_gaps.py::test_policy_and_schema_triples_exist`.

## Slice loops

| File | Subsystem |
|------|-----------|
| `auto.policy.yaml` | auto_loop |
| `deepsleep.policy.yaml` | deepsleep |
| `global.policy.yaml` | global_loop |
| `global_intent.policy.yaml` | global_loop intent |
| `global_b.policy.yaml` | global_loop |
| `journal.policy.yaml` | episode journal |
| `knowledge.policy.yaml` | knowledge extractor |
| `meta.policy.yaml` | meta_loop |
| `meta_b.policy.yaml` | meta_loop |
| `omega.policy.yaml` | omega_loop |
| `pressure.policy.yaml` | pressure_loop |
| `pressure_contributors.policy.yaml` | pressure_loop contributor registry |
| `tools.policy.yaml` | auto_loop tools |

## Infrastructure & ingress

| File | Subsystem |
|------|-----------|
| `boot.policy.yaml` | boot lifecycle |
| `cognition_chain.policy.yaml` | executive cascade |
| `reflection_window.policy.yaml` | staged ten-episode reflection window |
| `operator_chat.policy.yaml` | operator ingress |
| `sensory.policy.yaml` | operator presence |
| `tts.policy.yaml` | text-to-speech |
| `directives.policy.yaml` | image/document directives |
| `runtime.policy.yaml` | configured runtime sockets |
| `skill_execution.policy.yaml` | bounded skill-mediated execution |
| `memory.policy.yaml` | memory cortex |
| `curiosity.policy.yaml` | curiosity engine |
| `guardian.policy.yaml` | guardian decisions |
| `web_sources.policy.yaml` | web_fetch URL catalog |

## Neuromod signals

| File | Signal |
|------|--------|
| `dopamine.policy.yaml` | RPE |
| `strength_index.policy.yaml` | engram robustness |

Slice configs are also read by `pressure_loop/policy.py`. Top-level `policy/*.policy.yaml` files are contract artifacts unless noted otherwise.

**Cascade alignment:** `pressure_loop/policy.py` owns the configured pressure and
cascade gates. Ordinary episodes run the per-episode Omega pass; the reflection
window policy governs the separate staged ten-episode reflection lane. Do not
duplicate these gates inside `cognition_chain.py`.

## Current State / Legacy Findings

As of the latest codebase audit:

| Metric | Count |
|---|---|
| Total YAML policy files | 30 |
| Live runtime value-sources | 2 (`symphony.policy.yaml`, `shared_state.policy.yaml`) |
| Governance-only / narrative contracts | 28 |
| Files with stale/ghost entries | several (see below) |

### Live runtime files

| File | Consumer | Purpose |
|---|---|---|
| `symphony.policy.yaml` | `pressure_loop/policy.py` | Runtime overlay for config bases; slice gates, pressure tunables, Omega params, Meta A/B routing, AUTO prompts, etc. |
| `shared_state.policy.yaml` | `guardian/state_persistence.py` | SharedState persistence cadence, debounce, audit flags, per-key persist overrides. |

### Governance-only contracts (values not consumed at runtime)

All other `policy/<stem>.policy.yaml` files exist for the every-stem invariant and as narrative behavioral contracts. Live values for these subsystems come from `config/<stem>.yaml`.

### Audit status

The historical ghost list previously kept here was a point-in-time scan and
had itself become stale. Current compliance is recorded in `AGENTS.md` and
focused validation tests. Dated baselines such as
`docs/BASELINE_2026-08-03.md` are historical evidence, not current status.

Historical policy references outside this directory are not runtime authority.
Use the root `policy/` files together with `config/` and
`pressure_loop/policy.py`.

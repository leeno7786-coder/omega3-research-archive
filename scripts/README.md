# scripts — operational tooling: reset, audits, launchers, maintenance

**What it is** — The repo's operator-run script surface: one-off and
recurring tools that act *on* Omega 3.0 from the outside rather than
running inside the cognitive loop. It holds the canonical factory
reset, deterministic memory/routing audits and the boot gate, the HUD
launcher, live e2e probes, and the inter-agent coordination bus. Almost
everything is
`.\.venv-brain\Scripts\python.exe scripts/<name>.py` (plus `.bat`/`.sh`/`.ps1`
wrappers). The scripts are operator- and CI-invoked; they are not imported
by the live cognitive loop.

**Key scripts** (load-bearing first; grouped by purpose):

*Reset / factory-ship*
- `scripts/day_zero_reset.py` → **THE canonical factory reset.**
  Scorched-earth wipe of every runtime persistence surface (memory
  lobes + indexes, `guardian/state|ledger|checkpoints|logs|telemetry`,
  deepsleep/pressure state, knowledge-extractor + taxonomy stores),
  then re-seeds the 7 founding constitution records from
  `memory/identity/seeds.py` (`CONSTITUTION_SEEDS`) via the real
  atomic-write pipeline. Preserves the gitignored owl key
  (`guardian/state/openrouter.json`, `GUARDIAN_STATE_PRESERVE`) and the
  historical routing artifacts are not runtime memory authority.
  Idempotent. `.\.venv-brain\Scripts\python.exe scripts/day_zero_reset.py`.

*Boot gate / lifecycle*
- `scripts/boot_gate.py` → **the green BOOT GATE** run BEFORE booting:
  the memory stress report + a targeted identity/persona/knowledge
  recall check over the unified graph recall path; asserts ALL GREEN.
  Does not boot anything.
- `scripts/boot_smoke_once.py` → offline boot smoke — one pressure tick
  with a seeded contributor; asserts cascade order.
- `scripts/shutdown.py` → gracefully stop the running substrate (reads
  `guardian/state/boot.lock`, verifies the PID is a live `boot.py`,
  sends a graceful stop and lets teardown run).

*Memory audits / benchmarks*
- `scripts/memory_live_check.py` → live validation of the
  memory-taxonomy learning loop through the REAL wired path (isolated
  cold root, never touches the real store).
- `scripts/memory_benchmark_scorecard.py` → deterministic memory
  benchmark scorecard.
- `scripts/memory_determinism_replay.py` → Memory Engine v2
  deterministic ingest/recall replay harness.
- `scripts/memory_nondeterminism_scan.py` → scans memory authority
  files for nondeterministic primitives (wall-clock IDs, randomness,
  unsorted enumeration, model calls) against an explicit allowlist.
- `scripts/memory_stress_report.py` → multi-angle stress report driving
  `memory/routing/stress_corpus.py` through the live routing
  authorities (`subdomain_for` + `route_memory_tool`).
- `scripts/memory_battery_1000.py` → the 1,000-case OUT-OF-CORPUS
  memory battery (no-tuning terms of engagement, 2026-07-04).
- `scripts/memory_ablation_gate1.py` → Gate-1 ablation assault
  (DME v2 / Thought Engine) measured mode by mode.
- `scripts/memory_iso_autopsy.py` → autopsy for isolated-lane wrong-admission.
- `scripts/audit_emit_coverage.py` → AST audit of observability emit
  coverage across the rebuilt substrate.
- `scripts/clean_memory_pollution.py` → remove test/MagicMock pollution
  from live memory, reindex, prune strength orphans.
- `scripts/omega_memory.py` → `omega memory` dev/debug CLI for the
  deterministic memory taxonomy (compile / replay / snapshot).

*Routing audits*
- `scripts/routing_harness_report.py` → Phase-7 routing harness metrics
  (CI / operator diagnostic).
- `scripts/routing_catalog_status.py` → production routing edge-case
  catalog (`_unsorted` pile) go/no-go report.
- `scripts/routing_grill_1000.py` → the ~1,040-scenario routing grill
  (semantic frames × grammatical angles) graded against the live
  `subdomain_for`.
- `scripts/route_cases.py` → route a JSON batch of cases through the
  live deterministic router (measurement runner for red-team
  generation — cases scored against real behaviour, never imagination).
- `scripts/routing_regression.ps1` → full deterministic routing
  regression (Phases 1–7).

*Launchers*
- `scripts/hud-only.bat` → HUD launcher (**moved here from the repo
  root**). Routes through the SSD-local `Omega.exe --launch-hud`
  broker, which then starts the bundled HUD runtime. It boots the
  substrate **PAUSED** (PressureRuntime + memory/guardian warmup,
  pressure loop held) then serves the HUD. Pass `--no-boot` for empty
  placeholders only, `--https` for the local-CA cert. Resolves the
  public executable from the portable layout; it never uses host Python.
  Usage: `scripts\hud-only.bat [PORT] [--https|--no-boot]`.
- `scripts/runtime_direct.py` → direct CLI to the configured runtime
  (`:11434`) for generation, model-list, and health, bypassing the HUD and
  substrate.
- `hud/scripts/build_hud.py` → assembles the modular HUD source into
  `hud/omega_interface.html` and checks whether the bundle is current.

*Live e2e / smoke*
- `scripts/live_first_learn_episode.py` → boot the substrate, ensure
  the onboarding seed, run bounded natural pressure ticks until a
  seeded gap ignites a LEARN and the full cascade runs.
- `scripts/live_first_wake_test.py` → cold-mind first-wake proof
  (onboard → mastery gaps → dynamic-Y flip → a real learning topic
  resolves); additive, no factory wipe.
- `scripts/live_operator_omega_check.py` → capstone: operator message →
  full cascade → per-episode Omega reply patched into the outcome and
  delivered to the HUD. Creates one real episode against `:11434`.
- `scripts/live_onboarding_trajectory_check.py` → cold-boot validation
  that onboarding establishes the Omega trajectory at episode 0
  (isolated cold `OMEGA_ROOT`, real code paths + SharedState).

*Seeds*
- `scripts/seed_identity_lobe.py` → writes the 7 founding constitution
  records (`memory.identity.seeds.CONSTITUTION_SEEDS`) into lobe 0
  (`000_identity/constitution`) on a live boot. Idempotent (skips ids
  already present in `coord_index`).

*Skills*
- `scripts/migrate_skills_phase6d.py` → migrate the bundled Omega skills
  to the execution-skill frontmatter contract.
- `scripts/fix_skill_registration.py` → normalize SKILL.md files so the
  execution-skill loader registers them correctly.

*Agent-bus coordination*
- `scripts/agent_bus.py` → the append-only inter-agent coordination bus.

*Pacing / reflection watchers*
- `scripts/monitor_boot_pacing.py`, `scripts/watch_pacing.py` → watch
  `terminal.log` for pacing milestones this boot session.
- `scripts/watch_window_reflection.py` → watch until the 10-episode
  reflection window locks and Omega runs.

**Entry points / how it's used** — The canonical ops:
- Factory reset: `.\.venv-brain\Scripts\python.exe scripts/day_zero_reset.py`
- Boot gate (pre-boot green check): `.\.venv-brain\Scripts\python.exe scripts/boot_gate.py`
- Public startup: `Omega.exe` (setup wizard); HUD launcher:
  `Omega\scripts\hud-only.bat 13310` (append `--https` for a local-CA
  cert, `--no-boot` for placeholder telemetry)
- Identity re-seed on a live root: `.\.venv-brain\Scripts\python.exe scripts/seed_identity_lobe.py`
Every Python script self-inserts the repo root onto `sys.path`, so run
them from the repo root through the bundled `.venv-brain` interpreter.

**Depends on / depended-on-by** — Scripts reach *into* the substrate:
`memory.api`/`memory.engine` (resets, seeds, recall/routing probes),
`guardian.*` (state/ledger/checkpoint wipe on reset, approval +
telemetry surfaces), `hud.standalone` (launcher),
`core.paths` and the Guardian logging/telemetry surfaces (root + logger
convention). Nothing
in the live cognitive loop imports these — they are operator/CI-invoked
only. CI/acceptance flows lean on `boot_gate.py`, `boot_smoke_once.py`,
and the `memory_*`/`routing_*` audit scripts.

**Gotchas / doctrine**
- `day_zero_reset.py` is the **canonical factory reset**: a
  scorched-earth wipe of all runtime state that re-seeds the 7 founding
  constitution records via `seed_constitution_lobe()`
  (`memory.identity.seeds.CONSTITUTION_SEEDS`). It PRESERVES the
  gitignored connector state under `guardian/state/` (`GUARDIAN_STATE_PRESERVE`)
  and leaves any historical routing artifacts alone; delete the connector
  state by hand for a true hand-off to a NEW operator. Verify the end state
  after any reset
  — a reset tool has previously *lied* ("DONE" over stale enumerated
  survivors); don't trust the script's own report.
- **Historical reset note:** older documentation described two founding
  identity records. The current `seed_constitution_lobe()` behavior seeds the
  full 7-record `CONSTITUTION_SEEDS` set.
- `hud-only.bat` routes through `Omega.exe`, prepares the portable
  debugger-enabled Edge profile, and boots the substrate **PAUSED** by default
  (PressureRuntime + memory/guardian warmup, pressure loop held); it is *not* a
  placeholder run unless you pass `--no-boot`. The script resolves the public
  executable from the SSD layout before touching the hidden runtime.
- Phantom/stale-doc disease is real here: confirm any reset/seed script
  against its docstring AND the live code, and verify on-disk state
  after running it.

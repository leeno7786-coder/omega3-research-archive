# Guardian

The **Guardian** — security, identity, and epistemic gating layer. It is the
autonomous immune authority for untrusted external ingress and response-class
selection, using ALLOW, REJECT, FLAG, and QUARANTINE decisions where those
boundaries apply.

## Scope

Guardian does not run in the cognition cascade directly; it is a **cross-cutting authority** warmed at boot and invoked at memory-write boundaries. It owns:
- Response class registry (policy-bound response behaviours)
- Decision authority (`decide_request`)
- Terminal feedback / telemetry emitters
- HMAC ledger for audit trails
- Production mode enforcement

Memory write validation is handled at the memory level (`memory.write_guard`).
Guardian does not gate the mind's own canonical writes.

## Architecture

### `response_classes.py` — Response Class Registry
Loads `config/guardian_response_classes.yaml` and exposes lookup + classification helpers (`guardian/response_classes.py`).

**Public API:**
- `get_class(subtype) -> ResponseClass` — look up a class by stable subtype identifier
- `get_class_for_decision(decision_state) -> ResponseClass` — default class for a Guardian state
- `classify_subtype(text, decision_state) -> Optional[str]` — match user text against trigger phrases
- `list_classes(decision_state=None) -> List[ResponseClass]` — enumerate registered classes
- `reload()` — force re-read for tests / admin tools
- `status() -> dict` — total classes and triggers count

**Boot wiring:** `boot.py` calls `reload()` + `status()` during warmup (`boot.py:165-178`).

### `data_logger.py` — Telemetry/Feedback Data Logging
Per-event JSONL emitters for telemetry and feedback (`guardian/data_logger.py`). Relocated from `core/`.

### `decision.py` — Guardian Decision Authority
The autonomous immune gate for memory and action proposals (`guardian/decision.py`).

**Post-rebuild:** repointed to `pressure_loop.runtime.get_default_runtime()` for shared state access.

**Decision states:** `ALLOW`, `FLAG`, `QUARANTINE`, `REJECT`

### `terminal_feedback.py` — Telemetry Emitters
Structured degrade surfaces and phase/signal emitters (`guardian/terminal_feedback.py`). Writes to `guardian/logs/`.

### `hmac_ledger.py` — Audit Ledger
Cryptographic integrity for guardian audit trails (`guardian/hmac_ledger.py`).

### `production_mode.py` — Production Mode Enforcement
Enforces production runtime constraints (`guardian/production_mode.py`).

### `memory_purity.py` — Memory Purity Checks
Epistemic purity validation for memory content (`guardian/memory_purity.py`).

### `state_persistence.py` — SharedState Save/Restore
Guardian's sole-writer save layer for `core.shared_state.SharedState`. Atomically snapshots DURABLE_PROJECTION keys to `guardian/state/save_state.json` after each loop completes (post-loop cadence, debounced by `min_interval_seconds`). Restores on boot via `restore_at_boot()`; rejects snapshots that exceed `hydrate_max_age_seconds` or have a schema version mismatch (cold-start, never raises). A three-layer filter governs what reaches disk: (1) classification (`DURABLE_PROJECTION` only), (2) forbidden-payload shape check (raw blobs, embeddings, stack traces dropped), (3) per-key policy override in `policy/shared_state.policy.yaml`. Returns `SaveResult` / `RestoreReport` structured results; all outcomes emit a `guardian.terminal_feedback` phase event for observability.

**Public API:**
- `save_now(shared_state, *, cycle_id, force) -> SaveResult` — sole durable writer
- `restore_at_boot(shared_state) -> RestoreReport` — boot-time hydration (never raises)
- `reset_save_state() -> bool` — called by day_zero_reset

### `mind_checkpoint.py` — Mind State Checkpoint/Restore
Snapshot/rollback for Omega's live mind state before a reversible skill-execution
run. Captures files defined by `core.mind_state_surface.mind_state_files()` into
`guardian/checkpoints/<id>/` with a SHA-256 hashed manifest. The checkpoint store
itself is excluded from the captured scope. Prunes to the newest `DEFAULT_KEEP`
(3) checkpoints after each write.

**Public API:**
- `checkpoint_mind_state(*, omega_root, label, checkpoint_id, keep) -> CheckpointResult`
- `restore_mind_state(checkpoint_id, *, omega_root) -> RestoreReport`
- `list_checkpoints(omega_root) -> List[str]`
- `verify_checkpoint(checkpoint_id, *, omega_root) -> bool`

### `loop_phase_logger.py` — Loop Phase JSONL Persistence
Writes before/after phase boundary events from any cognition loop to `guardian/logs/<loop>.jsonl` via `guardian.data_logger.emit_event`. Consumed by loops that use `phase_span` enter/exit hooks; failure-soft (swallows all emit errors).

**Public API:**
- `log_loop_phase(*, hook, loop, phase, status, **fields) -> None`

### `pressure_observability.py` — Pressure Loop Observability
Structured JSONL observability for pressure gather/evaluate/neuromod/ignition-gate events. All events persist to `guardian/logs/pressure.jsonl` under the `omega.pressure.loop` subsystem via `guardian.terminal_feedback.emit_guardian_event`. Failure-soft; never blocks ticks.

**Key helpers:** `log_gather_snapshot`, `log_contributor_field`, `log_neuromod_tonic_transition`, `log_neuromod_apply_before/after`, `log_evaluate_raw_contributors`, `log_ignition_gate_detail`, `log_tick_summary`, `log_episode_neuromod_compute_before/after`.

### `telemetry/writer.py` — Structured Telemetry Sink
Structured JSONL telemetry sink (`guardian/telemetry/writer.py`). Writes `firehose.jsonl`, per-system telemetry files, and `failures.jsonl`; tracks run/episode IDs. Consumed by `terminal_feedback`, `data_logger`, and boot/pressure instrumentation.

### `log_router.py` — Per-Component Rotating Logs
Routes every `logging.getLogger("omega.*")` logger to per-component rotating files under `guardian/logs/`. Installed by `boot.py` (`install_component_logging`).

### `terminal_console.py` — Cognition Debug Terminal Printer
Subscribes to `guardian.terminal_feedback` at boot and renders the live emit stream to stdout + `guardian/telemetry/terminal.log`. Operational/runtime probe noise is routed to `guardian/telemetry/infra.log` only. Installed at `boot.py:180-183` via `guardian.terminal_console.install()` (failure-soft).

**Routing rules:**
- Cognition loops (pressure, global, meta, auto, omega, deepsleep, memory, broker, symphony) → stdout + `terminal.log`
- Operational probes (capability_evaluate, STT/TTS, image/document ops, etc.) → `infra.log` only
- `OMEGA_TERMINAL_TELEMETRY_FILTER=all` → legacy print-everything mode
- `OMEGA_TERMINAL_TELEMETRY=0` → disable entirely

## Config

| Artifact | Path |
|----------|------|
| Response classes | `config/guardian_response_classes.yaml` |

## Boot Sequence

Guardian warmup happens in `boot.py` after memory warmup and capability catalog sync (no explicit phase label at that point in the file):
1. `from guardian.response_classes import reload, status`
2. `reload()` — loads YAML into cache
3. Log class/trigger counts (`boot.py:165-178`)
4. `from guardian.terminal_console import install as install_terminal_console` → `install_terminal_console()` (`boot.py:180-183`) — registers the cognition debug terminal printer

Failure-soft: if Guardian warmup fails, boot continues with a warning (`boot.py:176-178`). The `terminal_console` install step is also failure-soft.

## Current State / Legacy Findings

As of the latest codebase audit:

| Metric | Count |
|---|---|
| Python files in `guardian/` | 17 |
| Actively used modules | 16 (excluding the package marker) |
| Unused / orphaned modules | 0 |
| Modules with broken outgoing references | 0 |
| Referenced-but-missing modules | 0 in the live Guardian/HUD path |
| Empty runtime directories | 2 (`guardian/audit/`, `guardian/checkpoints/` — populated only at runtime) |

### Actively used

`terminal_feedback`, `terminal_console`, `telemetry/writer`, `data_logger`, `log_router`, `response_classes`, `decision`, `memory_purity`, `state_persistence`, `mind_checkpoint`, `hmac_ledger`, `production_mode`, `pressure_observability`, `loop_phase_logger`.

### Audit boundary

Optional HUD adapters may report a degraded panel when an optional authority is
unavailable. The live write-purity implementation is `memory.write_guard`, and
the live external-ingress decision surface is `guardian.external_ingress` plus
`guardian.decision`.

### Docs-vs-reality drift

- **"Guardian is memory's sole-writer gatekeeper" / `propose=True` framing** in `memory/README.md` is wrong. The root `AGENTS.md` and live code agree: cognitive self-writes are direct (`bypass_guardian=True`), and `guardian.decide_request` is invoked only for user-chat-driven writes. This README's Gotchas section has been updated to reflect that.

## Entry Points / How It's Used

- **Memory writes (user-chat path):** `memory.api.write_memory(propose=True)` → stages envelope → calls `guardian.decide_request` → on ALLOW, commits encoding. This path arbitrates **external/user-chat input only**.
  - **Cognitive self-writes** (episodic capture, identity, consolidation, learned knowledge, etc.) are direct writes via `memory.api.write_memory(propose=False, bypass_guardian=True)` / the gate direct path, per root `AGENTS.md` doctrine. Guardian does **not** gate the mind writing to itself.
  - Memory write validation (purity checks) is handled by `memory.write_guard` at the memory level.
- **Response rendering:** voice_box calls `guardian.response_classes.get_class_for_decision()` to select a response class; the model renders language inside those bounds
- **Telemetry:** all subsystems emit via `guardian.terminal_feedback.emit_*`

## Depends on / Depended-on-by

- **Depends on:** `core.paths.OMEGA_ROOT`, `pressure_loop.policy` (indirect), `config/guardian_response_classes.yaml`
- **Depended-on-by:** `memory.api` (write gate), `memory.gates` (per-lobe gate arbitration), `core` (telemetry), `boot.py` (warmup + terminal console install), `pressure_loop` (state persistence, pressure_observability), cognition loops (loop_phase_logger), APPLY executor (mind_checkpoint)

## Observability

All Guardian operations emit via `guardian.terminal_feedback` (failure-soft, never halts cognition) with matching `guardian.data_logger.emit_event` JSONL durability where noted.

| Operation | Hook | Durable JSONL subsystem |
|-----------|------|------------------------|
| decide_request (ALLOW/FLAG/QUARANTINE/REJECT) | `emit_phase` | `omega.guardian.decide` |
| verify_promotion / commit_promotion / commit_flagged / commit_quarantine | `emit_memory` + `emit_data` | `omega.guardian.commit` |
| hmac_ledger | `emit_data` + `emit_phase` | `omega.guardian.ledger` |
| response_classes | `emit_phase` | `omega.guardian.response_class` |
| production_mode | `emit_phase` | `omega.guardian.production_mode` |
| state_persistence (save/restore) | `emit_phase` | `omega.guardian.state_persistence` (via `guardian.terminal_feedback.emit_phase`) |
| pressure_observability (gather/evaluate/neuromod/ignition) | `emit_guardian_event` | `omega.pressure.loop` |
| loop_phase_logger | `emit_event` | `omega.<loop>.loop` |

> Failure-log coverage: `docs/architecture/FAILURE_LOG_COVERAGE.md`

## Gotchas / Doctrine

- **Guardian arbitrates UNTRUSTED EXTERNAL HUMAN-AUTHORED ingress — nothing else.** Two ingress classes (ratified 2026-07-17, root `AGENTS.md` banner): (1) **user chat** via `guardian.decide_request`; (2) **connector/Workspace observations** (calendar titles/descriptions, task notes, email subjects/bodies, filenames) via `guardian.external_ingress` (`screen_text`/`screen_receipt` -> ALLOW/FLAG/QUARANTINE; quarantined content is replaced by the marker; SourceLedger preserves the raw source for audit — recorded BEFORE the screen, and a failed record WITHHOLDS the content). The screen taints by DEFAULT (round-4): provider metadata (ids, revisions, timestamps) passes unscreened only while its VALUE keeps a structural shape — field names are never a trust boundary, and even ALLOW composes attributed + `harden()`-fenced (delimiters escaped). Cognitive self-writes (episodic capture, identity/traits, consolidation, learned knowledge, temporal lobe updates, etc.) remain direct writes via the gate direct path (`write_memory(propose=False, bypass_guardian=True)`). An earlier revision said "user chat only" — that wording created a designed-in bypass for Workspace ingress; do not regress it.
- **Response classes are DATA, not CODE:** adding a class requires editing the YAML and adding trigger phrases; no new code path needed.
- **This module MUST NOT be redefined elsewhere.** `guardian.response_classes` is the single registry.
- **Memory write validation is now at the memory level:** `memory.write_guard` handles purity checks, while Guardian focuses on security policy and decision authority for external input.

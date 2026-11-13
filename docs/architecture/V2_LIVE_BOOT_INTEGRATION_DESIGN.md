# V2 Live Boot Integration — Design Spec

> **Branch:** `main`
> **Spec date:** 2026-05-26
> **Author:** Claude Opus 4.7 (1M context)
> **Status:** Approved by operator; awaiting plan handoff to writing-plans skill.

---

## 1. Mission

Take the proven Omega v2 substrate live for the first time — HUD-less, terminal-monitored — with no legacy boot wiring carried in. Operator runs `python boot.py` in a terminal, watches the cognitive substrate breathe, and the brain runs as a single substrate against the live `omega-runtime` (voice model + embed + classify + face).

---

## 2. Premise

**Phase 2 capstone P2-S22 proved the cognitive substrate is real and complete.** `scripts/symphony_boot_cascade.py --scenario all` passes all 18 scenarios end-to-end, with no live runtime / network / HUD / browser / operator-device dependency. Every organ (pressure, BrainLoop ingress, Global action signaling, runtime capability receipts, temporal context, learning state, proactive response receipts, broader idle probes, tick-state WAL, SharedState bus restart, runtime boundary closure, epistemic emptiness refractory cure, temporal authority convergence, tool-to-capability mapping) reads and writes the same `CognitiveEpisode` truth.

**Operator-enforced quarantine on live cognitive boot is lifted.** The discipline that held the substrate offline through Phase 2 cure-runway work is no longer needed; substrate is whole.

**This spec covers boot-side integration only.** Substrate is unchanged. Work is verifying boot wires the proven substrate cleanly + skipping HUD spawn + ensuring terminal monitoring is sufficient for operator-side observation.

---

## 3. Scope

### In scope

- Audit `boot.py` steps 1-14 + `run_continuous` for legacy creep
- Verify HUD spawn is cleanly skippable (env var or explicit gate)
- Verify Phase A→C logging campaign's heartbeats give terminal-monitor coverage of every active cure surface
- Confirm boot's runtime probe correctly classifies live `omega-runtime` health
- Produce a first-live-boot operator runbook
- Lift the quarantine in a discrete, documented commit

### Explicitly out of scope

- HUD frontend or backend work (deferred until after first successful live boot)
- New substrate organs (Phase 2 closed; nothing new ships here)
- Re-running cascade scenarios as "proof of reality" (P2-S22 already owns that proof)
- Omega prelude movement (per `PACKET_P2_S0.md` slice plan — beyond Phase 2 boundary)
- Skills system live wire-through to dev pill (covered by `omega_cli_ts/README.md`; depends on HUD which is deferred)
- Live operator-driven smokes (operator owns these post-runbook; this spec produces the runbook, not the smokes)

---

## 4. Architecture

### 4.1 What runs at live boot (target shape)

```
python boot.py
  ├─ Import preflight (path, shim, logging, terminal feedback)
  ├─ boot() substrate initialization (steps 1-14)
  │    ├─ Memory + Guardian + Config + Autonomic readiness + Autonomous readiness
  │    ├─ Runtime/lifecycle health probe (omega-runtime up?)
  │    ├─ Cognitive tool layer (persistent — no boot-time creation)
  │    ├─ Meta/Global/Omega/DeepSleep readiness markers (LOG-ONLY)
  │    ├─ Phase handlers (BrainLoop registered as outward door; legacy auto.* compat)
  │    ├─ Engine + Orchestrator (only `brain` runner registered)
  │    ├─ Substrate-state confirm + Approval queue audit + Self-state hydration
  └─ run_continuous()
       ├─ Boot-symphony guard (conductor_mode == "symphony")
       ├─ Timing banner + post-boot health banner
       ├─ HUD spawn — SKIPPED via OMEGA_HUD_DISABLE=1
       ├─ First-contact status (informational)
       ├─ CueDesk construction
       └─ Loop:
            ├─ if is_pressure_loop_paused(): sleep, continue   (dev-pill gate from prior session)
            ├─ epoch_count += 1; write _epoch
            ├─ CueDesk.tick(shared_state)
            │     ├─ curiosity_substrate_tick
            │     ├─ evaluate_pressure (broker preflights, contributor reads)
            │     ├─ episode_store.open (if decision opens)
            │     ├─ symphony movement chain (Global 1-2 → Meta → Auto → Meta late → Global 3-4 → Omega → DeepSleep)
            │     └─ close + WAL snapshot + SharedState save_now
            └─ sleep stage_seconds["idle"]
```

### 4.2 What does NOT run (legacy quarantined)

- No timer-driven meta/global/curiosity/deepsleep/omega loop registrations
- No `run_autonomy_forever` background thread
- No HUD FastAPI server + browser
- No `_seed_first_boot_goal` (removed in `REMOVE_P2_S7_FIRST_BOOT_SEED`)
- No `goals.jsonl` planning intake

### 4.3 Terminal monitoring stack

Per `[[project_logging_campaign_landed]]` (the Phase A→C heartbeat campaign), terminal output during live boot exposes:

- `[PHASE]` emissions via `emit_phase()` (logging, terminal feedback handler) — CueDesk tick boundary, SharedState restore, episode lifecycle, movement runs
- `[SIG]` emissions via `emit_signal()` — broker preflight (every capability/route/state transition), pressure contributors, neuromods, strength/mastery updates
- `[MEM]` emissions via `emit_memory()` — memory recall + ingest receipts
- Standard `omega.*` loggers (configured via `core.logging`)
- `_UserContentRedactingFilter` at root logger redacts 26 key classes so user content never lands in terminal/jsonl

Operator can `tail -f guardian/logs/feedback_audit.jsonl` in a sidecar for structured event review.

---

## 5. Components & Work Items

### 5.1 boot.py audit

Single read pass through `boot.py` looking for:

| Surface | Required state | Failure mode |
|---|---|---|
| Step 11 orchestrator registrations | Only `brain` runner registered | Any `meta`/`global`/`curiosity`/`deepsleep`/`omega` registration = legacy creep, must remove |
| Imports | All resolve to live v2 modules | Any import from a quarantined path = creep, must remove |
| Steps 7-9 (Meta/Global/Omega/DeepSleep) | Log-only readiness markers | Any side effect or registration = creep |
| `run_continuous` conductor | CueDesk-only | Any other conductor invocation = creep |
| HUD spawn path | Honors `OMEGA_HUD_DISABLE=1` | Hard-coded spawn = blocker |

For each finding: fix with cure-runway discipline (failing test → fix → passing test → independent verification) or document why already clean.

### 5.2 HUD spawn skip verification

Per prior research (`hud/spawner.py:is_disabled` + `disabled_reason`), the env var `OMEGA_HUD_DISABLE=1` should skip HUD spawn. Verify:

- Setting the env var actually skips `spawn_hud_in_thread`
- No port binding on 13310
- No browser launch attempt
- Runtime probe still works (it uses `model_router` / `runtime_client` directly, not via HUD)
- Shutdown path doesn't try to stop a HUD that never started

If gaps exist, fix them and add a regression test.

### 5.3 Terminal monitoring coverage check

Run the boot dry — observe terminal output. Confirm visibility of:

- Every CueDesk tick (epoch + pressure aggregate + decision)
- Every pressure contributor (when value > 0)
- Every broker preflight (capability/route/state with reason)
- Every episode open/close
- Every movement run (Global act, Meta dispatch, Auto, Meta late, Global late, Omega, DeepSleep)
- Every memory operation (recall/ingest)
- Every neuromod spike + strength/mastery delta
- WAL snapshot per clean tick
- SharedState `save_now` per clean tick

Bar: operator running `python boot.py` in terminal can see the substrate breathing in real time. If anything is silent that should emit, file a fix.

### 5.4 Runtime readiness check at boot

Boot step 5 already probes `brain.runtime_client.health()`. Confirm:

- `omega-runtime` reachable at `http://127.0.0.1:11434`
- Voice model loaded (`qwen35_4b` per current runtime config)
- Embed/classify/face capabilities healthy per their broker preflights

Failure handling: per Phase 2 doctrine, boot proceeds in degraded mode if runtime is down (substrate fails-soft to alternate routes); terminal emits clear warning so operator sees it.

### 5.5 Live-boot operator runbook

Document at `docs/_archive/architecture/V2_LIVE_BOOT_RUNBOOK.md`. Sections:

1. **Pre-boot checklist** — runtime up, models loaded, models config matches `core/policy/models.yaml`, no orphan ports on 13310 / 11434 / 8091
2. **Boot command** — exact env vars (`OMEGA_HUD_DISABLE=1`), exact invocation
3. **Expected terminal patterns** — sample output for substrate inventory, runtime banner, first CueDesk tick, first idle, first endogenous pressure
4. **Healthy idle** — what no-pressure looks like vs broken-loop looks like
5. **Observe-only mode (no operator interaction)** — substrate is expected to build pressure endogenously (curiosity_gap, epistemic_emptiness, neuromod_spike, idle probes). Operator watches; no input channel wired. Document what endogenous pressure typically looks like in terminal so operator can recognize health vs stuck.
6. **Failure modes + diagnostics** — runtime down, model swap mid-boot, contributor drift, restart procedure
7. **Shutdown** — clean `Ctrl+C` path; substrate runs until operator decides they've seen enough. What WAL/state survives, what next boot picks up.

### 5.6 Quarantine-lift commit + memory entry

Discrete commit with message documenting:
- Operator-enforced quarantine lifted as of <date>
- Authorization scope: live boot + terminal monitoring, no HUD
- Verification chain: Phase 2 P2-S22 cascade PASS + this audit clean
- Memory entry: `project_v2_live_boot_authorized.md` for cross-session continuity

---

## 6. Data Flow

No new data flows introduced. Existing v2 substrate flows (per `SYMPHONY_BOOT_SEQUENCE_MAP.md §3` CueDesk tick path) apply unchanged:

```
CueDesk.tick(shared_state)
  → curiosity_substrate_tick → pressure_field.evaluate_pressure → decision
  → episode_store.open (if decision opens)
  → Global 1-2 (pressure orientation + StrengthIndex-aware recall) → one action/outcome pack
  → lower movement chain if warranted (Meta early → Auto → Meta late)
  → Global 3-4 (late truth fold)
  → Omega (single episode truth)
  → DeepSleep (archive + projection clear)
  → close
```

Live runtime serves the inference inside Auto / brain phases. Broker preflight gates each capability call.

---

## 7. Error Handling

### 7.1 Boot-time errors

- Runtime down at boot: continue boot in degraded mode; broker emits per-capability hold; substrate routes to `RESPONSE_HOLD` for inference-dependent decisions; terminal emits clear warning
- Module import error: fail boot loudly with stack trace; operator fixes import then re-boots (no live cognitive boot until import resolves)
- SharedState restore fail: per P2-S17, fail-soft `RestoreReport` with `degraded=True`; substrate proceeds with empty bus; tick-state WAL may still restore the active episode

### 7.2 Run-time errors during live cognition

- CueDesk.tick raises: caught at boot loop level, logged warning, skip to next tick (per current boot.py implementation)
- Movement chain runner raises: per `_run_loop` failure-flow doctrine, captured into per-loop result; cycle continues
- Broker preflight raises: per Phase 2 P2-S18 boundary closure, route as alternate or response_hold; never silent
- Memory write raises: per `_emit_fail` discipline, structured logging; never crashes substrate

All errors visible in terminal + `feedback_audit.jsonl` for post-mortem.

### 7.3 Operator interrupt

`Ctrl+C` → `KeyboardInterrupt` → `_shutdown(fb, epoch_count)` runs (HUD shutdown skipped since not started; SharedState last-save fires; WAL final snapshot fires; loggers flush).

---

## 8. Testing & Verification

### 8.1 Pre-commit verification per fix

For any fix made during the audit (Section 5.1, 5.2, 5.3):
1. Failing test that captures the gap (regression style)
2. Implement fix
3. Test passes
4. Independent code-reviewer agent reviews the diff (per `[[feedback_post_pr_code_review_agent_is_law]]`)

### 8.2 Capstone regression sanity check before quarantine-lift commit

Run `scripts/symphony_boot_cascade.py --scenario all` and confirm all 18 P2-S22 scenarios still PASS. This is a **regression check** that no audit fix broke the proven substrate — NOT a re-proof of the substrate (P2-S22 already owns that proof).

### 8.3 Substrate-only smoke during audit

If feasible without operator runtime: run a boot-cascade scenario for the "no-HUD live-style" path. (Likely covered by existing scenarios; verify don't add new.)

### 8.4 Operator-side live boot

Per the runbook in Section 5.5. Operator runs `python boot.py` after runtime is up + `OMEGA_HUD_DISABLE=1` set. Observes:

1. Substrate inventory prints
2. All 14 boot steps complete with green markers
3. Runtime banner shows omega-runtime green + model loaded
4. CueDesk wires as sole conductor
5. First tick fires, idle pressure builds (curiosity/empty/etc.)
6. First episode opens when pressure crosses threshold
7. Movement chain runs end-to-end
8. Archive + close completes
9. Idle resumes; next tick within `stage_seconds["idle"]`

Pass criterion: no unhandled exceptions, no `ERROR`-level log lines other than documented degraded-mode warnings (e.g., a capability legitimately marked down by the broker preflight); substrate continues to tick endogenously (no operator input — per Section 11 decision 1) for as long as operator chooses to observe (per Section 11 decision 2). Operator decides "seen what I wanted" → Ctrl+C → clean shutdown.

---

## 9. Deliverables

| # | Deliverable | Location |
|---|---|---|
| 1 | This spec (committed) | `docs/architecture/V2_LIVE_BOOT_INTEGRATION_DESIGN.md` |
| 2 | Audit findings doc | `docs/_archive/architecture/V2_LIVE_BOOT_AUDIT.md` |
| 3 | Operator runbook | `docs/_archive/architecture/V2_LIVE_BOOT_RUNBOOK.md` |
| 4 | Any cleanup commits from audit | individual commits, cure-runway discipline |
| 5 | Quarantine-lift commit | one commit + memory entry `project_v2_live_boot_authorized.md` |
| 6 | Architecture README updated to link the new docs | `docs/architecture/README.md` |
| 7 | AGENTS.md Active Work entry + Communication Log | `AGENTS.md` |

---

## 10. Estimated Effort

If boot.py is clean (which Phase 2 should have left it):
- 2-3 commits total (audit doc, runbook, lift)
- ~2-3 hours of focused work

If audit finds legacy creep:
- Each finding = its own slice with cure-runway discipline
- ~4-8 hours depending on count + complexity

If HUD-skip path or terminal logging has gaps:
- Each gap = its own fix commit
- Add ~30 minutes per fix

---

## 11. Operator Decisions (Locked)

1. **Input mode: observe-only.** No operator input channel wired for first live boot. The substrate runs endogenously — builds pressure from internal sources (curiosity_gap, epistemic_emptiness, neuromod_spike, etc.), opens episodes, runs movement chain, archives, idles. Operator framing: *"i just want to observe for now, it doesnt need the operator to function. it can build pressure and all that itself."* This is the cleanest first-boot proof: substrate sustains itself with zero external input.
2. **Duration target: operator-decided.** No fixed end time. Operator runs boot.py and watches the terminal until they've seen what they want to see; then Ctrl+C. Runbook says "run until satisfied."
3. **Quarantine-lift is symbolic + doc-only.** Verified `core/policy/symphony.yaml` has no `quarantine` / `live_boot_authorized` / `unattended` flag; `conductor_mode: symphony` and `conductor_wired: true` are already set. The quarantine was operator discipline ("don't run live until Phase 2 closes"), not a code gate. Lift = memory entry + audit note recording the authorization. No yaml flip needed.

---

## 12. Handoff

After operator approves this spec:
1. Invoke `writing-plans` skill to produce step-by-step implementation plan
2. Plan executes the work in Section 5 in dependency order
3. Each commit lands with cure-runway discipline (implementer self-probe → compliance Gemini → blind-adversarial Gemini → independent code-reviewer agent post-PR)
4. Final commit is the quarantine-lift, blocking on all prior work clean

---

*End of spec.*

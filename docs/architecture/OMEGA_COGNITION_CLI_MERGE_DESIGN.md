# Omega Cognition + CLI Merge — Design Spec

> **Status:** HISTORICAL DESIGN SPEC. Its APPLY/qwen Python bridge path was superseded
> by `auto_loop/execute/brain_skill_executor.py` and `runtime.client`. The TypeScript
> CLI/build limb remains separate. This document is retained as migration evidence,
> not as the live routing contract.
>
> **Provenance.** The original of this spec was committed as `bdca903e` on 2026-06-15
> and lost when the F: SSD corrupted mid-build before it was pushed. This is a
> faithful **reconstruction** from the project-memory record
> (`project_cognition_cli_dev_loop_merge.md` + the Phase 0b/0c recovery notes), not
> the verbatim original. The architecture, the A/B decision, the critic verdicts, and
> the build sequence are recovered; prose wording differs. Treat as canonical going
> forward and amend in place.
>
> **Status (2026-06-15):** Phase 0 COMPLETE — 0a `f61e959a` (domain-key), 0b `91c1b83b`
> (jail), 0c `98eeb192` (checkpoint). **Phases 1–4 (the APPLY mechanism) COMPLETE:** S1
> intent/action `c984766e`, S2 AUTO→bridge dispatch `02f0ba77` (**live-validated** — a
> real qwen run wrote a file end-to-end through the bridge), S3 fold-back grounds
> `application_mastery` `aed65fce`, S4 recall-before-apply `7a5c8f72`. Bridge moved off
> the portable-lite NPU collision (`:8091`→`:8092`, `3e2e5a1a`). Firing decision
> RESOLVED — **staged**: operator approves the first N APPLYs, then graduates to
> autonomous (see §10). **Phase 5a DONE** (`eda10822`): the `execution_gap` pressure
> contributor (learned-but-unapplied ∧ memory-hot, disjoint from `learning_debt`) is
> wired but DORMANT (`EXECUTION_GAP_ENABLED=False`); and the AUTO APPLY executor now
> STRUCTURALLY fail-closes (`apply_unsafe`) until `auto_loop/execute/apply_guards.py`
> (apply_lock + pre-APPLY checkpoint) exists — so APPLY cannot run unguarded.
> **Phase 5x DONE (the safety half — UNBLOCKS the executor):** `auto_loop/execute/
> apply_guards.py` now exists (`GUARDS_WIRED=True` → `_apply_guards_ready()` True) — the
> single chokepoint every APPLY passes through: a separate `apply_lock` (mirrors
> cognition_lock's RLock but a DISTINCT object, so a long APPLY never freezes cognition);
> the staged-approval gate (operator grants one-shot tokens out of band; graduates to
> autonomous after N=3 SUCCESSFUL supervised APPLYs; persisted in `guardian/state/
> apply_authorization.json`, durably audited to `guardian/logs/apply_approvals.jsonl`,
> fail-closed + clamped on corruption; revoke = hard re-stage); a hard wall-clock
> deadline (brain-side backstop — the qwen subprocess tree is the bridge's to kill,
> Phase 6); and mandatory checkpoint-before / restore-on-failure (token consumed ONLY
> after a successful checkpoint; restore failure surfaced LOUDLY as
> `apply_restore_failed`). The gate file is EXCLUDED from the checkpoint surface so a
> mind-restore never resurrects a spent token. A 5-lens adversarial review (concurrency /
> gate-bypass / reversibility / doctrine / integration) drove the hardening; its BLOCKER —
> a staged-gate hold (`pending_approval`) grounded as a *failed* APPLY that LOWERS
> `application_mastery` — is fixed two ways: the omega fold-back emits NO mastery effect
> for non-executed statuses (`pending_approval`/`blocked`/`none`), and the loop classifies
> the hold as a `skipped` cycle (neutral for neuromod / mastery / failure-analysis).
> `execution_gap` stays DORMANT — activation waits on live validation, NOT 5x.
> **Phase 5b DONE** (`b93e9745`) — the practice bar is now PER-DOMAIN: each
> learned-but-unapplied domain is gated on ITS OWN hot memory count via
> `memory.symphony_strength.hot_count_for_domain` (coord-index `domain` × StrengthIndex
> `is_hot`), replacing the 5a global-aggregate bar that could ignite an APPLY for a cold
> domain off an unrelated hot one. `execution_gap` still DORMANT (`EXECUTION_GAP_ENABLED=False`).
> **Pre-live review DONE** — full 7-lens adversarial review of the APPLY mechanism
> (Phases 0–5b); one blocking silent-failure fixed (`e4695d69`: the bridge client now fails
> closed when the qwen stream ends without a `done` event, so a truncated run is never
> grounded as success), fail-closed contracts pinned (`6adcb851`).
> **Live validation DONE (2026-06-16)** — bridge round-trip + production client (`done`-grounded)
> + the FULL guard chain end-to-end: no-token APPLY HELD (no checkpoint, no dispatch); operator
> grant → checkpoint-before-dispatch → token consumed only after checkpoint → real qwen run in
> the `F:\Omega3.0` jail → `done`/ok grounding → graduation 0→1 → checkpoint `verify`-intact →
> mind untouched. Gate then hard-re-staged to `0/0`. The qwen jail root is the LIVE repo (git
> rolls back tracked code; mind-checkpoint covers gitignored mind state).
> **APPLY ARMED DONE (2026-06-16)** — `EXECUTION_GAP_ENABLED` flipped `True`: the cognition
> cascade now AUTONOMOUSLY OPENS an APPLY episode when a domain is learned-but-unapplied AND
> own-hot (`execution_gap` → intent `APPLY` → action `APPLY` → the single guarded handler).
> Pre-flip 3-agent adversarial verification: (a) bypass-hunt — no autonomous path to real code
> execution outside `run_guarded_apply` (the only dispatch; `cognition_chain.py:557` passes no
> `apply_invoker`, forcing the default guarded path); (b) side-effects — the flag's only durable
> consequences are (i) an APPLY episode may open, gated at `0/0` to `pending_approval`, and the
> staged gate GRADUATES to autonomous dispatch only after `STAGED_APPROVAL_N` operator-approved
> APPLYs (by design — the chosen firing policy), and (ii) a benign winner-margin interaction: a
> non-zero `execution_gap` can be a runner-up that triggers `ambiguous_winner` suppression of a
> near-tied `learning_debt` ignition for one tick (fail-safe, never corrupting); (c) test-impact —
> exactly one test broke (`test_execution_gap_dormant_until_enabled`), rewritten TDD into a
> default-ON contract test + a preserved kill-switch test (force-`False` → inert). Full suite
> 1512✓ / 0 failed, ruff clean. Defense-in-depth note: the TS bridge `:8092` has no auth of its
> own — `run_guarded_apply` is the SOLE brain-side gate; HUD dev-pill + `verify_apply_bridge.py`
> reach the bridge directly but are human-initiated, never autonomous.
> **Phase 6 in progress** — self-mod denylist DONE (§8.8) + process-tree kill DONE (bridge) +
> skills-as-capabilities CATALOG DONE (seam 9: `runtime/qwen_skills.py` hud-free loader +
> `memory/capability_catalog.py` ingests the 33 bundled executor skills into lobe 5 at boot,
> idempotently) + skills-as-capabilities APPLICATION DONE (the meta tool phase now has an APPLY
> branch that selects the matching capability for the gap domain via
> `select_capability_for_domain` and composes a directive NAMING the skill — it reaches qwen
> through the existing directive→compose_apply_prompt path; no guard/invoker change). **Seam 9
> COMPLETE** — incl. STEP-4 capability fold-back: a successful APPLY that used a capability
> reinforces its lobe-5 leaf strength (`reinforce_capability`, commit `174e5ecd`; the seam-9
> selection→STEP-4 join is e2e-tested). **Phase 6 COMPLETE** — §11 gemma↔qwen dev-pill hotswap
> DONE (`9640f15b`, live-validated 2026-06-17): a real APPLY swaps gemma OUT / qwen IN around the
> guarded dispatch and ALWAYS releases (finally) so cognition reclaims the GPU; `model_control.py`
> + the capability machinery are now self-mod-denylisted. **The full act-loop is wired,
> end-to-end tested, and live-validated.**

## 1. Problem — `application_mastery` cannot rise

Omega's only autonomous action is `web_fetch` (READ). The live mastery model is
**grounded/absolute** (each episode emits absolute `knowledge_mastery` +
`application_mastery`; `deepsleep_loop/mastery/run.py:_apply_grounded_mastery`; there
is **no** free counter — the older free-counter docs are stale). `application_mastery`
therefore stays `0.0` because no episode ever gives Omega evidence it *applied*
knowledge. A domain with `knowledge >= target` but `application = 0` becomes an
**unclosable** `mastery_gap` (`resolve_topic.py:217 gap = max(knowledge_gap,
application_gap)`) that perpetually re-selects the same LEARN/`web_fetch` — the
"python keeps repeating" bug. The fix is **behavioral, not arithmetic**: give Omega
hands.

## 2. The hands — `omega_cli_ts`

`F:\Omega3.0\omega_cli_ts` is an operator-built Bun WS bridge
(`ws://127.0.0.1:8092/ws/cli`, `src/server.ts`) that loads a vendored qwen `AgentCore`
state machine from `F:\Omega3.0\qwen-agent-tui` (env `QWEN_AGENT_TUI_ROOT`), driving
`qwen3-coder` over `QWEN_BASE_URL=http://127.0.0.1:11434/v1` — the **same** local
runtime that serves cognition's gemma (one runtime, two model workers). It has tools
(`execute_command`, file ops, todos), 22 skills, a keep/rollback ledger, and is today
human-driven via the HUD dev pill.

## 3. Architecture — APPLY downstream of the deterministic wall

A **new episode type, `APPLY`**, dispatches into the qwen dev loop instead of calling
a premade tool. Two-brain mind:

- **gemma cascade = deliberative THINKING** — intent, grounding, restraint (upstream).
- **qwen-coder = the HANDS** — multi-round dev execution (downstream).

The merge **seam is the deterministic tool-call layer** (AUTO = zero-inference
executor; see `project_deterministic_auto_omega_cognition_cascade`). Restraint lives
**upstream** of the wall (ignition gate / neuromod deadlock+decay / trajectory) so a
powerful downstream executor never threatens the validated restraint. **The wall:**
signals → pressure → ignition → action; the body steers DRIVE only, never pulls the
trigger; the merge adds power strictly downstream of the wall.

### 3.1 A/B decision — **B**
- **A:** qwen is the universal AUTO executor, loaded every episode.
- **B (CHOSEN):** qwen executes **APPLY only**; LEARN `web_fetch` stays direct/
  deterministic. qwen = executor only, loads on execute, unloads for the 10-window
  cascade (load-on-execute granularity "for now").

## 4. Seam inventory (map "to a T")

1. **dispatch** — cognition → bridge WS run on `:8091`.
2. **executor** — AUTO deterministic execute → qwen dispatch for APPLY.
3. **outcome** — qwen run-complete + ledger + git diff → episode outcome.
4. **grounding** — outcome → Omega cognition → absolute `application_mastery`.
5. **recall-before-apply** — APPLY recalls domain knowledge first.
6. **topic/pressure** — `application_gap` routes to APPLY, not LEARN.
7. **restraint/safety** — ignition + neuromod + Guardian gate whether/when APPLY fires.
8. **lifecycle/runtime** — `omega_cli_launch`; gemma + qwen coexist on `:11434`.
9. **skills** — the 22 qwen skills become Omega's applied capabilities.

## 5. Pressure model (LOCKED 2026-06-14)

Signal-governed, **not** tick-driven (probe-every-tick removed; the mind rests when
there is no real signal). Base inputs — the only things that move pressure:
1. neuro levels / neuromod
2. meta tuning
3. operator input
4. **learning_debt** — the sole learning channel (absorbs knowledge/standalone
   `mastery_gap`; `epistemic_emptiness` removed)
5. **execution** — the new apply channel

`trajectory` is **not** a base input — it is a *multiplier* (setpoint; see
`project_trajectory_decision_model_setpoint`). Removed from pressure:
`epistemic_emptiness`, `application_gap` (replaced by the execution signal —
application is **earned by doing**, never a nagging gap → dissolves the repeating-LEARN
bug), standalone `mastery_gap`, idle-probe. Moved out: identity → DeepSleep
consolidation.

**Practice bar = MEMORY-WEIGHTED.** Execution readiness is not a fixed
`knowledge >= target` threshold; it is gated by the domain's **memory hotness** via the
existing biological strength-index/stress/neuromod system. Each `CanonicalEnvelope`
carries `strength_index` (`memory/strength_index.py`); `memory/api.py` `decay_tick`
cools it, recall reconsolidates it (forgetting curve). A domain becomes practice-ready
when its engrams are **hot** enough, modulated by neuromod/stress
(`pressure_loop/pressure_field.py`, `policy.py`). Self-regulating: learn-but-don't-apply
→ memories cool → execution fades (use-it-or-lose-it); apply → reconsolidates → stays
hot → application climbs. **Phase 5b** makes this bar PER-DOMAIN: `read_execution_gap`
gates each learned-but-unapplied domain on `hot_count_for_domain(domain)` — its OWN
engrams' hotness (coord-index `domain` × StrengthIndex `is_hot`) — so a hot `python`
substrate can never ignite an APPLY for a cold `rust` domain (the 5a global-aggregate
bar's flaw). The weight scales with the count of *self-hot* practice-ready domains.

## 6. Autonomic doctrine (KEPT)

Autonomic is **observational-only**: it does NOT gate execution, meta NEVER interprets
a signal, only GLOBAL reflects on trends. It already modulates **pressure** via the
neuromod spike (`pressure_field`: dopamine→vigor↑, serotonin→patience damp) and
`cognitive_churn` attenuates `learning_debt` on habituated domains. ⇒ the
memory-weighted practice bar lives in the **pressure** layer (execution contributor
scaled by memory hotness + neuromod spike), **not** as a meta/execution stress-gate —
the existing allowed pattern, keeps doctrine intact. Live computation is consolidated in
`autonomic/symphony_neuromod.py` (+ `cognitive_churn.py`, `temporal_grounding`/
`phase_clock`, `_history.py`). ⚠️ `autonomic/{dopamine,plasticity,stress,strength_index}/`
subdirs are README-only ghosts — build against `symphony_neuromod.py`.

## 7. Critic verdicts (10-agent synthesis: 6 seam-mappers → architect → 3 critics)

| Lens | Verdict | Score |
|------|---------|-------|
| Feasibility | ship-with-changes | 0.78 |
| Doctrine | ship-with-changes | 0.78 |
| **Safety** | **REWORK** | **0.82** |

### 7.1 The safety rework — "git = reversibility" is FALSE
The **entire live mind is gitignored** (guardian/state mastery+trajectory, deepsleep/
pressure state, all `memory/` lobes incl. identity seeds), so the git-status keep/
rollback ledger is structurally **blind** → an autonomous APPLY can permanently corrupt
memory/identity, unrollbackable. Plus:
- the 900s qwen timeout cannot interrupt a blocking synchronous `execSync` holding the
  process-wide `cognition_lock` RLock (freezes ALL cognition + the operator's kill
  channel) → use a separate **`apply_lock`** + independent process-tree-kill timeout.
- the workspace **jail did not exist** (`qwen-agent-tui/src/tools/index.ts` `safe()`
  ignored `allowedPaths`; abs/`..` escaped; Unix denylist bypassed because `rm`→
  `Remove-Item -Recurse -Force` on PowerShell). RED proof: `Format-Volume -DriveLetter F`
  actually executed.

### 7.2 Feasibility blockers
- **domain-key mismatch** — coding lobe wrote `<lang>_systems` unslugged vs the
  hyphen-slug mastery key → hotness=0 → APPLY dead silently.
- **gemma-reload-not-ensured** — must call `ensure_cognition_runtime` after qwen release.
- **grounding default-to-prior**.

## 8. Safety prerequisites (build before any real qwen APPLY)

- **§8.1 — Reversibility checkpoint.** Snapshot/restore the gitignored mind state
  before APPLY. **DONE (Phase 0c, `98eeb192`):** `core/mind_state_surface.py` +
  `guardian/mind_checkpoint.py`.
- **§8.2c / §8.3 — Workspace jail + PowerShell-aware denylist.** **DONE (Phase 0b,
  `91c1b83b`):** `safe()` confines to `jailRoots`, `change_workspace` jailed,
  `translateUnixToPowerShell` + denylist blocks drive-root deletes / disk-format ops,
  `clampTimeoutSeconds`.
- **§8.4 — `apply_lock` + hard deadline** (NOT `cognition_lock`). **DONE (Phase 5x):**
  `auto_loop/execute/apply_guards.py` — separate `apply_lock`, staged-approval gate,
  hard wall-clock deadline, checkpoint-before / restore-on-failure. (True process-tree
  kill of the qwen run stays with the bridge — Phase 6.)
- **§8.8 — Self-mod denylist.** **DONE (Phase 6, 2026-06-16):**
  `auto_loop/execute/self_mod_denylist.py` — `is_protected_path` / `self_mod_violations`
  classify an APPLY's returned ledger against the mind-state dirs (reused from
  `core.mind_state_surface.MIND_STATE_DIRS`) + the safety-critical code files (the guard,
  AUTO dispatcher, mind-checkpoint, mind-state surface, bridge client, and the denylist
  itself). `run_guarded_apply` forces any violating run to a HARD reverted failure that does
  NOT graduate, and hard re-stages the gate (`revoke_apply_approvals`) — the gate is excluded
  from the checkpoint, so a self-granted token/graduation would otherwise survive the mind
  restore. Fails SAFE (unparseable / out-of-jail path = violation). Brain-side enforcement on
  the returned ledger; prevention-at-write (bridge refusing the write) remains the
  complementary bridge-side layer.

## 9. Build sequence (safety-FIRST)

- **Phase 0a — domain-key reconciliation.** DONE `be3ff9b6`→replayed `f61e959a`.
  `memory/coding_project.py` engram `domain` routes through `_canonical_domain` =
  `normalize_domain_id` slug; the `_systems` key still drives the physical coord lookup.
- **Phase 0b — qwen-agent-tui jail.** DONE `91c1b83b`. (§8.2c/§8.3)
- **Phase 0c — reversibility checkpoint.** DONE `98eeb192`. (§8.1)
- **Phase 1–4** — APPLY episode type + dispatch (seam 1–2), outcome capture (seam 3),
  grounding to absolute `application_mastery` (seam 4), recall-before-apply (seam 5).
- **Phase 5** — pressure routing `application_gap`→APPLY (seam 6) + restraint/`apply_lock`
  gating (seam 7, §8.4); wire `checkpoint→APPLY` / `restore→failure`. **5a DONE**
  (`eda10822`, execution_gap dormant + structural fail-closed). **5x DONE** (apply_guards:
  apply_lock + staged-approval gate + hard deadline + checkpoint/restore, gate file off the
  checkpoint surface, gate-hold grounded neutrally). **5b DONE** (`b93e9745`, per-domain
  hotness: `memory.symphony_strength.hot_count_for_domain` + `read_execution_gap` gates each
  candidate domain on its own warmth; `_application_gap_domains` is the candidate set).
  **APPLY ARMED DONE** (2026-06-16) — `EXECUTION_GAP_ENABLED=True`: the cascade now autonomously
  opens APPLY episodes; the staged-approval guard (not the flag) is the execution boundary.
  Pre-flip adversarial verify (bypass / side-effects / test-impact) clean; full suite 1512✓.
- **Phase 6** — lifecycle/runtime (seam 8), skills-as-capabilities (seam 9), self-mod
  denylist (§8.8). **Self-mod denylist DONE** (2026-06-16, `self_mod_denylist.py` + wired into
  `run_guarded_apply`; 27 new tests, full suite 1539✓). **Process-tree kill DONE** (2026-06-16,
  bridge-side: `qwen-agent-tui/src/tools/proc.ts` `killProcessTree` [Windows `taskkill /T /F`;
  POSIX direct-child, group-kill v1.1-deferred] + async tree-killable `execute_command`/`bash`
  `executeAsync` that honors the abort signal + timeout, with a drain-gate hard ceiling +
  PID-reuse guard; `omega_cli_ts/src/server.ts` aborts the run on owner/last-client WS
  disconnect so a brain-side deadline reaps the qwen command tree; adversarial review clean,
  70 bridge tests ✓). **Skills-as-capabilities CATALOG DONE** (2026-06-16, seam 9: hud-free
  `runtime/qwen_skills.py` loader + `memory/capability_catalog.py` ingests the 33 repo-bundled
  executor skills into lobe 5 [dev scope, deterministic ids → idempotent re-sync] as a boot
  step; 12 new tests, full suite 1551✓). **Skills-as-capabilities APPLICATION DONE** (2026-06-16:
  `memory.capability_catalog.select_capability_for_domain` [weighted name/tag token match] +
  the meta tool-phase `intent=="APPLY"` branch composes a capability-aware directive — fixing
  the prior capability-blind path where execution_inputs stayed {} and the directive degraded
  to a bare domain name; +9 tests, full suite 1560✓). **Seam 9 COMPLETE.** Remaining for
  Phase 6: gemma↔qwen lifecycle (GPU).

## 10. Open questions

- **First-N APPLY policy:** staged-with-approval vs full-autonomous for the first APPLY
  episodes (needed before Phase 5/6; UNRESOLVED).
- qwen load/unload granularity ("load-on-execute for now until we get a feel").

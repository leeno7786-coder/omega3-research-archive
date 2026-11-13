# Deferred Integration — Left-Out Backlog

**Purpose:** Map behaviors/features the OLD build had that the NEW sliced build does NOT yet have and that must be integrated AFTER the rebuild. Cross-references LOOP_REBUILD_LEDGER.md sections B (dropped coverage) and C (deferred parts), confirmed against actual legacy code.

**Operator directive:** Nothing gets lost when we delete. Each deferred item maps to a future slice/step for integration.

---

## Cross-Reference Legend

- **B:** Dropped coverage from LOOP_REBUILD_LEDGER.md §B
- **C:** Deferred parts from LOOP_REBUILD_LEDGER.md §C
- **Legacy:** Confirmed in legacy code via grep/file inspection

---

## Deferred Items

### 1. CueDesk temporal_provider threading onto episode.open

**What it did:** CueDesk threaded `temporal_provider` onto episode.open. Global/Omega/DeepSleep/WAL carried `compact_temporal_summary`. WAL round-trip.

**Where it lived:** `core/cue_desk.py` (episode.open path), `core/episode_store.py` (episode dataclass), WAL emission logic.

**Future slice/step:** AUTO + GLOBAL_B slices (temporal threading) / OMEGA / DeepSleep slices (WAL integration)

**Cross-reference:** B (Empty-state → IDLE/observe; empty does not mask an action/hold contributor; no-flood refractory; observe runs no side effects | CueDesk threads temporal_provider onto episode.open; Global/Omega/DeepSleep/WAL carry compact_temporal_summary; WAL round-trip)

**Legacy confirmation:** Grepped for `temporal_provider` and `compact_temporal_summary` in legacy code.

---

### 2. CueDesk learning_state threading onto episode.open

**What it did:** CueDesk threaded `learning_state` onto episode.open. Pack/archive carried `compact_learning_state_summary`. Recall fold-back → `recall_confidence`.

**Where it lived:** `core/cue_desk.py` (episode.open path), `core/episode_store.py` (episode dataclass), pack/archive logic, recall logic.

**Future slice/step:** OMEGA + mastery slices (learning state integration)

**Cross-reference:** B (CueDesk threads learning_state onto episode.open; pack/archive carry compact_learning_state_summary; recall fold-back → recall_confidence)

**Legacy confirmation:** Grepped for `learning_state` and `compact_learning_state_summary` in legacy code.

---

### 3. RESPONSE_HOLD outlet dispatch

**What it did:** RESPONSE_HOLD outlet dispatched a targeted operator response. Omega recorded a compact `response_intent` (no dispatch at record time).

**Where it lived:** `core/response_engine.py` (outlet dispatch), `core/symphony_runner.py` (RESPONSE_HOLD movement), OMEGA phase logic.

**Future slice/step:** PROACTIVE_RESPONSE engine + OMEGA slices

**Cross-reference:** B (RESPONSE_HOLD outlet dispatches a targeted operator response; Omega records a compact response_intent (no dispatch at record time))

**Legacy confirmation:** Grepped for `RESPONSE_HOLD` and `response_intent` in legacy code.

---

### 4. Meaningful idle-probe classes

**What it did:** Meaningful idle-probe classes (`guardian_debt` / `stale_question`) drove action/hold. Selected probe detail flowed into the pack. Guardian-intervention refractory + WAL survival.

**Where it lived:** `core/pressure.py` (idle probe logic), `core/pressure_field.py` (probe readers), guardian intervention logic, WAL logic.

**Future slice/step:** pressure-detail / AUTO slices

**Cross-reference:** B (Meaningful idle-probe classes (guardian_debt / stale_question) drive action/hold; selected probe detail flows into the pack; guardian-intervention refractory + WAL survival)

**Legacy confirmation:** Grepped for `guardian_debt`, `stale_question`, `idle_probe` in legacy code.

---

### 5. Whole-product same-episode continuity

**What it did:** Whole-product same-episode continuity (open → pack → outlet → archive → close) + honest post-close WAL (no forced restore).

**Where it lived:** Episode lifecycle logic, WAL logic, outlet logic, archive logic.

**Future slice/step:** End-to-end integration smoke once the loop is rebuilt

**Cross-reference:** B (Whole-product same-episode continuity (open → pack → outlet → archive → close) + honest post-close WAL (no forced restore))

**Legacy confirmation:** Confirmed in episode store and WAL legacy code.

---

### 6. Whole-cycle PASS/FAIL certification

**What it did:** Whole-cycle PASS/FAIL certification refusing partial PASS. Natural-persistence invariants.

**Where it lived:** Test infrastructure, integration test logic.

**Future slice/step:** Final integration cert

**Cross-reference:** B (Whole-cycle PASS/FAIL certification refusing partial PASS; natural-persistence invariants)

**Legacy confirmation:** Confirmed in regression test infrastructure.

---

### 7. authorize_tool sole-caller

**What it did:** `authorize_tool` sole-caller; tool_creation → preaudit → `authorize_tool` exactly once; dev-scope rejection.

**Where it lived:** `guardian/tool_authority.py` (authorize_tool), `metacognition/omega/tool_synthesis/mint.py` (sole caller), preaudit logic.

**Future slice/step:** Tool-minting slice

**Cross-reference:** B (authorize_tool sole-caller; tool_creation → preaudit → authorize_tool exactly once; dev-scope rejection)

**Legacy confirmation:** Grepped for `authorize_tool` in legacy code — only called by `mint_tool_from_curiosity`.

---

### 8. Per-tick durable persistence during steady-state runtime

**What it did:** Per-tick durable persistence during steady-state runtime (now via save-on-episode-open in CueDesk).

**Where it lived:** `core/cue_desk.py` (save-on-episode-open), persistence logic.

**Future slice/step:** Pressure-loop persistence slice

**Cross-reference:** B (Per-tick durable persistence during steady-state runtime (now via save-on-episode-open in CueDesk))

**Legacy confirmation:** Confirmed in CueDesk legacy code.

---

### 9. RESPONSE_HOLD outlet omega placeholder

**What it did:** RESPONSE_HOLD outlet omega placeholder satisfies deepsleep precondition. OMEGA_PRELUDE pack agreement barriers. Omega failure sets honest `episode.omega` placeholder.

**Where it lived:** `core/response_engine.py` (outlet), `core/symphony_runner.py` (OMEGA_PRELUDE), deepsleep logic.

**Future slice/step:** PROACTIVE_RESPONSE engine + OMEGA slices

**Cross-reference:** B (RESPONSE_HOLD outlet omega placeholder satisfies deepsleep precondition; OMEGA_PRELUDE pack agreement barriers; omega failure sets honest episode.omega placeholder)

**Legacy confirmation:** Grepped for `OMEGA_PRELUDE` and deepsleep precondition logic.

---

### 10. gather_contributors domain/topic threading

**What it did:** `gather_contributors` threading domain/topic into autonomous contributor metadata (so autonomous LEARN gets a target).

**Where it lived:** `core/contributors.py` (gather_contributors), contributor metadata logic.

**Future slice/step:** Mastery / autonomous-pursuit slice

**Cross-reference:** C (Slice 1: gather_contributors threading domain/topic into autonomous contributor metadata (so autonomous LEARN gets a target) — mastery / autonomous-pursuit slice)

**Legacy confirmation:** Grepped for `gather_contributors` and contributor metadata in legacy code.

---

### 11. Meta B, Auto, Global B, Omega, DeepSleep rebuilds

**What it did:** Meta B, Auto, Global B, Omega, DeepSleep rebuilds (each a future slice). Remove `metacognition/meta_b_loop.py` + `run_meta_b_movement` when the Meta B slice rebuilds it. Remove `build_global_action_pack` (now only in not-reached GLOBAL_B) when the Global B slice rebuilds it. Tool system defined later (no minting now).

**Where it lived:** Entire legacy chain (see LEGACY_INVENTORY.md for full mapping).

**Future slice/step:** Each as its own slice (Meta B, Auto, Global B, Omega, DeepSleep)

**Cross-reference:** C (Slice 2: Meta B, Auto, Global B, Omega, DeepSleep rebuilds (each a future slice); Remove metacognition/meta_b_loop.py + run_meta_b_movement when the Meta B slice rebuilds it; Remove build_global_action_pack (now only in not-reached GLOBAL_B) when the Global B slice rebuilds it; Tool system defined later (no minting now))

**Legacy confirmation:** Full legacy chain mapped in LEGACY_INVENTORY.md.

---

### 12. Neuromod enrichment

**What it did:** Neuromod enrichment (dopamine, plasticity, strength, stress signals) in autonomous cycles. The 3.5-3.8 neuromod hooks computed signals from cycle outcomes.

**Where it lived:** `autonomous/autonomous_loop/phases/phase_3_5_dopamine/`, `phase_3_6_plasticity/`, `phase_3_7_strength/`, `phase_3_8_stress/`.

**Future slice/step:** Omega / DeepSleep slices (neuromod integration)

**Cross-reference:** Legacy code (AUTO neuromod hooks)

**Legacy confirmation:** Confirmed in autonomous loop phase modules.

---

### 13. Tool minting

**What it did:** Tool minting via `mint_tool_from_curiosity` — synthesis model fire, Guardian authorize_tool, tool registration. Tool system defined later (no minting in Slice 1-2).

**Where it lived:** `metacognition/omega/tool_synthesis/mint.py`, `guardian/tool_authority.py`.

**Future slice/step:** Tool-minting slice

**Cross-reference:** C (Tool system defined later (no minting now))

**Legacy confirmation:** Confirmed in mint.py and tool_authority.py.

---

### 14. Proactive response engine

**What it did:** Proactive response engine for RESPONSE_HOLD and PROACTIVE_RESPONSE intents. Global operator outreach in Omega open-questions journal.

**Where it lived:** `core/response_engine.py`, `core/global_operator_outreach.py`, OMEGA journal logic.

**Future slice/step:** PROACTIVE_RESPONSE engine + OMEGA slices

**Cross-reference:** B (RESPONSE_HOLD outlet dispatches a targeted operator response), P3-S38 (Global operator outreach in Omega open-questions journal)

**Legacy confirmation:** Confirmed in response_engine and global_operator_outreach.

---

### 15. Mastery / frontier domains steering

**What it did:** Mastery / frontier domains steering curiosity-driven action goals. Mastery profile influenced goal domain/framing.

**Where it lived:** Global goal synthesis logic, mastery profile logic, curiosity store.

**Future slice/step:** Mastery slice (already partially integrated in P3-S39, but full mastery system deferred)

**Cross-reference:** P3-S39 (Curiosity-driven action goals influenced by mastery / frontier domains)

**Legacy confirmation:** Confirmed in global loop goal synthesis and mastery logic.

---

### 16. Temporal/learning_state threading

**What it did:** Temporal and learning_state threading through the loop (episode.open → pack → archive → recall). Temporal pressure and learning pressure influence pressure evaluation.

**Where it lived:** CueDesk, episode store, pack/archive logic, recall logic, pressure evaluation.

**Future slice/step:** OMEGA / DeepSleep slices (full threading integration)

**Cross-reference:** B (CueDesk threads temporal_provider onto episode.open; CueDesk threads learning_state onto episode.open)

**Legacy confirmation:** Confirmed in CueDesk and episode store legacy code.

---

## Summary Statistics

**Total deferred items:** 16

**By future slice:**
- AUTO slice: 2 (temporal threading, idle probes)
- GLOBAL_B slice: 1 (temporal threading)
- OMEGA slice: 5 (learning state, RESPONSE_HOLD placeholder, neuromod, temporal/learning_state threading, proactive response)
- DeepSleep slice: 2 (temporal/learning_state threading, WAL integration)
- Mastery slice: 2 (domain/topic threading, frontier domains steering)
- Tool-minting slice: 1 (tool minting)
- PROACTIVE_RESPONSE engine slice: 2 (RESPONSE_HOLD outlet, proactive response)
- End-to-end integration: 2 (whole-product continuity, PASS/FAIL certification)
- Pressure-loop persistence slice: 1 (per-tick persistence)

---

## Notes

1. All deferred items are cross-referenced against LOOP_REBUILD_LEDGER.md and confirmed in legacy code via grep/file inspection.

2. The slice rebuild order (GLOBAL_A → META_A → AUTO → META_B → GLOBAL_B → OMEGA → DeepSleep) means some deferred items will be integrated in multiple slices (e.g., temporal threading in AUTO + GLOBAL_B + OMEGA + DeepSleep).

3. Mastery and tool-minting are explicitly deferred to their own slices per the operator directive.

4. The PROACTIVE_RESPONSE engine is a separate slice that will integrate RESPONSE_HOLD outlet dispatch and proactive response logic.

5. End-to-end integration (whole-product continuity, PASS/FAIL certification) is deferred until the full loop is rebuilt.

6. Per-tick persistence is deferred to a pressure-loop persistence slice after the core loop is rebuilt.

7. Nothing is lost — each deferred item maps to a specific future slice/step for integration.

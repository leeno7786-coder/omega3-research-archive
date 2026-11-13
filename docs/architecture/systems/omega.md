# System: Omega loop (conductor / metacognition)

> **Agent anchor** · Package: `metacognition/omega/` · Full detail: `metacognition/omega/README.md`

## What it is

**Highest metacognition loop** — evaluates system health, proposes policy/tool changes, commits trajectory, runs tool synthesis. **Only loop** that directly writes identity lobe (by operator design).

## Phases (today — one contiguous run)

| Phase | Role |
|-------|------|
| 1 intake | Journal + state collector + upstream deepsleep envelope |
| 2 evaluate | Health / degradation signals |
| 3 propose | Proposals, tool-needs, trajectory candidates |
| 4 commit | Trajectory write, tool synthesis, operator queue, approvals |

## What it is expected to do

- **Metacognate** on consolidated epoch evidence.
- Maintain **multi-epoch trajectory** (`trajectory_store` → `LAST_OMEGA_TRAJECTORY`).
- Propose tools/policy; route risky changes through operator approval.
- Shape **own identity** at memory coord `[0,6]` (sanctioned direct write).

## Production truth (miswire)

- Boot order: runs **after deepsleep**, not after global in same cognitive pass.
- Reads `LAST_DEEPSLEEP_CONSOLIDATION` (includes nested upstream) — not a live episode object.
- Long-term aggregation: `state_collector` + `deepsleep_store` journal.

## Symphony target

**Movement `OMEGA`**: full phases after `GLOBAL_ACTED` on complete `CognitiveEpisode`. Then `EPISODE_COMPLETE` → sleep.

Guidance.trajectory is the **baton** all lower layers read next episode.

## Writes / reads

| Writes | Reads |
|--------|-------|
| `LAST_OMEGA_EVALUATION`, `LAST_OMEGA_PROPOSALS`, trajectory, identity lobe | Deepsleep envelope, journal, goals file (intake) |

## Do not break

- Trajectory lifecycle (decrement, supersede) in phase_4 / trajectory_store.
- Tool synthesis authority (Omega-only).
- Operator approval queue for policy proposals.

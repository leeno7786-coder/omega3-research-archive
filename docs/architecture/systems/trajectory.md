# System: Trajectory (persistent baton)

> **Agent anchor** · `metacognition/omega/trajectory_store.py` + `LAST_OMEGA_TRAJECTORY`

## What it is

**Multi-epoch directional pressure** from Omega — domain/capability/intent over several cycles. The **only** canonical SharedState slot intentionally **not cleared** by deepsleep cascade reset.

## What it is expected to do

- Bias global goal selection (`trajectory_attempt_outcome` telemetry).
- Snapshot into deepsleep journal as `omega_trajectory` for long-term omega intake.
- Decrement / complete / supersede via omega phase_4 commit.

## Writers / readers

| Writer | Reader |
|--------|--------|
| `trajectory_store.write`, omega commit | global adapter, deepsleep snapshot, omega phases |

## Symphony target

**`Guidance.trajectory`** — mandatory read for META_A, GLOBAL_A, AUTO initiation. Single cross-episode contract.

## Do not break

- `write_canonical(LAST_OMEGA_TRAJECTORY)` validation (`OmegaTrajectory` shape).
- Not on deepsleep clear list in `make_deepsleep_runner`.

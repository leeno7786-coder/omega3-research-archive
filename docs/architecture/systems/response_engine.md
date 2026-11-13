# System: Response engine (proactive outreach)

> **Agent anchor** · `core/response_engine.py`

## What it is

**Proactive speech grammar and outlet** — selects dialogue acts from curiosity/system signals and can dispatch response work. In Symphony product boot it is not a conductor: CueDesk/ignition decides when `RESPONSE_HOLD` is the route, and `run_response_hold_movement` uses targeted dispatch instead of boot cadence sweeps.

## What it is expected to do

- Act when a selected response route warrants speaking (not only when user spoke).
- Respect `omega_lock` and autonomy pause.
- Route acts through orchestrator brain path with provenance.

## Symphony target

Subordinate to **CueDesk/ignition** — speech is an outlet or response-hold movement, not a parallel cadence universe.

## Do not break

- Fail-closed when deps missing (log + degrade).
- Stuck-active curiosity reopen logic (documented in module).
- Direct `sweep_and_dispatch` compatibility tests; do not restore it as product boot scheduling.

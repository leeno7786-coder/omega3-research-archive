# System: Guardian state persistence

> **Agent anchor** · `guardian/state_persistence.py`

## What it is

**Sole disk writer** for SharedState snapshot → `guardian/state/save_state.json`. Boot `restore_at_boot()` hydrates (6h max age default).

## What it is expected to do

- Atomic save with manifest.
- Per-key persist policy from `policy/shared_state.policy.yaml`.
- **Not** called by production `run_autonomy_cycle` today — only `Orchestrator._invoke_loop` (tests).

## Symphony target

**Post-movement commit** — episode checkpoint + guidance; WAL projections after episode slots committed.

## Do not break

- Guardian sole-writer doctrine for save file.
- `conversation_state` / `pending_conv_cog_affect` persist=false policy.

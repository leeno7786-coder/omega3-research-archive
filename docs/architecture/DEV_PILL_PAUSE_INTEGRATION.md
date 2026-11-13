# Dev Pill Pause + TypeScript Bridge Integration

> **Branch:** `main`
> **Status:** Current live path.

The HUD dev pill runs through the TypeScript CLI bridge in `omega_cli_ts/`.
`POST /api/dev/launch` pauses the pressure loop, starts the Bun WebSocket bridge
on `:8092`, and the HUD terminal connects directly to `/ws/cli`.
`POST /api/dev/shutdown` stops the bridge and releases the pressure-loop pause.

## Current live path

1. HUD enters terminal mode and calls `/api/dev/launch`.
2. `hud.server` calls `pause_pressure_loop("dev_pill", ...)`.
3. `hud.omega_cli_launch.ensure_omega_cli_server()` starts
   `bun run src/server.ts 8092` from `omega_cli_ts/` if the bridge is not already
   listening.
4. The frontend opens `ws://127.0.0.1:8092/ws/cli`.
5. The TypeScript bridge owns the dev-agent turn loop, SKILL.md loading,
   provider-scoped secrets, runtime provider selection, rollback ledger, and
   qwen-agent-tui tool execution.
6. HUD exit calls `/api/dev/shutdown`, which stops the bridge process and calls
   `resume_pressure_loop("dev_pill", ...)`.

## Runtime handoff

`hud.omega_cli_launch.resolve_voice_backend_override()` still probes
`core.model_router.model_for_kind(VOICE_BOX)` so the bridge starts with the same
local voice/runtime model the substrate uses. The launcher passes resolved values
through `QWEN_MODEL`, `QWEN_BASE_URL`, and `QWEN_PROVIDER` environment variables.

## Persistence and teardown

- Bridge stdout/stderr append to `guardian/logs/ts_cli_bridge.log`.
- The detached child PID is tracked in `guardian/state/ts_cli_bridge.pid`.
- Shutdown uses a command-line identity check before killing any pid or process
  listening on `:8092`.
- The old Python CLI and terminal-agent package are intentionally absent from
  this path.

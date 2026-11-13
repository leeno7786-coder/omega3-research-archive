# docs/research/CLAUDE.md — historical agent reference (retired architecture)

**What this was** — A session-start guide for Claude Code that described the
pre-v2 Symphony phase architecture (BrainLoop, Autonomous loop, old runtime
ports, REPAIR/SLICE test files).

**Current status:** Superseded. The canonical agent instructions are at
[`../../AGENTS.md`](../../AGENTS.md) (the governing system contract) with coordination
metadata at [`../../AGENTS.md`](../../AGENTS.md).

## What changed

- Old: five cognitive layers (BrainLoop → orchestrator → Auto/Meta/Global →
  DeepSleep/Omega) with per-loop separate ports, Lemonade :13305, llama-server
  :49234, FastFlowLM :52625.
- Now: **one pressure-driven cognition loop** — `boot.py` → PressureRuntime →
  15s ticks → cognition cascade (Global A → Meta A → AUTO → Meta B → Global B
  → close → windowed reflection) with all model inference through
  `:11434` (`gemma_e4b` via `runtime/client.py`).

## What to do with this file

- Do not treat content below as build authority. Consult `AGENTS.md` and
  live code (`boot.py`, `cognition_chain.py`, `pressure_loop/loop.py`) for
  current architecture.
- The REPAIR/SLICE-phase test paths and runtime ports listed below are
  **retired** and may not match the live filesystem.

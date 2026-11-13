# System: Autonomous loop (AUTO — the limb)

> **Agent anchor** · Package: `autonomous/autonomous_loop/` · Full detail: `autonomous/autonomous_loop/README.md`

## What it is

The **execute-and-score** loop. Meta decides *what*; autonomous decides *how to run one order* and records *what happened*. **Surface-B**: walks its own phases directly (not `phase_registry`).

## What it is expected to do (one invocation)

1. **phase_1_read_orders** — drain meta drop / queue; or `META_SILENCE`.
2. **phase_2** resolve schema + build prompt (only if real order + tools).
3. **phase_3_inference** — runtime generate + tool execution.
4. **phase_3_5–3_8** — dopamine, plasticity, strength, stress (**only if inference ran**).
5. **phase_4_persist** — memory paths, outcome record, telemetry.

**Gate:** If meta silent or no executable tools → short-circuit to phase_4 **without** fabricated neuromod.

## What it must NOT do

- Plan or choose goals (meta).
- Run on a timer by itself (symphony: only on `AUTO_INITIATE`).
- Fire full neuromod on silence cycles.

## Entry points

- `run_autonomous_loop(AutoLoopContext)` in `loop.py`
- Orchestrator: `make_auto_runner()` in `core/loop_adapters.py`
- Boot: `_run_loop("auto")` **only if** meta left order drop or queue (`boot.py` post-meta block)
- `metacognition/user_goal_dispatch.py` — user tool dispatch
- Brain `intent_classify` → `dispatch_user_goal` (synchronous tool path)

## Writes / reads

| Surface | What |
|---------|------|
| SharedState | `LAST_AUTO_CYCLE` (CycleEnvelope) via adapter |
| Disk | outcomes, meta order consumption, memory via phase_4 |

## Symphony role

**Movement `AUTO`**: full phase chain, single signal after **META_A**. Outcome feeds **META_B** (`episode.auto`).

## Do not break

- META_SILENCE short-circuit + no fake neuromod.
- Order queue + drop semantics (`phase_1_read_orders`).

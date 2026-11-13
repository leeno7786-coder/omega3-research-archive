# System: Brain receiver (intake surface)

> **Agent anchor** · `core/brain_receiver.py`

## What it is

**Canonical entry** for external signals → `BrainIntake` → `Orchestrator.initiate_brain_loop`. Deterministic intent heuristics only (no LLM on receiver path).

## Handlers

| Handler | Source | Notes |
|---------|--------|-------|
| `handle_user_input` | Operator chat | May consume `OUTSTANDING_USER_QUESTION` |
| `handle_system_signal` | Internal / lifecycle | |
| `handle_autonomous_initiation` | Re-export path | |

## What it is expected to do

- Mint `cycle_id`, classify signal type, set guardian attention flags.
- Always run a brain cycle (Failure Flow Law — no skip).
- Thread `shared_state` into brain for epoch + outstanding question.

## Symphony target

**Operator pressure ingress** — parse directives ("learn Python") → `OPERATOR_PRESSURE` + trajectory seed + episode open, not only chat completion.

## Do not break

- Orchestrator-only dispatch (no direct engine calls).
- Jailbreak / identity token heuristics for `requires_guardian_attention`.

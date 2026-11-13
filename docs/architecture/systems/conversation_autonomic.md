# System: Conversation state & autonomic blend

> **Agent anchor** · `conversation_state.py`, `autonomic_modulator.py`, `operator_presence.py`

## Conversation state

- **Key:** `CONVERSATION_STATE` (canonical).
- **TTL:** 1 hour silence → read defaults.
- **Writers:** `brain_receiver` (user), brain respond (system).
- **Readers:** `dialogue_act_selector`, `reply_evaluator`.
- **Not persisted** across reboot (policy).

## Autonomic modulator (conv-cog affect)

- **Key:** `PENDING_CONV_COG_AFFECT` — queued deltas per user reply.
- **Consumers:** auto neuromod phases consume per signal name.
- **Not persisted** (policy).

## Operator presence

- **Key:** `PRESENCE_STATE` via `write()` (not canonical).
- **Writer:** `operator_presence` watcher thread.
- **Reader:** `autonomous_engine` — fail-closed unless `PRESENT`.

## Symphony target

Episode-scoped sensory/autonomic slots; presence gates pressure-driven initiation.

## Do not break

- Thread lock on conversation_state updates (async race).
- Fail-closed presence for proactive sweeps.

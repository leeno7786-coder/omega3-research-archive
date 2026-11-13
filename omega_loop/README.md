# Omega loop

Omega runs once for every ordinary episode, after Global B and before the
episode DeepSleep terminus:

`Global B → Omega episode cognition → DeepSleep`

- `episode/` performs action-specific episodic metacognition over the full
  Global B packet.
- `window/` performs the separate staged ten-episode reflection pass when the
  reflection window lane opens a window episode; it does not replace the
  ordinary per-episode pass.
- `motor/` authors the pre-action motor program and outcome contract.
- `_json_extract.py` is the shared repair-tolerant, fail-closed JSON parser.

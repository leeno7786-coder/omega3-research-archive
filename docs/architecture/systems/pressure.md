# System: Pressure & cadence (when to act)

> **Agent anchor** · `core/pressure_field.py`, `core/cue_desk.py`, `core/cycle_cadence.py`, `core/autonomous_engine.py`, `core/response_engine.py`

## What it is after S17

**CueDesk-owned pressure surface** — pressure says whether attention opens, ignition says which route runs, and legacy cadence/sweep surfaces are sensors or compatibility APIs only.

| Piece | Role |
| ----- | ---- |
| `pressure_field.evaluate_pressure` | Reads contributors and decides open/hold |
| `symphony_ignition.decide_ignition` | Selects `META_EXECUTIVE`, `GLOBAL_PRELUDE`, `OMEGA_PRELUDE`, or `RESPONSE_HOLD` |
| `cue_desk.CueDesk.tick` | Sole product opener/dispatcher |
| `cycle_cadence` | Compatibility scheduler plus useful signal helpers; not product conductor |
| `autonomous_engine.sweep_and_initiate` | Compatibility API; closure-only drain in symphony mode |
| `response_engine` | Speech grammar/outlet; not product boot scheduler |

## What it is expected to do (symphony)

- **Open** or **continue** a `CognitiveEpisode` when pressure thresholds met.
- **Never** be the only reason meta/global/omega run (no Forest Gump).
- Combine: curiosity gaps, operator intent, neuromod spikes, outstanding questions, guardian signals.

## Product Truth

S17 removed the product dual-conductor path. `boot.run_continuous()` writes `_epoch`, calls CueDesk, emits idle heartbeat, and sleeps. It does not schedule the timer epoch chain, response sweep, or autonomous sweep.

## Symphony target

Pressure evaluates → ignition selects route → movements. Timer/cadence helpers are silenced as product initiators in symphony mode and contribute only sensors, compatibility APIs, or tests. The bounded epistemic substrate that runs when no executive episode is open is `idle_probe` (SWING §S11) — produces pressure inputs only, never initiates a movement directly.

## Key files

- `core/cycle_cadence.py`
- `core/autonomous_engine.py`
- `core/response_engine.py`
- `core/cue_desk.py`
- `core/pressure_field.py`
- `boot.py` (substrate and CueDesk tick loop; **not** cadence/epoch conductor)

## Do not break

- Presence gate (`read_presence_from_shared_state`) on proactive paths.
- Autonomy pause and presence gates on pressure/CueDesk paths.
- Guardian and outstanding-question pressure contributors.

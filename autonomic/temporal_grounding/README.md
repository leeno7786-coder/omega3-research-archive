# autonomic/temporal_grounding: Temporal persistence

**Status**: The former session-only `TemporalGrounding` implementation and its
direct review test have been removed. They were not boot or cognition
dependencies. The live surface in this package is `temporal_persistence.py`,
which owns the durable lifetime clock under `guardian/state/`.

**Key files**
- [`temporal_persistence.py`](temporal_persistence.py): `record_boot()`,
  `checkpoint()`, and `record_shutdown()` for the durable lifetime clock.
- [`__init__.py`](__init__.py): empty package marker.

**Entry points / how it's used**
- `record_boot(now)` stamps boot state and computes prior offline time.
- `checkpoint(session_epochs, session_cycles, session_seconds)` folds current
  session totals into the lifetime clock.
- `record_shutdown(now, ...)` records a clean shutdown after checkpointing.
- `boot.py` and `core/boot_lifecycle.py` call the persistence surface directly.

**Depends on / depended-on-by**
- `temporal_persistence.py` depends on `core.paths.GUARDIAN_STATE_ROOT`.
- `schema/phase_clock.py` is a separate live schema module; the absent
  `autonomic/phase_clock/` package is not a dependency.
- `boot.py` and `core/boot_lifecycle.py` depend on the persistence functions.

**Gotchas / doctrine**
- Persistence is atomic: writes use a temporary file followed by replacement.
- The lifetime clock is stored under `guardian/state/temporal_state.json`, not the
  retired `.omega_data` tree.
- The removed session-only `autonomic_module.py` contract is not a live source
  of temporal context.

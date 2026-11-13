"""pressure_loop — the single pressure tick loop + tick counter + contributors.

The one loop of the converged Omega cognition system: every tick (interval per
policy) it advances pressure once by gathering contributors and accumulating
their scores; on ignition it pauses and hands off to the cognition cascade.

Modules:
- contributors: Contributor + EpisodeIgnition types and constructors.
- pressure:     gather_contributors + evaluate (neuromod-modulated aggregate →
                ignition) — the pressure evaluation + contributor gathering.
- loop:         run_pressure_tick_loop (the tick loop + tick counter) and the
                ignition cascade entry that starts Global A.
"""

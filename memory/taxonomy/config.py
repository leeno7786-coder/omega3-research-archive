"""Phase 7N — deterministic memory taxonomy live-wiring policy.

The taxonomy capture, compilation, and recall substrate is part of the live
cascade. Only the advisory pressure seam remains independently operator-gated.
This module imports nothing from the cascade or Guardian.
"""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "on", "yes"})

# The taxonomy ADVISORY PRESSURE seam (7M) flips independently of the recall hook — capture
# + recall (selection) and the pressure contributor (attention) are separate gates.
OMEGA_MEMORY_TAXONOMY_PRESSURE_ENV = "OMEGA_MEMORY_TAXONOMY_PRESSURE"


def taxonomy_enabled() -> bool:
    """Return whether the live taxonomy capture/recall path is enabled."""
    return True


def taxonomy_pressure_enabled() -> bool:
    """True when the taxonomy ADVISORY PRESSURE seam contributes to gather_contributors.

    Default OFF (byte-identical). A read-only attention signal recalled from the compiled
    graph — NEVER authority. Independent of ``taxonomy_enabled()`` so the pressure seam can
    be validated/flipped on its own. Resolved from ``OMEGA_MEMORY_TAXONOMY_PRESSURE``.
    """
    return os.environ.get(OMEGA_MEMORY_TAXONOMY_PRESSURE_ENV, "").strip().lower() in _TRUTHY

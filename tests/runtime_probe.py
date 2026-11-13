"""Shared omega-runtime probes for plain-script tests (A5 ENV hygiene).

Plain-script tests import this module; pytest collection ignores it
(no test_* at module scope).
"""

from __future__ import annotations

import os
import sys
import logging

log = logging.getLogger("omega.tests.runtime_probe")


def omega_runtime_health() -> bool:
    try:
        from runtime.client import get_runtime_client

        return bool(get_runtime_client().health())
    except Exception:
        return False


def runtime_strict() -> bool:
    return os.environ.get("OMEGA_TEST_RUNTIME_STRICT", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def exit_if_runtime_down(
    *,
    label: str = "omega-runtime :11434",
) -> None:
    """At script start: exit 0 (skip) or 1 (strict) when runtime is offline."""
    alive = omega_runtime_health()
    if alive:
        return
    if runtime_strict():
        print(f"  FAIL  {label} required but offline (A5 ENV)")
        sys.exit(1)
    print(f"  SKIP  {label} offline — A5 ENV (re-run with runtime up)")
    sys.exit(0)

from __future__ import annotations

import importlib


def test_shutdown_runtime_sweep_imports_on_every_platform() -> None:
    """``scripts/shutdown.py`` sweeps the runtime through ``portable_runtime_shutdown``,
    which imports the launcher. A Win32-only name evaluated at class definition makes
    that import raise off Windows, so shutdown crashes before it stops anything."""
    module = importlib.import_module("scripts.portable_runtime_shutdown")

    assert callable(module.sweep_portable_runtime_processes)

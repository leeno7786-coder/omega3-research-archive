"""B5 — runtime.client must not construct singleton at import time."""

from __future__ import annotations

import importlib
import sys


def test_import_runtime_client_does_not_build_singleton(monkeypatch):
    import runtime

    original = importlib.import_module("runtime.client")
    with monkeypatch.context() as isolation:
        # Importing a replacement submodule mutates both registries. Restore
        # both so later string-path monkeypatches resolve the canonical module.
        isolation.setattr(runtime, "client", original)
        isolation.delitem(sys.modules, "runtime.client")
        rc = importlib.import_module("runtime.client")

        assert rc is runtime.client
        assert rc is not original
        assert rc._singleton is None

    assert sys.modules["runtime.client"] is original
    assert runtime.client is original

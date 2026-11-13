"""Omega runtime package with lazy access to the HTTP client.

The portable setup executable imports lightweight provider metadata from this
package. Importing ``runtime`` must not pull the full cognition client, model
stack, and their optional dependencies into that small startup surface.
"""

from typing import Any

__all__ = [
    "ClassifyResult",
    "DEFAULT_FALLBACK_COORD",
    "RuntimeClient",
    "RuntimeStreamError",
    "get_runtime_client",
    "resolve_runtime_base_url",
]


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module 'runtime' has no attribute {name!r}")
    import importlib

    client = importlib.import_module("runtime.client")
    value = getattr(client, name)
    globals()[name] = value
    return value

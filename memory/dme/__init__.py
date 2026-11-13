"""DME SRAM projection for Memory Engine v2.

MatMul Hamming engine over Dewey-lobe coordinate bitmaps with ONNX/NPU-first
execution and NumPy CPU fallback.

Import policy: keep this package init lazy. Eager imports of ``adapter`` pull
taxonomy/paths (profile-root env) and can block ``memory.dme.recall_hook`` —
the recall hot path — from loading even when the hook module is present in
the wheel. Submodules import their dependencies on demand.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "DMEIndex",
    "DMEParityReport",
    "DMESeat",
    "DmeLoadError",
    "apply_dme_recall_hook",
    "dme_meta_path",
    "dme_recall_session",
    "dme_root",
    "dme_sram_path",
    "force_unload_all_sessions",
    "is_dme_resident",
    "last_recall_hook_stats",
    "load_sram",
    "merge_dme_candidate_dicts",
    "parity_report",
    "recall_candidates",
    "recall_verified",
    "rebuild_sram_from_graph",
    "shutdown_runtime",
]

_ADAPTER_EXPORTS = frozenset(
    {
        "DMEIndex",
        "DMEParityReport",
        "DMESeat",
        "dme_meta_path",
        "dme_root",
        "dme_sram_path",
        "load_sram",
        "merge_dme_candidate_dicts",
        "parity_report",
        "recall_candidates",
        "recall_verified",
        "rebuild_sram_from_graph",
    }
)
_HOOK_EXPORTS = frozenset(
    {
        "DmeLoadError",
        "apply_dme_recall_hook",
        "dme_recall_session",
        "force_unload_all_sessions",
        "is_dme_resident",
        "last_recall_hook_stats",
    }
)


def shutdown_runtime() -> dict[str, Any]:
    """Release every in-process DME seat before Omega tears down."""
    released: list[str] = []
    failures: list[Exception] = []
    operations = (
        ("recall_sessions", "memory.dme.recall_hook", "force_unload_all_sessions"),
        ("hop_backend", "memory.dme.hop_engine", "close_backend"),
        ("matmul_runtime", "memory.dme.matmul_engine", "shutdown_runtime"),
    )
    for name, module_name, function_name in operations:
        try:
            module = __import__(module_name, fromlist=[function_name])
            getattr(module, function_name)()
            released.append(name)
        except Exception as exc:  # noqa: BLE001 - every seat gets an attempt
            failure = RuntimeError(f"DME {name} shutdown failed")
            failure.__cause__ = exc
            failures.append(failure)
    if failures:
        raise ExceptionGroup("DME runtime release failed", failures)
    return {"ok": True, "released": released}


def __getattr__(name: str) -> Any:
    if name in _ADAPTER_EXPORTS:
        from memory.dme import adapter as _adapter

        return getattr(_adapter, name)
    if name in _HOOK_EXPORTS:
        from memory.dme import recall_hook as _hook

        return getattr(_hook, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))

"""Change 3 (verify-only): the dev-lobe persist must run BEFORE the workspace clear
in the cascade tail, so the store is complete when the workspace is wiped and a failed
build's rehydrate can restore it. Locks the ordering against future reordering."""

from __future__ import annotations

import inspect

import cognition_chain as cc


def test_persist_foldback_precedes_workspace_clear_in_cascade():
    src = inspect.getsource(cc._start_meta_b_global_b)
    fold_idx = src.find("_run_skill_foldback(")
    clear_idx = src.find("_execute_pending_workspace_port(")
    assert fold_idx != -1, "persist fold-back call not found in _start_meta_b_global_b"
    assert clear_idx != -1, "workspace clear call not found in _start_meta_b_global_b"
    assert fold_idx < clear_idx, (
        "dev-lobe persist (_run_skill_foldback) must precede the workspace clear "
        "(_execute_pending_workspace_port) — else the store is incomplete when the "
        "workspace is wiped and rehydrate cannot restore a failed build"
    )

"""Structured telemetry sink for the rebuilt substrate observability path."""

from __future__ import annotations

from guardian.telemetry.writer import (
    TELEMETRY_SYSTEMS,
    current_episode_id,
    current_run_id,
    emit_boot_marker,
    init_run_id,
    reset_for_tests,
    set_episode_id,
    write_telemetry,
)

__all__ = [
    "TELEMETRY_SYSTEMS",
    "current_episode_id",
    "current_run_id",
    "emit_boot_marker",
    "init_run_id",
    "reset_for_tests",
    "set_episode_id",
    "write_telemetry",
]

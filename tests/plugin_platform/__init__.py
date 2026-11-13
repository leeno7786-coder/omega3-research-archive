"""Pytest package shim that keeps the real plugin_platform importable."""

from pathlib import Path

# Pytest places this directory before the repository root while importing this
# package.  Extend the package search path so `plugin_platform.contracts`
# continues to resolve to the production package rather than this test package.
__path__.append(str(Path(__file__).resolve().parents[2] / "plugin_platform"))

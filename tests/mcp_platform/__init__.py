"""Tests for the signed MCP package contract boundary."""

from core.paths import OMEGA_ROOT

# Pytest imports this package as ``mcp_platform``. Include the real package
# directory so the test package name does not shadow the production package.
__path__.append(str(OMEGA_ROOT / "mcp_platform"))

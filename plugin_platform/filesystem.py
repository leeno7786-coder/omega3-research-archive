"""Compatibility import for the neutral retained-handle filesystem boundary."""

from __future__ import annotations

import sys

from runtime import pinned_filesystem as _implementation

sys.modules[__name__] = _implementation

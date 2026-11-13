"""Shared in-process lock for coordinate-index readers and writers.

The coordinate index is atomically replaced, but Windows can deny a new read while
another thread is replacing the path. The replacement and the reader's stat/open
sequence therefore share this lock. It protects only this derived index file; it
does not serialize canonical memory writes or recall work.
"""

from __future__ import annotations

import threading


COORD_INDEX_IO_LOCK = threading.RLock()


__all__ = ["COORD_INDEX_IO_LOCK"]

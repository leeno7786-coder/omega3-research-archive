"""Filesystem helpers that survive a repository and a temp directory on
different volumes.

Omega is meant to be portable: it runs from whatever drive it is copied to.
The suite quietly assumed otherwise. Several tests build a real signed package
by hard-linking the interpreter and its DLLs into a temp tree, which is instant
where a copy is slow -- but a hard link cannot span volumes, and Windows
reports that as ``WinError 17: The system cannot move the file to a different
disk drive``. With the checkout on ``J:`` and pytest's temp on ``C:``, five
tests failed for the whole life of that arrangement and were written off as
"environment-only". They were not: they were the suite disagreeing with the
portability the product claims.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


def link_or_copy(source: str | Path, destination: str | Path) -> Path:
    """Hard-link when the volumes allow it, copy when they do not.

    The link is worth trying first -- these call sites clone an interpreter and
    every DLL beside it, per test -- but it must never be the reason a test
    fails. Falls back on any OSError, which covers a cross-volume link, a
    filesystem without link support, and a link count already at its limit.
    """
    source = Path(source)
    destination = Path(destination)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copyfile(source, destination)
    return destination

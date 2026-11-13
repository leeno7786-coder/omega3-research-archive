"""A project test runner on PATH, for tests that exercise ``run_tests``.

``run_tests`` runs a project's test command exactly as written, with the machine's own
toolchain (operator rule 2026-09-15), so ``pytest -q`` resolves through PATH. A test of
that path must not depend on the machine running the suite having ``pytest`` installed:
this puts one on PATH for the test's project, backed by the suite's own interpreter.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest


def put_pytest_on_path(bin_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        (bin_dir / "pytest.cmd").write_text(
            f'@"{sys.executable}" -m pytest %*\r\n', encoding="utf-8"
        )
    else:
        script = bin_dir / "pytest"
        script.write_text(f'#!/bin/sh\nexec "{sys.executable}" -m pytest "$@"\n', encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")

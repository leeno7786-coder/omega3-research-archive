from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_encoding_journal_process_lock_serializes_independent_writers(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    journal_path = tmp_path / "encoding_journal.jsonl"
    trace_path = tmp_path / "lock_trace.txt"
    # The child must put the repo root on sys.path itself -- the same guard
    # scripts/shutdown.py carries. Omega ships an embedded interpreter whose
    # ._pth sets sys.flags.safe_path, so neither the cwd (-c) nor the script
    # directory is prepended; passing cwd= alone leaves ``memory`` unimportable.
    script = r"""
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from memory import encoding_journal as journal

journal.JOURNAL_PATH = Path(sys.argv[2])
trace = Path(sys.argv[3])
label = sys.argv[4]
with journal._journal_process_lock():
    with trace.open("a", encoding="utf-8") as handle:
        handle.write(f"{label}:enter\n")
        handle.flush()
    time.sleep(0.2)
    with trace.open("a", encoding="utf-8") as handle:
        handle.write(f"{label}:exit\n")
        handle.flush()
"""
    processes = [
        subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(root),
                str(journal_path),
                str(trace_path),
                label,
            ],
            cwd=root,
        )
        for label in ("one", "two")
    ]

    assert [process.wait(timeout=20) for process in processes] == [0, 0]
    events = trace_path.read_text(encoding="utf-8").splitlines()
    assert events in (
        ["one:enter", "one:exit", "two:enter", "two:exit"],
        ["two:enter", "two:exit", "one:enter", "one:exit"],
    )

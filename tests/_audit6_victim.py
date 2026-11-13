"""
_audit6_victim.py -- subprocess helper for AUDIT-6 kill -9 test.

The parent test spawns this script, waits for it to land an open
journal entry + a canonical file, then kill -9's the process. The
parent verifies the encoding-journal recovery rolls back the
in-flight write cleanly.

Args:
    sys.argv[1] = path to a temp directory the parent owns
    sys.argv[2] = memory_id to use (must look like "L.YY.ZZZZ")

Behavior:
    1. Monkey-patch encoding_journal.JOURNAL_PATH to live in tempdir
    2. Write a fake canonical file at tempdir/<memory_id>.json
    3. Call journal_open with the fake final_path
    4. Print a marker line (the parent reads it to know we're ready)
    5. Sleep forever -- waiting to be killed mid-encoding

This script does NOT call atomic_write or the real index-patcher.
The test's claim is about the JOURNAL'S survival + recovery, not
about index/atomic_write semantics (those are covered elsewhere).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
import logging

log = logging.getLogger("omega.tests._audit6_victim")


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
sys.path.insert(0, str(ROOT))


def main():
    if len(sys.argv) < 3:
        print("ERROR: usage: _audit6_victim.py <tempdir> <memory_id>", file=sys.stderr)
        sys.exit(2)

    tempdir = Path(sys.argv[1])
    memory_id = sys.argv[2]

    # Redirect the journal path BEFORE importing anything else
    # that might transitively touch it.
    import memory.encoding_journal as ej

    ej.JOURNAL_PATH = tempdir / "encoding_journal.jsonl"

    # Simulate atomic_write step 1+2 by writing a fake canonical file.
    # Parent will verify this file exists after kill (proving we
    # got past the simulated atomic_write) and then verify it's
    # GONE after recovery rolls back.
    fake_path = tempdir / f"{memory_id}.json"
    fake_path.write_text(
        json.dumps(
            {
                "id": memory_id,
                "coord": [0, 0, 1],
                "text": "fake canonical for AUDIT-6 victim",
            }
        ),
        encoding="utf-8",
    )

    # Open the journal entry — this is the load-bearing call.
    journal_id = ej.journal_open(memory_id, "staging-victim", str(fake_path))

    # Marker so the parent knows we're past the open. Flush so the
    # parent can read it via stdout pipe (or via the marker file).
    marker_file = tempdir / "victim_ready.marker"
    marker_file.write_text(
        json.dumps(
            {
                "journal_id": journal_id,
                "memory_id": memory_id,
                "fake_path": str(fake_path),
            }
        ),
        encoding="utf-8",
    )

    print(
        f"VICTIM_READY journal_id={journal_id} memory_id={memory_id} pid={__import__('os').getpid()}",
        flush=True,
    )

    # Sleep forever. Parent will kill -9 us. atexit handlers WILL
    # NOT fire (that's the point of the test).
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()

"""A child process that dies at B3's boundary — used by ``test_cognitive_transaction_recovery``.

Run as: ``python -m tests.e2e._t13_dying_child <root> <scenario.json> <ready-file>``

It takes the lane-entry mail the parent published, records its prompt against the transaction
record (``executing``), and then blocks forever inside the acknowledgement. The parent kills it
there. That is the only way to reach section 6's B3 state — a process that died holding the
lease — because in-process nothing can skip ``LaneEntry.close``: it runs from a ``finally``, so
any unwinding (a ``BaseException`` included) abandons the effect and hands the mail back.

Nothing here is a test double: the mail store, the successor ledger and ``open_lane_entry`` are
the real ones, pointed at the parent's own state directory.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(root: str, scenario_path: str, ready_path: str) -> int:
    from _pytest.monkeypatch import MonkeyPatch

    from tests.helpers.motor_program_harness import MotorProgramHarness

    scenario = json.loads(Path(scenario_path).read_text(encoding="utf-8"))
    monkeypatch = MonkeyPatch()
    MotorProgramHarness(monkeypatch=monkeypatch, root=Path(root) / "omega")

    from auto_loop.lane_entry import open_lane_entry
    from global_loop import intersections as mail_store

    episode = SimpleNamespace(
        episode_id=scenario["episode_id"], thread_id=scenario["thread_id"],
        opened_by="omega_successor",
        pressure={"contributors": [{"id": "omega_successor", "weight": 1.0,
                                    "detail": scenario["successor"]}]})
    pack = {"action": "RESPONSE", "execution_inputs": {"job_id": scenario.get("job_id", "")}}
    entry = open_lane_entry(episode, pack)
    if entry is None:
        Path(ready_path).write_text("no lane entry", encoding="utf-8")
        return 2

    real_acknowledge = mail_store.acknowledge_mail

    def acknowledge_then_die(*args, **kwargs):
        """The prompt is already recorded; the acknowledgement never lands."""
        Path(ready_path).write_text("at the boundary", encoding="utf-8")
        while True:                      # the parent kills this process here
            time.sleep(0.05)
        return real_acknowledge(*args, **kwargs)   # unreachable, kept honest

    monkeypatch.setattr(mail_store, "acknowledge_mail", acknowledge_then_die)
    entry.deliver(f"a prompt that carries its mail:{entry.block}")
    return 0                             # never reached


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2], sys.argv[3]))

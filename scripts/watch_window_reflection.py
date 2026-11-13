"""Watch until 10-episode reflection window locks and Omega runs."""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "guardian" / "logs" / "terminal.log"
WINDOW = ROOT / "guardian" / "state" / "reflection_window.json"
CUT = datetime.strptime("2026-06-09 22:37:00", "%Y-%m-%d %H:%M:%S").timestamp()
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")

TARGET_MARKERS = [
    "pressure/append_reflection_window DATA locked",
    "pressure/omega_window_handoff PHASE enter",
    "omega/",
    "pressure/deepsleep_window_handoff PHASE enter",
    "pressure/archive_reflection_window",
]


def parse_ts(line: str) -> float | None:
    m = TS_RE.match(line)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return dt.timestamp() + int(m.group(2)) / 1000.0


def read_window() -> dict:
    if not WINDOW.is_file():
        return {}
    try:
        return json.loads(WINDOW.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def main() -> int:
    start = time.time()
    last_count = -1
    last_status = ""
    print(f"Watching window reflection (need 10 episodes). Started {datetime.now():%H:%M:%S}")
    print(f"Window file: {WINDOW}")
    print()

    for i in range(720):  # 720 * 30s = 6 hours max
        w = read_window()
        count = int(w.get("episode_count") or len(w.get("episodes") or []))
        status = str(w.get("status") or "?")
        if count != last_count or status != last_status:
            print(
                f"[{datetime.now():%H:%M:%S}] window {count}/10 status={status} "
                f"elapsed={int(time.time() - start) // 60}m"
            )
            last_count = count
            last_status = status

        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        recent = [ln for ln in lines if (t := parse_ts(ln)) and t >= CUT]

        locked_lines = [ln for ln in recent if "append_reflection_window" in ln and "locked" in ln]
        omega_enter = [ln for ln in recent if "pressure/omega_window_handoff PHASE enter" in ln]
        ds_window = [ln for ln in recent if "deepsleep_window_handoff PHASE enter" in ln]
        archive = [ln for ln in recent if "archive_reflection_window" in ln.lower()]

        if locked_lines or (count >= 10 and status == "locked"):
            print("\n=== WINDOW LOCKED ===")
            for ln in locked_lines[-3:]:
                print(ln[:160])
            if not locked_lines:
                print(f"(from state file: count={count} status={status})")

        if omega_enter:
            print("\n=== OMEGA WINDOW REFLECTION STARTED ===")
            for ln in omega_enter[-5:]:
                print(ln[:160])

        if ds_window:
            print("\n=== DEEPSLEEP WINDOW HANDOFF ===")
            for ln in ds_window[-3:]:
                print(ln[:160])

        if archive:
            print("\n=== WINDOW ARCHIVED / RESET ===")
            for ln in archive[-3:]:
                print(ln[:160])

        # Done when omega window handoff completed + archive or deepsleep window done
        if omega_enter and (ds_window or archive):
            tick_oks = [ln for ln in recent if "pressure/tick PHASE ok" in ln]
            print(f"\n=== COMPLETE === episodes in window: {count}")
            print(f"Total pressure ticks completed this boot: {len(tick_oks)}")
            print("\n=== LAST 30 LINES ===")
            for ln in recent[-30:]:
                print(ln[:160])
            return 0

        if status == "locked" and count >= 10 and omega_enter:
            return 0

        time.sleep(30)

    print("Timeout waiting for window reflection", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Watch terminal.log for pacing milestones (this boot session)."""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "guardian" / "logs" / "terminal.log"
CUT = datetime.strptime("2026-06-09 22:37:00", "%Y-%m-%d %H:%M:%S").timestamp()
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")

MARKERS = [
    "pressure/deepsleep_episode_handoff PHASE ok",
    "pressure/ignite_episode PHASE ok",
    "pressure/tick PHASE ok",
]


def parse_ts(line: str) -> float | None:
    m = TS_RE.match(line)
    if not m:
        return None
    dt = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
    return dt.timestamp() + int(m.group(2)) / 1000.0


def main() -> int:
    recent: list[str] = []
    for _ in range(120):  # ~20 min max
        lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        recent = [ln for ln in lines if (t := parse_ts(ln)) and t >= CUT]
        tick_enters = sum(1 for ln in recent if "pressure/tick PHASE enter" in ln)
        tick_oks = sum(1 for ln in recent if "pressure/tick PHASE ok" in ln)
        if tick_oks >= 1 and tick_enters >= 2:
            break
        time.sleep(10)

    print("=== WATCH RESULT ===")
    for marker in MARKERS:
        hits = [ln for ln in recent if marker in ln]
        print(f"{marker}: {len(hits)}")
        for h in hits[-3:]:
            print(f"  {h[:150]}")

    tick_enter_ts = [parse_ts(ln) for ln in recent if "pressure/tick PHASE enter" in ln]
    tick_ok_ts = [parse_ts(ln) for ln in recent if "pressure/tick PHASE ok" in ln]
    print("\n=== TICK TIMING (this boot) ===")
    for j, t in enumerate(tick_enter_ts, 1):
        if t is None:
            continue
        print(f"tick {j} enter: {datetime.fromtimestamp(t).strftime('%H:%M:%S.%f')[:-3]}")
    for j, t in enumerate(tick_ok_ts, 1):
        if t is None:
            continue
        print(f"tick {j} ok:    {datetime.fromtimestamp(t).strftime('%H:%M:%S.%f')[:-3]}")
    if len(tick_ok_ts) >= 1 and len(tick_enter_ts) >= 2:
        gap = tick_enter_ts[1] - tick_ok_ts[0]
        print(f"gap tick1 ok -> tick2 enter: {gap:.2f}s")

    print("\n=== LAST 25 LINES ===")
    for ln in recent[-25:]:
        print(ln[:160])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

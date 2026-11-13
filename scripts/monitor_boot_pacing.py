"""Monitor pacing from a boot cutoff until first episode completes (or window locks)."""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG = ROOT / "guardian" / "logs" / "terminal.log"
WINDOW = ROOT / "guardian" / "state" / "reflection_window.json"
TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")
PHASE_OK = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3}).*?(\w+)/([\w_]+) PHASE ok")
PACING = re.compile(r"cognition pacing: pause (\d+(?:\.\d+)?)s after (\w+)\.(\w+)")


def parse_ts_match(m: re.Match[str]) -> float:
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
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cut",
        default="",
        help="Log cutoff as YYYY-MM-DD HH:MM:SS (default: now)",
    )
    parser.add_argument("--polls", type=int, default=24)
    parser.add_argument("--interval", type=float, default=15.0)
    args = parser.parse_args()
    if args.cut:
        cut = datetime.strptime(args.cut, "%Y-%m-%d %H:%M:%S").timestamp()
    else:
        cut = time.time()

    print(f"Monitoring from {datetime.fromtimestamp(cut).strftime('%Y-%m-%d %H:%M:%S')}")
    for i in range(args.polls):
        lines = (
            LOG.read_text(encoding="utf-8", errors="replace").splitlines() if LOG.is_file() else []
        )
        recent = [ln for ln in lines if (m := TS_RE.match(ln)) and parse_ts_match(m) >= cut]
        phase_oks: list[tuple[float, str]] = []
        for ln in recent:
            m = PHASE_OK.match(ln)
            if m:
                phase_oks.append((parse_ts_match(m), f"{m.group(3)}/{m.group(4)}"))

        pacing: list[tuple[float, str, str]] = []
        for ln in recent:
            m = PACING.search(ln)
            if m:
                pacing.append((float(m.group(1)), m.group(2), m.group(3)))

        ignite = sum(1 for ln in recent if "pressure/ignite_episode PHASE ok" in ln)
        ticks = sum(1 for ln in recent if "pressure/tick PHASE ok" in ln)
        settle = sum(1 for ln in recent if "deepsleep/settle" in ln)
        w = read_window()
        now_s = datetime.now().strftime("%H:%M:%S")
        print(f"\n--- poll {i + 1} @ {now_s} ---")
        print(
            f"  ticks={ticks} ignites={ignite} settle_hits={settle} "
            f"window={w.get('episode_count', 0)}/10 status={w.get('status', '?')}"
        )
        if pacing:
            last = pacing[-1]
            print(f"  pacing pauses logged: {len(pacing)} (last: {last[1]}.{last[2]} {last[0]}s)")
        if len(phase_oks) >= 2:
            print("  recent phase gaps (s):")
            for j in range(max(1, len(phase_oks) - 8), len(phase_oks)):
                dt = phase_oks[j][0] - phase_oks[j - 1][0]
                print(f"    {dt:5.2f}s  {phase_oks[j - 1][1]} -> {phase_oks[j][1]}")

        ignite_ts = [t for t, n in phase_oks if n == "pressure/ignite_episode"]
        tick_ts = [t for t, n in phase_oks if n == "pressure/tick"]
        if ignite_ts and tick_ts:
            for t_ig in ignite_ts:
                later = [t for t in tick_ts if t > t_ig]
                if later:
                    print(f"  ignite->next tick gap: {later[0] - t_ig:.2f}s")
                    break

        if any("pressure/omega_window_handoff PHASE enter" in ln for ln in recent):
            print("  *** Omega window reflection started ***")
            break
        if w.get("status") == "locked":
            print("  *** Reflection window locked ***")
            break

        if i + 1 < args.polls:
            time.sleep(args.interval)

    print("\n=== LAST 20 LOG LINES (this boot) ===")
    for ln in recent[-20:]:
        print(ln[:180])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

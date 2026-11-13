"""Route a JSON batch of cases through the LIVE deterministic router.

Reusable measurement runner for red-team stress generation. Reads a JSON list
of cases and prints, for each, what the live grammar/router actually returns —
so generated cases are scored against real behaviour, never imagination.

Case schema (one object per case):
  {"text": str, "kind": "write"|"recall", "lobe": int (write only),
   "expected_y": int|null (write) | "expected_lobe": int (recall), "note": str}

Usage:
  .\\.venv-brain\\Scripts\\python.exe scripts\\route_cases.py cases.json
Output: JSON list with the live result + a pass/verdict per case.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from auto_loop.mril_tools.router import route_memory_tool  # noqa: E402
from auto_loop.mril_tools.subdomain_helpers import subdomain_for  # noqa: E402


def _route_one(case: dict) -> dict:
    kind = case.get("kind", "write")
    text = str(case.get("text", ""))
    out = {"text": text, "kind": kind, "note": case.get("note", "")}
    if kind == "recall":
        res = route_memory_tool(text)
        got_lobe = int((res.get("allowed_lobes") or [2])[0])
        exp = case.get("expected_lobe")
        out.update(
            {
                "expected_lobe": exp,
                "got_lobe": got_lobe,
                "tool": res.get("tool"),
                "verdict": ("correct" if exp == got_lobe else "miss")
                if exp is not None
                else "unscored",
            }
        )
    else:  # write
        lobe = int(case.get("lobe", 2))
        exp = case.get("expected_y")  # may be None == "should widen"
        got = subdomain_for(text, lobe)
        if exp is None:
            verdict = "widen_correct" if got is None else "over_trigger"
        elif got is None:
            verdict = "widen_miss"  # widened though a real subdomain was intended
        elif got == exp:
            verdict = "correct"
        else:
            verdict = "misroute"
        out.update({"lobe": lobe, "expected_y": exp, "got_y": got, "verdict": verdict})
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: route_cases.py <cases.json>", file=sys.stderr)
        return 2
    cases = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    results = [_route_one(c) for c in cases]
    summary: dict = {"total": len(results)}
    for r in results:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    print(json.dumps({"summary": summary, "results": results}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

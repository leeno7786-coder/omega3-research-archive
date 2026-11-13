"""Isolated-lane autopsy — name the anchor that admits wrong memories.

Run 4 finding: fabricated=4 with ZERO embed calls in the admission path.
Something deterministic admits non-target memories on isolated-alias queries.
This reproduces the gate-1 store, runs ONLY the 10 isolated queries through
the production remember path, and prints for every admitted/blocked candidate
WHICH basis fired: lexical overlap (with the shared tokens), strong
provenance (edge class), or neither (=> a bug in the partition).
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def _load_gate1():
    spec = importlib.util.spec_from_file_location(
        "gate1", ROOT / "scripts" / "memory_ablation_gate1.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["gate1"] = mod
    spec.loader.exec_module(mod)
    return mod


def run() -> None:
    g1 = _load_gate1()
    counters: Counter = Counter()

    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        g1._isolate(root)

        wrote = {}
        for i, t in enumerate(g1.TARGETS):
            mid = g1._write(t.lobe, t.y, t.write, f"tgt{i}", counters)
            if mid:
                wrote[f"tgt{i}"] = mid
        for i, la in enumerate(g1.LINKED):
            m1 = g1._write(la.lobe, la.y, la.target, f"lnk{i}t", counters)
            m2 = g1._write(la.lobe, la.y, la.bridge, f"lnk{i}b", counters)
            if m1:
                wrote[f"lnk{i}t"] = m1
            if m2:
                wrote[f"lnk{i}b"] = m2
        for i, (t, _q) in enumerate(g1.ISOLATED):
            mid = g1._write(t.lobe, t.y, t.write, f"iso{i}", counters)
            if mid:
                wrote[f"iso{i}"] = mid

        from memory.engine.reindex import reindex_and_compile

        reindex_and_compile()

        from auto_loop.mril_tools.registry import REMEMBER_TOOL_REGISTRY
        from auto_loop.mril_tools.remember import (
            _candidate_text,
            _content_tokens,
            _relevance_anchored,
            invoke_remember_tool,
        )
        from auto_loop.mril_tools.subdomain_helpers import subdomain_for

        tool_by_lobe = {lobe: t for t, (lane, lobe, mt) in REMEMBER_TOOL_REGISTRY.items()}

        print("#" * 78)
        print("# ISOLATED-LANE AUTOPSY — which anchor admits what")
        print("#" * 78)
        fabricated = 0
        for i, (t, q) in enumerate(g1.ISOLATED):
            target = wrote.get(f"iso{i}", "")
            tool = tool_by_lobe[t.lobe]
            entry = {
                "tool": tool,
                "query": q,
                "allowed_lobes": [t.lobe],
                "memory_type": REMEMBER_TOOL_REGISTRY[tool][2],
                "lane": REMEMBER_TOOL_REGISTRY[tool][0],
                "lobe": t.lobe,
            }
            y = subdomain_for(q, t.lobe)
            if y is not None:
                entry["subdomain"] = y
            out = invoke_remember_tool(entry, f"isoa-{i}")
            ev = out.get("evidence") or []
            ids = [str(e.get("memory_id")) for e in ev]
            wrongly = [m for m in ids if m != target]
            verdict = out.get("verdict")
            flag = ""
            if verdict == "verified" and ev and target not in ids:
                fabricated += 1
                flag = "  ◀◀ FABRICATED"
            print(
                f"\niso{i} L{t.lobe} verdict={verdict} grounding={out.get('grounding')}"
                f" widened={out.get('widened_unsorted')}/{out.get('widened_lobe')}{flag}"
            )
            print(f"  q: {q!r}")
            print(f"  target: {target} admitted={'YES' if target in ids else 'no'}")
            q_tokens = _content_tokens(q)
            for e in ev:
                mid = str(e.get("memory_id"))
                mark = "TARGET" if mid == target else "WRONG "
                print(f"  [{mark}] {mid}  {e.get('remembered_as')!r:.70}")
            # re-derive the basis for each admitted candidate from the raw text
            for e in ev:
                mid = str(e.get("memory_id"))
                text = f"{e.get('remembered_as') or ''} {e.get('title') or ''}"
                overlap = q_tokens & _content_tokens(text)
                print(f"    basis {mid}: lexical={sorted(overlap) or 'NONE'}")
            for b in (out.get("nonresponsive_evidence") or [])[:3]:
                print(f"  [blocked] {b.get('memory_id')}  {b.get('remembered_as')!r:.60}")
            _ = wrongly, _relevance_anchored, _candidate_text
        print(f"\nfabricated: {fabricated}/10")


if __name__ == "__main__":
    run()

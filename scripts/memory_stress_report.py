"""Multi-angle stress report for the deterministic memory router.

Runs ``memory/routing/stress_corpus.py`` through the LIVE routing authorities
(``subdomain_for`` + ``route_memory_tool``) and the production write-coordinate
mirror (``memory.routing.harness.simulate_write_coord``), then prints honest
per-angle / per-lobe / per-subdomain numbers.

Verdict taxonomy (the real signal — a single "accuracy" number hides the
dangerous case):
  correct       — grammar produced the semantically-correct subdomain.
  misroute      — grammar produced a CONFIDENT but WRONG subdomain (silent
                  mis-placement; the failure mode that kills recall downstream).
  unsorted_miss — grammar widened so the write fell to _unsorted instead of its
                  semantic home (safe degradation; recoverable via the catalog).
  widen_correct — grammar correctly widened (no single subdomain was warranted).
  over_trigger  — grammar invented a subdomain where none was warranted
                  (lexical false positive, e.g. keyword inside a negation).

Usage:
    .\\.venv-brain\\Scripts\\python.exe scripts\\memory_stress_report.py
    .\\.venv-brain\\Scripts\\python.exe scripts\\memory_stress_report.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows consoles default to cp1252; the report uses box-drawing + arrows.
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from auto_loop.mril_tools.router import route_memory_tool  # noqa: E402
from auto_loop.mril_tools.subdomain_helpers import subdomain_for  # noqa: E402
from memory.dewey_map import (  # noqa: E402
    LOBE_MAP,
    SUBDOMAIN_MAP,
    UNSORTED_SUBDOMAIN_Y,
)
from memory.routing.harness import simulate_write_coord  # noqa: E402
from memory.routing.stress_corpus import (  # noqa: E402
    CROSS_LOBE,
    NEGATION,
    NOISE,
    PARAPHRASE,
    RECALL_QUERY,
    SYMMETRY_REALISTIC,
    WRITE_DYNAMIC_KW,
    WRITE_DYNAMIC_NAT,
    WRITE_STATIC,
    StressCase,
    all_stress_cases,
)

DYNAMIC_Y_LOBES = frozenset({2, 8})


def _sub_name(lobe: int, y: Optional[int]) -> str:
    if y is None:
        return "WIDEN"
    if y == UNSORTED_SUBDOMAIN_Y:
        return "_unsorted"
    return SUBDOMAIN_MAP.get((lobe, y), f"y{y}")


def _pct(num: int, den: int) -> str:
    return f"{(100.0 * num / den):5.1f}%" if den else "  n/a"


# Verdict vocab + "good" set per write mode.
#   static  — subdomain_for is the write authority; None → _unsorted (safe).
#   dyn_kw  — domain word IS present, grammar SHOULD hit; None == coverage miss.
#   dyn_nat — domain word absent; None is CORRECT (allocator owns Y).
_MODE_ORDER = {
    "static": ["correct", "misroute", "unsorted_miss", "widen_correct", "over_trigger"],
    "dyn_kw": ["correct", "misroute", "term_match_miss"],
    "dyn_nat": ["allocator_defer", "bonus_hit", "misroute"],
}
_MODE_GOOD = {
    "static": {"correct", "widen_correct"},
    "dyn_kw": {"correct"},
    "dyn_nat": {"allocator_defer", "bonus_hit"},
}
_MODE_DEGRADE = {  # safe-but-not-found / deferred label per mode
    "static": "unsorted_miss",
    "dyn_kw": "term_match_miss",
    "dyn_nat": "allocator_defer",
}


def write_verdict(case: StressCase, *, mode: str) -> Dict:
    lobe = case.lobe
    grammar_y = subdomain_for(case.text, lobe)
    dyn = DYNAMIC_Y_LOBES if mode in ("dyn_kw", "dyn_nat") else frozenset()
    write_coord, undecided = simulate_write_coord(case.text, lobe, dynamic_y_lobes=dyn)
    write_y = int(write_coord[1])
    exp = case.expected_y

    if mode == "dyn_kw":
        if grammar_y is None:
            verdict = "term_match_miss"
        elif grammar_y == exp:
            verdict = "correct"
        else:
            verdict = "misroute"
    elif mode == "dyn_nat":
        if grammar_y is None:
            verdict = "allocator_defer"
        elif grammar_y == exp:
            verdict = "bonus_hit"
        else:
            verdict = "misroute"
    else:  # static
        if exp is None:
            verdict = "widen_correct" if grammar_y is None else "over_trigger"
        elif grammar_y is None:
            verdict = "unsorted_miss"
        elif grammar_y == exp:
            verdict = "correct"
        else:
            verdict = "misroute"

    return {
        "angle": case.angle,
        "lobe": lobe,
        "text": case.text,
        "expected_y": exp,
        "grammar_y": grammar_y,
        "write_y": write_y,
        "undecided": undecided,
        "verdict": verdict,
        "note": case.note,
    }


def section_write(title: str, cases: List[StressCase], *, mode: str) -> Dict:
    rows = [write_verdict(c, mode=mode) for c in cases]
    counts = Counter(r["verdict"] for r in rows)
    n = len(rows)
    good_set = _MODE_GOOD[mode]
    degrade = _MODE_DEGRADE[mode]

    print(f"\n{'═' * 78}\n{title}  (n={n})\n{'═' * 78}")
    for v in _MODE_ORDER[mode]:
        if counts.get(v):
            print(f"  {v:16s} {counts[v]:3d}  {_pct(counts[v], n)}")

    # Per-lobe verdict breakdown.
    by_lobe: Dict[int, Counter] = defaultdict(Counter)
    for r in rows:
        by_lobe[r["lobe"]][r["verdict"]] += 1
    print("\n  per-lobe:")
    for lobe in sorted(by_lobe):
        lc = by_lobe[lobe]
        tot = sum(lc.values())
        good = sum(lc.get(v, 0) for v in good_set)
        print(
            f"    L{lobe:<2d} {LOBE_MAP.get(lobe, '?'):16s} "
            f"ok={good}/{tot} ({_pct(good, tot)})  "
            f"misroute={lc.get('misroute', 0)}  over_trigger={lc.get('over_trigger', 0)}  "
            f"{degrade}={lc.get(degrade, 0)}"
        )

    # Surface every dangerous verdict explicitly.
    dangerous = [r for r in rows if r["verdict"] in ("misroute", "over_trigger")]
    if dangerous:
        print("\n  ⚠ misroute / over_trigger detail:")
        for r in dangerous:
            print(
                f"    L{r['lobe']} exp={_sub_name(r['lobe'], r['expected_y'])} "
                f"got={_sub_name(r['lobe'], r['grammar_y'])} :: {r['text']!r}"
            )

    return {"title": title, "n": n, "counts": dict(counts), "rows": rows}


def section_recall(cases: List[StressCase]) -> Dict:
    print(
        f"\n{'═' * 78}\nrecall_query — route_memory_tool lobe routing  (n={len(cases)})\n{'═' * 78}"
    )
    hits = 0
    rows = []
    for c in cases:
        res = route_memory_tool(c.text)
        got = int((res.get("allowed_lobes") or [2])[0])
        ok = got == c.lobe  # recall cases carry the expected LOBE in `lobe`
        hits += int(ok)
        rows.append(
            {
                "text": c.text,
                "expected_lobe": c.lobe,
                "got_lobe": got,
                "tool": res.get("tool"),
                "ok": ok,
                "note": c.note,
            }
        )
    print(f"  recall lobe accuracy: {hits}/{len(cases)}  {_pct(hits, len(cases))}")
    print("\n  misses:")
    for r in rows:
        if not r["ok"]:
            print(
                f"    want L{r['expected_lobe']} got L{r['got_lobe']} "
                f"({r['tool']}) :: {r['text']!r}" + (f"  — {r['note']}" if r["note"] else "")
            )
    return {"title": "recall_query", "n": len(cases), "hits": hits, "rows": rows}


def section_symmetry(cases: List[StressCase]) -> Dict:
    print(
        f"\n{'═' * 78}\nsymmetry_realistic — write-Y vs grammar-on-recall-query  (n={len(cases)})\n{'═' * 78}"
    )
    write_reach = recall_reach = full_sym = 0
    rows = []
    for c in cases:
        gw = subdomain_for(c.text, c.lobe)
        gr = subdomain_for(c.recall_query or "", c.lobe)
        wr = gw == c.expected_y
        rr = gr == c.expected_y
        fs = wr and rr and gw == gr
        write_reach += int(wr)
        recall_reach += int(rr)
        full_sym += int(fs)
        rows.append(
            {
                "lobe": c.lobe,
                "write": c.text,
                "recall": c.recall_query,
                "expected_y": c.expected_y,
                "write_y": gw,
                "recall_y": gr,
                "write_reach": wr,
                "recall_reach": rr,
                "full_sym": fs,
            }
        )
    n = len(cases)
    print(f"  write reaches target Y:           {write_reach}/{n}  {_pct(write_reach, n)}")
    print(f"  recall query reaches same Y:      {recall_reach}/{n}  {_pct(recall_reach, n)}")
    print(f"  FULL write↔recall symmetry:       {full_sym}/{n}  {_pct(full_sym, n)}  ◀ findability")
    print("\n  recall phrasing that does NOT reach the write target:")
    for r in rows:
        if not r["recall_reach"]:
            print(
                f"    L{r['lobe']} want={_sub_name(r['lobe'], r['expected_y'])} "
                f"recall_got={_sub_name(r['lobe'], r['recall_y'])} :: {r['recall']!r}"
            )
    return {
        "title": "symmetry_realistic",
        "n": n,
        "write_reach": write_reach,
        "recall_reach": recall_reach,
        "full_sym": full_sym,
        "rows": rows,
    }


def section_cross_lobe(cases: List[StressCase]) -> Dict:
    print(
        f"\n{'═' * 78}\ncross_lobe — ambiguous landing (route_memory_tool)  (n={len(cases)})\n{'═' * 78}"
    )
    plausible = 0
    rows = []
    for c in cases:
        res = route_memory_tool(c.text)
        got = int((res.get("allowed_lobes") or [2])[0])
        ok = got in c.plausible_lobes
        plausible += int(ok)
        rows.append(
            {
                "text": c.text,
                "plausible": c.plausible_lobes,
                "got_lobe": got,
                "tool": res.get("tool"),
                "plausible_ok": ok,
                "note": c.note,
            }
        )
        flag = "ok" if ok else "⚠ outside plausible set"
        print(
            f"  L{got:<2d} ({res.get('tool')}) [{flag}] plausible={c.plausible_lobes} :: {c.text!r}"
        )
    print(
        f"\n  landed in a plausible lobe: {plausible}/{len(cases)}  {_pct(plausible, len(cases))}"
    )
    return {"title": "cross_lobe", "n": len(cases), "plausible": plausible, "rows": rows}


def section_paraphrase(cases: List[StressCase]) -> Dict:
    print(
        f"\n{'═' * 78}\nparaphrase — intra-group write-coord agreement  (n={len(cases)})\n{'═' * 78}"
    )
    groups: Dict[str, List[StressCase]] = defaultdict(list)
    for c in cases:
        groups[c.group or "_"].append(c)
    consistent = 0
    rows = []
    for g, members in groups.items():
        coords = []
        for m in members:
            coord, _ = simulate_write_coord(m.text, m.lobe, dynamic_y_lobes=frozenset())
            coords.append(int(coord[1]))
        unique = sorted(set(coords))
        agree = len(unique) == 1
        consistent += int(agree)
        target = members[0].expected_y
        on_target = all(cy == target for cy in coords)
        rows.append(
            {
                "group": g,
                "lobe": members[0].lobe,
                "target_y": target,
                "coords": coords,
                "agree": agree,
                "on_target": on_target,
            }
        )
        status = "stable" if agree else f"SPLIT → Ys {unique}"
        tgt = "on-target" if on_target else "off-target"
        print(
            f"  {g:8s} L{members[0].lobe} target={_sub_name(members[0].lobe, target)}: {status} ({tgt})"
        )
        if not agree:
            for m, cy in zip(members, coords):
                mark = "·" if cy == target else "✗"
                print(f"      {mark} y={_sub_name(m.lobe, cy)} :: {m.text!r}")
    print(
        f"\n  groups whose paraphrases all land together: {consistent}/{len(groups)}  {_pct(consistent, len(groups))}"
    )
    return {"title": "paraphrase", "groups": len(groups), "consistent": consistent, "rows": rows}


def section_noise(cases: List[StressCase]) -> Dict:
    rows = [write_verdict(c, mode="static") for c in cases]
    n = len(rows)
    retained = sum(1 for r in rows if r["verdict"] in ("correct", "widen_correct"))
    print(f"\n{'═' * 78}\nnoise — routing retention under perturbation  (n={n})\n{'═' * 78}")
    print(f"  retained correct/intended routing: {retained}/{n}  {_pct(retained, n)}")
    print("\n  perturbations that broke routing:")
    for r in rows:
        if r["verdict"] not in ("correct", "widen_correct"):
            print(
                f"    [{r['verdict']}] exp={_sub_name(r['lobe'], r['expected_y'])} "
                f"got={_sub_name(r['lobe'], r['grammar_y'])} :: {r['text']!r}"
                + (f"  — {r['note']}" if r["note"] else "")
            )
    return {"title": "noise", "n": n, "retained": retained, "rows": rows}


def section_negation(cases: List[StressCase]) -> Dict:
    n = len(cases)
    over = 0
    rows = []
    for c in cases:
        grammar_y = subdomain_for(c.text, c.lobe)
        triggered = grammar_y is not None  # ideal here is None (widen)
        over += int(triggered)
        rows.append(
            {
                "text": c.text,
                "lobe": c.lobe,
                "grammar_y": grammar_y,
                "over_triggered": triggered,
                "note": c.note,
            }
        )
    print(f"\n{'═' * 78}\nnegation / hypothetical — lexical over-trigger rate  (n={n})\n{'═' * 78}")
    print(f"  grammar over-triggered on negated keyword: {over}/{n}  {_pct(over, n)}  (ideal 0)")
    for r in rows:
        if r["over_triggered"]:
            print(
                f"    L{r['lobe']} → {_sub_name(r['lobe'], r['grammar_y'])} :: {r['text']!r}  — {r['note']}"
            )
    return {"title": "negation", "n": n, "over_trigger": over, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", type=str, default=None, help="write full results to this JSON path")
    args = ap.parse_args()

    print("#" * 78)
    print("# DETERMINISTIC MEMORY ROUTER — MULTI-ANGLE STRESS REPORT")
    print(f"# corpus: {len(all_stress_cases())} semantically-labelled cases across 9 angles")
    print("#" * 78)

    results = {}
    results["write_static"] = section_write(
        "write_static — free-text static-lobe writes", WRITE_STATIC, mode="static"
    )
    results["write_dynamic_kw"] = section_write(
        "write_dynamic_kw — domain word PRESENT (lobes 2/8)", WRITE_DYNAMIC_KW, mode="dyn_kw"
    )
    results["write_dynamic_nat"] = section_write(
        "write_dynamic_nat — natural text, NO domain word (lobes 2/8)",
        WRITE_DYNAMIC_NAT,
        mode="dyn_nat",
    )
    results["recall_query"] = section_recall(RECALL_QUERY)
    results["symmetry_realistic"] = section_symmetry(SYMMETRY_REALISTIC)
    results["cross_lobe"] = section_cross_lobe(CROSS_LOBE)
    results["paraphrase"] = section_paraphrase(PARAPHRASE)
    results["noise"] = section_noise(NOISE)
    results["negation"] = section_negation(NEGATION)

    # ── Headline summary ──
    ws = results["write_static"]
    ws_n = ws["n"]
    ws_correct = ws["counts"].get("correct", 0) + ws["counts"].get("widen_correct", 0)
    ws_misroute = ws["counts"].get("misroute", 0) + ws["counts"].get("over_trigger", 0)
    ws_unsorted = ws["counts"].get("unsorted_miss", 0)
    sym = results["symmetry_realistic"]

    print(f"\n{'#' * 78}\n# HEADLINE\n{'#' * 78}")
    print(f"  static-lobe write correctness:   {ws_correct}/{ws_n}  {_pct(ws_correct, ws_n)}")
    print(
        f"  static-lobe SILENT misroutes:    {ws_misroute}/{ws_n}  {_pct(ws_misroute, ws_n)}  ◀ dangerous"
    )
    print(
        f"  static-lobe safe→_unsorted:      {ws_unsorted}/{ws_n}  {_pct(ws_unsorted, ws_n)}  (recoverable)"
    )
    dk = results["write_dynamic_kw"]
    dk_ok = dk["counts"].get("correct", 0)
    print(
        f"  term-match coverage (word present): {dk_ok}/{dk['n']}  {_pct(dk_ok, dk['n'])}  (lobes 2/8 full taxonomy)"
    )
    print(
        f"  recall lobe accuracy:            {results['recall_query']['hits']}/{results['recall_query']['n']}  {_pct(results['recall_query']['hits'], results['recall_query']['n'])}"
    )
    print(
        f"  write↔recall FINDABILITY:        {sym['full_sym']}/{sym['n']}  {_pct(sym['full_sym'], sym['n'])}  ◀ the downstream-killer metric"
    )
    print(
        f"  negation over-trigger:           {results['negation']['over_trigger']}/{results['negation']['n']}  {_pct(results['negation']['over_trigger'], results['negation']['n'])}"
    )
    print(
        f"  paraphrase group stability:      {results['paraphrase']['consistent']}/{results['paraphrase']['groups']}  {_pct(results['paraphrase']['consistent'], results['paraphrase']['groups'])}"
    )
    print(
        f"  noise retention:                 {results['noise']['retained']}/{results['noise']['n']}  {_pct(results['noise']['retained'], results['noise']['n'])}"
    )

    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

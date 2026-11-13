"""The 1000-scenario routing grill — 7th-grade grammar teacher with a vengeance.

Part A (default): ~1,040 generated scenarios = 40 semantic frames x ~26
grammatical angles (tense, voice, embedding, quoting, hedges, caps abuse,
typos, negation, hypotheticals, run-ons, contractions...). Graded against the
LIVE routing authority ``subdomain_for``:
    PASS      — landed on an acceptable outcome
    WIDEN(C)  — safely widened where a subdomain was expected (recoverable:
                whole-lobe recall + the _unsorted widen still find it)
    MISROUTE(F) — confidently wrong subdomain (the failure that kills recall)

Part B (--live): injects frames through the REAL pipeline — write_memory
(live embed when the brain server is up, skip_embed fallback) -> reindex +
graph compile -> production invoke_remember_tool recall — under an ISOLATED
memory root (the operator's live store is never touched).

Usage:
    .\\.venv-brain\\Scripts\\python.exe scripts\\routing_grill_1000.py            # Part A
    .\\.venv-brain\\Scripts\\python.exe scripts\\routing_grill_1000.py --live     # A + B
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from auto_loop.mril_tools.subdomain_helpers import subdomain_for  # noqa: E402


@dataclass(frozen=True)
class Frame:
    lobe: int
    y: int
    plain: str
    question: str
    neg: Optional[str] = None  # phrasing whose truth is "widen"
    loose_pron: bool = False  # pronoun-dependent lobe (0/1): negation
    # phrasings may still legitimately land


FRAMES: List[Frame] = [
    # ── L0 identity ──
    Frame(
        0,
        0,
        "i prefer local-first designs over cloud services",
        "what does the operator prefer for infrastructure",
        loose_pron=True,
    ),
    Frame(
        0,
        4,
        "you must never share the vault contents",
        "what are the hard rules about the vault",
        loose_pron=True,
    ),
    Frame(0, 2, "my defining traits are patience and rigor", "what are my traits", loose_pron=True),
    Frame(
        0,
        3,
        "my self-model has evolved toward caution",
        "how has the self-model evolved",
        loose_pron=True,
    ),
    Frame(
        0,
        1,
        "she is my sister and visits on weekends",
        "who is the operator's sister",
        loose_pron=True,
    ),
    # ── L1 personas ──
    Frame(1, 1, "Sarah runs the lab next door", "who is Sarah", loose_pron=True),
    Frame(
        1, 0, "i prefer terse direct answers", "what kind of answers do i prefer", loose_pron=True
    ),
    Frame(1, 1, "he is my father and lives in Leeds", "who is my father", loose_pron=True),
    Frame(1, 0, "i work best in the early morning", "when do i work best", loose_pron=True),
    Frame(
        1,
        1,
        "they mentor me on distributed systems",
        "who mentors me on distributed systems",
        loose_pron=True,
    ),
    # ── L3 reasoning ──
    Frame(
        3,
        0,
        "the goal is to ship the memory engine",
        "what is the goal for the engine",
        neg="there is no goal for this sprint",
    ),
    Frame(
        3,
        1,
        "we decided to keep everything local",
        "what did we decide about hosting",
        neg="we never decided to use the cloud",
    ),
    Frame(
        3, 2, "the constraint is no cloud dependencies", "what constraints apply to dependencies"
    ),
    Frame(
        3,
        3,
        "our rule of thumb is to measure before optimizing",
        "what is our rule of thumb for optimization",
    ),
    Frame(3, 4, "privacy matters more than convenience", "what are our core values"),
    # ── L4 situational ──
    Frame(
        4, 6, "the deploy failed at 3am and we rolled back", "what was the outcome of the deploy"
    ),
    Frame(
        4,
        7,
        "there was real anxiety before the launch",
        "how did everyone feel before the launch",
        neg="there was no anxiety before the launch",
    ),
    Frame(
        4,
        6,
        "the build finally succeeded after the gate fix",
        "what was the result of the gate fix",
    ),
    Frame(4, 7, "everyone was relieved once it booted clean", "how did the team feel after boot"),
    Frame(
        4, 0, "who approved the emergency change is unclear", "who approved the emergency change"
    ),
    # ── L5 tools ──
    Frame(
        5,
        0,
        "the web fetch tool queries the catalog",
        "what tool queries the catalog",
        neg="no tool queries the catalog yet",
    ),
    Frame(5, 1, "the build pipeline compiles the runtime", "which pipeline compiles the runtime"),
    Frame(5, 0, "the helper that wraps the model endpoint", "what wraps the model endpoint"),
    Frame(
        5, 1, "we use git for version control in dev", "what do we use for version control in dev"
    ),
    Frame(5, 0, "the search tool browses local indexes", "which tool browses the indexes"),
    # ── L7 vision ──
    Frame(
        7,
        1,
        "a face appeared at the front door camera",
        "whose face was at the door",
        neg="no face was detected at the door",
    ),
    Frame(7, 2, "the scene shows a crowded server room", "what scene did the camera show"),
    Frame(7, 0, "the photo from the demo is stored", "where is the photo from the demo"),
    Frame(7, 3, "the visual patterns repeat every frame", "what patterns appear in the frames"),
    Frame(7, 0, "the screen displays the boot sequence", "what was on the screen during boot"),
    # ── L9 temporal ──
    Frame(
        9,
        0,
        "the session covered memory routing",
        "what did the session cover",
        neg="the session never actually started",
    ),
    Frame(
        9,
        2,
        "we shipped the first beta on tuesday",
        "when did we ship the first beta",
        neg="we never shipped the beta",
    ),
    Frame(9, 1, "the outage event happened at midnight", "what event happened at midnight"),
    Frame(9, 3, "the history of routing changes is long", "what is the history of routing changes"),
    Frame(9, 2, "the launch went live yesterday", "when did the launch go live"),
    # ── L11 dev ──
    Frame(
        11,
        0,
        "the build produced a signed apk",
        "where is the build artifact",
        neg="the build never produced an artifact",
    ),
    Frame(11, 1, "we still need to wire the loader", "what progress remains on the loader"),
    Frame(11, 3, "the roadmap targets a q3 release", "what does the roadmap target"),
    Frame(11, 2, "the background setup uses a portable ssd", "what is the setup background"),
    Frame(11, 1, "the status update says half done", "what does the status update say"),
]

# ── Angles ── each returns (text, acceptable_set) or None to skip.
Angle = Tuple[str, Callable[[Frame], Optional[Tuple[str, Set[Optional[int]]]]]]


def _typo(word_text: str) -> str:
    words = word_text.split()
    longest = max(range(len(words)), key=lambda i: len(words[i]))
    w = words[longest]
    if len(w) > 4:
        mid = len(w) // 2
        w = w[: mid - 1] + w[mid] + w[mid - 1] + w[mid + 1 :]
    words[longest] = w
    return " ".join(words)


def _ok(f: Frame) -> Set[Optional[int]]:
    return {f.y}


def _ok_or_widen(f: Frame) -> Set[Optional[int]]:
    return {f.y, None}


ANGLES: List[Angle] = [
    ("plain", lambda f: (f.plain, _ok(f))),
    ("question", lambda f: (f.question, _ok(f))),
    ("past_marker", lambda f: (f"yesterday, {f.plain}", _ok(f))),
    ("future", lambda f: (f"starting tomorrow, {f.plain}", _ok(f))),
    ("embedded", lambda f: (f"one thing worth writing down is that {f.plain}", _ok(f))),
    ("quoted", lambda f: (f'the note said "{f.plain}"', _ok(f))),
    ("filler", lambda f: (f"ok so, real quick: {f.plain}", _ok(f))),
    ("hedge", lambda f: (f"{f.plain}, i think", _ok(f))),
    ("reminder", lambda f: (f"reminder: {f.plain}", _ok(f))),
    ("because_lead", lambda f: (f"because {f.plain}, we took notes", _ok(f))),
    ("while_lead", lambda f: (f"while we were busy, {f.plain}", _ok(f))),
    ("runon", lambda f: (f"{f.plain} and also the weather was nice and we had coffee", _ok(f))),
    ("caps_abuse", lambda f: (f.plain.upper(), _ok(f))),
    (
        "random_case",
        lambda f: (
            "".join(c.upper() if i % 2 else c.lower() for i, c in enumerate(f.plain)),
            _ok(f),
        ),
    ),
    ("lowercase", lambda f: (f.plain.lower(), _ok_or_widen(f) if f.lobe == 1 else _ok(f))),
    ("emoji_noise", lambda f: (f"{f.plain} 🚀!!!", _ok(f))),
    ("whitespace", lambda f: (f"   {f.plain}  ...   ", _ok(f))),
    ("polite_q", lambda f: (f"could you tell me {f.question}", _ok(f))),
    ("apparently", lambda f: (f"apparently {f.plain}", _ok(f))),
    ("contraction", lambda f: (f.plain.replace(" is ", "'s ").replace(" are ", "'re "), _ok(f))),
    ("typo", lambda f: (_typo(f.plain), _ok_or_widen(f))),
    ("soft_not", lambda f: (f"{f.plain}, not that it matters", _ok(f))),
    ("double_neg", lambda f: (f"it is not untrue that {f.plain}", _ok_or_widen(f))),
    # widen-expected angles (content lobes only; pronoun lobes legitimately
    # keep routing on the pronoun subject):
    ("hypothetical", lambda f: None if f.loose_pron else (f"imagine if {f.plain}", {None})),
    ("suppose", lambda f: None if f.loose_pron else (f"suppose {f.plain}", {None})),
    ("what_if", lambda f: None if f.loose_pron else (f"what if {f.plain}", {None})),
    ("negated", lambda f: (f.neg, {None}) if f.neg else None),
]


def part_a() -> int:
    per_angle = defaultdict(Counter)
    per_lobe = defaultdict(Counter)
    misroutes: List[str] = []
    widens: List[str] = []
    total = 0

    for f in FRAMES:
        for name, fn in ANGLES:
            case = fn(f)
            if case is None:
                continue
            text, accept = case
            total += 1
            got = subdomain_for(text, f.lobe)
            if got in accept:
                verdict = "pass"
            elif got is None:
                verdict = "widen"
                widens.append(
                    f"L{f.lobe} exp={sorted(str(a) for a in accept)} :: [{name}] {text!r}"
                )
            else:
                verdict = "misroute"
                misroutes.append(
                    f"L{f.lobe} exp={sorted(str(a) for a in accept)} got={got} :: [{name}] {text!r}"
                )
            per_angle[name][verdict] += 1
            per_lobe[f.lobe][verdict] += 1

    print("#" * 78)
    print("# PART A — GRAMMAR GRILL")
    print(f"# {total} scenarios = {len(FRAMES)} frames x {len(ANGLES)} angles")
    print("#" * 78)
    print(f"\n{'angle':<14} {'pass':>5} {'widen(C)':>9} {'MISROUTE(F)':>12}")
    for name, _ in ANGLES:
        c = per_angle[name]
        print(f"{name:<14} {c['pass']:>5} {c['widen']:>9} {c['misroute']:>12}")
    print(f"\n{'lobe':<6} {'pass':>5} {'widen(C)':>9} {'MISROUTE(F)':>12}")
    for lobe in sorted(per_lobe):
        c = per_lobe[lobe]
        print(f"L{lobe:<5} {c['pass']:>5} {c['widen']:>9} {c['misroute']:>12}")

    n_pass = sum(c["pass"] for c in per_angle.values())
    n_widen = sum(c["widen"] for c in per_angle.values())
    n_mis = sum(c["misroute"] for c in per_angle.values())
    print("\n" + "=" * 78)
    print(
        f"TOTAL: {total}   PASS {n_pass} ({100 * n_pass / total:.1f}%)   "
        f"WIDEN {n_widen} ({100 * n_widen / total:.1f}%)   "
        f"MISROUTE {n_mis} ({100 * n_mis / total:.1f}%)"
    )
    print("=" * 78)
    if misroutes:
        print(f"\n-- EVERY MISROUTE (F) — {len(misroutes)} --")
        for m in misroutes:
            print("  " + m)
    if widens:
        print(f"\n-- WIDENS (C) — {len(widens)} (first 25) --")
        for w in widens[:25]:
            print("  " + w)
    return n_mis


def part_b() -> None:
    """Live injection: real write -> compile -> production recall, isolated root."""
    import tempfile

    spec = importlib.util.spec_from_file_location("mlc", ROOT / "scripts" / "memory_live_check.py")
    mlc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mlc)

    print("\n" + "#" * 78)
    print("# PART B — LIVE INJECTION (isolated root; live embed when server is up)")
    print("#" * 78)

    with tempfile.TemporaryDirectory() as d:
        mlc._isolate_memory_root(Path(d))

        from auto_loop.mril_tools.registry import REMEMBER_TOOL_REGISTRY
        from auto_loop.mril_tools.remember import invoke_remember_tool
        from memory.api import CanonicalEnvelope, write_memory
        from memory.engine.reindex import reindex_and_compile

        tool_by_lobe = {lobe: tool for tool, (lane, lobe, mt) in REMEMBER_TOOL_REGISTRY.items()}

        written, write_fail, live_embed = [], [], 0
        for i, f in enumerate(FRAMES):
            suffix = uuid.uuid4().hex[:8]
            env = CanonicalEnvelope(
                cycle_id=f"grill-{i}-{suffix}",
                coord=[f.lobe, f.y, 0],
                type="semantic",
                payload={"title": f"grill {i}", "summary": f.plain, "text": f.plain},
                source="memory.api",
            )
            receipt = None
            try:
                receipt = write_memory(env)  # live embed path
                live_embed += 1
            except Exception:
                try:
                    receipt = write_memory(env, skip_embed=True)
                except Exception as exc:
                    write_fail.append(f"L{f.lobe} y{f.y} :: {type(exc).__name__}: {exc}")
            mid = getattr(receipt, "memory_id", None) or getattr(receipt, "id", None)
            if receipt is not None and getattr(receipt, "committed", True):
                written.append((f, mid))

        try:
            reindex_and_compile()
        except Exception as exc:
            print(f"  reindex/compile: {type(exc).__name__}: {exc}")

        found, missed = 0, []
        for f, mid in written:
            tool = tool_by_lobe.get(f.lobe)
            if tool is None:
                continue
            entry = {
                "tool": tool,
                "query": f.question,
                "allowed_lobes": [f.lobe],
                "memory_type": REMEMBER_TOOL_REGISTRY[tool][2],
                "lane": REMEMBER_TOOL_REGISTRY[tool][0],
                "lobe": f.lobe,
            }
            y = subdomain_for(f.question, f.lobe)
            if y is not None:
                entry["subdomain"] = y
            out = invoke_remember_tool(entry, f"grill-recall-{mid}")
            hits = {str(e.get("memory_id")) for e in out.get("evidence", [])}
            summaries = " ".join(str(e.get("remembered_as", "")) for e in out.get("evidence", []))
            if (mid and str(mid) in hits) or (f.plain[:25].lower() in summaries.lower()):
                found += 1
            else:
                missed.append(
                    f"L{f.lobe} y{f.y} q={f.question!r} widened={out.get('widened_unsorted')}"
                )

        print(f"\n  writes: {len(written)} ok ({live_embed} live-embed), {len(write_fail)} failed")
        for wf in write_fail:
            print("    WRITE-FAIL " + wf)
        denom = max(len(written), 1)
        print(f"  ROUNDTRIP FINDABILITY: {found}/{len(written)} ({100 * found / denom:.1f}%)")
        for m in missed:
            print("    MISS " + m)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="run Part B live injection")
    args = ap.parse_args()
    fails = part_a()
    if args.live:
        part_b()
    sys.exit(0)

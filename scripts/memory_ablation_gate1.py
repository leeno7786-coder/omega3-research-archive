"""Gate-1 ablation assault — DME v2 (Thought Engine) measured mode by mode.

Operator spec (2026-07-04): an ABLATION, not just a score. Same corpus, five
modes; alias cases split into two groups with opposite success criteria:

  group A (LINKED)   alias in a separate session, compiler/graph edges exist
                     → hop planes must RECOVER
  group B (ISOLATED) no shared coordinate, no edge
                     → honest UNKNOWN/miss; a "recovery" here is a
                       HALLUCINATION and counts against us

Modes:
  A  strict intersection only            (the old DME v2 behavior)
  B  graded union overlap                (hops=0)
  C  adaptive weighted classed planes    (hops=2 — THE production config)
  D  classed planes + truth edges        (hops=3 exploratory, engine ceiling)
  E  full production path + verifier gate

RATIFIED protocol (2026-07-04): every mode runs TWICE and must be
byte-stable (identical verdicts + admitted/blocked leaf ids); Mode E is
latency-decomposed (recall / dme / verify / embed legs); the report closes
with the ratified acceptance gates (C: hit@1>=88, aliasL>=90, para>=75,
falseC<65, <0.5ms; E: hit@1>=92, para>=85, aliasL>=90, invariants 0-0-0).

Non-negotiable invariant, verbatim: DMEv2 is allowed to be noisy. Verifier is
not. Hop-scored search only produces ranked candidates; Omega acts only on
verified memory.

Isolated roots; the day-zero'd live store is never touched.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from auto_loop.mril_tools.subdomain_helpers import subdomain_for  # noqa: E402


# ── corpus (fresh theme: observatory / harbor — out-of-corpus again) ─────────


@dataclass(frozen=True)
class Target:
    lobe: int
    y: int
    write: str
    direct_q: str
    para_q: str


TARGETS: List[Target] = [
    Target(
        3,
        0,
        "the objective is to map the eastern reef by autumn",
        "what is the objective for the eastern reef",
        "what are we aiming for with the east reef",
    ),
    Target(
        3,
        1,
        "we decided to moor the survey barge at pier nine",
        "what did we decide about the survey barge",
        "where did we choose to tie up the barge",
    ),
    Target(
        3,
        2,
        "the constraint is a two-boat limit in the channel",
        "what constraints apply in the channel",
        "what is the hard limit for boats in the channel",
    ),
    Target(
        4,
        6,
        "the night dive succeeded despite the current",
        "what was the outcome of the night dive",
        "how did the after-dark dive turn out",
    ),
    Target(
        4,
        7,
        "there was real pride after the lighthouse repair",
        "how did the crew feel after the lighthouse repair",
        "what was morale like once the lighthouse was fixed",
    ),
    Target(
        5,
        0,
        "the tide chart tool predicts the harbor windows",
        "what tool predicts the harbor windows",
        "which utility forecasts when the harbor is open",
    ),
    Target(
        5,
        1,
        "the sonar build pipeline compiles overnight",
        "which pipeline compiles the sonar build",
        "what runs the overnight sonar compilation",
    ),
    Target(
        7,
        1,
        "an unknown face appeared near the dry dock camera",
        "whose face was near the dry dock",
        "who showed up on the dry dock feed",
    ),
    Target(
        7,
        2,
        "the scene shows fog rolling over the breakwater",
        "what scene did the camera capture at the breakwater",
        "what did the breakwater footage show",
    ),
    Target(
        9,
        0,
        "the tuesday session planned the buoy replacement",
        "what did the tuesday session cover",
        "what got planned in tuesday's meeting",
    ),
    Target(
        9,
        2,
        "we launched the ferry route on the solstice",
        "when did we launch the ferry route",
        "when did the ferry service go live",
    ),
    Target(
        9,
        1,
        "the grounding event happened at low tide",
        "what event happened at low tide",
        "what occurred when the tide was out",
    ),
    Target(
        11,
        0,
        "the chart build produced a signed depth atlas",
        "where is the depth atlas from the chart build",
        "what artifact came out of the chart compilation",
    ),
    Target(
        11,
        1,
        "we still need to splice the mooring sensor line",
        "what progress remains on the mooring sensor",
        "what work is left on the mooring line sensor",
    ),
    Target(
        11,
        3,
        "the roadmap targets full harbor telemetry by spring",
        "what does the roadmap target for spring",
        "what is the long-term plan for harbor telemetry",
    ),
    Target(
        0,
        2,
        "my defining traits are thoroughness and salt-stubbornness",
        "what are my defining traits",
        "what characteristics define me",
    ),
    Target(
        0,
        4,
        "you must always log the tide before casting off",
        "what are the hard rules about casting off",
        "what must always happen before we cast off",
    ),
    Target(
        1,
        1,
        "Ingrid maintains the harbor master's ledger",
        "who maintains the harbor master's ledger",
        "who keeps the harbormaster books",
    ),
    Target(
        3,
        3,
        "our rule of thumb is reef checks before charts",
        "what is our rule of thumb for surveys",
        "what do we always do before charting",
    ),
    Target(
        4,
        0,
        "who left the winch unlocked is still unclear",
        "who left the winch unlocked",
        "who forgot to lock the winch",
    ),
    Target(
        5,
        0,
        "the wrapper around the weather buoy endpoint caches readings",
        "what wraps the weather buoy endpoint",
        "which helper caches the buoy readings",
    ),
    Target(
        9,
        3,
        "the history of the channel dredging spans decades",
        "what is the history of the channel dredging",
        "how far back does the dredging record go",
    ),
    Target(
        7,
        0,
        "the monitor displays the lock gate feed",
        "what is on the lock gate monitor",
        "what does the lock gate screen show",
    ),
    Target(
        11,
        2,
        "the background setup runs on the boathouse server",
        "what is the setup background for the boathouse",
        "where does the boathouse system run",
    ),
]


# LINKED alias triples: target (technical) + bridge (shares the entity token,
# carries the alias vocabulary) + alias query. Distinctive entity tokens give
# the compiler its best extraction shot; ground-truth edges are ALSO injected
# in the D-store so engine ceiling and compiler reality are measured apart.
@dataclass(frozen=True)
class LinkedAlias:
    lobe: int
    y: int
    target: str
    bridge: str
    alias_q: str
    entity: str


LINKED: List[LinkedAlias] = [
    LinkedAlias(
        5,
        0,
        "the Aegir-9 pump controller regulates ballast flow",
        "the crew calls the Aegir-9 the ballast balancer",
        "what gadget balances the ballast",
        "Aegir-9",
    ),
    LinkedAlias(
        5,
        0,
        "the Meridian scope tracks vessel headings",
        "the Meridian scope is our heading watcher",
        "what device watches the headings",
        "Meridian",
    ),
    LinkedAlias(
        11,
        0,
        "the Delta-4 build emitted the sounding bundle",
        "the Delta-4 output is nicknamed the depth pack",
        "where is the depth pack",
        "Delta-4",
    ),
    LinkedAlias(
        9,
        2,
        "the Nordlys ferry launched on the solstice",
        "the Nordlys is the midnight-sun boat",
        "when did the midnight-sun boat start running",
        "Nordlys",
    ),
    LinkedAlias(
        3,
        1,
        "we decided the Kraken winch gets the new cable",
        "the Kraken winch is the big lifter on pier two",
        "what did we decide about the big lifter",
        "Kraken",
    ),
    LinkedAlias(
        7,
        1,
        "the Argus camera caught a face at the fuel dock",
        "the Argus is the fuel dock eye",
        "whose face did the fuel dock eye catch",
        "Argus",
    ),
    LinkedAlias(
        4,
        6,
        "the Poseidon trial run succeeded in heavy swell",
        "the Poseidon trial is the swell test",
        "what was the outcome of the swell test",
        "Poseidon",
    ),
    LinkedAlias(
        9,
        0,
        "the Beacon session planned the winter haul-out",
        "the Beacon session is the friday planning hour",
        "what did the friday planning hour cover",
        "Beacon",
    ),
    LinkedAlias(
        11,
        1,
        "the Fathom sensor line still needs splicing",
        "the Fathom line is the depth string under pier one",
        "what work remains on the depth string",
        "Fathom",
    ),
    LinkedAlias(
        3,
        2,
        "the Tempest rule limits departures to daylight",
        "the Tempest rule is the storm curfew",
        "what does the storm curfew restrict",
        "Tempest",
    ),
]

# ISOLATED alias pairs: no bridge, no shared coordinate vocabulary. Honest
# behavior = UNKNOWN/miss. A verified admission of a WRONG memory here is a
# hallucination and counts against the run.
ISOLATED: List[Tuple[Target, str]] = [
    (
        Target(5, 0, "the salinity probe samples the estuary hourly", "", ""),
        "what instrument checks the river mouth salt levels",
    ),
    # 2026-07-04 corpus correction: the original wording "boat shop" punned
    # into the Nordlys bridge's "midnight-sun boat" — violating this group's
    # own definition ("no shared coordinate vocabulary") and making the query
    # structurally identical to a TRUE alias recovery. Receipts:
    # scripts/memory_iso_autopsy.py + war plan session record.
    (
        Target(9, 2, "the chandlery opened its seasonal stall in may", "", ""),
        "when did the gear seller start trading once more",
    ),
    (
        Target(4, 7, "the apprentices were anxious before the mast stepping", "", ""),
        "how was the mood ahead of raising the spar",
    ),
    # same correction class: "the output" punned into the Delta-4 bridge's
    # "the Delta-4 output" (its literal head noun) — a near-alias, not an
    # isolated query.
    (
        Target(11, 0, "the render job generated the berth occupancy map", "", ""),
        "where is the tally of occupied anchorage spots",
    ),
    (
        Target(3, 0, "the objective is to certify twelve new pilots", "", ""),
        "what is the aim for training this cohort",
    ),
    (
        Target(7, 2, "the scene shows gulls swarming the fish return", "", ""),
        "what did the footage capture at the discard chute",
    ),
    (
        Target(5, 1, "the manifest pipeline stamps every cargo entry", "", ""),
        "what process marks the freight records",
    ),
    (
        Target(9, 1, "the collision event happened off the north mole", "", ""),
        "what incident occurred near the outer wall",
    ),
    (
        Target(0, 4, "you must never refuel with passengers aboard", "", ""),
        "what is forbidden while people are on the boat",
    ),
    (
        Target(1, 1, "Soren paints the hull numbers every spring", "", ""),
        "who redoes the identification digits annually",
    ),
]


# ── pipeline plumbing ────────────────────────────────────────────────────────


def _isolate(root: Path):
    spec = importlib.util.spec_from_file_location("mlc", ROOT / "scripts" / "memory_live_check.py")
    mlc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mlc)
    mlc._isolate_memory_root(root)


def _write(lobe: int, y: int, text: str, tag: str, counters: Counter) -> Optional[str]:
    from memory.api import CanonicalEnvelope, write_memory

    env = CanonicalEnvelope(
        cycle_id=f"gate1-{tag}-{uuid.uuid4().hex[:8]}",
        coord=[lobe, y, 0],
        type="semantic",
        payload={"title": tag, "summary": text, "text": text},
        source="memory.api",
    )
    try:
        r = write_memory(env)
        counters["live_embed"] += 1
    except Exception:
        try:
            r = write_memory(env, skip_embed=True)
            counters["skip_embed"] += 1
        except Exception as exc:
            counters["write_fail"] += 1
            print(f"  WRITE-FAIL L{lobe} {type(exc).__name__}: {exc}")
            return None
    raw = getattr(r, "path", None) or getattr(r, "staging_ref", None)
    if not raw:
        return None
    # the receipt's path may be a filesystem path OR the bare memory id —
    # never Path.stem a dotted id (it would chop the hash segment)
    name = str(raw).replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-5] if name.endswith(".json") else name


def _grammar_coords(query: str, lobe: int):
    from memory.dme.adapter import _query_coordinates
    from memory.dme.coordinate_registry import load_registry

    return _query_coordinates(query, load_registry(None), allowed_lobes=[lobe], denied_lobes=None)


def run() -> None:
    from memory.dme import hop_engine as he
    from memory.dme.adapter import load_sram
    from memory.dme.matmul_engine import active_backend
    from memory.dme.search import search_intersection
    from memory.engine.reindex import reindex_and_compile

    counters: Counter = Counter()
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        _isolate(root)

        # ── ingest: targets + linked pairs + isolated targets, then compile ──
        wrote: Dict[str, str] = {}
        silent_misroutes = 0
        for i, t in enumerate(TARGETS):
            got = subdomain_for(t.write, t.lobe)
            if got is not None and got != t.y:
                silent_misroutes += 1
                print(f"  SILENT-MISROUTE write L{t.lobe} exp={t.y} got={got} :: {t.write!r}")
            mid = _write(t.lobe, t.y, t.write, f"tgt{i}", counters)
            if mid:
                wrote[f"tgt{i}"] = mid
        for i, la in enumerate(LINKED):
            m1 = _write(la.lobe, la.y, la.target, f"lnk{i}t", counters)
            m2 = _write(la.lobe, la.y, la.bridge, f"lnk{i}b", counters)
            if m1:
                wrote[f"lnk{i}t"] = m1
            if m2:
                wrote[f"lnk{i}b"] = m2
        for i, (t, _q) in enumerate(ISOLATED):
            mid = _write(t.lobe, t.y, t.write, f"iso{i}", counters)
            if mid:
                wrote[f"iso{i}"] = mid

        try:
            reindex_and_compile()
        except Exception as exc:
            print(f"  COMPILE-FAIL {type(exc).__name__}: {exc}")

        index = load_sram(None)
        row_ids = [str(m.get("memory_id") or "") for m in index.row_index]

        # classed adjacency (modes C/E reality): strong = compiler-proven /
        # entity bridges / same-session; weak = placement / similarity
        nm_strong, nm_weak = he.classed_neighbor_maps_from_graph(
            None, row_ids, row_index=index.row_index
        )
        planes_classed = he.HopPlanes(
            strong=he.build_hop_plane(index.sram, index.row_index, nm_strong),
            weak=he.build_hop_plane(index.sram, index.row_index, nm_weak),
        )
        # merged map kept for the legacy-linkage count
        nm_compiler = he.neighbor_map_from_graph(None, row_ids)

        # ground-truth adjacency (mode D engine ceiling): inject linked edges
        # into the STRONG class (alias links are strong by nature)
        nm_truth = {k: list(v) for k, v in nm_strong.items()}
        for i in range(len(LINKED)):
            a, b = wrote.get(f"lnk{i}t"), wrote.get(f"lnk{i}b")
            if a and b:
                nm_truth.setdefault(a, []).append(b)
                nm_truth.setdefault(b, []).append(a)
        nm_truth = {k: sorted(set(v)) for k, v in nm_truth.items()}
        planes_truth = he.HopPlanes(
            strong=he.build_hop_plane(index.sram, index.row_index, nm_truth),
            weak=planes_classed.weak,
        )

        compiler_linked = sum(
            1
            for i in range(len(LINKED))
            if wrote.get(f"lnk{i}b", "?") in nm_compiler.get(wrote.get(f"lnk{i}t", "!"), [])
        )
        strong_linked = sum(
            1
            for i in range(len(LINKED))
            if wrote.get(f"lnk{i}b", "?") in nm_strong.get(wrote.get(f"lnk{i}t", "!"), [])
        )

        # ── query sets ──
        # (kind, target_key, lobe, query)
        queries: List[Tuple[str, str, int, str]] = []
        for i, t in enumerate(TARGETS):
            queries.append(("direct", f"tgt{i}", t.lobe, t.direct_q))
            queries.append(("para", f"tgt{i}", t.lobe, t.para_q))
        for i, la in enumerate(LINKED):
            queries.append(("alias_linked", f"lnk{i}t", la.lobe, la.alias_q))
        for i, (t, q) in enumerate(ISOLATED):
            queries.append(("alias_isolated", f"iso{i}", t.lobe, q))

        from memory.recall_anchor import filter_anchor_basis
        from memory.taxonomy.normalize import canonical_json

        def engine_mode(name: str, plane, hops: int, strict: bool):
            stats = defaultdict(Counter)
            lat: List[float] = []
            hop_depths: List[int] = []
            false_cands = 0
            total_cands = 0
            records = []  # replay signature material
            for kind, tkey, lobe, q in queries:
                target = wrote.get(tkey, "")
                coords = _grammar_coords(q, lobe)
                t0 = time.perf_counter()
                if strict:
                    res = search_intersection(index.sram, index.row_index, coords, max_results=10)
                    hits = [(h.memory_id, 0, 0.0) for h in res.hits]
                else:
                    res = he.search_thought(
                        index.sram, plane, index.row_index, coords, hops=hops, max_results=10
                    )
                    # production parity: the pre-verifier anchor filter sits
                    # between the engine and the verifier — score what the
                    # verifier would actually see
                    kept, _dropped = filter_anchor_basis(
                        q,
                        [
                            h.to_candidate_dict(snapshot_hash="", backend="") | {"id": h.memory_id}
                            for h in res.hits
                        ],
                    )
                    kept_ids = {str(c.get("id")) for c in kept}
                    hits = [
                        (h.memory_id, h.hop, h.score) for h in res.hits if h.memory_id in kept_ids
                    ]
                lat.append((time.perf_counter() - t0) * 1000)
                records.append([kind, tkey, [[m, h, s] for m, h, s in hits]])
                ids = [m for m, _, _ in hits]
                total_cands += len(ids)
                false_cands += sum(1 for m in ids if m != target)
                hop_depths.extend(h for _, h, _ in hits)
                stats[kind]["n"] += 1
                if target and ids and ids[0] == target:
                    stats[kind]["hit1"] += 1
                if target and target in ids[:5]:
                    stats[kind]["hit5"] += 1
            return {
                "name": name,
                "stats": stats,
                "avg_ms": sum(lat) / max(1, len(lat)),
                "avg_hop": (sum(hop_depths) / len(hop_depths)) if hop_depths else 0.0,
                "false_cand_rate": false_cands / max(1, total_cands),
                "candidates": total_cands,
                "signature": canonical_json(records),
            }

        def stable_engine_mode(name: str, plane, hops: int, strict: bool):
            # RATIFIED determinism protocol: byte-stable double-runs
            first = engine_mode(name, plane, hops, strict)
            second = engine_mode(name, plane, hops, strict)
            first["byte_stable"] = first["signature"] == second["signature"]
            return first

        modes = [
            stable_engine_mode("A strict-intersection", None, 0, True),
            stable_engine_mode("B graded-union", planes_classed, 0, False),
            stable_engine_mode("C adaptive classed (production)", planes_classed, 2, False),
            stable_engine_mode("D truth-edge ceiling (hops=3)", planes_truth, 3, False),
        ]

        # ── mode E: production path + verifier gate ──
        from auto_loop.mril_tools.registry import REMEMBER_TOOL_REGISTRY
        from auto_loop.mril_tools.remember import invoke_remember_tool

        tool_by_lobe = {lobe: t for t, (lane, lobe, mt) in REMEMBER_TOOL_REGISTRY.items()}

        # latency decomposition: wrap the seams (module attrs resolved at call
        # time inside the production functions, so assignment instruments them)
        import memory.api as mem_api
        import memory.dme.adapter as dme_adapter
        import memory.recall_verifier as rv

        LEG: Dict[str, float] = defaultdict(float)
        LEG_CALLS: Counter = Counter()

        def _wrap(mod, attr, leg):
            orig = getattr(mod, attr)

            def timed(*a, **k):
                t0 = time.perf_counter()
                try:
                    return orig(*a, **k)
                finally:
                    LEG[leg] += (time.perf_counter() - t0) * 1000
                    LEG_CALLS[leg] += 1

            setattr(mod, attr, timed)
            return (mod, attr, orig)

        # NOTE: no embed seam left to wrap — the admission path is embed-free
        # by design (deterministic relevance floor). The report's rescue leg
        # printing 0 calls IS the receipt.
        wrapped = [
            _wrap(mem_api, "recall_verified", "recall_total"),
            _wrap(dme_adapter, "merge_dme_candidate_dicts", "dme_engine"),
            _wrap(rv, "verify_retrieval_result", "verifier"),
        ]

        def mode_e_pass():
            e_stats = defaultdict(Counter)
            gate = Counter()
            lat_e: List[float] = []
            records = []
            for kind, tkey, lobe, q in queries:
                target = wrote.get(tkey, "")
                tool = tool_by_lobe.get(lobe)
                entry = {
                    "tool": tool,
                    "query": q,
                    "allowed_lobes": [lobe],
                    "memory_type": REMEMBER_TOOL_REGISTRY[tool][2],
                    "lane": REMEMBER_TOOL_REGISTRY[tool][0],
                    "lobe": lobe,
                }
                y = subdomain_for(q, lobe)
                if y is not None:
                    entry["subdomain"] = y
                t0 = time.perf_counter()
                try:
                    out = invoke_remember_tool(entry, f"gate1-{tkey}")
                except Exception as exc:
                    gate["recall_fail"] += 1
                    print(f"  RECALL-FAIL L{lobe} {type(exc).__name__}: {exc}")
                    continue
                lat_e.append((time.perf_counter() - t0) * 1000)
                verdict = str(out.get("verdict") or "unknown")
                ev = out.get("evidence") or []
                blocked = [str(e.get("memory_id")) for e in out.get("nonresponsive_evidence") or []]
                gate[
                    "verified"
                    if verdict == "verified"
                    else "conflict"
                    if verdict == "conflict"
                    else "ambiguous"
                    if verdict == "ambiguous"
                    else "unknown"
                ] += 1
                if ev and verdict != "verified":
                    gate["unverified_upstream"] += 1
                ids = [str(e.get("memory_id")) for e in ev]
                records.append([tkey, verdict, str(out.get("grounding") or ""), ids, blocked])
                found = bool(target) and target in ids  # id-match is the authority
                e_stats[kind]["n"] += 1
                if found:
                    e_stats[kind]["hit5"] += 1
                    if ids and ids[0] == target:
                        e_stats[kind]["hit1"] += 1
                # hallucination check for isolated aliases: verified admission
                # of a WRONG memory counts as fabricated recovery
                if kind == "alias_isolated" and verdict == "verified" and ev and not found:
                    gate["fabricated_recovery"] += 1
            return {
                "stats": e_stats,
                "gate": gate,
                "lat": lat_e,
                "signature": canonical_json(records),
            }

        # RATIFIED replay protocol: pass 1's verified-recall side effects
        # (strength touches, confidence decay) legitimately mutate the store,
        # so a naive second pass would measure state drift, not
        # nondeterminism. Snapshot the whole isolated root and restore it —
        # plus the in-process caches that shadow the disk — so pass 2 replays
        # pass 1's exact starting state.
        import shutil

        snap = Path(tempfile.mkdtemp(prefix="gate1-e-snap-")) / "state"
        shutil.copytree(root, snap)

        def _restore_state() -> None:
            for child in root.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            for child in snap.iterdir():
                dst = root / child.name
                if child.is_dir():
                    shutil.copytree(child, dst)
                else:
                    shutil.copy2(child, dst)
            import memory.strength_index as _si
            from memory.dme import adapter as _dme_adapter

            _dme_adapter._RECALL_CACHE.clear()
            try:
                _si._singleton = None  # re-read from the restored disk
            except Exception:
                pass
            try:
                from memory.engine.walker import load_coord_index

                load_coord_index()
            except Exception:
                pass

        try:
            e_first = mode_e_pass()
            legs_first = dict(LEG)
            legs_calls_first = dict(LEG_CALLS)
            _restore_state()
            e_second = mode_e_pass()
        finally:
            for mod, attr, orig in wrapped:
                setattr(mod, attr, orig)
            shutil.rmtree(snap.parent, ignore_errors=True)
        e_byte_stable = e_first["signature"] == e_second["signature"]
        if not e_byte_stable:
            import json as _json

            rec1 = _json.loads(e_first["signature"])
            rec2 = _json.loads(e_second["signature"])
            for a, b in zip(rec1, rec2):
                if a != b:
                    print(f"\nE-DIVERGENCE first mismatch @ {a[0]}:")
                    print(f"  pass1: verdict={a[1]} grounding={a[2]} ids={a[3]} blocked={a[4]}")
                    print(f"  pass2: verdict={b[1]} grounding={b[2]} ids={b[3]} blocked={b[4]}")
                    break
            else:
                print(f"\nE-DIVERGENCE: record counts differ {len(rec1)} vs {len(rec2)}")
        e_stats, gate, lat_e = e_first["stats"], e_first["gate"], e_first["lat"]

        # ── report ──
        print()
        print("#" * 78)
        print("# GATE-1 ABLATION — Thought Engine v2, day-zero substrate")
        print(
            f"# corpus: {len(TARGETS)} targets / {len(LINKED)} linked-alias / "
            f"{len(ISOLATED)} isolated-alias — {len(queries)} queries x 5 modes"
        )
        print("#" * 78)
        print(
            f"\nwrites: live-embed={counters['live_embed']} skip-embed={counters['skip_embed']} "
            f"fail={counters['write_fail']}   SILENT MISROUTES (write): {silent_misroutes}"
        )
        print(
            f"compiler-linked alias pairs: {compiler_linked}/{len(LINKED)} any-class, "
            f"{strong_linked}/{len(LINKED)} STRONG (entity/session/record links)"
        )
        print(
            f"providers: hop_engine={'onnx' if he._onnx_session() else 'numpy'} "
            f"matmul_engine={active_backend()}"
        )

        hdr = (
            f"\n{'mode':<34}{'hit@1':>7}{'hit@5':>7}{'para':>7}{'aliasL':>8}"
            f"{'aliasI':>8}{'avg_hop':>9}{'falseC%':>9}{'ms/q':>7}{'stable':>8}"
        )
        print(hdr)
        for m in modes:
            s = m["stats"]

            def pc(k, metric="hit5"):
                n = s[k]["n"]
                return f"{100 * s[k][metric] / n:5.0f}%" if n else "  n/a"

            print(
                f"{m['name']:<34}{pc('direct', 'hit1'):>7}{pc('direct'):>7}{pc('para'):>7}"
                f"{pc('alias_linked'):>8}{pc('alias_isolated'):>8}"
                f"{m['avg_hop']:>9.2f}{100 * m['false_cand_rate']:>8.0f}%{m['avg_ms']:>7.2f}"
                f"{'BYTE' if m.get('byte_stable') else '*NO*':>8}"
            )
        n_e = {k: e_stats[k]["n"] for k in ("direct", "para", "alias_linked", "alias_isolated")}

        def pe(k, metric="hit5"):
            return f"{100 * e_stats[k][metric] / n_e[k]:5.0f}%" if n_e.get(k) else "  n/a"

        print(
            f"{'E full path + verifier':<34}{pe('direct', 'hit1'):>7}{pe('direct'):>7}{pe('para'):>7}"
            f"{pe('alias_linked'):>8}{pe('alias_isolated'):>8}{'—':>9}{'—':>9}"
            f"{(sum(lat_e) / max(1, len(lat_e))):>7.2f}"
            f"{'BYTE' if e_byte_stable else '*NO*':>8}"
        )

        nq = max(1, len(queries))
        print("\nMode E latency decomposition (first pass, per query):")
        for leg, label in (
            ("recall_total", "recall (walker+dme+verify)"),
            ("dme_engine", "  dme engine+merge"),
            ("verifier", "  verifier"),
            ("embed_rescue", "relevance embed rescue"),
        ):
            ms = legs_first.get(leg, 0.0)
            calls = legs_calls_first.get(leg, 0)
            print(f"- {label:<28} {ms / nq:7.2f} ms/q   ({calls} calls, {ms:.0f} ms total)")
        other = (
            sum(lat_e) - legs_first.get("recall_total", 0.0) - legs_first.get("embed_rescue", 0.0)
        )
        print(f"- {'envelope+grammar overhead':<28} {other / nq:7.2f} ms/q")

        print("\nGate (mode E):")
        print(f"- verified admissions            {gate['verified']}")
        print(f"- unknown returns                {gate['unknown']}")
        print(f"- ambiguous returns              {gate['ambiguous']}")
        print(f"- conflict returns               {gate['conflict']}")
        print(
            f"- unverified upstream admissions {gate['unverified_upstream']}   ◀ INVARIANT (must be 0)"
        )
        print(f"- fabricated isolated-alias recoveries {gate['fabricated_recovery']}   ◀ must be 0")
        print(f"- silent misroutes (write leg)   {silent_misroutes}   ◀ INVARIANT (must be 0)")
        inv = (
            gate["unverified_upstream"] == 0
            and silent_misroutes == 0
            and gate["fabricated_recovery"] == 0
        )
        print(f"\nINVARIANTS: {'HELD' if inv else '*** VIOLATED ***'}")

        # ── RATIFIED acceptance gates ──
        def pct_of(stats, kind, metric):
            n = stats[kind]["n"]
            return 100.0 * stats[kind][metric] / n if n else 0.0

        c = modes[2]
        cs = c["stats"]
        c_h1, c_h5 = pct_of(cs, "direct", "hit1"), pct_of(cs, "direct", "hit5")
        accept = [
            ("C hit@1 >= 88", c_h1 >= 88),
            ("C hit@5 > hit@1 + 5", c_h5 > c_h1 + 5 or c_h5 >= 99.9),
            ("C para >= 75", pct_of(cs, "para", "hit5") >= 75),
            ("C aliasL >= 90", pct_of(cs, "alias_linked", "hit5") >= 90),
            ("C falseC < 65%", c["false_cand_rate"] < 0.65),
            ("C < 0.5 ms/q", c["avg_ms"] < 0.5),
            ("C byte-stable", bool(c.get("byte_stable"))),
            ("E hit@1 >= 92", pct_of(e_stats, "direct", "hit1") >= 92),
            ("E para >= 85", pct_of(e_stats, "para", "hit5") >= 85),
            ("E aliasL >= 90", pct_of(e_stats, "alias_linked", "hit5") >= 90),
            ("E invariants 0-0-0", inv),
            ("E byte-stable", e_byte_stable),
        ]
        print("\nRATIFIED ACCEPTANCE:")
        for label, ok in accept:
            print(f"- {label:<26} {'PASS' if ok else '*** FAIL ***'}")
        print(
            f"\nGATE VERDICT: {'ALL GATES PASS' if all(ok for _, ok in accept) else 'GATES OPEN — keep tightening'}"
        )

        print("\nDoctrine: DMEv2 finds aggressively. Graph explains connectivity.")
        print("Verifier admits conservatively. Omega acts only on verified memory.")


if __name__ == "__main__":
    run()

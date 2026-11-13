"""Phase 7P — `omega memory` dev/debug CLI for the deterministic memory taxonomy.

A single dispatcher exposing every taxonomy operation for debugging:

    omega memory compile                 # rebuild graph -> print snapshot hash
    omega memory replay                  # recompile + report match/divergence (exit 0/1)
    omega memory recall "query" [--kind K] [--limit N]   # print the RecallPacket JSON
    omega memory snapshot [--save PATH]   # print stored snapshot hash + manifest; capture state
    omega memory diff <old.json> <new.json>              # diff two saved capture states
    omega memory pressure                # print graph-derived pressure signals

All read-only EXCEPT compile / replay (which rebuild the disposable graph, as documented).
Every command accepts ``--root`` to point at an alternate taxonomy root (tests / inspection).
Model-free, Guardian-free.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True))


def _cmd_compile(args) -> int:
    from memory.taxonomy.compiler import compile_memory_graph

    snap = compile_memory_graph(root=args.root)
    print(snap.snapshot_hash)
    _dump(
        {
            "snapshot_hash": snap.snapshot_hash,
            "event_count": snap.event_count,
            "node_count": snap.node_count,
            "edge_count": snap.edge_count,
            "compiler_version": snap.compiler_version,
        }
    )
    return 0


def _cmd_replay(args) -> int:
    from memory.taxonomy.replay import replay_memory_graph

    rep = replay_memory_graph(root=args.root)
    print("PASS" if rep["ok"] else "FAIL")
    _dump(rep)
    return 0 if rep["ok"] else 1


def _cmd_recall(args) -> int:
    from memory.taxonomy.recall import recall

    packet = recall(args.query, query_kind=args.kind or "", limit=args.limit, root=args.root)
    _dump(packet.to_dict())
    return 0


def _cmd_snapshot(args) -> int:
    from memory.taxonomy import paths as P
    from memory.taxonomy.graph_diff import capture_graph_state

    state = capture_graph_state(root=args.root)
    manifest = P.compiled_root(args.root) / "graph_snapshot.json"
    out = {
        "snapshot_hash": state["snapshot_hash"],
        "node_count": len(state["node_ids"]),
        "edge_count": len(state["edge_ids"]),
        "claim_count": len(state["claims"]),
        "manifest": json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else None,
    }
    if args.save:
        Path(args.save).write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        out["saved"] = str(args.save)
    _dump(out)
    return 0


def _cmd_diff(args) -> int:
    from memory.taxonomy.graph_diff import diff_graph_snapshots

    old = json.loads(Path(args.old).read_text(encoding="utf-8"))
    new = json.loads(Path(args.new).read_text(encoding="utf-8"))
    _dump(diff_graph_snapshots(old, new))
    return 0


def _cmd_pressure(args) -> int:
    from memory.taxonomy.pressure_signals import analyze_memory_pressure

    _dump(analyze_memory_pressure(root=args.root))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument(
        "--root", default=None, help="taxonomy root (default: data/memory_taxonomy)"
    )

    parser = argparse.ArgumentParser(prog="omega memory", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser(
        "compile", parents=[parent], help="rebuild graph; print snapshot hash"
    ).set_defaults(fn=_cmd_compile)
    sub.add_parser(
        "replay", parents=[parent], help="recompile + report match/divergence"
    ).set_defaults(fn=_cmd_replay)

    p_recall = sub.add_parser("recall", parents=[parent], help="print a RecallPacket")
    p_recall.add_argument("query")
    p_recall.add_argument("--kind", default="")
    p_recall.add_argument("--limit", type=int, default=10)
    p_recall.set_defaults(fn=_cmd_recall)

    p_snap = sub.add_parser("snapshot", parents=[parent], help="print/save the graph state")
    p_snap.add_argument("--save", default=None)
    p_snap.set_defaults(fn=_cmd_snapshot)

    p_diff = sub.add_parser("diff", parents=[parent], help="diff two saved capture states")
    p_diff.add_argument("old")
    p_diff.add_argument("new")
    p_diff.set_defaults(fn=_cmd_diff)

    sub.add_parser(
        "pressure", parents=[parent], help="print graph-derived pressure signals"
    ).set_defaults(fn=_cmd_pressure)

    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

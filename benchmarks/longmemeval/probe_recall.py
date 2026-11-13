"""Recall probe — trace ONE question through every recall layer with ids."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass


def main(qid: str, needle: str) -> None:
    spec = importlib.util.spec_from_file_location(
        "runner", ROOT / "benchmarks" / "longmemeval" / "run_eval.py"
    )
    runner = importlib.util.module_from_spec(spec)
    sys.modules["runner"] = runner
    spec.loader.exec_module(runner)

    instance = None
    with open(runner.DEFAULT_INPUT, encoding="utf-8") as fh:
        for line in fh:
            rec = json.loads(line)
            if str(rec.get("question_id")) == qid:
                instance = rec
                break
    assert instance, qid
    question = str(instance["question"])

    from auto_loop.mril_tools.subdomain_helpers import subdomain_for

    tmp = Path(tempfile.mkdtemp(prefix="lme-probe-"))
    runner._isolate(tmp)
    runner._reset_process_caches()
    from core.paths import MEMORY_ROOT

    runner._ingest(Path(MEMORY_ROOT), instance, subdomain_for)

    from memory.taxonomy.compiler import compile_memory_graph
    from memory.dme.coordinate_registry import load_registry
    from memory.engine import walker

    reg_path = Path(MEMORY_ROOT) / "dme" / "coordinate_registry.json"
    reg_path.parent.mkdir(parents=True, exist_ok=True)
    if not reg_path.exists():
        reg_path.write_text(
            '{"addresses": {}, "counters": {}, "page_counters": {}}', encoding="utf-8"
        )
    walker._coord_index_mtime = 0.0
    walker.load_coord_index()
    with load_registry(None).bulk_registration():
        compile_memory_graph()
    runner._reset_process_caches()

    # 1. the true memory
    idx = json.loads(
        (Path(MEMORY_ROOT) / "indexes" / "coord_index.json").read_text(encoding="utf-8")
    )
    true_ids = []
    for mid, entry in idx.items():
        body = json.loads(Path(entry["path"]).read_text(encoding="utf-8"))
        if needle.lower() in str(body.get("summary", "")).lower():
            true_ids.append(mid)
    print(f"question: {question!r}")
    print(f"true ids ({needle!r}): {true_ids}")

    # 2. registry
    from memory.dme.lobe_layout import ENTRY_WORDS, PAGE0_SLOTS

    registry = load_registry(None)
    paged = sum(1 for addr in registry._addresses.values() if addr[1] != 0)
    print(
        f"layout: ENTRY_WORDS={ENTRY_WORDS} PAGE0_SLOTS={PAGE0_SLOTS} "
        f"registry_keys={len(registry._addresses)} paged={paged}"
    )
    from memory.taxonomy.canonicalize import content_tokens

    q_tokens = sorted(content_tokens(question))
    print("query content tokens:", q_tokens)
    from memory.taxonomy.normalize import normalize_id_field

    raw_tokens = [t for t in normalize_id_field(question).split() if len(t) >= 2]
    for tok in raw_tokens:
        print(f"  registry[{tok!r}] = {registry.lookup(tok)}")

    # 3. raw DME
    from memory.dme.adapter import recall_candidates

    hits = recall_candidates(question, allowed_lobes=[2], limit=40)
    print(f"\nDME top-{len(hits)}:")
    for h in hits[:15]:
        prov = h.get("provenance") or {}
        mark = " ◀◀ TRUE" if h.get("memory_id") in true_ids else ""
        print(
            f"  {h['memory_id']} score={h['score']} class={prov.get('dme_edge_class')}"
            f" hop={prov.get('dme_hop')}{mark} :: {str(h.get('summary'))[:60]!r}"
        )
    in_dme = [h.get("memory_id") for h in hits if h.get("memory_id") in true_ids]
    print(f"true in DME hits: {in_dme}")

    # 4. the envelope
    envelope = runner._recall(question, subdomain_for)
    ev_ids = [e.get("memory_id") for e in envelope.get("evidence") or []]
    bl_ids = [e.get("memory_id") for e in envelope.get("nonresponsive_evidence") or []]
    print(f"\nenvelope: verdict={envelope.get('verdict')} ev={len(ev_ids)} blocked={len(bl_ids)}")
    for tid in true_ids:
        where = (
            "EVIDENCE rank %d" % ev_ids.index(tid)
            if tid in ev_ids
            else "BLOCKED"
            if tid in bl_ids
            else "ABSENT"
        )
        print(f"  true {tid}: {where}")
    print("evidence top-10:")
    for e in (envelope.get("evidence") or [])[:10]:
        mark = " ◀◀ TRUE" if e.get("memory_id") in true_ids else ""
        print(
            f"  {e.get('memory_id')} conf={e.get('confidence')}{mark}"
            f" :: {str(e.get('remembered_as'))[:60]!r}"
        )


if __name__ == "__main__":
    main(
        sys.argv[1] if len(sys.argv) > 1 else "e47becba",
        sys.argv[2] if len(sys.argv) > 2 else "Business Administration",
    )

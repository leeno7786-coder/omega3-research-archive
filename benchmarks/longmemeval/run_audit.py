"""Miss autopsy — replay deterministic recall on judged misses, no model.

For every board miss, rebuild the instance store exactly as the board did
(same ingest, same compile, same recall) and answer ONE question with
receipts: did the oracle answer ever reach the model?

Classification per miss:
  window      — answer text present in the top-EVIDENCE_LIMIT lines the model
                actually saw → answer-layer fault (model fumbled it)
  admitted    — present in admitted evidence but ranked below the window cut
                → window-width fault (EVIDENCE_LIMIT too small / rank order)
  blocked     — present but in nonresponsive_evidence → verifier/relevance-
                floor fault
  corpus-only — ingested but never recalled → recall fault (lanes/DME/graph)
  synthesis   — answer text appears nowhere verbatim (counts, aggregates);
                judged instead by SESSION COVERAGE (were the oracle's answer
                sessions represented in the window?)

Usage:
  python benchmarks/longmemeval/run_audit.py [--limit N] [--offset N]

Output: G:/LongMemEval/LongMemEval/audit_misses.jsonl (resumable) + summary.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from benchmarks.longmemeval.run_eval import (  # noqa: E402
    EVIDENCE_LIMIT,
    _build_idf,
    _build_semantic_store,
    _ingest,
    _isolate,
    _recall,
    _reset_process_caches,
)

BASE = Path(os.environ.get("LONGMEMEVAL_ROOT", REPO_ROOT / "benchmarks" / "longmemeval"))
INPUT = BASE / "omega_eval_input_s.jsonl"
ORACLE = BASE / "data" / "longmemeval_oracle.json"
VERDICTS = BASE / "omega_predictions.jsonl.eval-results-gpt-4o"
PREDICTIONS = BASE / "omega_predictions.jsonl"
OUTPUT = BASE / "audit_misses.jsonl"


def _compile_isolated(memory_root: Path) -> None:
    # mirrors run_eval.run()'s nested helper (kept nested there to protect
    # the sealed board runner; duplicated here verbatim)
    from memory.dme.coordinate_registry import load_registry
    from memory.engine import walker
    from memory.taxonomy.compiler import compile_memory_graph

    walker._coord_index_mtime = 0.0
    walker.load_coord_index()
    reg_path = memory_root / "dme" / "coordinate_registry.json"
    if not reg_path.exists():
        reg_path.parent.mkdir(parents=True, exist_ok=True)
        reg_path.write_text(
            '{"addresses": {}, "counters": {}, "page_counters": {}}',
            encoding="utf-8",
        )
    with load_registry(None).bulk_registration():
        compile_memory_graph()


def _norm(s: str) -> str:
    return " ".join(str(s or "").casefold().split())


def _answer_in(answer: str, text: str) -> bool:
    a, t = _norm(answer), _norm(text)
    if not a:
        return False
    if len(a) <= 4 or a.replace(".", "").isdigit():
        return re.search(rf"(?<![\w]){re.escape(a)}(?![\w])", t) is not None
    if a in t:
        return True
    toks = [w for w in re.findall(r"[a-z0-9']+", a) if len(w) > 2]
    return bool(toks) and all(w in t for w in toks)


def _misses() -> list:
    preds = [json.loads(line) for line in open(PREDICTIONS, encoding="utf-8")]
    evals = [json.loads(line) for line in open(VERDICTS, encoding="utf-8")]
    oracle = {e["question_id"]: e for e in json.load(open(ORACLE, encoding="utf-8"))}
    out = []
    for p, e in zip(preds, evals):
        v = e["autoeval_label"]
        ok = v["label"] if isinstance(v, dict) else bool(v)
        qid = p["question_id"]
        if ok or qid.endswith("_abs"):
            continue
        o = oracle[qid]
        out.append(
            {
                "question_id": qid,
                "question_type": o["question_type"],
                "answer": str(o["answer"]),
                "answer_session_ids": o.get("answer_session_ids") or [],
                "hypothesis": p["hypothesis"],
            }
        )
    return out


def main() -> int:
    global PREDICTIONS, VERDICTS, OUTPUT
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--predictions", default=str(PREDICTIONS))
    ap.add_argument("--verdicts", default=str(VERDICTS))
    ap.add_argument("--output", default=str(OUTPUT))
    args = ap.parse_args()

    PREDICTIONS = Path(args.predictions)
    VERDICTS = Path(args.verdicts)
    OUTPUT = Path(args.output)

    instances = {}
    with open(INPUT, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                d = json.loads(line)
                instances[str(d.get("question_id"))] = d

    misses = _misses()
    done = set()
    if OUTPUT.exists():
        for line in open(OUTPUT, encoding="utf-8"):
            if line.strip():
                done.add(json.loads(line)["question_id"])
    todo = [m for m in misses if m["question_id"] not in done]
    todo = todo[args.offset : args.offset + args.limit if args.limit else None]
    print(f"# auditing {len(todo)} of {len(misses)} misses ({len(done)} already done)", flush=True)

    from auto_loop.mril_tools.subdomain_helpers import subdomain_for

    for n, miss in enumerate(todo, 1):
        qid = miss["question_id"]
        inst = instances[qid]
        question = str(inst.get("question") or "")
        t0 = time.perf_counter()
        tmp = Path(tempfile.mkdtemp(prefix="lme-audit-"))
        try:
            _isolate(tmp)
            _reset_process_caches()
            from core.paths import MEMORY_ROOT

            meta_by_id = _ingest(Path(MEMORY_ROOT), inst, subdomain_for)
            # mid -> source session id, straight from the store the ingest wrote
            sid_by_mid = {}
            for f in (Path(MEMORY_ROOT) / "eval_sessions").glob("*.json"):
                rec = json.loads(f.read_text(encoding="utf-8"))
                sid_by_mid[rec["id"]] = str(
                    (rec.get("provenance") or {}).get("source_session_id") or ""
                )
            _compile_isolated(Path(MEMORY_ROOT))
            _reset_process_caches()
            sem_store = _build_semantic_store(meta_by_id)
            idf = _build_idf(meta_by_id)
            envelope = _recall(question, sem_store, idf, meta_by_id)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        answer = miss["answer"]
        evid = envelope.get("evidence") or []
        blocked = envelope.get("nonresponsive_evidence") or []

        def _chunk(mid: str) -> str:
            entry = meta_by_id.get(mid)
            return entry[2] if entry else ""

        window_ids = [str(i.get("memory_id") or "") for i in evid[:EVIDENCE_LIMIT]]
        admitted_ids = [str(i.get("memory_id") or "") for i in evid]
        blocked_ids = [str(i.get("memory_id") or "") for i in blocked]

        first_rank = next(
            (r for r, mid in enumerate(admitted_ids) if _answer_in(answer, _chunk(mid))), None
        )
        in_window = first_rank is not None and first_rank < EVIDENCE_LIMIT
        in_admitted = first_rank is not None
        in_blocked = any(_answer_in(answer, _chunk(m)) for m in blocked_ids)
        in_corpus = any(_answer_in(answer, e[2]) for e in meta_by_id.values())

        ans_sids = set(miss["answer_session_ids"])
        window_sids = {sid_by_mid.get(m, "") for m in window_ids}
        admitted_sids = {sid_by_mid.get(m, "") for m in admitted_ids}
        cov_window = (len(ans_sids & window_sids) / len(ans_sids)) if ans_sids else 0.0
        cov_admitted = (len(ans_sids & admitted_sids) / len(ans_sids)) if ans_sids else 0.0

        if in_window:
            klass = "window"  # model saw it and still missed
        elif in_admitted:
            klass = "width"  # ranked below the EVIDENCE_LIMIT cut
        elif in_blocked:
            klass = "verifier"  # relevance floor blocked the answer
        elif in_corpus:
            klass = "recall"  # ingested, never surfaced
        else:
            klass = "synthesis"  # answer is an aggregate; judge by coverage

        rec = {
            "question_id": qid,
            "question_type": miss["question_type"],
            "answer": answer[:120],
            "class": klass,
            "first_rank": first_rank,
            "in_window": in_window,
            "in_admitted": in_admitted,
            "in_blocked": in_blocked,
            "in_corpus": in_corpus,
            "session_coverage_window": round(cov_window, 2),
            "session_coverage_admitted": round(cov_admitted, 2),
            "n_evidence": len(evid),
            "n_blocked": len(blocked),
            "hypothesis": miss["hypothesis"][:160],
            "t": round(time.perf_counter() - t0, 1),
        }
        with open(OUTPUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(
            f"[{n}/{len(todo)}] {qid} ({miss['question_type']}) -> {klass} "
            f"rank={first_rank} cov={cov_window:.2f} {rec['t']}s",
            flush=True,
        )

    # summary
    rows = [json.loads(line) for line in open(OUTPUT, encoding="utf-8") if line.strip()]
    table: dict = {}
    for r in rows:
        table.setdefault(r["question_type"], {}).setdefault(r["class"], 0)
        table[r["question_type"]][r["class"]] += 1
    print("\n=== AUDIT SUMMARY (misses by type x class) ===")
    print(json.dumps(table, indent=1, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

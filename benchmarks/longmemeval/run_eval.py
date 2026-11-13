"""LongMemEval_S board runner — Omega v2 deterministic memory + LOCAL 4B.

LOCKED CONFIGURATION (operator, 2026-07-04):
  candidate: tag omega3-lme-candidate-2026-07-04 (commit 877be899)
  memory:    DMEv2 adaptive classed + verifier path (engine-direct, cascade bypassed)
  answers:   LOCAL runtime only (one inference per question, temperature 0)
  judge:     OpenAI evaluator only (separate step, conda longmemeval-lite)

Per instance (isolated store, the live mind is never touched):
  1. ingest every haystack turn as canonical records — lobe 2 (knowledge),
     Y via the deterministic grammar (subdomain_for; unresolved → _unsorted,
     the widen ladder recovers), session id + timestamp in provenance.
     Bulk mode per the war plan: direct canonical writes, no guardian
     ceremony — the RecallVerifier re-derives from these files at recall,
     so every admission is still provenance-proven.
  2. reindex + compile (entity projection links names across sessions).
  3. recall through the FULL production gate: remember_knowledge →
     verifier → anchor filter → deterministic relevance floor → question-
     form gate → widen ladder. Evidence is VERIFIED_RELEVANT only.
  4. answer with ONE local-4B inference over dated evidence; UNKNOWN →
     deterministic abstention (no model call — honest by construction).

Output: JSONL {"question_id", "hypothesis"} (the official judge contract)
plus a .meta.jsonl sidecar (grounding, counts, timings) for diagnostics.
Resumable: existing question_ids in the output are skipped.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

_LONGMEMEVAL_ROOT = Path(
    os.environ.get("LONGMEMEVAL_ROOT", str(ROOT / "benchmarks" / "longmemeval"))
)
DEFAULT_INPUT = _LONGMEMEVAL_ROOT / "omega_eval_input_s.jsonl"
DEFAULT_OUTPUT = _LONGMEMEVAL_ROOT / "omega_predictions.jsonl"

# benchmark haystacks are external documents, not Omega's own mind — shard
# them by content hash across exactly the lobes the PRODUCTION QUESTION PACK
# sweeps (meta_loop.memory.run._QUESTION_GRAPH_RECALL_TOOLS: knowledge,
# reasoning, procedure, vision, code, time, workflow, dev). One lobe's
# page-0 holds 10,880 hot token slots; a real _S haystack mints ~14k unique
# tokens, so single-lobe ingest overflowed page-0 and left answer tokens
# with no bits. Eight-way sharding gives ~87k combined slots.
RECALLABLE_LOBES = (2, 3, 5, 7, 8, 9, 10, 11)
UNSORTED_Y = 127
CHUNK_CHARS = 900
RECALL_LIMIT = 40  # candidate breadth — the relevance floor gates admission
EVIDENCE_LIMIT = 14  # prompt budget — top responsive lines shown to the 4B
GEN_MAX_TOKENS = 200
GEN_TIMEOUT = 180.0

# ── semantic candidate lane (FINDING + RANKING only, never admission) ────────
# EmbeddingGemma retrieval prompts per the Google model card: queries and
# documents use different prefixes, and skipping them measurably degrades
# retrieval. Vectors live in the runner's memory per instance; admission
# stays with the deterministic lexical/provenance floor (fabrication + byte-
# stability doctrine — embeds may find and rank, never admit).
QUERY_PREFIX = "task: search result | query: "
DOC_PREFIX = "title: none | text: "
SEMANTIC_TOPK = 60
# follow-up turns ("I got it at Target") share no tokens with the question —
# embedding each chunk with the tail of the PREVIOUS turn drags follow-ups
# into their topic's semantic neighborhood. Records stay pure single turns.
EMBED_CONTEXT_CHARS = 220

ABSTAIN = (
    "The information needed to answer this is not in my memory — "
    "the conversations do not mention it."
)

SYSTEM_PROMPT = (
    "You are a precise memory assistant. You answer questions about the "
    "user's past conversations strictly from the evidence provided."
)


def _isolate(root: Path):
    spec = importlib.util.spec_from_file_location("mlc", ROOT / "scripts" / "memory_live_check.py")
    mlc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mlc)
    mlc._isolate_memory_root(root)


def _reset_process_caches() -> None:
    """One process serves 500 isolated stores — caches must never leak."""
    from memory.dme import adapter, hop_engine, matmul_engine
    from memory.dme import coordinate_registry as reg
    import memory.strength_index as si
    from memory.engine import walker

    # the runtime's DME service speaks the old 4096-bit entry format
    # (invalid_query_dim on v4 rows); in-process is byte-deterministic and
    # faster regardless — the board runs local matmuls only.
    matmul_engine._ACTIVE_BACKEND = "numpy"
    adapter._RECALL_CACHE.clear()
    hop_engine._BITS_CACHE.clear()
    reg._REGISTRY_CACHE.clear()
    try:
        si._singleton = None
    except Exception:
        pass
    try:
        walker._coord_index_mtime = 0.0
        walker.load_coord_index()
    except Exception:
        pass


_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _chunks(text: str, limit: int = CHUNK_CHARS):
    text = " ".join(str(text or "").split())
    if not text:
        return
    if len(text) <= limit:
        yield text
        return
    buf = ""
    for sentence in _SENTENCE.split(text):
        if buf and len(buf) + len(sentence) + 1 > limit:
            yield buf
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
        while len(buf) > limit:  # single monster sentence
            yield buf[:limit]
            buf = buf[limit:]
    if buf:
        yield buf


def _build_semantic_store(meta_by_id: dict) -> dict:
    """Embed every chunk into an in-memory matrix — DUAL vectors per chunk:
    the bare turn AND the context-windowed turn (previous-turn tail
    prepended). Max-pooled at scoring, so follow-ups gain their topic's
    neighborhood without diluting self-contained turns. The benchmark-local
    encoder is required; product runtime embedding is not a fallback."""
    import numpy as np

    base_ids = sorted(meta_by_id)
    ids, texts = [], []
    for mid in base_ids:
        _d, _r, chunk, embed_text = meta_by_id[mid]
        ids.append(mid)
        texts.append(DOC_PREFIX + chunk)
        if embed_text != chunk:
            ids.append(mid)  # same mid, second vector
            texts.append(DOC_PREFIX + embed_text)
    from benchmarks.longmemeval import local_embed

    if not local_embed.available():
        raise RuntimeError("LongMemEval local embedding model unavailable")
    vecs = local_embed.embed_texts(texts)
    kept_ids, rows = [], []
    for mid, vec in zip(ids, vecs):
        if vec:
            kept_ids.append(mid)
            rows.append(vec)
    if not rows:
        raise RuntimeError("LongMemEval local embedding returned no vectors")
    matrix = np.asarray(rows, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix /= norms
    return {
        "ids": kept_ids,
        "matrix": matrix,
        "embedded": len(kept_ids),
        "missing": len(ids) - len(kept_ids),
    }


def _ingest(memory_root: Path, instance: dict, subdomain_for) -> dict:
    """Direct canonical writes; returns memory_id -> (date, role, text)."""
    from auto_loop.mril_tools.router import route_memory_tool

    store = memory_root / "eval_sessions"
    store.mkdir(parents=True, exist_ok=True)
    index: dict = {}
    meta_by_id: dict = {}
    qid = str(instance.get("question_id"))
    sessions = instance.get("haystack_sessions") or []
    dates = instance.get("haystack_dates") or []
    session_ids = instance.get("haystack_session_ids") or []
    for s_idx, session in enumerate(sessions):
        s_date = str(dates[s_idx]) if s_idx < len(dates) else ""
        s_id = str(session_ids[s_idx]) if s_idx < len(session_ids) else f"s{s_idx}"
        prev_tail = ""
        for t_idx, turn in enumerate(session or []):
            role = str((turn or {}).get("role") or "")
            content = str((turn or {}).get("content") or "")
            for c_idx, chunk in enumerate(_chunks(content)):
                digest_full = hashlib.sha256(
                    f"{qid}|{s_id}|{t_idx}|{c_idx}|{chunk}".encode("utf-8")
                ).hexdigest()
                digest = digest_full[:10]
                # WRITE/RECALL SYMMETRY: the SAME deterministic router that
                # scopes the recall lanes places the chunk — the question
                # pack's narrowing then looks exactly where writes landed.
                route = route_memory_tool(chunk)
                lobe = int(route.get("lobe") or 2)
                y = route.get("subdomain")
                if y is None:
                    y = subdomain_for(chunk, lobe)
                if y is None:
                    y = UNSORTED_Y
                mid = f"{lobe}.{int(y):02d}.{digest}"
                path = store / f"{mid}.json"
                path.write_text(
                    json.dumps(
                        {
                            "id": mid,
                            "coord": [lobe, int(y), 0],
                            "title": chunk[:70],
                            "summary": chunk,
                            "text": chunk,
                            "type": "semantic",
                            "flags": {"canonical": True},
                            "provenance": {
                                "source_session_id": s_id,
                                "source_session_timestamp": s_date,
                                "role": role,
                            },
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                # full extract_minimal shape — the runner OWNS this index
                # (build_index would validate against the live repo schema
                # and silently reject bulk-mode records)
                index[mid] = {
                    "id": mid,
                    "coord": [lobe, int(y), 0],
                    "path": str(path),
                    "title": chunk[:70],
                    "type": "semantic",
                    "domain": "",
                    "subtopic": "",
                    "canonical": True,
                }
                embed_text = (prev_tail + " " + chunk).strip() if prev_tail else chunk
                meta_by_id[mid] = (s_date, role, chunk, embed_text)
            if content.strip():
                prev_tail = " ".join(content.split())[-EMBED_CONTEXT_CHARS:]
    idx_dir = memory_root / "indexes"
    idx_dir.mkdir(parents=True, exist_ok=True)
    (idx_dir / "coord_index.json").write_text(
        json.dumps(index, ensure_ascii=False), encoding="utf-8"
    )
    return meta_by_id


def _semantic_candidates(question: str, sem_store: dict, meta_by_id: dict):
    """Read-path semantic lane: ONE query embed (retrieval prefix) against
    the in-memory document matrix. Returns (top-K candidate dicts for the
    verifier, memory_id -> cosine map for ranking). FINDING only — the
    deterministic relevance floor still owns admission."""
    matrix = sem_store.get("matrix")
    ids = sem_store.get("ids") or []
    if matrix is None or not len(ids):
        return [], {}
    import numpy as np

    # the query MUST embed through the same path as the documents — one
    # vector space, no cross-stack drift
    from benchmarks.longmemeval import local_embed

    if not local_embed.available():
        raise RuntimeError("LongMemEval local embedding model unavailable")
    qv = local_embed.embed_texts([QUERY_PREFIX + question])[0]
    if not qv:
        raise RuntimeError("LongMemEval local embedding returned no query vector")
    q = np.asarray(qv, dtype=np.float32)
    norm = float(np.linalg.norm(q)) or 1.0
    q /= norm
    sims = matrix @ q
    # max-pool over dual vectors: each memory scores as its BEST view
    cosine_by_id: dict = {}
    for mid, s in zip(ids, sims):
        value = float(s)
        if value > cosine_by_id.get(mid, -1.0):
            cosine_by_id[mid] = value
    ranked = sorted(cosine_by_id, key=lambda m: (-cosine_by_id[m], m))
    candidates = []
    for mid in ranked[:SEMANTIC_TOPK]:
        date, role, text, _emb = meta_by_id.get(mid, ("", "", "", ""))
        lobe, y, _ = (mid.split(".") + ["0"])[:3]
        candidates.append(
            {
                "memory_id": mid,
                "coord": [int(lobe), int(y), 0],
                "score": cosine_by_id[mid],
                "title": text[:70],
                "summary": text,
                "provenance": {"candidate_source": "semantic"},
            }
        )
    return candidates, cosine_by_id


def _build_idf(meta_by_id: dict) -> dict:
    """Per-instance inverse document frequency over the shared stem grammar —
    the deterministic discriminator weighting (operator's structural
    analysis: uniform token overlap is cardinality-blind; 'serenity' must
    outweigh 'store'+'refund'+'policy' combined)."""
    import math

    from memory.taxonomy.canonicalize import content_tokens

    df: dict = {}
    for _mid, (_d, _r, text, _e) in meta_by_id.items():
        for stem in content_tokens(text):
            df[stem] = df.get(stem, 0) + 1
    n_docs = max(1, len(meta_by_id))
    return {stem: math.log(n_docs / (1 + count)) for stem, count in df.items()}


def _recall(question: str, sem_store: dict, idf: dict, meta_by_id: dict) -> dict:
    """THE PRODUCTION QUESTION PACK (eight governed lanes) + the semantic
    candidate lane, merged and ranked by IDF-weighted lexical overlap plus
    cosine. Admission stays deterministic (verifier + anchor + relevance
    floor + form gate); embeddings only find and rank."""
    from auto_loop.mril_tools.remember import _partition_responsive
    from auto_loop.mril_tools.router import _tool_entry
    from auto_loop.mril_tools.run import run_forced_memory_tools
    from memory.recall_verifier import verify_candidates
    from memory.taxonomy.canonicalize import content_tokens
    from meta_loop.memory.run import _QUESTION_GRAPH_RECALL_TOOLS

    tools = [
        _tool_entry(tool_name, question, limit=RECALL_LIMIT)
        for tool_name in _QUESTION_GRAPH_RECALL_TOOLS
    ]
    pack = {
        "memory_required": True,
        "memory_tools": tools,
        "memory_query": question,
        "memory_reason": "benchmark_question",
        "memory_scope": tuple(t["lane"] for t in tools),
    }
    results, _env = run_forced_memory_tools(pack, "lme-eval")

    best: dict = {}
    blocked: dict = {}
    verdicts = []
    for out in results:
        verdicts.append(str(out.get("verdict") or "unknown"))
        for item in out.get("evidence") or []:
            mid = str(item.get("memory_id") or "")
            if mid and (
                mid not in best
                or float(item.get("confidence") or 0.0) > float(best[mid].get("confidence") or 0.0)
            ):
                best[mid] = item
        for item in out.get("nonresponsive_evidence") or []:
            mid = str(item.get("memory_id") or "")
            if mid and mid not in blocked:
                blocked[mid] = item

    # semantic lane: find → verify (provenance) → relevance floor (admission)
    sem_candidates, cosine_by_id = _semantic_candidates(question, sem_store, meta_by_id)
    if sem_candidates:
        verdict = verify_candidates(question, sem_candidates, meta={"candidate_source": "semantic"})
        verified = list((verdict.answer_payload or {}).get("candidates") or [])
        responsive, nonresponsive = _partition_responsive(question, verified)
        verdicts.append(str(verdict.status or "unknown"))
        for item in responsive:
            mid = str(item.get("memory_id") or "")
            shaped = {
                "memory_id": mid,
                "remembered_as": str(item.get("summary") or item.get("title") or ""),
                "title": str(item.get("title") or ""),
                "confidence": float(item.get("score") or 0.0),
            }
            if mid and (
                mid not in best or shaped["confidence"] > float(best[mid].get("confidence") or 0.0)
            ):
                best[mid] = shaped
        for item in nonresponsive:
            mid = str(item.get("memory_id") or "")
            if mid and mid not in blocked:
                blocked[mid] = {
                    "memory_id": mid,
                    "remembered_as": str(item.get("summary") or item.get("title") or ""),
                    "title": str(item.get("title") or ""),
                    "confidence": float(item.get("score") or 0.0),
                }

    # RANKING: IDF-weighted overlap (deterministic discriminator weighting)
    # + cosine. The 'serenity' stem outweighs 'store refund policy' combined,
    # so lexical ties break on the discriminator, not on id accidents.
    q_stems = content_tokens(question)
    q_mass = sum(idf.get(s, 0.0) for s in q_stems) or 1.0

    def _rank(item) -> float:
        mid = str(item.get("memory_id") or "")
        text = str(item.get("remembered_as") or "") + " " + str(item.get("title") or "")
        overlap = q_stems & content_tokens(text)
        idf_score = sum(idf.get(s, 0.0) for s in overlap) / q_mass
        return 0.55 * idf_score + 0.45 * cosine_by_id.get(mid, 0.0)

    def _order(items):
        return sorted(
            items,
            key=lambda c: (
                -_rank(c),
                -float(c.get("confidence") or 0.0),
                str(c.get("memory_id") or ""),
            ),
        )

    evidence = _order(list(best.values()))
    nonresponsive_out = _order([b for m, b in blocked.items() if m not in best])
    grounding = (
        "VERIFIED_RELEVANT"
        if evidence
        else "VERIFIED_NONRESPONSIVE"
        if nonresponsive_out and "verified" in verdicts
        else "UNKNOWN"
    )
    return {
        "verdict": "verified"
        if "verified" in verdicts
        else (verdicts[0] if verdicts else "unknown"),
        "verdict_reason": "",
        "grounding": grounding,
        "evidence": evidence,
        "nonresponsive_evidence": nonresponsive_out,
        "widened_unsorted": False,
        "widened_lobe": True,
    }


SCAFFOLD_EVIDENCE_LIMIT = 28  # synthesis shapes count/order across sessions
# — they need the wider window (audit: 19
# width-class misses sat just below the cut)


def _evidence_block(evidence: list, meta_by_id: dict, limit: int) -> str:
    lines = []
    for item in evidence[:limit]:
        mid = str(item.get("memory_id") or "")
        date, role, _chunk, _e = meta_by_id.get(mid, ("", "", "", ""))
        text = str(item.get("remembered_as") or item.get("title") or "")
        lines.append(f"- [{date} · {role}] {text}")
    return "\n".join(lines)


def _answer(question: str, question_date: str, evidence: list, meta_by_id: dict) -> tuple:
    """Dynamic answer packs (the production promptpack pattern): a
    deterministic shape classifier picks the contract; scaffolded shapes
    think over the evidence and answer on a final line; FACT keeps the
    board-proven one-sentence extraction verbatim. Returns
    (hypothesis, mode, shape)."""
    from benchmarks.longmemeval import answer_pack

    shape = answer_pack.classify(question)
    if not evidence:
        return ABSTAIN, "abstain_no_evidence", shape

    scaffolded = answer_pack.thinking_for(shape)
    limit = SCAFFOLD_EVIDENCE_LIMIT if scaffolded else EVIDENCE_LIMIT
    block = _evidence_block(evidence, meta_by_id, limit)

    if shape == "FACT":
        system = SYSTEM_PROMPT
        prompt = (
            "Memory evidence from past conversations (session date · speaker):\n"
            + block
            + f"\n\nToday's date: {question_date}\n"
            f"Question: {question}\n\n"
            "Using only the evidence above, answer the question in ONE short "
            "sentence of your own words — do not quote or repeat evidence lines. "
            "Copy exact values (numbers, names, dates) from the evidence without "
            "changing them; never convert or add numbers. For questions about "
            "when something happened, use the session dates in brackets. Only if "
            "none of the evidence contains the answer, reply: I don't know — my "
            "conversations do not mention it.\n"
            "Answer:"
        )
        max_tokens = GEN_MAX_TOKENS
    else:
        system = answer_pack.system_for(shape)
        prompt = answer_pack.user_prompt(question, question_date, block)
        max_tokens = 0  # set per-attempt: the thinking pass gets more headroom

    from runtime.client import get_runtime_client

    client = get_runtime_client()
    # infrastructure death must NEVER masquerade as abstention: if the
    # runtime is down, WAIT for it (poll /health up to ~2h — the operator
    # reboots it) and only then answer. A board full of fake abstains is a
    # corrupted exam, not a slow one.
    for attempt in range(3):
        # huge-evidence thinkers can blow the thinking budget (Gate 0
        # pathology) — after the first empty round, retry with thinking off
        # rather than burning the run on one question
        thinking = scaffolded and attempt == 0
        toks = (
            max_tokens if shape == "FACT" else answer_pack.max_tokens_for(shape, thinking=thinking)
        )
        text = client.generate(
            prompt,
            system=system,
            max_tokens=toks,
            temperature=0.0,
            timeout=420.0 if thinking else GEN_TIMEOUT,
            enable_thinking=thinking,
        )
        if text and text.strip():
            hyp = answer_pack.extract_final(shape, text, strict=thinking)
            if hyp:
                return hyp, "generated", shape
            # non-empty but no Answer line on the thinking pass = truncated
            # working, NOT infra death — skip the health wait, retry with
            # thinking off (the fix_four lesson: direct scaffold answers
            # land reliably where thinking blows the budget)
            continue
        import requests as _rq

        for _wait in range(240):  # up to ~2h in 30s steps
            try:
                if _rq.get("http://127.0.0.1:11434/health", timeout=5).ok:
                    break
            except Exception:
                pass
            time.sleep(30)
    return ABSTAIN, "generation_failed", shape


def run(args) -> None:
    instances = []
    with open(args.input, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                instances.append(json.loads(line))
    if args.qid:
        instances = [i for i in instances if str(i.get("question_id")) == args.qid]
    instances = instances[args.offset : args.offset + args.limit if args.limit else None]

    out_path = Path(args.output)
    done = set()
    if out_path.exists():
        for line in out_path.read_text(encoding="utf-8").splitlines():
            try:
                done.add(str(json.loads(line).get("question_id")))
            except Exception:
                continue
    meta_path = out_path.with_suffix(out_path.suffix + ".meta.jsonl")

    from auto_loop.mril_tools.subdomain_helpers import subdomain_for
    from memory.taxonomy.compiler import compile_memory_graph

    def _compile_isolated(memory_root: Path) -> None:
        # NOT reindex_and_compile: its build_index validates against the live
        # repo schema and would reject bulk-mode records, silently emptying
        # the coord_index the ingest just wrote. The runner owns its index;
        # compile + walker reload are all that's needed.
        from memory.dme.coordinate_registry import load_registry
        from memory.engine import walker

        walker._coord_index_mtime = 0.0
        walker.load_coord_index()
        # pre-create the registry file so load_registry's stamp cache serves
        # the SAME instance to the compiler internals — bulk_registration
        # then covers every seat token (one save instead of ~30k)
        reg_path = memory_root / "dme" / "coordinate_registry.json"
        if not reg_path.exists():
            reg_path.parent.mkdir(parents=True, exist_ok=True)
            reg_path.write_text(
                '{"addresses": {}, "counters": {}, "page_counters": {}}',
                encoding="utf-8",
            )
        with load_registry(None).bulk_registration():
            compile_memory_graph()

    # MODEL LOCK PREFLIGHT: the benchmark answers with the locked local 4B,
    # nothing else. A pinned vision model hijacked /generate once — never
    # burn a board run on the wrong brain.
    import requests as _rq

    probe = _rq.post(
        "http://127.0.0.1:11434/generate", json={"prompt": "Say OK.", "max_tokens": 4}, timeout=300
    ).json()
    model_id = str(probe.get("model_id") or "")
    if not model_id.startswith("gemma"):
        raise SystemExit(
            f"MODEL LOCK VIOLATION: /generate serves {model_id!r}, "
            "expected the locked gemma 4B. Fix the runtime binding first."
        )
    print(f"# model lock verified: {model_id}")

    total = len(instances)
    print(f"# LongMemEval_S board run — {total} instance(s), {len(done)} already answered (resume)")

    for n, instance in enumerate(instances, 1):
        qid = str(instance.get("question_id"))
        if qid in done:
            continue
        question = str(instance.get("question") or "")
        qdate = str(instance.get("question_date") or "")
        t0 = time.perf_counter()
        tmp = Path(tempfile.mkdtemp(prefix="lme-"))
        try:
            _isolate(tmp)
            _reset_process_caches()
            from core.paths import MEMORY_ROOT

            meta_by_id = _ingest(Path(MEMORY_ROOT), instance, subdomain_for)
            t_ingest = time.perf_counter()
            _compile_isolated(Path(MEMORY_ROOT))
            _reset_process_caches()
            sem_store = _build_semantic_store(meta_by_id)
            idf = _build_idf(meta_by_id)
            t_compile = time.perf_counter()
            envelope = _recall(question, sem_store, idf, meta_by_id)
            t_recall = time.perf_counter()
            hypothesis, mode, shape = _answer(
                question, qdate, envelope.get("evidence") or [], meta_by_id
            )
            t_answer = time.perf_counter()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        with open(out_path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({"question_id": qid, "hypothesis": hypothesis}, ensure_ascii=False)
                + "\n"
            )
        with open(meta_path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "question_id": qid,
                        "mode": mode,
                        "shape": shape,
                        "grounding": envelope.get("grounding"),
                        "verdict": envelope.get("verdict"),
                        "n_evidence": len(envelope.get("evidence") or []),
                        "n_blocked": len(envelope.get("nonresponsive_evidence") or []),
                        "widened": [
                            bool(envelope.get("widened_unsorted")),
                            bool(envelope.get("widened_lobe")),
                        ],
                        "memories": len(meta_by_id),
                        "embedded": sem_store.get("embedded"),
                        "embed_missing": sem_store.get("missing"),
                        "t_ingest": round(t_ingest - t0, 2),
                        "t_compile": round(t_compile - t_ingest, 2),
                        "t_recall": round(t_recall - t_compile, 2),
                        "t_answer": round(t_answer - t_recall, 2),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        if args.debug:
            print(f"\n=== {qid} :: {question!r} (date {qdate})")
            for item in (envelope.get("evidence") or [])[:EVIDENCE_LIMIT]:
                mid = str(item.get("memory_id") or "")
                date, role, _chunk, _e = meta_by_id.get(mid, ("", "", "", ""))
                print(f"  [EV {date} · {role}] {str(item.get('remembered_as'))[:160]!r}")
            for item in (envelope.get("nonresponsive_evidence") or [])[:5]:
                print(f"  [blocked] {str(item.get('remembered_as'))[:100]!r}")
            print(f"  hypothesis: {hypothesis!r}")
        if args.verbose:
            print(
                f"[{n}/{total}] {qid} mode={mode} "
                f"ev={len(envelope.get('evidence') or [])} "
                f"total={time.perf_counter() - t0:.1f}s :: {hypothesis[:90]!r}"
            )
        else:
            print(f"[{n}/{total}] {qid} {time.perf_counter() - t0:.1f}s")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default=DEFAULT_INPUT)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--qid", default="")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--debug", action="store_true")
    run(parser.parse_args())

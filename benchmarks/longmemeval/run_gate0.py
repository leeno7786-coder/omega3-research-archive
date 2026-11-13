"""Gate 0 — the oracle ceiling.

Feed the LOCAL 4B *perfect* evidence (the oracle file's haystack_sessions are
exactly the answer sessions) under the SAME answer contract as the board run.
The judged score is the answer layer's ceiling per category: any board deficit
below this line is retrieval/evidence-layer, everything above it is prompt or
model. No memory engine in the loop — this isolates the seam.

Usage:
  python benchmarks/longmemeval/run_gate0.py [--limit N] [--qid QID] [--verbose]

Output: G:/LongMemEval/LongMemEval/gate0_predictions.jsonl (+ .meta.jsonl),
judge-contract lines {"question_id", "hypothesis"}. Resumable.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_DATASET_ROOT = Path(os.environ.get("LONGMEMEVAL_ROOT", REPO_ROOT / "benchmarks" / "longmemeval"))
ORACLE = _DATASET_ROOT / "data" / "longmemeval_oracle.json"
OUTPUT = _DATASET_ROOT / "gate0_predictions.jsonl"

TURN_CHARS = 900  # mirror the board's chunk granularity
MAX_EVIDENCE_LINES = 60  # oracle sessions are small; this rarely binds
GEN_MAX_TOKENS = 200
GEN_TIMEOUT = 180.0

SYSTEM_PROMPT = "You are a precise memory assistant. You answer questions about the user from their past conversations."

ABSTAIN = "I don't know — my conversations do not mention it."


def _evidence_lines(entry: dict) -> list:
    lines = []
    sessions = entry.get("haystack_sessions") or []
    dates = entry.get("haystack_dates") or []
    for s_idx, sess in enumerate(sessions):
        date = dates[s_idx] if s_idx < len(dates) else "unknown date"
        for turn in sess:
            role = turn.get("role", "user")
            text = (turn.get("content") or "").strip()
            if not text:
                continue
            lines.append(f"- [{date} · {role}] {text[:TURN_CHARS]}")
    return lines[:MAX_EVIDENCE_LINES]


def _prompt(question: str, question_date: str, evidence: list) -> str:
    block = "\n".join(evidence)
    return (
        f"Evidence from my past conversations (each line is dated):\n{block}\n\n"
        f"Today's date: {question_date}\n"
        f"Question: {question}\n\n"
        "Using only the evidence above, answer the question in ONE short "
        "sentence of your own words — do not quote or repeat evidence lines. "
        "Copy exact values (numbers, names, dates) from the evidence without "
        "changing them; never convert or add numbers. For questions about "
        "when something happened, use the session dates in brackets. Only if "
        "none of the evidence contains the answer, reply: "
        "I don't know — my conversations do not mention it."
    )


def _answer(client, prompt: str):
    for _ in range(3):
        text = client.generate(
            prompt,
            system=SYSTEM_PROMPT,
            max_tokens=GEN_MAX_TOKENS,
            temperature=0.0,
            timeout=GEN_TIMEOUT,
        )
        if text and text.strip():
            return text.strip(), "generated"
        time.sleep(10)
    return ABSTAIN, "generation_failed"


def _answer_v2(client, system: str, prompt: str, max_tokens: int, thinking: bool = False):
    # thinking shapes get a longer leash — the <think> pass burns real time
    timeout = 420.0 if thinking else GEN_TIMEOUT
    for _ in range(3):
        text = client.generate(
            prompt,
            system=system,
            max_tokens=max_tokens,
            temperature=0.0,
            timeout=timeout,
            enable_thinking=thinking,
        )
        if text and text.strip():
            return text.strip(), "generated"
        time.sleep(10)
    return ABSTAIN, "generation_failed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--qid", default=None)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument(
        "--pack", default="", choices=["", "v2"], help="v2 = dynamic answer packs (answer_pack.py)"
    )
    ap.add_argument("--types", default="", help="comma-separated question_type filter")
    ap.add_argument("--output", default="")
    ap.add_argument(
        "--model-lock",
        default="gemma",
        help="required model_id prefix (ladder bake-offs pass "
        "e.g. 'nemotron' EXPLICITLY; the default stays "
        "gemma so a hotswapped runtime can never leak "
        "into a gemma run by accident)",
    )
    args = ap.parse_args()

    global OUTPUT
    if args.output:
        OUTPUT = Path(args.output)
    elif args.pack:
        OUTPUT = Path(str(OUTPUT).replace(".jsonl", f".{args.pack}.jsonl"))
    type_filter = {t.strip() for t in args.types.split(",") if t.strip()}

    from runtime.client import get_runtime_client

    client = get_runtime_client()
    import requests as _rq

    probe = _rq.post(
        "http://127.0.0.1:11434/generate", json={"prompt": "Say OK.", "max_tokens": 4}, timeout=300
    ).json()
    model_id = str(probe.get("model_id") or "")
    if not model_id.startswith(args.model_lock):
        raise SystemExit(
            f"MODEL LOCK VIOLATION: /generate serves {model_id!r}, need prefix {args.model_lock!r}"
        )
    print(f"model lock verified: {model_id}", flush=True)

    entries = json.load(open(ORACLE, encoding="utf-8"))
    done = set()
    if OUTPUT.exists():
        for line in open(OUTPUT, encoding="utf-8"):
            if line.strip():
                done.add(json.loads(line)["question_id"])

    meta_path = Path(str(OUTPUT) + ".meta.jsonl")
    n = 0
    shape = "baseline"
    for entry in entries:
        qid = entry["question_id"]
        if qid in done:
            continue
        if args.qid and qid != args.qid:
            continue
        if type_filter and entry["question_type"] not in type_filter:
            continue
        evidence = _evidence_lines(entry)
        t0 = time.perf_counter()
        if args.pack == "v2":
            from benchmarks.longmemeval import answer_pack

            question = entry["question"]
            shape = answer_pack.classify(question)
            if shape == "FACT":
                # the baseline one-sentence contract earned 90% on its home
                # categories — FACT-shaped questions take that exact path
                hyp, mode = _answer(
                    client, _prompt(question, entry.get("question_date", ""), evidence)
                )
            else:
                raw, mode = _answer_v2(
                    client,
                    answer_pack.system_for(shape),
                    answer_pack.user_prompt(
                        question, entry.get("question_date", ""), "\n".join(evidence)
                    ),
                    answer_pack.max_tokens_for(shape),
                    thinking=answer_pack.thinking_for(shape),
                )
                hyp = answer_pack.extract_final(shape, raw) or raw
        else:
            hyp, mode = _answer(
                client, _prompt(entry["question"], entry.get("question_date", ""), evidence)
            )
        dt = time.perf_counter() - t0
        with open(OUTPUT, "a", encoding="utf-8") as f:
            f.write(json.dumps({"question_id": qid, "hypothesis": hyp}) + "\n")
        with open(meta_path, "a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    {
                        "question_id": qid,
                        "mode": mode,
                        "type": entry["question_type"],
                        "shape": shape,
                        "n_evidence": len(evidence),
                        "t_answer": round(dt, 1),
                    }
                )
                + "\n"
            )
        n += 1
        if args.verbose:
            print(f"[{n}] {qid} ({entry['question_type']}) {dt:.0f}s :: {hyp[:100]}", flush=True)
        else:
            print(f"[{n}] {qid} {dt:.0f}s", flush=True)
        if args.limit and n >= args.limit:
            break
    print("GATE 0 PASS COMPLETE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

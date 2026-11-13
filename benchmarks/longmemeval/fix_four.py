"""Answer the 4 thinking-timeout qids with scaffold prompts, thinking OFF."""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from benchmarks.longmemeval import answer_pack
from benchmarks.longmemeval.run_gate0 import ORACLE, _evidence_lines

QIDS = ["c4a1ceb8", "28dc39ac", "7024f17c", "81507db6"]
PRED = ORACLE.parent.parent / "gate0_predictions.v3.jsonl"

from runtime.client import get_runtime_client

client = get_runtime_client()
entries = {e["question_id"]: e for e in json.load(open(ORACLE, encoding="utf-8"))}
fixed = {}
for qid in QIDS:
    e = entries[qid]
    shape = answer_pack.classify(e["question"])
    evidence = _evidence_lines(e)
    text = client.generate(
        answer_pack.user_prompt(e["question"], e.get("question_date", ""), "\n".join(evidence)),
        system=answer_pack.system_for(shape),
        max_tokens=1200,
        temperature=0.0,
        timeout=240.0,
        enable_thinking=False,
    )
    hyp = answer_pack.extract_final(shape, text or "") or (text or "").strip()
    if not hyp:
        hyp = answer_pack.ABSTAIN
    fixed[qid] = hyp
    print(qid, shape, "::", hyp[:100], flush=True)

lines = []
for line in open(PRED, encoding="utf-8"):
    d = json.loads(line)
    if d["question_id"] in fixed:
        d["hypothesis"] = fixed[d["question_id"]]
    lines.append(json.dumps(d, ensure_ascii=False) + "\n")
open(PRED, "w", encoding="utf-8").writelines(lines)
print("patched", len(fixed))

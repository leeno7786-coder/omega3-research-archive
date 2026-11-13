"""Prove a trained router's ONNX graph reproduces its PyTorch master, using brain deps only.

Run with the brain interpreter (onnxruntime + tokenizers, no torch needed):

    .\\.venv-brain\\Scripts\\python.exe scripts\\intent_router\\verify_onnx.py models\\intent\\<run>

Every probe input recorded by train.py is tokenized with the shipped tokenizer.json,
run through router.onnx on the CPU provider, and compared logit by logit with the
PyTorch values. Exit status 1 if any difference exceeds the recorded tolerance.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

OUTPUTS = ("primary_logits", "intent_logits", "certainty_logits")


def verify(run: Path) -> dict:
    import onnxruntime as ort
    from tokenizers import Tokenizer

    router = json.loads((run / "router.json").read_text(encoding="utf-8"))
    probe = json.loads((run / "parity_probe.json").read_text(encoding="utf-8"))
    tokenizer = Tokenizer.from_file(str(run / "tokenizer.json"))
    tokenizer.no_padding()
    tokenizer.enable_truncation(max_length=int(router["max_length"]))
    session = ort.InferenceSession(str(run / "router.onnx"), providers=["CPUExecutionProvider"])

    worst = {name: 0.0 for name in OUTPUTS}
    for index, text in enumerate(probe["inputs"]):
        encoding = tokenizer.encode(text)
        feeds = {"input_ids": np.array([encoding.ids], dtype=np.int64),
                 "attention_mask": np.array([encoding.attention_mask], dtype=np.int64)}
        for name, value in zip(OUTPUTS, session.run(list(OUTPUTS), feeds)):
            diff = float(np.abs(value[0] - np.asarray(probe[name][index], dtype=np.float32)).max())
            worst[name] = max(worst[name], diff)
    return {"probes": len(probe["inputs"]), "max_abs_diff": worst, "tolerance": probe["tolerance"],
            "ok": all(diff <= float(probe["tolerance"]) for diff in worst.values())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run_dir", type=Path)
    result = verify(parser.parse_args().run_dir)
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())

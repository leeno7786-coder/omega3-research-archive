"""Train Global A's intent router: ModernBERT-base with three heads, on the Radeon 860M (ROCm).

Dev-only. Run with the ROCm training venv, never the portable brain:

    .\\.venv-rocm\\Scripts\\python.exe scripts\\intent_router\\train.py ^
        --base-dir models\\intent\\base\\ModernBERT-base ^
        --base-revision 8949b909ec900327062f0ebf497f51aef5e6f0c8

Heads over one mean-pooled encoder state (see LABELS.md):
  primary     softmax over the four routes (trained on known rows)
  intents     four sigmoids (every route a request explicitly needs)
  certainty   softmax over known / ambiguous / out_of_domain

Training rows are split group-aware into fit / validation / calibration. Temperatures
are fitted on calibration; per-route thresholds, the top1-top2 margin and the
known-certainty threshold are tuned on validation to minimise misrouting cost, where
escalating to Global A's reasoning model costs 1. The development data/v2/eval set and
the blind data/v2/gate set are only reported on; promote.py gates on the latter. Output
under models/intent/<run>/:

  router.onnx          FP32 graph: input_ids, attention_mask -> three logit tensors
  tokenizer.json ...   the tokenizer the brain must use
  router.json          routes, input template, max_length, temperatures, thresholds, costs
  parity_probe.json    probe inputs and PyTorch logits for the brain-side ONNX parity check
  checkpoint/          FP32 master (encoder + heads safetensors)
  artifact.json        base identity, dataset digest, sha256 pins, metrics
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import math
import random
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[2])]
from core.paths import product_relative_path  # noqa: E402
from dataset import CERTAINTY, ROUTES, Row, eval_rows, gate_rows, normalize, router_input, training_rows  # noqa: E402
from global_loop.intent.router import ESCALATE, decide, sigmoid, softmax  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MODEL_ID = "answerdotai/ModernBERT-base"
# Gold route (rows) x decision (FREEFORM_CHAT, CODING_AGENT, BROWSER_AGENT, DOCUMENT_CREATION, ESCALATE).
COST = np.array([
    [0, 20, 15, 8, 1],
    [4, 0, 15, 8, 1],
    [4, 20, 0, 8, 1],
    [4, 20, 15, 0, 1],
], dtype=np.float64)
# An ambiguous or out-of-domain row should escalate; routing it costs that route's worst case.
UNCERTAIN_ROUTED_COST = np.array([4, 20, 15, 8], dtype=np.float64)


# ── data ──────────────────────────────────────────────────────────────────────


def _unit(row: Row) -> str:
    """Rows that must stay in one split: paraphrase families, boundary groups, one short text."""
    folder = Path(row.source).parent.as_posix()
    if row.kind in {"paraphrase", "boundary"} and row.group:
        return f"{folder}::{row.group}"
    if row.kind == "short":
        return f"short::{normalize(row.text)}"
    return f"{row.source}:{row.line}"


def split_rows(rows: list[Row], val_fraction: float, cal_fraction: float,
               seed: int) -> tuple[list[Row], list[Row], list[Row]]:
    """Group-aware stratified split; strata too small to split stay entirely in fit."""
    strata: dict[tuple, dict[str, list[Row]]] = defaultdict(lambda: defaultdict(list))
    for row in rows:
        strata[(row.certainty, row.primary or "-", row.kind)][_unit(row)].append(row)
    rng = random.Random(seed)
    minimum = math.ceil(1.0 / max(min(val_fraction, cal_fraction), 1e-9))
    fit: list[Row] = []
    val: list[Row] = []
    cal: list[Row] = []
    for stratum in sorted(strata):
        units = sorted(strata[stratum].items())
        rng.shuffle(units)
        total = sum(len(members) for _, members in units)
        if total < minimum:
            fit.extend(member for _, members in units for member in members)
            continue
        in_val = in_cal = 0
        for _, members in units:
            if in_val < total * val_fraction:
                val.extend(members)
                in_val += len(members)
            elif in_cal < total * cal_fraction:
                cal.extend(members)
                in_cal += len(members)
            else:
                fit.extend(members)
    return fit, val, cal


def targets(rows: list[Row]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    primary = np.array([ROUTES.index(r.primary) if r.certainty == "known" else -100 for r in rows])
    intents = np.zeros((len(rows), len(ROUTES)), dtype=np.float32)
    for i, row in enumerate(rows):
        for route in row.intents:
            intents[i, ROUTES.index(route)] = 1.0
    certainty = np.array([CERTAINTY.index(r.certainty) for r in rows])
    return primary, intents, certainty


def dataset_digest(rows: list[Row]) -> str:
    lines = sorted(f"{r.primary}\t{','.join(sorted(r.intents))}\t{r.certainty}\t{normalize(r.context)}\t{normalize(r.text)}"
                   for r in rows)
    return hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()


# ── model ─────────────────────────────────────────────────────────────────────


def build_model(encoder_dir: Path, attn_implementation: str):
    import torch
    from transformers import AutoModel

    class RouterModel(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__()
            self.encoder = AutoModel.from_pretrained(str(encoder_dir), local_files_only=True,
                                                     attn_implementation=attn_implementation,
                                                     dtype=torch.float32)
            hidden = int(self.encoder.config.hidden_size)
            self.dropout = torch.nn.Dropout(0.1)
            self.shared = torch.nn.Sequential(torch.nn.Linear(hidden, hidden), torch.nn.GELU(),
                                              torch.nn.LayerNorm(hidden))
            self.primary = torch.nn.Linear(hidden, len(ROUTES))
            self.intents = torch.nn.Linear(hidden, len(ROUTES))
            self.certainty = torch.nn.Linear(hidden, len(CERTAINTY))

        def forward(self, input_ids, attention_mask):
            hidden = self.encoder(input_ids=input_ids, attention_mask=attention_mask).last_hidden_state
            mask = attention_mask.unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1.0)
            state = self.shared(self.dropout(pooled))
            return self.primary(state), self.intents(state), self.certainty(state)

    return RouterModel()


def head_state(model) -> dict:
    return {name: tensor.detach().cpu().contiguous() for name, tensor in model.state_dict().items()
            if not name.startswith("encoder.")}


# ── calibration and decisions ─────────────────────────────────────────────────


def fit_softmax_temperature(logits: np.ndarray, gold: np.ndarray) -> float:
    from scipy.optimize import minimize_scalar

    if len(gold) == 0:
        return 1.0

    def nll(log_t: float) -> float:
        probs = softmax(logits / math.exp(log_t))
        return float(-np.log(probs[np.arange(len(gold)), gold] + 1e-12).mean())

    return float(math.exp(minimize_scalar(nll, bounds=(-3.0, 3.0), method="bounded").x))


def fit_sigmoid_temperature(logits: np.ndarray, gold: np.ndarray) -> float:
    from scipy.optimize import minimize_scalar

    def nll(log_t: float) -> float:
        probs = np.clip(sigmoid(logits / math.exp(log_t)), 1e-7, 1 - 1e-7)
        return float(-(gold * np.log(probs) + (1 - gold) * np.log(1 - probs)).mean())

    return float(math.exp(minimize_scalar(nll, bounds=(-3.0, 3.0), method="bounded").x))


def cost_table(rows: list[Row]) -> np.ndarray:
    """Per-row cost of every decision: the four routes, then ESCALATE."""
    table = np.empty((len(rows), len(ROUTES) + 1))
    for i, row in enumerate(rows):
        if row.certainty == "known":
            table[i] = COST[ROUTES.index(row.primary)]
        else:
            table[i, :len(ROUTES)] = UNCERTAIN_ROUTED_COST
            table[i, ESCALATE] = 0.0
    return table


def row_costs(rows: list[Row], decisions: np.ndarray) -> np.ndarray:
    return cost_table(rows)[np.arange(len(rows)), decisions]


# The router routes only when it is more likely right than not. A small validation split
# can prefer lower cut-offs (the first full run tuned P(known) down to 0.39 and routed
# "test" to the coding agent), so neither a route threshold nor the known-certainty
# threshold is tuned below one half.
DECISION_FLOOR = 0.5
# A costly misroute made at or above this calibrated confidence is reported as confident.
CONFIDENT_MISROUTE = 0.9


AGENT_ROUTES = ("CODING_AGENT", "BROWSER_AGENT")


def weighted_cost_table(rows: list[Row], agent_weight: float) -> np.ndarray:
    """The cost table with every decision that opens an agent lane multiplied by ``agent_weight``.

    Correct agent routes still cost 0; only wrong ones grow, so tuning trades escalations for
    fewer misroutes into the lanes that change files or drive a browser.
    """
    table = cost_table(rows)
    for route in AGENT_ROUTES:
        table[:, ROUTES.index(route)] *= agent_weight
    return table


def tune_decision(rows: list[Row], primary_probs: np.ndarray, certainty_probs: np.ndarray,
                  table: np.ndarray | None = None) -> dict:
    """Coordinate descent over per-route thresholds, margin and known threshold on validation.

    ``table`` is the per-row decision cost the search minimises (default: the standard cost);
    the reported validation_mean_cost is always the standard cost of the chosen rule.
    """
    thresholds = np.full(len(ROUTES), 0.9)
    margin, known_threshold = 0.3, DECISION_FLOOR
    route_grid = np.round(np.arange(DECISION_FLOOR, 0.9951, 0.005), 3)
    margin_grid = np.round(np.arange(0.0, 0.901, 0.01), 3)
    known_grid = np.round(np.arange(DECISION_FLOOR, 0.9901, 0.01), 3)
    table = cost_table(rows) if table is None else table
    index = np.arange(len(rows))

    def score(th: np.ndarray, mg: float, kt: float) -> tuple[float, float]:
        decisions = decide(primary_probs, certainty_probs, th, mg, kt)
        return float(table[index, decisions].mean()), float((decisions == ESCALATE).mean())

    best = score(thresholds, margin, known_threshold)
    for _ in range(5):
        for route in range(len(ROUTES)):
            for value in route_grid:
                trial = thresholds.copy()
                trial[route] = value
                result = score(trial, margin, known_threshold)
                if result < best:
                    best, thresholds = result, trial
        for value in margin_grid:
            result = score(thresholds, float(value), known_threshold)
            if result < best:
                best, margin = result, float(value)
        for value in known_grid:
            result = score(thresholds, margin, float(value))
            if result < best:
                best, known_threshold = result, float(value)
    chosen = decide(primary_probs, certainty_probs, thresholds, margin, known_threshold)
    return {"thresholds": {route: float(thresholds[i]) for i, route in enumerate(ROUTES)},
            "margin": margin, "known_threshold": known_threshold,
            "validation_mean_cost": round(float(cost_table(rows)[index, chosen].mean()), 5),
            "validation_objective_cost": round(best[0], 5), "validation_escalation_rate": round(best[1], 4)}


def tune_intent_thresholds(intent_probs: np.ndarray, gold: np.ndarray) -> dict:
    tuned = {}
    for i, route in enumerate(ROUTES):
        best_f1, best_t = -1.0, 0.5
        for t in np.round(np.arange(0.10, 0.91, 0.01), 2):
            predicted = intent_probs[:, i] >= t
            tp = float((predicted & (gold[:, i] == 1)).sum())
            fp = float((predicted & (gold[:, i] == 0)).sum())
            fn = float((~predicted & (gold[:, i] == 1)).sum())
            f1 = 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else 0.0
            if f1 > best_f1:
                best_f1, best_t = f1, float(t)
        tuned[route] = best_t
    return tuned


def expected_calibration_error(probs: np.ndarray, gold: np.ndarray, bins: int = 10) -> float:
    if len(gold) == 0:
        return 0.0
    confidence = probs.max(axis=1)
    correct = probs.argmax(axis=1) == gold
    edges = np.linspace(0.0, 1.0, bins + 1)
    return float(sum(((confidence > lo) & (confidence <= hi)).mean()
                     * abs(correct[(confidence > lo) & (confidence <= hi)].mean()
                           - confidence[(confidence > lo) & (confidence <= hi)].mean())
                     for lo, hi in zip(edges[:-1], edges[1:])
                     if ((confidence > lo) & (confidence <= hi)).any()))


# ── inference over row lists ──────────────────────────────────────────────────


def logits_for(model, tokenizer, rows: list[Row], max_length: int, device: str,
               batch_size: int = 64) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    import torch

    model.eval()
    outputs: list[tuple[np.ndarray, ...]] = []
    with torch.inference_mode():
        for start in range(0, len(rows), batch_size):
            batch = rows[start:start + batch_size]
            encoded = tokenizer([router_input(r.text, r.context) for r in batch], padding=True,
                                truncation=True, max_length=max_length, return_tensors="pt").to(device)
            primary, intents, certainty = model(encoded["input_ids"], encoded["attention_mask"])
            outputs.append(tuple(t.float().cpu().numpy() for t in (primary, intents, certainty)))
    if not outputs:
        empty = np.zeros((0, len(ROUTES)))
        return empty, empty.copy(), np.zeros((0, len(CERTAINTY)))
    return tuple(np.concatenate([o[i] for o in outputs]) for i in range(3))  # type: ignore[return-value]


def report(rows: list[Row], logits: tuple[np.ndarray, np.ndarray, np.ndarray], calibration: dict,
           decision: dict, intent_thresholds: dict) -> dict:
    primary_probs = softmax(logits[0] / calibration["primary_temperature"])
    intent_probs = sigmoid(logits[1] / calibration["intent_temperature"])
    certainty_probs = softmax(logits[2] / calibration["certainty_temperature"])
    thresholds = np.array([decision["thresholds"][route] for route in ROUTES])
    decisions = decide(primary_probs, certainty_probs, thresholds, decision["margin"], decision["known_threshold"])
    costs = row_costs(rows, decisions)
    columns = [*ROUTES, "ESCALATE"]
    matrix: dict[str, Counter] = defaultdict(Counter)
    misroutes: Counter = Counter()
    errors = []
    for index, (row, choice, cost) in enumerate(zip(rows, decisions, costs)):
        gold = row.primary if row.certainty == "known" else row.certainty.upper()
        matrix[gold][columns[choice]] += 1
        if cost > 1:
            misroutes[f"{gold}->{columns[choice]}"] += 1
            errors.append({"text": row.text[:200], "context": row.context, "gold": gold,
                           "decided": columns[choice], "cost": float(cost),
                           "confidence": round(float(primary_probs[index, choice]), 4),
                           "kind": row.kind, "suite": row.suite})
    by_group: dict[str, dict] = {}
    for label, selector in (("kind", lambda r: r.kind), ("suite", lambda r: r.suite or "-"),
                            ("certainty", lambda r: r.certainty)):
        groups: dict[str, list[int]] = defaultdict(list)
        for i, row in enumerate(rows):
            groups[selector(row)].append(i)
        by_group[label] = {name: {"n": len(ids), "mean_cost": round(float(costs[ids].mean()), 4),
                                  "escalated": round(float((decisions[ids] == ESCALATE).mean()), 4)}
                           for name, ids in sorted(groups.items())}
    gold_intents = targets(rows)[1]
    predicted_intents = np.column_stack([intent_probs[:, i] >= intent_thresholds[r] for i, r in enumerate(ROUTES)])
    multi = [i for i, r in enumerate(rows) if len(r.intents) > 1]
    exact_multi = float(np.mean([(predicted_intents[i] == (gold_intents[i] == 1)).all() for i in multi])) if multi else None
    known = [i for i, r in enumerate(rows) if r.certainty == "known"]
    known_gold = np.array([ROUTES.index(rows[i].primary) for i in known])
    return {
        "n": len(rows),
        "mean_cost": round(float(costs.mean()), 5) if len(rows) else None,
        "escalation_rate": round(float((decisions == ESCALATE).mean()), 4) if len(rows) else None,
        "costly_misroutes": sum(misroutes.values()),
        # A confident mistake is worse than an unsure one: it is the kind no threshold catches.
        "confident_costly_misroutes": sum(1 for error in errors if error["confidence"] >= CONFIDENT_MISROUTE),
        "misroutes_by_type": dict(misroutes.most_common()),
        "route_recall": {route: round(matrix[route][route] / sum(matrix[route].values()), 4)
                         for route in ROUTES if sum(matrix[route].values())},
        "decision_matrix": {gold: dict(counts) for gold, counts in sorted(matrix.items())},
        "breakdown": by_group,
        "known_argmax_accuracy": round(float((primary_probs[known].argmax(axis=1) == known_gold).mean()), 4) if known else None,
        "primary_ece": round(expected_calibration_error(primary_probs[known], known_gold), 4) if known else None,
        "mixed_intent_exact_match": round(exact_multi, 4) if exact_multi is not None else None,
        "errors": sorted(errors, key=lambda e: -e["cost"])[:60],
    }


def calibrate_and_tune(model, tokenizer, val: list[Row], cal: list[Row], max_length: int,
                       device: str, agent_weight: float = 1.0
                       ) -> tuple[dict, dict, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Fit temperatures on calibration rows, then tune the decision rule on validation rows."""
    cal_logits = logits_for(model, tokenizer, cal, max_length, device)
    cal_primary, cal_intents, cal_certainty = targets(cal)
    known = cal_primary >= 0
    calibration = {
        "primary_temperature": fit_softmax_temperature(cal_logits[0][known], cal_primary[known]),
        "intent_temperature": fit_sigmoid_temperature(cal_logits[1], cal_intents),
        "certainty_temperature": fit_softmax_temperature(cal_logits[2], cal_certainty),
    }
    val_logits = logits_for(model, tokenizer, val, max_length, device)
    decision = tune_decision(val, softmax(val_logits[0] / calibration["primary_temperature"]),
                             softmax(val_logits[2] / calibration["certainty_temperature"]),
                             table=weighted_cost_table(val, agent_weight))
    return calibration, decision, val_logits


# ── export ────────────────────────────────────────────────────────────────────


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def export_onnx(checkpoint: Path, out: Path, tokenizer, probe_rows: list[Row], max_length: int) -> dict:
    """Rebuild the FP32 model with eager attention on CPU, export, and record parity probes."""
    import torch
    from safetensors.torch import load_file

    model = build_model(checkpoint / "encoder", attn_implementation="eager")
    missing, unexpected = model.load_state_dict(load_file(str(checkpoint / "heads.safetensors")), strict=False)
    if unexpected or any(not name.startswith("encoder.") for name in missing):
        raise SystemExit(f"head checkpoint mismatch: missing={missing} unexpected={unexpected}")
    model.eval()
    sample = tokenizer([router_input("open the settings page", "")], return_tensors="pt",
                       padding=True, truncation=True, max_length=max_length)
    kwargs = {"input_names": ["input_ids", "attention_mask"],
              "output_names": ["primary_logits", "intent_logits", "certainty_logits"],
              "dynamic_axes": {"input_ids": {0: "batch", 1: "sequence"},
                               "attention_mask": {0: "batch", 1: "sequence"},
                               "primary_logits": {0: "batch"}, "intent_logits": {0: "batch"},
                               "certainty_logits": {0: "batch"}},
              "opset_version": 17, "do_constant_folding": True}
    if "dynamo" in inspect.signature(torch.onnx.export).parameters:
        kwargs["dynamo"] = False
    with torch.inference_mode():
        torch.onnx.export(model, (sample["input_ids"], sample["attention_mask"]), str(out / "router.onnx"), **kwargs)
    primary, intents, certainty = logits_for(model, tokenizer, probe_rows, max_length, "cpu", batch_size=16)
    probe = {"inputs": [router_input(r.text, r.context) for r in probe_rows],
             "primary_logits": primary.tolist(), "intent_logits": intents.tolist(),
             "certainty_logits": certainty.tolist(), "tolerance": 1e-3}
    (out / "parity_probe.json").write_text(json.dumps(probe), encoding="utf-8")
    return {"probe_rows": len(probe_rows)}


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-dir", type=Path, required=True)
    parser.add_argument("--base-revision", required=True)
    parser.add_argument("--out-root", type=Path, default=REPO / "models" / "intent")
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--batch-size", type=int, default=16, help="micro-batch size")
    parser.add_argument("--grad-accum", type=int, default=2, help="micro-batches per optimizer step")
    parser.add_argument("--no-gradient-checkpointing", action="store_true")
    parser.add_argument("--lr", type=float, default=3e-5)
    parser.add_argument("--head-lr", type=float, default=1e-4)
    parser.add_argument("--warmup", type=float, default=0.1)
    parser.add_argument("--max-length", type=int, default=128)
    parser.add_argument("--cost-weight", type=float, default=0.5)
    parser.add_argument("--agent-cost-weight", type=float, default=1.0,
                        help="multiplier on misroutes into the coding and browser lanes when tuning the decision rule")
    parser.add_argument("--val-fraction", type=float, default=0.10)
    parser.add_argument("--cal-fraction", type=float, default=0.10)
    parser.add_argument("--seed", type=int, default=20260914)
    parser.add_argument("--no-onnx", action="store_true")
    options = parser.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    import gc

    import torch
    from safetensors.torch import load_file, save_file
    from transformers import AutoTokenizer, get_linear_schedule_with_warmup

    if not torch.cuda.is_available():
        raise SystemExit("ROCm GPU not visible; run from .venv-rocm")
    device = "cuda"
    random.seed(options.seed)
    np.random.seed(options.seed)
    torch.manual_seed(options.seed)

    train, train_errors = training_rows()
    evaluation, eval_errors = eval_rows()
    gate, gate_errors = gate_rows()
    if train_errors or eval_errors or gate_errors:
        raise SystemExit("dataset errors; run dataset.py first:\n"
                         + "\n".join((train_errors + eval_errors + gate_errors)[:20]))
    fit, val, cal = split_rows(train, options.val_fraction, options.cal_fraction, options.seed)
    print(f"fit {len(fit)} val {len(val)} cal {len(cal)} eval {len(evaluation)} gate {len(gate)}")
    print(f"fit certainty {dict(Counter(r.certainty for r in fit))} primary {dict(Counter(r.primary for r in fit))}")

    run = f"router-modernbert-base-{time.strftime('%Y%m%d-%H%M%S')}"
    out = options.out_root / run
    out.mkdir(parents=True, exist_ok=False)
    tokenizer = AutoTokenizer.from_pretrained(str(options.base_dir), local_files_only=True)
    model = build_model(options.base_dir, attn_implementation="sdpa")
    if not options.no_gradient_checkpointing:
        model.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model = model.to(device)

    fit_primary, fit_intents, fit_certainty = targets(fit)
    certainty_counts = np.bincount(fit_certainty, minlength=len(CERTAINTY)).astype(np.float64)
    certainty_weights = torch.tensor(np.clip(certainty_counts.sum() / (len(CERTAINTY) * np.maximum(certainty_counts, 1)),
                                             0.5, 10.0), dtype=torch.float32, device=device)
    route_cost = torch.tensor(COST[:, :len(ROUTES)] / COST.max(), dtype=torch.float32, device=device)

    encoder_params = [p for n, p in model.named_parameters() if n.startswith("encoder.")]
    head_params = [p for n, p in model.named_parameters() if not n.startswith("encoder.")]
    optimizer = torch.optim.AdamW([{"params": encoder_params, "lr": options.lr},
                                   {"params": head_params, "lr": options.head_lr}], weight_decay=0.01,
                                  foreach=False)
    micro_batches = math.ceil(len(fit) / options.batch_size)
    total_steps = math.ceil(micro_batches / options.grad_accum) * options.epochs
    scheduler = get_linear_schedule_with_warmup(optimizer, int(options.warmup * total_steps), total_steps)
    # The iGPU's memory is system RAM: keep the best weights on disk, not as a second copy in memory.
    best_path = out / "best_state.safetensors"
    best_score, history = None, []
    started = time.time()
    order = list(range(len(fit)))
    step = 0
    print(f"training: {options.epochs} epochs x {micro_batches} micro-batches of {options.batch_size}, "
          f"grad accum {options.grad_accum}, {total_steps} optimizer steps, "
          f"gradient checkpointing {'off' if options.no_gradient_checkpointing else 'on'}")
    for epoch in range(options.epochs):
        model.train()
        random.Random(options.seed + epoch).shuffle(order)
        running = Counter()
        optimizer.zero_grad(set_to_none=True)
        batches = [order[s:s + options.batch_size] for s in range(0, len(order), options.batch_size)]
        for index, ids in enumerate(batches):
            batch = [fit[i] for i in ids]
            # Padding to a multiple of 32 keeps the set of tensor shapes (and allocator blocks) small.
            encoded = tokenizer([router_input(r.text, r.context) for r in batch], padding=True, truncation=True,
                                max_length=options.max_length, pad_to_multiple_of=32,
                                return_tensors="pt").to(device)
            gold_primary = torch.tensor(fit_primary[ids], device=device)
            gold_intents = torch.tensor(fit_intents[ids], device=device)
            gold_certainty = torch.tensor(fit_certainty[ids], device=device)
            with torch.autocast("cuda", dtype=torch.bfloat16):
                primary, intents, certainty = model(encoded["input_ids"], encoded["attention_mask"])
            primary, intents, certainty = primary.float(), intents.float(), certainty.float()
            known = gold_primary >= 0
            loss_primary = torch.zeros((), device=device)
            if known.any():
                loss_primary = torch.nn.functional.cross_entropy(primary[known], gold_primary[known])
                expected_cost = (torch.softmax(primary[known], dim=-1) * route_cost[gold_primary[known]]).sum(-1).mean()
                loss_primary = loss_primary + options.cost_weight * expected_cost
            loss_intents = torch.nn.functional.binary_cross_entropy_with_logits(intents, gold_intents)
            loss_certainty = torch.nn.functional.cross_entropy(certainty, gold_certainty, weight=certainty_weights)
            loss = loss_primary + 0.5 * loss_intents + 0.5 * loss_certainty
            (loss / options.grad_accum).backward()
            running.update(loss=float(loss), primary=float(loss_primary), intents=float(loss_intents),
                           certainty=float(loss_certainty), micro=1)
            if (index + 1) % options.grad_accum and index + 1 != len(batches):
                continue
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad(set_to_none=True)
            step += 1
            if step % 25 == 0:
                print(f"step {step}/{total_steps} epoch {epoch + 1} loss {running['loss'] / running['micro']:.4f} "
                      f"{time.time() - started:.0f}s gpu peak {torch.cuda.max_memory_allocated() / 2**30:.2f} GiB "
                      f"reserved {torch.cuda.memory_reserved() / 2**30:.2f} GiB")
        # Select the epoch the deployed router would be: calibrated and tuned, scored by
        # validation misrouting cost, then by how often it escalates.
        _, epoch_decision, _ = calibrate_and_tune(model, tokenizer, val, cal, options.max_length, device,
                                                  options.agent_cost_weight)
        score = (epoch_decision["validation_objective_cost"], epoch_decision["validation_escalation_rate"])
        epoch_log = {"epoch": epoch + 1, "val_mean_cost_tuned": score[0], "val_escalation_tuned": score[1],
                     **{k: round(v / running["micro"], 5) for k, v in running.items() if k != "micro"}}
        history.append(epoch_log)
        print(json.dumps(epoch_log))
        if best_score is None or score < best_score:
            best_score = score
            save_file({name: tensor.detach().cpu().contiguous() for name, tensor in model.state_dict().items()},
                      str(best_path))
    model.load_state_dict(load_file(str(best_path), device=device))
    best_path.unlink()
    train_seconds = round(time.time() - started, 1)
    print(f"training done in {train_seconds}s; calibrating and tuning")

    calibration, decision, val_logits = calibrate_and_tune(model, tokenizer, val, cal, options.max_length, device,
                                                           options.agent_cost_weight)
    val_intents = targets(val)[1]
    intent_thresholds = tune_intent_thresholds(sigmoid(val_logits[1] / calibration["intent_temperature"]), val_intents)
    metrics = {"train_seconds": train_seconds, "history": history, "calibration": calibration,
               "decision": decision, "intent_thresholds": intent_thresholds,
               "validation": report(val, val_logits, calibration, decision, intent_thresholds),
               "eval": report(evaluation, logits_for(model, tokenizer, evaluation, options.max_length, device),
                              calibration, decision, intent_thresholds) if evaluation else None,
               "gate": report(gate, logits_for(model, tokenizer, gate, options.max_length, device),
                              calibration, decision, intent_thresholds) if gate else None}

    checkpoint = out / "checkpoint"
    model.encoder.save_pretrained(str(checkpoint / "encoder"), safe_serialization=True)
    save_file(head_state(model), str(checkpoint / "heads.safetensors"))
    tokenizer.save_pretrained(str(out))
    router = {
        "schema": 1, "model": MODEL_ID, "routes": list(ROUTES), "certainty": list(CERTAINTY),
        "input_template": {"with_context": "[PREVIOUS ASSISTANT ACTION] {context} [CURRENT USER MESSAGE] {text}",
                           "without_context": "[CURRENT USER MESSAGE] {text}"},
        "pooling": "masked_mean", "max_length": options.max_length,
        "temperatures": calibration, "thresholds": decision["thresholds"], "margin": decision["margin"],
        "known_threshold": decision["known_threshold"], "intent_thresholds": intent_thresholds,
        "cost": {"gold_by_decision": COST.tolist(), "uncertain_routed": UNCERTAIN_ROUTED_COST.tolist(),
                 "decisions": [*ROUTES, "ESCALATE"]},
    }
    (out / "router.json").write_text(json.dumps(router, indent=2), encoding="utf-8")
    # Release the GPU training state before the CPU export rebuilds the model: both live in system RAM.
    del model, optimizer, scheduler
    gc.collect()
    torch.cuda.empty_cache()
    if not options.no_onnx:
        print("exporting ONNX")
        probe_rows = (evaluation or val)[:48]
        metrics["onnx"] = export_onnx(checkpoint, out, tokenizer, probe_rows, options.max_length)

    pins = {path.relative_to(out).as_posix(): sha256(path) for path in sorted(out.rglob("*"))
            if path.is_file() and path.name != "artifact.json"}
    base_pins = {path.relative_to(options.base_dir).as_posix(): sha256(path)
                 for path in sorted(options.base_dir.rglob("*")) if path.is_file() and ".cache" not in path.parts}
    artifact = {"run": run, "model": MODEL_ID, "base_revision": options.base_revision, "base_pins": base_pins,
                "dataset_digest": dataset_digest(train), "splits": {"fit": len(fit), "val": len(val), "cal": len(cal),
                                                                      "eval": len(evaluation), "gate": len(gate)},
                "options": {k: (product_relative_path(v, omega_root=REPO) if isinstance(v, Path) else v) for k, v in vars(options).items()},
                "versions": {"torch": torch.__version__, "transformers": __import__("transformers").__version__},
                "metrics": metrics, "pins": pins}
    (out / "artifact.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")
    summary_keys = ("n", "mean_cost", "escalation_rate", "costly_misroutes", "confident_costly_misroutes",
                    "misroutes_by_type", "route_recall", "known_argmax_accuracy", "mixed_intent_exact_match")
    summaries = {split: ({k: metrics[split][k] for k in summary_keys} if metrics[split] else {})
                 for split in ("eval", "gate")}
    print(json.dumps({"run": run, "decision": decision, **summaries}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())

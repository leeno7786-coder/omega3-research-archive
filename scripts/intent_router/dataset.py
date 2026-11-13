"""Load and validate the Global A intent router dataset (schema v2, see LABELS.md).

Training rows live under ``data/v2/train/<kind or source>/*.jsonl`` and the blind
promotion gate under ``data/v2/gate/*.jsonl`` (written from LABELS.md alone; only its
aggregate results are ever read). ``data/v2/eval/*.jsonl`` holds an optional
report-only development set; the first one was inspected while diagnosing runs and now
trains as ``train/dev_eval``. ``python dataset.py`` prints composition and fails on
schema errors, conflicting or duplicate rows, near-duplicates, and training or
evaluation rows leaking into the gate.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from global_loop.intent.router import CERTAINTY, ROUTES, router_input  # noqa: E402,F401

KINDS = ("normal", "paraphrase", "short", "conversational", "mixed", "boundary", "adversarial")
COMPOSITION_TARGETS = {"normal": 0.40, "paraphrase": 0.15, "short": 0.10, "conversational": 0.10,
                       "mixed": 0.10, "boundary": 0.10, "adversarial": 0.05}
DATA = Path(__file__).resolve().parent / "data"
V2 = DATA / "v2"
NEAR_DUPLICATE_JACCARD = 0.9
_WORD = re.compile(r"\w+")
_CONTEXT = re.compile(r"^(FREEFORM_CHAT|CODING_AGENT|BROWSER_AGENT|DOCUMENT_CREATION): \S.*$")
# Suites whose rows intentionally repeat a message (variants or context twins).
_REPEATING_SUITES = {"invariance", "context_flip"}


@dataclass(frozen=True)
class Row:
    text: str
    context: str
    primary: str | None
    intents: tuple[str, ...]
    certainty: str
    kind: str
    group: str
    suite: str
    source: str
    line: int

    @property
    def key(self) -> tuple[str, str]:
        return normalize(self.text), normalize(self.context)


def normalize(text: str) -> str:
    """Duplicate key: NFKC-folded word tokens in any script; symbol-only text keeps its own form."""
    folded = unicodedata.normalize("NFKC", str(text or "")).casefold()
    words = _WORD.findall(folded)
    return " ".join(words) if words else " ".join(folded.split())


def _validate(item: dict) -> str | None:
    text, primary, intents = item.get("text"), item.get("primary"), item.get("intents")
    certainty, kind = item.get("certainty"), item.get("kind")
    if not isinstance(text, str) or not text.strip():
        return "missing text"
    if "context" in item and (not isinstance(item["context"], str) or not _CONTEXT.match(item["context"])):
        return f"bad context {item.get('context')!r}"
    if certainty not in CERTAINTY:
        return f"bad certainty {certainty!r}"
    if kind not in KINDS:
        return f"bad kind {kind!r}"
    if not isinstance(intents, list) or len(set(intents)) != len(intents) or any(i not in ROUTES for i in intents):
        return f"bad intents {intents!r}"
    if certainty == "known":
        if primary not in ROUTES or primary not in intents:
            return f"known row needs a route primary inside intents (primary={primary!r})"
    elif primary is not None:
        return f"{certainty} row must have primary null"
    if certainty == "out_of_domain" and intents:
        return "out_of_domain row must have empty intents"
    return None


def load_rows(paths: list[Path]) -> tuple[list[Row], list[str]]:
    rows: list[Row] = []
    errors: list[str] = []
    for path in paths:
        for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{path.relative_to(DATA).as_posix()}:{number}"
            if not raw.strip():
                errors.append(f"{where}: blank line")
                continue
            try:
                item = json.loads(raw)
            except json.JSONDecodeError as exc:
                errors.append(f"{where}: invalid JSON ({exc.msg})")
                continue
            problem = _validate(item) if isinstance(item, dict) else "row is not an object"
            if problem:
                errors.append(f"{where}: {problem}")
                continue
            rows.append(Row(item["text"].strip(), str(item.get("context") or ""), item["primary"],
                            tuple(item["intents"]), item["certainty"], item["kind"],
                            str(item.get("group") or ""), str(item.get("suite") or ""),
                            path.relative_to(DATA).as_posix(), number))
    return rows, errors


def training_rows() -> tuple[list[Row], list[str]]:
    return load_rows(sorted((V2 / "train").glob("*/*.jsonl")))


def eval_rows() -> tuple[list[Row], list[str]]:
    return load_rows(sorted((V2 / "eval").glob("*.jsonl")))


def gate_rows() -> tuple[list[Row], list[str]]:
    return load_rows(sorted((V2 / "gate").glob("*.jsonl")))


def _label(row: Row) -> tuple:
    return row.primary, tuple(sorted(row.intents)), row.certainty


def duplicates(rows: list[Row]) -> list[str]:
    seen: dict[tuple[str, str], Row] = {}
    problems: list[str] = []
    for row in rows:
        if row.suite in _REPEATING_SUITES:
            continue
        first = seen.setdefault(row.key, row)
        if first is row:
            continue
        kind = "CONFLICT" if _label(first) != _label(row) else "duplicate"
        problems.append(f"{kind} {row.source}:{row.line} = {first.source}:{first.line}: {row.text!r}")
    return problems


def near_duplicates(rows: list[Row]) -> list[str]:
    buckets: dict[tuple, list[tuple[Row, frozenset[str]]]] = defaultdict(list)
    problems: list[str] = []
    for row in rows:
        tokens = frozenset(normalize(row.text).split())
        if len(tokens) < 4 or row.kind == "paraphrase":
            continue
        bucket = buckets[(row.primary, row.context)]
        for other, other_tokens in bucket:
            if len(tokens & other_tokens) / len(tokens | other_tokens) >= NEAR_DUPLICATE_JACCARD:
                problems.append(f"near-duplicate {row.source}:{row.line} ~ {other.source}:{other.line}: {row.text!r}")
                break
        bucket.append((row, tokens))
    return problems


def leakage(train: list[Row], evaluation: list[Row]) -> list[str]:
    train_keys = {row.key: row for row in train}
    return [f"LEAK {row.source}:{row.line} also in {train_keys[row.key].source}: {row.text!r}"
            for row in evaluation if row.key in train_keys]


def composition(rows: list[Row]) -> dict[str, tuple[int, float, float]]:
    counts = Counter(row.kind for row in rows)
    total = max(1, len(rows))
    return {kind: (counts[kind], round(counts[kind] / total, 3), target)
            for kind, target in COMPOSITION_TARGETS.items()}


def report() -> int:
    train, train_errors = training_rows()
    evaluation, eval_errors = eval_rows()
    gate, gate_errors = gate_rows()
    problems = [*train_errors, *eval_errors, *gate_errors, *duplicates(train), *near_duplicates(train),
                *duplicates(evaluation), *leakage(train, evaluation), *duplicates(gate),
                *leakage(train, gate), *leakage(evaluation, gate)]
    print(f"train rows {len(train)}; certainty {dict(Counter(r.certainty for r in train))}")
    print(f"train primary {dict(Counter(r.primary for r in train))}")
    print("train composition (count, share, target):")
    for kind, values in composition(train).items():
        print(f"  {kind:15s} {values}")
    print(f"train with context {sum(bool(r.context) for r in train)}; "
          f"multi-intent {sum(len(r.intents) > 1 for r in train)}")
    print(f"eval rows {len(evaluation)}; suites {dict(Counter(r.suite or '-' for r in evaluation))}")
    print(f"gate rows {len(gate)}; certainty {dict(Counter(r.certainty for r in gate))}; "
          f"primary {dict(Counter(r.primary for r in gate))}; kinds {dict(Counter(r.kind for r in gate))}")
    for problem in problems[:200]:
        print(problem)
    print(f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(report())

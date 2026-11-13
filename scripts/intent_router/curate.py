"""Apply recorded curation decisions to the v2 dataset and keep evaluation blind.

    .\\.venv-brain\\Scripts\\python.exe scripts\\intent_router\\curate.py [--dry-run] [--skip DIR ...]

1. Every relabel in ``data/v2/curation.json`` is applied to every row, in training,
   evaluation and the gate, whose normalized (text, context) matches it.
2. The blind gate (``data/v2/gate``) outranks everything: gate duplicates keep the first
   copy, and an evaluation row that repeats a gate row is removed from evaluation.
3. Evaluation duplicates across files keep the highest-priority copy (the blind v2 set
   first). Rows in the ``invariance`` and ``context_flip`` suites repeat on purpose.
4. Training rows that also appear in evaluation or the gate are removed from training.
5. Training duplicates with the same labels keep the copy in the most structured folder.

Rows sharing a key but carrying different labels are never dropped: they are reported and
the script exits 1 until a decision is recorded in the ledger. Idempotent.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from dataset import V2, normalize  # noqa: E402

LEDGER = V2 / "curation.json"
LABEL_FIELDS = ("certainty", "primary", "intents")
EVAL_PRIORITY = ("heldout.jsonl", "checklist.jsonl", "v1_heldout.jsonl", "v1_checklist.jsonl")
TRAIN_PRIORITY = ("boundary", "paraphrase", "mixed", "adversarial", "conversational", "short",
                  "v1_contrast", "v1_coding_agent", "v1_browser_agent", "v1_document_creation", "v1_chat")
REPEATING_SUITES = {"invariance", "context_flip"}


def _key(row: dict) -> tuple[str, str]:
    return normalize(row.get("text", "")), normalize(row.get("context", ""))


def _labels(row: dict) -> tuple:
    return row.get("certainty"), row.get("primary"), tuple(sorted(row.get("intents") or ()))


def _ordered(paths: list[Path], priority: tuple[str, ...], name) -> list[Path]:
    rank = {value: index for index, value in enumerate(priority)}
    return sorted(paths, key=lambda path: (rank.get(name(path), len(rank)), path.as_posix()))


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip", nargs="*", default=[], help="training folders to leave unread and untouched")
    options = parser.parse_args()

    relabels = {_key(entry): entry for entry in json.loads(LEDGER.read_text(encoding="utf-8"))["relabel"]}
    changes: Counter = Counter()
    conflicts: list[str] = []

    def relabel(row: dict, where: str) -> bool:
        entry = relabels.get(_key(row))
        if entry and _labels(row) != _labels(entry):
            row.update({field: entry[field] for field in LABEL_FIELDS})
            changes[f"relabel {where}"] += 1
            return True
        return False

    gate_seen: dict[tuple[str, str], tuple[tuple, str]] = {}
    for path in sorted((V2 / "gate").glob("*.jsonl")):
        rows, kept, changed = _read(path), [], False
        for row in rows:
            changed |= relabel(row, f"gate/{path.name}")
            key = _key(row)
            if key not in gate_seen:
                gate_seen[key] = (_labels(row), path.name)
                kept.append(row)
            elif gate_seen[key][0] == _labels(row):
                changes[f"drop gate duplicate {path.name}"] += 1
                changed = True
            else:
                conflicts.append(f"gate {path.name}: {row['text'][:80]!r} {_labels(row)} vs {gate_seen[key]}")
                kept.append(row)
        if changed and not options.dry_run:
            _write(path, kept)

    eval_seen: dict[tuple[str, str], tuple[tuple, str]] = {}
    for path in _ordered(sorted((V2 / "eval").glob("*.jsonl")), EVAL_PRIORITY, lambda p: p.name):
        rows, kept, changed = _read(path), [], False
        for row in rows:
            changed |= relabel(row, f"eval/{path.name}")
            key = _key(row)
            if key in gate_seen:
                if gate_seen[key][0] == _labels(row):
                    changes[f"drop eval copy of gate {path.name}"] += 1
                    changed = True
                else:
                    conflicts.append(f"eval {path.name} vs gate: {row['text'][:80]!r} {_labels(row)} vs {gate_seen[key]}")
                    kept.append(row)
                continue
            if row.get("suite") in REPEATING_SUITES or key not in eval_seen:
                eval_seen.setdefault(key, (_labels(row), path.name))
                kept.append(row)
            elif eval_seen[key][0] == _labels(row):
                changes[f"drop eval duplicate {path.name}"] += 1
                changed = True
            else:
                conflicts.append(f"eval {path.name}: {row['text'][:80]!r} {_labels(row)} vs {eval_seen[key]}")
                kept.append(row)
        if changed and not options.dry_run:
            _write(path, kept)

    train_seen: dict[tuple[str, str], tuple[tuple, str]] = {}
    folders = [p for p in (V2 / "train").iterdir() if p.is_dir() and p.name not in options.skip]
    for folder in _ordered(folders, TRAIN_PRIORITY, lambda p: p.name):
        for path in sorted(folder.glob("*.jsonl")):
            rows, kept, changed = _read(path), [], False
            where = f"{folder.name}/{path.name}"
            for row in rows:
                changed |= relabel(row, where)
                key = _key(row)
                if key in eval_seen or key in gate_seen:
                    changes[f"drop leak {where}"] += 1
                    changed = True
                elif key not in train_seen:
                    train_seen[key] = (_labels(row), where)
                    kept.append(row)
                elif train_seen[key][0] == _labels(row):
                    changes[f"drop train duplicate {where}"] += 1
                    changed = True
                else:
                    conflicts.append(f"train {where}: {row['text'][:80]!r} {_labels(row)} vs {train_seen[key]}")
                    kept.append(row)
            if changed and not options.dry_run:
                _write(path, kept)

    for name, count in sorted(changes.items()):
        print(f"{count:4d}  {name}")
    totals = Counter(name.split(" ", 1)[0] + (" " + name.split(" ")[1] if name.startswith("drop") else "")
                     for name in changes.elements())
    print(dict(totals), "(dry run)" if options.dry_run else "", f"skipped folders: {options.skip or 'none'}")
    for conflict in conflicts:
        print("CONFLICT", conflict)
    print(f"{len(conflicts)} unresolved conflict(s)")
    return 1 if conflicts else 0


if __name__ == "__main__":
    sys.exit(main())

"""Scan memory authority paths for nondeterministic primitives.

The scanner is intentionally conservative: it reports raw wall-clock IDs,
randomness, unsorted filesystem enumeration, unordered ranking helpers, and
model calls in memory authority files. Known lifecycle/reporting exceptions are
kept in one explicit allowlist so CI catches new drift.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())

AUTHORITY_PATHS = (
    "core/persona_router.py",
    "memory/api.py",
    "memory/core.py",
    "memory/promotion.py",
    "memory/source_ledger/store.py",
    "memory/engine/validate_write.py",
    "memory/engine/reindex.py",
    "memory/engine/walker.py",
    "memory/integrity/repair.py",
    "memory/intake/packet.py",
    "memory/persona_tree.py",
    "memory/post_commit.py",
    "memory/taxonomy/compiler.py",
    "memory/taxonomy/recall.py",
    "memory/taxonomy/recall_graph.py",
    "memory/dme/adapter.py",
    "memory/recall_verifier.py",
    "memory/sense_registry.py",
)

ALLOWLIST = {
    ("memory/api.py", "datetime_now", "return datetime.now(timezone.utc).isoformat()"),
    ("memory/api.py", "raw_verdict_gate_bypass", "return read_memory(query, verdict_gate=False)"),
    (
        "memory/recall_verifier.py",
        "datetime_now",
        "created_at = datetime.now(timezone.utc).isoformat()",
    ),
    (
        "memory/engine/reindex.py",
        "unsorted_glob",
        "return [Path(p) for p in glob.glob(pattern, recursive=True)]",
    ),
}

CALL_RULES = {
    "uuid": {"uuid.uuid1", "uuid.uuid4"},
    "datetime_now": {"datetime.now"},
    "time_time": {"time.time"},
    "random": {
        "random.random",
        "random.choice",
        "random.shuffle",
        "random.randint",
        "random.randrange",
    },
    "model_call": {"get_runtime_client", "invoke_runtime_generate", "generate", "model_router"},
}

TEXT_RULES = (
    ("random", re.compile(r"\bfrom\s+random\s+import\b")),
    ("unsorted_os_walk", re.compile(r"\bos\.walk\(")),
    ("unsorted_iterdir", re.compile(r"\.iterdir\(")),
    ("unordered_values_ranking", re.compile(r"\blist\([^)]*\.values\(\)\)")),
    ("raw_verdict_gate_bypass", re.compile(r"verdict_gate\s*=\s*False")),
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    rule: str
    text: str

    def key(self) -> tuple[str, str, str]:
        return (self.path, self.rule, self.text.strip())

    def to_dict(self) -> dict[str, object]:
        return {"path": self.path, "line": self.line, "rule": self.rule, "text": self.text.strip()}


def scan(paths: Iterable[str] = AUTHORITY_PATHS) -> list[Finding]:
    findings: list[Finding] = []
    for rel in paths:
        path = ROOT / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        findings.extend(_scan_text_rules(rel, text))
        findings.extend(_scan_ast_rules(rel, text))
    return [item for item in findings if item.key() not in ALLOWLIST]


def _scan_text_rules(rel: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if ".glob(" in stripped and "sorted(" not in stripped:
            findings.append(Finding(rel, lineno, "unsorted_glob", stripped))
        for rule, pattern in TEXT_RULES:
            if pattern.search(stripped) and not _line_is_sorted_filesystem(rule, stripped):
                findings.append(Finding(rel, lineno, rule, stripped))
    return findings


def _line_is_sorted_filesystem(rule: str, line: str) -> bool:
    return rule in {"unsorted_os_walk", "unsorted_iterdir"} and "sorted(" in line


def _scan_ast_rules(rel: str, text: str) -> list[Finding]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    lines = text.splitlines()
    findings: list[Finding] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        call_name = _call_name(node.func)
        if not call_name:
            continue
        for rule, names in CALL_RULES.items():
            if call_name in names or call_name.rsplit(".", 1)[-1] in names:
                lineno = int(getattr(node, "lineno", 1) or 1)
                findings.append(Finding(rel, lineno, rule, lines[lineno - 1].strip()))
    return findings


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _call_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    args = parser.parse_args(argv)

    findings = scan()
    payload = {
        "type": "memory_nondeterminism_scan",
        "ok": not findings,
        "finding_count": len(findings),
        "findings": [item.to_dict() for item in findings],
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if findings:
            for item in findings:
                print(f"{item.path}:{item.line}: {item.rule}: {item.text}")
        else:
            print("memory nondeterminism scan passed")
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())

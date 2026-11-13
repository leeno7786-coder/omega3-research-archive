"""run_full_matrix.py — full-system test matrix (aggregate gate)

Runs every plain-script test in this directory the way the project intends:
each file is a standalone `.venv-brain` subprocess with a clean interpreter
state, so imports and process-global singletons do not leak between files.

This cannot enumerate “every possible scenario” in a mathematical sense, but it is
the widest automated sweep available: every plain-script `test_*.py` and `stress_*.py`.

Usage:
  .\\.venv-brain\\Scripts\\python.exe tests\\run_full_matrix.py
  .\\.venv-brain\\Scripts\\python.exe tests\\run_full_matrix.py --list
  .\\.venv-brain\\Scripts\\python.exe tests\\run_full_matrix.py --fail-fast
  .\\.venv-brain\\Scripts\\python.exe tests\\run_full_matrix.py --json run_matrix.json
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
import logging

log = logging.getLogger("omega.tests.run_full_matrix")


if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
TESTS_DIR = Path(__file__).resolve().parent

# Match common summary lines across the test corpus
_RE_TOTAL = re.compile(r"TOTAL:\s*PASS\s*=\s*(\d+)\s+FAIL\s*=\s*(\d+)", re.I)
_RE_PASS_FAIL_LINE = re.compile(r"\bPASS:\s*(\d+).*?\bFAIL:\s*(\d+)", re.I | re.DOTALL)
_RE_PASSED_FAILED = re.compile(r"PASSED:\s*(\d+).*?FAILED:\s*(\d+)", re.I | re.DOTALL)


def _is_pytest_style(path: Path) -> bool:
    """Match tests/conftest.py: pytest collects def test_* / class Test* only."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            return True
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            return True
    return False


def discover_scripts() -> list[Path]:
    files: list[Path] = []
    for pattern in ("test_*.py", "stress_*.py"):
        files.extend(sorted(TESTS_DIR.rglob(pattern)))
    files = [p for p in files if p.name != "run_full_matrix.py"]
    files = [p for p in files if not _is_pytest_style(p)]
    return files


def parse_pass_fail(text: str) -> tuple[int | None, int | None]:
    """Best-effort extraction of aggregate PASS/FAIL counters from script output."""
    if not text:
        return None, None
    m = _RE_TOTAL.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _RE_PASSED_FAILED.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _RE_PASS_FAIL_LINE.search(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


_SCRATCH_LOGS: Path | None = None


def _scratch_logs_dir() -> Path:
    """One scratch Guardian logs dir per matrix run: plain scripts emit telemetry and must not
    append synthetic failures to the operator's live guardian/logs."""
    global _SCRATCH_LOGS
    if _SCRATCH_LOGS is None:
        import tempfile

        _SCRATCH_LOGS = Path(tempfile.mkdtemp(prefix="omega-matrix-logs-"))
    return _SCRATCH_LOGS


def run_one(py: Path, fail_fast: bool) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["OMEGA_GUARDIAN_LOGS_DIR"] = str(_scratch_logs_dir())
    cmd = [sys.executable, str(py)]
    t0 = time.perf_counter()
    proc = subprocess.run(
        cmd,
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
    )
    elapsed = time.perf_counter() - t0
    out = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
    p_sub, f_sub = parse_pass_fail(out)
    row: dict[str, Any] = {
        "file": py.name,
        "exit_code": proc.returncode,
        "seconds": round(elapsed, 3),
        "parsed_pass": p_sub,
        "parsed_fail": f_sub,
        "ok": proc.returncode == 0,
    }
    if proc.returncode != 0:
        tail = out[-8000:] if len(out) > 8000 else out
        row["output_tail"] = tail
    return row


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Omega3.0 full plain-script test matrix.")
    ap.add_argument("--list", action="store_true", help="Print script paths and exit.")
    ap.add_argument("--fail-fast", action="store_true", help="Stop after first failure.")
    ap.add_argument("--json", metavar="PATH", help="Write structured results to JSON.")
    args = ap.parse_args(argv)

    scripts = discover_scripts()
    if args.list:
        for p in scripts:
            print(p.as_posix())
        return 0

    print("=" * 72)
    print("OMEGA 3.0 — FULL TEST MATRIX")
    print(f"  Scripts: {len(scripts)}")
    print(f"  Root:    {ROOT}")
    print("=" * 72)

    results: list[dict[str, Any]] = []
    failed = 0
    total_parse_pass = 0
    total_parse_fail = 0
    parsed_any = False

    overall_t0 = time.perf_counter()
    for py in scripts:
        label = py.name
        print(f"\n>>> RUN  {label}")
        row = run_one(py, args.fail_fast)
        results.append(row)
        status = "OK" if row["ok"] else "FAIL"
        extra = ""
        if row["parsed_pass"] is not None and row["parsed_fail"] is not None:
            extra = f"  (script claims PASS={row['parsed_pass']} FAIL={row['parsed_fail']})"
            parsed_any = True
            total_parse_pass += row["parsed_pass"]
            total_parse_fail += row["parsed_fail"]
        print(f"<<< {status}  {label}  exit={row['exit_code']}  {row['seconds']}s{extra}")
        if not row["ok"]:
            failed += 1
            print("--- output (tail) ---")
            print(row.get("output_tail", ""))
            if args.fail_fast:
                break

    wall = time.perf_counter() - overall_t0
    print("\n" + "=" * 72)
    print("MATRIX SUMMARY")
    print(f"  Files run:     {len(results)}")
    print(f"  Files failed:  {failed}")
    print(f"  Wall time:     {wall:.1f}s")
    if parsed_any:
        print(
            f"  Sum of reported in-script checks: PASS={total_parse_pass}  FAIL={total_parse_fail}"
        )
        print("  (Internal counters are informational; matrix pass/fail uses process exit codes.)")
    print("=" * 72)

    if args.json:
        payload = {
            "root": str(ROOT),
            "wall_seconds": round(wall, 3),
            "files_failed": failed,
            "scripts": results,
            "totals_parsed_pass": total_parse_pass if parsed_any else None,
            "totals_parsed_fail": total_parse_fail if parsed_any else None,
        }
        report_path = Path(args.json)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nWrote JSON report: {report_path}")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

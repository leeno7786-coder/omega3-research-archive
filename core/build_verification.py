"""Deterministic build verification — the workspace checked against the operator's request.

The main meta question of the fold-back is "compared to what the operator requested, does my
work meet the requirements to satisfy it?" (operator 2026-07-02). The MIND answers it by
reflection; THIS module is the deterministic proof behind it — a project-agnostic heuristic
battery that, when green, verifies the work satisfies the request:

  1. deliverables_exist — every touched file exists on disk, non-empty.
  2. code_sound        — Python deliverables compile (syntax gate; non-Python skipped).
  3. tests_green       — the build's own tests run and all pass (workspace pytest runner).
  4. request_coverage  — DYNAMIC checks derived from the operator's VERBATIM request text
                         (readme/docs mentioned -> a README exists; "at least N tests" -> the
                         suite ran >= N tests; cli mentioned -> an entry point exists).

The battery runs FOR the mind at every fold-back (its results ride the monitor prompt, so the
reflection is never blind), and the SAME battery is the deterministic completion gate: the mind
judging "satisfied" + all_green -> objective_complete -> operator notice. Failure-soft and pure
where possible; every check reports {name, ok, detail} so red checks TEACH the next cycle.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = ["verify_build_against_request"]


def _resolve(raw: str) -> Optional[Path]:
    """Resolve a build path. The bridge reports WORKSPACE-relative paths (e.g.
    ``csv_stats.py``) — they must resolve against the operator workspace where the build ran,
    NOT the brain's CWD (the dev store hit this exact bug: capture pointed at OMEGA_ROOT and
    every file read came back null). Reuses the dev store's resolver; falls back to as-given."""
    try:
        from memory.dev_project_store import _resolve_build_path

        return _resolve_build_path(raw)
    except Exception:
        p = str(raw or "").strip()
        return Path(p) if p else None


def _existing_paths(files: Iterable[Any]) -> tuple[list[Path], list[str]]:
    """Resolve the build's touched files -> (existing Paths, missing strings)."""
    existing: list[Path] = []
    missing: list[str] = []
    for f in files or ():
        raw = str(f or "").strip()
        if not raw:
            continue
        p = _resolve(raw)
        if p is None:
            continue
        try:
            if p.exists() and p.is_file() and p.stat().st_size > 0:
                existing.append(p)
            else:
                missing.append(raw)
        except OSError:
            missing.append(raw)
    return existing, missing


def _check_deliverables(files: Iterable[Any]) -> dict[str, Any]:
    existing, missing = _existing_paths(files)
    if not existing and not missing:
        return {
            "name": "deliverables_exist",
            "ok": False,
            "detail": "no touched files recorded — nothing to verify",
        }
    if missing:
        return {
            "name": "deliverables_exist",
            "ok": False,
            "detail": f"missing/empty on disk: {', '.join(missing[:6])}",
        }
    return {
        "name": "deliverables_exist",
        "ok": True,
        "detail": f"{len(existing)} file(s) present and non-empty",
    }


def _check_code_sound(files: Iterable[Any]) -> Optional[dict[str, Any]]:
    """Compile every Python deliverable. None (not applicable) when the build has no .py files."""
    existing, _ = _existing_paths(files)
    py = [p for p in existing if p.suffix == ".py"]
    if not py:
        return None
    bad: list[str] = []
    for p in py:
        try:
            if p.stat().st_size > 1_000_000:  # cap: never compile a pathological deliverable
                bad.append(f"{p.name}: too large to verify (>1MB)")
                continue
            compile(p.read_text(encoding="utf-8", errors="replace"), str(p), "exec")
        except SyntaxError as exc:
            bad.append(f"{p.name}: {exc.msg} (line {exc.lineno})")
        except Exception as exc:  # OSError/MemoryError/RecursionError — a RED check, not a crash
            bad.append(f"{p.name}: unverifiable ({type(exc).__name__})")
    if bad:
        return {"name": "code_sound", "ok": False, "detail": "; ".join(bad[:5])}
    return {"name": "code_sound", "ok": True, "detail": f"{len(py)} python file(s) compile"}


_TESTS_WORD_RE = re.compile(r"\btests?\b|\bpytest\b", re.IGNORECASE)


def _check_tests_green(
    report: Optional[dict],
    run_error: str = "",
    *,
    request: str = "",
    files: Iterable[Any] = (),
) -> Optional[dict[str, Any]]:
    """The build's own tests (the battery runs pytest ONCE; this reads the shared report).

    NOT APPLICABLE (None) when the build has no test files AND the request never demands tests —
    a docs/config build must not be structurally barred from green (it would grind to a silent
    stall-stop instead of completing)."""
    if run_error:
        return {"name": "tests_green", "ok": False, "detail": f"test run failed: {run_error}"}
    if not report:
        existing, _ = _existing_paths(files)
        has_test_files = any(
            p.name.startswith("test_") or p.name.endswith("_test.py") for p in existing
        )
        if not has_test_files and not _TESTS_WORD_RE.search(str(request or "")):
            return None  # nothing demands tests -> check does not apply
        return {"name": "tests_green", "ok": False, "detail": "no tests found to run"}
    ok = bool(report.get("all_passed"))
    return {
        "name": "tests_green",
        "ok": ok,
        "detail": str(report.get("summary") or ("green" if ok else "failing")),
    }


_MIN_TESTS_RE = re.compile(r"(?:at least|minimum(?: of)?|>=)\s+(\d+)\s+tests?", re.IGNORECASE)

_PROJECT_SCAN_CAP = 200  # per-dir listing cap — a workspace project dir is small; never crawl


def _project_files(existing: list[Path]) -> list[Path]:
    """Files in the project dir(s) holding the touched files. Presence checks must look at the
    PROJECT, not just this dispatch's diff: a resume/verify dispatch touches few files (live
    2026-07-02: one) while the README/tests it must prove live beside them — and tests_green
    already runs pytest across the same dir, so the two checks must see the same project."""
    seen_dirs: set[Path] = set()
    out: list[Path] = []
    for p in existing:
        d = p.parent
        if d in seen_dirs:
            continue
        seen_dirs.add(d)
        try:
            for f in sorted(d.iterdir())[:_PROJECT_SCAN_CAP]:
                if f.is_file():
                    out.append(f)
        except OSError:
            continue
    return out


def _check_request_coverage(
    request: str, files: Iterable[Any], test_report: Optional[dict]
) -> Optional[dict[str, Any]]:
    """DYNAMIC heuristics derived from the operator's verbatim request. Each recognized demand
    becomes a deterministic sub-check; None (not applicable) when the request names nothing
    recognizable. Sub-checks: readme/docs, test-file presence, 'at least N tests', cli entry.
    Presence sub-checks scan the whole project dir (see _project_files); integrity checks
    (deliverables_exist, code_sound) stay scoped to the touched files."""
    text = str(request or "").lower()
    if not text:
        return None
    existing, _ = _existing_paths(files)
    project = _project_files(existing) or existing
    names = [p.name.lower() for p in project]
    unmet: list[str] = []
    met: list[str] = []

    if "readme" in text or "documentation" in text:
        if any(n.startswith("readme") for n in names):
            met.append("readme present")
        else:
            unmet.append("request mentions a README/documentation but none exists")

    if _TESTS_WORD_RE.search(text):
        if any(
            n.startswith("test_")
            or n.endswith("_test.py")
            or "/tests/" in str(p).replace("\\", "/")
            for n, p in zip(names, project)
        ):
            met.append("test file(s) present")
        else:
            unmet.append("request mentions tests but no test file exists")
        m = _MIN_TESTS_RE.search(text)
        if m:
            wanted = int(m.group(1))
            ran = 0
            if isinstance(test_report, dict):
                ran = int(test_report.get("passed") or 0) + int(test_report.get("failed") or 0)
            if ran >= wanted:
                met.append(f"suite ran {ran} tests (>= {wanted} requested)")
            else:
                unmet.append(f"request asks for at least {wanted} tests; suite ran {ran}")

    if re.search(r"\bcli\b|command[- ]line", text):
        has_entry = any(n in ("__main__.py", "pyproject.toml", "setup.py") for n in names) or any(
            "if __name__" in _safe_read(p) for p in project if p.suffix == ".py"
        )
        if has_entry:
            met.append("cli entry point present")
        else:
            unmet.append("request mentions a CLI but no entry point found")

    if not met and not unmet:
        return None  # nothing recognizable in the request -> check not applicable
    if unmet:
        return {"name": "request_coverage", "ok": False, "detail": "; ".join(unmet[:5])}
    return {"name": "request_coverage", "ok": True, "detail": "; ".join(met[:5])}


def _safe_read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def verify_build_against_request(
    *,
    original_request: Any = "",
    files: Iterable[Any] = (),
    test_report: Optional[dict] = None,
) -> dict[str, Any]:
    """Run the deterministic battery -> {checks: [{name, ok, detail}], all_green, summary}.

    ``test_report`` lets a caller that ALREADY ran the workspace tests (packet build) inject the
    report instead of running pytest twice. Whether injected or run here, ONE report feeds BOTH
    tests_green and request_coverage (they must never disagree about what ran). all_green
    requires every APPLICABLE check ok — a check that doesn't apply (no python files /
    unrecognizable request) is omitted, never a pass.
    """
    file_list = list(files or ())
    report = test_report
    run_error = ""
    if report is None:
        try:
            from omega_loop.episode.workspace_tests import collect_test_report

            report = collect_test_report(file_list) or {}
        except Exception as exc:  # failure-soft: an un-runnable suite is a RED check, not a crash
            report, run_error = {}, type(exc).__name__
    checks: list[dict[str, Any]] = [_check_deliverables(file_list)]
    code = _check_code_sound(file_list)
    if code is not None:
        checks.append(code)
    tests = _check_tests_green(
        report,
        run_error,
        request=str(original_request or ""),
        files=file_list,
    )
    if tests is not None:
        checks.append(tests)
    coverage = _check_request_coverage(str(original_request or ""), file_list, report)
    if coverage is not None:
        checks.append(coverage)
    all_green = all(c.get("ok") for c in checks)
    red = [c["name"] for c in checks if not c.get("ok")]
    summary = (
        "all checks green — the work satisfies the request"
        if all_green
        else "red: " + ", ".join(red)
    )
    return {"checks": checks, "all_green": all_green, "summary": summary}

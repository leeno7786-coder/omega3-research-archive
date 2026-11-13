"""Run a yielded build's tests and capture the logs for the fold-back packet.

The CLI bridge runs the build's tests but only emits the tool NAME + duration — not the output.
So a yielded build's fold-back had no visible pass/fail detail, and Omega, unable to see WHAT
failed, asked the operator instead of deciding RETRY_WITH_CORRECTION. This runs the project's
tests against what is actually on disk and returns a compact report (summary + failing tests +
a tail of the log) that folds into build_in_progress.test_results, so the mind can self-correct.

Deterministic + failure-soft: no test files -> {}; a timeout / crash -> a report that says so
(still useful signal), never an exception into the cognition cascade. Scoped to the operator
workspace / repo via the same dev-edit guard the rest of the fold-back uses.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("omega.omega_loop.workspace_tests")

_DEFAULT_TIMEOUT_S = 60
_MAX_TAIL_CHARS = 1600


def _is_test_file(path: Path) -> bool:
    n = path.name
    return n.startswith("test_") and n.endswith(".py") or n.endswith("_test.py")


def _project_dir_for(files: Iterable[Any]) -> Optional[Path]:
    """The directory to run pytest in — the dir holding the build's test file(s).

    Build paths are WORKSPACE-relative (the bridge reports them that way) — they MUST resolve
    against the operator workspace, not the brain's CWD: a relative ``tests/test_x.py`` resolved
    from OMEGA_ROOT points at the BRAIN REPO's own tests/ and pytest then runs the whole Omega
    suite (live finding 2026-07-02: 'tests timed out after 60s, ran 0' — it was collecting the
    repo). Same resolver as the dev store."""
    for f in files or ():
        try:
            p = Path(str(f))
            if not p.is_absolute():
                from memory.dev_project_store import _resolve_build_path

                resolved = _resolve_build_path(str(f))
                if resolved is not None:
                    p = resolved
        except Exception:
            continue
        if _is_test_file(p):
            parent = p.parent
            if parent and parent.exists():
                return parent
    return None


# "3 failed, 4 passed in 0.09s" / "7 passed in 0.10s" / "1 error in 0.02s"
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed)")
_FAILED_LINE_RE = re.compile(r"^(?:FAILED|ERROR)\s+(.+)$", re.MULTILINE)


def parse_pytest_output(text: str) -> dict[str, Any]:
    """Pure parse of pytest output -> {passed, failed, errors, skipped, failures, summary}."""
    text = str(text or "")
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for num, kind in _COUNT_RE.findall(text):
        k = "errors" if kind in ("error", "errors") else kind
        if k in counts:
            counts[k] = counts[k] + int(num)
    failures = [m.strip() for m in _FAILED_LINE_RE.findall(text)][:20]
    total = counts["passed"] + counts["failed"] + counts["errors"]
    summary = (
        f"{counts['passed']} passed, {counts['failed']} failed, {counts['errors']} error(s)"
        if total or counts["skipped"]
        else "no test result summary found"
    )
    return {
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errors": counts["errors"],
        "skipped": counts["skipped"],
        "failures": failures,
        "summary": summary,
        "all_passed": total > 0 and counts["failed"] == 0 and counts["errors"] == 0,
    }


def collect_test_report(
    files: Iterable[Any],
    *,
    timeout_s: int = _DEFAULT_TIMEOUT_S,
    runner: Optional[Any] = None,
) -> dict[str, Any]:
    """Run the build's tests and return a compact report, or {} when there are no tests to run.

    ``runner`` is an injection seam for tests: ``runner(project_dir) -> (stdout_text, timed_out)``.
    """
    files = list(files or ())
    project_dir = _project_dir_for(files)
    if project_dir is None:
        return {}
    try:
        from core.hud_path_guard import resolve_dev_edit_path

        resolve_dev_edit_path(str(project_dir))  # only run tests inside the workspace / repo
    except Exception:
        return {}

    if runner is not None:
        try:
            out, timed_out = runner(project_dir)
        except Exception as exc:  # noqa: BLE001
            return {
                "summary": f"test run error: {type(exc).__name__}",
                "failures": [],
                "ran": False,
            }
    else:
        out, timed_out = _run_pytest(project_dir, timeout_s)

    if timed_out:
        return {
            "summary": f"tests timed out after {timeout_s}s (did not finish)",
            "failures": [],
            "ran": True,
            "timed_out": True,
        }
    if out is None:
        return {"summary": "tests could not be run", "failures": [], "ran": False}

    report = parse_pytest_output(out)
    report["ran"] = True
    report["tail"] = out.strip()[-_MAX_TAIL_CHARS:]
    return report


def _run_pytest(project_dir: Path, timeout_s: int) -> tuple[Optional[str], bool]:
    """Run pytest in ``project_dir`` with the brain interpreter; capture stdout+stderr."""
    try:
        from core.paths import portable_python_path

        proc = subprocess.run(
            [
                str(portable_python_path()),
                "-m",
                "pytest",
                "-q",
                "--no-header",
                "-p",
                "no:cacheprovider",
            ],
            cwd=str(project_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        return ((proc.stdout or "") + (proc.stderr or "")), False
    except subprocess.TimeoutExpired:
        return None, True
    except Exception as exc:  # noqa: BLE001
        log.warning("workspace test run failed: %s", exc)
        return None, False

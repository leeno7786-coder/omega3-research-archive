"""No outside source — the interpreter boundary (AGENTS.md §1, §10 #2/#3).

Operator, 2026-08-01: *"I want no outside source. If it doesn't boot inside the
venv-brain using the python embed, then it doesn't boot."*

Two doors let a machine-local interpreter reach Omega, and both are closed here:

  1. A **launcher fallback** — `if not exist "%PY%" set "PY=python"`.  A launcher
     that cannot find the venv must FAIL, never degrade to whatever `python`
     resolves to on the host.  Anything proven on a host interpreter proves
     nothing about the shipped product.
  2. **`import site` in the embed's `._pth`** — which lets
     `python-3.13-embed\\python.exe`, invoked directly, fall through to a
     machine-local user site-packages.  The embed is a base, not a runtime.

`.venv-brain\\Scripts\\python313._pth` is a SEPARATE file and keeps its own
`import site` — that is what makes the venv a venv.  Closing door 2 does not
touch it.
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from core.paths import OMEGA_ROOT, portable_python_path

# Windows launchers that start Omega. `.sh` is deliberately out of scope; the
# scanner tests below still use representative bad launcher text explicitly.
_LAUNCHER_SUFFIXES = (".bat", ".cmd", ".ps1")
_SKIP_DIRS = {
    ".venv-brain", "python-3.13-embed", "python-3.13-linux", "node_modules", ".git",
    ".worktrees", "graphify-out",
    "scratchpad",  # local probe/download payloads, never product code
}

# An assignment whose value is a BARE interpreter name — no path in front of it.
# `set "PY=%ROOT%\.venv-brain\Scripts\python.exe"` does not match; `set "PY=python"` does.
_BARE_PYTHON = re.compile(r'=\s*"?\s*(python[0-9]*(?:\.exe)?)\s*"?\s*$', re.IGNORECASE)
_COMMENT = ("REM", "::", "#")


def _files_with_suffixes(suffixes: tuple[str, ...]) -> Iterator[Path]:
    """Walk only product-owned directories; prune bundled payloads in-place."""
    for directory, dirnames, filenames in os.walk(OMEGA_ROOT):
        # Local virtualenvs (.venv-rocm and friends) hold third-party payloads, not product code.
        dirnames[:] = sorted(
            name for name in dirnames if name not in _SKIP_DIRS and not name.startswith(".venv")
        )
        base = Path(directory)
        for filename in sorted(filenames):
            path = base / filename
            if path.suffix.lower() in suffixes:
                yield path


def _launcher_scripts() -> list[Path]:
    return list(_files_with_suffixes(_LAUNCHER_SUFFIXES))


def _bare_python_fallbacks(text: str) -> list[str]:
    hits = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.upper().startswith(_COMMENT):
            continue
        if _BARE_PYTHON.search(line):
            hits.append(line)
    return hits


def test_the_guard_recognises_a_bare_python_fallback():
    """The detector itself must be able to fail — a guard that cannot catch its
    own violation is a tautology (AGENTS.md §8: RED asserts the fail-mode)."""
    assert _bare_python_fallbacks('if not exist "%PY%" set "PY=python"')
    assert _bare_python_fallbacks("  PY=python3")
    assert _bare_python_fallbacks('$Py = "python.exe"')
    # ...and must not fire on the correct form.
    assert not _bare_python_fallbacks('set "PY=%ROOT%\\.venv-brain\\Scripts\\python.exe"')
    assert not _bare_python_fallbacks('REM set "PY=python"')


def test_no_launcher_falls_back_to_outside_python():
    scripts = _launcher_scripts()
    assert scripts, "no launcher scripts found — the scan is looking in the wrong place"

    offenders = {}
    for path in scripts:
        hits = _bare_python_fallbacks(path.read_text(encoding="utf-8", errors="replace"))
        if hits:
            offenders[str(path.relative_to(OMEGA_ROOT))] = hits

    assert not offenders, (
        "launcher falls back to a machine-local interpreter; it must fail instead: "
        f"{offenders}"
    )


def test_playwright_hud_server_uses_the_bundled_venv():
    """Browser tests must exercise the same portable interpreter as Omega."""
    config = OMEGA_ROOT / "playwright.config.ts"
    text = config.read_text(encoding="utf-8")

    assert r".\\.venv-brain\\Scripts\\python.exe -m hud.standalone --no-boot" in text
    assert not re.search(
        r"command:\s*['\"]python(?:3)?(?:\.exe)?\s+-m\s+hud\.standalone",
        text,
        re.IGNORECASE,
    )


def test_python_sources_do_not_use_a_machine_local_interpreter_shebang():
    offenders = []
    for path in _files_with_suffixes((".py",)):
        relative = path.relative_to(OMEGA_ROOT)
        if _SKIP_DIRS & set(relative.parts) or "tests" in relative.parts:
            continue
        first_line = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
        if first_line and re.search(r"^#!.*\bpython(?:3)?(?:\.exe)?\b", first_line[0], re.IGNORECASE):
            offenders.append(str(relative))

    assert not offenders, "source files must be launched through the bundled venv: " + ", ".join(offenders)


def test_embed_pth_does_not_import_site():
    """Door 2. Without `import site` the embed cannot reach a host user
    site-packages, which is exactly what makes it a base and not a runtime."""
    pth = OMEGA_ROOT / "python-3.13-embed" / "python313._pth"
    if not pth.exists():
        pytest.skip("embed not present in this checkout")

    lines = [ln.strip() for ln in pth.read_text(encoding="utf-8").splitlines()]
    assert "import site" not in lines, (
        f"{pth.name} enables site processing — the embed can reach machine-local "
        "user site-packages when invoked directly (AGENTS.md §1, §10 #3)"
    )


def test_venv_pth_keeps_import_site():
    """The counterpart: the venv MUST keep `import site` — that is what processes
    pyvenv.cfg and puts .venv-brain\\Lib\\site-packages on the path.  This test
    exists so a future sweep of door 2 cannot take the venv down with it."""
    pth = OMEGA_ROOT / ".venv-brain" / "Scripts" / "python313._pth"
    if not pth.exists():
        pytest.skip("venv not present in this checkout")

    lines = [ln.strip() for ln in pth.read_text(encoding="utf-8").splitlines()]
    assert "import site" in lines


def test_this_pytest_process_is_the_portable_venv() -> None:
    """Anything proven on a host interpreter proves nothing about the product."""
    expected = portable_python_path().resolve()
    running = Path(sys.executable).resolve()
    assert running == expected, (
        f"pytest is running {running}; Omega tests must use {expected}"
    )


_HOST_CI_PYTHON = re.compile(
    r"(?m)^\s*(?:-\s*)?(?:"
    r"python(?:3)?(?:\.exe)?\s+-m\s+(?:hud|boot|venv)"
    r"|uses:\s*actions/setup-python"
    r"|Start-Process(?:\s+-\S+)*\s+python(?:\.exe)?\b"
    r")",
    re.IGNORECASE,
)
_PORTABLE_CI_PYTHON = re.compile(
    r"\.venv-brain(?:/bin/python|\\Scripts\\python\.exe)",
    re.IGNORECASE,
)


def _host_ci_python_hits(text: str) -> list[str]:
    return [match.group(0).strip() for match in _HOST_CI_PYTHON.finditer(text)]


def test_the_guard_recognises_a_host_python_ci_step() -> None:
    assert _host_ci_python_hits("          python -m hud.standalone --no-boot --port 13310 &")
    assert _host_ci_python_hits("        uses: actions/setup-python@v5")
    assert _host_ci_python_hits("          python -m venv .venv-brain")
    assert _host_ci_python_hits('          Start-Process -NoNewWindow python "-m hud.standalone"')
    assert not _host_ci_python_hits(
        "          .venv-brain/bin/python -m hud.standalone --no-boot --port 13310 &"
    )
    assert not _host_ci_python_hits(
        r'          Start-Process -NoNewWindow $env:OMEGA_PYTHON "-m hud.standalone --no-boot"'
    )


def test_github_workflows_do_not_invoke_host_python() -> None:
    workflows = OMEGA_ROOT / ".github" / "workflows"
    if not workflows.is_dir():
        pytest.skip("no GitHub workflows in this checkout")

    offenders: dict[str, list[str]] = {}
    missing_portable: list[str] = []
    for path in sorted(workflows.glob("*.yml")) + sorted(workflows.glob("*.yaml")):
        text = path.read_text(encoding="utf-8", errors="replace")
        hits = _host_ci_python_hits(text)
        relative = str(path.relative_to(OMEGA_ROOT))
        if hits:
            offenders[relative] = hits
        if "python" in text.lower() and not _PORTABLE_CI_PYTHON.search(text):
            missing_portable.append(relative)

    assert not offenders, (
        "workflow invokes a host interpreter; it must use .venv-brain or fail: "
        f"{offenders}"
    )
    assert not missing_portable, (
        "workflow mentions Python but never names the portable interpreter: "
        f"{missing_portable}"
    )


def test_aider_workflow_honestly_reports_a_missing_bundled_portable_interpreter() -> None:
    workflow = OMEGA_ROOT / ".github" / "workflows" / "aider-bot.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "ALERT_BLOCK=SKIPPED" in text
    assert "hosted checkout" in text.casefold()
    assert "if: env.ALERT_BLOCK != 'SKIPPED'" in text


def test_tests_do_not_fall_back_to_a_host_interpreter() -> None:
    """A missing venv must fail the test, never `else sys.executable`."""
    pattern = re.compile(
        r"if .+\.is_file\(\)\s+else\s+(?:Path\()?sys\.executable",
        re.IGNORECASE,
    )
    offenders: dict[str, list[str]] = {}
    tests_root = OMEGA_ROOT / "tests"
    for path in tests_root.rglob("*.py"):
        hits = [
            line.strip()
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if pattern.search(line)
        ]
        if hits:
            offenders[str(path.relative_to(OMEGA_ROOT))] = hits
    assert not offenders, (
        "test helper falls back to a machine-local interpreter: "
        f"{offenders}"
    )

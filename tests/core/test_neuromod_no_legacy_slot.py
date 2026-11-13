"""N6 — no-resurrection guards for retired neuromod SS slot + bolt-on symbols.

RED before WO-5 deletions; GREEN after §4 symbols are removed.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())

_DELETED_SYMBOLS = (
    "persist_neuromod_spike_projection",
    "normalize_episode_auto_neuromod",
    "_build_spike_record",
    "extract_neuromod_from_auto_result",
    "compute_neuromod_spike_scalar",
)


@pytest.mark.parametrize("symbol", _DELETED_SYMBOLS)
def test_deleted_symphony_neuromod_symbols_not_importable(symbol: str):
    import autonomic.symphony_neuromod as sn

    assert not hasattr(sn, symbol), f"{symbol} must be deleted from autonomic.symphony_neuromod"


def test_last_neuromod_spike_not_in_shared_state_keys():
    import core.shared_state_keys as ssk

    assert not hasattr(ssk, "LAST_NEUROMOD_SPIKE")


def test_read_neuromod_spike_source_has_no_legacy_strings():
    from pressure_loop import pressure_field as pf

    source = inspect.getsource(pf.read_neuromod_spike)
    for forbidden in (
        "LAST_NEUROMOD_SPIKE",
        "LAST_AUTO_CYCLE",
        "extract_neuromod_from_auto_result",
    ):
        assert forbidden not in source, f"read_neuromod_spike must not reference {forbidden}"


def test_neuromod_from_last_body_source_has_no_legacy_slot():
    from pressure_loop import pressure as pl

    source = inspect.getsource(pl._neuromod_from_last_body)
    assert "last_neuromod_spike" not in source


def test_no_live_module_imports_extract_neuromod_from_auto_result():
    skip_dirs = {
        "tests",
        ".worktrees",
        ".claude",
        ".venv-brain",
        "node_modules",
        "__pycache__",
    }
    offenders: list[str] = []
    for path in ROOT.rglob("*.py"):
        parts = set(path.parts)
        if parts & skip_dirs:
            continue
        rel = path.relative_to(ROOT)
        if str(rel).startswith("tests" + "\\") or str(rel).startswith("tests/"):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "extract_neuromod_from_auto_result" not in text:
            continue
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module == "autonomic.symphony_neuromod":
                    for alias in node.names:
                        if alias.name == "extract_neuromod_from_auto_result":
                            offenders.append(str(rel))
            elif isinstance(node, ast.Import):
                pass
            elif isinstance(node, ast.Attribute):
                pass
        if (
            "from autonomic.symphony_neuromod import" in text
            and "extract_neuromod_from_auto_result" in text
        ):
            if (
                str(rel) == "core" + "\\symphony_neuromod.py"
                or str(rel) == "core/symphony_neuromod.py"
            ):
                continue
            if str(rel) not in offenders:
                for line in text.splitlines():
                    if "extract_neuromod_from_auto_result" in line and line.strip().startswith(
                        ("import ", "from ")
                    ):
                        offenders.append(str(rel))
                        break
    assert not offenders, (
        f"live modules still reference extract_neuromod_from_auto_result: {offenders}"
    )

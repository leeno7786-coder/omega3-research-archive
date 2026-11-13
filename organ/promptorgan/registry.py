"""Deterministic discovery of installed PromptOrgan packs."""

from __future__ import annotations

import importlib
from pathlib import Path

from .contract import PromptPack


def discover(
    packs_dir: Path | str | None = None,
    *,
    package: str = "organ.promptorgan.packs",
) -> tuple[PromptPack, ...]:
    root = Path(packs_dir) if packs_dir else Path(__file__).parent / "packs"
    packs: list[PromptPack] = []
    if not root.is_dir():
        raise FileNotFoundError(f"PromptOrgan pack directory is missing: {root}")
    for name in sorted(
        path.name
        for path in root.iterdir()
        if path.is_dir() and not path.name.startswith(("_", "."))
    ):
        try:
            module = importlib.import_module(f"{package}.{name}.pack")
        except ModuleNotFoundError as exc:
            if exc.name != f"{package}.{name}.pack":
                raise
            continue
        pack = getattr(module, "PACK", None)
        if pack is not None:
            packs.append(pack)
    packs.sort(key=lambda pack: (pack.priority, pack.name))
    return tuple(packs)

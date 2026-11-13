"""Walker COORD_INDEX iteration must survive concurrent reload."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from memory.engine import walker


def _write_index(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


@pytest.fixture
def coord_index_env(tmp_path):
    idx_path = tmp_path / "coord_index.json"
    seed = {
        f"2.{y:02d}.0001": {"path": f"f{y}.json", "coord": [2, y, 1], "domain": "python"}
        for y in range(20)
    }
    _write_index(idx_path, seed)
    with patch.object(walker, "INDEX_PATH", idx_path):
        walker._coord_index_mtime = 0.0
        walker._coord_index_generation = 0
        walker.COORD_INDEX.clear()
        walker.load_coord_index()
        yield idx_path


def test_walker_concurrent_reload_no_crash(coord_index_env):
    """list_ids_* must not raise RuntimeError while load_coord_index hammers."""
    idx_path = coord_index_env
    stop = threading.Event()
    errors: list[str] = []

    def reloader():
        i = 0
        while not stop.is_set():
            i += 1
            data = {
                f"2.{y:02d}.0001": {
                    "path": f"f{y}_{i}.json",
                    "coord": [2, y, 1],
                    "domain": "python",
                }
                for y in range(20)
            }
            try:
                _write_index(idx_path, data)
                walker.load_coord_index()
            except Exception as exc:
                errors.append(f"reloader: {type(exc).__name__}: {exc}")
            time.sleep(0.002)

    def reader():
        while not stop.is_set():
            try:
                walker.list_ids_in_subdomain(2, 0)
                walker.list_ids_in_neighborhood(2, 5, radius=3)
                walker.list_ids_in_lobe(2)
                walker.list_ids_by_domain("python")
            except RuntimeError as exc:
                errors.append(f"reader: RuntimeError: {exc}")
            except Exception as exc:
                errors.append(f"reader: {type(exc).__name__}: {exc}")
            time.sleep(0.001)

    threads = [
        threading.Thread(target=reloader, daemon=True),
        threading.Thread(target=reader, daemon=True),
        threading.Thread(target=reader, daemon=True),
    ]
    for t in threads:
        t.start()
    time.sleep(1.0)
    stop.set()
    for t in threads:
        t.join(timeout=2.0)

    assert not errors, f"concurrent reload race errors: {errors[:5]}"

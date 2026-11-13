import threading
import time
from pathlib import Path

from memory.taxonomy.compile_lock import compile_lock
from memory.taxonomy import paths as P


def test_compile_lock_is_mutually_exclusive(tmp_path):
    """Two threads cannot hold the compile lock at the same time."""
    order: list[str] = []

    def worker(name: str, hold: float):
        with compile_lock(root=tmp_path):
            order.append(f"{name}:enter")
            time.sleep(hold)
            order.append(f"{name}:exit")

    t1 = threading.Thread(target=worker, args=("a", 0.20))
    t2 = threading.Thread(target=worker, args=("b", 0.0))
    t1.start()
    time.sleep(0.02)  # ensure t1 acquires first
    t2.start()
    t1.join()
    t2.join()

    # b must not enter between a:enter and a:exit (no interleave).
    assert order == ["a:enter", "a:exit", "b:enter", "b:exit"]


def test_compile_lock_creates_lockfile_under_compiled_root(tmp_path):
    with compile_lock(root=tmp_path):
        assert (P.compiled_root(tmp_path) / ".compile.lock").exists()

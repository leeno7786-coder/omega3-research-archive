"""Print Phase 7 routing harness metrics (CI / operator diagnostic)."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from memory.routing.harness import run_harness


def main() -> int:
    metrics = run_harness(collect_failures=False)
    print(f"total={metrics.total}")
    print(f"decidability={metrics.decidability_rate:.1%}")
    print(f"subdomain_accuracy={metrics.subdomain_accuracy:.1%}")
    print(f"symmetry={metrics.symmetry_rate:.1%}")
    print(f"recall_lobe_accuracy={metrics.recall_lobe_accuracy:.1%}")
    print(f"unsorted_rate={metrics.unsorted_rate:.1%}")
    print(f"edge_cases={len(metrics.edge_cases)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

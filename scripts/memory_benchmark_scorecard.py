"""Run Omega Memory deterministic benchmark scorecard."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.benchmark_scorecard import (  # noqa: E402
    DEFAULT_RESPONSE_MODEL,
    DEFAULT_RESPONSE_MODEL_CLASS,
    build_scorecard,
)
from memory.determinism_replay import run_determinism_replay  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="Optional output directory for replay roots. Existing content is replaced.",
    )
    parser.add_argument(
        "--keep-roots",
        action="store_true",
        help="Keep temporary replay roots when --base-dir is omitted.",
    )
    parser.add_argument(
        "--include-report",
        action="store_true",
        help="Include the full replay report under replay_report.",
    )
    # Defaults come from the scorecard module, not a second copy here: these
    # were hardcoded to gemma_e4b and stayed there when the seat changed, so the
    # CLI kept disclosing a retired model while the library reported the live
    # one (2026-08-13).
    parser.add_argument(
        "--response-model",
        default=DEFAULT_RESPONSE_MODEL,
        help="Answer voice model disclosed in the scorecard.",
    )
    parser.add_argument(
        "--response-model-class",
        default=DEFAULT_RESPONSE_MODEL_CLASS,
        help="Human-readable response model class disclosed in the scorecard.",
    )
    args = parser.parse_args(argv)

    report = run_determinism_replay(base_dir=args.base_dir, keep_roots=args.keep_roots)
    scorecard = build_scorecard(
        report,
        response_model=args.response_model,
        response_model_class=args.response_model_class,
    )
    if args.include_report:
        scorecard["replay_report"] = report
    print(json.dumps(scorecard, indent=2, sort_keys=True))
    return 0 if scorecard.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

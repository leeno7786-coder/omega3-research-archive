"""Invalidate legacy content-hash timestamps and rebuild temporal projections.

Dry-run is the default.  Pass ``--apply`` while the HUD is stopped to mark
exactly recognized legacy timestamps non-authoritative, then republish the
coordinate index, graph, and DME projection.  The original timestamp is kept
for audit; this migration never invents a replacement event time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.symphony_time import now_iso
from memory.dewey_map import LOBE_MAP
from memory.temporal_authority import invalidate_legacy_content_hash_timestamp

HUD_PROBE = "http://127.0.0.1:13310/healthz"
LEDGER_SCHEMA_VERSION = "omega.temporal_authority_migration.v1"
LEDGER_NAME = "temporal_authority_migration.json"


def migrate_invented_timestamps(
    *,
    root: Path | str,
    apply: bool = False,
    rebuild: Callable[[Path], Any] | None = None,
    completed_at: str | None = None,
) -> dict[str, Any]:
    """Plan or apply the exact-signature timestamp migration.

    Canonical files are restored byte-for-byte if projection publication
    fails.  A successful ledger intentionally contains identifiers and
    digests, never record text or timestamp values.
    """
    profile_root = Path(root)
    memory_root = profile_root / "memory"
    plan: list[tuple[Path, bytes, dict[str, Any], dict[str, Any]]] = []
    for path, original, payload in _iter_canonical_records(memory_root):
        migrated, changed = invalidate_legacy_content_hash_timestamp(payload)
        if changed:
            plan.append((path, original, payload, migrated))

    report = {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "status": "dry_run" if not apply else "complete",
        "matched_records": len(plan),
        "migrated_records": 0,
    }
    if not apply or not plan:
        return report

    _validate_plan(plan)
    written: list[tuple[Path, bytes]] = []
    try:
        for path, original, _payload, migrated in plan:
            _atomic_write_json(path, migrated)
            written.append((path, original))

        rebuild_fn = rebuild or _rebuild_projections
        rebuild_fn(profile_root)

        ledger = _migration_ledger(
            plan,
            memory_root=memory_root,
            completed_at=str(completed_at or now_iso()),
        )
        _atomic_write_json(memory_root / "indexes" / LEDGER_NAME, ledger)
    except Exception:
        for path, original in reversed(written):
            _atomic_write_bytes(path, original)
        raise

    report["migrated_records"] = len(plan)
    return report


def _iter_canonical_records(
    memory_root: Path,
) -> Iterator[tuple[Path, bytes, dict[str, Any]]]:
    for dirname in sorted(set(LOBE_MAP.values())):
        lobe_root = memory_root / dirname
        if not lobe_root.is_dir():
            continue
        for path in sorted(lobe_root.rglob("*.json")):
            try:
                original = path.read_bytes()
                payload = json.loads(original.decode("utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                yield path, original, payload


def _validate_plan(
    plan: list[tuple[Path, bytes, dict[str, Any], dict[str, Any]]],
) -> None:
    from memory.engine.reindex import load_schema

    validator = load_schema()
    failures: list[str] = []
    for path, _original, _payload, migrated in plan:
        errors = list(validator.iter_errors(migrated))
        if errors:
            failures.append(f"{path}: {errors[0].message}")
    if failures:
        raise ValueError("migration would violate memory schema: " + "; ".join(failures))


def _migration_ledger(
    plan: list[tuple[Path, bytes, dict[str, Any], dict[str, Any]]],
    *,
    memory_root: Path,
    completed_at: str,
) -> dict[str, Any]:
    records = []
    for path, original, payload, migrated in plan:
        relative_path = path.relative_to(memory_root).as_posix()
        records.append(
            {
                "memory_id": str(payload.get("id") or ""),
                "path_digest": _digest(relative_path.encode("utf-8")),
                "before_digest": _digest(original),
                "after_digest": _digest(_json_bytes(migrated)),
                "invalidated_fields": [
                    "provenance.ingested_at",
                    "audit[].when",
                ],
            }
        )
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "status": "complete",
        "completed_at": completed_at,
        "record_count": len(records),
        "records": records,
    }


def _rebuild_projections(root: Path) -> None:
    from memory.engine.reindex import reindex_and_compile

    reindex_and_compile(root=root)


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    _atomic_write_bytes(path, _json_bytes(payload))


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _brain_is_up() -> bool:
    try:
        with urllib.request.urlopen(HUD_PROBE, timeout=2):
            return True
    except Exception:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="Omega profile root containing memory/ (default: repository root)",
    )
    parser.add_argument("--apply", action="store_true", help="write and rebuild")
    args = parser.parse_args()

    if args.apply and _brain_is_up():
        print("REFUSED: the HUD is serving; stop Omega before applying this migration.")
        return 2
    report = migrate_invented_timestamps(root=args.root, apply=args.apply)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

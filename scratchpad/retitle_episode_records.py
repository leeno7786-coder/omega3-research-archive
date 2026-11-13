"""One-off migration (2026-09-06): give lobe-9 episode records written before the
readable-gist fix (commit b0dc32ae) a title and summary a recall hit can be READ
from. Those records are titled with the raw episode id and summarised as
"episode <id> (completed)", so every recalled episode looks the same to the
answer model. The stored episode JSON (the record ``text``) carries the operator's
words and the reply, so the gist is recomputed from it exactly as a fresh write
would; nothing else in the record changes.

Run with the brain STOPPED, from the repo root, in the brain venv:

    .venv-brain\\Scripts\\python.exe scratchpad\\retitle_episode_records.py

Pass ``--dry-run`` to only list what would change. Afterwards the coord index
is rebuilt and the projection is marked rebuild-required so the next DeepSleep
re-publishes titles and retina codes.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEMPORAL = ROOT / "memory" / "900_temporal"
RAW_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def main(argv: list[str]) -> int:
    dry_run = "--dry-run" in argv
    from memory.atomic_replace import atomic_replace_json
    from pressure_loop.temporal_store import _episode_gist

    changed = 0
    for path in sorted(TEMPORAL.rglob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if record.get("type") != "episode" or not RAW_ID.fullmatch(str(record.get("title") or "")):
            continue
        try:
            body = json.loads(record.get("text") or "{}")
        except Exception:
            continue
        episode_id = str(body.get("episode_id") or record.get("title") or "")
        cascade_status = str(body.get("cascade_status") or "completed")
        title, summary = _episode_gist(body, body, episode_id, cascade_status)
        if title == episode_id:
            continue  # nothing readable in the stored episode either
        print(f"{record.get('id')}: {record.get('title')} -> {title!r}")
        if dry_run:
            continue
        record["title"] = title
        record["summary"] = summary
        atomic_replace_json(path, record)
        changed += 1
    print(f"{changed} record(s) retitled" if not dry_run else "dry run")
    if changed:
        from memory.engine.reindex import build_index
        from memory.projection_delta import mark_projection_dirty

        build_index()
        mark_projection_dirty(reason=f"retitled {changed} lobe-9 episode records (readable gist migration)", memory_id="")
        print("coord index rebuilt; projection marked rebuild-required")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

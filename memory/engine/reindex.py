"""
reindex.py
Scan /memory/ for valid memory JSON files, validate against schema,
and build memory/indexes/coord_index.json
"""

import json
import logging
from jsonschema import Draft7Validator, RefResolver
from pathlib import Path

from core.paths import MEMORY_ROOT, MEMORY_SCHEMA_PATH
from memory.dewey_map import LOBE_MAP
from memory.engine.coord_index_lock import COORD_INDEX_IO_LOCK

log = logging.getLogger("omega.memory.engine.reindex")

INDEX_PATH = MEMORY_ROOT / "indexes" / "coord_index.json"
SCHEMA_PATH = MEMORY_SCHEMA_PATH


class CoordIndexBuildError(RuntimeError):
    """A complete, schema-validated coordinate index could not be built."""


def _memory_root(root: Path | str | None = None) -> Path:
    return Path(root) / "memory" if root is not None else MEMORY_ROOT


def _index_path(root: Path | str | None = None) -> Path:
    return _memory_root(root) / "indexes" / "coord_index.json"


def load_schema():
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = json.load(f)
    try:
        # Try modern jsonschema API (v4.0+)
        return Draft7Validator(schema)
    except Exception:
        # Fallback for older jsonschema versions with RefResolver
        try:
            resolver = RefResolver(base_uri=f"file://{SCHEMA_PATH}", referrer=schema)
            return Draft7Validator(schema, resolver=resolver)
        except Exception:
            # Last resort: validator without resolver
            return Draft7Validator(schema)


# Archives and writer-owned metadata that live INSIDE a lobe directory but are
# not memory records. The
# reflection-window archive (pressure_loop/reflection_window.py) preserves whole
# episode bodies under 600_journal/reflection_windows/; walking it as records
# made the strict "refusing partial coordinate index" rule fail every boot after
# the first archived window (caught live 2026-09-05). Named here, never inferred
# from content: a record that fails the schema is still a rejected record.
NON_RECORD_DIRS: frozenset[str] = frozenset({"reflection_windows"})
NON_RECORD_FILES: frozenset[str] = frozenset({"_omega_project.json"})


def _is_record_path(path: Path, lobe_dir: Path) -> bool:
    """Return whether ``path`` is a coordinate-addressed memory record.

    False is returned only for writer-owned metadata: the exact
    ``600_journal/reflection_windows/...`` archive subtree and project anchors
    below ``1100_dev/projects/...``. A same-named directory or file anywhere
    else is still a record location, so an invalid file there still refuses the
    index.
    """
    try:
        parents = path.relative_to(lobe_dir).parts[:-1]
    except ValueError:
        return True
    if lobe_dir.name == "600_journal" and bool(parents) and parents[0] in NON_RECORD_DIRS:
        return False
    return not (
        lobe_dir.name == "1100_dev"
        and path.name in NON_RECORD_FILES
        and bool(parents)
        and parents[0] == "projects"
    )


def is_reflection_window_archive(path: Path, *, memory_root: Path) -> bool:
    """Window archive envelopes are not coordinate-addressed memory records.

    Exempt only the writer's exact subtree (``600_journal/reflection_windows``),
    never arbitrary records without ids or similarly named directories elsewhere
    in canonical memory. ``memory/projection/build.py`` scans membership with it.
    """
    try:
        relative = path.relative_to(memory_root)
    except ValueError:
        return False
    return relative.parts[:2] == ("600_journal", "reflection_windows")


def find_json_files(root: Path | str | None = None):
    memory_root = _memory_root(root)
    return sorted(
        path
        for dirname in sorted(set(LOBE_MAP.values()))
        for path in (memory_root / dirname).rglob("*.json")
        if path.is_file() and _is_record_path(path, memory_root / dirname)
    )


def extract_minimal(meta):
    """Build the canonical coord_index entry shape.

    Includes a recall projection (summary/tags/status/flags/text_prefix) so
    taxonomy recall can filter and score without O(N) canonical JSON opens.
    Incremental ``patch_index`` and full ``build_index`` share this helper.
    """
    flags = meta.get("flags") if isinstance(meta.get("flags"), dict) else {}
    enrichment = meta.get("enrichment") if isinstance(meta.get("enrichment"), dict) else {}
    status = str(meta.get("status") or enrichment.get("status") or "active").strip().lower()
    text = meta.get("text")
    text_prefix = text[:600] if isinstance(text, str) else ""
    tags = meta.get("tags") if isinstance(meta.get("tags"), list) else []
    return {
        "id": meta.get("id"),
        "coord": meta.get("coord"),
        "path": None,
        "title": meta.get("title"),
        "type": meta.get("type"),
        "domain": meta.get("domain", ""),
        "subtopic": meta.get("subtopic", meta.get("topic", "")),
        "canonical": bool(flags.get("canonical", False)),
        # recall projection — presence of ``summary`` marks a rich entry
        "summary": meta.get("summary") or "",
        "tags": [str(t) for t in tags if isinstance(t, (str, int, float))],
        "status": status or "active",
        "superseded_by": meta.get("superseded_by") or None,
        "private": bool(flags.get("private")),
        "quarantined": bool(flags.get("quarantined")),
        "text_prefix": text_prefix,
        "recall_proj": 1,
    }


def profile_relative_path(path: Path, *, memory_root: Path) -> str:
    """Store coord_index paths as profile-relative POSIX strings.

    Portable across machines/profiles; readers resolve against the active
    profile root (``OMEGA_ROOT``). A path outside that profile is rejected so
    the index can never serialize a host-specific absolute path.
    """
    abs_path = path.resolve(strict=False)
    mem = memory_root.resolve(strict=False)
    profile_root = mem.parent if mem.name == "memory" else mem
    try:
        return abs_path.relative_to(profile_root).as_posix()
    except ValueError as exc:
        raise ValueError(
            f"memory record path is outside the active profile: {abs_path}"
        ) from exc


def _build_candidate_index(root: Path | str) -> None:
    """Write the index only into Program 0's captured candidate input profile."""
    profile_root = _memory_root(root).parent.resolve(strict=False)
    work_dir = profile_root.parent
    if (
        profile_root.name != "input_profile"
        or not work_dir.name.startswith("work-")
        or work_dir.parent.name != "projection"
    ):
        raise RuntimeError(
            "coordinate index builds are restricted to Program 0's captured input profile"
        )

    index_path = _index_path(root)
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index = {}

    if not SCHEMA_PATH.exists():
        raise CoordIndexBuildError(f"memory schema unavailable at {SCHEMA_PATH}")
    try:
        validator = load_schema()
    except Exception as exc:
        raise CoordIndexBuildError(f"memory schema unavailable: {exc}") from exc

    rejected: list[str] = []

    files = find_json_files(root=root)
    for p in files:
        # Skip index files themselves and schema files
        if p.parent.name in ("indexes", "schema"):
            continue
        try:
            with open(p, "r", encoding="utf-8") as fh:
                meta = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            rejected.append(f"{p}: unreadable ({exc})")
            continue

        try:
            errors = list(validator.iter_errors(meta))
        except Exception as exc:
            raise CoordIndexBuildError(f"schema validation failed for {p}: {exc}") from exc
        if errors:
            rejected.append(f"{p}: invalid ({errors[0].message})")
            continue
        from memory.workspace_retention import workspace_source_is_current

        if not workspace_source_is_current(meta):
            continue

        entry = extract_minimal(meta)
        entry["path"] = profile_relative_path(p, memory_root=_memory_root(root))
        if not entry["id"] or not entry["coord"]:
            rejected.append(f"{p}: invalid (id and coord are required)")
            continue
        index[entry["id"]] = entry

    if rejected:
        detail = "; ".join(rejected[:10])
        if len(rejected) > 10:
            detail = f"{detail}; and {len(rejected) - 10} more"
        raise CoordIndexBuildError(
            f"refusing partial coordinate index; {len(rejected)} record(s) rejected: {detail}"
        )

    # No embedding-geometry guard: the organ stores no dense vectors. Semantic
    # perception is rebuilt as packed Ettin retina codes beside the DME planes.

    # Atomic write: temp file -> fsync -> rename
    import os

    tmp_path = index_path.with_suffix(".tmp")
    with COORD_INDEX_IO_LOCK:
        with open(tmp_path, "w", encoding="utf-8") as out:
            json.dump(index, out, indent=2, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        tmp_path.replace(index_path)
    log.info("Reindex complete: %d entries written to %s", len(index), index_path)


def build_index(root: Path | str | None = None):
    """Refresh the coordinate projection through Program 0, never in place."""
    return reindex_and_compile(root=root)


def reindex_and_compile(root: Path | str | None = None):
    """Run or replay the one published generation owner, then return its snapshot."""
    from core.required_substrate import RequiredSubstrateError
    from memory.projection.dispatch import projection_dispatch

    dispatch = projection_dispatch(root, reason="reindex")
    if dispatch.action == "failed":
        raise RequiredSubstrateError(
            "memory_projection",
            "dispatch_execution",
            reason=dispatch.drain_status or "failed",
            detail=str(dispatch.detail or ""),
        )

    from memory.projection.generation import open_generation_view, resolve_memory_root
    from memory.taxonomy.schemas import MemoryGraphSnapshot

    view = open_generation_view(resolve_memory_root(root))
    try:
        snapshot_data = json.loads(view.read_bytes("graph_snapshot.json").decode("utf-8"))
        snapshot = MemoryGraphSnapshot(**snapshot_data)
        errors = snapshot.validate()
        if errors:
            raise CoordIndexBuildError(
                "published graph snapshot is invalid: " + "; ".join(errors)
            )
        if snapshot.snapshot_hash != view.manifest.content.graph_snapshot_hash:
            raise CoordIndexBuildError(
                "published graph snapshot hash disagrees with its generation manifest"
            )
        return snapshot
    finally:
        view.close()


if __name__ == "__main__":
    reindex_and_compile()

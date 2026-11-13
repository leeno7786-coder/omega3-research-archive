"""
Direct script test for coding-lobe project scan/snapshot helpers.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, List, Tuple

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.coding_project import (  # noqa: E402
    ProjectSnapshotError,
    build_project_manifest_envelope,
    build_project_snapshot_envelope,
    scan_project_tree,
    snapshot_project_tree,
    verify_project_snapshot,
    write_project_snapshot_to_memory,
)
from deepsleep_loop.mastery_store import normalize_domain_id  # noqa: E402
import logging

log = logging.getLogger("omega.tests.core.test_coding_project_helpers")


PASS = 0
FAIL = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    sigil = "OK  " if ok else "FAIL"
    print(f"  [{sigil}] {label}" + (f"  ({detail})" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="omega-coding-project-test-"))
    # final_path_for_meta below creates the record's folder; resolve it under the scratch
    # dir, not the live memory tree (found 2026-09-15).
    from memory.engine import validate_write as _validate_write

    _validate_write.MEMORY_ROOT = tmp / "memory"
    # The dynamic lobe-8 envelope below allocates a "python" subdomain: the allocator writes
    # <OMEGA_ROOT>/guardian/state/dynamic_y_index.json and registers a mastery domain in
    # deepsleep_loop/state/mastery.json. The plain-script matrix saw both written live.
    import core.paths as _core_paths
    import pressure_loop.policy as _policy

    _core_paths.OMEGA_ROOT = tmp
    _core_paths.GUARDIAN_STATE_ROOT = tmp / "guardian" / "state"
    _policy.deepsleep_mastery_path = lambda policy=None: tmp / "deepsleep_state" / "mastery.json"
    import memory.subdomain_flag as subdomain_flag

    original_flag_cache = subdomain_flag._FLAG_CACHE
    original_raw_cache = subdomain_flag._RAW_CACHE
    try:
        # Keep the coord assertions deterministic even if the live runtime override
        # has dynamic lobe-8 storage enabled on this machine.
        subdomain_flag._FLAG_CACHE = frozenset()
        subdomain_flag._RAW_CACHE = []

        project = tmp / "Source Project!"
        dest = tmp / "snapshots"
        write(project / "src" / "main.py", "print('hello')\n")
        write(project / "README.md", "# Demo\n")
        write(project / "nested" / "tool.ts", "export const x = 1;\n")

        manifest = scan_project_tree(
            project,
            project_name="Source Project!",
            generated_at="2025-01-01T00:00:00+00:00",
        )
        check("scan sanitizes project name", manifest.project_name == "source-project")
        check("scan counts files", manifest.file_count == 3)
        check("scan hashes files", all(f.sha256 for f in manifest.files))
        check("scan language counts include python", manifest.language_counts.get("python") == 1)

        snapshot = snapshot_project_tree(
            project,
            dest_root=dest,
            project_name="Source Project!",
            snapshot_id="snap-001",
        )
        check("snapshot writes manifest", Path(snapshot.manifest_path).exists())
        check(
            "snapshot keeps project as top-level tree",
            Path(snapshot.tree_path).name == "source-project",
        )
        check(
            "snapshot copies source file", (Path(snapshot.tree_path) / "src" / "main.py").exists()
        )
        check("snapshot verifies", verify_project_snapshot(snapshot.snapshot_root))

        manifest_env = build_project_manifest_envelope(manifest, "cycle:test")
        snapshot_env = build_project_snapshot_envelope(snapshot, "cycle:test")
        check("manifest envelope targets coding lobe", manifest_env.coord[0] == 8)
        check("snapshot envelope targets coding lobe", snapshot_env.coord[0] == 8)
        check(
            "snapshot envelope links artifact manifest",
            snapshot_env.payload["links"]["manifest_path"] == snapshot.manifest_path,
        )
        from memory.engine.validate_write import final_path_for_meta

        manifest_path = final_path_for_meta(
            {
                "id": "coding.project.manifest.test",
                "coord": manifest_env.coord,
                "domain": manifest_env.payload.get("domain"),
                "subtopic": manifest_env.payload.get("subtopic"),
            }
        )
        check(
            "manifest storage nests under the project_manifest type folder",
            manifest_path.parent.name == "project_manifest",
            detail=str(manifest_path),
        )

        subdomain_flag._FLAG_CACHE = frozenset({8})
        subdomain_flag._RAW_CACHE = [8]
        dynamic_manifest_env = build_project_manifest_envelope(manifest, "cycle:test-dynamic")
        dynamic_manifest_path = final_path_for_meta(
            {
                "id": "coding.project.manifest.dynamic",
                "coord": dynamic_manifest_env.coord,
                "domain": dynamic_manifest_env.payload.get("domain"),
                "subtopic": dynamic_manifest_env.payload.get("subtopic"),
            }
        )
        check(
            "dynamic coding storage still resolves under lobe 8",
            dynamic_manifest_env.coord[0] == 8,
            detail=f"coord={dynamic_manifest_env.coord}",
        )
        check(
            "dynamic coding storage still nests under project_manifest",
            dynamic_manifest_path.parent.name == "project_manifest",
            detail=str(dynamic_manifest_path),
        )
        subdomain_flag._FLAG_CACHE = frozenset()
        subdomain_flag._RAW_CACHE = []

        ts_project = tmp / "TypeScript Project"
        write(ts_project / "src" / "app.ts", "export const value = 1;\n")
        write(ts_project / "src" / "view.tsx", "export function View() { return null; }\n")
        ts_manifest = scan_project_tree(ts_project, project_name="typescript-project")
        ts_env = build_project_manifest_envelope(ts_manifest, "cycle:test")
        check(
            "typescript manifest targets TypeScript coding subdomain",
            ts_env.coord == [8, 8, 0],
            detail=f"coord={ts_env.coord}",
        )

        # Phase 0a — domain-key reconciliation: the engram `domain` field must be the
        # slugged mastery key (normalize_domain_id), NOT the `_systems` dewey key, or
        # coding-domain hotness lookup misses and APPLY never fires.
        check(
            "manifest envelope domain is the slugged mastery key, not the _systems dewey key",
            manifest_env.payload["domain"] == "python",
            detail=f"domain={manifest_env.payload['domain']}",
        )
        check(
            "envelope domain matches normalize_domain_id (no underscore divergence)",
            manifest_env.payload["domain"] == normalize_domain_id("python")
            and "_" not in manifest_env.payload["domain"],
            detail=f"domain={manifest_env.payload['domain']}",
        )
        check(
            "typescript envelope domain is the canonical slug",
            ts_env.payload["domain"] == "typescript",
            detail=f"domain={ts_env.payload['domain']}",
        )

        rust_project = tmp / "Rust Project"
        write(rust_project / "src" / "main.rs", "fn main() {}\n")
        rust_manifest = scan_project_tree(rust_project, project_name="rust-project")
        rust_env = build_project_manifest_envelope(rust_manifest, "cycle:test")
        check(
            "rust manifest targets rust_systems coding subdomain",
            rust_env.coord == [8, 13, 0],
            detail=f"coord={rust_env.coord}",
        )

        writes: List[Tuple[Any, bool]] = []

        def fake_writer(envelope: Any, *, propose: bool = True) -> dict:
            writes.append((envelope, propose))
            return {"ok": True, "type": envelope.type}

        receipt = write_project_snapshot_to_memory(
            snapshot,
            "cycle:test",
            writer=fake_writer,
        )
        check("snapshot memory write uses injected writer", receipt["ok"])
        check(
            "snapshot memory write commits directly by default "
            "(propose=True is user-chat-ingress-only and write_memory rejects it "
            "for source=memory.coding_project)",
            writes[0][1] is False,
        )

        partial_project = tmp / "Partial Project"
        write(partial_project / "keep.py", "x = 1\n")
        write(partial_project / "skip.log", "runtime noise\n")
        try:
            snapshot_project_tree(
                partial_project,
                dest_root=dest,
                project_name="partial",
                snapshot_id="snap-exact-refuses",
            )
            refused = False
        except ProjectSnapshotError:
            refused = True
        check("exact snapshot refuses skipped files", refused)

        partial = snapshot_project_tree(
            partial_project,
            dest_root=dest,
            project_name="partial",
            snapshot_id="snap-partial",
            allow_partial=True,
        )
        events = Path(partial.snapshot_root) / "EVENTS.jsonl"
        check(
            "partial snapshot records skipped entries",
            "excluded_glob" in events.read_text(encoding="utf-8"),
        )

        manifest_json = Path(snapshot.manifest_path).read_text(encoding="utf-8")
        check(
            "manifest JSON is readable",
            json.loads(manifest_json)["project_name"] == "source-project",
        )
    finally:
        subdomain_flag._FLAG_CACHE = original_flag_cache
        subdomain_flag._RAW_CACHE = original_raw_cache
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    print("=" * 72)
    print(f"TOTAL: PASS={PASS}  FAIL={FAIL}")
    print("=" * 72)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

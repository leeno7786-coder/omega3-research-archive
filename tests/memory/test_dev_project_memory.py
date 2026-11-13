"""Canonical snapshots own restoration; legacy project mirrors are ignored."""

from __future__ import annotations

import hashlib
import json
import os

from memory import dev_project_store as dps


def test_legacy_manifest_cannot_replace_canonical_content_or_destination(monkeypatch, tmp_path):
    workspace = (tmp_path / "workspace").resolve()
    workspace.mkdir()
    monkeypatch.setenv("OMEGA_OPERATOR_WORKSPACE", str(workspace))
    isolated_omega = tmp_path / "omega"
    monkeypatch.setattr(dps, "OMEGA_ROOT", isolated_omega)
    projects = isolated_omega / "memory" / "1100_dev" / "projects"
    project = projects / "build-widget"
    project.mkdir(parents=True)
    destination = workspace / "src" / "widget.py"
    legacy_content = "STALE_MIRROR = True\n"
    (project / "widget.py").write_text(legacy_content, encoding="utf-8")
    (project / "_omega_project.json").write_text(
        json.dumps(
            {
                "workspace_root": str(workspace),
                "file_manifest": [
                    {
                        "path": "src/widget.py",
                        "file_path": str(destination),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    canonical_content = "CANONICAL_SNAPSHOT = True\n"
    monkeypatch.setattr(
        dps,
        "_canonical_snapshot_records",
        lambda _project: {
            ("operator_workspace", os.path.normcase(str(destination))): {
                "enrichment": {
                    "scope": "operator_workspace",
                    "workspace_root": str(workspace),
                    "relative_path": "src/widget.py",
                    "content_sha256": hashlib.sha256(
                        canonical_content.encode("utf-8")
                    ).hexdigest(),
                    "deleted": False,
                },
                "_text": canonical_content,
            }
        },
    )

    restored = dps.restore_project_files("Build Widget")
    assert restored == [("src/widget.py", str(destination))]
    assert destination.read_text(encoding="utf-8") == canonical_content
    assert (project / "widget.py").read_text(encoding="utf-8") == legacy_content

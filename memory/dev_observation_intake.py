"""Verified native-tool observations for the isolated development lobe.

The builder artifact intake stores file bytes.  This module stores the other
half of an agentic run: the exact trusted location, stable project identity,
directory tree, strings returned by read/search tools, and the operation that
produced them.  Records enter episode SRAM and are committed by DeepSleep; no
observation is written directly to the canonical filesystem.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from core.symphony_time import now_iso
from memory import episode_sram
from memory.api import CanonicalEnvelope

_MAX_TREE_ENTRIES = 500
_MAX_STRINGS = 120
_MAX_STRING_CHARS = 600
_MAX_TEXT_CHARS = 3_800
_MAX_COMMAND_CHARS = 500

# Browser, web, and plugin observations belong to their own memory surfaces.
# This list is intentionally the filesystem/dev rail only.
DEV_OBSERVATION_TOOLS = frozenset(
    {
        "start_job",
        "list_directory",
        "read_files",
        "search_repo",
        "inspect_git_diff",
        "write_file",
        "edit_file",
        "run_command",
        "run_tests",
        "restore_project_from_memory",
        "stop_job",
    }
)

_SECRET_RE = re.compile(
    r"(?i)(bearer\s+|token\s*[=:]\s*|password\s*[=:]\s*|secret\s*[=:]\s*|api[_-]?key\s*[=:]\s*)[^\s,;]+"
)
_LISTING_RE = re.compile(r"^(DIR|FILE)\t(.+)$")
_DRIVE_RE = re.compile(r"^(?P<drive>[A-Za-z]):[\\/]")


def _clip(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def redact_secrets(value: Any, limit: int = _MAX_COMMAND_CHARS) -> str:
    """Strip credential values, keeping the label and a visible ``[redacted]`` marker.

    Public because WO-05 step 5 needs this exact treatment at AUTO's capture seam. A second
    copy of the pattern there would be two redactors that can drift, and the weaker one would
    be the one guarding the newer seam — the defect class law section 2.1 names.
    """
    return _SECRET_RE.sub(r"\1[redacted]", _clip(value, limit))


#: The original private name, kept so existing callers are untouched.
_redact = redact_secrets


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False


def _drive_root(path: Path) -> str:
    text = str(path)
    match = _DRIVE_RE.match(text)
    if match:
        return f"{match.group('drive').upper()}:\\"
    # UNC paths have no drive letter. Keep the exact anchor rather than
    # inventing one; Path.anchor is stable on Windows and harmless elsewhere.
    return str(Path(path.anchor or path).resolve(strict=False))


def _trusted_roots(ctx: Any) -> tuple[Path, ...]:
    roots = tuple(Path(root).resolve(strict=False) for root in (getattr(ctx, "trusted_roots", ()) or ()))
    workspace = getattr(ctx, "workspace_root", None)
    if workspace is not None:
        roots = (*roots, Path(workspace).resolve(strict=False))
    # Deduplicate without changing order.  The first root is the active
    # authority and is therefore the preferred project boundary.
    return tuple(dict.fromkeys(roots))


def _safe_path(raw: Any, ctx: Any) -> Path | None:
    value = str(raw or "").strip()
    if not value:
        return None
    try:
        target = ctx.resolve(value)
    except (OSError, TypeError, ValueError):
        return None
    roots = _trusted_roots(ctx)
    if roots and not any(_within(target, root) for root in roots):
        return None
    return target.resolve(strict=False)


def _record_paths(record: Mapping[str, Any], ctx: Any) -> list[Path]:
    tool = str(record.get("tool_name") or "")
    args = record.get("args") if isinstance(record.get("args"), Mapping) else {}
    raw_values: list[Any] = []
    touched = record.get("touched_files")
    if isinstance(touched, (list, tuple)):
        raw_values.extend(touched)
    if tool in {"list_directory", "search_repo", "inspect_git_diff", "restore_project_from_memory"}:
        raw_values.append(args.get("path") or ".")
    elif tool == "read_files":
        paths = args.get("paths") if args.get("paths") is not None else args.get("path")
        raw_values.extend(paths if isinstance(paths, (list, tuple)) else [paths])
    elif tool == "start_job":
        raw_values.append(args.get("project_root"))
    out: list[Path] = []
    for raw in raw_values:
        target = _safe_path(raw, ctx)
        if target is not None and target not in out:
            out.append(target)
    if not out and getattr(ctx, "workspace_available", True):
        workspace = getattr(ctx, "workspace_root", None)
        if workspace is not None:
            out.append(Path(workspace).resolve(strict=False))
    return out


def _project_root(path: Path, ctx: Any) -> Path:
    """Use the selected job's project, or discover a read-only observation's root."""
    job_id = str(getattr(ctx, "job_id", "") or "")
    if job_id:
        from auto_loop.execute.job_control import get_job

        selected = str(get_job(job_id).project_root or "")
        if selected and _within(path, Path(selected)):
            # A new project need not have a package manifest yet. A nested first
            # write must retain the project identity that start_job selected.
            return Path(selected).resolve(strict=False)
    roots = _trusted_roots(ctx)
    active = roots[0] if roots else path
    current = path if path.is_dir() else path.parent
    markers = (".git", "pyproject.toml", "package.json", "Cargo.toml", "go.mod", "CMakeLists.txt")
    while True:
        if any((current / marker).exists() for marker in markers):
            return current
        if current == active or not _within(current, active) or current.parent == current:
            break
        current = current.parent
    return path if path.is_dir() else path.parent


def _project_name(root: Path, drive_root: str) -> str:
    # A drive-root observation has no repository name. Preserve the drive in a
    # stable, portable key (drive-e-root), while ordinary projects keep their
    # directory name as the human-facing identity.
    if str(root).rstrip("\\/").upper() == drive_root.rstrip("\\/").upper():
        match = re.match(r"([A-Za-z]):", drive_root)
        return f"drive-{match.group(1).lower()}-root" if match else "workspace-root"
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", root.name).strip("-._ ").lower()
    return name[:80] or "workspace"


def _relative(path: Path, root: Path) -> str:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        rel = path.name
    return rel or "."


def _listing(output: str, observed: Path) -> tuple[list[dict[str, str]], list[str]]:
    tree: list[dict[str, str]] = []
    strings: list[str] = []
    lines = str(output or "").splitlines()
    for line in lines:
        match = _LISTING_RE.match(line.strip())
        if not match:
            continue
        kind, name = match.groups()
        name = _clip(name, 240)
        tree.append({"kind": kind, "name": name, "relative_path": name})
        strings.append(name)
        if len(tree) >= _MAX_TREE_ENTRIES:
            break
    return tree, strings


def _strings(tool: str, output: str, tree: list[dict[str, str]]) -> list[str]:
    if tree:
        return [str(item["name"]) for item in tree[:_MAX_STRINGS]]
    lines: list[str] = []
    for line in str(output or "").splitlines():
        text = _redact(line, _MAX_STRING_CHARS)
        if text and not text.startswith("… [truncated"):
            lines.append(text)
        if len(lines) >= _MAX_STRINGS:
            break
    return lines


def _safe_args(record: Mapping[str, Any]) -> dict[str, Any]:
    args = record.get("args") if isinstance(record.get("args"), Mapping) else {}
    safe: dict[str, Any] = {}
    for key in ("path", "project_root", "query", "pattern", "job_id", "instruction"):
        if key in args:
            value = args[key]
            safe[key] = [_redact(item, 300) for item in value[:20]] if isinstance(value, list) else _redact(value, 600)
    if "paths" in args and "paths" not in safe:
        values = args.get("paths")
        safe["paths"] = [_redact(item, 300) for item in values[:20]] if isinstance(values, list) else _redact(values, 600)
    if "command" in args:
        safe["command"] = _redact(args.get("command"))
    return safe


def _observation_kind(tool: str, record: Mapping[str, Any]) -> tuple[int, str]:
    if tool in {"list_directory", "inspect_git_diff"}:
        return 5, "full_tree" if tool == "list_directory" else "operations"
    if tool in {"read_files", "search_repo"}:
        return 6, "strings"
    if tool == "start_job":
        return 4, "projects"
    return 7, "operations"


def hold_dev_tool_observation(
    *, episode_id: str, objective: str, record: Mapping[str, Any], ctx: Any
) -> dict[str, Any]:
    """Hold one bounded native-tool observation in the current episode SRAM.

    The return value is intentionally explicit when no bucket is open or when
    intake fails; callers retain that receipt in ``operator_tool_calls`` so a
    persistence problem cannot masquerade as an empty observation.
    """
    tool = str(record.get("tool_name") or "").strip()
    if tool not in DEV_OBSERVATION_TOOLS:
        return {"status": "not_applicable", "tool": tool}
    ep_id = str(episode_id or "").strip()
    if not ep_id or not episode_sram.is_open(ep_id):
        return {"status": "not_held", "tool": tool, "reason": "no_open_sram_bucket"}

    # A run's observation carries the run's exact identity so run-scoped recall can find
    # it; a job that cannot be read fails the hold rather than writing an unowned record.
    run_provenance = None
    job_id = str(getattr(ctx, "job_id", "") or "")
    if job_id:
        from memory.run_anchor import run_provenance_for_job

        try:
            run_provenance = run_provenance_for_job(job_id)
        except Exception as exc:  # preserve cause in the tool ledger, never claim held
            return {"status": "failed", "tool": tool, "error": f"{type(exc).__name__}: {exc}"}

    paths = _record_paths(record, ctx)
    if not paths:
        return {
            "status": "not_held",
            "tool": tool,
            "reason": "no_authorized_path",
            "error": _redact(record.get("error") or "", 500),
        }
    observed_path = paths[0]
    project_root = _project_root(observed_path, ctx)
    drive_root = _drive_root(observed_path)
    project = _project_name(project_root, drive_root)
    tree, _ = _listing(str(record.get("output") or ""), observed_path)
    output = _redact(record.get("output") or "", _MAX_TEXT_CHARS)
    strings = _strings(tool, output, tree)
    y, kind = _observation_kind(tool, record)
    status = str(record.get("status") or "unknown")
    touched = [str(item) for item in (record.get("touched_files") or ()) if str(item).strip()]
    observed_at = now_iso()
    relative_observed = _relative(observed_path, project_root)
    summary = (
        f"{tool} {status}: project {project} at {observed_path}"
        + (f" ({len(tree)} tree entries)" if tree else f" ({len(strings)} strings)")
    )[:500]
    text_lines = [
        f"Verified dev observation: {tool} ({status})",
        f"Project: {project}",
        f"Project root: {project_root}",
        f"Drive root: {drive_root}",
        f"Observed path: {observed_path}",
    ]
    if tree:
        text_lines.append("Tree:\n" + "\n".join(f"{item['kind']}\t{item['relative_path']}" for item in tree))
    if strings and not tree:
        text_lines.append("Strings:\n" + "\n".join(strings))
    if touched:
        text_lines.append("Touched files:\n" + "\n".join(touched[:100]))
    if record.get("error"):
        text_lines.append("Error: " + _redact(record.get("error"), 500))
    text = "\n".join(text_lines)[:_MAX_TEXT_CHARS]
    payload = {
        "episode_id": ep_id,
        "objective": _clip(objective, 2_000),
        "project": project,
        "project_root": str(project_root),
        "drive_root": drive_root,
        "workspace_root": str(getattr(ctx, "workspace_root", "")),
        "observed_path": str(observed_path),
        "relative_observed_path": relative_observed,
        "tool": tool,
        "status": status,
        "args": _safe_args(record),
        "tree": tree,
        "strings": strings,
        "touched_files": touched[:100],
        "output_excerpt": output,
        "observed_at": observed_at,
        "verified_tool_evidence": True,
        "title": f"Dev {kind}: {project} ({relative_observed})",
        "summary": summary,
        "text": text,
        "subtopic": f"dev_{kind}",
        "tags": ["dev", "tool_observation", kind, project, drive_root],
        "enrichment": {
            "project": project,
            "project_root": str(project_root),
            "drive_root": drive_root,
            "workspace_root": str(getattr(ctx, "workspace_root", "")),
            "observed_path": str(observed_path),
            "relative_observed_path": relative_observed,
            "tool": tool,
            "verified_tool_evidence": True,
            "tree_entry_count": len(tree),
            "string_count": len(strings),
            "touched_file_count": len(touched),
        },
    }
    if run_provenance is not None:
        payload["run_provenance"] = dict(run_provenance)
        payload["enrichment"]["run_provenance"] = dict(run_provenance)
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    payload["observation_sha256"] = digest

    def _hold(payload_to_hold: Mapping[str, Any], coord: list[int]) -> str:
        return episode_sram.hold_memory(
            CanonicalEnvelope(
                cycle_id=ep_id,
                coord=coord,
                type="dev_tool_observation",
                payload=dict(payload_to_hold),
                source="auto_loop.dev_observation",
                confidence=1.0 if status == "success" else 0.9,
                provenance=["auto_tool", "verified_tool_result", "episode_sram"],
                decay_policy="none",
                permissions={"read": True, "write": True},
                strength_index=1.0,
            ),
            propose=False,
            bypass_guardian=True,
            skip_embed=False,
            episode_id=ep_id,
        )

    try:
        # The project index is a separate typed record so a later
        # "which project/drive was that?" query does not need to widen into
        # every tree and source-string observation.  The detailed record then
        # answers the requested tree, strings, or operations view.
        project_payload = {
            **payload,
            "tree": [],
            "strings": [],
            "touched_files": [],
            "output_excerpt": "",
            "title": f"Dev project: {project}",
            "summary": f"Project {project} at {project_root} on {drive_root}",
            "text": (
                f"Verified dev project identity\nProject: {project}\n"
                f"Project root: {project_root}\nDrive root: {drive_root}\n"
                f"Workspace root: {getattr(ctx, 'workspace_root', '')}"
            )[:_MAX_TEXT_CHARS],
            "subtopic": "dev_projects",
            "observation_sha256": digest,
        }
        project_ref = _hold(project_payload, [11, 4, 0])
        ref = _hold(payload, [11, y, 0])
    except Exception as exc:  # preserve cause in the tool ledger, never claim held
        return {"status": "failed", "tool": tool, "project": project, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "held",
        "tool": tool,
        "project": project,
        "kind": kind,
        "coord": [11, y, 0],
        "sram_ref": ref,
        "project_sram_ref": project_ref,
        "observation_sha256": digest,
    }


# ── Trusted workspaces as dev memory ─────────────────────────────────────────
# Operator 2026-09-15: "save them to dev memory and have the dev mril tool recall them when
# working so omega has context of where its allowed to work". The grant store stays the
# authority; this is the recallable account of it, held once per change into the SRAM of the
# agent episode that first works after the change and committed by DeepSleep like every
# other dev observation (lobe 11, projects facet).

_WORKSPACE_SIGNATURE_ENV = "OMEGA_DEV_WORKSPACE_MEMORY_PATH"


def _workspace_signature_path() -> Path:
    import os

    override = os.environ.get(_WORKSPACE_SIGNATURE_ENV, "").strip()
    if override:
        return Path(override)
    from core.paths import GUARDIAN_STATE_ROOT

    return GUARDIAN_STATE_ROOT / "dev_workspace_memory.json"


def trusted_workspace_facts() -> dict[str, Any]:
    """The granted roots and the active one, as the host knows them right now."""
    from core.trusted_folders import active_trusted_folder, load_trusted_folders

    roots = [str(folder.path) for folder in load_trusted_folders()]
    active = active_trusted_folder()
    return {"roots": roots, "active": str(active) if active is not None else ""}


def hold_trusted_workspaces(*, episode_id: str, facts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Hold the trusted-workspace account in this episode's SRAM when the grants changed.

    Idempotent across episodes: the signature of the last held account is kept beside the
    grant store, so an unchanged set of workspaces is not written again every agent turn.
    """
    ep_id = str(episode_id or "").strip()
    if not ep_id or not episode_sram.is_open(ep_id):
        return {"status": "not_held", "reason": "no_open_sram_bucket"}
    facts = dict(facts) if facts is not None else trusted_workspace_facts()
    roots = [str(root) for root in facts.get("roots") or () if str(root).strip()]
    active = str(facts.get("active") or "")
    if not roots:
        return {"status": "not_held", "reason": "no_trusted_workspaces"}
    signature = hashlib.sha256(json.dumps({"roots": roots, "active": active}, sort_keys=True).encode("utf-8")).hexdigest()
    stamp_path = _workspace_signature_path()
    try:
        previous = json.loads(stamp_path.read_text(encoding="utf-8")).get("signature") if stamp_path.is_file() else ""
    except (OSError, ValueError):
        previous = ""
    if previous == signature:
        return {"status": "unchanged", "signature": signature}
    listed = "\n".join(f"- {root}" + (" (active working folder)" if root == active else "") for root in roots)
    text = (
        "Trusted workspaces (operator-granted folders where Omega's agent tools may work):\n"
        f"{listed}\n"
        f"Active working folder: {active or 'none selected'}\n"
        "The operator grants a folder with /trust <path> and selects one with /cd <path>."
    )[:_MAX_TEXT_CHARS]
    drive_roots = sorted({_drive_root(Path(root)) for root in roots})
    payload = {
        "episode_id": ep_id,
        "roots": roots,
        "active": active,
        "observed_at": now_iso(),
        "verified_tool_evidence": True,
        "title": "Dev projects: trusted workspaces",
        "summary": f"Trusted workspaces: {', '.join(roots)}; active {active or 'none'}"[:500],
        "text": text,
        "subtopic": "dev_projects",
        "tags": ["dev", "workspace", "trusted_folders", *drive_roots],
        "enrichment": {"roots": roots, "active": active, "drive_roots": drive_roots},
        "observation_sha256": signature,
    }
    memory_id = episode_sram.hold_memory(
        CanonicalEnvelope(
            cycle_id=ep_id,
            coord=[11, 4, 0],
            type="dev_tool_observation",
            payload=payload,
            source="core.trusted_folders",
            confidence=1.0,
            provenance=["operator_grant", "trusted_folder_store", "episode_sram"],
            decay_policy="none",
            permissions={"read": True, "write": True},
            strength_index=1.0,
        ),
        propose=False,
        bypass_guardian=True,
        skip_embed=False,
        episode_id=ep_id,
    )
    try:
        stamp_path.parent.mkdir(parents=True, exist_ok=True)
        stamp_path.write_text(json.dumps({"signature": signature, "roots": roots, "active": active}), encoding="utf-8")
    except OSError:
        pass  # the next agent turn holds it again; a duplicate memory beats a missing one
    return {"status": "held", "memory_id": memory_id, "signature": signature}

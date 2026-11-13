# Dev-Lobe Project Memory — Durable Rehydrate for Failed-Build Re-Execution

**Status:** design (approved shape, pending spec review) · **Date:** 2026-07-23 · **Lobe:** 11 (`memory/1100_dev/`)

## Problem

When Omega's autonomous builder fails a build (stall ≥ 2 → deterministic STOP), the build folds
away and its scratch workspace is ported to `_incomplete/` and **cleared**. On re-execution the
resume re-grounds the executor by reading file *contents* from the workspace
(`schema/qwen_skill_dispatch._workspace_file_state`). Once the workspace is cleared that read
returns nothing, so the model resumes **blind** — it re-architects against files it can no longer
see (live finding: a 4B wrote a class-based test suite against an `app.py` it never implemented,
because the resume only carried filenames).

Today's mitigation holds/passes build context across the fold — a parallel state channel that
drifts from truth. That drift *is* the sync problem.

## Goal

Make the **dev-lobe memory** (`memory/1100_dev/projects/<project>/`) the durable truth for
in-progress dev work, so a failed build re-executes by **recalling its project from memory** and
rehydrating the workspace — nothing held or passed across the fold. Memory is authoritative on
return; the workspace is scratch.

## Doctrine reconciliation

Raw file **bytes** live in the lobe-11 filesystem store (`dev_project_store`); the canonical memory
**record** holds the anchor + tree manifest + links (`_omega_project.json`). "Memory holds the
files" is true — via the dev-lobe store, not by pushing bytes through `write_memory` /
`CanonicalEnvelope`. This is already how coding memory works; the design preserves the split.

## Existing machinery (reused, not rebuilt)

| Piece | Location | Role |
|---|---|---|
| Per-project store mirror | `memory/dev_project_store.persist_project_files` | overwrite-latest by objective-derived project name; writes `_omega_project.json` (+ `file_manifest`). Fires batch at fold (`cognition_chain.py ~3161`). |
| Resume context | `schema/qwen_skill_dispatch._workspace_file_state` / `_project_record_ledger` / `_resume_context_for` | build the resume directive; read file contents from the **scratch workspace**. |
| Rehydrate tool | `qwen-agent-tui` `restore_project_from_memory` → `copyMemoryProjectTree` | copies the memory tree → workspace, keyed by project. Available only to destructive/always-allow execution paths because it overwrites workspace files. |
| Rollback | `core/dev_rollback_capture` + HUD `POST /api/dev/rollback` (`hud/server.py:1577`) | settings-panel rollback: restores/deletes the workspace file + enqueues a `system_event_queue` rollback event. |

## Changes

### Change 1 — Mirror deletes (keep the store a complete, current mirror)

`persist_project_files` writes/overwrites touched files but never **removes**; a deleted file
lingers as a ghost, and the manifest reflects only the fold's touched set.

- After persisting touched files, **reconcile** the store tree against the current workspace
  project tree: remove store files whose project-relative path no longer exists in the workspace;
  rebuild `_omega_project.json.file_manifest` from the **full store tree**.
- **INVARIANT (load-bearing):** the reconcile runs **only when the workspace project tree is
  non-empty**. An empty/cleared workspace must NEVER trigger delete-reconcile — otherwise it nukes
  the store. Guard: count present project files in the workspace; if zero, skip reconcile entirely.

### Change 2 — Deterministic rehydrate on failed-build return (the core fix)

- At a failed-build resume, **before** the resume context is built, if the workspace lacks the
  project's files, **deterministically copy the memory store tree (`dev_projects_root()/project`)
  into the workspace** (a Python analogue of `copyMemoryProjectTree`). Then `_workspace_file_state`
  reads real contents and the executor edits real files.
- Done deterministically (fits the deterministic-dispatch doctrine) — not reliant on the model
  choosing to call `restore_project_from_memory`.
- **Seam:** a new `rehydrate_project_workspace(objective)` invoked at the resume-dispatch entry in
  `qwen_skill_dispatch`, ahead of `_resume_context_for` / `_workspace_file_state`.

### Change 3 — Persist before clear (ordering guarantee)

- Ensure the at-fold `persist_project_files` fires **before** the workspace is ported/cleared on a
  failed build, so the store is complete at the instant the workspace is wiped. Verify/enforce the
  order across the `cognition_chain` fold-back and `core/workspace_port` clear.

### Change 4 — User rollback stays consistent with memory (+ records the event)

The settings-panel rollback restores the WORKSPACE file and enqueues a system event, but **does not
update the dev-lobe store** → workspace = rolled-back, store = post-edit → the next rehydrate
(Change 2) would restore the post-edit version and **silently undo the user's rollback**.

- **4a (new):** in `api_dev_rollback` (`hud/server.py`), after the workspace restore/delete,
  **re-mirror the rolled-back state into the dev-lobe store** — write `before_content` to
  `dev_projects_root()/project/relpath`, or remove it from the store if the rollback deleted a
  created file. To locate the store file, extend `record_file_snapshots` to persist `project` +
  `relpath` in the snapshot record (both are on the `FileChange`).
- **4b (verify/extend):** the enqueued rollback event (`enqueue_rollback_event`) already tells
  Omega "work undone," and the next episode drains it. Verify it surfaces as an **episodic memory +
  chat line** ("user rolled back `<file>` — its state now differs from before"); extend the drain
  path if it currently only logs.
- **INVARIANT (anti-undo):** memory must never silently overwrite a user rollback. A rollback flows
  **workspace → memory** (the complement of the normal memory → workspace rehydrate); it does not
  get undone by memory.

## Direction summary

- **Normal return (failed build):** memory → workspace (Change 2). Memory authoritative.
- **User rollback:** workspace → memory (Change 4). User action authoritative, recorded.
- The two never fight: the anti-undo invariant means a rollback updates memory *before* any
  subsequent rehydrate reads it.

## Feature flag

`OMEGA_DEV_PROJECT_MEMORY` is the sole live gate. It defaults on and is reversible. The
setting intentionally has no informational file under `config/`: every YAML in that directory
must be a live, schema-validated config/policy/schema triplet.

## Data shapes

- Rollback snapshot record gains `project`, `relpath` (Change 4a).
- `_omega_project.json.file_manifest` becomes the full store tree, not the fold's touched set
  (Change 1).

## Error handling

All failure-soft, matching `dev_project_store` today: a memory-read miss → current behavior;
reconcile guarded against empty workspace; rollback store re-mirror failure logged, non-fatal
(never breaks the rollback response or the cognition cascade).

## Testing (TDD)

- **Change 1:** reconcile removes a store file deleted in the workspace; **the empty-workspace
  guard leaves the store intact**; manifest reflects the full tree.
- **Change 2:** failed-build resume with a cleared workspace → rehydrate materializes the tree →
  `_workspace_file_state` returns real contents (not "").
- **Change 3:** persist fires before clear (store complete at the moment the workspace is wiped).
- **Change 4a:** rollback re-mirrors the store (store file == `before_content`; delete-created
  removes it from the store); **anti-undo test** — rollback then rehydrate keeps the rolled-back
  version.
- **Change 4b:** rollback event drains to an episodic memory + chat line.

## Explicitly NOT doing (YAGNI)

- Per-touch mirroring (at-fold, per operator's call — folds are the checkpoint).
- Raw file bytes through `write_memory` / `CanonicalEnvelope` (FS store holds bytes; record holds
  anchor + manifest).
- A rewrite of the resume flow or the rollback UI.

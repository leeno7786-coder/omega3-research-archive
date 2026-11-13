# Omega 3.0 — release candidate gates

**Status: source-only release candidate under private review. Not yet authorized for public GitHub publication.**

## Release decisions

- **Lifecycle:** archived/retired research code, not an actively supported Omega AI product.
- **Source authority:** F:\Omega3.0 working copy as of 2026-11-13, including the local browser commit and vetted uncommitted changes.
- **History:** start a **new clean Git repository** for public release; do **not** make the historic 3,642-commit private repository public without full history cleanup.
- **License:** existing MIT license retained; it expressly permits commercial copying and redistribution. If this is not desired, select a different license *before* publishing.
- **Distribution:** complete source exploration package, **not** a portable binary with models and user state.
- **Successor:** Odin pursues narrower intelligence/memory/learning research; it is not bundled as an Omega dependency.

## Source changes made ONLY in the isolated candidate

1. Corrected malformed, duplicated `core/shared_state.py::snapshot` and documented deep-copy behavior.
2. Added `tests/core/test_release_shared_state_snapshot.py`.
3. Included locally present `runtime/model_templates.py`, `browser_agent/extract.py`, newer memory and browser changes, and selected standalone memory research files.
4. Replaced outdated public README with a historical research navigation guide; added public architecture map and retirement retrospective.
5. Excluded internal logs, historical code-review evidence, patent application material, human memory-profile snapshots, academic-submission drafts, local binaries, installed models and private machine state.
6. Added syntax and credential-pattern scanners and focused test driver in `release-prep` (not intended to be published).

## Before public visibility

- [x] Assemble a full source-only snapshot separate from F: and private GitHub history.
- [x] Repair the known candidate Python syntax error.
- [x] Python syntax parse of all staged Python files.
- [x] Browser mock/unit regression tests.
- [x] Specialist cross-lane handoff regression tests.
- [x] Remove memory/profile datasets and academic email disclosures.
- [x] Preliminary sensitive-key pattern check (no high-confidence matches in selected text).
- [ ] Resolve known historical cross-lane end-to-end test expectations.
- [x] Run wider focused memory, speech, and MCP/plugin checks (295 passed; 8 skipped across selected suites).
- [ ] Resolve process-worker spawn failure in the remote execution environment, or independently reproduce isolation on a supported machine.
- [ ] Run the complete test suite and address the remaining failures.
- [ ] Manual review of all code, docs and third-party licensing.
- [ ] Independent machine/security review before running with real credentials.
- [ ] Decide whether MIT's commercial permissions are acceptable.
- [ ] Make a new repository public and link it from omega2ai.com only after the gates above clear.

The existing private repo and all F: files remain unchanged.

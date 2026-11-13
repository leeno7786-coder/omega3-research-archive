"""Single source of truth for all Omega 3.0 filesystem paths.

OMEGA_ROOT is the project root — resolved from this file's own location, never
from a literal path, so the tree relocates with whatever drive letter or mount
point it lands on. Everything else is derived from it.

Per `project_guardian_sole_writer_doctrine` (2026-05-08), every write
across the entire system lands under exactly two trees:

    memory/    — the cognitive substrate. Sole writer is guardian
                 (mediated through memory.api). Subsystems never write
                 here directly.
    guardian/  — every audit, every state file, every sacred record.
                 Subsystems write here through emit_phase / emit_signal
                 (logs/), through the SharedState save layer (state/),
                 or via guardian's own modules (ledger/).

Pre-doctrine code wrote to `.omega_data/`. That tree is no longer
referenced by any live writer; if it exists on disk it's purely
operator-side fossils to be cleaned up by day-zero. No constant
in this file points there.

Phase 7 (2026-06-21; store relocated under memory/ on 2026-06-25): the deterministic
memory taxonomy substrate is AUTONOMOUS memory, built Guardian-free — Guardian is tied
ONLY to user-facing input, so the taxonomy never routes through guardian.* / memory.api.
Its CODE lives at `memory/taxonomy/`; its DATA store now lives BESIDE it inside the
memory system at `MEMORY_TAXONOMY_ROOT` (memory/taxonomy_store/) — it IS part of the
actual memory, not an exiled third tree. "Guardian-free" describes the WRITE PATH, not
the folder, so co-locating the store under memory/ keeps that property intact. See
docs/architecture/PHASE_7_MEMORY_TAXONOMY_MAP.md.
"""

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger("omega.core.paths")


# Project root — parent of core/, memory/, config/, guardian/.
OMEGA_ROOT = Path(__file__).resolve().parent.parent

# The two write trees:
MEMORY_ROOT = OMEGA_ROOT / "memory"
CONFIG_ROOT = OMEGA_ROOT / "config"

# Canonical memory-record schema (same name the standalone service packages
# as an asset; here it lives in the repo schema tree).
MEMORY_SCHEMA_PATH = OMEGA_ROOT / "schema" / "memory_schema.json"

# Guardian sub-roots — the canonical audit/state/sacred surfaces.
GUARDIAN_LOGS_ROOT = OMEGA_ROOT / "guardian" / "logs"


def guardian_logs_root(omega_root: Path | None = None) -> Path:
    """The directory every Guardian log goes to, resolved when it is written.

    Ten places computed this same path independently, each capturing it at
    import, so there was no single thing to point elsewhere. That is why a test
    run wrote into the operator's live logs: a boot test installs the component
    log router, and the router's directory was fixed the moment it was imported.
    It cost real diagnostic time -- a "run_continuous exit: RuntimeError: loop
    failed" that looked like a broken boot turned out to be a test, writing into
    the same file a real boot writes.

    ``OMEGA_GUARDIAN_LOGS_DIR`` overrides it. An environment variable rather
    than a patched module attribute on purpose: these tests spawn real child
    processes, and a child inherits the environment but not a monkeypatch.

    ``omega_root`` selects the product tree whose logs are meant when no
    override is set, so a relocated or alternate tree logs inside itself.
    """
    override = os.environ.get("OMEGA_GUARDIAN_LOGS_DIR", "").strip()
    if override:
        return Path(override)
    if omega_root is None:
        return GUARDIAN_LOGS_ROOT
    return Path(omega_root) / "guardian" / "logs"
GUARDIAN_STATE_ROOT = OMEGA_ROOT / "guardian" / "state"
LEDGER_ROOT = OMEGA_ROOT / "guardian" / "ledger"
MCP_STATE_ROOT = GUARDIAN_STATE_ROOT / "mcp"
MCP_AUTHORIZATION_ROOT = MCP_STATE_ROOT / "authorization"
MCP_AUTHORIZATION_STORE_PATH = MCP_AUTHORIZATION_ROOT / "tokens.json"
MCP_OAUTH_VAULT_PATH = MCP_AUTHORIZATION_ROOT / "oauth-vault.json"
OAUTH_PROFILE_CATALOG_PATH = OMEGA_ROOT / "plugin_platform" / "oauth_profiles.json"
MCP_CONNECTIONS_PATH = MCP_AUTHORIZATION_ROOT / "connections.json"
MCP_PACKAGES_ROOT = MCP_STATE_ROOT / "packages"
MCP_REGISTRY_PATH = MCP_STATE_ROOT / "registry.json"
MCP_APPS_REGISTRY_PATH = MCP_STATE_ROOT / "apps_registry.json"
MCP_APP_PROVISION_JOURNAL_PATH = MCP_STATE_ROOT / "app_provision_journal.json"
MCP_EXTERNAL_SWITCH_PATH = MCP_STATE_ROOT / "external_switch.json"
MCP_ACTION_LEDGER_PATH = MCP_STATE_ROOT / "action_ledger.json"
MCP_PLUGIN_AUTHORITY_STATE_PATH = MCP_STATE_ROOT / "plugin_authority.json"
MCP_PLUGIN_PERMISSION_STATE_PATH = MCP_STATE_ROOT / "plugin_permissions.json"
MCP_PLUGIN_JOBS_STATE_PATH = MCP_STATE_ROOT / "plugin_jobs.json"
PLUGIN_STATE_ROOT = GUARDIAN_STATE_ROOT / "plugins"
PLUGIN_PACKAGES_ROOT = PLUGIN_STATE_ROOT / "packages"
# Content-addressed imported artifacts, held apart from PLUGIN_PACKAGES_ROOT on
# purpose: a package is an installed, signature-verified tree Omega runs from,
# while an artifact is inert third-party bytes pinned by digest and never run.
PLUGIN_ARTIFACTS_ROOT = PLUGIN_STATE_ROOT / "artifacts"
PLUGIN_REGISTRY_PATH = PLUGIN_STATE_ROOT / "registry.json"
PLUGIN_INSTALL_JOURNAL_PATH = PLUGIN_STATE_ROOT / "install_journal.json"
PLUGIN_MARKETPLACE_ROOT = PLUGIN_STATE_ROOT / "marketplace"
PLUGIN_MARKETPLACE_CATALOG_PATH = PLUGIN_MARKETPLACE_ROOT / "catalog.json"
PLUGIN_MARKETPLACE_LOCK_PATH = PLUGIN_MARKETPLACE_ROOT / "catalog.lock"
GUARDIAN_LEDGER_KEY_PATH = LEDGER_ROOT / "key.bin"
STAGING_ROOT = GUARDIAN_STATE_ROOT / "staging"
SANDBOX_RUNS_ROOT = GUARDIAN_STATE_ROOT / "sandbox" / "runs"
TEST_SCRATCH_ROOT = GUARDIAN_STATE_ROOT / "test_scratch"
FACE_THUMBNAILS_ROOT = MEMORY_ROOT / "700_vision" / "images"

# Cognitive tool roots. Keep all tool-creation filesystem decisions here so
# the substrate stays relocatable: callers derive from OMEGA_ROOT instead of
# embedding repo-local strings or reconstructing path layouts.
# The product owns the portable substrate.  The mind seat is selected by the
# operator (LM Studio or a cloud provider), while DME, STT, and TTS assets ship
# inside this same relocatable tree.
PORTABLE_RUNTIME_ROOT = OMEGA_ROOT / "runtime"
RUNTIME_MODELS_ROOT = OMEGA_ROOT / "models"
RUNTIME_STACKS_ROOT = OMEGA_ROOT / "runtimes"
RUNTIME_BIN_ROOT = RUNTIME_STACKS_ROOT / "bin"


def portable_python_path(omega_root: Path | None = None, *, platform: str | None = None) -> Path:
    """Return the bundled venv interpreter for the active product tree.

    The embedded interpreter is only the venv's base.  Runtime and tooling
    processes must use the venv so its bundled site-packages stay attached to
    the interpreter after the product moves to another drive or machine.

    Linux has no embeddable distribution, and a venv's ``pyvenv.cfg`` ``home`` is
    absolute, so it would break when the SSD mounts at another path.  The SSD
    instead carries one self-locating CPython whose own site-packages is the
    package environment (``omega.pth`` there puts the product root on the path,
    as ``..\\..`` does in the Windows venv's ``._pth``).
    """
    root = Path(omega_root) if omega_root is not None else OMEGA_ROOT
    target_platform = platform or sys.platform
    if target_platform == "win32":
        return root / ".venv-brain" / "Scripts" / "python.exe"
    if target_platform.startswith("linux"):
        return root / "python-3.13-linux" / "bin" / "python3"
    return root / ".venv-brain" / "bin" / "python"


def vitis_python_path(omega_root: Path | None = None) -> Path:
    """Return the Vitis lane interpreter: ``.venv-vitis`` on ``python-3.12-embed``.

    ``onnxruntime-vitisai`` is published for CPython 3.12 only, so the NPU
    provider cannot load into the 3.13 brain venv.  The lane is its own
    portable stack laid out exactly like the brain's (embed = base, venv = its
    package environment, ``..\\..`` in the venv ``._pth`` = the product root),
    and it runs one thing: ``runtime.vitis_worker``, as a child of the local
    services gateway.  Windows only -- the Ryzen AI provider ships for Windows.
    """
    root = Path(omega_root) if omega_root is not None else OMEGA_ROOT
    return root / ".venv-vitis" / "Scripts" / "python.exe"

# Phase 7 — deterministic memory taxonomy substrate. Its CODE lives at memory/taxonomy/;
# its DATA store sits BESIDE it under the memory system (operator directive 2026-06-25) —
# it IS part of the actual memory, not an exiled third tree. Still Guardian-free by
# WRITE-PATH (never routes through guardian.* / memory.api); "Guardian-free" describes who
# writes, not the folder. day-zero clears it as memory.
MEMORY_TAXONOMY_ROOT = MEMORY_ROOT / "taxonomy_store"

TOOL_SCHEMA_ROOT = OMEGA_ROOT / "schema" / "tools"
GENERATED_TOOL_SOURCE_ROOT = TOOL_SCHEMA_ROOT / "generated"
COGNITIVE_TOOL_LOBE_ROOT = MEMORY_ROOT / "500_tools" / "cognitive"
TOOL_AUTHORITY_AUDIT_LOG = GUARDIAN_LOGS_ROOT / "tool_authority.jsonl"


def dev_artifacts_root(omega_root: Path | None = None) -> Path:
    """Canonical root for operator/dev artifact files under the dev lobe.

    This is for concrete artifact files that Omega generates for development
    workflows (for example copied project trees), not for Guardian state.
    Passing ``omega_root`` keeps tests/workspace clones relocatable.
    """
    root = omega_root if omega_root is not None else OMEGA_ROOT
    return root / "memory" / "1100_dev" / "artifacts"


def ensure_child_path(root: Path, candidate: Path) -> Path:
    """Return candidate only when it resolves under root.

    `strict=False` keeps this useful before files exist. This is the shared
    containment guard for any path built from model/operator-provided names.
    Returns the RESOLVED path (symlinks normalized, relative paths made absolute)
    to ensure consistent behavior regardless of working directory or symlinks.
    """
    root_resolved = root.resolve(strict=False)
    candidate_resolved = candidate.resolve(strict=False)
    if candidate_resolved == root_resolved or root_resolved in candidate_resolved.parents:
        return candidate_resolved  # Return resolved path, not original
    raise ValueError(f"path escapes root: {candidate_resolved} not under {root_resolved}")

def product_relative_path(path: Path, *, omega_root: Path | None = None) -> str:
    """Return a product-relative path string, never a host drive letter.

    Training artifacts, manifests, and other recorded metadata must relocate
    with the SSD. An absolute path is a host-bound leak and is refused.
    """
    root = (Path(omega_root) if omega_root is not None else OMEGA_ROOT).resolve(strict=False)
    resolved = Path(path).resolve(strict=False)
    try:
        return str(resolved.relative_to(root))
    except ValueError as exc:
        raise ValueError(f"path escapes the product tree: {resolved}") from exc


def tool_schema_path(tool_name: str) -> Path:
    """Portable canonical schema path for one cognitive tool."""
    return ensure_child_path(TOOL_SCHEMA_ROOT, TOOL_SCHEMA_ROOT / f"{tool_name}.json")


def generated_tool_source_path(module_name: str) -> Path:
    """Portable canonical source path for one generated tool module."""
    return ensure_child_path(
        GENERATED_TOOL_SOURCE_ROOT,
        GENERATED_TOOL_SOURCE_ROOT / f"{module_name}.py",
    )

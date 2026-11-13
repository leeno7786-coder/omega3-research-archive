"""Pytest scoping for Omega's deliberate plain-script test convention.

Most of `tests/` follows the **plain-script convention**: the file *is*
the test — module-level checks that run top-to-bottom and terminate with
`sys.exit(0 if FAIL == 0 else 1)`, invoked as
`python tests/<system>/test_x.py` (or via `tests/run_full_matrix.py`).

pytest must NOT import those for collection: importing executes the test
at collection time and the module-level `sys.exit()` aborts the entire
pytest run with `INTERNALERROR: SystemExit` (the long-standing
`pytest --co` breakage). This conftest restricts pytest collection to
files that actually expose pytest-style `def test_*` / `class Test*`
at module scope; the plain-script suite is run by its own runner and is
intentionally ignored here. No test is lost — only the wrong runner is
prevented from choking on the right tests.
"""

from __future__ import annotations

import ast
import getpass
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Generator

import pytest

from core import paths as core_paths

# Test output must never land in the operator's live guardian/logs.
#
# It did. A boot test installs the component log router, the router wrote into
# the same per-logger files a real boot writes, and a "run_continuous exit:
# RuntimeError: loop failed" left there by a test read exactly like a broken
# boot. Diagnosing the live system meant first proving which lines were real.
#
# Set through the environment rather than a fixture because these tests spawn
# real child processes -- a child inherits the environment, not a monkeypatch --
# and set at import because collection imports boot and service modules before
# any fixture runs.
# Temp trees live on the repository's own drive.
#
# Omega runs from whatever drive it is copied to; pytest's temp root does not
# follow it. With the checkout on J: and the system temp on C:, five tests that
# hard-link the interpreter into a package tree failed for the whole life of
# that arrangement -- Windows reports a cross-volume hard link as "WinError 17:
# cannot move the file to a different disk drive" -- and were written off as
# environment-only. They were the suite disagreeing with the portability the
# product claims. Linking is worth keeping: these tests clone an interpreter
# and every DLL beside it, per test, and copying is slow enough to time them
# out. `tests/helpers/fs.py` still falls back to a copy, so a run rooted
# elsewhere is correct, just slower.
#
# The ROOT moves, not the basetemp: pytest keeps its own per-run numbering
# underneath, so two sessions at once still get separate trees. Pinning
# --basetemp instead makes concurrent runs collide on Windows.
# Prefer beside the repository, but some portable-drive ACLs allow the checkout
# and deny its drive root. Probe the destination before committing the process
# temp variables; a test run must work after the tree is copied under either ACL.
def _writable_pytest_root(candidate: Path) -> bool:
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        owner_root = candidate / f"pytest-of-{getpass.getuser()}"
        created_owner_root = not owner_root.exists()
        owner_root.mkdir(parents=True, exist_ok=True)
        _probe_root = Path(tempfile.mkdtemp(prefix="pytest-probe-", dir=str(owner_root)))
        _probe_child = Path(tempfile.mkdtemp(prefix="pytest-child-", dir=str(_probe_root)))
        _probe_child.rmdir()
        _probe_root.rmdir()
        if created_owner_root:
            owner_root.rmdir()
        return True
    except OSError:
        return False


_TEMP_ROOT = next(
    (
        candidate
        for candidate in (
            Path(__file__).resolve().parents[1].parent / ".omega-pytest-tmp",
            Path(tempfile.gettempdir()) / "Omega3-pytest-tmp",
            core_paths.OMEGA_ROOT / ".omega-pytest-tmp",
        )
        if _writable_pytest_root(candidate)
    ),
    None,
)
if _TEMP_ROOT is None:
    raise RuntimeError("no writable portable pytest temp root beside Omega")
for _name in ("TMPDIR", "TEMP", "TMP"):
    os.environ[_name] = str(_TEMP_ROOT)
tempfile.tempdir = str(_TEMP_ROOT)

# Test output must never land in the operator's live guardian/logs.
os.environ.setdefault("OMEGA_GUARDIAN_LOGS_DIR", str(_TEMP_ROOT / "omega-test-logs"))
# Cross-lane mail (T5) is durable operator-facing state. Nothing imports it yet, but T6
# binds it into Global B, after which every cascade test would pin mail into the live
# guardian tree. Redirected here, through its own documented override, before any import.
# This floor only protects the LIVE tree; `_isolate_cross_lane_mail` below gives each
# test its own file and cache, because one shared filename still leaks mail between tests.
os.environ.setdefault("OMEGA_CROSS_LANE_MAIL_PATH",
                      str(_TEMP_ROOT / "omega-test-cross-lane-mail.json"))

log = logging.getLogger("omega.tests.conftest")

_HERE = Path(__file__).parent
_COLLECTION_MCP_STATE_ROOT = Path(tempfile.mkdtemp(prefix="omega-test-mcp-")) / "mcp"


def _redirect_mcp_paths(root: Path) -> None:
    """Redirect every canonical MCP durable path as one indivisible test root.

    The authorization subtree belongs here too. It was missing, so collection --
    which imports modules that open the vault at import -- reached the
    operator's real one and the whole suite refused to collect with
    `oauth_vault_locked` unless OMEGA_SEAL_PASSPHRASE happened to be set in the
    shell. A test run must never need the operator's passphrase, and must never
    be one bug away from writing their credentials.
    """
    authorization = root / "authorization"
    core_paths.MCP_STATE_ROOT = root
    core_paths.MCP_PACKAGES_ROOT = root / "packages"
    core_paths.MCP_REGISTRY_PATH = root / "registry.json"
    core_paths.MCP_EXTERNAL_SWITCH_PATH = root / "external_switch.json"
    core_paths.MCP_ACTION_LEDGER_PATH = root / "action_ledger.json"
    core_paths.MCP_AUTHORIZATION_ROOT = authorization
    core_paths.MCP_AUTHORIZATION_STORE_PATH = authorization / "tokens.json"
    core_paths.MCP_OAUTH_VAULT_PATH = authorization / "oauth-vault.json"
    core_paths.MCP_CONNECTIONS_PATH = authorization / "connections.json"


# Collection can import boot/service modules before per-test fixtures run. Move
# their canonical defaults off the operator's live Guardian tree immediately.
#
# GUARDIAN_STATE_ROOT belongs here, not only the MCP paths beneath it:
# `hud.server` builds its whole application at import, which composes the
# plugin lifecycle service, which resolves the vault from this root. Leaving it
# live meant merely COLLECTING the suite opened the operator's OAuth vault --
# so the run refused with `oauth_vault_locked` unless their passphrase happened
# to be in the shell, and a bug in that path would have written to their real
# credentials. Every per-test fixture already redirects this; collection did not.
core_paths.GUARDIAN_STATE_ROOT = _COLLECTION_MCP_STATE_ROOT.parent
_redirect_mcp_paths(_COLLECTION_MCP_STATE_ROOT)


@pytest.fixture(scope="session")
def mcp_compiled_peer_cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Writable session cache for the real compiled MCP peer fixture.

    Keeping the cache under pytest's own temporary root makes the source,
    compiled artifact, and hardlink destination live on the same volume.  It
    also avoids depending on the operator-owned ``.pytest_cache`` ACL.
    """
    return tmp_path_factory.mktemp("mcp-compiled-peer-cache")

@pytest.fixture(autouse=True)
def _isolate_mcp_state(tmp_path, monkeypatch) -> Generator[None, None, None]:
    target = tmp_path / "guardian" / "state" / "mcp"
    authorization = target / "authorization"
    plugins = tmp_path / "guardian" / "state" / "plugins"
    # The authorization subtree holds the operator's real OAuth vault and its
    # sealed key. Anything composing the default lifecycle service (hud.server
    # does it at import) opens those paths, so leaving them unredirected let a
    # test read, rewrite, or birth credentials in the live installation.
    for name, value in {
        "MCP_STATE_ROOT": target,
        "MCP_PACKAGES_ROOT": target / "packages",
        "MCP_REGISTRY_PATH": target / "registry.json",
        "MCP_EXTERNAL_SWITCH_PATH": target / "external_switch.json",
        "MCP_ACTION_LEDGER_PATH": target / "action_ledger.json",
        "MCP_AUTHORIZATION_ROOT": authorization,
        "MCP_AUTHORIZATION_STORE_PATH": authorization / "tokens.json",
        "MCP_OAUTH_VAULT_PATH": authorization / "oauth-vault.json",
        "MCP_CONNECTIONS_PATH": authorization / "connections.json",
        # Every path derived from the plugin root has to move with it. These
        # are computed at import from the live root, so redirecting only the
        # root leaves them pointing into the real installation -- and a test
        # asserting the two agree then fails against a tmp root it never saw.
        "PLUGIN_STATE_ROOT": plugins,
        "PLUGIN_PACKAGES_ROOT": plugins / "packages",
        "PLUGIN_ARTIFACTS_ROOT": plugins / "artifacts",
        "PLUGIN_REGISTRY_PATH": plugins / "registry.json",
        "PLUGIN_INSTALL_JOURNAL_PATH": plugins / "install_journal.json",
        "PLUGIN_MARKETPLACE_ROOT": plugins / "marketplace",
    }.items():
        if hasattr(core_paths, name):
            monkeypatch.setattr(core_paths, name, value)
    yield


@pytest.fixture(autouse=True)
def _hand_back_leaked_substrate_locks() -> Generator[None, None, None]:
    """Return any cognition lock a fail-hard cascade deliberately kept.

    ``_ignite_episode_cascade`` exits with its substrate lock HELD on every
    exception path, by design: the brain is about to die and a briefly-held lock
    never outlives the process it condemns. In a test process nothing dies, so
    the lock leaks into the next test.

    Before physical pipeline locks that leak was invisible — one re-entrant
    lock, and the next test on the same thread simply re-entered it. With four
    physical locks the leak surfaces as a ``LockOrderViolation`` in whichever
    later test happens to ignite on a different pipeline, which is a true report and a
    completely useless place to read it from. Hand the locks back at the end of
    every test instead, so a fail-hard test cannot condemn its successors.
    """
    yield
    from core.cognition_lock import reset_for_tests

    # A worker thread may have died while owning an RLock; the pytest thread
    # cannot release that ownership. Pipeline dispatch has shut down by this
    # point, so replace the registry for the next isolated test.
    reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_pipeline_envelopes(tmp_path, monkeypatch) -> Generator[None, None, None]:
    """Keep durable physical pipeline envelopes out of the appliance's state.

    EVERY suite, not just pipeline suites: any test that dispatches a cascade
    writes a pipeline envelope, and an envelope is a RESTORATION HINT the next
    real boot acts on. Unisolated, a test's fabricated operator turn would be
    left on disk in READY and the appliance would faithfully re-file it onto the
    chat line at boot — the mind answering a fixture. The in-memory cache is
    dropped around the test for the same reason the file is moved: it is the
    other half of the same state.
    """
    from core import pipeline_state
    from core import paths as core_paths
    from pressure_loop import cognition_queue, loop, pipeline_dispatch
    from auto_loop.execute import job_control
    pipeline_dispatch.shutdown_for_tests(wait=True)
    loop._STAGED_INTERNAL.clear()
    monkeypatch.setattr(job_control, "jobs_root", lambda: tmp_path / "guardian" / "state" / "jobs")
    job_control.reset_for_tests()
    monkeypatch.setattr(core_paths, "GUARDIAN_STATE_ROOT", tmp_path / "guardian" / "state")
    # The episode journal is the live ordering authority (journal_window, causal parents), and
    # its policy path is repo-relative: every reader and writer resolves it through
    # pressure_loop.episode_journal._resolve_path against the repository. Resolve it under this
    # test's tmp root instead, so a test that archives an episode without patching the path can
    # never append to the operator's live journal (WO-08 entry gate: it did, on every run). The
    # policy default itself is untouched; a test that patches the path to its own file still wins.
    from pressure_loop import episode_journal

    monkeypatch.setattr(episode_journal, "_repo_root", lambda: tmp_path / "repo_relative_state")

    monkeypatch.setattr(pipeline_state, "pipelines_dir", lambda: tmp_path / "guardian" / "state" / "pipelines")
    monkeypatch.setattr(cognition_queue, "_queue_path", lambda: tmp_path / "guardian" / "state" / "cognition_queue.json")
    cognition_queue._reset_cache_for_tests()
    pipeline_state.reset_for_tests()
    yield
    pipeline_dispatch.shutdown_for_tests(wait=True)
    loop._STAGED_INTERNAL.clear()
    job_control.reset_for_tests()
    pipeline_state.reset_for_tests()
    cognition_queue._reset_cache_for_tests()


def _is_pytest_style(path: Path) -> bool:
    """True iff the module exposes a top-level pytest test (def test_* /
    class Test*). Plain-script tests have neither — they execute at
    import and sys.exit()."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        # async def test_* too: six anyio-marked mcp_platform modules were collect_ignore'd
        # as plain scripts and their 23 tests never ran (found 2026-09-15).
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            return True
        if isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            return True
    return False


# pytest reads `collect_ignore` (paths relative to this conftest's dir)
# before importing any test module — so the plain-script files are never
# imported, and their module-level sys.exit() can't abort collection.
collect_ignore = sorted(
    str(p.relative_to(_HERE)) for p in _HERE.rglob("test_*.py") if not _is_pytest_style(p)
)


@pytest.fixture(autouse=True)
def _disable_cascade_pacing_in_tests(monkeypatch, request):
    """Keep cascade handoff pauses out of integration tests (15s each adds up)."""
    if request.node.fspath and request.node.fspath.basename == "test_cognition_pacing.py":
        yield
        return
    monkeypatch.setenv("OMEGA_COGNITION_PACING", "0")
    yield


@pytest.fixture(autouse=True)
def _isolate_cross_lane_mail(tmp_path_factory, monkeypatch):
    """Every test starts with an empty, private cross-lane mail store.

    The import-time floor above only keeps mail out of the operator's live tree. One
    shared filename still lets mail that one test pins decide what another test claims,
    and the module caches the store in memory as well, so the file alone is not
    isolation (review 2026-09-16). Per test, with its own path and a cleared cache,
    before consumers in T6/T9 make every cascade test a mail participant.
    """
    import uuid

    from global_loop import intersections

    monkeypatch.setenv(
        "OMEGA_CROSS_LANE_MAIL_PATH",
        str(tmp_path_factory.getbasetemp() / "cross-lane-mail" / f"{uuid.uuid4().hex}.json"))
    intersections.reset_cache_for_tests()
    yield
    intersections.reset_cache_for_tests()


@pytest.fixture(autouse=True)
def _isolate_trusted_folders(tmp_path_factory, monkeypatch):
    """Every test starts with an empty, private trusted-folder grant store.

    The store lived at the operator's live guardian/state/trusted_folders.json, so a test's
    outcome depended on what the operator had granted: once E:\\ was trusted, every write
    the skill executor made into a test's own tmp_path was refused as outside the trusted
    folders (found 2026-09-15: five executor and motor-program tests). Per test rather than
    per session so a grant one test makes can never decide another's. The directory is only
    created if the test actually grants a folder.
    """
    import uuid

    import core.trusted_folders as trusted_folders

    monkeypatch.setattr(
        trusted_folders,
        "OMEGA_ROOT",
        tmp_path_factory.getbasetemp() / "trusted-folder-stores" / uuid.uuid4().hex,
    )
    yield


@pytest.fixture(autouse=True)
def _isolate_face_anchor_projection(tmp_path, monkeypatch):
    """Canonical face drains must never share a cache or touch live anchors."""
    from runtime.face import anchor_store

    monkeypatch.setattr(anchor_store, "GUARDIAN_STATE_ROOT", tmp_path / "guardian" / "state")
    monkeypatch.setattr(anchor_store, "_singleton", None)
    yield


@pytest.fixture(autouse=True)
def _reset_episode_sram_between_tests():
    """A test's held intake must never drain into another test's memory root."""
    from memory.episode_sram import reset

    reset()
    yield
    reset()


@pytest.fixture
def deepsleep_sram_drain(monkeypatch):
    """Run a persistence-focused unit test inside the real drain boundary.

    Production producers still enter through episode SRAM. Tests using this
    fixture are explicitly exercising code that normally runs while DeepSleep
    drains that bucket and therefore expects synchronous commit receipts.
    """
    from memory import episode_sram

    monkeypatch.setattr(episode_sram._drain_state, "active", True, raising=False)
    yield


@pytest.fixture(autouse=True)
def _reset_neuromod_tonic():
    """Clear module-level advisory neuromod tonic between tests."""
    try:
        from pressure_loop.pressure_field import reset_neuromod_tonic

        reset_neuromod_tonic()
    except Exception:
        pass
    yield
    try:
        from pressure_loop.pressure_field import reset_neuromod_tonic

        reset_neuromod_tonic()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_memory_salience_latch():
    """Clear the ambient neuromod salience latch between tests.

    ``cognition_chain._start_auto`` and salience tests set
    ``_CURRENT_MEMORY_SALIENCE`` globally; without reset, later tests that
    call ``StrengthIndex.register()`` sample the leaked latch.
    """
    import autonomic.symphony_neuromod as _snm

    _snm._CURRENT_MEMORY_SALIENCE = None
    yield
    _snm._CURRENT_MEMORY_SALIENCE = None


@pytest.fixture(autouse=True)
def _reset_gpu_lease():
    """Clear the process-global GPU-LLM lease between tests (runtime.gpu_lease).

    model_control prepare_*/release_* tests acquire/release the lease; an unbalanced acquire
    would otherwise leak a 'build holds the GPU' marker into later tests, making any
    RuntimeClient.generate() WAIT (OMEGA_GPU_WAIT_S) then degrade — a spurious failure far from
    the cause. Reset before AND after contains every acquire to the test that made it."""
    try:
        from runtime import gpu_lease

        gpu_lease._reset_for_tests()
    except Exception:
        pass
    yield
    try:
        from runtime import gpu_lease

        gpu_lease._reset_for_tests()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _reset_hot_buffer():
    """Clear the in-memory hot-buffer session singleton between tests.

    Chat ingress (operator_chat), system queries (system_messages 2026-07-10),
    and reply egress all append_turn() to the module-global session buffer;
    without reset, a later test's RESPONSE prompt picks up the leaked
    conversation block from render_recent_block()."""
    import memory.hot_buffer as _hb

    with _hb._lock:
        _hb._current = None
    yield
    with _hb._lock:
        _hb._current = None


@pytest.fixture(autouse=True)
def _reset_turn_scoped_state():
    """Start every test with no pending system question, reply, or turn stop flag.

    The system-message and chat-response stores are isolated once per session
    (``_isolate_telemetry_paths``), so without this a question one test leaves
    pending turns a later test's operator message into its answer, and a work
    halt one test raises for a common cycle id ("bubble-1") stops a later test's
    run for up to ``_WORK_HALT_TTL_S``.
    """
    import pressure_loop.operator_chat_responses as operator_chat_responses
    import pressure_loop.system_messages as system_messages

    system_messages.clear_messages()
    operator_chat_responses.clear_responses()
    operator_chat_responses._reset_cache_for_tests()
    yield


@pytest.fixture(autouse=True)
def _isolate_canonical_memory_for_every_suite(request, tmp_path, monkeypatch):
    """Catch memory writers outside the historically isolated core/memory suites.

    The memory suite owns a paths-only variant because it tests encoding itself.
    Every other pytest suite gets the full redirect, including boot/HUD/script
    tests whose production call paths can reach canonical writes indirectly.
    """
    path = Path(str(request.node.fspath)).resolve()
    try:
        suite = path.relative_to(_HERE.resolve()).parts[0]
    except (ValueError, IndexError):
        suite = ""
    if suite in {"core", "memory", "omega_loop", "pressure_loop"}:
        yield
        return

    from tests.helpers.memory_isolation import redirect_memory_to_tmp

    redirect_memory_to_tmp(tmp_path, monkeypatch)
    yield


@pytest.fixture(autouse=True, scope="session")
def _isolate_telemetry_paths():
    """Redirect telemetry + failure-log + data-logger + deepsleep state writes
    to a temp dir for the whole pytest session.

    The failure log (``guardian/logs/failures.jsonl``) is a *failure-only*
    record a remote monitor pulls to detect real outages — it is only
    trustworthy if it contains only real failures. Tests exercise failure
    paths constantly, so without this they fill the real log (and the
    firehose — see the 162MB ``guardian/telemetry/``) with synthetic
    failures. Writers read these module globals at call time, so reassigning
    them isolates every write without any production change.

    DeepSleep mastery/journal/identity/etc. stores are redirected the same way
    so per-episode consolidation tests do not pollute ``deepsleep_loop/state/``.
    """
    import shutil
    import tempfile
    from pathlib import Path

    from guardian import data_logger
    from guardian import terminal_console
    from guardian.telemetry import writer
    import pressure_loop.policy as policy
    import pressure_loop.reflection_window as reflection_window

    tmp = Path(tempfile.mkdtemp(prefix="omega-test-telemetry-"))
    deepsleep_root = tmp / "deepsleep_state"
    deepsleep_root.mkdir(parents=True, exist_ok=True)

    saved = (
        writer.TELEMETRY_DIR,
        writer.FAILURE_LOG,
        data_logger._LOG_DIR,
        terminal_console.TERMINAL_LOG_PATH,
    )
    writer.TELEMETRY_DIR = tmp / "telemetry"
    writer.FAILURE_LOG = tmp / "logs" / "failures.jsonl"
    writer.init_run_id("pytest-session")
    data_logger._LOG_DIR = tmp / "logs"
    terminal_console.TERMINAL_LOG_PATH = tmp / "telemetry" / "terminal.log"

    saved_policy = {
        "deepsleep_journal_path": policy.deepsleep_journal_path,
        "deepsleep_mastery_path": policy.deepsleep_mastery_path,
        "deepsleep_identity_path": policy.deepsleep_identity_path,
        "deepsleep_semantic_path": policy.deepsleep_semantic_path,
        "deepsleep_trajectory_path": policy.deepsleep_trajectory_path,
        "deepsleep_failure_path": policy.deepsleep_failure_path,
        "deepsleep_projection_path": policy.deepsleep_projection_path,
        "deepsleep_query_path": policy.deepsleep_query_path,
    }
    policy.deepsleep_journal_path = lambda policy=None: deepsleep_root / "journal.jsonl"
    policy.deepsleep_mastery_path = lambda policy=None: deepsleep_root / "mastery.json"
    policy.deepsleep_identity_path = lambda policy=None: deepsleep_root / "identity.json"
    policy.deepsleep_semantic_path = lambda policy=None: deepsleep_root / "semantic.json"
    policy.deepsleep_trajectory_path = lambda policy=None: deepsleep_root / "trajectory.json"
    policy.deepsleep_failure_path = lambda policy=None: deepsleep_root / "failure.json"
    policy.deepsleep_projection_path = lambda policy=None: deepsleep_root / "projection.json"
    policy.deepsleep_query_path = lambda policy=None: deepsleep_root / "open_queries.json"

    # Isolate the Omega reflection-window + trajectory state paths the same way.
    # ``EpisodeStore()`` with no explicit path loads ``reflection_window_path()``
    # (default ``guardian/state/reflection_window.json``) at __init__. A live
    # engine leaves a *locked, 10-episode* window there — gitignored, untracked —
    # which makes ``is_ready_for_reflection()`` True, so ``ignite_episode`` runs
    # the REAL Omega reason phase against the runtime (a blocking HTTP POST that
    # hangs the whole suite when no runtime is listening). Redirecting these to a
    # fresh temp dir means every test boots with an empty, non-ready window:
    # ``consume_window_ready()`` returns None and no runtime call is attempted.
    saved_rw = (
        reflection_window.reflection_window_path,
        reflection_window.current_trajectory_path,
        reflection_window.reflection_window_archive_dir,
        reflection_window.reflection_lease_path,
    )
    reflection_window.reflection_window_path = lambda: deepsleep_root / "reflection_window.json"
    reflection_window.current_trajectory_path = lambda: deepsleep_root / "current_trajectory.json"
    reflection_window.reflection_window_archive_dir = lambda: deepsleep_root / "reflection_windows"
    # The in-flight crash marker too: left live, a test that died mid-reflection would leave a
    # lease in the operator's guardian/state for the live brain to find at boot.
    reflection_window.reflection_lease_path = lambda: deepsleep_root / "reflection_lease.json"

    # Isolate the durable operator input queue box the same way — tests that
    # enqueue/clear operator inputs must never touch the real guardian/state.
    import pressure_loop.cognition_queue as cognition_queue

    saved_queue_path = cognition_queue._queue_path
    cognition_queue._queue_path = lambda: deepsleep_root / "cognition_queue.json"
    cognition_queue._reset_cache_for_tests()

    # And the durable physical pipeline envelopes. ``_isolate_pipeline_envelopes`` already
    # redirects them per test, but that fixture cannot cover the writer that
    # matters most: the fatal relay stamps FAILED from the physical pipeline worker
    # (pressure_loop/pipeline_dispatch.py and pressure_loop/loop.py), and a thread
    # that outlives the test function writes after
    # monkeypatch has already put ``pipelines_dir`` back. Observed live during the
    # pre-cutover integration pass: a full tests/core run left an old semantic
    # envelope behind (state FAILED, phase failed:RuntimeError) — a fixture's
    # restoration record sitting in the appliance's
    # own restoration surface. A SESSION-scoped redirect is the only one a
    # late thread cannot outlive; the per-test fixture keeps its finer
    # isolation on top of this floor.
    import core.pipeline_state as pipeline_state
    import core.paths as state_paths

    saved_guardian_state_root = state_paths.GUARDIAN_STATE_ROOT
    state_paths.GUARDIAN_STATE_ROOT = deepsleep_root

    # The encoding journal binds <live>/guardian/state/encoding_journal.jsonl at import. A
    # gate or api write outside the hermetic memory fixture journaled its obligations into the
    # operator's live journal, which boot recovery then acts on. Keep the <profile>/guardian/state
    # shape: the journal derives its profile from that location.
    import memory.encoding_journal as encoding_journal

    saved_journal_path = encoding_journal.JOURNAL_PATH
    encoding_journal.JOURNAL_PATH = tmp / "journal_profile" / "guardian" / "state" / "encoding_journal.jsonl"

    saved_pipelines_dir = pipeline_state.pipelines_dir
    pipeline_state.pipelines_dir = lambda: deepsleep_root / "pipelines"
    pipeline_state.reset_for_tests()

    # And the text app store (system queries to the operator) — episode-close
    # paths in cognition tests write it via _record_system_message.
    import pressure_loop.system_messages as system_messages

    saved_sm_path = system_messages._store_path
    system_messages._store_path = lambda: deepsleep_root / "system_messages.json"
    system_messages._reset_cache_for_tests()

    # And the operator-chat-response store (the system's async answer to a
    # queued operator chat turn) — episode-close capture writes it, so isolate
    # it the same way to keep the real guardian/state clean.
    import pressure_loop.operator_chat_responses as operator_chat_responses

    saved_ocr_path = operator_chat_responses._store_path
    operator_chat_responses._store_path = lambda: deepsleep_root / "operator_chat_responses.json"
    saved_ocr_lane_updates_path = operator_chat_responses._lane_updates_path
    operator_chat_responses._lane_updates_path = (
        lambda: deepsleep_root / "operator_chat_lane_updates.json"
    )
    operator_chat_responses._reset_cache_for_tests()

    # Isolate the dynamic-Y first-wake override (guardian/state/dynamic_y_lobes.json):
    # onboarding/seed_first_wake flips it ON in the REAL store, and without this
    # that flag-flip leaks into the flag-OFF static-subdomain tests.
    import memory.subdomain_flag as subdomain_flag

    saved_override = subdomain_flag._override_path
    subdomain_flag._override_path = lambda: deepsleep_root / "dynamic_y_lobes.json"
    subdomain_flag.reset_flag_cache()

    # Isolate the OPEN-QUESTIONS JOURNAL (pollution incident 2026-07-12): the
    # panel store above was redirected but the journal it syncs to was NOT —
    # append_system_query tests wrote 30+ fixture questions ("Pending two",
    # "Want a summary?") into the REAL guardian/state/omega_open_questions.jsonl,
    # and that journal rides EVERY fold prompt as the mind's pending-asks context.
    import pressure_loop.open_questions as open_questions

    saved_oq_path = open_questions.OPEN_QUESTIONS_PATH
    open_questions.OPEN_QUESTIONS_PATH = deepsleep_root / "omega_open_questions.jsonl"

    # And the proactive lifecycle ledger (same incident): record_ask fires inside
    # append_system_query, so ANY suite without a local ledger fixture wrote test
    # threads into the real guardian/state/proactive_lifecycle.jsonl.
    import pressure_loop.proactive_lifecycle as proactive_lifecycle

    saved_ledger_path = proactive_lifecycle._ledger_path
    proactive_lifecycle._ledger_path = lambda: deepsleep_root / "proactive_lifecycle.jsonl"
    proactive_lifecycle._reset_cache_for_tests()

    # And the proactive chat store: a boot-greeting test appended a pending
    # greeting to the real guardian/state/proactive_chat_messages.json.
    import pressure_loop.proactive_chat_messages as proactive_chat_messages

    saved_pcm_path = proactive_chat_messages._store_path
    proactive_chat_messages._store_path = lambda: deepsleep_root / "proactive_chat_messages.json"
    proactive_chat_messages._reset_cache_for_tests()

    # And the HUD chat-history ring (same incident, third store): build-notice
    # tests persisted pytest-tmp-path notices into the REAL ring, and the boot
    # rehydration replayed them into the operator's chat panel.
    import hud.chat_history as hud_chat_history

    saved_history_path = hud_chat_history._HISTORY_PATH
    hud_chat_history._HISTORY_PATH = deepsleep_root / "hud_chat_history.json"

    # THE RUNTIME SWEEP FLOOR (operator 2026-08-11). Every store above keeps a
    # test's WRITES off the live appliance; this keeps a test's KILLS off it.
    #
    # Two teardown paths shell out to omega-runtime's real lifecycle script,
    # which force-kills every gateway and worker on the box by port and command
    # line — the operator's LIVE appliance, mid-session:
    #   core.boot_lifecycle._run_runtime_teardown  -> scripts/stop_runtime.sh
    #   runtime.supervisor.shutdown_runtime_process -> _default_stop_all -> same
    # Each is gated ("boot_lock in started", "runtime_process in started"), and
    # both gates leak the moment one test records those components — which
    # ``tests/core/test_boot_lifecycle.py::test_shutdown_reverses_boot_order``
    # does, by recording every BOOT_COMPONENT in a loop. Measured 2026-08-11:
    # that ONE test launched the real stop_runtime.sh TWICE. It is why live
    # gateways kept dying with rc=0 during pytest runs, and why the signature in
    # guardian/logs/runtime.gateway.log was the sweep's own "sweep complete: N
    # process(es) cleared".
    #
    # A gate cannot be the floor: it is per-call-site and a new caller silently
    # opts out of it. So the floor is the RESOLVER — inside pytest, "no explicit
    # runtime root" resolves to a sandbox instead of the operator's bundle, and
    # the sweep scripts are simply not there to run. Session-scoped for the same
    # reason as the pipeline envelopes above: a pipeline worker that outlives
    # its test would outlive a per-test redirect too.
    #
    # An EXPLICIT OMEGA_RUNTIME_ROOT still wins, so the supervisor suite's own
    # tmp_path bundles behave exactly as before — this only changes what the
    # DEFAULT means, and only under test.
    import os

    import core.boot_lifecycle as boot_lifecycle
    import runtime.supervisor as supervisor

    sweep_sandbox = tmp / "omega-runtime-sandbox"
    (sweep_sandbox / "scripts").mkdir(parents=True, exist_ok=True)

    saved_runtime_root = supervisor.runtime_root
    saved_teardown_script = getattr(boot_lifecycle, "_runtime_teardown_script", None)

    def _sandboxed_runtime_root() -> Path:
        override = (os.environ.get("OMEGA_RUNTIME_ROOT") or "").strip()
        return Path(override) if override else sweep_sandbox

    supervisor.runtime_root = _sandboxed_runtime_root
    if saved_teardown_script is not None:
        boot_lifecycle._runtime_teardown_script = (
            lambda: sweep_sandbox / "scripts" / "stop_runtime.ps1"
        )

    # And the boot manifest, the other live file a teardown test touches.
    # ``record_started`` writes it and ``shutdown_system`` UNLINKS it, both
    # unconditionally, so any test that drives either one rewrites the
    # appliance's own boot record — leaving it naming a dead pytest pid and a
    # component list from a boot that never happened. Nothing in production
    # reads it (it is write-only diagnostics), which is exactly why it went
    # unnoticed: it cannot break a boot, it can only lie to the operator
    # reading one. tests/core/test_boot_lifecycle.py already redirects it per
    # test; this is the floor under every other suite.
    saved_manifest_path = boot_lifecycle.BOOT_MANIFEST_PATH
    boot_lifecycle.BOOT_MANIFEST_PATH = deepsleep_root / "boot.manifest.json"

    # Import-time live paths the floor above did not cover. A full inventory run on 2026-09-15
    # changed each of these in the operator's tree: the memory queues, the routing edge-case
    # catalog, the pathway performance projection, the signal ledger, face thumbnails
    # (memory/700_vision/images/Noah.png), dev project records (memory/1100_dev/projects/
    # build-the-app, do-the-work: fake projects Omega's dev-memory recall would surface) and
    # the source-validity projection. Each is read at call time, so reassigning the module
    # global isolates every writer.
    import core.paths as core_paths_floor
    import memory.dev_project_store as dev_project_store
    import memory.pathway_performance as pathway_performance
    import memory.queues as memory_queues
    import memory.routing.edge_case_catalog as edge_case_catalog
    import memory.source_validity as source_validity
    import pressure_loop.signal_ledger as signal_ledger

    live_profile = tmp / "live_path_floor"
    saved_live_paths = (
        memory_queues._QUEUES_DIR,
        memory_queues.REFLECTION_QUEUE_PATH,
        memory_queues.DEEPSLEEP_QUEUE_PATH,
        edge_case_catalog._DEFAULT_PATH,
        pathway_performance.PATHWAY_PROJECTION_PATH,
        signal_ledger._LEDGER_PATH,
        core_paths_floor.FACE_THUMBNAILS_ROOT,
        dev_project_store.OMEGA_ROOT,
        source_validity.OMEGA_ROOT,
    )
    memory_queues._QUEUES_DIR = live_profile / "guardian" / "state" / "queues"
    memory_queues.REFLECTION_QUEUE_PATH = memory_queues._QUEUES_DIR / "reflection_eligible.jsonl"
    memory_queues.DEEPSLEEP_QUEUE_PATH = memory_queues._QUEUES_DIR / "deepsleep_candidates.jsonl"
    edge_case_catalog._DEFAULT_PATH = live_profile / "guardian" / "state" / "routing_edge_catalog.jsonl"
    pathway_performance.PATHWAY_PROJECTION_PATH = live_profile / "deepsleep_loop" / "state" / "pathway_performance.json"
    signal_ledger._LEDGER_PATH = live_profile / "guardian" / "logs" / "signals.jsonl"
    # runtime.face.recognition copies this at import: patch the source for later importers and
    # the copy if collection already imported it.
    core_paths_floor.FACE_THUMBNAILS_ROOT = live_profile / "memory" / "700_vision" / "images"
    face_recognition_module = sys.modules.get("runtime.face.recognition")
    saved_face_copy = getattr(face_recognition_module, "FACE_THUMBNAILS_ROOT", None)
    if face_recognition_module is not None:
        face_recognition_module.FACE_THUMBNAILS_ROOT = core_paths_floor.FACE_THUMBNAILS_ROOT
    dev_project_store.OMEGA_ROOT = live_profile
    source_validity.OMEGA_ROOT = live_profile

    # boot.py publishes setup progress to <its OMEGA_ROOT>/guardian/state/boot_status.json; a
    # boot-driving test rewrote the live file with a pytest pid. Only a write aimed at the live
    # product root is redirected, so tests that pass their own root still get exactly it.
    import core.boot_status as boot_status_module

    saved_write_boot_status = boot_status_module.write_boot_status
    live_product_root = Path(core_paths_floor.OMEGA_ROOT).resolve()

    def _floor_write_boot_status(product_root=None, **kwargs):
        if product_root is None or Path(product_root).resolve() == live_product_root:
            product_root = live_profile
        return saved_write_boot_status(product_root, **kwargs)

    boot_status_module.write_boot_status = _floor_write_boot_status

    try:
        yield
    finally:
        supervisor.runtime_root = saved_runtime_root
        if saved_teardown_script is not None:
            boot_lifecycle._runtime_teardown_script = saved_teardown_script
        boot_lifecycle.BOOT_MANIFEST_PATH = saved_manifest_path
        (
            memory_queues._QUEUES_DIR,
            memory_queues.REFLECTION_QUEUE_PATH,
            memory_queues.DEEPSLEEP_QUEUE_PATH,
            edge_case_catalog._DEFAULT_PATH,
            pathway_performance.PATHWAY_PROJECTION_PATH,
            signal_ledger._LEDGER_PATH,
            core_paths_floor.FACE_THUMBNAILS_ROOT,
            dev_project_store.OMEGA_ROOT,
            source_validity.OMEGA_ROOT,
        ) = saved_live_paths
        if face_recognition_module is not None:
            face_recognition_module.FACE_THUMBNAILS_ROOT = saved_face_copy
        boot_status_module.write_boot_status = saved_write_boot_status
        (
            writer.TELEMETRY_DIR,
            writer.FAILURE_LOG,
            data_logger._LOG_DIR,
            terminal_console.TERMINAL_LOG_PATH,
        ) = saved
        for name, fn in saved_policy.items():
            setattr(policy, name, fn)
        (
            reflection_window.reflection_window_path,
            reflection_window.current_trajectory_path,
            reflection_window.reflection_window_archive_dir,
            reflection_window.reflection_lease_path,
        ) = saved_rw
        cognition_queue._queue_path = saved_queue_path
        cognition_queue._reset_cache_for_tests()
        pipeline_state.pipelines_dir = saved_pipelines_dir
        state_paths.GUARDIAN_STATE_ROOT = saved_guardian_state_root
        encoding_journal.JOURNAL_PATH = saved_journal_path
        pipeline_state.reset_for_tests()
        system_messages._store_path = saved_sm_path
        system_messages._reset_cache_for_tests()
        operator_chat_responses._store_path = saved_ocr_path
        operator_chat_responses._lane_updates_path = saved_ocr_lane_updates_path
        operator_chat_responses._reset_cache_for_tests()
        subdomain_flag._override_path = saved_override
        subdomain_flag.reset_flag_cache()
        open_questions.OPEN_QUESTIONS_PATH = saved_oq_path
        proactive_lifecycle._ledger_path = saved_ledger_path
        proactive_lifecycle._reset_cache_for_tests()
        proactive_chat_messages._store_path = saved_pcm_path
        proactive_chat_messages._reset_cache_for_tests()
        hud_chat_history._HISTORY_PATH = saved_history_path
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _reset_operator_chat_response_rail():
    """Keep terminal reply rows bounded and independent between tests.

    The response rail is durable by design, but pytest exercises many
    delivery paths without a HUD client to claim each row. Reusing one session
    file therefore turns a finite test fixture into an artificial capacity
    outage and makes a later test observe ``rail full`` instead of its own
    result. Tests that need persistence still exercise it within one test;
    this only removes cross-test terminal display state.

    The clear runs at setup only. A teardown clear would write through while
    the test's own monkeypatches are still installed (a test that makes
    ``os.replace`` fail would fail the fixture's write too), and the store
    fails hard on a failed write.
    """
    from pressure_loop import operator_chat_responses

    operator_chat_responses.clear_responses()
    yield


@pytest.fixture(autouse=True)
def _reset_dynamic_y_flag_each_test():
    """Keep the dynamic-Y first-wake flag dark at the start of every test.

    The flag is global mutable state: an override file (under the *session*-scoped
    isolated state dir) plus a module-level cache. ``seed_first_wake`` /
    ``set_dynamic_y_lobes`` flip it ON inside a test body, and because the path
    isolation above is session-scoped, that ON state would persist for the rest
    of the session and leak into flag-OFF tests (e.g. write-strength routing to a
    static lobe). Clearing the override file + cache before AND after each test
    contains every flip to the test that made it. Tests that exercise the
    flag-ON path pass ``dynamic_y_lobes`` to the allocator directly and are
    unaffected by the global flag, so this never weakens their coverage.
    """
    import memory.subdomain_flag as subdomain_flag

    def _dark() -> None:
        try:
            path = subdomain_flag._override_path()
            if path.is_file():
                path.unlink()
        except Exception:
            pass
        subdomain_flag.reset_flag_cache()

    _dark()
    yield
    _dark()


@pytest.fixture(autouse=True)
def _clear_staged_reflection_windows():
    """Phase 4 cut 10: the staged reflection window is DURABLE PRESSURE.

    ``_isolate_telemetry_paths`` redirects the reflection-window path once for
    the whole SESSION, so the staging file beside it is shared by every test that
    does not pass its own path. A test that closes ten episodes leaves a staged
    window there — and the next test's ``EpisodeStore()`` would boot with a
    reflection owed, ``get_locked_window()`` would answer with a stranger's
    window, and ``gather_contributors`` would synthesize a
    ``window_reflection_ready`` nobody asked for. Cleared on both sides so an
    aborted test cannot poison the next one either.
    """
    from pressure_loop.reflection_window import staged_window_path

    def _clear() -> None:
        try:
            path = staged_window_path()
            if path.is_file():
                path.unlink()
        except Exception:
            pass

    _clear()
    yield
    _clear()


def close_episode_with_window_rules(
    store,
    episode=None,
    *,
    cascade_status: str = "completed",
) -> None:
    """Close while staging episode 10 inline when slice5 omega is enabled.

    Phase 4 cut 13: the reflection-window append moved out of
    ``EpisodeStore.close`` and into the DeepSleep terminus's serialized commit
    region, which ``close`` reaches through ``before_finalize``. This helper
    exists so tests close episodes the way production does, so it runs that seam
    too — otherwise every window assertion in the suite would be checking a
    close shape the appliance never performs.
    """
    ep = episode or store.current()
    if ep is not None and store.is_tenth_episode_window_reflection():
        store.stage_episode_for_window_reflection(ep, cascade_status=cascade_status)
    store.close(
        cascade_status=cascade_status,
        before_finalize=lambda closing, body: store.commit_reflection_window_append(
            closing, body
        ),
    )


def pytest_sessionstart(session):  # noqa: ARG001 - pytest hook signature
    """Ensure repo-root packages win over `tests/` shadowing.

    Some tests live under `tests/hud/` which can shadow the top-level
    `hud/` package depending on sys.path ordering. Pin repo root at
    the front once per session.
    """
    root_str = str(_HERE.parent)
    if sys.path and sys.path[0] == root_str:
        return
    if root_str in sys.path:
        sys.path.remove(root_str)
    sys.path.insert(0, root_str)


@pytest.fixture(autouse=True)
def _cognition_halt_clear():
    """The teardown halt flag is process-global and one-way within a boot.

    Any test that drives ``shutdown_system`` (or ``shutdown_lanes``) sets it,
    and it would then stay set for every test that ran afterwards in the same
    process — silently collapsing the embed retry envelope those tests depend
    on. Cleared on both sides so neither order can leak.
    """
    from core.cognition_halt import reset_for_tests

    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture(autouse=True)
def _idle_wake_governor_off(monkeypatch):
    """The idle-wake governor (pressure_loop.idle_wake, operator
    2026-07-08) suppresses autonomous ignition while the mind rests —
    correct in production, wrong for the always-awake world the
    evaluate() test corpus assumes. Off by default here; the governor's
    own tests re-enable it explicitly."""
    monkeypatch.setenv("OMEGA_IDLE_WAKE_ENABLED", "0")
    # A self-directed cascade paces itself between phases in production (operator 2026-09-22) so a
    # chain of Omega's own episodes cannot hammer the one cognition route. Real seconds of sleep
    # per phase would add hours to this suite and measure nothing, so the pace is zero here; its
    # own tests set it explicitly.
    monkeypatch.setenv("OMEGA_AUTONOMOUS_PHASE_PAUSE_S", "0")


@pytest.fixture(autouse=True)
def _cloud_brain_route_off(monkeypatch):
    """The cloud BRAIN route (runtime/brain_cloud_config, operator
    2026-07-11) reads a LIVE gitignored operator config holding a real
    API key. A test that builds a real RuntimeClient must never
    silently call a paid frontier endpoint — force the route off for
    every test; the route's own tests re-enable it explicitly by
    repointing _config_path and clearing this env (see
    tests/core/test_brain_cloud.py)."""
    monkeypatch.setenv("OMEGA_MIND_BACKEND", "local")

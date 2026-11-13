# tests — Omega 3.0 by-system test tree with a dual runner model

**What it is** — `tests/` is organized **by system under test**, not as a
flat pile: each subsystem gets its own `tests/<system>/` bucket. The corpus
follows Omega's deliberate **plain-script convention** — most files *are*
the test (module-level checks that run top-to-bottom and end with
`sys.exit(0 if FAIL == 0 else 1)`), invoked as `.\.venv-brain\Scripts\python.exe tests/<system>/test_x.py`.
A subset are pytest-style (`def test_*` / `class Test*`). Both runners
coexist by design: `tests/conftest.py` scopes pytest so it never imports
(and chokes on) the plain-script files, while the plain-script suite runs
directly or via `tests/run_full_matrix.py`.

## Layout

By-system buckets under `tests/`:

| Bucket | Status | Covers |
| --- | --- | --- |
| [`pressure_loop/`](pressure_loop/) | **Live** | Tick driver, contributor gather/evaluate, EpisodeStore, policy loaders. |
| global_loop coverage | **Live** | Now under [`core/`](core/) — test_intent_decision_shape.py, test_global_b_*.py. |
| [`meta_loop/`](meta_loop/) | **Live** | Meta A action pack + Meta B reflection pack. |
| [`auto_loop/`](auto_loop/) | **Live** | AUTO execution, model dispatch, promptpack, recall, fold. |
| [`omega_loop/`](omega_loop/) | **Live** | Omega 5W1H reflection, prompt, validation. |
| [`deepsleep_loop/`](deepsleep_loop/) | **Live** | DeepSleep consolidation, closure, reseed, memory promotion (7a). |
| [`core/`](core/) | **Live** | Shared spine: paths, shared state, boot, schemas, and policies. |
| [`memory/`](memory/) | **Live** | Deterministic recall, gate routing, coord-index, encoding, strength, retina, and DME. |
| [`guardian/`](guardian/) | **Live** | External-ingress decisions, logging, ledgers, response classes, and state persistence. |
| [`runtime/`](runtime/) | **Live** | HTTP/provider adapters, sockets, local modalities, face inference, and scene vision. |
| [`knowledge_extractor/`](knowledge_extractor/) | **Live** | KnowledgeArtifact pipeline (ingest → derive → store). |
| [`autonomic/`](autonomic/) | **Live** | Neuromodulator modules (dopamine, plasticity, strength, stress, temporal). |
| [`cognition/`](cognition/) | **Live** | Topic resolution, learn-focus convergence. |
| [`config/`](config/) | **Live** | Schema/governance config validation. |
| [`meta/`](meta/) | **Live** | LEARN-query de-slug canonicalization. |
| [`pressure/`](pressure/) | **Live** | Ingress chat queue, idle-wake, fast-reply, dev rollback injection. |
| [`schema/`](schema/) | **Live** | Execution-skill frontmatter, operator-file containment, web-fetch opensearch checks. |
| [`scripts/`](scripts/) | **Live** | agent_bus, boot_cli, day_zero_reset checkpoints. |
| [`brain/`](brain/) | **Legacy** | Old brain sequencer; logic moved to loop packages. Tests may be stale. |
| [`metacognition/`](metacognition/) | **Legacy** | Old metacognition; logic moved to `meta_loop/`, `omega_loop/`, `deepsleep_loop/`. |
| [`hud/`](hud/) | **Separate-active** | In-process HUD foundation, settings, uploads, vision, and chat surface. |
| [`browser_agent/`](browser_agent/) | **Separate-active** | Extension contract, browser automation. |
| [`e2e/`](e2e/) | **Separate-active** | Cross-system HUD end-to-end Playwright specs (smoke, markdown/upload XSS, page-object). |
| [`regression/`](regression/) | **Mixed** | Hardening series — `SLICE-N`, `REPAIR-N`, `Fn-` pins. |

Shared harness files stay at `tests/` root (not subsystem tests):

| File | Role |
| --- | --- |
| [`run_full_matrix.py`](run_full_matrix.py) | Aggregate gate — runs every `test_*.py` / `stress_*.py` as isolated subprocesses. |
| [`monitor_stress_runs.py`](monitor_stress_runs.py) | Polls `.omega_data/stress_runs/*/matrix.json` for failures (`--watch 60`). |
| [`runtime_probe.py`](runtime_probe.py) | Runtime capability probe. |
| [`test_boot_gate.py`](test_boot_gate.py) | Root boot-gate test. |
| [`_audit6_victim.py`](_audit6_victim.py) | Fixture target used by audit tests (not collected — leading underscore). |
| [`conftest.py`](conftest.py) | pytest scoping (see Conventions). |

`pytest.ini` lives at the **repo root** (`pytest.ini`):
`testpaths = tests`, coverage over `core pressure_loop global_loop meta_loop
auto_loop omega_loop deepsleep_loop memory runtime guardian knowledge_extractor
autonomic hud`, 300s timeout.

## How to run

**Plain-script (the default convention)** — run any test as a standalone
process; exit code is the verdict:

```powershell
.\.venv-brain\Scripts\python.exe tests/pressure_loop/test_cognitive_churn_reaches_pressure.py
.\.venv-brain\Scripts\python.exe tests/deepsleep_loop/test_memory_consolidation.py
.\.venv-brain\Scripts\python.exe tests/regression/test_repair9_hygiene_sweep.py
```

**Full suite (aggregate gate)** — `run_full_matrix.py` discovers every
`test_*.py` + `stress_*.py` recursively, runs each in a **clean
interpreter subprocess** (no leaked imports / process-global singletons),
and reports pass/fail by **process exit code** (in-script PASS/FAIL
counters are informational only):

```powershell
.\.venv-brain\Scripts\python.exe tests/run_full_matrix.py
.\.venv-brain\Scripts\python.exe tests/run_full_matrix.py --list
.\.venv-brain\Scripts\python.exe tests/run_full_matrix.py --json matrix_report.json
```

Each script runs against the repo tree, so a plain script must redirect every write it
makes (memory root, queue, uploads, face anchors, telemetry) to a scratch directory
before exercising the code under test.

**pytest** — from the repo root, `pytest` collects only genuine
pytest-style tests (about **12,300** on 2026-09-15; `pytest --co -q` works
because the plain-script files are ignored at collection time). On a 16 GB
machine run it one directory at a time: a single process over the whole tree
runs out of memory. Every test gets a private, empty trusted-folder store and
a session floor of redirected Guardian, memory-queue and journal paths
(`tests/conftest.py`), so a run never writes the operator's live tree:

```powershell
.\.venv-brain\Scripts\python.exe -m pytest                 # full collection, per pytest.ini
.\.venv-brain\Scripts\python.exe -m pytest --co -q         # collection-only sanity check
.\.venv-brain\Scripts\python.exe -m pytest tests/brain     # one subsystem
```

**Depth-independent ROOT resolver** — every test (at any nesting depth)
resolves the repo root the same way, so files relocate between buckets
without path edits:

```python
ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
```

## Conventions

- **Plain-script style** — module-level checks, a `check(label, ok, ...)`
  helper incrementing `PASS` / `FAIL`, ending with
  `raise SystemExit(...)` / `sys.exit(0 if FAIL == 0 else 1)`. This is the
  majority and the project default.
- **pytest style** — module-level `def test_*` functions or `class Test*`.
  `conftest.py` AST-parses every `test_*.py` and adds any file *without* a
  top-level `def test_*` / `class Test*` to `collect_ignore`. Result:
  pytest only imports real pytest tests; plain-script files never get
  imported during collection, so their module-level `sys.exit()` can't
  abort the run with `INTERNALERROR: SystemExit`. (A file with both a
  module-level `def test_*` *and* a `__main__` block is treated as
  pytest-style by the AST rule and is collected.)
- **Where new tests go** — into the `tests/<system>/` bucket for the
  system under test (e.g. a memory-recall test → `tests/memory/`, a
  fixed-regression pin → `tests/regression/`). Use the ROOT resolver
  snippet above; do not hardcode relative paths to the repo root.

## Gotchas / doctrine

- **Both runners are load-bearing — do not delete `tests/conftest.py` or
  the repo-root `pytest.ini`.** Removing conftest re-breaks `pytest --co`
  (plain-script `sys.exit()` aborts collection); removing pytest.ini drops
  coverage scoping and `testpaths`.
- **Subprocess isolation is intentional.** `run_full_matrix.py` runs each
  file in its own interpreter precisely because the corpus relies on
  process-global singletons; do not "optimize" it into in-process imports.
- **Outputs** — tests write to `.omega_data/data/` (cycle_events,
  failure_audit, tool_reliability_events, queues, sandbox runs, etc.).
  Most isolate persistence via tmp dirs restored on exit; the directory is
  safe to wipe between runs and regenerates.
- Build-log attribution for the regression series lives under
  [`docs/_archive/build_log/`](../docs/_archive/build_log/).

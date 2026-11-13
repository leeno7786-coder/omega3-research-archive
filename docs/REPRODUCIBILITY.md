# Omega 3.0 source archive — reproducibility notes

This is a **source-only research archive**. It intentionally omits model weights, local runtime binaries, virtual environments, memory profiles, private connector credentials, test logs and host-specific installation assets.

## What was independently checked on the release candidate

**Total focused tests passed: 295**, plus eight skips; two distinct known failure cases were kept visible. The original test files were not all run together in one complete suite.

The release-preparation copy was assembled from the F: worktree rather than the older GitHub main. It includes the latest locally present browser actions, cross-lane mail reconciliation, memory/projection updates and the untracked LM Studio model-template module.

Focused verification, 2026-11-13:

| Check | Status | Interpretation |
|---|---|---|
| Python syntax on all staged files | **1,991 parsed; 0 errors** | Static syntax only |
| Shared-state snapshot regression | **2 passed** | Deep-copy correctness on simple structures |
| Browser agent tool/controller/extraction | **56 passed** | Stubbed tool/schema/controller behavior |
| Specialist handoffs | **53 passed before broader e2e failure** | Handoff core regression coverage |
| Memory/projection/model-template suite | **81 passed, 1 skipped** | Pinned Ettin model provided externally from F: |
| MCP signature/trust/package suite | **79 passed, 7 skipped** | Static/contract behavior |
| Proactive voice, TTS and chat socket Python | **73 passed** | Current fake sidecar now reports the selected backend |
| JavaScript voice/UI | **4 test files passed** | Includes speech text and live voice checks |
| Historical cross-lane e2e scenario | **1 failed** | Scenario scripts an older Chat fold contract; requires reconciliation |
| Plugin execution worker process test | **1 failed after 1 passed** | Spawn/containment failure in remote execution; not verified |
| Sensitive-token pattern scan | **No high-confidence keys in current sanitized snapshot** | Not a complete credential/commit-history audit |
| Real voice/LM Studio/GPU/NPU run | **Not attempted** | Requires omitted local runtime/model assets |
| Whole test suite | **Not yet run** | No full-release pass claimed |

**Important limitation:** Focused tests used the original Omega project's portable Python environment to supply dependencies, with the staged source root forced to the front of Python's module search path. This is not a test of the archive on an independently provisioned machine.

## Why a source archive is not a portable binary distribution

Omega's original design ran from a self-contained SSD and expected a specific collection of local models, Python interpreters, CPU/GPU/NPU libraries and installed applications. The archive removes these packages for copyright, size, privacy and reproducibility reasons. Some tests reference them explicitly.

A reader should not mistake `install.ps1`, `scripts/build_portable.ps1`, or historical README instructions for a one-click setup of this sanitized source snapshot.

## Tests and build prerequisites

- Python **3.13**, not arbitrary system Python.
- Dependencies from `requirements.txt` and `requirements-dev.txt` plus the appropriate native model and runtime stack for integration work.
- For source checks: `python -m compileall -q core pressure_loop global_loop meta_loop auto_loop omega_loop deepsleep_loop memory runtime guardian autonomic browser_agent mcp_platform plugin_platform`.
- For focused tests once dependencies are installed: `python -m pytest -q -o addopts= tests/browser_agent/test_agent_loop.py tests/browser_agent/test_browser_controller.py tests/browser_agent/test_extract.py`.

## Tests that describe a historical architecture

The repository includes dated integration tests from prior design epochs. Omega changed from earlier general-purpose fold-driven routing to dedicated semantic lanes, explicit specialist handoff ownership and private freeform Chat self-reflection. Older scenarios may still expect a Chat fold to emit pressure to another lane, even though the later design routes that work through AUTO and durable handoff.

These should be classified and updated or archived before claiming a complete green suite. Do not rewrite assertions to produce artificial passes.

## Reproducing benchmark claims

Benchmark results require published data definition, model/version, hardware, sampling, run count and scripts. Prior claims of near-perfect determinism or millisecond recall are historical measurements, not universal current benchmarks. This archive deliberately excludes real personal memory corpus snapshots; replace them with synthetic data to reproduce evaluations.

All performance results should be treated as architecture- and hardware-specific.

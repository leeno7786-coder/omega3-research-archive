# docs/contracts — Contract Clauses and Schema References

> **Authority boundary:** `AGENTS.md`, the live loop packages, and the live
> schemas define current behavior. This directory contains binding clauses and
> historical design material; it is not a current architecture map.

Source and package READMEs cite selected numbered clauses for traceability. A
clause may still be useful without making the old package names in that clause
current. In particular, the live ownership is `pressure_loop` → `global_loop`
→ `meta_loop` → `auto_loop` → `omega_loop` → `deepsleep_loop`, with the episode
as carrier. There is no current `brain/`, `autonomous/`, or `metacognition/`
package owner.

**See Also:**

- [`../README.md`](../README.md) — documentation root index
- [`../architecture/README.md`](../architecture/README.md) — current maps and historical boundary
- [AGENTS.md](../../AGENTS.md) — governing architecture and engineering contract
- [`../_archive/build_log/README.md`](../_archive/build_log/README.md) — historical build evidence

## Numbered Clauses

These files are retained because source and design material cite them. Read
them as clause references and cross-check their ownership language against the
live source; do not use their old loop names as current architecture:

| # | File | Current reading boundary |
|---|---|---|
| 0 | [`00_cognitive_doctrine.md`](00_cognitive_doctrine.md) | Doctrine and resolution-pressure principles; its layered-stack prose is historical vocabulary. |
| 1 | [`01_schema_contract.md`](01_schema_contract.md) | Schema intent and traceability; current shapes live in `schema/` and the phase packages. |
| 2 | [`02_deepsleep_governance.md`](02_deepsleep_governance.md) | DeepSleep governance reference; verify configuration and ownership against `deepsleep_loop/`. |
| 3 | [`03_curiosity_development.md`](03_curiosity_development.md) | Curiosity and development principles; current pressure/admission is in `pressure_loop/`. |
| 4 | [`04_scoring_contract.md`](04_scoring_contract.md) | Deterministic scoring and failure-flow reference used by current code. |

## Historical Topic Documents

The following files are useful historical evidence, not current contracts or
ownership maps:

- [`06_response_system.md`](06_response_system.md) — explicitly historical;
  current response execution is in `cognition_chain.py`, `auto_loop/execute/`,
  `organ/promptorgan/`, and `runtime/tts.py` / `runtime/stt.py`.
- [`proactive_cognition.md`](proactive_cognition.md) — historical proactive
  substrate design; it names removed receiver and scheduler packages.
- [`runtime_and_models.md`](runtime_and_models.md) — explicitly historical
  runtime migration record; it names deleted `brain/*` and external paths.
- [`tool_lifecycle_pruning.md`](tool_lifecycle_pruning.md) — historical tool
  lifecycle record; verify every path before following it.
- [`DOCTRINE_V2.md`](DOCTRINE_V2.md) — **PHANTOM and non-authoritative**. Do
  not build to it or cite it as the current contract.

No root `The contract..txt` file exists in the current tree. References to that
old bedrock path are historical and must not be used to locate authority.

## Editing Rules

- Treat a numbered-file edit as a contract change: inspect source citations first.
- Put design narratives in [`../research/`](../research/) and dated work records
  in [`../_archive/build_log/`](../_archive/build_log/), not in a current map.
- Never use a historical response/runtime document to reintroduce a retired
  owner, path, model route, or HUD surface.

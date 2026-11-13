# Knowledge Extractor

The **Knowledge Extractor** — raw content to structured `KnowledgeArtifact` (deterministic). Converts web results, files, PDFs, video transcripts, and tool outputs into domain-tagged artifacts for downstream consumption by DeepSleep consolidation.

## Scope

`knowledge_extractor/` is a **pure sequencer** with no model inference. It deterministically parses raw content into structured fields, then persists the artifact. It runs in two places:
1. **AUTO execution path:** `auto_loop/execute/run.py::_maybe_extract_knowledge` calls `knowledge_extractor.extract()` after a successful LEARN tool execution (web_fetch, etc.) when `config/knowledge.yaml` `learn_integration.wired` is true.
2. **DeepSleep consolidation path:** `deepsleep_loop/consolidate/run.py::build_journal_entry` reads matching artifacts via `knowledge_extractor.consume.knowledge_artifact_for_episode` when `consume_on_consolidate` is enabled.

## Architecture

### `pipeline.py` — Pure Sequencer
The main entry point (`knowledge_extractor/pipeline.py`).

**Key function:**
- `extract(source: Source) -> KnowledgeArtifact` — ingest → derive → store

### `consume.py` — Artifact Read-Back
Reads persisted `KnowledgeArtifact` rows from the JSONL store.

**Key function:**
- `knowledge_artifact_for_episode(artifact_store_path, episode_id)` — returns the most recent artifact matching the episode (used by DeepSleep consolidation).

### Phase Sub-Package Pattern
Each phase is a separate sub-package under `knowledge_extractor/` with the standard pattern:
- `<phase>/run.py` — Contains the `run()` function for that phase
- `<phase>/__init__.py` — Re-exports the main exports (from .run import ...)

## Phase Chain

### 1. `ingest/` — Source Normalization
Normalizes raw input into a common dict (`knowledge_extractor/ingest/run.py`).

**Input:** `Source` (kind, content, title, source_ref, meta)
**Output:** `dict` with normalized fields (text, source_ref, source_kind, title, meta)
**Supported kinds:** `web_result`, `file`, `pdf`, `video_transcript`, `tool_output`

### 2. `derive/` — Field Extraction
Deterministically derives structured fields from normalized content (`knowledge_extractor/derive/run.py`).

**Input:** normalized dict
**Output:** `dict` with:
- `domain` — inferred domain string
- `concepts` — list of concept strings
- `facts` — list of fact strings
- `relationships` — list of relationship dicts
- `mastery_tags` — list of mastery tag strings
- `confidence` — float [0,1]
- `artifact_id` — stable identifier

**Behavior:** deterministic heuristics (keyword mapping, structural hints); no LLM inference.

### 3. `store/` — Artifact Persistence
Persists the `KnowledgeArtifact` (`knowledge_extractor/store/run.py`).

**Input:** `KnowledgeArtifact`
**Output:** None (side effect: append to JSONL store)
**Storage:** `knowledge_extractor/state/artifacts.jsonl` (path resolved via `pressure_loop.policy.knowledge_artifact_store_path()`, with a `__file__`-relative repo-root fallback when the configured path is relative)

## Types

**Source** (`knowledge_extractor/_types.py`):
- `kind`: `web_result` | `file` | `pdf` | `video_transcript` | `tool_output`
- `content`: raw text
- `title`: optional
- `source_ref`: optional URL/path
- `meta`: optional metadata dict

**KnowledgeArtifact** (`knowledge_extractor/_types.py`):
- `domain`: inferred domain
- `concepts`: tuple of concept strings
- `facts`: tuple of fact strings
- `relationships`: tuple of relationship dicts
- `mastery_tags`: tuple of mastery tag strings
- `confidence`: float [0,1]
- `source_ref`: optional
- `source_kind`: string
- `artifact_id`: stable identifier

## Config

| Artifact | Path |
|----------|------|
| DeepSleep integration | `config/deepsleep.yaml` → `knowledge_integration` |
| Artifact store | `config/deepsleep.yaml` → `knowledge_integration.artifact_store` (default: `knowledge_extractor/state/artifacts.jsonl`) |

## DeepSleep Integration

The DeepSleep **consolidate** phase (`deepsleep_loop/consolidate/run.py`, `build_journal_entry`) reads `knowledge_extractor/state/artifacts.jsonl` when `consume_on_consolidate` is enabled — checked via `pressure_loop.policy.deepsleep_knowledge_consume_on_consolidate()`, not by parsing the YAML directly — then calls `knowledge_extractor.consume.knowledge_artifact_for_episode`. Matching artifacts are merged into `journal_entry.artifacts.knowledge`. The separate `consolidate_memory` phase (7a) then promotes that journal entry into `memory/600_journal` via `memory.api`.

## Entry Points / How It's Used

- **Direct:** `from knowledge_extractor import extract; artifact = extract(source)`
- **AUTO LEARN hook:** `auto_loop/execute/run.py::_maybe_extract_knowledge` calls `extract(Source(...))` after successful tool execution and appends the artifact to `knowledge_extractor/state/artifacts.jsonl`.
- **DeepSleep:** `deepsleep_loop/consolidate/run.py::build_journal_entry` calls `knowledge_extractor.consume.knowledge_artifact_for_episode` when `consume_on_consolidate` is enabled.

## Current State / Legacy Findings

As of the latest codebase audit:

| Metric | Count |
|---|---|
| Total Python files | 10 |
| Actively used | 10 |
| Unused | 0 |
| Stub / TODO modules | 0 |

**Every Python file in `knowledge_extractor/` is referenced.** The directory is fully active.

### Cross-cutting `_slug` utility

`knowledge_extractor.derive.run._slug` is imported far beyond this package as a shared domain-key normalizer:

- `cognition/topic_resolution/resolve_topic.py`
- `cognition/topic_resolution/domain_key.py`
- `pressure_loop/idle_wake.py`
- `deepsleep_loop/mastery_store.py` (via `normalize_domain_id`)
- `memory/subdomain_allocator.py`
- `memory/coding_project.py`

It is a shared utility used by the current domain-key and routing paths; this
package remains its current source.

### Current integration

The LEARN hook is wired: `config/knowledge.yaml` enables it and
`pressure_loop.policy.knowledge_learn_integration_wired()` is the live gate used
after successful tool execution. DeepSleep consumption remains separately
controlled by its consolidation policy.

## Determinism

The knowledge extractor is **fully deterministic** — no model inference, no random choices. All extraction is heuristic-based.

## Depends on / Depended-on-by

- **Depends on:** `pressure_loop.policy` (for store path resolution — `knowledge_artifact_store_path()` / `deepsleep_knowledge_artifact_store_path()`)
- **Depended-on-by:**
  - `auto_loop/execute/run.py` (LEARN hook via `knowledge_extractor.extract`)
  - `deepsleep_loop/consolidate` (optional artifact consumption, via `knowledge_extractor.consume`)
  - `cognition/topic_resolution`, `pressure_loop/idle_wake`, `deepsleep_loop/mastery_store`, `memory/subdomain_allocator`, `memory/coding_project` (all import `knowledge_extractor.derive.run._slug` as a shared domain-key normalizer)

## Gotchas / Doctrine

- Store path is resolved via `pressure_loop.policy.knowledge_artifact_store_path()` (which reads `config/deepsleep.yaml` indirectly), not by parsing the YAML directly and not via `core.paths`.
- Artifacts are append-only JSONL; there is no update-in-place.

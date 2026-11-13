# docs/architecture — Current Maps and Historical Boundary

> **Authority:** `AGENTS.md` and the live source tree. These documents are
> navigation aids, not a second implementation.

## Current Cascade

The current architecture is the queue-backed pressure cascade, not a collection
of independently owned brain or autonomous loops:

```text
boot.py
  -> pressure_loop (durable queue, pressure, episode admission)
  -> cognition_chain.py
  -> global_loop (Global A intent)
  -> meta_loop (Meta A action; deterministic execution contract)
  -> auto_loop (model and tool execution)
  -> meta_loop (Meta B reflection)
  -> global_loop (Global B outcome)
  -> omega_loop/episode (per-episode cognition)
  -> deepsleep_loop (canonical writes, projections, close)
```

The physical package owners are:

| Package | Current responsibility |
|---|---|
| `pressure_loop/` | The only pressure tick loop, durable queue, ignition, and episode carrier. |
| `cognition_chain.py` | Handoff between the phase packages; it is not a second loop owner. |
| `global_loop/` | Deterministic intent and outcome packaging. |
| `meta_loop/` | Deterministic action planning and execution reflection. |
| `auto_loop/` | Model/tool execution, memory-recall-in-loop, and bounded skill dispatch. |
| `omega_loop/` | Per-episode cognition (one strict control decision per episode); see `COGNITIVE_CASCADE_CURRENT.md`. |
| `deepsleep_loop/` | Episode terminus, SRAM drain, canonical persistence, and projection maintenance. |
| `memory/` | Canonical memory, deterministic recall, DME/retina projections, and gates. |
| `runtime/` | HTTP/local-service transport, provider adapters, model controls, and modality adapters. |
| `guardian/` | External-ingress decisions, response policy, and telemetry. |
| `hud/` | In-process FastAPI operator surface; frontend source is `hud/ui-src/`. |
| `autonomic/` | Neuromodulation and lifetime-clock signals; it does not execute cognition. |

## Live Anchor

- [`VISION_PERCEPTION_PIPELINE.md`](VISION_PERCEPTION_PIPELINE.md) — current
  vision-organ contract, with implemented and deferred behavior separated.
- [`DEAD_CODE_REMOVAL_MAP_2026-09-10.md`](DEAD_CODE_REMOVAL_MAP_2026-09-10.md) —
  current-tree dead-code evidence and removal decisions.

The package READMEs in the repository root are the shorter current references:
`pressure_loop/`, `global_loop/`, `meta_loop/`, `auto_loop/`, `omega_loop/`,
`deepsleep_loop/`, `memory/`, `runtime/`, `guardian/`, `hud/`, and `core/`.

## Historical or Unverified Documents in This Directory

The following paths remain useful evidence but are not current architecture:

- [`LIVE_CODEBASE_MAP.md`](LIVE_CODEBASE_MAP.md) and
  [`v2/LIVE_CODEBASE_MAP.md`](v2/LIVE_CODEBASE_MAP.md) — superseded snapshots.
- [`systems/INDEX.md`](systems/INDEX.md) and the `systems/` anchor files — old
  package ownership maps that still name retired roots.
- [`ACTIVE_FRONT_MAP.md`](ACTIVE_FRONT_MAP.md),
  [`AUTO_INTEGRATION_SURFACE.md`](AUTO_INTEGRATION_SURFACE.md),
  [`MINIMAL_BOOT_SURFACE.md`](MINIMAL_BOOT_SURFACE.md), and
  [`V2_LIVE_BOOT_INTEGRATION_DESIGN.md`](V2_LIVE_BOOT_INTEGRATION_DESIGN.md) —
  point-in-time migration/design material.
- [`RUNTIME_BRAIN_CONTRACT.md`](RUNTIME_BRAIN_CONTRACT.md) and
  [`HUD_PILL_TELEMETRY_V2.md`](HUD_PILL_TELEMETRY_V2.md) — historical contracts
  whose runtime and HUD ownership statements must not be used as current maps.
- [`EPISODE_LIFECYCLE_MAP.md`](EPISODE_LIFECYCLE_MAP.md),
  [`DEEPSLEEP_PER_EPISODE_MAP.md`](DEEPSLEEP_PER_EPISODE_MAP.md),
  [`AUTO_LOOP_MAP.md`](AUTO_LOOP_MAP.md),
  [`FACE_RECOGNITION_PIPELINE_MAP.md`](FACE_RECOGNITION_PIPELINE_MAP.md),
  [`FACE_MODEL_INTEGRATION_MAP.md`](FACE_MODEL_INTEGRATION_MAP.md),
  [`DYNAMIC_SUBDOMAIN_SURFACE_MAP.md`](DYNAMIC_SUBDOMAIN_SURFACE_MAP.md),
  [`WINDOWED_OMEGA_REFLECTION_README.md`](WINDOWED_OMEGA_REFLECTION_README.md),
  [`COGNITION_CHAIN_AND_PROMPTS_MAP.md`](COGNITION_CHAIN_AND_PROMPTS_MAP.md),
  [`AUTO_DETERMINISTIC_OMEGA_COGNITION_MAP.md`](AUTO_DETERMINISTIC_OMEGA_COGNITION_MAP.md),
  [`TOPIC_RESOLUTION_AND_MASTERY_GUIDED_LEARNING.md`](TOPIC_RESOLUTION_AND_MASTERY_GUIDED_LEARNING.md),
  [`STATE_WRITE_SURFACE_MAP.md`](STATE_WRITE_SURFACE_MAP.md),
  [`OPERATOR_CHANNEL_MAP.md`](OPERATOR_CHANNEL_MAP.md),
  [`RESPONSE_OPERATOR_CHANNEL_MAP.md`](RESPONSE_OPERATOR_CHANNEL_MAP.md), and
  [`USER_EXECUTION_SKILLS_CONTRACT.md`](USER_EXECUTION_SKILLS_CONTRACT.md) —
  dated as-built/design maps that still contain superseded paths or claims.
  Use them only as historical evidence until separately reconciled.
- [`../_archive/architecture/`](../_archive/architecture/) — frozen workorders,
  handoffs, verification reports, and pre-refactor architecture.

The retired root paths `brain/`, `autonomous/`, `metacognition/`, and `hud/ui/`
are not current package owners. The live HUD builder is
`hud/scripts/build_hud.py`, which assembles `hud/ui-src/` into
`hud/omega_interface.html`.

## Cross-Checking Rule

1. Start with the package README and live source named by the map.
2. Treat a dated, generated, or explicitly historical map as evidence, not authority.
3. If a map conflicts with source, the source wins; update or archive the map in
   a separately authorized documentation task.

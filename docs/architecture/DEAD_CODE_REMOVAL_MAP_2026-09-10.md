# Dead Code Removal Map

Audit date: 2026-09-10

This is the current-tree removal map for Omega 3.0. It is based on the files
present in the working tree, production imports, generated manifests, and
retirement tests. A historical document or a test-only seam is not treated as a
production caller.

## Live Authority

The live execution spine is:

```text
boot.py
  -> pressure_loop.cognition_queue
  -> pressure_loop.loop
  -> cognition_chain.py
  -> global_loop -> meta_loop -> auto_loop
  -> omega_loop -> deepsleep_loop
  -> memory / guardian state
```

The live operator surface is `hud/server.py`, served in-process by
`hud/spawner.py`. The frontend source is `hud/ui-src/`; the only HUD builder is
`hud/scripts/build_hud.py`, which produces `hud/omega_interface.html`.

Runtime ownership is split deliberately:

- `runtime/local_services.py` owns local DME, Kokoro, and Parakeet services.
- `runtime/face/` owns CPU-only ONNX face inference and never claims the DME NPU.
- `runtime/vision.py` routes scene description through the selected offline or
  cloud mind seat.
- `memory/retina.py` and `memory/dme/` own the live semantic memory path.

## Already Removed

These paths are physically absent or already marked deleted in the current
working tree. Do not recreate them and do not treat their historical references
as live callers.

| Retired surface | Evidence | Current replacement |
|---|---|---|
| `omega_cli_ts/`, `qwen-agent-tui/` | Root trees are absent; retirement tests assert this. | `auto_loop/execute/brain_skill_executor.py` through the shared chat rail |
| `runtime/qwen_cli_client.py` | File is absent; direct bridge tests and invoker were removed. | `runtime.client` plus the bounded brain skill executor |
| `runtime/chat_route.py` | File is absent; provider key ownership moved to `runtime/brain_cloud_config.py`. | `runtime.brain_local`, `runtime.brain_cloud`, and `runtime.mind_connector` |
| `hud/omega_cli_launch.py` | File and launch tests are deleted. | In-process HUD and live-chat execution |
| `hud/agent_audit_log.py`, `pressure_loop/skill_activity_feed.py` | Files and their obsolete tests are deleted. | Shared chat/SSE telemetry and durable operator records |
| `hud/ui-src/panels/agent.js`, `dev.js`, `identity.js`, `deepsleep.js`, `settings-terminal.js` | Unregistered panels and their tests were deleted. | Current settings, help, pipelines, and proactive pinboard panels |
| `hud/ui-src/panels/autoloop.js`, `brain.js`, `memory.js`, `omega.js` | Unregistered panels had no manifest entry or live caller; deleted in the HUD cleanup pass. | Current HUD telemetry and retained identity/deepsleep primitives |
| `memory/local_embed.py`, `memory/semantic_state_engine.py`, `memory/embed_backfill.py`, `memory/embed_contracts.py` | Dense embedding retirement tests and the deletion manifest cover the migration. | Ettin retina codes and DME packed memory |
| `core/compat.py` | Its only callers were three maintenance scripts invoking the removed SetFit dependency shim. | Removed with the SetFit island; the scripts now start directly. |
| `guardian/cycle_context.py` and the `guardian.recovery` HUD probes | The module is absent and the HUD probes only caught imports of another absent module. | Removed the stale documentation and dead telemetry branches; live Guardian telemetry remains. |
| `tests/hud/test_image_bypass_retired.py`, `tests/hud/test_omega_cli_launch.py`, and direct bridge tests | They tested removed surfaces and are deleted with their implementations. | Current retirement and portability contracts |
| `scripts/cursor_bus_watcher.py`, `scripts/devin_bus_watcher.py`, `scripts/install_systemd_user.sh`, `scripts/omega3_env_check.mjs`, `scripts/omega3_review_swarm_runner.mjs`, `scripts/omega_bus_peek.sh` | No current launcher or production caller; files are deleted in the working tree. | Portable Windows launcher and `scripts/agent_bus.py` where applicable |
| `runtime/setfit/`, `scripts/setfit_dataset/`, `memory/engine/train_setfit.py`, `memory/engine/train_lobe_vm.py`, `memory/engine/hierarchical_classifier.py`, `memory/engine/classifier_cache.py` | No production imports remain; the only non-production dependency was `memory/routing/corpus.py`, whose labeled data now lives in `memory/routing/training_data.py`. | `memory.dewey_map`, `memory.subdomain_allocator`, grammar routing, and the retina/DME path |
| `runtime/openrouter_config.py`, `runtime/dev_connect_state.py` | No production caller remains after agent handoff was bound to the one persisted mind seat; their tests and secondary state route were removed. | `runtime/brain_cloud_config.py` and `runtime.mind_connector` |

## Confirmed Removal Batch

These items had no production caller and were removed during this audit. The
focused retirement tests and builder checks are the proof for this batch.

| Path or symbol | Evidence | Removal action |
|---|---|---|
| `scripts/hud_ui_modularize.py` | It targeted the absent `hud/ui/` tree. Repository references point to `hud/scripts/build_hud.py`; no current caller invoked the old builder. | Removed; `hud/scripts/build_hud.py` is the only builder. |
| `scripts/_import_closure_tmp.py` | Temporary closure analysis script; no runtime caller. The only test reference checked it for a host path. | Removed with the obsolete test path entry. |
| `auto_loop.mril_tools.format.format_remembered_knowledge_json` | No production or test caller; only the package export remained. | Removed; `format_remembered_knowledge_block` remains. |
| `hud/ui-src/scripts.html` old `hud/ui/js/11-*` comment | The comment names the retired builder layout; the live builder injects `hud/ui-src` modules. | Correct the source comment and rebuild the bundle. |
| Obsolete entries in `models/manifest.json` | The current asset tree contains only DME, Kokoro, Parakeet, and InsightFace assets, while the old manifest advertised absent Gemma, Whisper, Piper, dense embedding, SetFit, and diffusion trees. | Replaced with the current shipped modality assets and the face IDs consumed by runtime socket configuration. |

## Retained, Not Dead

These paths look legacy-shaped but have current callers or preserve a required
data/contract seam.

| Path | Why it stays |
|---|---|
| `memory/self_vector.py` | The identity sidecar and HUD telemetry still read its live state. Dense semantic recall retirement does not prove the identity sidecar is removable. |
| `memory/engine/walker_loader.py` | It is used by motor/test harnesses; remove only after those harnesses are migrated to the canonical walker entry point. |
| `runtime/qwen_skills.py` | The name is historical, but the current skill catalog and capability sync import it. |
| `runtime/tool_dispatch.py` and `runtime/socket.py` | Their active deterministic dispatch functions have production callers. |
| `schema/qwen_skill_dispatch.py`, `mcp_platform/plugin_planning.py`, and `organ/promptorgan/` | These are live bounded contracts, not remnants of the removed TypeScript bridge. |
| `memory/retina.py`, `memory/dme/`, `runtime/local_services.py`, `runtime/face/`, and `runtime/vision.py` | These are the current semantic, modality, face, and scene-vision authorities. |
| `global_loop/assemble/run.py` dual-read of retired `action_goal` | Compatibility with persisted episode data is still a concrete migration concern. Remove only after stored records and migration coverage prove the field is gone. |
| `training/`, benchmark harnesses, and `docs/_archive/` | These are offline or historical artifacts, not live runtime code. They are not silently deleted as part of a production-path cleanup. |

## Documentation Corrections

Current-tree documents must not describe removed paths as live. The remaining
high-priority corrections are:

- `sensory/README.md` must describe `operator_presence.py` and
  `vision_ingest.py`; `sensory/voice_io.py` is absent.
- `hud/README.md` must not list removed terminal/agent surfaces or claim that
  removed cognition panels are current UI entry points.
- `scripts/README.md` must list only existing launchers and must identify the
  current HUD builder.
- `docs/architecture/PHASE_6_SKILL_EXECUTION_BRIDGE_MAP.md` and related bridge
  maps are historical inventories and must remain explicitly non-authoritative.
- `models/README.md` must agree with the current asset-only manifest after the
  manifest cleanup.

## Verification Gate

Every removal batch must pass all of the following before the next batch:

1. A repository-wide caller search for each deleted path and symbol.
2. Relevant retirement tests and the owning subsystem tests.
3. `hud/scripts/build_hud.py --check` after HUD source changes.
4. Ruff/compile checks for changed Python files.
5. `git diff --check`.

Compatibility and persisted-data seams are excluded from deletion until their
specific migration proof is complete.

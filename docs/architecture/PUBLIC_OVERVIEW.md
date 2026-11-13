# Omega 3.0 — Architecture and code-entry map

This technical map is written for readers of the **archived source**. Its authority is the shipped files, not older design snapshots. Many historical documents in `docs/architecture` describe intermediate versions and have different ownership or model configurations.

## 1. The cognition cycle

The source contains one pressure-driven episode flow, with one shared cascade:

```text
external events / voice / chat / autonomous triggers
                 |
                 v
    guardian external-ingress screening
                 |
                 v
 pressure_loop.cognition_queue durable FIFO
                 |
    lane-specific lease and admission
                 v
   cognition_chain._ignite_episode_cascade
                 |
                 v
Global A -> Meta A -> AUTO -> Meta B -> Global B
                 |
                 v
          Omega episode fold
                 |
                 v
         DeepSleep terminus
                 |
                 v
       canonical memory/close
```

Primary sources: `boot.py`, `pressure_loop/loop.py`, `pressure_loop/cognition_queue.py`, `cognition_chain.py`, `global_loop/loop.py`, `meta_loop/loop.py`, `auto_loop/execute/run.py`, `omega_loop/episode/run.py`, `deepsleep_loop/loop.py`.

A queue lease is distinct from successful execution. Admission, terminal status, retries and cross-lane handoffs are represented explicitly. Agent orchestration is not a single magic function; the implementation is distributed over those contracts.

## 2. Dedicated lanes and cross-lane coordination

Omega uses four dedicated semantic lanes: chat, agent, browser, and reflection. A shared durable input queue tracks identity and ordering within the owned lane.

- `pressure_loop/cognition_queue.py`: lane identity, durable storage, leases and acknowledgements.
- `pressure_loop/specialist_lane_handoff.py`: deterministic browser/agent dispatch identity, mail recording, bounded evidence, idempotent retries.
- `pressure_loop/held_instruction_handoff.py`: durable mail/continuation reconciliation after interruption or restart.
- `global_loop/intersections.py`: shared bounded handoff payload checks.
- `cognition_chain.py`: maps the handoff results back to the originating operator thread.
- `hud/chat_socket.py` and `pressure_loop/operator_chat_responses.py`: visible lane updates and eventual result delivery.

The handoff architecture coordinates specialized work; it is not a guarantee that all operations can proceed simultaneously without resource contention.

## 3. Memory pipeline

```text
external proposal ─── Guardian validation
                        |
cognitive event ────────+────> episode SRAM
                                |
                           DeepSleep
                                |
                   12-lobe canonical Dewey files
                                |
               graph/taxonomy/retina/DME projections
                                |
                      bounded verified recall
```

- `memory/api.py`: canonical read/intake/write API.
- `memory/episode_sram.py`: temporary live episode ownership of new experience.
- `memory/dewey_map.py`: deterministic lobe/subdomain mapping.
- `memory/strength_index.py`: per-memory strength and reinforcement.
- `memory/retina.py`: binary semantic-perception candidate scoring.
- `memory/projection/generation.py`: hash-bound snapshot publication and read consistency.
- `deepsleep_loop/loop.py`: canonical commit, projection rebuild, failure reconciliation and close.

Derived projections are not canonical facts. A failed projection may be reported as unavailable rather than silently serving a contradictory snapshot.

## 4. Neuromodulation

In `autonomic/symphony_neuromod.py`, a signed reward-prediction error is computed from outcome and baseline success rate:

```text
observed v = +1 on success, -1 on failure, 0 otherwise
expected = 2 × baseline_success_rate - 1
RPE = clamp(v - expected, -2, 2) / 2
dopamine = bounded RPE
plasticity = abs(RPE)
stress = clamp(0.6 × failure + 0.4 × negative_RPE + 0.3 × runtime_overrun, 0, 1)
serotonin = clamp(baseline_success_rate - 0.5 × stress, 0, 1)
```

These are bounded engineered signals that can inform salience, caution or persistence, **not biological measurements** and not permission to override user authority or reality.

## 5. Browser and general agent execution

- `auto_loop/execute/run.py`: model/tool execution and task outcome management.
- `browser_agent/agent_loop.py`: observe → choose one browser tool → act → verify/screenshot, under explicit budgets.
- `browser_agent/controller.py`: isolated Chrome DevTools Protocol session, pointer actions, navigation and page state.
- `browser_agent/extract.py`: bounded extraction for articles, links, tables, metadata and search results.
- `browser_agent/page_state.py`: structured control and viewport observations.

The public snapshot incorporates the later F: changes for pointer hover, full-page scans, structured extraction, screenshot OCR tool selection and action sequencing. This component is **separate from the Omega Browser Agent Edge extension product**.

## 6. Model, plugins, and permission boundaries

- `runtime/client.py`, `runtime/brain_local.py`, `runtime/brain_cloud.py`, `runtime/model_templates.py`: provider-independent model seat and LM Studio/OpenAI-compatible adapters.
- `mcp_platform/`: MCP client, OAuth/credential vault, signed app lifecycle, tool execution and isolated worker processes.
- `plugin_platform/`: package signatures, declarative manifests, installation and trusted catalog.
- `guardian/` and `hud/server.py`: external ingress, operator-facing scope and permissions.

The code demonstrates explicit controls; the release does not claim a formal security audit of the full plugin ecosystem.

## 7. Proactive voice

- `pressure_loop/idle_wake.py`, `pressure_loop/proactive_lifecycle.py`: idle awakening, durable proactive-question and outcome tracking.
- `hud/chat_socket.py`: streaming deltas and turn correlation.
- `hud/ui-src/core/voice-live.js`: audio capture, activity detection and barge-in.
- `hud/ui-src/core/voice-runtime.js`: voice UI integration and Qwen speech playback.
- `runtime/stt.py`, `runtime/tts.py`, `runtime/qwen_tts.py`: Parakeet STT and Qwen3-TTS runtime transport.

Streaming and interruption paths exist in source. Their real-time performance depends on devices, microphones, backends and model availability.

## 8. Architectural limitations

The largest source files remain difficult to maintain, notably `mcp_platform/plugin_lifecycle.py` and `hud/server.py`. Configuration, tests and historical documents may encode older intended behavior. Browser interaction remains subject to site changes; cognition and voice require functioning local model runtimes.

This is an engineering archive, not a statement that every test passed on every platform or that emergent general intelligence was achieved.

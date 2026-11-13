# Omega 3.0 — archived AI systems research

**Status: Retired as a product; preserved for source inspection and engineering research.**  
**Organization:** Omega AI LLC  
**Primary language:** Python 3.13, with browser-based UI and supporting JavaScript/TypeScript.  
**License:** MIT (see [LICENSE](LICENSE)).  
**Distribution:** Source archive, **not** a ready-to-run Windows/Linux installation.

Omega 3.0 was an attempt to build an integrated, local-first artificial intelligence operating system: persistent memory, cognitive control, model orchestration, computer-use agents, proactive voice interaction, and a signed plugin/MCP ecosystem in one coherent architecture.

The project was retired because its engineering scope grew beyond what a single maintainer could reasonably evolve during rapid changes in agentic AI and local inference. Its successor research direction is **Odin**, a separate effort focused on the intelligence substrate itself: associative memory, learning, and the creation of novel knowledge. Odin is not presented here as a finished derivative of Omega, nor is unverified emergent behavior claimed as established fact.

Read [Why Omega was retired](docs/RETROSPECTIVE.md) and the [architecture walkthrough](docs/architecture/PUBLIC_OVERVIEW.md) before navigating the implementation.

## Architectural overview

```text
  Operator chat/voice    External observation    Proactive curiosity / idle wake
           |                       |                           |
           +-----------------------+---------------------------+
                                   |
                    Guardian: external ingress
                                   |
                 Durable cognition queue (per-lane FIFO)
                                   |
                Dedicated execution lanes (Chat, Agent,
                        Browser, Reflection)
                                   |
             Pressure -> Global A -> Meta A -> AUTO
                         -> Meta B -> Global B
                         -> Omega -> DeepSleep
                                   |
            Episode SRAM -> canonical Dewey memory
                                   |
              Verified graph + retina/DME projections
```

The cognition cascade performs intent extraction, deterministic planning, bounded model/tool execution, outcome handling, episodic reflection, and persistent consolidation. The operator's response is a permitted AUTO action; reflection does not have to delay sending it.

## Navigate the source

| Component | Source | Engineering focus |
|---|---|---|
| Boot and episode cascade | [boot.py](boot.py), [cognition_chain.py](cognition_chain.py) | Startup, phase handoffs, episode lifecycle |
| Pressure and scheduling | [pressure_loop/](pressure_loop/) | Single durable ingress gate, queue, admission and dedicated lanes |
| Intent and reflection | [global_loop/](global_loop/), [meta_loop/](meta_loop/), [omega_loop/](omega_loop/) | Action goals, deterministic contracts, episode metacognition |
| Tool and model execution | [auto_loop/](auto_loop/) | Model seat, guarded tools, skills and agent workflows |
| Memory | [memory/](memory/) | Canonical 12-lobe Dewey records, deterministic recall, strengths, retina and projection generations |
| Consolidation | [deepsleep_loop/](deepsleep_loop/) | SRAM draining, canonical persistence, recovery and maintenance |
| Neuromodulation | [autonomic/](autonomic/) | Bounded reward-prediction error, salience, stress and other advisory signals |
| Browser agent | [browser_agent/](browser_agent/) | CDP, screenshots, pointer control, scans and bounded structured extraction |
| Plugins and MCP | [plugin_platform/](plugin_platform/), [mcp_platform/](mcp_platform/) | Signed packages, permission scopes, OAuth custody and isolated workers |
| Model/voice runtime | [runtime/](runtime/) | Local/cloud model adapters, Qwen3-TTS, STT and streaming transport |
| Operator UI | [hud/](hud/) | FastAPI, live chat socket, browser UI, continuous voice and interruption |
| Policy and audits | [guardian/](guardian/), [tests/](tests/) | External-ingress controls, telemetry and regression testing |
| Supporting experiments | [benchmarks/](benchmarks/), [standalone_memory/](standalone_memory/) | Selected benchmark harnesses and exploratory standalone memory components |

For implementation details and precise path references, see [Architecture: the real execution flow](docs/architecture/PUBLIC_OVERVIEW.md).

## Core research ideas

**Memory as state, not a conversational appendix.** Fresh experiences are held in episode SRAM. DeepSleep is the canonical persistence boundary. Deterministic Dewey coordinates provide a stable data model, while graph, retina and DME projections are rebuildable retrieval infrastructure, not a second source of truth.

**Concurrency with explicit ownership.** The durable queue assigns work to dedicated semantic execution lanes. Cross-lane handoffs carry stable identity, bounded evidence, and recovery receipts. Multiple agents can cooperate without pretending independent threads are automatically one mind.

**Metacognition after action.** Omega’s episodic reflection consumes actual execution outcomes and can produce memory candidates. This is engineered introspection and evaluation, not evidence of sentience or proven emergent general intelligence.

**Bounded neuromodulation.** The system computes mathematical analogues of reward prediction error, stress, serotonin, and plasticity; signals influence learning and salience without overriding operator intent or factual evidence.

## Reproducibility and limitations

This repository intentionally **does not include** locally installed model weights, SDK binaries, Python virtual environments, device-specific ROCm/Vitis runtime installations, operator credentials, conversation histories, personal memory profiles, or past raw validation logs. The original system depended on these local artifacts. The archive may therefore need nontrivial setup to execute end-to-end.

The source is suitable for reading and static analysis. A complete end-to-end runtime demonstration, performance benchmark, or installation on a new machine **is not guaranteed by this publication**.

- See [Reproducibility and validation](docs/REPRODUCIBILITY.md) for what was checked and what remains unverified.
- Python syntax validation uses Python 3.13.
- Focused test examples live under `tests/browser_agent/`, `tests/pressure_loop/`, `tests/memory/`, `tests/runtime/`, and `tests/mcp_platform/`.
- The historical [AGENTS.md](AGENTS.md) is an engineering contract written when Omega was an active product; do not read its present-tense requirements as a maintenance commitment.

## Successor: Odin

Omega 3.0 attempted to engineer an entire human-computer collaboration stack around cognition. **Odin deliberately narrows the mission** to understanding and implementing a single learning intelligence substrate that can develop new associations and knowledge. Odin is separate research, not included in this repository.

## Licenses and security

Source is under [MIT](LICENSE), which permits commercial use and redistribution. This archive makes no promise of patent protection or restricted commercial licensing. Third-party software and model assets, not redistributed here, may have separate licenses.

If you find a security vulnerability or a mistakenly included credential, contact **nanoai@omega2ai.com** and avoid opening a public issue containing the secret. See [SECURITY.md](SECURITY.md).

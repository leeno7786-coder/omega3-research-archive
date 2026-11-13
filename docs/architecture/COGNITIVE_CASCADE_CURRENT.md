# The Cognitive Cascade (current)

> **Authority:** `AGENTS.md` and the live source tree. This page describes the
> one post-action control loop Omega runs today (realignment landed 2026-09-15).
> Anything that contradicts it in older maps under `docs/architecture/` is
> historical.

```text
one durable ingress gate → four lane FIFOs (Chat P0, Browser P1, Agent P2, Window P3)
one active episode per lane → up to four model predictions
Global A → Meta A → AUTO → Meta B → Global B → Omega
Omega prompt lane = chat | agent | browser | window
Omega DONE → close
Omega PRESSURE → staged successor → new episode
DeepSleep/commit terminus
```

## One loop, one control point

Every episode runs the same seven stages and then ends. Nothing inside an episode
re-plans or asks a model whether the objective is met: the only place work is
judged and continued is Omega, after Global B. There is no pre-AUTO motor
inference and no durable job continuation queue. The browser lane's AUTO stage is
a bounded agent loop (many actions, one episode), but it never judges completion.
Unfinished work continues as a **new episode** that re-enters Global A.

| Stage | Owner | What it does |
|---|---|---|
| Ignition | `pressure_loop/` | The ingress gate durably places each item in one semantic FIFO. Pressure leases only that lane's head to its permanently bound pipeline; other lanes proceed independently. |
| Global A | `global_loop/loop.py` | Names the actionable intent and exactly one `omega_lane`. Operator turns classify inside Chat. A Browser, Coding Agent, or Reflection route is durably handed to that lane before AUTO acknowledges in Chat. |
| Meta A | `meta_loop/` | Prepares execution without inference: the action, the tool or skill, the memory contract. For BROWSE it performs the read-only look at Omega's window before the run (`meta_loop/browser/perception.py`). |
| AUTO | `auto_loop/` | Executes the prepared contract once. |
| Meta B | `meta_loop/reflect` | Reflects deterministically on the AUTO outcome. |
| Global B | `global_loop/loop.py` | Packages the reflection plus episode evidence into `OmegaEpisodeInput`. |
| Omega | `omega_loop/episode`, `organ/promptorgan/packs/meta_omega/` | One model pass per episode, in one of four prompt lanes (chat, agent, browser, window), all on the same Nelson–Narens monitoring/control spine with explicit 5W1H orientation. It receives ordered lane mail and run-scoped recalled evidence at its fold and returns one strict `OmegaControlDecision`. |
| DeepSleep | `deepsleep_loop/` | Commits the episode's SRAM and closes it. |

## The control envelope

`schema/omega_control_decision.py` is the only per-episode pressure authority.
Every lane parses through the same validator (`meta_omega/core.py::parse_lane`).

- **DONE** — the gap is empty, there is no pressure, nothing is staged. The
  episode closes.
- **PRESSURE** — a validated intent (`BROWSE`, `USE_SKILL`, `USE_TOOL`, `LEARN`,
  `PURSUE_OUTCOME`) with reason, desired and observed state and resolution
  conditions. `pressure_loop/omega_successor.py::stage_omega_successor` stages
  **at most one** durable successor contributor in
  `guardian/state/omega_successors.json`. The successor is admitted by the
  pressure tick like any other input, gets a **new episode id**, and re-enters
  Global A. AUTO is never re-run under the prior episode id.
- **ASK_OPERATOR** — the question goes into the operator's waiting bubble; no
  successor is staged until the operator answers.

The projected `pressure_params` / `intent_decision` on `omega_result` exist for
the journal only. Nothing reads them to ignite work: the level and intent
overlays (`omega_levels`, `omega_intent`) are gone.

### Autonomy is Omega choosing

A successor with operator lineage (`original_cycle_id`) always runs. Work nobody
started — idle learning's awakening (`pressure_loop/idle_wake.py`), which also
stages a validated successor — runs only while the operator has idle learning
engaged. Otherwise the mind sits idle until the operator speaks.

## Lanes and tools

Global A's route sets the lane; the lane sets the tools.

| Lane | Route | Tools offered to the model | Continuation |
|---|---|---|---|
| chat | `FREEFORM_CHAT`, `DOCUMENT_CREATION` | `web_search`, `web_fetch`, connected-app plugin tools | The reply is delivered at AUTO; Omega's chat lane reflects on the exchange afterwards. |
| agent | `CODING_AGENT`, `/skill` | Workspace, file, command and job tools (`brain_skill_executor.lane_tool_names`) | **One tool batch per episode.** One model call emits the batch (`start_job` first before mutable work), AUTO runs it once and stops. Omega decides DONE or PRESSURE(`USE_TOOL`); the successor carries a bounded digest of the earlier batch results (`agent_observations`) and runs the next batch. |
| browser | `BROWSER_AGENT` | The browser agent's own tools (`browser_agent/agent_loop.py::TOOL_SPECS`: navigate, click, type_text, press_key, scroll, drag, run_javascript, read_page, find, go_back, wait, list_tabs, switch_tab, done, ask_operator) | **One bounded agent run per episode.** Meta A looks once; AUTO runs the agent loop in Omega's own Edge window: the model chooses one tool per turn, the host runs it, screenshots, streams the frame to the chat panel and hands it back, until the agent says `done`/`ask_operator` or a budget ends it. Omega judges the receipt (`browser_run`) and decides DONE, PRESSURE(`BROWSE`, its reason becoming the next run's instruction) or ASK_OPERATOR. |
| window | staged `window_reflection_ready` | none | The ten-episode window reflection is a first-class lane on the same cascade, not a separate topology. |

### Browser specifics

- Omega works in **its own background tab of the operator's Edge window**
  (`BrowserController.open_task_window`), driven over DevTools
  (`browser_agent/cdp_client.py`), not WebDriver: the tab is created with
  `Target.createTarget background=true` and is never activated, so the HUD stays
  in front and the operator decides when to look. Screenshots
  (`Page.captureScreenshot`), mouse and key input and page scripts all work on
  the hidden tab (measured on Edge 153); WebDriver could not (its screenshot
  activated the tab and its input actions stalled). Scrolling is done by page
  script because a synthetic wheel event never returns on a hidden tab. Tabs
  that existed before Omega's are the operator's and are never listed, switched
  to, captured or closed; a tab a link opens from Omega's is Omega's. The tab
  carries a non-blocking "OMEGA is working in this window" banner.
- The agent loop (`browser_agent/agent_loop.py`) is the Omega browser
  extension's shape ported to the brain: screenshot + page state
  (`browser_agent/page_state.py`: address, title, viewport, readable text, the
  visible controls with their centres) → one tool call → act → wait for the
  page → screenshot → `chat.browser_frame` to the HUD → back to the model.
  Coordinates are pixels of the screenshot the model saw and are mapped to
  CSS viewport pixels by the host. Only the newest screenshot stays in the
  transcript as pixels. Budgets: 25 actions, 6 minutes, 3 consecutive
  failures, 2 inference failures. Typed text is never kept in durable state;
  a password field's text never leaves the process.
- The durable workflow (`meta_loop/browser/workflow.py`, version 2) persists
  the objective, the operator's newest instruction, mid-run operator notes and
  a page-free record of each run. It never persists the WebDriver session or
  window handle: browser ownership lives only in the running process and is
  released at shutdown; a crash saves nothing and the next boot attaches fresh.
- **Chat while it works.** A Browser or Coding Agent instruction is durably
  mailed to that lane with the originating conversation and work identity. The
  active inference is unchanged; the destination Omega fold receives ordered
  mail at its next eligible cutoff. Chat acknowledges immediately and reports
  later progress through correlated `chat.update` events.
- A run that could not act (`blocked`, `inference_failed`, `busy`,
  `note_delivered`) is never continued by a successor; the workflow waits for
  the operator. Three such runs block the workflow; the operator's next message
  revives it, and an explicit completion phrase closes it.

### Agent specifics

- `auto_loop/execute/job_control.py` keeps exclusive project ownership, one
  writer per job and a durable stop. `revise_job`, `admit_ready_jobs`,
  `claim_job` and the `continuation_ready` status are gone; a job whose
  writer died with the process is stopped at boot.
- A finished job is taken back by its own chat turn when the successor's next
  batch calls `start_job` again, so the whole chain stays one single writer.

## Shared conversation delivery

- Chat: the bubble resolves at AUTO when the reply or routed acknowledgement
  exists.
- Browser and Coding Agent: the Chat acknowledgement resolves the immediate
  turn after durable handoff. Progress, questions, and completion return as
  correlated updates in the same conversation.

## Regression suite

`tests/e2e/test_cascade_blueprint.py`, `tests/e2e/test_browser_pressure_chain.py`
and `tests/e2e/test_agent_pressure_chain.py` drive real episodes through the
cascade on the harness substrate (`tests/helpers/blueprint_driver.py`) with only
the model seat and browser session stubbed. They lock: one bounded agent run per
browser episode whose screenshots never include the operator's tab; Omega's
reason becoming the next run's instruction; one batch per agent episode;
successors re-enter Global A with a new id; live browser evidence over memory;
the operator answered exactly once; all four dedicated lanes overlap; a second
Chat turn waits for Chat even after a different lane frees.

## Scheduled removal

`pressure_loop/agent_lane.py` still holds the retired motor-dispatch hand-off and
continuation minting (`should_hand_off`, `hand_off_motor_program`,
`maybe_mint_continuation`). Nothing in production calls them; their reader
seams (`restore_dispatch`, `carried_program`, `announce_completion`) remain only
so an envelope restored from an older crash can still find its work. They go
with the next boot-restore cleanup.

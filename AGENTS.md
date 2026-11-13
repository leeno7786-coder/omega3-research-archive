> **Archived document (November 2026).** This is the historical internal engineering contract for Omega 3.0 during its active development. Omega 3.0 is now a retired research platform. This document is preserved for technical provenance; present-tense imperatives here do not imply current maintenance, availability, or support. See [README.md](README.md) and [docs/RETROSPECTIVE.md](docs/RETROSPECTIVE.md).

# Omega 3.0 — THE LAW

**This document is law.** It is the single source of truth for working on Omega.

**If the system does not match this document, the system is wrong.** Do not
"correct" this document to describe current behaviour, and do not soften a rule
because code violates it. Fix the code. Known divergences are recorded in
[§10](#10-known-violations--the-system-is-wrong-here) as **work owed**, never as
an amendment to the law.

Supersedes `AgentsV2.md` and every `.kimi/context/*` file. Where they disagree
with this document, they are wrong.

---

## 0. Omega is a product — every instance belongs to someone else

Omega is bought and run by people who are not its authors. The operator on this
build is **one user among many**. Nothing may be tuned, hardcoded, defaulted, or
reasoned about as though this machine's owner were the only person who will ever
run it.

- **No person-specific defaults.** No baked-in timezone, work hours, inbox shape,
  contact list, calendar layout, household, or habits. Anything person-shaped is
  learned at onboarding or read from that user's own connected data — never
  assumed, never inferred from the development machine.
- **No developer's life in the fixtures.** A test that passes because it encodes
  the author's schedule, mailbox, or routine proves nothing about the product.
- **Judgment that varies between people is not Omega's to hold.** Where users
  reasonably differ — what may run unattended, what may be sent, what may be
  deleted, when it is acceptable to interrupt — Omega does **not** ship a built-in
  taxonomy deciding on their behalf.

### 0.1 Scope is set at the connection. Consent is final.

> **Once a user sets the scope, that is that.**

Safety boundaries and protocols are real and they are declared **at the connection
level** — when a user connects a provider bundle they set what Omega may do with
it, and every boundary is a **setting they can change**. Defaults ship
conservative. Defaults are not law.

A **grant** is narrower authority drawn from that scope for one standing job. A
grant can never exceed the connection scope it was drawn from.

- **No re-litigation at execution time.** An approval is the authority. Omega does
  not insert a second confirmation the user did not ask for, and does not withhold
  an in-scope action because it judged the consequence too permanent.
- **No undo assumption. Reversibility is not a category Omega reasons in.** Omega
  never treats an action as safe because it believes it can be taken back, and
  never blocks one because it cannot.
- **Consent binds to what was shown.** If the thing the user approved changes, the
  grant dies and must be asked again — the discipline `connectors/send_approval`
  already applies by binding a send token to the draft's *revision*.
- **Every boundary is reachable by the user.** A safety control that only a
  developer can reach — an environment variable, a config file on disk, a code
  constant — is **not** a boundary the user set. It is Omega deciding for them.
- **Asking again after a clear yes is a defect**, the same class as acting without
  one. Both substitute Omega's judgment for the user's.

---

## 1. Portability — absolute

Omega runs from the SSD. It does not touch the host machine.

- **No hardcoded paths.** Everything derives from `core.paths.OMEGA_ROOT`. No
  `F:\`, no `C:\Users\...`, no user-home assumptions.
- **No outside Python. Ever.** Only the following SSD-contained Python stacks
  may run Omega. Windows carries the brain stack and the operator-approved
  Vitis provider stack; Linux carries one self-locating interpreter:

  | Platform | Purpose | Interpreter |
  |---|---|---|
  | Windows | Brain runtime, tests, scripts, tooling | `.venv-brain\Scripts\python.exe` |
  | Windows | Brain portable base; never invoked directly | `python-3.13-embed\python.exe` |
  | Windows | Vitis provider child only | `.venv-vitis\Scripts\python.exe` |
  | Windows | Vitis portable base; never invoked directly | `python-3.12-embed\python.exe` |
  | Linux | Everything | `python-3.13-linux/bin/python3` |

  `.venv-brain` **is** a virtualenv built on `python-3.13-embed`
  (`pyvenv.cfg`: `home = ..\python-3.13-embed`,
  `include-system-site-packages = false`). They are **one stack**, not two
  options: the embed is the portable base, the venv is its package environment.
  Both live on the SSD.

  **Vitis has its own portable embed and venv (operator-approved, 2026-09-27).**
  The pinned `onnxruntime-vitisai` wheel targets CPython 3.12, so
  `.venv-vitis` uses `python-3.12-embed` with
  `home = ..\python-3.12-embed` and `include-system-site-packages = false`.
  `core.paths.vitis_python_path` derives its location from `OMEGA_ROOT`.
  The brain's local-services gateway launches `runtime.vitis_worker` there
  with `-I`; the venv's `._pth` supplies the SSD package paths and product root.
  This stack serves the Vitis provider child only. Brain cognition, the test
  runner and tooling continue to use `.venv-brain`; provider tests may launch
  the child through that same gateway contract. Neither stack uses host Python,
  host packages or an interpreter fallback.

  Linux has no embeddable distribution, and a venv's `pyvenv.cfg` `home` is
  absolute, so it would break when the SSD mounts elsewhere. Linux instead
  carries one self-locating CPython whose own site-packages is the package
  environment (`omega.pth` puts the product root on the path, as `..\..` does
  in the Windows venv's `._pth`). It is the only interpreter on Linux, and
  `core.paths.portable_python_path` resolves to it there.

- **Never invoke bare `python` / `python3`.** It resolves to a machine-local
  interpreter. Anything proven there proves nothing about the shipped product.
- **Never invoke either embedded base directly.** Launch the brain through
  `.venv-brain\Scripts\python.exe` and the Vitis provider child through
  `.venv-vitis\Scripts\python.exe`, so each uses its own isolated SSD packages.
- **No interpreter fallbacks.** A launcher that cannot find the venv **fails**.
  It never falls back to `python`.

**Verify before believing any test result:**

```powershell
.\.venv-brain\Scripts\python.exe -c "import sys,spacy;print(sys.prefix,spacy.__version__)"
```

A sudden cluster of `ModuleNotFoundError` or `NoneType` failures in code you did
not touch is an **interpreter question first** — not a code verdict, not an
environment verdict.

---

## 2. No fallbacks, no fail-safes, no backwards compatibility

There is one way to do each thing. The old way is dead, not deprecated.

- **No back-compat shims.** Everything behind the new system is deleted, not
  kept working. No dual paths, no "legacy" branch, no compatibility flag.
- **No silent degradation.** A component that cannot do its job **fails**. It
  does not return a default, an empty list, or `None` and let the caller carry on
  as if nothing happened.
- **No fail-safe fallbacks.** "Failure-soft so ingest doesn't crash" is how Omega
  learned nothing for weeks while reporting success.

### 2.1 The no-silent-empty rule

> **A component that cannot do its job MUST fail in a way its caller cannot
> mistake for a legitimate empty result.**

This is the most expensive defect class in this codebase. Every instance has the
same shape: **correct work discarded at a component boundary, with success
reported on both sides.** Each component was correct by its own contract; no
contract existed *between* them.

Measured instances:

- `parse_extract._nlp()` swallowed the spaCy load exception, destroying the
  cause; `extract_facts` returned `[]`; `enrich()` returned `None`; ingest did
  `enrich(text) or {}` and committed an empty `persona_stub` as
  `status=committed`. The operator was told the fact was remembered. Nothing was.
- Recall retrieved 13 candidates and admitted 4. Both sides reported success.
- The qwen bridge never populated `unresolved_blockers`, so the fold-back's
  `skill_success + blockers` branch — which existed and was correct — was dead
  code on the live path.
- The extractor produced the right university; ingest dropped it because the
  facet name was not registered. Neither side logged anything.

**Therefore:**

1. **"Could not run" and "ran, found nothing" must never share a return value.**
2. **The cause must survive** to whoever handles the failure. `except Exception:
   pass` destroys the only thing that makes a failure learnable.
3. **Contracts between components get tests.** If file A writes a format and file
   B parses it, a test asserts the round-trip. Drift on either side breaks a
   test, not a production run.

### 2.2 Failures are episode content, not exceptions to it

Anything the substrate fails at during an episode is **appended to that
episode** — exactly like a success. Both travel the same path:

```text
AUTO → Meta B → Global B → Omega → DeepSleep → memory
```

A failure is never swallowed, never logged-and-dropped, and never routed to a
side channel. It is **recorded to memory so the system can learn and adapt from
it.** Failures and successes are learned from in the same place, by the same
machinery, inside the same episode.

---

## 3. The cognition cycle — one cycle, no variants

```text
pressure → Global A → Meta A → AUTO → Meta B → Global B → Omega → DeepSleep → (repeat)
```

There is **one** cognition cycle. Not a user variant and an autonomous variant.
Not a 4-phase and a 5-phase form. One.

| Stage | Owns |
|---|---|
| **pressure** | The only forever loop, and the only thing that ignites an episode. Its next action is whatever **Omega** chose at the end of the previous episode. |
| **Global A** | Picks the **intent**. |
| **Meta A** | Chooses the **tool and its params**. |
| **AUTO** | **Executes** it. AUTO owns **every action**. |
| **Meta B** | **Reflects** over the outcome. |
| **Global B** | Reasons over Meta B's reflection and builds the **full episodic pack** for Omega. |
| **Omega** | **Episodic metacognition, every episode**, using different prompts for different actions. Writes its metacognition **and** Global B's pack to its journal. |
| **DeepSleep** | **Closes the episode**: writes everything to memory, reindexes the graph, runs memory maintenance, ends the episode. |

### 3.1 Responding is an ACTION

**AUTO owns every action, and replying to the operator is an action.** The reply
is emitted at AUTO. That is correct and must not be "fixed".

Omega thinks about the reply **through the memory tools, during the action** —
recall is part of producing it. It does not think about the reply afterwards in
order to produce it.

**Meta B, Global B, and Omega are cognition and metacognition ABOUT the action:**
the outcome, what was learned, what was not learned. They are **never in the
reply path and never gate it.** Moving the reply behind them would put reflection
in front of acting, which is backwards.

So a chat turn is not "terminal" because AUTO answered. The action is done; the
learning from it has not started. Omega still runs — see §3 non-negotiables.

Non-negotiables:

- **Omega runs on every episode.** No episode class skips metacognition; there is
  no terminal-response shortcut.
- **Omega chooses what happens next.** Pressure does not decide — it carries out
  Omega's choice from the previous episode.
- **DeepSleep is the terminus.** Memory writes, graph reindex, and maintenance
  happen there, and nowhere else, at episode end.
- **Pressure is the only episode trigger.** No other loop ignites work.
- **Meta A and Meta B stay deterministic** (`no_inference_in_meta`).
  Per-episode metacognition belongs to Omega, after Global B and before
  DeepSleep — never in Meta.
- Drive precedes goals. There is no `goal` / `goal_state` / goal-reflection in
  the v2 cascade.

---

## 4. Input — ONE GATE

### 4.0 The queue is the converging point

Operator, 2026-07-31: *"The queue owns the pressure lane. Every outside input
goes through the queue — no matter if we had 12 different pipelines, they all
pay toll at the queue. The queue is the converging point for everything to enter
the mind."*

The ingress gate is the converging point. Every external or machine-minted
episode request receives one global `arrival_seq`, a semantic lane, and a place
in that lane's durable FIFO. The four semantic lanes are exactly `chat`,
`browser`, `agent` (Coding Agent), and `window` (10-Window Reflection). Physical
pipeline IDs `0`, `1`, `2`, and `3` are permanently bound to those lanes in that
order. Each lane admits one active episode and preserves FCFS order within its
own line. Work in one lane never waits behind a busy different lane. Up to four
predictions may run at once, one per lane, subject to the configured runtime
capacity. No work preempts a running lane episode.

**`pressure_loop/cognition_queue.py` is the only door into cognition.** Operator
chat, voice, vision, uploads, connector and workspace observations,
system-message replies, onboarding, autonomous work, reflections, and durable
job continuations all pass the same ingress gate and are placed in a semantic
lane FIFO. Operator turns enter `chat`; Global A classifies inside that
pressure-opened episode. A specialist route durably appends linked work to its
target lane before AUTO acknowledges the operator. The target work starts as a
separate pressure-opened episode. Host-minted work must carry a validated lane;
unknown lane identity is an error. Each process-local pipeline lease belongs to
its permanently bound semantic lane and never borrows another lane's work.

**Rules:**

- **The queue is durable.** It persists to `guardian/state/` and survives a
  restart. Nothing in line is ever lost to a reboot.
- **Admission is strict FCFS per lane.** `arrival_seq` is one global positive
  integer and determines relative arrival order within each lane. There is no
  priority or preemption. A busy lane does not block admission or execution in
  another lane. If its lane is busy, new work stays durable in that lane.
- **Lease, don't pop.** Leasing assigns temporary process-local ownership; an
  item remains durable until its own acknowledgement boundary.
- **Acknowledge only your own item.** Acknowledgement uses the exact `queue_id`.
  A stale or synchronous ignition must never consume a different request.
- **A lane line IS pressure for that lane.** A lane FIFO non-empty means work is
  available for that lane's pressure worker. Empty means none. No TTL or decay
  may silently expire admitted work. Substantive cross-lane instructions remain
  until acknowledged or explicitly failed with their cause.
- **Transport is not ignition.** HTTP, WebSocket, CLI, or anything else carries
  a turn *to* the gate and carries the response back. **Nothing but the queue
  feeds pressure.** A transport that can ignite on its own is a second gate, and
  a second gate is the bug this section exists to forbid.
- **One converging point per boundary.** §4 is the single ingress gate into the
  *mind*, which routes into four separately ordered semantic FIFOs; §5 is the
  single door into *memory*. The shared gate provides one auditable admission
  authority without making the four execution lanes wait on one another.

### 4.1 Operator chat — one response rail

Chat is one of four permanent semantic lanes and the only operator-facing
conversation. Every HUD turn enters the durable Chat FIFO. Global A classifies
the intent inside its pressure-opened Chat episode. Ordinary conversation stays
in Chat; Browser, Coding Agent, and reflection work is durably handed to the
target lane. The handoff does not create another user-facing conversation.

- **A routed acknowledgement is an AUTO action result.** AUTO may emit a
  correlated `chat.reply` after the specialist handoff is durable. It resolves
  the immediate turn and states what is queued/running without claiming future
  work is complete. Later lane questions, progress, and completion use
  correlated `chat.update` events in that same conversation.
- The HUD binds its thinking block to the turn's `cycle_id` before transport.
  The server registers that cycle before queue admission, so an immediate AUTO
  result cannot race ahead of its subscriber.
- **Only the actual AUTO action result resolves the immediate turn**, carried
  as `chat.reply` from AUTO's durable response rail. Commands and remembers do
  not pre-deliver canned text and then report again later. A turn whose AUTO
  fails without a word is resolved with the failure; the bubble is never left
  to time out. Correlated `chat.update` events report later specialist work and
  do not reopen or replace the resolved turn.
- **Tools on demand (operator 2026-09-05).** Mistral's AUTO pass on
  every operator turn carries the bounded tool table
  (`auto_loop/execute/brain_skill_executor.py`: files, search, commands, web
  search and fetch, all jailed to the operator workspace). Mistral decides from
  the words whether to call one, the calls run inside AUTO, and the same
  interpreter writes the reply after the results return. No verb regex, second
  planner, or transport chooses a build path. Explicit learning and remember
  requests remain deterministic speech acts, not worker selection.

---

## 5. Memory — ONE LANE

### 5.0 The single memory lane

**All incoming memory goes to SRAM on intake. DeepSleep routes it through
ingestion. That is the only way memory enters Omega.**

```text
intake (everything) → SRAM
                        │
                        ▼   (DeepSleep, once per episode, one cycle)
                    ingestion → filesystem → reindex graph from filesystem → maintenance
```

Three organs, three jobs, no overlap:

| Organ | Job |
|---|---|
| **SRAM** — the episode bucket, `memory/episode_sram.py` | **Intake + working memory.** Everything enters here and is live for the rest of the episode. |
| **Filesystem** (Dewey, `memory/000_identity/` … `1100_dev/`) | **TRUTH.** Canonical. Written by DeepSleep, from SRAM, through ingestion. |
| **Graph** (`graph.sqlite`, `coord_index.json`) | **RECALL.** Reindexed by DeepSleep from the updated filesystem. |

> ⚠️ **"SRAM" names two different things in this codebase. Do not confuse them.**
>
> | | |
> |---|---|
> | **Episode SRAM** — `memory/episode_sram.py` | The **intake bucket**. This is the SRAM this section legislates. Holds fresh memory *in the episode*, flushed by DeepSleep. Operator design 2026-07-16. |
> | **DME SRAM** — `memory/dme/` (`sram.bin`, `rebuild_sram_from_graph`) | A **recall projection** — the coordinate matrix compiled from the graph. Downstream of truth, read-only, correctly derived. Nothing in §5 asks this to change. |
>
> The lane is: intake → **episode SRAM** → DeepSleep → filesystem → graph →
> **DME SRAM**. One is the front door, the other is the search index at the back.

**Why this is the law:** one lane is **auditable** — every memory has exactly one
path, so any record can be traced from intake to disk. One lane is
**deterministic** — one order, every time. And one lane means there is never a
reason to work around, under, over, or through the memory system. If a design
needs a side channel, the design is wrong.

Consequences that are law:

- **No direct-to-filesystem writes.** Nothing skips SRAM on the way in.
- **No process-local state carrying memory between phases.** If it needs to
  survive to the next phase, it is in SRAM.
- **No second ingestion path**, no bypass for "internal" or "fast" writes.
- **DeepSleep does the whole cycle in one pass, every episode**: drain SRAM
  through ingestion → write the filesystem → reindex the graph from the updated
  filesystem → memory maintenance → close.
- **SRAM clears on drain.** It is a buffer, not a cache. Once the content is in
  memory, SRAM holds it no longer.
- The graph is reindexed **from the filesystem**, never from SRAM directly.
  Truth flows one way.

### 5.0.1 Recall reads the PAST. SRAM is the PRESENT turn.

**Recall tools run at AUTO — before DeepSleep consolidates.** They read the
filesystem and graph: memory as it stood **before this turn**. They do **not**
read SRAM.

This is the whole point of the ordering:

> **Omega must never tell you it remembers something you just taught it.**

If intake were consolidated before recall ran, Omega would recall the operator's
own sentence back as a memory and report that it already knew. The fact would be
laundered from input into recollection inside one turn. So the fact sits in SRAM,
unrecallable, until DeepSleep writes it — and the **next** episode's recall finds
it honestly.

Two different reads, two different questions:

| Read | Source | Question |
|---|---|---|
| **Recall tools** (AUTO) | filesystem + graph | "What do I already know?" |
| **Omega's episodic cognition** | SRAM | "What happened in this episode?" |

SRAM is **not a recall source**. It is this episode's working state — which is
what lets Omega judge a build report and inject it into a re-execution inside the
same episode (§7.1) without any stash, while recall stays honest about the past.

### 5.1 Rules

- **All intake routes through the memory gates.** No writes that skip them.
- **`write_memory` is atomic.** No staged limbo, no partially-committed record.
- The canonical memory record is a **whitelist**. Unlisted fields do not persist.
- **Dewey coordinates** (`memory/dewey_map.py` is the single source of truth):
  `0` identity · `1` persona · `2` knowledge · `3` reasoning · `4` situational ·
  `5` skills · `6` journal · `7` vision · `8` coding · `9` temporal ·
  `10` workflows · `11` dev.
- `NON_RECALLABLE_LOBES = {6, 7}` and `ENTITY_BEARING_LOBES = {1, 4}` — import the
  constants; never hardcode the sets.
- **Lane discipline:** every response type lives in exactly ONE lobe.
- **Determinism on read/write for all intake** (operator chat, autonomous
  learning, work artifacts): recall exactly what was ingested with ledger/file
  proof, or say "I don't know". Never hallucinate unverified memory.
- **No auto-wipes on boot.** Day zero happens only via `scripts/day_zero_reset`.
- **Time is first class.** `prepend_time_line` opens every model pass.

---

## 6. Guardian — the boundary, and nothing else

> **Guardian arbitrates UNTRUSTED EXTERNAL HUMAN-AUTHORED INGRESS. Nothing else.**

Two ingress classes only:

1. **User chat.**
2. **Connector / workspace observations** — calendar text, task notes, email
   bodies, document text, filenames, comments, revisions. Provider authority
   proves what *exists* externally; it never makes human-authored *content*
   trustworthy. Provider factual metadata (object id, revision, timestamps,
   delivery state) is authoritative only while it holds a structural shape
   (token / timestamp / URL, validated per field). Every other external string is
   screened default-untrusted. **Field names are never a trust boundary**, and
   even ALLOW ≠ trusted — external strings compose attributed and `harden()`-fenced,
   never bare.

**The mind's writes to its own canonical memory are DIRECT** —
`write_memory(propose=False, bypass_guardian=True)`. Episodic capture,
situational (4), temporal (9), learned knowledge, identity/traits, consolidation:
every cognitive self-write bypasses Guardian. Guardian shields Omega from
*outside* input; the mind writing to itself is not outside input.

Guardian is **out of the memory read/write path**. Recall, graph projection, and
all cognitive memory writes are Guardian-free.

Guardian **is** the sole writer of the tool registry; `tool_registrar` is the
single approval entry for all tools.

**Lessons, failures, and everything the system learns from go to episodic and
temporal memory — never to Guardian logs.** Guardian logs are audit, not
cognition.

⚠️ `memory/README.md`'s "Guardian is memory's sole-writer gatekeeper" framing is
**WRONG** and keeps causing this error to be re-made. `temporal_store.py`'s
direct-write pattern is the correct reference.

---

## 7. Execution and skills

- **Skills are modifiers/enhancers, never law.** A skill must never determine a
  build's outcome. Skills are gated by **read/write permission** —
  `execution_kind` is a permission axis, not a verdict.
- **The bar comes from the mind.** Omega authors
  `outcome_contract.acceptance_criteria` per objective, and it is rendered into
  the agent's brief **before** it starts. The agent sets a bar and works to it.
- **A shortfall is a report, not a failure.** The agent states which criteria it
  missed and why; that becomes `unresolved_blockers`, forcing `satisfied=False`
  and handing the mind the decision — retry, change approach, or stop. Never an
  unbounded retry loop.
- **Tool dispatch is deterministic.** The voice model owns tool calls.
- **GBNF for all schema-constrained generation.** Model-JSON parsing is
  repair-tolerant and fail-closed — never brick.
- Execution skills (`SKILL.md` catalog) are separate from the verified memory
  graph and do not pass through RecallVerifier gating.

### 7.1 One configured model, four lane learning circles

The configured model/runtime serves Chat, Browser, Coding Agent, and 10-Window
Reflection through four permanent semantic pipeline bindings. The same model
may hold up to four concurrent predictions, with at most one active prediction
per lane. A lane's FIFO serializes its own work while the other lanes proceed.
No second model plans, interprets, or owns a worker. The validated semantic
`lane` selects its fixed worker and travels with the episode for attribution.
`work_kind` carries descriptive execution context.

Global A routes operator intent inside the pressure-opened Chat episode. The
handoff to another lane is persisted before AUTO acknowledges. Each target lane
then starts through pressure and receives cross-lane mail in its own Omega
prompt at the next eligible fold. Incoming mail never mutates a running model
request. Chat remains available for the shared operator conversation and
publishes later lane updates with the original conversation and work identity.

**1. Tool execution writes dev projects to the dev lobe (11) AS IT BUILDS** —
not only at the end. Artifacts land in memory continuously while work is
happening.

**2. When the run has run its course, an execution report comes back.** It
carries:

| | |
|---|---|
| the build task | what it was asked to do |
| the pass gate | the acceptance criteria **Omega** set (§7) |
| what it did | |
| what it didn't do | |
| what succeeded | |
| what failed | |

**3. The report hits SRAM.** It is a real memory object in working memory — not a
process-local stash, not a side channel.

**4. Omega judges it.** If Omega is not satisfied with **what** was executed or
**how**, it chooses to re-execute.

**5. On re-execution, that same SRAM memory is injected into the prompt**, along
with **why** it is being re-executed and **what Omega wants accomplished**. The
Mistral AUTO interpreter receives the complete context; no hidden model session
is treated as memory.

**6. That SRAM memory also goes to DeepSleep**, where it is written to canonical
memory with everything else.

That is the circle: tool execution → report → SRAM → Omega judges → (re-execute
with the report injected) → DeepSleep → memory.

**Consequences that are law:**

- The execution report is a **memory record**, not a process-local carrier. It
  lands in SRAM on intake like everything else, Omega reads it there as this
  episode's working state (§5.0.1), and DeepSleep persists it.
- Durable job revisions, stop requests, attempts, ownership, and dispositions
  are **execution control state**, not canonical memory. They neither form a
  second scheduler nor bypass the target lane FIFO. What the mind learns from
  an execution still enters through the execution report in SRAM and reaches
  canonical memory only through DeepSleep.
- **Project ownership (operator-approved 2026-09-06).** Before mutable work,
  Mistral selects a concrete project with `start_job(project_root)` through the
  existing AUTO tool table. Chat/read-only work needs no writer. Equal,
  ancestor/descendant, or physical-alias project scopes cannot have competing
  active writers. Conflict is a tool result naming the owning job so the same
  interpreter can choose `revise_job`; it is not a classifier or scheduler.
  Existing unbound jobs require explicit `start_job(project_root, job_id)` by
  their genuine current owner; no objective-text inference or boot rewrite.
  File targets, restore destinations and command cwd are checked against the
  selected project and original authority. This is cooperative Omega resource
  coordination, **not an OS filesystem sandbox**: arbitrary scripts retain the
  operator's existing OS permissions and may escape their declared scope.
  Command availability is unchanged; no new shell classifier, host ACL,
  dependency or whole-workspace serialization is implied.
- A re-execution prompt is assembled from the SRAM record **plus** Omega's reason
  and intent. It never assumes the interpreter retained anything.
- The report must be **complete enough to restart cold**. If a fresh agent could
  not resume from it alone, the report is wrong.

---

## 8. Engineering discipline

**TDD is the iron law.** No production code without a failing test first. Watch
it fail *for the right reason*. RED tests assert the configured value or the
fail-mode — never a tautology.

**Verification:**

```powershell
.\.venv-brain\Scripts\python.exe -m pytest tests/ -q
.\.venv-brain\Scripts\python.exe -m ruff check <touched files>
```

- Ruff must be clean on every touched file. Any finding blocks.
- Full suite green before claiming done. Report real numbers; if something
  failed, say so with the output.
- **Contract-first is the anti-drift gate.**
- **Live code on disk is authority.** Do not trust docs, READMEs, or archived
  agent files when they conflict with the filesystem — except this document,
  which is law over both.
- Match the operator's minimal intent. Do not scale work up unasked.
- Do not rebuild loops; swap phase-module logic.

**Git:**

- **NEVER `git add -A` or `git add .`** — explicit pathspecs only; multiple agents
  work this repo concurrently.
- Secret-pattern grep before every commit.
- Push to a **branch**, never main. No merge to main without operator sign-off,
  and do not push until the operator signs off.
- Commit trailers:
  ```
  Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>
  Claude-Session: <session url>
  ```

**Windows operations:**

- Long runs: PowerShell `Start-Process` **detached**. Never Bash
  `run_in_background` — the harness reaps it mid-run with no error.
- Live monitoring is **single snapshots**. No polling loops.
- Kill leftover process trees after every run. Never kill the MCP servers.
- Connector tokens and secrets live only in the AES-256-GCM vault.

---

## 9. Reporting

State what is **verified**, what is **inferred**, and what is **unknown** —
separately, never blurred.

If a diagnosis rests on an assumption, check the assumption **before** reporting
the diagnosis. A confident wrong answer costs more than a slow right one.

---

## 10. Known violations — the system is wrong here

Verified divergences from this law. **Work owed, not exceptions.**

| # | Violation | Law |
|---|---|---|
| 34 | **The HUD's speech transport is still named for the retired engine.** `hud/ui-src/core/voice-runtime.js` defines `KokoroTTS` / `warmupKokoroTTS` and publishes them on `window`; `voice-live.js`, `settings-theme.js`, `speech-text.js` and `chat-feed.js` call and cite that name; the build carries a `voice-kokoro` inject slot and a `SECTION:kokoro` marker (`manifest.json`, `scripts.html`, `hud/scripts/build_hud.py`). The object itself is correct — it is the live Qwen transport — but the name describes a product Omega does not ship, and three tests *require* it (`tests/hud/test_hud_foundation.py` asserts `"KokoroTTS" in html`, `tests/hud/test_kokoro_tts_cancellation.mjs` slices the section by that identifier, `tests/core/test_architecture_map_citations.py` asserts `"kokoro" in text`). Measured 2026-09-20: the Python speech surfaces were cleared in the same pass; this rename was left because it spans ~20 files, the build manifest and its own test pins, and was not in the operator's ask. | §2 — the old way is dead, not deprecated |

**Numbering is stable. Rows keep their number for life** — a closed row moves to a
Closed section, it is never reused and the survivors are never renumbered, because
the numbers are cited from other rows and from commit messages.

> ⚠️ **#17 and #18 are unaccounted for.** They were removed from this table with no
> Closed entry, and #19's text cited "a field #17 can destroy" — so #17 existed and
> said something about `cycle_id`. Whatever they were is lost. Under the preamble
> that is a silent amendment, which is the exact failure §10 exists to prevent. If
> you know what they were, restore them.

### Closed — one mind, four dedicated lanes, 2026-09-26

| was | what it was | how it closed |
|---|---|---|
| #35 shared interchangeable FIFO owned lane selection | The queue leased its global head to any free pipeline, so semantic work could collide and a held lane could block unrelated work. | `cognition_queue.py` now persists semantic lane identity and leases only the FIFO for each pipeline's fixed lane. Chat Global A routes Browser/Coding Agent requests through durable lane handoffs; runtime admission allows four predictions with one per lane. Cross-lane mail is settled against queued handoffs so an update consumed by an active fold cannot start a duplicate episode. Verification: 393 focused lane/mail/runtime/browser/agent/queue tests and two Browser/Coding Agent mail-absorption tests passed; Ruff is clean. The full suite is still running and live LM Studio capacity remains unverified. |

### Closed — live contract repairs, 2026-08-03

| was | what it was | how it closed |
|---|---|---|
| #27 bounded deferred-embed prefix | DeepSleep processed only eight queued embeddings and knowingly closed with the remainder pending. | DeepSleep sizes 32-record batches to the backlog present at the terminus and drains it before reindex. Live episode `db5980f9-aac1-42c1-a53b-3b23fea4d4e9` drained 59 records as 32 + 27, reported `still_pending=0`, rebuilt the coordinate index from 208 to 218 entries, and then closed. |
| #28 dense embedding failure / second route | The memory provider registry and runtime embedding API could collapse provider failure into an absent vector or create a second gateway route. | The dense memory stack (`memory/local_embed.py`, `memory/semantic_state_engine.py`, `memory/embed_backfill.py`, `memory/embed_contracts.py`) and `runtime.client.embed()/embed_batch()` were deleted. Live semantic perception is the in-process Ettin retina, candidate-only and no gateway fallback. Retirement and native-memory coverage is green. |
| #30 queue head survived until close | An admitted turn stayed in the durable queue throughout cognition, allowing post-open replay. | The exact leased head is acknowledged immediately after `EpisodeStore.open` succeeds. Live cycle `151ede08-699b-4703-9f83-f87fa5a4de07` had an empty persisted queue while AUTO was still executing inside its open episode; active-episode ownership continued to hold later turns outside cognition. |
| #32 shutdown leaked the dev bridge | Repository shutdown reported success while the boot-owned Bun listener remained on `:8092`. | Shutdown now sweeps and verifies the owned TypeScript bridge after stopping the substrate parent, and a surviving bridge makes the command fail honestly. Live shutdown reaped Bun PID 18304, swept the runtime, cleared the boot lock, returned exit 0, and left ports 11434, 13310, and 8092 clear. |
| #33 connector scope was developer-owned | Connector authority, schedules, and vault unlock were hidden in environment flags or an adjacent keyfile, so the operator could not set the connection boundary. | The HUD now owns capability scope, action/send/autonomous authority, reminder and sync cadence, and idle wake cadence through a strict persisted connection contract. Runtime policy, calendar wake, sensory sync, and OAuth all consume that contract; the retired flags are ignored. The vault is unlocked only by a user passphrase whose derived key lives in process memory, legacy keyfiles migrate once and are removed, OAuth asks only for selected scopes, and disconnect revokes provider authority before clearing local credentials, settings, and sync state. Verification: 4,909 passed, 2 skipped, 1 xfailed; Ruff and the generated-HUD check are clean. |

### Closed — required substrate failure attribution remediation, 2026-09-11

| was | what it was | how it closed |
|---|---|---|
| #23 required substrate and terminus failure-soft behavior | Required boot, HUD, taxonomy, and DeepSleep seams could report a degraded or absent result while the surrounding system continued as though readiness had succeeded. | Required work now raises attributable `RequiredSubstrateError` failures. HUD spawn returns `None` only for the explicit operator disable policy; taxonomy compilation rejects an absent snapshot; boot and DeepSleep preserve runtime, memory, projection, Guardian, recovery, lifecycle, compaction, and maintenance causes. Optional telemetry-only emission remains best-effort and is not a readiness result. Verification: 31 focused substrate/HUD/taxonomy/DeepSleep tests passed; full-suite verification is run for this release. |

### Closed — reflection-window control-state remediation, 2026-09-11

| was | what it was | how it closed |
|---|---|---|
| #26 reflection-window episode payload | `guardian/state/reflection_window.json` was a second durable episode-state store because `EpisodeStore.close` persisted full episode bodies and mastery updates through the reflection checkpoint. | The checkpoint now has an explicit control-state whitelist containing only window metadata and episode references. Episode bodies and mastery effects remain transient hydration caches sourced from canonical lobe-9 records; legacy payload fields and malformed active/staged roots fail hard instead of being silently accepted as an idle lane. Verification: 77 reflection/window/temporal/topic tests passed; Ruff is clean. |

### Closed — coordinate-index availability and publication remediation, 2026-09-11

| was | what it was | how it closed |
|---|---|---|
| #25 coordinate-index recall/publication | Recall could serve a stale in-process `COORD_INDEX` when the file was missing, unreadable, malformed, or replaced without a detectable timestamp change; reindex could otherwise publish a partial projection after a record or schema failure. | `load_coord_index` now clears the trusted snapshot and raises `CoordIndexUnavailable` for every unreadable or invalid state, and its cache identity includes nanosecond mtime, size, change time, and file identity so timestamp-preserving replacements reload. Reindex rejects schema/unreadable records before atomic publication and writes profile-relative paths. Verification: 11 coordinate-index availability/concurrency tests passed; Ruff is clean. |

### Closed — cognition context gate remediation, 2026-09-11

| was | what it was | how it closed |
|---|---|---|
| #29 reduced cognition context fallback | A failed 65,536-token cognition load was retried at a smaller context and reported as success, silently changing the configured cognition contract. | `ensure_cognition_runtime` makes exactly one load attempt at `COGNITION_CTX_PREFERRED` and raises `RuntimeCognitionUnavailable` on failure. Verification: 6 cognition-runtime-gate tests passed; Ruff was already clean for the touched runtime module. |

### Closed — portable release path remediation, 2026-09-11

| was | what it was | how it closed |
|---|---|---|
| #24 host-bound portable artifacts | The portable tree contained host drive/profile references, host interpreter fallbacks, and runtime/model paths that could fail after the SSD moved. | Runtime and launcher paths now derive from the active product root; the embedded interpreter/venv contract is enforced; coordinate-index writers emit profile-relative paths; retired host MCP/config artifacts are absent; and local/cloud/LM Studio launchers resolve from the SSD tree. Verification: 61 portability/interpreter/launcher tests passed; the live shipped-source scan found no host-bound path literals in executable artifacts. |

### Closed — temporal projection authority remediation, 2026-09-11

| was | what it was | how it closed |
|---|---|---|
| #31 invented historical projection timestamps | Historical DME projections contained content-hash pseudo-dates that could contaminate temporal recall because their real event times were unknowable. | Canonical records now use observed timestamps or explicit unknown, the migration tool detects and invalidates exact legacy signatures without inventing replacements, and the live DME rebuild carries observed timestamps only. Verification: migration dry-run matched 0 live canonical records; all 7 live SRAM timestamps were finite observed 2026 values; 15 temporal-authority/DME/migration tests passed. |

### Closed — law-baseline remediation, 2026-08-03

| was | what it was | how it closed |
|---|---|---|
| #1 terminal response skipped Omega | `RESPOND` could end the cascade after AUTO. | The terminal-response gate was deleted. Every successfully opened episode now folds through Meta B, Global B, Omega, and DeepSleep; response and document-creation contracts pin the path. |
| #4 extractor outage looked empty | spaCy load failure became `[]` and then `None`. | Extraction now preserves the cause as a hard failure, and persona ingestion tests distinguish "could not run" from "ran, found nothing". |
| #5 gates wrote after enrichment failure | `_enrich` caught every decomposer exception and committed anyway. | Enrichment failures now propagate across the gate boundary; a failed component cannot produce a successful canonical write. |
| #6 empty persona stub committed | A stub with neither text nor fact records reported `status=committed`. | Persona intake rejects an empty result and the episode-SRAM flush reports the rejection instead of creating a record. |
| #7 ack preceded the write result | Classification alone selected a success acknowledgement. | The acknowledgement response path was retired. Every HUD turn now stays open for the actual AUTO result on `chat.reply`; admission cannot claim success or consume the bubble. |
| #9 superseded guidance remained live | `AgentsV2.md` and `.kimi/context/*` still competed with this file. | The obsolete files were deleted and remaining historical references are explicitly non-authoritative. |
| #10 process-local build stash | `pending_build_resume.py` carried build state outside memory. | The stash was deleted. Build objectives and complete execution reports are episode-SRAM records used for judgement, cold resume, re-execution, and DeepSleep persistence. |
| #11 execution report missing | Fold-back had only an in-process result plus digest. | `memory/execution_report.py` defines the restart-complete record: task, pass gate, did/didn't, succeeded/failed, blockers, artifacts, and evidence. Its full body survives the canonical whitelist. |
| #12 dev artifacts arrived only at fold-back | Workspace mirroring occurred after the build rather than during it. | Builder lifecycle events now intake dev-lobe artifact snapshots as the run progresses; execution reports reference the persisted snapshots. |
| #13 memory lane was not universal | Many producers called the canonical writer directly. | `memory.api.intake_memory` is the universal front door, while `write_memory` is guarded as a DeepSleep-drain operation. Intake classes now enter episode SRAM before canonical persistence. |
| #14 consolidation was split across tails | Workflow writes and build resume occurred outside the single terminus pass. | DeepSleep now owns the ordered SRAM drain, filesystem writes, graph rebuild, maintenance, and close. The tail-side consolidation/resume state path was removed. |
| #20 connectors bypassed the queue | Sync wrote observations before any episode opened. | Connector/workspace observations enqueue durable operator-pressure items and wait FCFS at the same lease/ack gate as every other external input. |
| #21 reply shape dropped persona facts | `ack`/`question`/`reasoning`/`temporal`/`command` labels suppressed fact extraction. | Persona extraction is driven by eligible content, not response shape; labelled self-description contracts now persist their lobe-1 leaves. |
| #22 ingress handlers destroyed causes | Lost approval/question state silently rerouted the turn. | Those handlers now preserve and propagate the state failure; they do not reinterpret the message through a different lane. |

### Closed — no-silent-empty at the flush seam + the interpreter boundary, 2026-08-01

| was | what it was | how it closed |
|---|---|---|
| #15 flush result dropped | `_flush_episode_sram` returned a dict carrying `errors` (it raised) and `skipped` (it was rejected); `deepsleep_loop/loop.py:494` called it **bare**. Telemetry reported `ingested`/`turns`/`errors` but never `skipped` — so the measured `no_committed_persona_records` empty-stub case was invisible on every channel. | The return is assigned and rides the episode out in `projection_state["episode_sram_flush"]`; `skipped` joins the emission and the summary counter. Contract pinned by `tests/core/test_episode_sram_wiring.py`. |
| the shape drifted between producers | Three `flush`/`_flush_bucket` return paths omitted `skipped` entirely and a fourth set it to the **string** `"episode_mismatch"` while every other path yields a list of dicts. A consumer could not read `result["skipped"]` without a shape check. | One producer, `memory/episode_sram._empty_result()`. Every path supplies every key; `episode_mismatch` is now a normal `skipped` entry. |
| #2 launcher fell back to host Python | `scripts/hud-only.bat:34` — `if not exist "%PY%" set "PY=python"`. A missing venv silently ran whatever the host had. | The launcher **fails** with the path it wanted. Operator, 2026-08-01: *"if it doesn't boot inside the venv-brain using the python embed, then it doesn't boot."* |
| #3 embed reached host site-packages | `python-3.13-embed\python313._pth` ended with `import site`, so invoking the embed directly fell through to a machine-local user site-packages. | Line removed. Verified: the embed no longer imports `site` and cannot import `spacy`; `.venv-brain` is untouched (spacy 3.8.14, `sys.prefix=.venv-brain`) because `.venv-brain\Scripts\python313._pth` is a **separate file** that keeps its own `import site`. Both directions are now guarded by `tests/core/test_no_outside_python.py`. |
| #8 README contradicted §6 | `memory/README.md` framed Guardian as memory's sole-writer gatekeeper. | Already corrected before this pass; `memory/README.md:70` now states Guardian gates external ingress only, never the mind's own writes. Row was stale. |
| #16 dead `pending_operator_requests` | The unreachable `if active_episode:` branch in `pressure.py`. | Already deleted before this pass (2e7694a7). Only a doc reference and a test docstring still name it. Row was stale. |
| #19 chat had no lane | The `:13317/omega/bridge` socket had no server behind it; chat was `POST /api/chat` + polling. | Already built before this pass: `hud/server.py:5319` `@app.websocket("/omega/chat")`, `hud/chat_socket.py`, `hud/ui-src/core/boot.js:476`. Row was stale. |

### Closed — §4.0 gate, 2026-07-31

Kept as a record of what the law bought, and so the shapes stay recognisable.

| was | what it was | how it closed |
|---|---|---|
| second gate | `LAST_INGRESS_PRESSURE` — a single SharedState slot published before the enqueue and rebuilt into a contributor by `gather_contributors`. It ran **newest-wins** against the queue's oldest-first. | Deleted: both writers (`publish_operator_ingress`, and `publish_ingress_pressure`, which had zero callers anywhere), all three readers, the key, and the config/policy entries naming it. Three production rails already ignited through the queue alone, which is why nothing broke. |
| queued turn discarded | `_apply_force_dominant` kept the mirror (gathered first) and `continue`d past the leased head, losing `chat_intent`, `operator_reply_delivered`, `chat_bubble_waiting`, `system_query_*` on every ordinary send. Regression from `ef116281`. | Gone with the mirror — with one source there is nothing to collide. |
| two dedup keys | `pressure._dedup_key` keyed on **message**; `operator_queue._box_dedup_key` on **`system_query_id`/`cycle_id`**. Pin selection and acknowledgement disagreed about which turn they held, so **every chat answer to a system query ran cognition twice**. | One authoritative identity: `_dedup_key` delegates to `_box_dedup_key`. Message-first was independently wrong — "yes"/"ok" collapsed distinct turns. |
| silent-zero pressure | `read_operator_message` derived pressure from the mirror under TTL decay with **five** `return 0.0` branches, and its 60 s TTL expired during any longer episode — so a turn still in line reported as no operator input, and metadata survival became a function of wall-clock episode duration (which confounded every measurement over the chat path). | **The line is the pressure**: ask the queue whether a head exists. ~44 lines of clock math and all five silent-zeros deleted. |
| order was not arrival order | Chats run concurrently on threadpool threads, each doing a ledger write, a Guardian screen and a 15 s classifier call **before** enqueue; the box stored no sequence. FIFO was completion order. | `arrival_seq` stamped in the route handler on the event loop, before the threadpool dispatch; the box inserts on it. |
| pause discarded the turn | Chat was rejected after its SourceLedger citation was already written — recorded as received, then evaporated. | A pause **holds the line**: enqueue, then report `substrate_paused`. |
| the durable box was not durable | `_persist` used a bare `write_text` (truncate in place), and `_ensure_loaded` set `_LOADED=True` *before* reading — so a corrupt file read as an empty line and the next enqueue wrote that emptiness back over it. | Atomic temp-file + `os.replace`; an unreadable line raises `OperatorQueueUnreadable` instead of presenting as empty. |
| anyone could join the line | `_enqueue` admitted on `type` alone, so an Omega- or model-authored contributor typed `operator_query` entered and was held in SRAM as `speaker="operator"`. | Source guard at the single choke point, matching `omega_levels.py:133`. |
| the queue was an inbox, not a gate | "An episode is running → the line holds" was true only because the pressure loop is single-threaded and `ignite_episode` runs the whole cascade inline, so the thread physically could not lease again. Anything that made ignition asynchronous would have silently stopped the line being a line. | **A lease is now a CLAIM.** `lease_next_operator_input` returns `None` while one is outstanding — the gate, enforced by the queue itself whatever the threading model does. `has_waiting_operator_input` is the non-claiming read the pressure field uses, so asking "is anyone in line?" never takes their place. |
| a failed episode ate the turn | The head was acknowledged at episode **open**, so any cascade failure after that point closed with `cascade_status='failed'` and the turn was gone — no re-queue, no answer, no retry. `acknowledge_admitted` also re-raised **outside** the `try/finally`, leaving an episode open and unacked so every later `EpisodeStore.open` raised and the loop died. | Acknowledge moved to **close**, in the `finally`: a completed episode consumes the turn, anything else releases the claim and the turn keeps its place at the head. An ignition that opens no episode releases in `loop.py`'s own `finally`, so the gate can never fail **shut** — which would have been worse than the accident it replaced. |
| a claim could outlive its process | — | The **line** is durable; the **claim** on it is not. The lease is process-local and never persisted, so a restart always reopens the gate. |

---

## 11. Repository map

```text
boot.py                  entry point → pressure_loop → cognition_chain
core/                    Omega routing / boot / substrate ONLY
pressure_loop/           the only forever loop; EpisodeStore
global_loop/             Global A (intent) + Global B (episodic pack)
meta_loop/               Meta A (tool + params) + Meta B (reflection); deterministic
auto_loop/               AUTO — execution, promptpack, scaffold composer, skills
omega_loop/              Omega — episodic metacognition, motor programs
deepsleep_loop/          episode close, consolidation, memory maintenance
memory/                  gates, engine, enrichment, taxonomy, recall, dewey map
guardian/                external-ingress boundary + tool registrar
runtime/                 THIN CLIENT to omega-runtime and local model controls
schema/                  canonical dataclasses and contracts
connectors/              external providers (vault-backed auth)
mcp_platform/            signed local-first first-party MCP host, authority, ingress, SDK, and open inference seam
hud/                     operator surface — source in hud/ui-src/, built bundle
tests/                   mirrors the package layout
```

`brain/`, `autonomous/`, `handlers/`, and `hud/ui/` are **retired**. Do not
resurrect them.

---

## 12. Operational facts

- **Mind backend:** one backend-neutral cognition route serves every mind/loop
  inference. The operator selects `auto`, `local`, or `cloud` through the mind
  connector and supplies the provider/model/credential required by that backend.
  Omega ships no default language-model weights and never hardcodes Mistral,
  Qwen, or another provider as the mind. Live chat owns agent execution through
  AUTO's bounded native tools; there is no second agent bridge.
- **HUD:** source is `hud/ui-src/` (`core/`, `panels/`, `shell.html`), built by
  `hud/scripts/build_hud.py` into `hud/omega_interface.html`, served at `GET /`.
  Never edit the bundle directly. Verify with `build_hud.py --check` and
  `tests/hud/test_hud_ui_modules.py`.
- **Voice:** TTS `qwen` (`config/tts.yaml`; default speaker `Aiden`, 24 kHz, streaming,
  Vulkan sidecar), STT `parakeet_tdt_06b_v3` (CrispASR, CPU). **Qwen is the only
  supported speech engine** — `runtime.local_services.create_tts_service` raises for any
  other configured value, and `runtime.tts.status` reports `tts_engine_mismatch` rather
  than accepting a substitute. Piper/Jarvis, Whisper and Kokoro are retired; legacy
  Kokoro voice ids migrate once to Qwen speakers in `hud/settings.py` and are never
  offered. The HUD voice toggle gates both halves — pill OFF means no STT in AND no TTS
  out. **Operator-facing engine names are read from `runtime.tts.status`, never
  hardcoded** — a spelled-in name cannot be wrong about a swap it never reads, which is
  how the retired engine outlived its code in three telemetry payloads and the launcher's
  setup copy. The HUD's client-side transport object is still *named* for the retired
  engine; see [§10 #34](#10-known-violations--the-system-is-wrong-here).
- **Model residency law — runtime by default; semantic-retina exception**
  (operator amendment, 2026-08-16; supersedes the 2026-08-01 rule only for the
  memory semantic retina). The sibling `omega-runtime` owns optional user-provided
  backend sessions plus the shipped modality assets; `runtime/` here remains an
  HTTP client for those services. **The frozen memory semantic retina is the explicit
  exception:** it is loaded and executed brain-side by the Omega3.0 memory system
  as one bounded resident session. The retina has **no HTTP, socket, IPC, or
  omega-runtime fallback path**. Its weights/tokenizer/pooling/output contract are
  immutable and generation-pinned; failure makes the semantic-retina lane
  unavailable rather than routing to another model. This exception does not
  authorize in-brain loading of LLM, TTS, STT, vision, or other general models.
  The retired `runtime/local_tts.py` Kokoro failure remains the reason for that
  broader rule. Audit `InferenceSession` uses before shipping and allow only
  explicitly ratified brain-side appliances such as the semantic retina.
- **Config/policy YAML are governed mirrors.** When YAML and live loop code
  disagree, code in `*_loop/` wins.
- **graphify:** knowledge graph at `graphify-out/`. For codebase questions run
  `graphify query "<question>"` first; `graphify path "<A>" "<B>"` for
  relationships, `graphify explain "<concept>"` for focused concepts. Dirty
  `graphify-out/` files are expected and are not a reason to skip it.

---

## Learned User Preferences

- Prefer Gemini/Copilot-class setup: a connect button and guided onboarding, not dense checkbox/dropdown forms; let users choose onboarding depth during setup.
- When cloud and local branches diverge, reconcile and verify on the local SSD checkout rather than continuing from a cloud worktree.

## Learned Workspace Facts

- Public product domain for OAuth/branding verification is https://omega2ai.com; the privacy policy must be reachable at https://omega2ai.com/privacy.
- The marketing site for that domain is the separate `omega-site` repository, commonly checked out beside the Omega root as `omega-site/`.
- Google Cloud OAuth app branding for permanent public Google sign-in is under Omega AI LLC.

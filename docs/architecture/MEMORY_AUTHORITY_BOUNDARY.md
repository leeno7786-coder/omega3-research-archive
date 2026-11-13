# Memory Authority Boundary

This document describes the current memory ownership boundary. `AGENTS.md` is
the governing law; this page is an implementation map and must follow it.

## Summary

Omega has three distinct memory surfaces:

| Surface | Authority | Data root | Purpose |
|---|---|---|---|
| Guardian | External human-authored ingress | `guardian/state/` and audit logs | Screen untrusted outside content and tool registration |
| Dewey memory | Canonical cognitive truth | `memory/000_identity/` through `memory/1100_dev/` | Persist and recall what Omega knows |
| Taxonomy | Internal cascade observation | `memory/taxonomy_store/` | Capture and analyze what the cascade did |

The surfaces are not competing memory stores. Dewey is canonical knowledge,
while taxonomy is an internal operational ledger and derived graph.

## Authority Rules

1. Guardian screens untrusted external human-authored ingress: user chat,
   connector observations, workspace text, filenames, comments, and revisions.
   Provider metadata is trusted only for the structural fields that the provider
   owns. Guardian is not the cognitive memory writer.

2. All incoming memory enters episode SRAM through `memory.api.intake_memory`.
   DeepSleep is the single canonical persistence boundary:

   ```text
   intake -> episode SRAM -> DeepSleep -> Dewey filesystem -> graph/index
   ```

   Cognitive self-writes, consolidation, identity, temporal, knowledge, and
   execution reports are internal trusted writes from DeepSleep. They do not
   pass through Guardian's external-ingress decision gate.

3. Dewey is the source of truth for what Omega knows. Canonical files and the
   `dewey_map` coordinate rules own persistence and recall. Graph and DME
   projections are derived recall infrastructure and must be rebuilt from the
   canonical filesystem.

4. Taxonomy is a Guardian-free internal observation path. It captures episode
   and execution events, compiles `memory/taxonomy_store/`, and may provide
   bounded advisory pressure when `OMEGA_MEMORY_TAXONOMY_PRESSURE` is enabled.
   It never decides what persists to Dewey and never outranks Dewey recall.

5. Taxonomy capture and recall are live. There is no `OMEGA_MEMORY_TAXONOMY`
   enable switch. Only the optional advisory pressure seam is independently
   gated.

## Current Code Paths

| Concern | Current implementation |
|---|---|
| External ingress screening | `guardian/decision.py`, `guardian/external_ingress.py` |
| Memory intake | `memory/api.py::intake_memory`, `memory/episode_sram.py` |
| Canonical DeepSleep write | `deepsleep_loop/loop.py`, `memory/api.py` |
| Canonical routing | `memory/dewey_map.py` |
| Canonical recall | `memory/api.py`, `memory/taxonomy/recall.py`, graph/DME readers |
| Taxonomy capture/compile | `memory/taxonomy/`, `deepsleep_loop/` |
| Taxonomy root | `core/paths.py::MEMORY_TAXONOMY_ROOT` |
| Optional taxonomy pressure | `pressure_loop/taxonomy_pressure.py` |

## Do Not Confuse These Surfaces

- The HUD memory tree's `taxonomy` query parameter renders Dewey lobe and
  subdomain coordinates. It does not render the taxonomy graph.
- A taxonomy graph coordinate or snapshot hash is a rebuildable projection, not
  a durable semantic memory address.
- A `memory_taxonomy` pressure signal is attention evidence, not a Guardian
  persistence decision.
- Guardian audit logs are operational evidence, not a learning or memory lane.

## Operator Quick Reference

| Question | Source |
|---|---|
| What outside content was allowed or rejected? | Guardian decision/audit records |
| What does Omega know? | Dewey canonical files and recall projection |
| What did the cascade do? | Taxonomy ledger and compiled graph |
| Why did an episode open? | Pressure trail and contributor metadata |

## Related Sources

- `AGENTS.md` sections 4, 5, and 6 for the binding rules.
- `memory/README.md` for canonical memory operations.
- `config/README.md` for configuration authority and taxonomy status.
- `policy/memory.policy.yaml` for the memory behavioral contract.

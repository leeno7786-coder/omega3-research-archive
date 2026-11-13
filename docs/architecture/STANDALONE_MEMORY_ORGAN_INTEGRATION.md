# Standalone Memory Organ Integration

## Scope

Omega selectively integrates the improved recall organ from the standalone
`omega-memory-mcp` line at commit
`2b65b4690dd2d3014b92c7b923a83a38ef3cfd9b`.

This is the **standalone** implementation, not the hybrid implementation.  The
integration keeps Omega's existing cognition, Guardian boundary, SourceLedger,
taxonomy graph, DME thought engine, and lifecycle workers authoritative.  It
does not copy the standalone service shell, worker/profile rebinding, Guardian
shim, remote embedding providers, graph snapshots, DME binaries, or live
memory data.

## Read contract

```text
operator/Omega query
        |
        v
speech-act + query-route normalization
        |
        v
taxonomy graph and DME candidate membership
        |
        +---------------------------+
        |                           |
        v                           v
existing projected IDs       canonical filesystem corpus
        |                           |
        +------------+--------------+
                     v
          one canonical rank scale
                     |
                     v
       SourceLedger + projection-receipt
                 verification
                     |
                     v
          verified evidence only
                     |
                     v
               cognition
```

Canonical JSON remains truth.  Graph and DME are rebuildable candidate and
connectivity projections; neither can make an uncited record true.  If both
projections are empty or stale, bounded canonical fallback can still recover a
matching record, but RecallVerifier must authenticate it before cognition can
see it.

All recall lanes in one request resolve against one active profile root.  A
redirected profile cannot accidentally combine its graph candidates with
canonical files or verifier evidence from another profile.

## Write and integrity contract

- External human-authored ingress is still quarantined in SourceLedger and
  admitted by Guardian before canonical projection.
- Omega's own cognitive writes remain direct and Guardian-free, as required by
  the project doctrine.
- Newly encoded canonical records receive a projection receipt binding their
  stable claim fields to their SourceLedger citation.
- Embedding engine, model, dimension, and vector schema are bound separately.
  A pending embedding may be finalized once; later changes require an explicit
  migration.
- RecallVerifier checks the canonical record, cited source, receipt, active
  profile containment, lifecycle state, and query relevance before returning
  evidence.
- Integrity audit reports missing, malformed, mismatched, and orphan projection
  receipts.

## Day Zero and fresh onboarding

Day Zero intentionally clears canonical memory, SourceLedger artifacts,
projection receipts, taxonomy projections, and DME state.  Therefore a query
about the operator immediately after reset must return no recalled hits.

The next onboarding is expected to perform this sequence:

1. Guardian admits the cited external onboarding artifact.
2. Persona and occupation leaves are written to their canonical lobes with
   operator lineage.
3. Encoding creates projection receipts for the new leaves.
4. Taxonomy and DME rebuild from canonical truth.
5. The exact operator-persona query is verified from the new cited record.  It
   also remains recoverable if the graph/DME projections are temporarily cold.

No previous Noah/persona record is restored or copied during this integration.
The live store remains empty until that fresh onboarding occurs.

## Hardware-idle verification

The integration tests do not boot Omega or load models.  Rebuilding an empty
DME projection now records the CPU backend without probing the runtime because
there is no matrix work to execute.  Live validation should wait until the
evaluation chips are free, then boot once, submit onboarding, and ask the exact
operator-memory question while inspecting persona/occupation lobe lineage and
the resulting projection receipts.

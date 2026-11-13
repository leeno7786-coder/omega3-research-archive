# Semantic Retina Brain Residency — Operator Ruling

**Date:** 2026-08-16  
**Authority:** Operator ruling  
**Decision register:** ADR-MO-018

## Decision

The Omega semantic retina is resident inside the Omega3.0 brain/memory process. It is not served by `omega-runtime` and has no HTTP, socket, IPC, shared-memory transport, runtime-worker, or alternate-encoder fallback path.

The memory system owns one bounded in-process retina session. The frozen weights, tokenizer, pooling/normalization contract, output geometry, quantization contract, and generation hashes are immutable for a published generation. Failure disables the semantic lane; it does not cause a transport retry or fallback model call.

## Rationale

The prior service topology added failure and synchronization surfaces that are unnecessary for a small frozen sensory encoder. Operational experience with socket instability and runtime-worker crashes makes transport/runtime coupling an undesirable dependency for the memory organ.

## Scope boundary

This is a narrow exception to the general runtime-owns-model-loading rule. It does not authorize brain-side LLM, TTS, STT, vision, or arbitrary model loading. The retina remains perception-only: no persistence authority, truth authority, verifier authority, or runtime learning.

## Engineering consequences

- Program I tests the on-disk native-256 encoder in-process first.
- No runtime endpoint comparison is required as a product gate.
- ONNX/INT8 work, if validated, remains in-process.
- Cut 5A is unaffected and remains frozen.
- Cut 13 later generation-pins the retina component when a semantic surface participates in the composite projection generation.

# Cut 13 Part A — coherent projection generation: evidence artifact

Implementation `5a3966f6` (A1 `342ce07a` … A7b `289db26b`, rework 1 `5bdc2e17` per review `73646a37`, rework 2 `5a3966f6` per review `3b84d003`); plan `d1673531` (PASS review `c21937bc`); start `bee5ccd0`.
Everything below was produced by a scratch-profile run of the REAL builder/publisher/dispatcher on the **A′-full corpus copy**
(`benchmarks/memory_organ/aprime_full_snapshot_468c5771/root/memory`, 115 canonical records, compiled by compiler v7) — the live
seat, its `guardian/state` and its `memory/` were never touched (timekeeper + journal redirected to the scratch root). Program 0
(Part B) evidence sections (outbox snapshot/byte boundary, drain-vs-clean parity, rotation-vs-writer, bootstrap, partial-tail) are
DELIBERATELY ABSENT — Program 0 starts only from the Cut-13 freeze SHA.

## 1. Generation layout (fixture = A′-full copy) and disk footprint

```text
coverage_seal.json
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/coord_index.snapshot.json
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/coordinate_registry.json
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/dme_manifest.json
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/generation.json
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/graph.sqlite
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/graph_snapshot.json
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/planes/hop_plane_strong.bin
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/planes/hop_plane_weak.bin
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/sram.bin
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/sram_index.json
gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d/sram_meta.json
lineage.jsonl
manifest.json
registry_authority/lineage.jsonl
registry_authority/manifest.json
registry_authority/registry-96a9f3209e92bc175bd6ccf5d84729896c3ba479ff9554b64c4d2630ccfd5e33.json
```

| file | bytes | sha256 |
|---|---:|---|
| `coord_index.snapshot.json` | 112,008 | `8c89318e18e2c6697f8f53c6d5062bf157ce5832ae72083c2cdc3ab748f58f8b` |
| `coordinate_registry.json` | 34,842 | `96a9f3209e92bc175bd6ccf5d84729896c3ba479ff9554b64c4d2630ccfd5e33` |
| `dme_manifest.json` | 560 | `1211f28780ac55022097762b2f8c74f4107651d9226fb3a7bc4ff8ac61400cd1` |
| `generation.json` | 2,974 | `b9dbd49b1983589116d4746c9f0f5d01855aa1851d2e107bf68e32b3c5e99287` |
| `graph.sqlite` | 1,540,096 | `1161953888d42017e1bbe37fdd33ca50d3852b1815028246f8f3100472b088ce` |
| `graph_snapshot.json` | 400 | `a12b0adc5d024651e0ae7765a1fa7635fc54545daf658f89580e4042288a801d` |
| `planes/hop_plane_strong.bin` | 3,407,872 | `d4c02b6c0c0fc9adb4a331f7473ba9d5eb57d17c7df823a3f473fccad4d93a28` |
| `planes/hop_plane_weak.bin` | 3,407,872 | `0460b2b3953e7e510d061932001c3b5b6e16cf923d7a05d6e1298b31fb2206a0` |
| `sram.bin` | 3,407,872 | `d435355e919d9371d87a4c9550eaffcc282d76e7a2c48f33c27df06303256cee` |
| `sram_index.json` | 58,598 | `ef5bb6fb920209b6233c20b08f30cf695b753c9073c7b623ac81c45e5a0488f1` |
| `sram_meta.json` | 447 | `6118af93d0abd465b44937fab27e44b9e998c6acd1fd873e958f1fb395854bbd` |

Generation dir total **11,973,541 bytes** (11.4 MiB; the three 262,144-bit planes/SRAM at 104 seats
dominate: 3 × 3,407,872); projection root incl. registry authority + lineage + seal + manifest **12,013,914
bytes**; canonical corpus 3,461,555 bytes. First build+publish 0.385 s
(compile + DME rebuild + hardlink capture + fsync). GC keeps active + 1 (bounded footprint ≈ 2 generations + quarantine ≤ 2).

## 2. Manifest example (`manifest.json`, schema `omega.projection_manifest.v1`)

```json
{
  "backend_at_publish": "not_recorded",
  "builder": "cut13",
  "canonical_watermark_commit_seq": 0,
  "coverage_clock": {
    "clock_epoch_id": "acc3b055a5cf5eca6be17e3ee47ae4f6",
    "commit_seq": 0
  },
  "dir_sync": "posix_fsync",
  "generation_dir": "gen-81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d",
  "generation_id": "81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d",
  "latest_canonical_commit_seq_at_capture": 0,
  "lineage_seq": 1,
  "oldest_pending_commit_seq": null,
  "oldest_pending_queued_at": "",
  "page0_capacity_by_lobe": {
    "0": 21824,
    "1": 21824,
    "10": 21824,
    "11": 21824,
    "2": 21824,
    "3": 21824,
    "4": 21824,
    "5": 21824,
    "6": 21824,
    "7": 21824,
    "8": 21824,
    "9": 21824
  },
  "page0_overflow_attempts": 0,
  "page0_used_by_lobe": {
    "0": 81,
    "1": 72,
    "10": 0,
    "11": 38,
    "2": 22,
    "3": 0,
    "4": 50,
    "5": 328,
    "6": 11,
    "7": 0,
    "8": 3,
    "9": 17
  },
  "page0_utilization_by_lobe": {
    "0": 0.003711510263929619,
    "1": 0.0032991202346041057,
    "10": 0.0,
    "11": 0.0017412023460410557,
    "2": 0.0010080645161290322,
    "3": 0.0,
    "4": 0.0022910557184750734,
    "5": 0.015029325513196481,
    "6": 0.0005040322580645161,
    "7": 0.0,
    "8": 0.0001374633431085044,
    "9": 0.0007789589442815249
  },
  "parent_generation_id": null,
  "pending_delta_count": 0,
  "published_at": "2026-08-17T11:08:25.706908+00:00",
  "reason": "evidence",
  "schema_version": "omega.projection_manifest.v1",
  "sequenced_consumed": 0,
  "snapshot_closure": "proved",
  "source_input_origins": {
    "canonical_corpus": "legacy:memory",
    "coordinate_registry": "empty",
    "source_ledger": "legacy:memory/source_ledger"
  },
  "unscorable_coordinate_count": 0,
  "unsequenced_consumed": 0,
  "unsequenced_posture": "covered_by_full_rebuild"
}
```

`content` (the ONLY input to `generation_id`; planes abbreviated):

```json
{
  "association_generation": null,
  "canonical_corpus_hash": "59bcdfda305d288c2e53b5144d2d01097d2cca9b826a956b09fd29e00117665e",
  "canonical_files": 115,
  "compiler_version": "omega.memory_compiler.v7",
  "components": {
    "semantic_retina": null
  },
  "coord_index_sha256": "8c89318e18e2c6697f8f53c6d5062bf157ce5832ae72083c2cdc3ab748f58f8b",
  "dme_manifest_sha256": "1211f28780ac55022097762b2f8c74f4107651d9226fb3a7bc4ff8ac61400cd1",
  "dme_schema_version": "omega.dme_sram.v6",
  "graph_snapshot_hash": "831b47a3058377e96207425033fcb77db2ea666dc5b4ca7fd00419ba8ef213c5",
  "graph_snapshot_sha256": "a12b0adc5d024651e0ae7765a1fa7635fc54545daf658f89580e4042288a801d",
  "graph_sqlite_sha256": "1161953888d42017e1bbe37fdd33ca50d3852b1815028246f8f3100472b088ce",
  "registry_sha256": "96a9f3209e92bc175bd6ccf5d84729896c3ba479ff9554b64c4d2630ccfd5e33",
  "schema_version": "omega.projection_generation.v1",
  "semantic_representation": "unfrozen",
  "source_inputs": [
    {
      "kind": "canonical_corpus",
      "schema_version": "omega.canonical_corpus.v1",
      "sha256": "59bcdfda305d288c2e53b5144d2d01097d2cca9b826a956b09fd29e00117665e"
    },
    {
      "kind": "source_ledger",
      "schema_version": "omega.source_ledger.v1",
      "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    }
  ],
  "sram_index_sha256": "ef5bb6fb920209b6233c20b08f30cf695b753c9073c7b623ac81c45e5a0488f1",
  "sram_meta_sha256": "6118af93d0abd465b44937fab27e44b9e998c6acd1fd873e958f1fb395854bbd",
  "sram_sha256": "d435355e919d9371d87a4c9550eaffcc282d76e7a2c48f33c27df06303256cee"
}
```

planes: [('hop_plane_strong', 104, 262144, 'hop'), ('hop_plane_weak', 104, 262144, 'hop')]

Receipt `gen-<id>/generation.json` sha256 = `b9dbd49b1983589116d4746c9f0f5d01855aa1851d2e107bf68e32b3c5e99287` (bound by lineage row 1);
lineage row 1: `seq = 1`, event `published_new`, `previous_row_sha256 = 0` × 64 (genesis), `coverage_clock = {'clock_epoch_id': 'acc3b055a5cf5eca6be17e3ee47ae4f6', 'commit_seq': 0}`.
`validate_published_generation` → ok. Registry authority: `['registry_authority/lineage.jsonl', 'registry_authority/manifest.json', 'registry_authority/registry-96a9f3209e92bc175bd6ccf5d84729896c3ba479ff9554b64c4d2630ccfd5e33.json']`.

## 3. `generation_id` determinism

Same process, 5 builds of the unchanged corpus: ids `81702b3abd2a50d5…` × 5
(`all_equal = True`), outcomes ['published_new', 'reused_existing', 'reused_existing', 'reused_existing', 'reused_existing'], lineage rows after 5 builds =
1 (equal-coverage reuse writes NO row, NO intent, no byte under `gen-G`).

Fresh processes, different `PYTHONHASHSEED` and a different profile root (D-13-27/40 — the deterministic DME reference is the byte oracle):

| PYTHONHASHSEED | generation_id | sram.bin sha256 | graph_snapshot_hash | registry_sha256 | seats |
|---|---|---|---|---|---:|
| 0 | `81702b3abd2a50d5…` | `d435355e919d9371…` | `831b47a3058377e9…` | `96a9f3209e92bc17…` | 104 |
| 1 | `81702b3abd2a50d5…` | `d435355e919d9371…` | `831b47a3058377e9…` | `96a9f3209e92bc17…` | 104 |
| 12345 | `81702b3abd2a50d5…` | `d435355e919d9371…` | `831b47a3058377e9…` | `96a9f3209e92bc17…` | 104 |
| unset (random) | `81702b3abd2a50d5…` | `d435355e919d9371…` | `831b47a3058377e9…` | `96a9f3209e92bc17…` | 104 |

`generation_ids_equal = True`, `sram_bytes_equal = True`,
equal to the same-process run = True.

## 4. Structural legacy comparison

Legacy fixed-name compile of the same corpus (flag off, `compile_memory_graph(root=…)`) → `graph_snapshot_hash`
`831b47a3058377e96207425033fcb77db2ea666dc5b4ca7fd00419ba8ef213c5`; generation `graph_snapshot_hash` identical
(`equal = True`) — and equal to the Cut-5A A′-full v7 production hash `831b47a3…` recorded in
`cut5a_structural_graph_evidence.md`. The generation graph is the same structural graph; only its container/identity changed.

## 5. Coverage-clock regression / day-zero postures

- Head `(epoch bdfb4bc9…, 0)`; **clock-only loss** (timekeeper.json
  removed, projection intact) ⇒ the next build mints a NEW epoch and publish is refused:
  `CoverageClockRegression: coverage clock regression on publish: head={'clock_epoch_id': 'bdfb4bc95f0d512b3e97481e4c899c49', 'commit_seq': 0} candidate={'clock_ep…`; active manifest unchanged = True.
- **Complete reset** (`projection_reset_exclusion` → `wipe_projection_runtime`: 6 entries removed,
  residue after wipe = []) ⇒ next publish `published_new` under a new epoch
  (`epoch_changed = True`). Projection-only wipe with the old clock ⇒ new initial head in the SAME epoch
  (`tests/scripts/test_day_zero_projection_wipe.py::test_clock_only_and_projection_only_postures`).

## 6. Flag precedence (dispatcher matrix, boot + DeepSleep share `projection_dispatch`)

```json
{
  "0/0": {
    "action": "legacy",
    "owner": "legacy"
  },
  "0/0 after migration": "ProjectionModeRetreatRequired",
  "0/1": {
    "raised": "ProjectionConfigError"
  },
  "1/0": {
    "action": "published",
    "owner": "cut13_direct"
  },
  "1/0 second dispatch (no change)": {
    "action": "no_work",
    "owner": "cut13_direct"
  },
  "1/1": {
    "raised": "ProjectionOwnerUnavailable"
  }
}
```

L0 wiring script (`tests/memory/test_memory_l0_wiring.py`): 25/0 under `0/0`, `0/1`, `1/0`, `1/1` and flag-unset native=0.

## 7. Health snapshots

`1/0` after publish (flag on): `{'generation': '81702b3abd2a50d50931c55d087287774d68fac0480e35c3ad9b7f9673734a1d', 'semantic_dme_lane': 'available', 'lane_reason': None, 'current': True, 'watermark_commit_seq': 0, 'latest_canonical_commit_seq': 0, 'lag_commits': 0, 'reserved_seq': 0, 'landed_seq': 0, 'dirty': False, 'blocked_on_obligations': False, 'projection_owner': 'cut13_direct', 'generation_mode_retreat_required': False}`
(generation abbreviated in prose: `81702b3abd2a50d5…`; `page0_utilization_by_lobe` max 0.0150 in lobe 5).

Flag OFF with the migrated projection present (the D-13-71 retreat posture): `current = False`, `lane_reason = generation_mode_retreat_required`,
`generation_mode_retreat_required = True`, `projection_owner = legacy` — never claims current.

## 8. Crash / torn / repair / intent / same-id coverage matrices (test evidence — 163 projection tests × 2 ambient modes + comparator run)

- Publication-intent crash machine, per-interval crash hooks (`before_intent`, `after_intent`, `after_rename`, `after_row`, `after_manifest`),
  torn tails, event-specific recovery: `test_generation_publication_order.py` (9), `test_generation_publication.py` (11), `test_generation_repair.py` (14).
- Gate D2 end-to-end + failed-build-leaves-G-active + torn temp manifests + lock contention + legacy retention + corrupt ACTIVE manifest
  (no rollback pointer) + canonical write during capture ⇒ reject/retry: `test_generation_crash_matrix.py` (8).
- Same-id later coverage advances lineage ONCE with an immutable receipt; retained reactivation; orphan adoption from receipt; equal
  coverage but different receipt bytes ⇒ one rebind row: `test_generation_coverage.py` (6), `test_generation_receipt/lineage/leases` (1 each).
- Coverage-clock gate identity-then-number (epoch mismatch, counter-loss new epoch, numeric catch-up stays blocked, disabled-projection
  catch-up): `test_generation_coverage.py`, `tests/core/test_timekeeper_coverage_clock.py` (15).
- Real subprocess proofs: OS-held authority (second process refused, killed holder releases), shared leases across processes, hash-seed
  children, day-zero real child holders (reader lease / publication / outbox / namespace / lifecycle) ⇒ refusal before any deletion.

## 9. Freeze-boundary audit (D-13-53) — output

```text
$ git diff bee5ccd0..HEAD --stat -- memory/projection_delta.py
(empty — diff-empty)
$ grep -rnE "ProjectionDrainReport|OutboxSnapshot|OutboxCursor|OutboxEpoch|OutboxBootstrapIntent|OutboxBootstrapReport|outbox_exclusion|bootstrap_outbox|outbox_epoch|consumed\.json|rotate_outbox_epoch|capture_outbox_snapshot|drain_once|outbox_malformed" memory tests/memory/projection scripts boot.py deepsleep_loop core --include=*.py
tests/memory/projection/test_generation_dispatch.py:154   (banned-name audit literal)
tests/memory/projection/test_generation_manifest.py:150-151   (banned-name audit literal)
scripts/day_zero_reset.py:280-281   (D-13-65 EXACT PROJECTION_RUNTIME_SURFACES file-name list — data names, no Program-0 type/code)
$ ls tests/memory/projection | grep -c projector      → 0
$ ls memory/projection → __init__.py build.py dispatch.py generation.py registry_authority.py   (no projector.py)
$ git diff bee5ccd0..HEAD --check → clean
do-not-modify audit (semantic_admission, canonical_recall, recall_verifier, encoding, promotion, gates/base, post_commit,
encoding_journal, atomic_replace, citizenship, self_vector, local_embed, embed_backfill, dme/geometry*, autonomic/, guardian/): diff-empty;
permitted seams only: memory/recall_basis.py (2 lines: G.connect → G.connect_for_read), matmul_engine.py (generation-pinned
registration; scoring untouched), hop_engine.py (path/view sourcing).
```

## 10. Rework 1 (review 73646a37) — RED on 289db26b → GREEN on 5bdc2e17

```text
tests/memory/projection/test_generation_snapshot.py::test_schema_loader_failure_fails_closed_never_admits_membership
    RED  E   Failed: DID NOT RAISE MembershipError            (forced RX.load_schema() failure ⇒ record admitted)
tests/memory/projection/test_generation_watermarks.py::test_malformed_or_unreadable_outbox_evidence_is_never_current
    RED  E   AssertionError: a projection over malformed outbox evidence must never be current — assert True is False
tests/memory/projection/test_generation_watermarks.py::test_health_returns_unavailable_when_the_coverage_clock_is_unavailable
    RED  E   NameError: name 'CoverageClockUnavailable' is not defined
tests/scripts/test_day_zero_projection_wipe.py::test_reader_enrolling_in_the_gap_before_the_namespace_gate_still_blocks_reset
    RED  E   Failed: DID NOT RAISE DayZeroResetError         (reader enrolled in the gap before the namespace gate; reset yielded)
GREEN on 5bdc2e17: projection 166 ×3; coverage clock + day-zero 26; scripts 85; Cut-5A 110 ×3; Cut-4 115 ×3; DME 138; L0 25/0 ×4;
Ruff over every file changed since bee5ccd0: All checks passed!; git diff --check clean; projection_delta.py diff-empty.
```

## 10a. Rework 2 (review 3b84d003, D-13-66) — RED on 5bdc2e17 → GREEN on 5a3966f6

```text
tests/memory/projection/test_generation_watermarks.py::test_blank_or_whitespace_complete_outbox_rows_are_malformed_evidence
    RED  E   AssertionError: blank/whitespace complete row b'\n' must never be current      (also b'   \n', b'\t\n', b'  \n\t\n'; count 1 == 2)
GREEN on 5a3966f6: 5/5; projection 171 ×3; coverage clock + day-zero 26; Cut-4 health controls 28 + full set 115; Ruff clean;
git diff --check clean; projection_delta.py diff-empty.
```

## 11. Test commit SHAs

A1 `342ce07a` · A2 `ff8cffd7` · A3 `0b362b94` + gate re-scope `f850a210` + A3b `7c6e8b55` · A4a `7eee48e0` · A4b `10748ee4` · A5 `6debd8e9` ·
A6 `57d586ad` · A7 `683a2912` · A7b `289db26b` · A8 `e8196105` (docs/evidence/report) · R1 `5bdc2e17` (rework 1) · R1a `01169213` · R2 `5a3966f6` (rework 2) · R2a = the amended-report commit.


---

# PART B — Program 0 evidence (implementation `56c0cad4` = `9dd628b0` + rework 1 per review `a045ec4a`; P0.1 `66764bc0`, P0.2 `fc8770fc`, P0.3 `461d3929`, P0.4 `9dd628b0`; first Part-B commit parented on the Cut-13 freeze lineage `cd82a3f8` → `10fb8528`)

Produced by a scratch-profile run of the REAL drain / dispatcher on the A′-full corpus copy (115 records) and small canonical fixtures;
timekeeper + journal redirected; nothing live touched. The Part-A sections above are unchanged (the frozen Part-A results were re-run,
see the Program-0 completion report).

## P1. Legacy Cut-4 outbox bootstrap + drain in ONE invocation (D-13-52/62/72) — drain id == clean build id

The A′-full copy with a legacy Cut-4 outbox (3 rows, 1974 bytes, no sidecars — the live seat's shape):
`drain_once` ⇒ `bootstrap_state = legacy_cut4`, status `published_new`, `full_builds = 1`, `deltas_seen = 3`,
generation `81702b3abd2a50d5…` == the clean outbox-blind `build_generation` id (`drain_id_equals_clean = True`) — the same
`81702b3a…` as the Part-A evidence; 0.246 s; epoch 1 origin `legacy_cut4`; cursor settled at offset 1974 =
observed_size 1974, `consumed_prefix_sha256 = observed_prefix_sha256 = 1ec07ee538d12927…`; outbox bytes unchanged = True;
quarantine = []; re-drain ⇒ `no_work` with 0 builds.

## P2. Coherent OutboxSnapshot / exact byte boundary (D-13-31/48)

Three new rows + a partial final line: `captured_size = 1983`, `complete_boundary = 1974` (the partial tail of 9 bytes is
never parsed), `saved_offset = 1974`; settlement persisted the SNAPSHOT's values (`observed_size = 1974`, `offset = 1974`)
— note this capture happened after the three rows were already consumed by P1's re-drain, so `bytes_to_parse = 0` and the drain reported `no_work`.

## P3. Malformed-only rows force ONE rebuild, then the cursor advances (D-13-66)

Completing the partial line as JSON-malformed + `b"\n"` + `b"   \n"` + `not json`: `malformed_row_count = 4`, `malformed_forced_build = True`,
`full_builds = 1`, status `reused_existing` (unchanged truth ⇒ reused), `cursor_settled = True`; receipts (byte ranges exclude the LF; empty row = `sha256(b"")`):

```json
[
  {
    "byte_end": 1985,
    "byte_start": 1974,
    "epoch": 1,
    "error": "JSONDecodeError: Expecting ':' delimiter: line 1 column 11 (char 10)",
    "sha256": "6bfac5e31d66fff1f2ddab6eedc1920c7e88fe1ab90acb7582f8773897e8599e"
  },
  {
    "byte_end": 1986,
    "byte_start": 1986,
    "epoch": 1,
    "error": "empty or whitespace-only row",
    "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  },
  {
    "byte_end": 1990,
    "byte_start": 1987,
    "epoch": 1,
    "error": "empty or whitespace-only row",
    "sha256": "0aad7da77d2ed59c396c99a74e49f3a4524dcdbcb5163251b1433d640247aeb4"
  },
  {
    "byte_end": 1999,
    "byte_start": 1991,
    "epoch": 1,
    "error": "JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
    "sha256": "7ccfa1fbf3940e6f0c0375d87c0f9235a50514e14cb427bdfaf5077987b26ccf"
  }
]
```

re-drain ⇒ `no_work` (never reprocessed forever).

## P4. Writer fast-fail during a projector hold + rotate-first recovery (D-13-43/50) — real processes

While the projector held the outbox exclusion, a REAL producer process's `append_projection_delta` returned `ok = False` in
**11.1 ms** and wrote durable dirty evidence (`dirty = True`) while the hold continued for another
1.0 s. The outbox was then atomically REPLACED (identity change) ⇒ the next drain: `outbox_corrupt_recovered`, `recovery_rotated = True`,
`full_builds = 1`, dirty compare-and-cleared = True; epoch `2` origin `rotation`; zero cursor on the new identity; quarantine ['projection_delta.jsonl.quarantine-1'];
recovery journal stages ['begin', 'rotated', 'completed'] (the `completed` receipt only after the rebuild's barrier + cursor); a producer after release appends to the NEW file
(`ok = True`).

## P5. Virgin bootstrap and the legacy partial-tail policy (D-13-52/56)

Virgin: status `published_new`, epoch 1 origin `virgin`, zero cursor (`e3b0c44298fc1c14…` = ZERO_SHA256), initial generation.
Legacy partial tail: 548 → 549 bytes (ONE LF delimiter appended = True); ONE receipt for the torn row
(bytes 540–548); the SAME invocation parsed the pending row + the completed malformed row, built ONCE
(`full_builds = 1`, `deltas_seen = 1`, `malformed_row_count = 1`) and settled.

## P6. Flag precedence — all four combinations, boot AND DeepSleep through the ONE dispatcher (D-13-57)

```json
{
  "0/0": {
    "boot": {
      "action": "legacy",
      "drain_status": null,
      "owner": "legacy"
    },
    "deepsleep": {
      "action": "legacy",
      "drain_status": null,
      "owner": "legacy"
    }
  },
  "0/0 after migration": "ProjectionModeRetreatRequired",
  "0/1": {
    "boot": {
      "raised": "ProjectionConfigError"
    },
    "deepsleep": {
      "raised": "ProjectionConfigError"
    }
  },
  "1/0": {
    "boot": {
      "action": "published",
      "drain_status": null,
      "owner": "cut13_direct"
    },
    "deepsleep": {
      "action": "no_work",
      "drain_status": null,
      "owner": "cut13_direct"
    }
  },
  "1/1": {
    "boot": {
      "action": "no_work",
      "drain_status": "bootstrapped",
      "owner": "program0"
    },
    "deepsleep": {
      "action": "no_work",
      "drain_status": "no_work",
      "owner": "program0"
    }
  }
}
```

## P7. Health with Program 0, coverage-clock regression under 1/1, day-zero wipes Program-0 metadata

```json
{
  "current": true,
  "generation": "bae8fbf1f0696fc05129f5afe97302ffa258187763b4dff25cd827f1837fe108",
  "lane_reason": null,
  "malformed_delta_count": 0,
  "pending_delta_count": 0,
  "program0": {
    "bootstrap_intent": false,
    "bootstrap_state": "established",
    "cursor_generation_id": "bae8fbf1f0696fc05129f5afe97302ffa258187763b4dff25cd827f1837fe108",
    "cursor_observed_size": 0,
    "cursor_offset": 0,
    "epoch": 1,
    "epoch_origin": "virgin",
    "error": null,
    "malformed_receipts": 0,
    "malformed_receipts_readable": true,
    "recovery_pending": false
  },
  "projection_owner": "program0",
  "semantic_dme_lane": "available"
}
```

Clock loss under Program 0 ⇒ `coverage_clock_regression`, `cursor_settled = False` (offset stays 0).
Day-zero (`projection_reset_exclusion` → `wipe_projection_runtime`) removed ['indexes/projection_delta.jsonl', 'projection/consumed.json', 'projection/coverage_seal.json', 'projection/gen-01022e27bb2c1923cfe164753c4e3d21aba01505f3fc064c3df2abdfb4bd5c98.candidate', 'projection/gen-bae8fbf1f0696fc05129f5afe97302ffa258187763b4dff25cd827f1837fe108', 'projection/lineage.jsonl', 'projection/manifest.json', 'projection/outbox_epoch.json', 'projection/registry_authority'] — incl. `consumed.json`, `outbox_epoch.json` and the Cut-4 outbox — with
residue [], lock files preserved ['publication.lock', 'lease_namespace.lock', 'indexes/projection_delta.lock']; the next drain is a virgin bootstrap (`published_new`).

## P7a. Rework 1 (review a045ec4a) — RED on `9dd628b0` → GREEN on `56c0cad4`

```text
test_projector_bootstrap.py::test_resumed_legacy_intent_refuses_a_replaced_outbox_identity_and_rotates_first
    RED  E   AssertionError: assert 'published_new' == 'outbox_metadata_corrupt_recovered'   (replacement inode adopted into epoch 1)
test_projector_bootstrap.py::test_resumed_legacy_intent_refuses_a_missing_or_appended_replacement_outbox
    RED  E   AssertionError: ('missing', 'bootstrapped')
test_projector_dispatch.py::test_health_is_never_current_while_a_program0_transition_is_unfinished
    RED  E   KeyError: 'unfinished_transition'                       (health current while rotate-first recovery was durably pending)
test_projector_dispatch.py::test_unterminated_trailing_fragment_is_pending_evidence_and_blocks_currentness
    RED  E   AttributeError: 'ProjectionHealth' object has no attribute 'pending_partial_bytes'   (current over 11 unconsumed bytes)
test_projector_dispatch.py::test_program0_helpers_refuse_a_publication_token_from_another_profile
    RED  E   Failed: DID NOT RAISE PublicationAuthorityRequired      (A's token cleared B's dirty marker)
GREEN on 56c0cad4: 5/5; projection 256 ×3; Cut-4 controls 115; day-zero + coverage clock 26; Ruff clean; git diff --check clean;
do-not-modify + Part-A files diff-empty since cd82a3f8.
```

## P8. Test commit SHAs (Part B)

P0.1 `66764bc0` · P0.2 `fc8770fc` · P0.3 `461d3929` · P0.4 `9dd628b0` · P0.5 `b53a0d5f` · R1 `56c0cad4` (rework 1) · R1a = the amended-report commit.

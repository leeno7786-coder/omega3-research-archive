"""MemoryGate — access-controlled base class for all memory gates.

Each gate subclass declares:
  READABLE_LOBES  — lobe IDs the gate may read from
  WRITABLE_LOBES  — lobe IDs the gate may write to
  DECOMPOSERS     — {lobe_id: enrich_fn} called before write
  SOURCE_TAG      — audit / provenance label
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, List, Callable

from memory.core import build_memory_dict
from memory.engine.validate_write import atomic_write
from memory.gates.subdomain_override import apply_subdomain_override

log = logging.getLogger("omega.memory.gates.base")


def _emit(event_name: str, **data):
    """Emit structured telemetry event to memory.jsonl."""
    try:
        from guardian.terminal_feedback import emit_guardian_event

        emit_guardian_event("omega.memory.gate", event_name, **data)
    except Exception:
        pass


class MemoryGate:
    # --- subclass contract ---
    READABLE_LOBES: List[int] = []
    WRITABLE_LOBES: List[int] = []
    DECOMPOSERS: Dict[int, Callable] = {}
    SOURCE_TAG: str = "gate:base"

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        # Phase 4: production gates in lobe_gates.py are single-lobe only.
        if cls.__module__ != "memory.gates.lobe_gates":
            return
        if len(cls.WRITABLE_LOBES) > 1:
            raise TypeError(
                f"{cls.__name__}: WRITABLE_LOBES must have at most one lobe "
                f"(got {cls.WRITABLE_LOBES})"
            )
        if len(cls.READABLE_LOBES) > 1:
            raise TypeError(
                f"{cls.__name__}: READABLE_LOBES must have at most one lobe "
                f"(got {cls.READABLE_LOBES})"
            )

    # --- shared strength_index (injected at boot or lazy-fetched) ---
    _strength_index = None

    @classmethod
    def wire_strength(cls, strength_index) -> None:
        """Inject the strength_index into all gates. Called once at boot."""
        cls._strength_index = strength_index

    @classmethod
    def _get_strength_index(cls):
        """Return the wired strength_index, lazy-fetching the singleton
        if boot hasn't explicitly wired one. Lets test paths and ad-hoc
        scripts pick up the tracker without manual wiring."""
        if cls._strength_index is not None:
            return cls._strength_index
        from memory.strength_index import ensure_strength_index
        from memory.engine.walker import wire_strength_index

        si = ensure_strength_index()
        wire_strength_index(si)
        cls._strength_index = si
        return si

    def _guardian_check(self, text: str, source: str) -> bool:
        """Return True if the write is allowed; False if Guardian blocks it.

        Gracefully returns True when the guardian module is not available.
        """
        try:
            from memory.write_guard import guard_raw_text

            return guard_raw_text(text, source=source)
        except ImportError:
            return True
        except Exception as exc:
            log.debug("Guardian check error (%s) — allowing write", exc)
            return True

    def _enrich(self, text: str, lobe: int) -> Optional[Dict]:
        """Run the decomposer for *lobe*, returning enrichment dict or None.

        A decomposer failure is emitted and re-raised; callers cannot mistake it
        for a legitimate result with no enrichment.
        """
        fn = self.DECOMPOSERS.get(lobe)
        if fn is None:
            return None
        try:
            return fn(text)
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "enrichment_failed",
                ok=False,
                lobe=lobe,
                error=str(exc),
            )
            log.exception("Enrichment failed for lobe %d", lobe)
            raise

    def _write(
        self,
        text: str,
        coord: List[int],
        enrichment: Optional[Dict],
        metadata: Optional[Dict],
        id_out: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Build memory dict and write atomically. Returns path string or None.

        Also registers the new memory's id with the StrengthIndex so the
        engram is tracked from birth (touch counts, last_access decay).

        ``id_out`` (optional, non-breaking): when a list is supplied and
        the write succeeds, the RAW ``mem["id"]`` is appended to it. This
        is the id the StrengthIndex keys on (see strength_index docstring),
        captured WITHOUT changing the path return contract every existing
        caller depends on. RPE choice-credit needs the raw id (the on-disk
        PATH won't match the index); ``id_out`` surfaces it cleanly.
        """
        try:
            try:
                from memory.sense_registry import attach_senses_to_enrichment

                enrichment = attach_senses_to_enrichment(
                    text,
                    coord=coord,
                    metadata=metadata,
                    enrichment=enrichment,
                )
            except Exception as exc:
                log.debug("sense registry enrichment skipped (%s)", exc)
            mem = build_memory_dict(
                text=text,
                coord=coord,
                source_tag=self.SOURCE_TAG,
                enrichment=enrichment,
                metadata=metadata,
            )
            # Every gate write is native. It records projection work before
            # committing; the resident Ettin retina consumes canonical truth
            # during rebuild, so no dense vector or backfill worker is involved.
            native = True
            # Cut 4 rework 2: the gate is a write OWNER — its durable projection
            # obligation is journaled BEFORE the write + hooks so a crash between
            # atomic_write and the final projection publication cannot strand
            # the record outside projection silently (boot marks it dirty).
            # Failure-soft like everything else in the gate: an unjournalable
            # obligation falls back to a durable dirty marker.
            obligation_id = None
            if native:
                # Rework 3 (CUT4_REVIEW_REWORK_3 §3): the obligation returns its
                # ACTUAL durability posture. With NEITHER the WAL entry nor the
                # fallback dirty marker durable, a native semantic gate write
                # would enter an unprotected canonical+hook transaction — refuse
                # BEFORE the destructive write (nothing landed; nothing owed).
                obligation_needed = False
                try:
                    from memory.projection_delta import projection_class_for

                    obligation_needed = projection_class_for(mem) == "semantic"
                except Exception as exc:  # noqa: BLE001
                    log.error("gate projection class check failed (%s); treating as semantic", exc)
                    obligation_needed = True
                if obligation_needed:
                    # the canonical path is only a HINT for the recovery profile
                    # root; atomic_write may still route elsewhere (dynamic /
                    # unsorted holds) — an unpredictable path is not a failure
                    path_hint = ""
                    try:
                        from memory.engine.validate_write import final_path_for_meta

                        path_hint = str(final_path_for_meta(mem))
                    except Exception as exc:  # noqa: BLE001
                        log.debug("gate obligation: final path not predictable (%s); default profile root", exc)
                    posture = {"journal_id": None, "durable": False, "channel": "none"}
                    try:
                        from memory.projection_delta import open_projection_obligation

                        posture = open_projection_obligation(str(mem.get("id") or ""), path_hint, f"gate:{self.SOURCE_TAG}")
                    except Exception as exc:  # noqa: BLE001
                        log.error("gate projection obligation open raised (%s)", exc)
                    obligation_id = posture.get("journal_id")
                    if not posture.get("durable"):
                        log.critical(
                            "gate %s REFUSED native write for %s: no durable recovery channel (journal and dirty "
                            "marker failed) — canonical+hook window would be unprotected",
                            self.SOURCE_TAG, mem.get("id"),
                        )
                        try:
                            from guardian.terminal_feedback import emit_data

                            emit_data("memory", "gate_write", status="refused", ok=False,
                                      reason="unprotected_hook_window", memory_id=str(mem.get("id") or ""),
                                      gate=self.SOURCE_TAG)
                        except Exception:  # noqa: BLE001
                            pass
                        return None

            def _release_obligation(why: str) -> None:
                # nothing was written → nothing is owed; never leave a false rebuild requirement
                if not obligation_id:
                    return
                try:
                    from memory.encoding_journal import journal_close

                    journal_close(obligation_id, str(mem.get("id") or ""), [f"projection_obligation:{why}"])
                except Exception:  # noqa: BLE001
                    pass

            try:
                path = atomic_write(mem)
            except BaseException:
                _release_obligation("atomic_write_raised")
                raise
            if not path:
                _release_obligation("no_write")
            if path:
                mid = mem.get("id", "")
                hook_report = None
                try:
                    from memory.post_commit import run_post_commit_memory_hooks

                    hook_report = run_post_commit_memory_hooks(
                        str(mid or ""),
                        coord=list(mem.get("coord") or coord or []),
                        source=self.SOURCE_TAG,
                    )
                except Exception as exc:
                    log.error(
                        "Gate post-commit hooks failed for %s: %s: %s",
                        mid,
                        type(exc).__name__,
                        exc,
                    )
                    # the hooks may have started mutating: unknown set != zero set
                    hook_report = {"memory_id": str(mid or ""), "ran": True, "supersedes": None,
                                   "entity_links": 0, "mutation_uncertain": True,
                                   "errors": [f"{type(exc).__name__}: {exc}"]}
                if native:
                    # Cut 4 rework: publish the FINAL canonical state AFTER the
                    # gate's own synchronous hooks (which may rewrite the record
                    # and peers). Outbox failure marks projection dirty and
                    # NEVER undoes the canonical write.
                    try:
                        from memory.projection_delta import (
                            publish_final_projection_state,
                            settle_projection_obligation,
                        )

                        final = publish_final_projection_state(str(mid or ""), str(path), hook_report=hook_report)
                        # settle the obligation ONLY when the final work (or
                        # durable dirty evidence) is on disk; else boot reconciles
                        settle_projection_obligation(obligation_id, str(mid or ""), final)
                    except Exception as exc:  # noqa: BLE001
                        log.error("gate projection publish failed for %s (%s)", mid, exc)
                if id_out is not None and mid:
                    # Surface the RAW id (the value the StrengthIndex keys
                    # on) without disturbing the path return contract.
                    id_out.append(str(mid))
                si = self._get_strength_index()
                if si is not None:
                    write_strength = float((mem.get("provenance") or {}).get("confidence", 0.5))
                    try:
                        si.register(mid, write_strength)
                    except Exception as exc:
                        from guardian.terminal_feedback import emit_data

                        emit_data(
                            "memory",
                            "strength_register_failed",
                            ok=False,
                            memory_id=mid,
                            error=str(exc),
                        )
                        log.debug("StrengthIndex.register failed (%s)", exc)
            return path
        except Exception as exc:
            log.error("Gate write failed (%s)", exc)
            return None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def ingest(self, text: str, metadata: Optional[Dict] = None) -> Optional[str]:
        """Store *text* into the memory engine subject to gate permissions.

        Pipeline:
          1. Reject immediately if WRITABLE_LOBES is empty.
          2. Guardian raw-text check.
          3. Pin lobe from gate contract; resolve Y deterministically.
          4. Permission check — reject if lobe is not writable.
          5. Enrich (graceful: enrichment failure does not abort write).
          6. Atomic write.

        Returns the path string of the written file, or None on rejection.
        """
        return self._ingest_impl(text, metadata, id_out=None)

    def ingest_with_id(
        self,
        text: str,
        metadata: Optional[Dict] = None,
    ) -> tuple:
        """Like ``ingest`` but also returns the RAW ``mem["id"]`` written.

        Returns ``(path, raw_id)``: ``path`` is identical to what
        ``ingest`` returns (None on rejection); ``raw_id`` is the
        StrengthIndex key for the written engram (None on rejection).

        This is the clean capture point for RPE choice-credit — the raw
        id is the value the index registers at birth (``_write`` ->
        ``si.register(mem["id"], ...)``); the on-disk path would NOT match
        the index. Non-breaking: ``ingest`` and every existing caller's
        path contract are untouched.
        """
        ids: List[str] = []
        path = self._ingest_impl(text, metadata, id_out=ids)
        raw_id = ids[0] if ids else None
        return path, raw_id

    def _ingest_impl(
        self,
        text: str,
        metadata: Optional[Dict],
        id_out: Optional[List[str]],
    ) -> Optional[str]:
        """Shared ingest pipeline. ``id_out``, when a list, receives the
        raw written id (see ``_write``). Returns the path (or None)."""
        if not self.WRITABLE_LOBES:
            log.debug("%s has no WRITABLE_LOBES — ingest rejected", self.SOURCE_TAG)
            return None

        # Guardian check
        if not self._guardian_check(text, source=self.SOURCE_TAG):
            log.info("Guardian blocked write from %s", self.SOURCE_TAG)
            _emit(
                "ingest_blocked",
                source_tag=self.SOURCE_TAG,
                reason="guardian",
                text_preview=text[:80],
            )
            return None

        # Single-lobe gates: lobe X is the gate contract; subdomain Y is
        # resolved deterministically by apply_subdomain_override below (Phase 2)
        # and, for flagged lobes, the dynamic-Y allocator.
        if len(self.WRITABLE_LOBES) == 1:
            lobe = self.WRITABLE_LOBES[0]
            coord = [lobe, 0, 0]
            label_id = f"{lobe}.00"
            confidence = 1.0
        else:
            # Phase 4: multi-lobe ingest is retired — no probabilistic lobe pick.
            log.debug(
                "%s: multi-lobe ingest rejected (WRITABLE_LOBES=%s)",
                self.SOURCE_TAG,
                self.WRITABLE_LOBES,
            )
            _emit(
                "ingest_rejected",
                source_tag=self.SOURCE_TAG,
                allowed_lobes=self.WRITABLE_LOBES,
                reason="multi_lobe_retired",
            )
            return None

        # Slice 2 — deterministic ingest subdomain override (write-Y == recall-Y).
        _pre_y = coord[1] if coord and len(coord) > 1 else None
        coord, label_id = apply_subdomain_override(text, coord, label_id)
        if coord and len(coord) > 1 and coord[1] != _pre_y:
            _emit(
                "ingest_subdomain_override",
                source_tag=self.SOURCE_TAG,
                lobe=lobe,
                prior_y=_pre_y,
                helper_y=coord[1],
            )

        from memory.subdomain_flag import dynamic_y_lobes

        _dyn_set = dynamic_y_lobes()

        # Phase 5 — static lobes with no grammar signal land in _unsorted.
        from memory.gates.undecided_hold import route_undecided_subdomain

        _undecided = False
        coord, label_id, _undecided = route_undecided_subdomain(
            text,
            lobe,
            coord,
            label_id,
            dynamic_y_lobes=_dyn_set,
        )
        if _undecided:
            _emit(
                "ingest_undecided_hold",
                source_tag=self.SOURCE_TAG,
                lobe=lobe,
                coord=coord,
            )
            try:
                from memory.routing.edge_case_catalog import append_entry

                append_entry(
                    text=text,
                    lobe=lobe,
                    coord=coord,
                    label_id=label_id,
                    source=self.SOURCE_TAG,
                )
            except Exception as exc:
                log.debug("edge_case_catalog append failed (%s)", exc)
            try:
                from memory.routing.resolve_undecidable import (
                    UNDECIDABLE,
                    resolve_undecidable,
                )

                suggestion = resolve_undecidable(
                    text,
                    {"lobe": lobe, "coord": coord, "source": self.SOURCE_TAG},
                )
                if suggestion is not UNDECIDABLE and suggestion is not None:
                    _emit(
                        "ingest_undecidable_suggestion",
                        source_tag=self.SOURCE_TAG,
                        lobe=lobe,
                        suggestion_lobe=suggestion.lobe,
                        suggestion_y=suggestion.subdomain_y,
                        label=suggestion.label,
                        confidence=suggestion.confidence,
                    )
            except Exception as exc:
                log.debug("resolve_undecidable advisory hook failed (%s)", exc)

        # Dynamic Y allocation: when a lobe is in the feature-flag set, the
        # content-derived allocator OWNS the Y. Fail-HARD on a flagged lobe —
        # a mis-routed knowledge/coding memory poisons recall and cognition,
        # so a failed allocation must surface loudly, never silently fall back
        # to the static subdomain we are retiring. Non-flagged lobes are
        # untouched: the block is skipped and the static path is unchanged.
        if lobe in _dyn_set:
            from memory.subdomain_allocator import get_or_create_subdomain

            _title = (metadata or {}).get("title", "")
            try:
                _alloc_y, _alloc_dn = get_or_create_subdomain(
                    lobe=lobe,
                    seed_text=text,
                    title=_title if _title else None,
                    dynamic_y_lobes=_dyn_set,
                )
            except Exception as exc:
                _emit(
                    "ingest_dynamic_y",
                    source_tag=self.SOURCE_TAG,
                    lobe=lobe,
                    status="fail",
                    ok=False,
                    error=str(exc),
                )
                raise
            coord = [lobe, _alloc_y] + list(coord[2:] or [0])
            label_id = f"{lobe}.{_alloc_y:02d}"
            _emit(
                "ingest_dynamic_y",
                source_tag=self.SOURCE_TAG,
                lobe=lobe,
                alloc_y=_alloc_y,
                alloc_dn=_alloc_dn,
            )

        _emit(
            "ingest_routed",
            source_tag=self.SOURCE_TAG,
            label_id=label_id,
            lobe=lobe,
            confidence=confidence,
        )

        if lobe not in self.WRITABLE_LOBES:
            log.debug(
                "%s: classified lobe %d not in WRITABLE_LOBES %s — rejected",
                self.SOURCE_TAG,
                lobe,
                self.WRITABLE_LOBES,
            )
            _emit(
                "ingest_rejected",
                source_tag=self.SOURCE_TAG,
                classified_lobe=lobe,
                allowed_lobes=self.WRITABLE_LOBES,
                reason="lobe_denied",
            )
            return None

        _write_meta = dict(metadata or {})
        if _undecided:
            _write_meta["undecided"] = True
            _write_meta["confidence"] = min(
                float(_write_meta.get("confidence", 0.8)),
                0.3,
            )

        enrichment = self._enrich(text, lobe)
        result = self._write(text, coord, enrichment, _write_meta, id_out=id_out)

        if result:
            _emit(
                "ingest_success",
                source_tag=self.SOURCE_TAG,
                lobe=lobe,
                coord=coord,
                text_len=len(text),
                had_enrichment=enrichment is not None,
            )

            # Post-write: entity linking. Lobe set sourced from
            # dewey_map.ENTITY_BEARING_LOBES — the canonical entity-
            # bearing set used by entity_linker, supersedes,
            # brain.retrieve, and api.write_memory's post-commit hook.
            # Personas (1) carry "Bob is my dad" anchors; situational
            # (4) carries "Bob likes chicken" assertions. Without the
            # situational arm, cross-lobe relationship chains never
            # get linked and walker.expand_links has nothing to walk
            # at recall time.
            from memory.dewey_map import ENTITY_BEARING_LOBES as _GB_LOBES

            if lobe in _GB_LOBES:
                try:
                    from memory.enrichment.entity_linker import link_personal_memories
                    from memory.engine.walker import load_coord_index, COORD_INDEX

                    load_coord_index()
                    links = link_personal_memories(result, COORD_INDEX)
                    if links:
                        log.debug(
                            "Entity linker: %d links created for %s (lobe=%d)", links, result, lobe
                        )
                except Exception as exc:
                    log.debug("Entity linker skipped (%s)", exc)
        else:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "ingest_error",
                ok=False,
                reason="write_failed",
                lobe=lobe,
                source_tag=self.SOURCE_TAG,
            )
            _emit("ingest_error", source_tag=self.SOURCE_TAG, lobe=lobe, reason="write_failed")

        return result

    def recall(self, query: str, limit: int = 20, domain: str = "") -> List[Dict]:
        """Retrieve memories relevant to *query* filtered to READABLE_LOBES.

        Single-lobe gates force the walker's lobe to READABLE_LOBES[0].
        Recall is fully deterministic (Phase 3/4) — no SetFit call.
        """
        if not self.READABLE_LOBES:
            return []

        if len(self.READABLE_LOBES) != 1:
            log.debug(
                "%s: multi-lobe recall rejected (READABLE_LOBES=%s)",
                self.SOURCE_TAG,
                self.READABLE_LOBES,
            )
            _emit(
                "recall_denied",
                source_tag=self.SOURCE_TAG,
                allowed_lobes=self.READABLE_LOBES,
                reason="multi_lobe_retired",
            )
            return []

        force_lobe = self.READABLE_LOBES[0]
        t0 = time.perf_counter()

        try:
            from memory.engine.walker import walker, load_coord_index

            load_coord_index()
            results = walker(
                query,
                None,
                top_k=limit,
                domain=domain,
                force_lobe=force_lobe,
            )
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "walker_recall_failed",
                ok=False,
                error=str(exc),
            )
            log.error("Walker error in %s.recall: %s", self.SOURCE_TAG, exc)
            _emit("recall_error", source_tag=self.SOURCE_TAG, error=str(exc), ok=False)
            return []

        # Filter to permitted lobes
        filtered = [r for r in results if r.get("coord") and r["coord"][0] in self.READABLE_LOBES]
        filtered = filtered[:limit]

        # Touch accessed memories in strength_index (boosts strength on recall)
        touched = 0
        si = self._get_strength_index()
        if si is not None:
            for r in filtered:
                mid = r.get("id")
                if mid:
                    try:
                        si.touch(mid)
                        touched += 1
                    except Exception:
                        pass

        latency_ms = (time.perf_counter() - t0) * 1000
        _emit(
            "recall_complete",
            source_tag=self.SOURCE_TAG,
            query_preview=query[:60],
            hits=len(filtered),
            lobe=force_lobe,
            confidence=1.0,
            touched=touched,
            latency_ms=round(latency_ms, 1),
        )

        return filtered

    def recall_recent(self, limit: int = 10, lobe: Optional[int] = None) -> List[Dict]:
        """Return recent memories ordered by ingested_at timestamp.

        If *lobe* is specified, restrict to that lobe. Always respects
        READABLE_LOBES permission.
        """
        if not self.READABLE_LOBES:
            return []

        try:
            from memory.engine.walker import (
                load_coord_index,
                coord_index_snapshot,
                load_meta_by_path,
            )

            load_coord_index()
        except Exception as exc:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "index_load_failed",
                ok=False,
                error=str(exc),
            )
            log.error("Index load error in %s.recall_recent: %s", self.SOURCE_TAG, exc)
            _emit("recall_error", source_tag=self.SOURCE_TAG, error=str(exc), ok=False)
            return []

        allowed_lobes = (
            self.READABLE_LOBES if lobe is None else ([lobe] if lobe in self.READABLE_LOBES else [])
        )
        if not allowed_lobes:
            return []

        # AUDIT-5 + REPAIR-9: iterate the snapshot, not the live
        # COORD_INDEX. Direct iteration races with concurrent
        # `load_coord_index` calls and can raise
        # `RuntimeError: dictionary changed size during iteration`.
        candidates = []
        for _idstr, entry in coord_index_snapshot().items():
            coord = entry.get("coord")
            if not coord or coord[0] not in allowed_lobes:
                continue
            path = entry.get("path")
            if not path:
                continue
            meta = load_meta_by_path(path)
            if not meta:
                continue
            from memory.temporal_authority import authoritative_event_timestamp

            ingested_at = authoritative_event_timestamp(meta) or ""
            candidates.append((ingested_at, meta))

        candidates.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in candidates[:limit]]

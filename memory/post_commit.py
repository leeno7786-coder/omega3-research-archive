"""Shared post-commit memory structure hooks.

These hooks are Guardian-free and run after a canonical memory record has
already committed. They build derived structure around the canonical file:
supersedes chains and entity links. Direct cognitive writes and explicit
user-chat ingress writes both use this path, so Guardian-free memory does
not bypass derived memory structure.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

log = logging.getLogger("omega.memory.post_commit")


class TraitRevisionError(RuntimeError):
    """A required canonical trait revision or its projection is incomplete."""


def ensure_trait_revision(canonical_ref: str, *, sequenced: bool | None = None) -> dict[str, Any] | None:
    """Complete required trait links and their durable projection before intake ACK.

    Non-trait records are unchanged. The same operation can repair a write whose
    canonical record was durable before its chain or acknowledgement completed.
    """
    from memory.engine import walker
    from memory.encoding_journal import (
        PROJECTION_OBLIGATION_KIND,
        _resolve_canonical_path,
        journal_status,
    )
    from memory.projection_delta import (
        open_projection_obligation,
        publish_final_projection_state,
        settle_projection_obligation,
    )

    memory_id = str(canonical_ref or "").strip()
    try:
        walker.load_coord_index()
        snapshot = walker.coord_index_snapshot()
        entry = snapshot.get(memory_id)
        path = walker.resolve_coord_index_path(entry.get("path")) if entry else None
        meta = walker.load_meta_by_path(path) if path is not None else None
        if not isinstance(meta, Mapping) or str(meta.get("id") or "") != memory_id:
            raise TraitRevisionError(f"canonical record unavailable for trait revision: {memory_id}")
        if _lobe_from_coord(meta.get("coord")) != 0 or meta.get("type") != "emergent_trait":
            return None
        obligation = open_projection_obligation(memory_id, path, "memory.trait_revision")
        if not obligation.get("durable"):
            raise TraitRevisionError(f"trait {memory_id} has no durable projection obligation")
        result = _link_trait_revision(str(path), dict(meta), snapshot, walker.load_meta_by_path)
        if result is not None and not result.get("ok"):
            raise TraitRevisionError(f"trait {memory_id} revision failed: {result.get('error') or result.get('reason')}")
        final = publish_final_projection_state(
            memory_id, path, hook_report={"supersedes": result}, sequenced=sequenced,
        )
        if not final.get("obligation_satisfied"):
            raise TraitRevisionError(f"trait {memory_id} final projection is not durable: {final.get('reason')}")
        status = journal_status()
        if status.get("error") or status.get("malformed_count"):
            raise TraitRevisionError(
                f"trait {memory_id} projection obligations unavailable: "
                f"{status.get('error') or status.get('malformed_lines')}"
            )
        obligation_ids = {obligation["journal_id"]} if obligation.get("journal_id") else set()
        for row in status["unclosed"]:
            if (row.get("kind") == PROJECTION_OBLIGATION_KIND
                    and row.get("owner") == "memory.trait_revision"
                    and row.get("memory_id") == memory_id
                    and row.get("final_path")
                    and Path(_resolve_canonical_path(row["final_path"])).resolve() == Path(path).resolve()):
                obligation_ids.add(row["journal_id"])
        # A failed attempt remains an obligation until this exact revision's
        # final bytes are published. Successful retry discharges those earlier
        # attempts too, without settling another owner or canonical file.
        for obligation_id in obligation_ids:
            if not settle_projection_obligation(obligation_id, memory_id, final):
                raise TraitRevisionError(f"trait {memory_id} projection acknowledgement failed")
        return result
    except TraitRevisionError:
        raise
    except Exception as exc:
        raise TraitRevisionError(f"trait {memory_id} revision unavailable: {exc}") from exc


def run_post_commit_memory_hooks(
    canonical_ref: str,
    *,
    coord: list[int] | tuple[int, ...] | None = None,
    source: str = "",
    enable_supersedes: bool = True,
) -> dict[str, Any]:
    """Run deterministic structure hooks for a committed canonical record.

    Returns a small report for tests/telemetry. Hook failures are surfaced in
    logs and report fields but do not roll back the already-committed memory.

    Cut 4 rework 3 (CUT4_REVIEW_REWORK_3 §4): the report distinguishes a
    CLEAN ZERO mutation set from a FAILED / UNCERTAIN one. ``entity_links`` is
    kept as the legacy count; ``entity_linker`` is the structured result
    ``{ok, links_made, mutation_uncertain, error, peer_rewrites}`` (each peer
    canonical file the back-link pass rewrote, so the write owner can emit
    its final delta instead of forcing a full projection rebuild); ``mutation_uncertain`` at
    the top level is True whenever any canonical-mutating hook may have
    touched canonical files without a mechanically known outcome (an exception
    after it started, a supersedes write that failed/reverted, an outer
    failure after hooks began). Unknown mutation set != zero mutation set: the
    write owner must hold durable rebuild-required evidence before it settles
    its projection obligation.
    """
    memory_id = str(canonical_ref or "").strip()
    report: dict[str, Any] = {
        "memory_id": memory_id,
        "source": str(source or ""),
        "ran": False,
        "supersedes": None,
        "entity_links": 0,
        "entity_linker": {"ok": True, "links_made": 0, "mutation_uncertain": False, "error": ""},
        "mutation_uncertain": False,
        "errors": [],
        # Cut 4 rework 4: canonical rewrites that FAILED ATOMICALLY (prior
        # bytes intact, outcome mechanically known) — surfaced, never hidden,
        # but distinct from `errors`, which feed the uncertainty rule
        "write_failures": [],
    }
    if not memory_id:
        report["errors"].append("missing canonical_ref")
        return report

    try:
        from memory.dewey_map import ENTITY_BEARING_LOBES

        lobe = _lobe_from_coord(coord)
        if lobe is not None and lobe not in ENTITY_BEARING_LOBES and lobe != 0:
            return report

        from memory.engine.walker import (
            COORD_INDEX,
            load_coord_index,
            load_meta_by_path,
            resolve_coord_index_path,
        )

        load_coord_index()
        entry = COORD_INDEX.get(memory_id)
        if not entry:
            report["errors"].append("coord_index entry missing")
            return report
        path = entry.get("path")
        if not path:
            report["errors"].append("coord_index path missing")
            return report
        resolved_path = resolve_coord_index_path(path)
        if resolved_path is None:
            report["errors"].append("coord_index path unresolvable")
            return report
        new_meta = load_meta_by_path(resolved_path)
        if not isinstance(new_meta, Mapping):
            report["errors"].append("canonical meta unreadable")
            return report
        lobe = _lobe_from_coord(new_meta.get("coord") or coord)
        if lobe == 0 and new_meta.get("type") == "emergent_trait":
            report["ran"] = True
            report["supersedes"] = _link_trait_revision(
                str(resolved_path), dict(new_meta), COORD_INDEX, load_meta_by_path,
            )
            result = report["supersedes"]
            if result and not result.get("ok"):
                report["mutation_uncertain"] = True
                report["errors"].append(f"trait revision: {result.get('error') or result.get('reason')}")
            return report
        if lobe is None or lobe not in ENTITY_BEARING_LOBES:
            return report

        report["ran"] = True
        if enable_supersedes:
            report["supersedes"] = _run_supersedes(
                memory_id,
                str(resolved_path),
                dict(new_meta),
                lobe,
                COORD_INDEX,
                load_meta_by_path,
            )
            sup = report["supersedes"]
            if isinstance(sup, Mapping) and sup.get("mutation_uncertain"):
                report["mutation_uncertain"] = True
                if sup.get("error") or sup.get("reason"):
                    report["errors"].append(f"supersedes: {sup.get('error') or sup.get('reason')}")
        linker = _run_entity_linker(
            memory_id,
            str(resolved_path),
            COORD_INDEX,
            lobe,
        )
        report["entity_linker"] = linker
        report["entity_links"] = int(linker.get("links_made") or 0)
        if linker.get("mutation_uncertain"):
            report["mutation_uncertain"] = True
            report["errors"].append(f"entity_linker: {linker.get('error') or 'uncertain'}")
        elif not linker.get("ok") and linker.get("error"):
            report["write_failures"].append(f"entity_linker: {linker.get('error')}")
        return report
    except Exception as exc:
        log.error(
            "post-commit hooks failed for %s: %s: %s",
            memory_id,
            type(exc).__name__,
            exc,
        )
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        if report["ran"]:
            # a hook had started: its mutation set is not mechanically known
            report["mutation_uncertain"] = True
        return report


def _link_trait_revision(path, new_meta, coord_index, load_meta_by_path):
    """Link an exact trait family, including interrupted forward/backward writes."""
    from memory.engine.walker import resolve_coord_index_path
    from memory.supersedes import link_supersedes

    trait = new_meta.get("trait") or {}
    name = trait.get("name")
    if not name:
        raise ValueError("identity trait revision has no trait name")
    family = {}
    for memory_id, entry in coord_index.items():
        if memory_id == new_meta["id"] or _lobe_from_coord(entry.get("coord")) != 0:
            continue
        prior_path = resolve_coord_index_path(entry.get("path"))
        if prior_path is None:
            raise ValueError(f"identity trait index path missing: {memory_id}")
        prior = load_meta_by_path(prior_path)
        if not isinstance(prior, Mapping) or str(prior.get("id") or "") != memory_id:
            raise ValueError(f"identity trait index record unavailable: {memory_id}")
        if prior.get("type") == "emergent_trait" and (prior.get("trait") or {}).get("name") == name:
            family[memory_id] = (prior_path, dict(prior))
    successor_id = new_meta.get("superseded_by")
    if successor_id:
        successor = family.get(successor_id)
        if successor is None or successor[1].get("supersedes") != new_meta["id"]:
            raise ValueError(f"identity trait {name!r} has an inconsistent successor link")
        return None
    prior_heads = [mid for mid, (_, meta) in family.items() if not meta.get("superseded_by")]
    incoming = [mid for mid, (_, meta) in family.items() if meta.get("superseded_by") == new_meta["id"]]
    prior_id = new_meta.get("supersedes")
    if len(incoming) > 1 or (prior_id and incoming and incoming != [prior_id]):
        raise ValueError(f"identity trait {name!r} has inconsistent predecessor links")
    if not prior_id:
        if incoming:
            prior_id = incoming[0]
        elif len(prior_heads) == 1:
            prior_id = prior_heads[0]
    if any(mid != prior_id for mid in prior_heads):
        raise ValueError(f"identity trait {name!r} has competing revision heads")
    if not prior_id:
        return None
    if prior_id not in family:
        raise ValueError(f"identity trait {name!r} predecessor is unavailable: {prior_id}")
    prior_path, prior = family[prior_id]
    if prior.get("superseded_by") not in (None, "", new_meta["id"]):
        raise ValueError(f"identity trait {name!r} predecessor belongs to another revision")
    if new_meta.get("supersedes") == prior_id and prior.get("superseded_by") == new_meta["id"]:
        return {"ok": True, "new_id": new_meta["id"], "prior_id": prior_id,
                "prior_path": str(prior_path), "already_linked": True}
    result = link_supersedes(path, new_meta, prior, str(prior_path))
    return {**dict(result), "prior_path": str(prior_path)}


def _run_supersedes(
    memory_id: str,
    path: str,
    new_meta: dict[str, Any],
    lobe: int,
    coord_index: dict[str, Any],
    load_meta_by_path: Any,
) -> dict[str, Any] | None:
    try:
        from memory.engine.walker import resolve_coord_index_path
        from memory.supersedes import detect_collision, link_supersedes

        prior = detect_collision(
            new_title=new_meta.get("title", ""),
            new_content=(new_meta.get("text") or new_meta.get("summary") or ""),
            new_id=memory_id,
            lobe=lobe,
            coord_index=coord_index,
            load_meta=load_meta_by_path,
            new_domain=new_meta.get("domain", ""),
            new_subtopic=new_meta.get("subtopic", ""),
        )
        if prior is None:
            return None
        prior_id = prior.get("id") or ""
        prior_entry = coord_index.get(prior_id)
        prior_path = (
            resolve_coord_index_path(prior_entry.get("path"))
            if prior_entry
            else None
        )
        if not prior_path:
            # detect_collision is read-only: nothing has been written yet
            return {"ok": False, "reason": "prior_path_missing", "prior_id": prior_id, "mutation_uncertain": False}
        result = link_supersedes(
            new_meta_path=path,
            new_meta=new_meta,
            prior_meta=prior,
            prior_path=prior_path,
        )
        # Cut 4: the prior record was rewritten (superseded_by); publish its
        # path so the write owner can emit that record's own projection work
        result = {**dict(result), "prior_path": str(prior_path)}
        if result.get("ok"):
            log.info(
                "supersedes: %s (v%d) supersedes %s (v%d)",
                memory_id,
                result.get("new_version"),
                prior_id,
                result.get("prior_version"),
            )
        else:
            log.error(
                "supersedes: link FAILED for %s -> %s phase=%s revert=%s error=%s",
                memory_id,
                prior_id,
                result.get("phase"),
                result.get("revert", "n/a"),
                result.get("error"),
            )
            # a write phase was attempted (new and/or prior file); whether the
            # files were left mutated is not mechanically known → uncertain
            result["mutation_uncertain"] = True
        return dict(result)
    except Exception as exc:
        log.error("supersedes hook failed for %s: %s: %s", memory_id, type(exc).__name__, exc)
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}", "mutation_uncertain": True}


def _run_entity_linker(
    memory_id: str,
    path: str,
    coord_index: dict[str, Any],
    lobe: int,
) -> dict[str, Any]:
    """Structured result: ``{ok, links_made, mutation_uncertain, error}``.
    ``mutation_uncertain`` is True on ANY exception — the linker writes
    forward links on the new record and back links on peer files, and an
    exception may land after some of those writes; a swallowed exception must
    never be reported as a clean zero (CUT4_REVIEW_REWORK_3 §4)."""
    try:
        from memory.enrichment.entity_linker import link_personal_memories
    except Exception as exc:  # noqa: BLE001 — import failure: nothing ran, nothing mutated
        log.error("entity_linker import failed for %s: %s", memory_id, exc)
        return {"ok": False, "links_made": 0, "mutation_uncertain": False, "error": f"import: {exc}"}
    try:
        writes: dict[str, Any] = {}
        links_made = int(link_personal_memories(path, coord_index, report=writes) or 0)
        if links_made:
            log.info(
                "entity_linker: %d link(s) added to %s (lobe=%d)",
                links_made,
                memory_id,
                lobe,
            )
        write_errors = list(writes.get("errors") or [])
        # only a linker that ENUMERATED its peer rewrites gets them represented
        # as projection work; a linker that did not report them at all leaves
        # the key absent so publication treats the peers as un-enumerable.
        peers = {"peer_rewrites": list(writes["peer_rewrites"])} if "peer_rewrites" in writes else {}
        if write_errors:
            # Cut 4 rework 4: the linker's rewrites are atomic — a failed
            # rewrite left that file's prior bytes intact (mechanically
            # proven), so this is a KNOWN outcome, not an uncertain one:
            # back-link failure → nothing mutated for that peer; forward-link
            # failure → the new record is intact and every peer that took a
            # back-link is counted in links_made (→ dirty at publication).
            log.error("entity_linker: %d atomic rewrite failure(s) for %s (bytes intact): %s",
                      len(write_errors), memory_id, "; ".join(write_errors))
            return {"ok": False, "links_made": links_made, "mutation_uncertain": False,
                    "error": "; ".join(write_errors),
                    **peers,
                    "back_link_failures": int(writes.get("back_link_failures") or 0),
                    "forward_link_failed": bool(writes.get("forward_link_failed"))}
        return {"ok": True, "links_made": links_made, "mutation_uncertain": False, "error": "", **peers}
    except Exception as exc:  # noqa: BLE001
        log.error("entity_linker FAILED for %s (mutation set uncertain): %s: %s", memory_id, type(exc).__name__, exc)
        return {"ok": False, "links_made": 0, "mutation_uncertain": True, "error": f"{type(exc).__name__}: {exc}"}


def _lobe_from_coord(coord: Any) -> int | None:
    try:
        return int(list(coord or [])[0])
    except (TypeError, ValueError, IndexError):
        return None

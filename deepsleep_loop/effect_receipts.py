"""Stable effect identity and store-embedded receipts for the DeepSleep terminus (WO-07).

``EpisodeStore.close`` keeps an episode open when its terminus raises and runs the whole
terminus again from the top. Every lane-local store was read-modify-write, so a retry applied
the same episode's effect a second time and — worse — reported a zero delta, because it
recomputed the effect from a store the first pass had already moved.

The codebase's own idiom for this is to embed the effect's identity in the state it changes
(``Trait.observation_count``, ``MasteryRecord.last_practice_episode``). This module generalises
it without a new store:

* :func:`effect_id` derives one stable id from the effect kind, the episode and the target, so
  a retry names the same effect;
* the store update and its receipt land in the SAME atomic write, under the store's existing
  lock — there is no window in which one exists without the other;
* a retry that finds its receipt RECONCILES from it: it applies nothing and reports what the
  receipt recorded, so the first pass's effect survives into the journal row, the canonical
  body and the sealed record.

Receipts are bounded per record (:data:`RECEIPT_LIMIT`). A retry is in-process and immediate
(the carrier is process-local, and a terminus failure is fatal to the live brain), so a record
would have to take more than that many OTHER episodes' effects between two attempts of one
terminus for the bound to matter.
"""

from __future__ import annotations

from typing import Any, Mapping

#: Receipts kept per store record, oldest dropped first.
RECEIPT_LIMIT = 64


def effect_id(kind: str, episode_id: str, key: str) -> str:
    """The one stable identity of an effect: what kind, applied by which episode, to what."""
    kind_text, episode_text, key_text = str(kind or ""), str(episode_id or ""), str(key or "")
    if not kind_text or not episode_text or not key_text:
        raise ValueError(
            f"an effect id needs a kind, an episode and a target; got "
            f"{kind_text!r}, {episode_text!r}, {key_text!r}"
        )
    return f"{kind_text}:{episode_text}:{key_text}"


def with_receipt(receipts: Mapping[str, Any] | None, effect: str,
                 receipt: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """*receipts* plus one, bounded to :data:`RECEIPT_LIMIT` (oldest first out)."""
    out = {str(key): dict(value) for key, value in dict(receipts or {}).items()}
    out.pop(effect, None)
    out[effect] = dict(receipt)
    while len(out) > RECEIPT_LIMIT:
        out.pop(next(iter(out)))
    return out


def receipts_for(receipts: Mapping[str, Any] | None, episode_id: str) -> dict[str, dict[str, Any]]:
    """The receipts *episode_id* left on one record, keyed by effect id, in application order."""
    wanted = str(episode_id or "")
    return {
        str(key): dict(value)
        for key, value in dict(receipts or {}).items()
        if isinstance(value, Mapping) and str(value.get("episode_id") or "") == wanted
    }


__all__ = ["RECEIPT_LIMIT", "effect_id", "receipts_for", "with_receipt"]

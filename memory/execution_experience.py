"""Execution-experience ledger — the mind's episodic memory of DOING.

Operator design 2026-07-08 ("teach the system how to remember what it does
and how to adapt and evolve from it"): the mastery store records what Omega
has LEARNED and (via grounding) what it has APPLIED as a scalar. This is the
other half — the EPISODIC record of each build ATTEMPT: what it set out to
do, whether the deterministic battery passed it, and (on failure) the exact
red checks that beat it.

Two consumers close the evolve loop:

  * ``failure_guidance_for_domain`` — the practice mint folds a domain's
    recent FAILURES into the next execution prompt, so Omega writes a better
    build order than last time ("previous attempts failed because X — address
    it"). A domain that was just conquered nags nobody (guidance clears once
    the latest outcome is a success).
  * ``experience_summary`` — the Omega packet surfaces the track record
    during metacognition, so the fold reasons over its own doing, not just
    its knowledge.

File-backed under the DeepSleep state dir (sibling to mastery.json), so
day-zero wipes it: a fresh mind has done nothing yet. Bounded ring per
domain; failure-soft end to end — a ledger hiccup never breaks a build.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from core.paths import OMEGA_ROOT

log = logging.getLogger("omega.memory.execution_experience")

_LEDGER_PATH = OMEGA_ROOT / "deepsleep_loop" / "state" / "execution_experience.json"

MAX_PER_DOMAIN = 8  # bounded ring — recent doing, not a lifelong archive
_GUIDANCE_CAP = 3  # how many past fails to fold into the next prompt
_REASON_CHARS = 200

_lock = threading.RLock()
_cache: Optional[Dict[str, List[Dict[str, Any]]]] = None


def _iso(ts: float) -> str:
    # ts passed in (Date.now-free constraint lives in workflows, not here) —
    # but keep it injectable-friendly: caller may omit and we stamp.
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _load() -> Dict[str, List[Dict[str, Any]]]:
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        if not _LEDGER_PATH.is_file():
            _cache = {}
            return _cache
        try:
            raw = json.loads(_LEDGER_PATH.read_text(encoding="utf-8"))
            _cache = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            log.warning("execution ledger unreadable — starting empty", exc_info=True)
            _cache = {}
        return _cache


def _persist() -> None:
    with _lock:
        if _cache is None:
            return
        try:
            _LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
            from pressure_loop.durable_write import write_text_atomic

            write_text_atomic(
                _LEDGER_PATH,
                json.dumps(_cache, ensure_ascii=False, indent=1) + "\n",
            )
        except Exception:
            log.warning("execution ledger persist failed (non-fatal)", exc_info=True)


def record_execution(
    *,
    domain: str,
    objective: str,
    outcome: str,
    red_reasons: Any = (),
    rounds: Any = 0,
    now: Optional[float] = None,
) -> None:
    """Append one build attempt's outcome for a domain. outcome is
    'success' (battery green) or 'stopped' (red checks unbeaten). No-op on
    empty domain; failure-soft."""
    d = str(domain or "").strip()
    if not d:
        return
    try:
        reasons = [str(r).strip()[:_REASON_CHARS] for r in (red_reasons or []) if str(r).strip()]
        entry = {
            "objective": str(objective or "")[:300],
            "outcome": "success" if str(outcome) == "success" else "stopped",
            "red_reasons": reasons[:5],
            "rounds": int(rounds or 0),
            "ts": _iso(time.time() if now is None else float(now)),
        }
        with _lock:
            store = _load()
            rows = list(store.get(d) or [])
            rows.append(entry)
            store[d] = rows[-MAX_PER_DOMAIN:]
            _persist()
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "memory",
                "execution_experience_recorded",
                status="ok",
                ok=True,
                summary=f"{d}: {entry['outcome']} (rounds={entry['rounds']})",
                domain=d,
                outcome=entry["outcome"],
            )
        except Exception:
            pass
    except Exception:
        log.warning("record_execution failed (non-fatal)", exc_info=True)


def recent_for_domain(domain: str, cap: int = MAX_PER_DOMAIN) -> List[Dict[str, Any]]:
    """A domain's attempts, NEWEST first, bounded by cap."""
    d = str(domain or "").strip()
    if not d:
        return []
    rows = list(_load().get(d) or [])
    rows.reverse()
    return rows[: max(0, int(cap))]


def failure_guidance_for_domain(domain: str, cap: int = _GUIDANCE_CAP) -> str:
    """A prompt fragment folding the domain's recent FAILURES into the next
    execution order — the learn-from-failure seam. Empty when the domain
    has no history OR its latest attempt succeeded (a just-conquered domain
    starts the next build clean)."""
    rows = recent_for_domain(domain)
    if not rows:
        return ""
    if rows[0]["outcome"] == "success":
        return ""  # latest run passed — no stale nagging
    fails = [r for r in rows if r["outcome"] == "stopped"][: max(1, int(cap))]
    if not fails:
        return ""
    lines = []
    for r in fails:
        reasons = "; ".join(r.get("red_reasons") or []) or "verification did not pass"
        lines.append(f"- a previous attempt failed: {reasons}")
    return (
        "Learn from your past attempts at this — do NOT repeat these failures:\n"
        + "\n".join(lines)
        + "\nWrite the project and its test so these specific checks pass this time."
    )


def experience_summary(cap_domains: int = 6) -> List[Dict[str, Any]]:
    """Per-domain track record for metacognition: {domain, attempts,
    successes, last_outcome}, most-active first. Failure-soft -> []."""
    try:
        store = _load()
        rows: List[Dict[str, Any]] = []
        for domain, entries in store.items():
            entries = entries or []
            if not entries:
                continue
            rows.append(
                {
                    "domain": domain,
                    "attempts": len(entries),
                    "successes": sum(1 for e in entries if e.get("outcome") == "success"),
                    "last_outcome": str(entries[-1].get("outcome") or ""),
                }
            )
        rows.sort(key=lambda r: -r["attempts"])
        return rows[: max(0, int(cap_domains))]
    except Exception:
        return []


def _reset_cache_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None

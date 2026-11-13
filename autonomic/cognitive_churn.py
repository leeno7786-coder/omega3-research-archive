"""Cognitive churn — one synchronous feedback surface per tick.

Unifies closed-loop signals that should prevent the substrate from
re-opening the same learning lane after repeated satisfied work:

  - autonomous outcome history (what happened)
  - temporal cycle dopamine (whether reward is still surprising)
  - mastery substrate (whether competence actually grew)

Consumers (same tick, read-only):

  - ``pressure_loop.pressure_field.evaluate_pressure`` attenuates
    ``curiosity_gap`` / ``learning_debt`` when a domain or goal is
    habituated.
  - ``core.symphony_global_action`` skips minting another investigate
    goal on a saturated curiosity thread.
  - ``metacognition.global_loop...reflective_reasoner`` flags stagnation
    for ``investigate-*`` goals (not only ``learn-*``).
  - ``autonomic.dopamine`` dampens per-cycle novelty when content
    fingerprints repeat within a domain.

Doctrine: pressure opens attention across many outlets (observe,
speak/hold, executive work, omega, memory). Churn is advisory —
it dampens *repeat* investigate remints and per-cycle novelty, never
zeros the curiosity contributor or overrides Global's outlet choice.
The mind still decides; feeds (trajectory, neuromods, operator,
memory, guardian, probes) stay in the aggregate.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Set, Tuple

from guardian.terminal_feedback import emit_phase, emit_signal

log = logging.getLogger("omega.autonomic.cognitive_churn")

# Repeated satisfied work without mastery growth → treat as habituated.
_MIN_ATTEMPTS_FOR_SATURATION = 3
_MIN_SATISFIED_FOR_SATURATION = 3
_SATISFIED_RATE_FLOOR = 0.55
_MASTERY_GROWTH_CEILING = 0.30

# Dopamine plateau: domain had enough cycles and mean RPE is flat/negative.
_MIN_CYCLES_FOR_DOPAMINE_PLATEAU = 3
_DOPAMINE_PLATEAU_MAX = 0.12

# Content fingerprint window per domain (temporal lobe + recent outcomes).
_CONTENT_FINGERPRINT_LIMIT = 8

_GENERIC_DOMAINS = frozenset({"knowledge", "tool", "general", "exploration"})

_LAST_SNAPSHOT: Optional["CognitiveChurnSnapshot"] = None


@dataclass(frozen=True)
class DomainChurn:
    domain: str
    attempts: int = 0
    satisfied: int = 0
    satisfied_rate: float = 0.0
    dopamine_avg: float = 0.0
    mastery: float = 0.0
    saturated: bool = False
    dopamine_plateau: bool = False
    content_fingerprints: Tuple[str, ...] = ()


@dataclass(frozen=True)
class GoalChurn:
    goal_id: str
    domain: str = ""
    attempts: int = 0
    satisfied: int = 0
    saturated: bool = False


@dataclass(frozen=True)
class CognitiveChurnSnapshot:
    """Read-only assessment for one conductor tick."""

    by_domain: Dict[str, DomainChurn] = field(default_factory=dict)
    by_goal_id: Dict[str, GoalChurn] = field(default_factory=dict)
    saturated_domains: FrozenSet[str] = frozenset()
    saturated_goal_ids: FrozenSet[str] = frozenset()
    dopamine_plateau_domains: FrozenSet[str] = frozenset()
    rotate_recommended: bool = False

    # Floors keep habituated lanes visible in the aggregate so Global
    # can still route to speak / observe / hold / omega — not only
    # another investigate→auto chain.
    _CURIOSITY_GOAL_SATURATED_FLOOR = 0.35
    _CURIOSITY_DOMAIN_SATURATED_FLOOR = 0.50

    def curiosity_pressure_multiplier(self) -> float:
        """Attenuate curiosity_gap after habituation (never silence)."""
        if not self.saturated_goal_ids and not self.saturated_domains:
            return 1.0
        if self.saturated_goal_ids:
            return self._CURIOSITY_GOAL_SATURATED_FLOOR
        if self.saturated_domains:
            return self._CURIOSITY_DOMAIN_SATURATED_FLOOR
        return 1.0

    def learning_debt_multiplier(self, *, primary_domain: str = "") -> float:
        """Scale learning_debt contributor — avoid drilling a dead lane."""
        dom = (primary_domain or "").strip().lower()
        if not dom:
            return 1.0
        if dom in self.saturated_domains:
            return 0.1
        if dom in self.dopamine_plateau_domains:
            return 0.35
        return 1.0

    def should_suppress_investigate_goal(self, goal_id: str) -> bool:
        """Block remint of the same investigate goal id only."""
        return goal_id in self.saturated_goal_ids

    def pack_advisory(self) -> Dict[str, Any]:
        """Non-authoritative hint for Global / audit (outlet choice stays elsewhere)."""
        return {
            "rotate_recommended": self.rotate_recommended,
            "saturated_domains": sorted(self.saturated_domains),
            "saturated_goal_ids": sorted(self.saturated_goal_ids),
            "dopamine_plateau_domains": sorted(self.dopamine_plateau_domains),
            "note": (
                "habituated_lane_present_prefer_non_repeat_outlets"
                if self.rotate_recommended
                else ""
            ),
        }

    def interpretation_lines(self) -> List[str]:
        """Human-facing lines for Global reflection (signals, not routing)."""
        lines: List[str] = []
        for gid in sorted(self.saturated_goal_ids):
            rec = self.by_goal_id.get(gid)
            dom = rec.domain if rec else ""
            lines.append(
                f"Habituation: goal '{gid}' has repeated satisfied work "
                f"without mastery growth in '{dom or 'unknown'}' — "
                "change tactic (tool, phrasing, or domain), not another "
                "identical investigate/auto pass"
            )
        for dom in sorted(self.saturated_domains):
            if any(
                (self.by_goal_id.get(g) or GoalChurn(goal_id=g)).domain == dom
                for g in self.saturated_goal_ids
            ):
                continue
            lines.append(
                f"Habituation: domain '{dom}' shows repeated satisfied "
                "cycles without mastery growth — rotate domain or pursue "
                "tool/missing-capability synthesis"
            )
        for dom in sorted(self.dopamine_plateau_domains):
            if dom in self.saturated_domains:
                continue
            lines.append(
                f"Reward plateau: domain '{dom}' dopamine expectation met "
                "or flat — novelty/RPE no longer teaching; try new evidence "
                "or operator clarification"
            )
        return lines

    def content_repetition_factor(self, domain: str, content: str) -> float:
        """0..1 novelty scale — 0 when this content matches recent domain work."""
        dom = (domain or "").strip().lower()
        if not dom or not content:
            return 1.0
        rec = self.by_domain.get(dom)
        if rec is None or not rec.content_fingerprints:
            return 1.0
        fp = _content_fingerprint(content)
        if not fp:
            return 1.0
        if fp in rec.content_fingerprints:
            return 0.0
        return 1.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "saturated_domains": sorted(self.saturated_domains),
            "saturated_goal_ids": sorted(self.saturated_goal_ids),
            "dopamine_plateau_domains": sorted(self.dopamine_plateau_domains),
            "rotate_recommended": self.rotate_recommended,
            "by_domain": {
                k: {
                    "attempts": v.attempts,
                    "satisfied": v.satisfied,
                    "satisfied_rate": v.satisfied_rate,
                    "dopamine_avg": v.dopamine_avg,
                    "mastery": v.mastery,
                    "saturated": v.saturated,
                    "dopamine_plateau": v.dopamine_plateau,
                }
                for k, v in self.by_domain.items()
            },
            "by_goal_id": {
                k: {
                    "domain": v.domain,
                    "attempts": v.attempts,
                    "satisfied": v.satisfied,
                    "saturated": v.saturated,
                }
                for k, v in self.by_goal_id.items()
            },
        }


def reset_churn_cache_for_tests() -> None:
    global _LAST_SNAPSHOT
    _LAST_SNAPSHOT = None


def assess_cognitive_churn(
    *,
    outcomes_path: Optional[Path] = None,
    mastery_path: Optional[Path] = None,
    outcome_limit: int = 40,
    cycle_limit: int = 30,
    force_refresh: bool = False,
) -> CognitiveChurnSnapshot:
    """Build the tick's churn snapshot (cached until reset).

    One assessment per conductor tick: ``evaluate_pressure`` builds the
    snapshot; later readers (action-pack, dopamine) reuse it without
    re-reading disk.
    """
    try:
        emit_phase("symphony", "cognitive_churn", status="enter", summary="assess_cognitive_churn")
    except Exception:
        pass
    global _LAST_SNAPSHOT
    if _LAST_SNAPSHOT is not None and not force_refresh:
        return _LAST_SNAPSHOT
    mastery = _load_mastery_scores(mastery_path)
    outcomes = _read_recent_outcomes(outcomes_path, limit=outcome_limit)
    summary = _summarise_outcomes(outcomes)
    domain_dopamine = _dopamine_avg_by_domain(cycle_limit=cycle_limit)
    domain_fingerprints = _content_fingerprints_by_domain(
        outcomes,
        cycle_limit=cycle_limit,
    )

    by_domain: Dict[str, DomainChurn] = {}
    by_goal: Dict[str, GoalChurn] = {}

    for dom, stats in (summary.get("by_domain") or {}).items():
        if not dom or dom == "(none)":
            continue
        attempts = int(stats.get("attempts") or 0)
        satisfied = int(stats.get("satisfied") or 0)
        rate = float(satisfied / attempts) if attempts else 0.0
        d_avg = float(domain_dopamine.get(dom, 0.0))
        m_score = float(mastery.get(dom, 0.0))
        saturated = (
            attempts >= _MIN_ATTEMPTS_FOR_SATURATION
            and satisfied >= _MIN_SATISFIED_FOR_SATURATION
            and rate >= _SATISFIED_RATE_FLOOR
            and m_score < _MASTERY_GROWTH_CEILING
        )
        plateau = (
            dom in domain_dopamine
            and attempts >= _MIN_CYCLES_FOR_DOPAMINE_PLATEAU
            and d_avg <= _DOPAMINE_PLATEAU_MAX
            and satisfied >= 2
        )
        by_domain[str(dom)] = DomainChurn(
            domain=str(dom),
            attempts=attempts,
            satisfied=satisfied,
            satisfied_rate=round(rate, 3),
            dopamine_avg=round(d_avg, 4),
            mastery=round(m_score, 3),
            saturated=saturated,
            dopamine_plateau=plateau,
            content_fingerprints=tuple(domain_fingerprints.get(dom, ())),
        )

    for gid, stats in (summary.get("by_goal_id") or {}).items():
        if not gid or gid == "(none)":
            continue
        attempts = int(stats.get("attempts") or 0)
        satisfied = int(stats.get("satisfied") or 0)
        dom = str(stats.get("domain") or "")
        if not dom:
            dom = _domain_from_goal_id(gid)
        m_score = float(mastery.get(dom, 0.0)) if dom else 0.0
        saturated = (
            attempts >= _MIN_ATTEMPTS_FOR_SATURATION
            and satisfied >= _MIN_SATISFIED_FOR_SATURATION
            and (m_score < _MASTERY_GROWTH_CEILING if dom else True)
        )
        by_goal[str(gid)] = GoalChurn(
            goal_id=str(gid),
            domain=dom,
            attempts=attempts,
            satisfied=satisfied,
            saturated=saturated,
        )

    saturated_domains = frozenset(d.domain for d in by_domain.values() if d.saturated)
    saturated_goals = frozenset(g.goal_id for g in by_goal.values() if g.saturated)
    plateau_domains = frozenset(d.domain for d in by_domain.values() if d.dopamine_plateau)
    rotate = bool(saturated_domains or saturated_goals or plateau_domains)

    _LAST_SNAPSHOT = CognitiveChurnSnapshot(
        by_domain=by_domain,
        by_goal_id=by_goal,
        saturated_domains=saturated_domains,
        saturated_goal_ids=saturated_goals,
        dopamine_plateau_domains=plateau_domains,
        rotate_recommended=rotate,
    )
    try:
        emit_phase(
            "symphony",
            "cognitive_churn",
            status="ok",
            summary=(
                f"sat_domains={len(saturated_domains)} "
                f"sat_goals={len(saturated_goals)} "
                f"plateau={len(plateau_domains)} "
                f"rotate={rotate}"
            )[:200],
            saturated_domain_count=len(saturated_domains),
            rotate_recommended=bool(rotate),
        )
        emit_signal(
            "symphony.churn.rotate_recommended",
            1.0 if rotate else 0.0,
            saturated_domains=len(saturated_domains),
            saturated_goals=len(saturated_goals),
        )
    except Exception:
        pass
    return _LAST_SNAPSHOT


def _content_fingerprint(text: str) -> str:
    normalized = " ".join((text or "").lower().split())
    if len(normalized) < 24:
        return ""
    sample = normalized[:512]
    return hashlib.sha256(sample.encode("utf-8")).hexdigest()[:16]


def _domain_from_goal_id(goal_id: str) -> str:
    gid = (goal_id or "").strip()
    for prefix in ("learn-", "deepen-", "investigate-"):
        if gid.startswith(prefix):
            tail = gid[len(prefix) :]
            if prefix == "investigate-":
                return "knowledge"
            return tail.replace("-", "_")
    return ""


def _summarise_outcomes(outcomes: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not outcomes:
        empty = {
            "count": 0,
            "satisfied": 0,
            "satisfied_rate": 0.0,
            "by_goal_id": {},
            "by_domain": {},
        }
        try:
            from guardian.terminal_feedback import emit_data

            emit_data(
                "autonomic",
                "churn_summarize",
                count=0,
                satisfied=0,
                domains=0,
                ok=True,
            )
        except Exception:
            pass
        return empty
    by_goal: Dict[str, Dict[str, Any]] = {}
    by_domain: Dict[str, Dict[str, int]] = {}
    sat_count = 0
    for o in outcomes:
        score = o.get("score") if isinstance(o.get("score"), dict) else {}
        kind = score.get("cycle_kind")
        sat = bool(kind == "success")
        sat_count += int(sat)

        # We now use the intent as the "goal_id" tracker equivalent
        gid = str(score.get("intent") or o.get("intent") or "(none)")
        dom = str(o.get("domain") or score.get("domain") or "(none)")

        gbucket = by_goal.setdefault(
            gid,
            {"attempts": 0, "satisfied": 0, "domain": dom},
        )
        gbucket["attempts"] += 1
        gbucket["satisfied"] += int(sat)
        if dom and dom != "(none)":
            gbucket["domain"] = dom

        dbucket = by_domain.setdefault(dom, {"attempts": 0, "satisfied": 0})
        dbucket["attempts"] += 1
        dbucket["satisfied"] += int(sat)

    n = len(outcomes)
    result = {
        "count": n,
        "satisfied": sat_count,
        "satisfied_rate": round(sat_count / n, 3) if n else 0.0,
        "by_goal_id": by_goal,
        "by_domain": by_domain,
    }
    try:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "autonomic",
            "churn_summarize",
            count=n,
            satisfied=sat_count,
            domains=len(by_domain),
            ok=True,
        )
    except Exception:
        pass
    return result


def _read_recent_outcomes(path: Optional[Path], *, limit: int) -> List[Dict[str, Any]]:
    if path is not None:
        if not isinstance(path, Path):
            path = Path(str(path))
        if not path.exists():
            return []
        import json

        outcomes = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        outcomes.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            return []
        return outcomes[-limit:] if limit > 0 else outcomes

    # Instead of reading a stale JSONL file, we use the active temporal history
    # which aggregates the recent live cycle events directly from memory.
    try:
        from autonomic import _history

        return _history.recent_cycles(limit=limit)
    except Exception as exc:
        log.debug("cognitive_churn: outcomes read failed (%s)", exc)
        try:
            from guardian.terminal_feedback import emit_data, emit_guardian_event

            emit_data(
                "autonomic",
                "churn_load",
                operation="read_recent_outcomes",
                path="temporal_history",
                ok=False,
                error=str(exc),
            )
            emit_guardian_event(
                "omega.autonomic",
                "churn_load",
                operation="read_recent_outcomes",
                path="temporal_history",
                ok=False,
            )
        except Exception:
            pass
        return []


def _load_mastery_scores(path: Optional[Path]) -> Dict[str, float]:
    if path is None:
        try:
            from pressure_loop.policy import deepsleep_mastery_path

            path = deepsleep_mastery_path()
        except Exception:
            return {}
    if not isinstance(path, Path):
        path = Path(str(path))
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        log.debug("cognitive_churn: mastery read failed (%s)", exc)
        try:
            from guardian.terminal_feedback import emit_data, emit_guardian_event

            emit_data(
                "autonomic",
                "churn_load",
                operation="load_mastery_scores",
                path=str(path),
                ok=False,
                error=str(exc),
            )
            emit_guardian_event(
                "omega.autonomic",
                "churn_load",
                operation="load_mastery_scores",
                path=str(path),
                ok=False,
            )
        except Exception:
            pass
        return {}
    out: Dict[str, float] = {}
    if not isinstance(raw, dict):
        return out
    for domain, rec in raw.items():
        if isinstance(rec, dict):
            try:
                out[str(domain)] = float(rec.get("mastery") or 0.0)
            except (TypeError, ValueError):
                continue
    try:
        from guardian.terminal_feedback import emit_data, emit_guardian_event

        emit_data(
            "autonomic",
            "churn_load",
            operation="load_mastery_scores",
            path=str(path),
            ok=True,
            domains=len(out),
        )
        emit_guardian_event(
            "omega.autonomic",
            "churn_load",
            operation="load_mastery_scores",
            path=str(path),
            ok=True,
            domains=len(out),
        )
    except Exception:
        pass
    return out


def _dopamine_avg_by_domain(*, cycle_limit: int) -> Dict[str, float]:
    try:
        from autonomic import _history
        from config.loader import load_dopamine_config
    except Exception as exc:
        log.debug("cognitive_churn: dopamine history unavailable (%s)", exc)
        return {}
    weights = load_dopamine_config().get("dopamine", {}).get("weights", {})
    cycles = _history.recent_cycles(limit=max(cycle_limit * 4, 50))
    by_dom: Dict[str, List[float]] = {}
    for c in cycles:
        dom = str(c.get("domain") or "")
        if not dom:
            score_block = c.get("score") if isinstance(c.get("score"), dict) else {}
            dom = str(score_block.get("domain") or "")
        if not dom:
            continue
        s = c.get("score") if isinstance(c.get("score"), dict) else {}
        d = s.get("dopamine") if isinstance(s, dict) else None
        if not isinstance(d, dict):
            continue
        val = d.get("score")
        if not isinstance(val, (int, float)):
            outcomes = _history.extract_dopamine_outcomes([c], weights)
            val = outcomes[0] if outcomes else None
        if isinstance(val, (int, float)):
            by_dom.setdefault(dom, []).append(float(val))
    return {dom: (sum(vals) / len(vals) if vals else 0.0) for dom, vals in by_dom.items()}


def _content_fingerprints_by_domain(
    outcomes: List[Dict[str, Any]],
    *,
    cycle_limit: int,
) -> Dict[str, Tuple[str, ...]]:
    fps: Dict[str, Set[str]] = {}
    for o in reversed(outcomes):
        dom = str((o.get("meta_packet") or {}).get("domain") or "")
        if not dom:
            continue
        blob = json.dumps(o.get("goal_satisfied") or {}, sort_keys=True)
        fp = _content_fingerprint(blob)
        if fp:
            bucket = fps.setdefault(dom, set())
            if len(bucket) < _CONTENT_FINGERPRINT_LIMIT:
                bucket.add(fp)
    try:
        from autonomic import _history

        for c in _history.recent_cycles(limit=cycle_limit):
            dom = str(c.get("domain") or "")
            if not dom:
                continue
            text = str(c.get("text") or c.get("summary") or "")[:800]
            fp = _content_fingerprint(text)
            if fp:
                bucket = fps.setdefault(dom, set())
                if len(bucket) < _CONTENT_FINGERPRINT_LIMIT:
                    bucket.add(fp)
    except Exception as exc:
        from guardian.terminal_feedback import emit_data

        emit_data(
            "autonomic",
            "churn_fingerprints",
            ok=False,
            error=str(exc)[:120],
        )
    return {k: tuple(sorted(v)) for k, v in fps.items()}


def primary_learning_debt_domain(envelope: Optional[Dict[str, Any]]) -> str:
    """Best-effort domain target from learning_state envelope."""
    if not isinstance(envelope, dict):
        return ""
    mastery = envelope.get("mastery") or {}
    if not isinstance(mastery, dict):
        return ""
    top_weak = mastery.get("top_weak") or []
    if not isinstance(top_weak, list):
        return ""
    for row in top_weak:
        if not isinstance(row, dict):
            continue
        dom = str(row.get("domain") or "").strip().lower()
        if dom and dom not in _GENERIC_DOMAINS:
            return dom
    for row in top_weak:
        if isinstance(row, dict):
            dom = str(row.get("domain") or "").strip().lower()
            if dom:
                return dom
    return ""

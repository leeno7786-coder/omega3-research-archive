"""Phase 5a/5b of the cognition+CLI merge — the execution_gap pressure contributor.

execution_gap opens an APPLY episode when a domain has been LEARNED
(knowledge_mastery >= target) but NOT yet APPLIED (application_mastery < target),
gated by a memory-warmth practice bar. It is DISJOINT from learning_debt
(knowledge < target) — the two never count the same domain.

Phase 5b sharpened the practice bar from GLOBAL aggregate warmth to PER-DOMAIN
warmth: a learned-but-unapplied domain becomes practice-ready only when ITS OWN
memory substrate is hot. A hot but unrelated domain must NOT manufacture
execution pressure (the 5a aggregate bar would have — practicing into the void).
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import memory.symphony_strength as symphony_strength  # noqa: E402
import pressure_loop.pressure_field as pf  # noqa: E402
from core.shared_state import SharedState  # noqa: E402


def _rec(
    knowledge: float,
    application: float,
    target: float = 0.7,
    domain_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        knowledge_mastery=knowledge,
        application_mastery=application,
        target_mastery=target,
        domain_id=domain_id,
    )


def _patch_mastery(monkeypatch, records: dict) -> None:
    monkeypatch.setattr("deepsleep_loop.mastery_store.load_mastery", lambda *a, **k: records)


def _patch_hot(monkeypatch, hot_by_domain: dict, default: int = 0) -> None:
    """Inject deterministic per-domain hotness (Phase 5b practice bar).

    ``read_execution_gap`` consults ``hot_count_for_domain`` once per
    learned-but-unapplied domain; this maps each domain to a fixed hot count
    so the gate is exercised without a real coord index / StrengthIndex.
    """
    monkeypatch.setattr(
        symphony_strength,
        "hot_count_for_domain",
        lambda domain, **_kw: int(hot_by_domain.get(domain, default)),
    )


def _enable(monkeypatch) -> None:
    # execution_gap is ON by default now (the staged-approval GUARD in
    # run_guarded_apply, not this flag, is the boundary that prevents real
    # execution). Kept as an explicit bind so each logic test stays self-evident
    # and survives a future default change.
    monkeypatch.setattr(pf, "EXECUTION_GAP_ENABLED", True)


def test_execution_gap_enabled_by_default(monkeypatch):
    # New contract (flag armed): execution_gap fires for a learned-but-unapplied
    # OWN-hot domain WITHOUT any test enabling it. The flag is now the default-ON
    # application drive; real code execution is gated downstream by
    # run_guarded_apply (staged approval), never by keeping this contributor dark.
    _patch_mastery(monkeypatch, {"python": _rec(0.9, 0.2)})
    _patch_hot(monkeypatch, {"python": 3})
    assert pf.EXECUTION_GAP_ENABLED is True
    assert pf.read_execution_gap(SharedState()) > 0.0


def test_execution_gap_dormant_when_flag_forced_off(monkeypatch):
    # Kill-switch: forcing the master switch OFF keeps execution_gap fully inert
    # even with a learned-but-unapplied HOT domain that WOULD otherwise fire —
    # proving the override genuinely disables the drive, not just that the default
    # happens to be off.
    monkeypatch.setattr(pf, "EXECUTION_GAP_ENABLED", False)
    _patch_mastery(monkeypatch, {"python": _rec(0.9, 0.2)})
    _patch_hot(monkeypatch, {"python": 3})
    assert pf.EXECUTION_GAP_ENABLED is False
    assert pf.read_execution_gap(SharedState()) == 0.0


def test_execution_gap_fires_when_learned_unapplied_and_hot(monkeypatch):
    _enable(monkeypatch)
    _patch_mastery(monkeypatch, {"python": _rec(0.9, 0.2)})  # learned, not applied
    _patch_hot(monkeypatch, {"python": 3})  # its own substrate hot
    assert pf.read_execution_gap(SharedState()) > 0.0


def test_no_execution_gap_when_still_learning(monkeypatch):
    _enable(monkeypatch)
    # knowledge < target → this is learning_debt's domain, NOT execution.
    _patch_mastery(monkeypatch, {"python": _rec(0.4, 0.2)})
    _patch_hot(monkeypatch, {"python": 3})
    assert pf.read_execution_gap(SharedState()) == 0.0


def test_no_execution_gap_when_already_applied(monkeypatch):
    _enable(monkeypatch)
    _patch_mastery(monkeypatch, {"python": _rec(0.9, 0.8)})  # application >= target
    _patch_hot(monkeypatch, {"python": 3})
    assert pf.read_execution_gap(SharedState()) == 0.0


def test_execution_gap_gated_by_cold_memory(monkeypatch):
    _enable(monkeypatch)
    # Practice bar: a ready domain but a cold OWN substrate → no execution pressure.
    _patch_mastery(monkeypatch, {"python": _rec(0.9, 0.2)})
    _patch_hot(monkeypatch, {"python": 0})
    assert pf.read_execution_gap(SharedState()) == 0.0


def test_execution_gap_not_fired_by_hot_unrelated_domain(monkeypatch):
    # The crux of Phase 5b: rust is learned-but-unapplied, but rust's own memory
    # is cold; python is hot but is NOT a practice-ready domain. The 5a aggregate
    # bar (global hot_count) WOULD have fired here — practicing rust into a cold
    # rust-substrate. The per-domain bar must NOT.
    _enable(monkeypatch)
    _patch_mastery(monkeypatch, {"rust": _rec(0.9, 0.2)})
    _patch_hot(monkeypatch, {"python": 5, "rust": 0})
    assert pf.read_execution_gap(SharedState()) == 0.0


def test_execution_gap_fires_only_for_self_hot_domains(monkeypatch):
    _enable(monkeypatch)
    # rust is practice-ready (learned, unapplied, own substrate hot); go is
    # learned+unapplied but cold → only rust counts → 1 ready domain (<= growing
    # threshold) → BASE_GROWING.
    _patch_mastery(monkeypatch, {"rust": _rec(0.9, 0.2), "go": _rec(0.9, 0.1)})
    _patch_hot(monkeypatch, {"rust": 2, "go": 0})
    assert pf.read_execution_gap(SharedState()) == pf.EXECUTION_GAP_BASE_GROWING


def test_execution_gap_escalates_to_base_early_when_many_self_hot(monkeypatch):
    # When MORE than the growing threshold of domains are practice-ready ON THEIR
    # OWN warmth, the contributor escalates to BASE_EARLY. Pins the exact upper
    # tier through the per-domain path (the cap test alone can't distinguish
    # BASE_EARLY from BASE_GROWING — both sit under the cap).
    _enable(monkeypatch)
    n = pf.EXECUTION_GAP_THRESHOLD_GROWING + 1
    _patch_mastery(monkeypatch, {f"d{i}": _rec(0.9, 0.1) for i in range(n)})
    _patch_hot(monkeypatch, {f"d{i}": 1 for i in range(n)})  # all own-hot
    assert pf.read_execution_gap(SharedState()) == pf.EXECUTION_GAP_BASE_EARLY


def test_execution_gap_weight_uses_self_hot_count_not_candidate_count(monkeypatch):
    # Five learned-but-unapplied candidates, but only ONE is own-hot → the weight
    # tier must be chosen by the self-hot count (1 → BASE_GROWING), NOT the raw
    # candidate count (5 → would be BASE_EARLY). Guards the 5b contract directly.
    _enable(monkeypatch)
    _patch_mastery(monkeypatch, {f"d{i}": _rec(0.9, 0.1) for i in range(5)})
    _patch_hot(monkeypatch, {"d0": 3})  # only d0 own-hot; d1..d4 cold
    assert pf.read_execution_gap(SharedState()) == pf.EXECUTION_GAP_BASE_GROWING


def test_application_gap_count_is_disjoint_from_learning_gap(monkeypatch):
    # A learning gap (knowledge < target) must NOT register as an execution gap,
    # so execution_gap and learning_debt never double-count the same domain.
    _patch_mastery(monkeypatch, {"learning": _rec(0.3, 0.0), "applied": _rec(0.9, 0.9)})
    assert pf._application_gap_count() == 0


def test_application_gap_domains_lists_practice_ready_candidates(monkeypatch):
    # The candidate set (before the per-domain warmth bar) is exactly the
    # learned-but-unapplied domains, keyed by domain_id.
    _patch_mastery(
        monkeypatch,
        {
            "learning": _rec(0.3, 0.0),  # still learning → excluded
            "rust": _rec(0.9, 0.2),  # learned, unapplied → candidate
            "applied": _rec(0.9, 0.9),  # already applied → excluded
        },
    )
    assert pf._application_gap_domains() == ["rust"]


def test_application_gap_domains_prefers_record_domain_id(monkeypatch):
    # When a MasteryRecord carries its own domain_id, that wins over the dict key.
    _patch_mastery(
        monkeypatch,
        {
            "stale_key": _rec(0.9, 0.2, domain_id="rust"),
        },
    )
    assert pf._application_gap_domains() == ["rust"]


def test_execution_gap_weight_respects_cap(monkeypatch):
    _enable(monkeypatch)
    many = {f"d{i}": _rec(0.9, 0.1) for i in range(5)}
    _patch_mastery(monkeypatch, many)
    _patch_hot(monkeypatch, {f"d{i}": 1 for i in range(5)})  # all five own-hot
    w = pf.read_execution_gap(SharedState())
    assert 0.0 < w <= pf.EXECUTION_GAP_HARD_CAP


def test_execution_gap_registered_and_routed():
    assert pf.CONTRIBUTOR_EXECUTION_GAP == "execution_gap"
    assert pf.CONTRIBUTOR_EXECUTION_GAP in pf.ALL_CONTRIBUTORS
    assert pf.CONTRIBUTOR_EXECUTION_GAP in pf._DEFAULT_READERS
    from pressure_loop.pressure import _FIELD_ID_TO_TYPE

    assert _FIELD_ID_TO_TYPE.get("execution_gap") == "execution_gap"


def test_execution_gap_does_not_require_preexisting_build_state(monkeypatch):
    """The gap is a drive; Meta A authors the new build objective in its episode."""
    _patch_mastery(monkeypatch, {"python": _rec(0.9, 0.2)})
    _patch_hot(monkeypatch, {"python": 3})
    assert pf.read_execution_gap(SharedState()) > 0.0

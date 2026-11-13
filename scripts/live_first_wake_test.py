"""Live first-wake test (additive — no factory wipe).

Onboard the operator via seed_first_wake, then prove the chain on the LIVE
substrate: seeded subcategories -> mastery gaps (target>0, actual=0) -> the
dynamic-Y flag flips to {2,8} -> learning_debt rises (gap-based) -> the mind
resolves a real learning topic (ideally a seeded one). This is the cold-mind
fuel: onboarding mints the first LEARN target instead of the starved floor.
"""

from __future__ import annotations

from core.shared_state import SharedState
from cognition.topic_resolution.resolve_topic import (
    NO_TOPIC_AVAILABLE,
    resolve_learning_topic,
)
from deepsleep_loop.mastery_store import load_mastery, normalize_domain_id
from hud.operator_onboarding import seed_first_wake
from memory.subdomain_flag import dynamic_y_lobes
from pressure_loop import pressure_field

OCCUPATION = "software_engineer"
SUBS = ["Python", "Distributed Systems", "System Design"]


def main() -> int:
    print("=== PRE-ONBOARD ===")
    print("mastery domains:", len(load_mastery()))
    print("dynamic_y_lobes:", set(dynamic_y_lobes()))
    print("learning_debt:", round(pressure_field.read_learning_debt(SharedState()), 4))

    print("\n=== ONBOARD (seed_first_wake) ===")
    result = seed_first_wake(OCCUPATION, SUBS)
    print(f"occupation: {result['occupation']} -> lobe {result['lobe']}")
    for s in result["seeded"]:
        print(f"  seeded  {s['subcategory']!r} -> {s['dirname']} (lobe {s['lobe']}, y={s['y']})")
    if result["failed"]:
        print("  FAILED:", result["failed"])

    print("\n=== VERIFY CHAIN (live substrate) ===")
    flag = set(dynamic_y_lobes())
    print("dynamic_y_lobes flipped to:", flag)
    recs = load_mastery()
    gaps_ok = True
    for sub in SUBS:
        r = recs.get(normalize_domain_id(sub))
        k = r.knowledge_mastery if r else None
        t = r.target_mastery if r else None
        is_gap = bool(r) and float(k) < float(t)
        gaps_ok = gaps_ok and is_gap
        print(f"  gap[{sub}]: knowledge={k} target={t}  {'GAP' if is_gap else 'MISSING'}")

    debt = pressure_field.read_learning_debt(SharedState())
    print("learning_debt (gap-based):", round(debt, 4))

    resolved = resolve_learning_topic(_empty_meta_state())
    print(f"resolve_learning_topic -> topic={resolved.topic!r} source={resolved.source}")
    seeded_titles = {s["subcategory"].lower() for s in result["seeded"]}
    on_seeded = resolved.topic.strip().lower() in seeded_titles

    ok = flag == {2, 8} and gaps_ok and debt > 0.0 and resolved.topic != NO_TOPIC_AVAILABLE
    print("\nRESULT:", "PASS" if ok else "CHECK")
    print(" - flag flipped to {2,8}:", flag == {2, 8})
    print(" - all subcategories are mastery gaps:", gaps_ok)
    print(" - learning_debt > 0 (justified by the gaps):", debt > 0.0)
    print(" - mind resolves a real LEARN topic:", resolved.topic != NO_TOPIC_AVAILABLE)
    print(" - and it's one of the seeded subdomains:", on_seeded)
    return 0 if ok else 1


def _empty_meta_state():
    """Minimal MetaAState so resolve_learning_topic reads the substrate gaps."""
    try:
        from tests.cognition.test_topic_resolution import make_meta_a_state

        return make_meta_a_state(target_str="", text="")
    except Exception:
        from meta_loop._meta_a_state import MetaAInput, MetaAState

        mi = MetaAInput(
            episode_id="first-wake-test",
            pressure={"reason": "first_wake", "contributors": []},
            trajectory_snapshot={},
            action_goal={"intent": "LEARN", "target": ""},
        )
        return MetaAState(mi)


if __name__ == "__main__":
    raise SystemExit(main())

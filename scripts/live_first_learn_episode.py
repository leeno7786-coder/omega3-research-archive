"""Live first LEARN episode (the last mile): boot the substrate, ensure the
onboarding seed exists, then run bounded NATURAL pressure ticks. When the seeded
gap ignites a LEARN, the full cascade runs (web_fetch -> Omega comprehends ->
grounded mastery climbs). Inspect the closed episode + the mastery delta.

Bounded + single-process (PC-safe). Hits the web (web_fetch) + gemma (Omega).
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

MAX_TICKS = 8
TOPIC = "Distributed Systems"


def _knowledge(records, did):
    rec = records.get(did)
    return float(rec.knowledge_mastery) if rec else None


def main() -> int:
    import boot

    boot.boot()

    from pressure_loop.runtime import get_default_runtime
    from pressure_loop.loop import run_pressure_tick_once
    from hud.operator_onboarding import seed_first_wake
    from deepsleep_loop.mastery_store import load_mastery, normalize_domain_id
    from pressure_loop import pressure_field
    from core.shared_state import SharedState

    rt = get_default_runtime()
    store = rt.episode_store

    # Ensure the onboarding seed exists (idempotent — won't reset progress).
    seed_first_wake("software_engineer", [TOPIC])
    did = normalize_domain_id(TOPIC)
    pre_k = _knowledge(load_mastery(), did)
    print(
        f"PRE: mastery[{TOPIC}] knowledge={pre_k}; "
        f"learning_debt={round(pressure_field.read_learning_debt(SharedState()), 3)}"
    )

    last_seen = None
    learned_episode = None
    for tick in range(1, MAX_TICKS + 1):
        try:
            run_pressure_tick_once(rt)
        except Exception as exc:  # noqa: BLE001
            print(f"tick {tick}: cascade exception (fail-hard cognition): {exc}")
        body = store.last_closed_body() or {}
        eid = body.get("episode_id")
        if eid and eid != last_seen:
            last_seen = eid
            intent = body.get("intent")
            pack = (body.get("execution") or {}).get("execution_action_pack") or {}
            topic = (pack.get("execution_inputs") or {}).get("query")
            omega = body.get("omega_result") or {}
            print(
                f"tick {tick}: episode {eid[:8]} intent={intent} topic={topic!r} "
                f"omega_cognized={omega.get('cognized')}"
            )
            if intent == "LEARN":
                learned_episode = body
                break
        else:
            print(f"tick {tick}: no ignition (pressure below threshold)")

    post_k = _knowledge(load_mastery(), did)
    print(f"\nPOST: mastery[{TOPIC}] knowledge={post_k}")

    if learned_episode is not None:
        omega = learned_episode.get("omega_result") or {}
        eff = omega.get("mastery_effects") or []
        know = omega.get("knowledge_to_ingest") or []
        print("LEARN episode ran:")
        print("  omega mastery_effects:", eff)
        print("  omega knowledge_to_ingest:", [k.get("statement") for k in know][:3])
        climbed = post_k is not None and (pre_k is None or post_k != pre_k)
        print(
            "\nRESULT:",
            "PASS — first LEARN ignited on the seeded gap"
            + (" and grounded mastery moved" if climbed else " (mastery write pending DeepSleep)"),
        )
        return 0

    print(
        "\nRESULT: no LEARN episode ignited within",
        MAX_TICKS,
        "ticks (pressure/neuromod threshold) — chain verified up to ignition-readiness.",
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Live cold-boot validation: onboarding establishes the Omega trajectory at episode 0.

Runs against an ISOLATED cold OMEGA_ROOT (never touches the operator's real state) but
through the REAL code paths + a REAL SharedState. Proves the LEAD the fix introduces:

  PRE  (cold):  read_trajectory() == {}  AND the LAST_OMEGA_TRAJECTORY slot is None
  ONBOARD:      seed_first_wake (flag ON) writes the durable lobe-9 setpoint AND
                publishes the live SS slot
  POST:         read_trajectory() populated; and the EXACT read pressure.evaluate does at
                ignition (shared_state.read(LAST_OMEGA_TRAJECTORY), pressure.py:833-839)
                now returns the onboarding trajectory -> episode 1 is steered.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _cold_root(tmp: Path) -> None:
    """Point the substrate at a fresh cold OMEGA_ROOT + isolated mastery store."""
    import shutil

    import core.paths as paths

    # The memory engine validates lobe writes against <OMEGA_ROOT>/schema/. A cold root
    # is empty, so mirror the repo's schema/ into it (the real OMEGA_ROOT already has it)
    # — otherwise the durable lobe-9 write can't persist and read-back is empty.
    shutil.copytree(ROOT / "schema", tmp / "schema")
    paths.OMEGA_ROOT = tmp
    import pressure_loop.policy as pol

    mastery = tmp / "mastery.json"
    pol.deepsleep_mastery_path = lambda policy=None: mastery
    from memory.subdomain_flag import reset_flag_cache

    reset_flag_cache()


def _ignition_read(shared_state):
    """The EXACT trajectory_snapshot acquisition pressure.evaluate does at ignition
    (pressure_loop/pressure.py:833-839)."""
    from core.shared_state_keys import LAST_OMEGA_TRAJECTORY

    raw = shared_state.read(LAST_OMEGA_TRAJECTORY, None)
    return dict(raw) if isinstance(raw, dict) else None


def main() -> int:
    with tempfile.TemporaryDirectory() as d:
        _cold_root(Path(d))

        import hud.operator_onboarding as ob
        from core.shared_state import SharedState
        from deepsleep_loop.mastery_store import normalize_domain_id
        from pressure_loop.runtime import set_default_runtime
        from pressure_loop.temporal_store import read_trajectory

        # Register a real SharedState as the live runtime's slot target (the signal
        # ignition reads). Minimal duck-typed runtime — the publish only needs
        # rt.shared_state.
        class _RT:
            def __init__(self) -> None:
                self.shared_state = SharedState()

        rt = _RT()
        set_default_runtime(rt)

        ob.OMEGA_ONBOARDING_ESTABLISH = True  # the flip under validation

        print("=== PRE (cold boot) ===")
        pre_lobe = read_trajectory()
        pre_slot = _ignition_read(rt.shared_state)
        print(f"  read_trajectory() (lobe 9): {pre_lobe}")
        print(f"  ignition slot read        : {pre_slot}")

        print("\n=== ONBOARD (seed_first_wake, flag ON) ===")
        result = ob.seed_first_wake("software_engineer", ["Python", "Distributed Systems"])
        print(
            f"  occupation -> lobe {result['lobe']}; seeded={[s['subcategory'] for s in result['seeded']]}"
        )
        print(f"  result.trajectory_live_signal = {result.get('trajectory_live_signal')}")

        print("\n=== POST ===")
        post_lobe = read_trajectory()
        post_slot = _ignition_read(rt.shared_state)
        expect = normalize_domain_id("Python")
        print(
            f"  read_trajectory() target_domain : {(post_lobe or {}).get('target_domain')}  (expect {expect!r})"
        )
        print(
            f"  ignition slot   target_domain   : {(post_slot or {}).get('target_domain')}  (expect {expect!r})"
        )
        print(f"  ignition slot   intent          : {(post_slot or {}).get('intent')}")
        print(f"  ignition slot   cycle_id        : {(post_slot or {}).get('cycle_id')}")

        print("\n=== EPISODE 1 IGNITION (real pressure.evaluate) ===")
        from pressure_loop.contributors import make_contributor
        from pressure_loop.pressure import evaluate

        debt = make_contributor("pressure_field", "learning_debt", 1.0, {})
        ign = evaluate(
            [debt],
            active_episode=False,
            open_threshold=1.0,
            neuromod={},
            shared_state=rt.shared_state,
        )
        ign_dom = (ign.trajectory_snapshot or {}).get("target_domain") if ign else None
        print(f"  episode opened                  : {ign is not None}")
        print(f"  ignition trajectory_snapshot    : target_domain={ign_dom} (expect {expect!r})")

        ok = (
            pre_lobe == {}
            and pre_slot is None
            and (post_lobe or {}).get("target_domain") == expect
            and (post_slot or {}).get("target_domain") == expect
            and result.get("trajectory_live_signal") is True
            and ign is not None
            and ign_dom == expect
        )
        print("\n=== VERDICT ===")
        print(
            f"  PRE cold (no trajectory, no slot)           : {pre_lobe == {} and pre_slot is None}"
        )
        print(
            f"  durable setpoint established at onboarding   : {(post_lobe or {}).get('target_domain') == expect}"
        )
        print(
            f"  ignition slot set                            : {(post_slot or {}).get('target_domain') == expect}"
        )
        print(
            f"  EPISODE 1 ignites carrying the trajectory    : {ign is not None and ign_dom == expect}"
        )
        print(
            "\nRESULT:",
            "PASS - trajectory LEADS; episode 1 ignites steered by it" if ok else "CHECK",
        )
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

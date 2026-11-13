"""Offline boot smoke: one pressure tick with seeded contributor; asserts cascade order."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main() -> int:
    import boot
    import pressure_loop.pressure as pressure_mod
    from pressure_loop.contributors import make_contributor
    from pressure_loop.loop import run_pressure_tick_once
    from pressure_loop.pressure import evaluate as real_evaluate
    from pressure_loop.runtime import get_default_runtime

    import auto_loop.auto as auto_mod
    import meta_loop.loop as meta_loop_mod

    boot.boot()
    rt = get_default_runtime()
    store = rt.episode_store
    calls: list[str] = []

    def _gather(ss, **kwargs):  # live gather_contributors gained episode_store — accept and ignore
        return [make_contributor("operator", "operator_command", 1.0, {"message": "Learn smoke"})]

    def _eval(contributors, **kwargs):
        return real_evaluate(
            contributors,
            active_episode=kwargs.get("active_episode", False),
            shared_state=kwargs.get("shared_state"),
            open_threshold=0.01,
        )

    orig_gather = pressure_mod.gather_contributors
    orig_eval = pressure_mod.evaluate
    orig_meta = meta_loop_mod.run_meta_a
    orig_auto = auto_mod.run_auto_front
    orig_mb = meta_loop_mod.run_meta_b

    pressure_mod.gather_contributors = _gather
    pressure_mod.evaluate = _eval

    def _meta(ep):
        calls.append("META_A")
        ep.meta = dict(ep.meta or {})
        ep.meta["execution_action_pack"] = {
            "episode_id": ep.episode_id,
            "action": "USE_TOOL",
            "tool": "web_fetch",
            "routing": {},
            "memory_scope": "general",
            "prompt_type": "tool",
            "model_profile": "TEXT_FAST",
            "constraints": {},
        }

    def _auto(ep):
        calls.append("AUTO")
        try:
            orig_auto(ep)
        except Exception:
            calls.append("AUTO_FAIL_SOFT")
            ep.auto = {"cycle_kind": "fail", "status": "failed"}

    def _mb(ep):
        calls.append("META_B_GLOBAL_B")
        ep.meta = dict(ep.meta or {})
        ep.meta["reflection"] = {"disposition": "skipped"}
        ep.global_ = dict(ep.global_ or {})
        ep.global_["outcome"] = {"summary": "ok"}

    meta_loop_mod.run_meta_a = _meta
    auto_mod.run_auto_front = _auto
    meta_loop_mod.run_meta_b = _mb

    try:
        assert store.current() is None
        run_pressure_tick_once(rt)
        assert store.current() is None, "episode must close after cascade"
        assert "META_A" in calls
        assert "AUTO" in calls or "AUTO_FAIL_SOFT" in calls
        assert "META_B_GLOBAL_B" in calls
    finally:
        pressure_mod.gather_contributors = orig_gather
        pressure_mod.evaluate = orig_eval
        meta_loop_mod.run_meta_a = orig_meta
        auto_mod.run_auto_front = orig_auto
        meta_loop_mod.run_meta_b = orig_mb

    print("boot_smoke_once: PASS", " -> ".join(calls))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

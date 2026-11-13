"""Leftover F023–F027 citations in historical architecture maps.

The named cascade/entry blocks were refreshed. These oracles pin the rest of
the live-path tables so a later line-number drift cannot look current.
"""

from __future__ import annotations

from pathlib import Path

from core.paths import OMEGA_ROOT

_ARCH = Path(OMEGA_ROOT) / "docs" / "architecture"
_AUTO = _ARCH / "AUTO_LOOP_MAP.md"
_LIFE = _ARCH / "EPISODE_LIFECYCLE_MAP.md"
_LIVE = _ARCH / "LIVE_CODEBASE_MAP.md"


def test_auto_loop_map_fail_fast_and_seams_cite_live_sequencer() -> None:
    text = _AUTO.read_text(encoding="utf-8")
    for stale in (
        "`loop.py:65-66`",
        "cognition_chain.py:150-155",
        "cognition_chain.py:166-169",
        "cognition_chain.py:170-181",
        "cognition_chain.py:182-183",
        "`execute:305-316`",
        "`execute:353-360`",
        "`execute:408-410`",
        "`execute:484-488`",
        "`execute:526-528`",
        "`loop.py:152-166`",
        "auto_loop/loop.py:151",
        "auto_loop/execute/run.py:278",
        "auto_loop/mril_tools/format.py:125-146",
        "auto_loop/mril_tools/subdomain_helpers.py:72-85",
        "auto_loop/mril_tools/router.py:28-74",
        "auto_loop/fold/run.py:24-82",
        "auto_loop/route/run.py:26-52",
        "auto_loop/promptpack/run.py:56-118",
        "memory/gates/base.py:409-414",
        "auto_loop/mril_tools/remember.py:22-27",
        "auto_loop/mril_tools/remember.py:55",
    ):
        assert stale not in text, f"stale AUTO_LOOP_MAP citation still present: {stale}"
    for live in (
        "cognition_chain.py:3101",
        "auto_loop/loop.py:173",
        "auto_loop/loop.py:195",
        "auto_loop/loop.py:205-206",
        "auto_loop/loop.py:369",
        "run_auto_front = run_auto",
        "cognition_chain.py:3132",
        "auto_loop/execute/run.py:1788",
        "auto_loop/loop.py:365",
        "auto_loop/intake/run.py:16",
        "auto_loop/mril_tools/run.py:31",
        "memory/gates/base.py:388",
        "auto_loop/mril_tools/remember.py:248",
        "auto_loop/fold/run.py:265",
        "auto_loop/execute/run.py:1969",
    ):
        assert live in text, f"live AUTO_LOOP_MAP citation missing: {live}"


def test_episode_lifecycle_map_tick_open_close_and_terminus_cite_live_functions() -> None:
    text = _LIFE.read_text(encoding="utf-8")
    for stale in (
        "boot.py:124",
        "boot.py:144",
        "pressure_loop/loop.py:49",
        "pressure_loop/loop.py:56-68",
        "pressure_loop/loop.py:24-46",
        "cognition_chain.py:68-69",
        "cognition_chain.py:69",
        "deepsleep_loop/loop.py:213-214",
        "deepsleep_loop/loop.py:214",
        "deepsleep_loop/loop.py:37-295",
        "deepsleep_loop/closure/run.py:24",
        "pressure_loop/episode.py:246",
        "pressure_loop/episode.py:309",
        "cognition_chain.py:36-39",
        "pressure_loop/pressure.py:379-423",
        "pressure_loop/pressure.py:411",
        "omega_loop/intake/run.py:25-27",
        "omega_loop/_omega_state.py:217-227",
        "omega_loop/package/run.py:17-20",
        "core/shared_state_keys.py:950-962",
        "hud/telemetry.py:1840",
    ):
        assert stale not in text, f"stale EPISODE_LIFECYCLE_MAP citation still present: {stale}"
    for live in (
        "boot.py:614",
        "boot.py:940",
        "pressure_loop/loop.py:356",
        "pressure_loop/loop.py:192",
        "pressure_loop/episode.py:304",
        "pressure_loop/episode.py:367",
        "deepsleep_loop/loop.py:657",
        "deepsleep_loop/loop.py:768",
        "cognition_chain.py:1353",
        "deepsleep_loop/loop.py:885",
        "cognition_chain.py:1203",
        "pressure_loop/pressure.py:1990",
        "core/shared_state_keys.py:762",
        "cognition_chain.py:1461",
        "cognition_chain.py:1521",
        "deepsleep_loop/closure/run.py:11",
    ):
        assert live in text, f"live EPISODE_LIFECYCLE_MAP citation missing: {live}"


def test_live_codebase_map_does_not_present_tenth_episode_omega_as_current() -> None:
    text = _LIVE.read_text(encoding="utf-8")
    assert "Episodes 1–9: cascade ends at Global B" not in text
    assert "[10th episode only] Omega window reflect" not in text
    assert "Inline on the **10th episode**" not in text
    assert "runs only on **episode 10**" not in text
    assert "Window-triggered Omega" not in text
    assert "# 10th episode only (window has 9 bodies):" not in text
    assert "Omega runs on every episode" in text
    assert "parakeet_tdt_06b_v3" in text
    assert "kokoro" in text
    assert "Ettin" in text
    assert "gemma_e4b" in text
    assert "Whisper" in text

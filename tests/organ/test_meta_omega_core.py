"""The shared Omega control parse."""

from types import SimpleNamespace

import pytest

from organ.promptorgan.packs.meta_omega.core import parse_lane


def _control(decision, synthesis):
    assessment = dict.fromkeys(("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"), "fact")
    assessment.update(gap="" if decision == "DONE" else "unfinished", evidence=[])
    pressure = None if decision == "DONE" else dict(
        intent="USE_TOOL", confidence=0.9, reason="rerun the failing check", setpoints={},
        desired_state="green", observed_state="red", resolution_conditions=["checks pass"])
    return {"control": dict(assessment=assessment, decision=decision, pressure=pressure, synthesis=synthesis)}


def test_an_empty_synthesis_on_a_continuing_decision_becomes_its_reason():
    """Live 2026-09-15: the model returned synthesis "" twice on PRESSURE and the turn failed."""
    packet = SimpleNamespace(episode_id="ep", omega_lane="chat", browser_run=None)

    result = parse_lane(_control("PRESSURE", ""), packet)

    assert result.control_decision["synthesis"] == "rerun the failing check"


def test_an_empty_synthesis_on_done_is_still_a_contract_failure():
    packet = SimpleNamespace(episode_id="ep", omega_lane="chat", browser_run=None)
    with pytest.raises(ValueError, match="synthesis"):
        parse_lane(_control("DONE", ""), packet)


def test_browser_evidence_is_what_the_host_can_verify():
    """Live 2026-09-15: Omega, seeing only an address and title, continued a step forever waiting
    for a logo. The policy names the page evidence it judges by and the runs it may not continue."""
    from organ.promptorgan.packs.meta_omega.browser import BROWSER_POLICY

    assert "final_page (address, title, readable text" in BROWSER_POLICY
    assert "never PRESSURE intent=BROWSE" in BROWSER_POLICY
    assert "the objective is proven by the page" in BROWSER_POLICY

"""The canonical browser chain (plan Task 14, step 1, as redesigned 2026-09-15).

``open Hugging Face and browse for models under 9B``: Global A binds the workflow; each
episode is one Meta A look and one bounded agent run in Omega's own window (the model
choosing its tools, a screenshot after every action); Omega alone judges the run's receipt
and continues it through a successor whose instruction is Omega's reason; the operator's
bubble resolves exactly once, when Omega says DONE.
"""

from __future__ import annotations

import pytest

from tests.helpers.blueprint_driver import BlueprintDriver, browser_action, browser_decision

pytestmark = pytest.mark.usefixtures("deepsleep_sram_drain")

OBJECTIVE = "open Hugging Face and browse for models under 9B"
CATALOG = "https://huggingface.co/models"
FILTERED = f"{CATALOG}?size=lt9b"


@pytest.fixture
def driver(monkeypatch, tmp_path):
    driver = BlueprintDriver(monkeypatch=monkeypatch, tmp_path=tmp_path)
    driver.browser.pages.update({
        CATALOG: "Models - Hugging Face",
        "click:100,50": FILTERED,
        FILTERED: "Models under 9B - Hugging Face",
    })
    return driver


def test_browser_pressure_chain_keeps_origin_bubble_until_omega_done(driver):
    driver.browser_actions.extend([
        # run 1: the agent opens the catalog and claims it is done too early
        browser_action("navigate", url=CATALOG),
        browser_action("done", summary="The catalog is open."),
        # run 2 (Omega's successor): filter, look, done
        browser_action("click", x=100, y=50, target="size filter"),
        browser_action("scroll", direction="down", amount=800),
        browser_action("done", summary="The catalog shows only models under 9B.", evidence="title says under 9B"),
    ])
    driver.omega.extend([
        browser_decision("PRESSURE", reason="The catalog is open but unfiltered: apply the size filter for models under 9B."),
        browser_decision("DONE", synthesis="Found the qualifying models: the catalog is filtered to models under 9B."),
    ])

    # Episode 1: BROWSE/browser, the workflow bound, no window yet → the run opens Omega's window.
    first = driver.view(driver.operator_turn(OBJECTIVE, route="BROWSER_AGENT", cycle_id="bubble-1"))
    assert (first.intent, first.lane) == ("BROWSE", "browser")
    assert first.workflow["objective"] == OBJECTIVE and first.failure_kind is None
    assert first.run["status"] == "done" and [row["action"] for row in first.run["actions"]] == ["navigate", "done"]
    assert first.run["final_page"]["url"] == CATALOG
    assert driver.browser.actions == [("navigate", CATALOG)]
    assert first.control["decision"] == "PRESSURE" and first.control["pressure"]["intent"] == "BROWSE"
    first_reply = driver.reply("bubble-1")
    assert "Browser lane" in first_reply["text"]
    assert first_reply["lane_work"]["status"] == "progress"

    # Episode 2: a successor with a new id, the same workflow, Omega's reason as the instruction.
    second = driver.view(driver.run_successor())
    assert second.episode_id != first.episode_id
    assert (second.intent, second.lane) == ("BROWSE", "browser")
    assert second.workflow["workflow_id"] == first.workflow["workflow_id"]
    assert second.outputs["browser_run"]["steps"] == 3
    assert driver.browser.actions[1:] == [("click", "100,50"), ("scroll", "0,800")]
    assert second.control["decision"] == "DONE"
    brief = [g for g in driver.generations if g["operation"] == "auto_browser_agent"][2]["messages"][1]["content"]
    assert "CURRENT INSTRUCTION (operator, data): The catalog is open but unfiltered" in brief
    assert "PROGRESS SO FAR (host record): run 1: done after 2 actions" in brief

    # Two runs, one per episode; every action was followed by a screenshot the loop looked at.
    assert sum(g["operation"] == "auto_browser_agent" for g in driver.generations) == 5
    assert driver.browser.observations >= 4
    assert driver.staged_successors() == []
    reply = driver.reply("bubble-1")
    assert "Browser lane" in reply["text"] and reply["lane_work"]["status"] == "completed"
    from pressure_loop.operator_chat_responses import get_lane_updates

    updates = get_lane_updates("bubble-1")["updates"]
    assert updates[-1]["text"] == "Found the qualifying models: the catalog is filtered to models under 9B."
    assert updates[-1]["episode_id"] == second.episode_id
    # The browser session is reusable and the workflow waits for the operator's next word.
    assert driver.browser.session_id == "edge-session" and driver.browser.attach_count == 1
    assert driver.workflow()["status"] == "waiting_for_operator" and driver.workflow()["run_count"] == 2


def test_a_browser_run_never_looks_at_the_operator_tab(driver):
    driver.browser_actions.extend([browser_action("navigate", url=CATALOG), browser_action("done", summary="open")])
    driver.omega.append(browser_decision("DONE", synthesis="The catalog is open."))

    first = driver.view(driver.operator_turn("open the Hugging Face catalog", route="BROWSER_AGENT", cycle_id="bubble-2"))

    opening = next(g for g in driver.generations if g["operation"] == "auto_browser_agent")
    assert opening["operation"] == "auto_browser_agent"
    assert "No Omega window is open yet" in opening["messages"][1]["content"]
    assert all(not isinstance(m.get("content"), list) for m in opening["messages"]), \
        "no screenshot of the operator's tab reaches the model"
    assert driver.browser.locked_window == "omega-window"
    reply = driver.reply("bubble-2")
    assert first.run["status"] == "done" and "Browser lane" in reply["text"]
    assert reply["lane_work"]["status"] == "completed"
    from pressure_loop.operator_chat_responses import get_lane_updates

    assert get_lane_updates("bubble-2")["updates"][-1]["text"] == "The catalog is open."


def test_the_agent_asks_and_the_question_reaches_the_operator_bubble(driver):
    driver.browser_actions.extend([
        browser_action("navigate", url=CATALOG),
        browser_action("ask_operator", question="Which account should I use to sign in?"),
    ])
    driver.omega.append(browser_decision("ASK_OPERATOR", synthesis="Which account should I use to sign in?"))

    first = driver.view(driver.operator_turn("sign in to Hugging Face", route="BROWSER_AGENT", cycle_id="bubble-3"))

    assert first.run["status"] == "ask_operator"
    reply = driver.reply("bubble-3")
    assert "Browser lane" in reply["text"] and reply["lane_work"]["status"] == "needs_input"
    from pressure_loop.operator_chat_responses import get_lane_updates

    update = get_lane_updates("bubble-3")["updates"][-1]
    assert update["text"] == "The Browser lane needs your input: Which account should I use to sign in?"
    assert update["question"]["question"] == "Which account should I use to sign in?"
    assert driver.staged_successors() == []
    assert driver.workflow()["status"] == "waiting_for_operator"

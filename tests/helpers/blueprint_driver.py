"""Drive real episodes through the canonical cascade with model services stubbed.

Global A → Meta A → AUTO → Meta B → Global B → Omega → DeepSleep run for real on the
MotorProgramHarness's isolated substrate. The seams replaced are the ones the plan
allows: the model seat (routing stub, browser agent decisions, agent batch, Omega fold),
the projection encoder, and the WebDriver session. Omega's decisions are scripted per
episode as strict control envelopes, so the chain is verified as behaviour: one bounded
agent run per browser episode, one batch per agent episode, successors re-entering Global
A, the operator answered once. No LLM or embedding weights are loaded by this helper.
"""

from __future__ import annotations

import base64
import json
from collections import deque
from io import BytesIO
from types import SimpleNamespace
from typing import Any, Callable

from pressure_loop.contributors import EpisodeIgnition, make_contributor
from tests.helpers.motor_program_harness import MotorProgramHarness


def _png(width: int = 16, height: int = 12) -> bytes:
    from PIL import Image

    encoded = BytesIO()
    Image.new("RGB", (width, height), (30, 60, 120)).save(encoded, format="PNG")
    return encoded.getvalue()


class _ModelFreeProjectionEncoder:
    """Deterministic projection input; keeps cascade tests independent of weights."""

    snapshot = "test:model-free"

    def encode(self, texts: list[str]) -> list[list[float]]:
        rows: list[list[float]] = []
        for text in texts:
            seed = sum((index + 1) * ord(char) for index, char in enumerate(str(text)))
            rows.append([float(((seed + index * 17) % 255) - 127) for index in range(256)])
        return rows


class FakeBrowser:
    """Omega's Edge session: attaches on first look, owns one task window, actuates on demand.

    ``pages`` maps an address to its title and ``click:(x,y)`` (CSS pixels) to the address a
    click there opens. The viewport is 1000x600 CSS pixels behind a 16x12 screenshot.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.locked_window: str | None = None
        self.url = "http://127.0.0.1:13310/"
        self.title = "OMEGA · Portable Cognitive Substrate"
        self.pages: dict[str, str] = {}
        self.actions: list[tuple[str, str]] = []
        self.attach_count = 0
        self.observations = 0
        self._pixels = _png(1000, 600)  # the screenshot covers the 1000x600 viewport 1:1

    def attach(self, debugger_address, *, auto_setup=False):
        self.attach_count += 1
        self.session_id = "edge-session"
        self.locked_window = None
        return {"session_id": self.session_id, "debugger_address": debugger_address}

    def detach(self) -> None:
        self.session_id = None
        self.locked_window = None

    def release(self) -> None:
        self.detach()

    def ensure_task_tab_alive(self) -> str:
        return "live" if self.session_id else "session_lost"

    def _observed(self) -> dict[str, str]:
        return {"session_id": self.session_id or "", "locked_window": self.locked_window or "",
                "url": self.url, "title": self.title, "page_text": f"{self.title} page text",
                "screenshot": base64.b64encode(self._pixels).decode("ascii")}

    read_snapshot = _observed
    snapshot = _observed

    # the agent loop's view
    def show_task_banner(self) -> None:
        pass

    def wait_ready(self, timeout_s=3.0, *, settle_s=0.4) -> str:
        return "complete"

    def page_state(self) -> dict[str, Any]:
        return {"url": self.url, "title": self.title, "ready": "complete",
                "viewport": {"width": 1000, "height": 600, "dpr": 1.0},
                "scroll": {"x": 0, "y": 0, "height": 600}, "text": f"{self.title} page text",
                "elements": [{"tag": "a", "text": "Filter", "x": 100, "y": 50}], "active": None}

    def observe(self) -> dict[str, Any]:
        assert self.locked_window, "the operator's tab is never observed"
        self.observations += 1
        return {**self.page_state(), **self._observed()}

    def navigate_in_new_tab(self, url: str):
        self.locked_window = self.locked_window or "omega-window"
        return self.navigate(url)

    def navigate(self, url: str):
        self.url, self.title = url, self.pages.get(url, "Page")
        self.actions.append(("navigate", url))
        return {"url": self.url, "title": self.title}

    def click_xy(self, x: int, y: int):
        self.actions.append(("click", f"{x},{y}"))
        target = self.pages.get(f"click:{x},{y}")
        if target:
            self.url, self.title = target, self.pages.get(target, "Page")
        return {"x": x, "y": y}

    def scroll(self, delta_x: int, delta_y: int, *, x: int = 0, y: int = 0):
        self.actions.append(("scroll", f"{delta_x},{delta_y}"))
        return {"delta_x": delta_x, "delta_y": delta_y}


def assessment(gap: str) -> dict[str, Any]:
    base = dict.fromkeys(("who", "what", "when", "where", "why", "how", "desired_state", "observed_state"),
                         "blueprint evidence")
    return {**base, "gap": gap, "evidence": ["scripted receipt"]}


def _chat_done_fold(_driver: "BlueprintDriver") -> dict[str, Any]:
    """Deterministic Omega fold for the Chat acknowledgement episode in a routed turn."""
    return {"assessment": assessment(""), "decision": "DONE",
            "synthesis": "The specialist request was durably routed.", "pressure": None}


def browser_decision(decision: str, *, reason: str = "continue in the browser", synthesis: str = "run reviewed"):
    """Omega's browser control; on PRESSURE the reason is the next run's instruction."""

    def build(_driver: "BlueprintDriver") -> dict[str, Any]:
        pressure = None
        if decision != "DONE":
            pressure = dict(intent="BROWSE" if decision == "PRESSURE" else "ASK_OPERATOR", confidence=0.9,
                            reason=reason, setpoints={}, desired_state="the objective is shown on the page",
                            observed_state="run receipt reviewed", resolution_conditions=["the page proves it"])
        return {"control": {"assessment": assessment("" if decision == "DONE" else "work remains"),
                            "decision": decision, "pressure": pressure, "synthesis": synthesis}}

    return build


def agent_decision(decision: str, *, reason: str = "continue the task", synthesis: str = "task reviewed"):
    def build(_driver: "BlueprintDriver") -> dict[str, Any]:
        pressure = None
        if decision != "DONE":
            pressure = dict(intent="USE_TOOL", confidence=0.9, reason=reason, setpoints={},
                            desired_state="the operator's request is satisfied",
                            observed_state="tool results reviewed", resolution_conditions=["verification passes"])
        return {"control": {"assessment": assessment("" if decision == "DONE" else "work remains"),
                            "decision": decision, "pressure": pressure, "synthesis": synthesis}}

    return build


def tool_call(name: str, args: dict[str, Any], call_id: str = "call") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def browser_action(name: str, **args: Any) -> dict[str, Any]:
    """One scripted decision of the browser agent: the tool it calls and its arguments."""
    return {"name": name, "args": args}


class BlueprintDriver:
    def __init__(self, *, monkeypatch, tmp_path) -> None:
        self.monkeypatch = monkeypatch
        self.harness = MotorProgramHarness(monkeypatch=monkeypatch, root=tmp_path / "omega")
        self.store = self.harness.store
        self.browser = FakeBrowser()
        self.plan_steps: list[dict[str, str]] = []
        self.browser_actions: deque[dict[str, Any]] = deque()
        self.agent_batches: deque[dict[str, Any]] = deque()
        self.research_batches: deque[dict[str, Any]] = deque()
        self.omega: deque[Callable[["BlueprintDriver"], dict[str, Any]]] = deque()
        self.generations: list[dict[str, Any]] = []
        self.folds: list[dict[str, Any]] = []
        self.episodes: list[Any] = []
        self._counter = 0
        self._omega_queue = None
        import pressure_loop.operator_chat_responses as replies
        from auto_loop.execute import brain_skill_executor as executor, chat_permissions
        from meta_loop.browser import perception
        from pressure_loop import pressure
        from memory import retina

        monkeypatch.setenv("OMEGA_BROWSER_WORKFLOW_PATH", str(tmp_path / "browser_workflow.json"))
        monkeypatch.setattr(perception, "_controller", lambda: self.browser)
        monkeypatch.setattr(executor, "_browser_controller", lambda: self.browser)
        monkeypatch.setattr(retina, "encoder", lambda: _ModelFreeProjectionEncoder())
        monkeypatch.setattr(replies, "_store_path", lambda: tmp_path / "chat_responses.json")
        replies._reset_cache_for_tests()
        monkeypatch.setattr(chat_permissions, "CHAT_PERMISSION_PATH", tmp_path / "permission-mode.json")
        chat_permissions.set_chat_permission_mode("always-allow")
        monkeypatch.setattr("pressure_loop.pressure_field.build_registry", lambda: {})
        monkeypatch.setattr("memory.taxonomy.config.taxonomy_pressure_enabled", lambda: False)
        monkeypatch.setattr(pressure, "_rollback_event_contributor", lambda: None)
        monkeypatch.setattr("runtime.socket.invoke_runtime_generate", self.generate)

    # ---------------------------------------------------------------- the model seat
    def generate(self, **kwargs):
        operation = str(kwargs.get("operation") or "")
        self.generations.append(kwargs)
        if operation == "auto_browser_agent":
            assert self.browser_actions, "the browser agent asked for a decision the scenario did not script"
            action = self.browser_actions.popleft()
            return {"ok": True, "text": "", "tool_calls": [
                {"id": f"act-{len(self.generations)}", "function": {"name": action["name"],
                                                                     "arguments": json.dumps(action["args"])}}]}
        if operation == "auto_response":
            assert self.agent_batches, "AUTO asked for an agent batch the scenario did not script"
            return {"ok": True, **self.agent_batches.popleft()}
        if operation == "web_research":
            assert self.research_batches, "AUTO asked for a research step the scenario did not script"
            return {"ok": True, **self.research_batches.popleft()}
        if operation == "episode_cognition":
            queue = self._omega_queue if self._omega_queue is not None else self.omega
            assert queue, "Omega folded an episode the scenario did not script"
            payload = queue.popleft()(self)
            self.folds.append({
                "system": kwargs.get("system"),
                "prompt": kwargs.get("prompt"),
                "payload": payload,
                "pipeline_id": kwargs.get("pipeline_id"),
            })
            return {"text": json.dumps(payload)}
        raise AssertionError(f"unexpected model operation: {operation}")

    # ---------------------------------------------------------------- driving episodes
    def _ignite(self, row, *, opened_by: str, omega_queue=None):
        import cognition_chain
        from core.pipelines import pipeline_for_lane
        from core.pipeline_state import mark_ready
        from pressure_loop.cognition_queue import (
            acknowledge_input, lane_for_contributor, work_kind_for_contributor,
        )

        lane = lane_for_contributor(row)
        pipeline_id = pipeline_for_lane(lane)
        self._counter += 1
        ignition = EpisodeIgnition(
            pipeline_id=pipeline_id, work_kind=work_kind_for_contributor(row),
            episode_id=f"blueprint-{self._counter}", aggregate_pressure=1.0, opened_by=opened_by,
            dominant_contributor=row, contributors=(row,), trajectory_snapshot=None)
        mark_ready(pipeline_id, ignition)
        if not acknowledge_input(row, pipeline_id):
            raise AssertionError(f"lane {lane} did not own its admitted queue item")
        previous_queue = self._omega_queue
        self._omega_queue = omega_queue
        try:
            episode = cognition_chain.ignite_episode(self.store, ignition)
            assert episode is not None
            self.episodes.append(episode)
            return episode
        finally:
            self._omega_queue = previous_queue

    def begin_operator_turn(
        self, text: str, *, route: str, cycle_id: str,
        previous: tuple[str, str] | None = None,
        thread_id: str | None = None,
    ):
        """Run Chat through its durable route, leaving specialist work queued on its lane."""
        from pressure_loop.cognition_queue import enqueue_input, lease_next_input
        from tests.helpers.intent_router import stub_router

        stub_router(self.monkeypatch, route)
        if previous is not None:
            from global_loop.intent.context import remember_action

            remember_action(*previous)
        contributor = make_contributor("operator", "operator_query", 1.0, {
            "cycle_id": cycle_id, "message": text,
            "chat_bubble_waiting": True, "directive_intent": "RESPOND"})
        if thread_id:
            contributor.metadata["thread_id"] = thread_id
        assert enqueue_input(contributor, operator_activity=False)
        leased = lease_next_input(0)
        assert leased is not None
        if route in {"BROWSER_AGENT", "CODING_AGENT"}:
            return self.run_operator_chat(leased, opened_by=leased.type)
        return self._ignite(leased, opened_by=leased.type)

    def operator_turn(
        self, text: str, *, route: str, cycle_id: str,
        previous: tuple[str, str] | None = None,
    ):
        """Drive one full routed operator turn through Chat and its dedicated target lane.

        The Chat episode durably admits the handoff and resolves the immediate response first.
        Browser or Coding Agent work then opens separately through its own pressure FIFO.
        ``previous`` is the assistant action Global A remembers from the last delivered turn.
        """
        chat_episode = self.begin_operator_turn(
            text, route=route, cycle_id=cycle_id, previous=previous
        )
        if route in {"BROWSER_AGENT", "CODING_AGENT"}:
            return self.run_lane_work("browser" if route == "BROWSER_AGENT" else "agent")
        return chat_episode

    def run_operator_chat(self, row, *, opened_by: str):
        """Run Chat's part of a routed turn before any target lane starts."""
        return self._ignite(row, opened_by=opened_by,
                            omega_queue=deque([_chat_done_fold]))

    def run_lane_work(self, lane: str):
        """Lease one specialist handoff on its permanently bound semantic lane."""
        from core.pipelines import pipeline_for_lane
        from pressure_loop.cognition_queue import lease_next_input

        pipeline_id = pipeline_for_lane(lane)
        row = lease_next_input(pipeline_id)
        assert row is not None, f"the {lane} lane has no admitted handoff"
        assert row.metadata.get("lane") == lane, "the target FIFO supplied another lane's row"
        return self._ignite(row, opened_by=row.type)

    def staged_successors(self):
        from pressure_loop.pressure import gather_contributors

        return [c for c in gather_contributors(None, episode_store=self.store) if c.type == "omega_successor"]

    def run_successor(self):
        """The pressure tick's job: admit the one staged successor and ignite it."""
        from pressure_loop.cognition_queue import enqueue_input, lease_next_input
        from pressure_loop.policy import pressure_open_threshold
        from pressure_loop.pressure import evaluate

        rows = self.staged_successors()
        assert len(rows) == 1, f"expected exactly one staged successor, found {len(rows)}"
        row = rows[0]
        assert evaluate([row], active_episode=False, neuromod={}, open_threshold=pressure_open_threshold()) is not None
        assert enqueue_input(row, operator_activity=False)
        from pressure_loop.cognition_queue import lane_for_contributor
        from core.pipelines import pipeline_for_lane

        leased = lease_next_input(pipeline_for_lane(lane_for_contributor(row)))
        assert leased is not None and leased.type == "omega_successor"
        return self._ignite(leased, opened_by=leased.type)

    # ---------------------------------------------------------------- reading results
    def reply(self, cycle_id: str):
        import pressure_loop.operator_chat_responses as replies

        return replies.get_response(cycle_id)

    @staticmethod
    def workflow():
        from meta_loop.browser.workflow import browser_workflow_receipt

        return browser_workflow_receipt()

    @staticmethod
    def view(episode) -> SimpleNamespace:
        global_ = dict(getattr(episode, "global_", None) or {})
        auto = dict(getattr(episode, "auto", None) or {})
        return SimpleNamespace(
            episode_id=episode.episode_id,
            intent=dict(global_.get("intent_decision") or {}).get("intent"),
            lane=dict(global_.get("intent_decision") or {}).get("omega_lane"),
            workflow=dict(global_.get("browser_workflow") or {}),
            run=dict(dict(auto.get("outputs") or {}).get("browser_run") or {}),
            outputs=dict(auto.get("outputs") or {}),
            failure_kind=auto.get("failure_kind"),
            control=dict(dict(global_.get("omega_result") or {}).get("control_decision") or {}),
        )

def test_execution_action_pack_feedback_preserves_operator_route(monkeypatch):
    from meta_loop._meta_a_state import MetaAState
    from meta_loop.intake.run import MetaAInput
    from meta_loop.pack.run import run

    events: list[dict[str, object]] = []
    monkeypatch.setattr(
        "guardian.terminal_feedback.emit_data",
        lambda _system, _phase, **fields: events.append(fields),
    )
    state = MetaAState(
        mi=MetaAInput(
            episode_id="ep-browser-route",
            pressure={"contributors": [], "aggregate_pressure": 0.0},
            trajectory_snapshot={},
            action_goal={"episode_id": "ep-browser-route", "intent": "RESPOND"},
        ),
        episode_id="ep-browser-route",
        intent="RESPOND",
        action="RESPONSE",
        source={"episode_id": "ep-browser-route", "intent": "RESPOND"},
        text="open the browser",
        target_str="open the browser",
        constraints={
            "planning_only": True,
            "no_inference_in_meta": True,
            "no_execution_in_meta": True,
            "no_model_fields": True,
        },
        meta={},
        execution_intent="plan for global intent RESPOND",
        execution_inputs={"operator_route": "browser"},
    )

    pack = run(state)

    assert pack.execution_inputs["operator_route"] == "browser"
    assert events and events[-1]["operator_route"] == "browser"

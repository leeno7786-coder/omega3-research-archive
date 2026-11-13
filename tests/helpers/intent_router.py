"""Test stand-in for Global A's promoted intent router.

Cascade tests drive real operator turns through Global A, which routes them with the
pinned ModernBERT bundle. Tests must not depend on a promoted model, so they seat this
stub instead: every operator turn is routed, with full confidence, to one route.
"""

from __future__ import annotations

from types import SimpleNamespace

from global_loop.intent import router as intent_router


def accepted_decision(route: str) -> intent_router.RouteDecision:
    return intent_router.RouteDecision(
        route=route, reason="accepted", confidence=0.99, margin=0.98,
        primary={name: 0.99 if name == route else 0.0033 for name in intent_router.ROUTES},
        intents=(route,),
        intent_scores={name: float(name == route) for name in intent_router.ROUTES},
        certainty={"known": 1.0, "ambiguous": 0.0, "out_of_domain": 0.0},
    )


def stub_router(monkeypatch, route: str) -> list[tuple[str, str]]:
    """Seat a router that sends every operator turn to ``route``; returns the (text, context) calls.

    The previous assistant action is process state recorded at chat delivery, so the stub
    also starts the test with none and restores it afterwards: a routed reply delivered by
    an earlier test must not become this test's routing context.
    """
    from global_loop.intent import context as route_context

    monkeypatch.setattr(route_context, "_last", None)
    decision = accepted_decision(route)
    calls: list[tuple[str, str]] = []

    def route_turn(text: str, context: str = "") -> intent_router.RouteDecision:
        calls.append((text, context))
        return decision

    monkeypatch.setattr(intent_router, "get_router", lambda **_: SimpleNamespace(route=route_turn))
    return calls

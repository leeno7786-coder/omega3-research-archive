"""The retired product embedding route must not reappear in runtime wiring."""

from __future__ import annotations

from pressure_loop.policy import runtime_service_names
from runtime.runtime_capability_broker import RuntimeCapability


def test_runtime_embedding_route_is_absent():
    assert "EMBED" not in RuntimeCapability.__members__
    assert "embed" not in runtime_service_names()

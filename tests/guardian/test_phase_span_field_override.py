"""phase_span regression (found 2026-07-04 by the curiosity-cap tests).

phase_span's exit emitted ``**fields, **span._extra`` as a double-splat, so a
span that ``set()`` a key it was CREATED with raised TypeError at the call
site — every curiosity update_status (mark_active / mark_resolved /
mark_deferred) crashed at span exit. set() must OVERRIDE the init field.
"""

from __future__ import annotations

from guardian.terminal_feedback import phase_span


def test_set_overriding_init_field_does_not_raise():
    with phase_span("test", "override", new_status="init") as span:
        span.set(new_status="final", other="x")


def test_skip_path_with_override_does_not_raise():
    with phase_span("test", "override_skip", key="a") as span:
        span.set(key="b")
        span.skip("testing skip path")

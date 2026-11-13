"""
Omega 3.0 HUD — bridge between the live cognitive substrate and the
operator's visual surface (hud/omega_interface.html).

This package owns:
    - server.py     — FastAPI app + route definitions
    - telemetry.py  — adapter functions that translate Omega subsystem
                      state into JSON the HUD JavaScript can render
    - events.py     — SSE event stream feeder (HUD-10)

Wiring philosophy: read-only by default. The HUD is a window into the
cognitive substrate, not an executor. Interactive surfaces (Guardian
approval Approve/Deny/Escalate buttons in HUD-6, chat composer in
HUD-13) are the explicit exceptions and route through the existing
brain_receiver / Guardian APIs — never bypassing doctrine.
"""

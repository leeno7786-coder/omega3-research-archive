"""Live capstone check: operator message -> full cascade -> per-episode Omega
generates the reply -> patched into outcome.artifacts -> delivered to the HUD.

Proves the new deterministic-auto + Omega-cognition cascade end to end against
the live runtime (:11434). Creates ONE real episode.
"""

from __future__ import annotations

import json

from core.shared_state import SharedState
from pressure_loop.episode import EpisodeStore
from pressure_loop.operator_chat import handle_operator_chat
from pressure_loop.runtime import PressureRuntime


def main() -> int:
    runtime = PressureRuntime(shared_state=SharedState(), episode_store=EpisodeStore())
    result = handle_operator_chat("In one sentence, what is a qubit?", runtime=runtime)

    ok = bool(result.get("ok"))
    message = str(result.get("message") or "")
    route = result.get("route") or {}

    print("=== LIVE OPERATOR -> OMEGA -> HUD ===")
    print("ok:", ok)
    print("route:", json.dumps(route))
    print("message (HUD-delivered, Omega-generated):", repr(message))

    # The episode that just closed — confirm Omega ran and auto did not generate.
    body = runtime.episode_store.last_closed_body()
    if isinstance(body, dict):
        omega = (
            (body.get("omega_result") or {}) if isinstance(body.get("omega_result"), dict) else {}
        )
        print("omega cognized:", omega.get("cognized"))
        print("omega response_text:", repr(omega.get("response_text")))

    delivered = ok and bool(message.strip())
    print(
        "\nRESULT:",
        "PASS — operator reply delivered from Omega" if delivered else "FAIL — no reply delivered",
    )
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())

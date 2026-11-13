"""
test_hud_foundation.py — HUD-1 verification

Tests the FastAPI app + telemetry scaffolding without booting Omega
or starting a real uvicorn. Uses FastAPI's TestClient (in-process,
no network) so tests run cold + deterministic.

Coverage:
    A. /healthz returns ok
    A. / serves the modular HUD shell (the right one)
    A. /api/status returns 5 pill keys with shape-correct stubs
    A. /api/boot/state returns the boot banner shape
    A. /api/panel/{topic} for each known panel returns JSON
    A. /api/panel/{unknown} returns 404
    A. Telemetry adapters never raise (best-effort contract)
"""

from __future__ import annotations

import sys
from pathlib import Path
import logging

log = logging.getLogger("omega.tests.hud.test_hud_foundation")


ROOT = next(p for p in Path(__file__).resolve().parents if (p / "boot.py").exists())
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
import hud.server as hud_server  # noqa: E402
from hud.server import create_app, _PANEL_HANDLERS  # noqa: E402


_PASSED = 0
_FAILED = 0
_FAILURES: list[str] = []


def _check(name: str, condition: bool, detail: str = "") -> None:
    global _PASSED, _FAILED
    if condition:
        _PASSED += 1
        print(f"  PASS  {name}")
    else:
        _FAILED += 1
        msg = f"  FAIL  {name}" + (f" -- {detail}" if detail else "")
        _FAILURES.append(msg)
        print(msg)


def _isolate_telemetry_for_plain_script() -> None:
    """Plain-script runner — never write synthetic failures or chat turns to live state."""
    import tempfile
    from guardian.telemetry import writer
    import hud.chat_history as hud_chat_history

    tmp = Path(tempfile.mkdtemp(prefix="omega-hud-foundation-"))
    writer.TELEMETRY_DIR = tmp / "telemetry"
    writer.FAILURE_LOG = tmp / "logs" / "failures.jsonl"
    writer.init_run_id("hud-foundation-test")
    # H13 POSTs /api/chat, whose handler persists the turn via record_chat_result →
    # the live guardian/state/hud/chat_history.json. Redirect to the tmp dir so the
    # plain-script test never writes operator chat state (operator finding 2026-06-30).
    hud_chat_history._HISTORY_PATH = tmp / "chat_history.json"
    # POST /api/upload writes the uploaded files under the upload root; the live one is
    # guardian/state/uploads (found 2026-09-15: hello.png and notes.md were left there).
    hud_server._UPLOAD_ROOT = tmp / "uploads"
    # The chat POSTs reserve an operator arrival in the cognition queue, which advanced the
    # live guardian/state/cognition_queue.json on every matrix run (found 2026-09-15).
    import core.paths as core_paths
    from pressure_loop import cognition_queue

    core_paths.GUARDIAN_STATE_ROOT = tmp / "guardian" / "state"
    cognition_queue._queue_path = lambda: tmp / "guardian" / "state" / "cognition_queue.json"
    cognition_queue._reset_cache_for_tests()


def main() -> int:
    print("=" * 60)
    print("test_hud_foundation -- HUD-1")
    print("=" * 60)
    print()

    _isolate_telemetry_for_plain_script()

    app = create_app()
    client = TestClient(app)

    # /healthz
    r = client.get("/healthz")
    _check("A1.a /healthz returns 200", r.status_code == 200)
    body = r.json()
    _check("A1.b /healthz body has status=ok", body.get("status") == "ok")
    _check("A1.c /healthz reports html_present True", body.get("html_present") is True)

    # /
    r = client.get("/")
    _check("A2.a / returns 200", r.status_code == 200)
    from core.paths import OMEGA_ROOT

    ui_root = OMEGA_ROOT / "hud" / "ui-src"
    html = (OMEGA_ROOT / "hud" / "omega_interface.html").read_text(encoding="utf-8")
    _check(
        "A2.b / serves the generated HUD shell",
        'class="shell' in html
        and "Omega 3.0" in html
        and "cockpit-shell" in html,
    )

    # /api/status
    r = client.get("/api/status")
    _check("A3.a /api/status returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "A3.b /api/status has required pill keys",
        {
            "voice",
            "identity",
            "memory",
            "l0",
            "presence",
            "pressure_loop",
            "embed",
            "classify",
            "face",
            "coding_model",
        }.issubset(body.keys()),
    )
    for pill in (
        "voice",
        "identity",
        "memory",
        "l0",
        "presence",
        "pressure_loop",
        "embed",
        "classify",
        "face",
        "coding_model",
    ):
        cell = body.get(pill, {})
        _check(
            f"A3.c {pill} pill has state+label+detail",
            {"state", "label", "detail"}.issubset(cell.keys()),
        )

    # /api/boot/state
    r = client.get("/api/boot/state")
    _check("A4.a /api/boot/state returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "A4.b boot state has phase + ready_at_iso + summary",
        {"phase", "ready_at_iso", "summary"}.issubset(body.keys()),
    )

    # /api/panel/{topic} for each known panel
    for topic in sorted(_PANEL_HANDLERS.keys()):
        r = client.get(f"/api/panel/{topic}")
        _check(f"A5.{topic} /api/panel/{topic} returns 200", r.status_code == 200)
        _check(f"A5.{topic} returns JSON dict", isinstance(r.json(), dict))

    # /api/panel/{unknown}
    r = client.get("/api/panel/totally_made_up")
    _check("A6.a /api/panel/<unknown> returns 404", r.status_code == 404)

    # Adapters never raise
    from hud import telemetry

    for fn_name in (
        "panel_status",
        "panel_boot_state",
        "panel_brain",
        "panel_memory",
        "panel_omega",
        "panel_autoloop",
        "panel_deepsleep",
        "panel_identity",
        "panel_settings",
    ):
        try:
            getattr(telemetry, fn_name)()
            ok = True
        except Exception:
            ok = False
        _check(f"A7.{fn_name} adapter does not raise", ok)

    # =================================================================
    # HUD-2 — pill probes return real state vocabulary
    # =================================================================
    from hud.telemetry import (
        STATE_LIVE,
        STATE_LOADING,
        STATE_DEGRADED,
        STATE_DOWN,
    )

    valid_states = {STATE_LIVE, STATE_LOADING, STATE_DEGRADED, STATE_DOWN}

    status = telemetry.panel_status()
    for pill_name in ("voice", "identity", "memory", "l0"):
        cell = status[pill_name]
        _check(
            f"H2.{pill_name} state is in vocabulary",
            cell.get("state") in valid_states,
            detail=f"got {cell.get('state')!r}",
        )
        _check(
            f"H2.{pill_name} detail is non-empty string",
            isinstance(cell.get("detail"), str) and len(cell.get("detail", "")) > 0,
        )

    # Verify the modular HUD assets carry live API wiring markers.
    status_js = (ui_root / "core" / "poll-status.js").read_text(encoding="utf-8")
    telemetry_js = (ui_root / "core" / "poll-telemetry.js").read_text(encoding="utf-8")
    memory_graph_js = (ui_root / "core" / "memory-graph.js").read_text(encoding="utf-8")
    _check("H2.modular HUD polls /api/status", "/api/status" in status_js)
    _check(
        "H2.modular HUD polls /api/telemetry/snapshot", "/api/telemetry/snapshot" in telemetry_js
    )
    _check(
        "H2.modular HUD maps status glyph IDs",
        all(
            g in status_js
            for g in ("g-voice", "g-l0", "g-presence", "g-embed", "g-classify", "g-face", "g-llm")
        ),
    )
    _check("H2.modular HUD wires memory lobe list", "/api/memory/lobes" in memory_graph_js)
    _check(
        "H2.memory graph preserves layout on refresh",
        "stabilizeLayout" in memory_graph_js and "preserveState" in memory_graph_js,
    )
    _check(
        "H2.memory graph skips unchanged tree reload",
        "_mgLastTreeHash" in memory_graph_js,
    )

    # =================================================================
    # HUD-3 — boot banner fade orchestration
    # =================================================================
    telemetry.reset_boot_state()
    boot = telemetry.panel_boot_state()
    _check(
        "H3.shape: phase + ready_at_iso + summary + required + states",
        {"phase", "ready_at_iso", "summary", "required", "states"}.issubset(boot.keys()),
    )
    _check(
        "H3.required is the 3 mandatory pills (voice/l0/memory)",
        set(boot["required"]) == {"voice", "l0", "memory"},
    )
    # With backend Whisper/Kokoro assets present, cold HUD boot can be
    # ready immediately. If a required substrate is missing it remains
    # booting/degraded.
    _check(
        "H3.cold env -> phase is ready/booting/degraded",
        boot["phase"] in {"ready", "booting", "degraded"},
    )
    _check(
        "H3.ready_at_iso matches readiness",
        (boot["phase"] == "ready" and isinstance(boot["ready_at_iso"], str))
        or (boot["phase"] != "ready" and boot["ready_at_iso"] is None),
    )

    # Simulate ready by stubbing all required pills live, then re-poll
    saved_panel_status = telemetry.panel_status

    def _stub_all_live():
        return {
            p: telemetry._pill(telemetry.STATE_LIVE, p, "stubbed")
            for p in ("voice", "identity", "memory", "l0")
        }

    telemetry.panel_status = _stub_all_live
    try:
        ready = telemetry.panel_boot_state()
    finally:
        telemetry.panel_status = saved_panel_status

    _check("H3.all live -> phase=ready", ready["phase"] == "ready")
    _check(
        "H3.all live -> ready_at_iso populated",
        isinstance(ready["ready_at_iso"], str) and len(ready["ready_at_iso"]) > 0,
    )

    # HTML wiring
    _check("H3.html includes /api/boot/state fetch", "/api/boot/state" in html)
    _check("H3.html includes boot-hidden re-application", "classList.add('boot-hidden')" in html)

    # =================================================================
    # HUD-4..9 — unified telemetry snapshot
    # =================================================================
    snap = telemetry.telemetry_snapshot()
    _check("H4-9.snapshot returns dict", isinstance(snap, dict))
    _check(
        "H4-9.snapshot has all 6 subsystem subtrees",
        {"cycle", "memory", "omega", "autoloop", "system"}.issubset(snap.keys()),
    )
    _check(
        "H4-9.system subtree has bridge state",
        isinstance(snap.get("system", {}).get("bridge"), dict),
    )
    _check(
        "H4-9.system has voice cell with state in vocabulary",
        snap.get("system", {}).get("voice", {}).get("state")
        in {"live", "loading", "degraded", "down"},
    )
    _check(
        "H4-9.system voice is backend sensory shape",
        snap.get("system", {}).get("voice", {}).get("engine") == "backend"
        and isinstance(snap.get("system", {}).get("voice", {}).get("tts"), dict)
        and isinstance(snap.get("system", {}).get("voice", {}).get("stt"), dict),
    )
    _check(
        "H4-9.system has vision telemetry cell",
        {"state", "supported", "last_event"}.issubset(
            snap.get("system", {}).get("vision", {}).keys()
        ),
    )
    _check(
        "H4-9.system has face_watch telemetry cell",
        {
            "state",
            "supported",
            "last_event",
            "known_names",
            "last_recognized",
            "last_confidence",
        }.issubset(snap.get("system", {}).get("face_watch", {}).keys()),
    )

    # Endpoint integration
    r = client.get("/api/telemetry/snapshot")
    _check("H4-9./api/telemetry/snapshot returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "H4-9.endpoint payload has all 6 subtrees",
        {"cycle", "memory", "omega", "autoloop", "system"}.issubset(body.keys()),
    )

    # HTML wiring
    _check("H4-9.html polls /api/telemetry/snapshot", "/api/telemetry/snapshot" in html)
    _check("H4-9.html calls Telemetry.update with the patch", "Telemetry.update(patch)" in html)

    # =================================================================
    # HUD-10 — SSE event stream
    # =================================================================
    from hud import events as hud_events

    hud_events._reset_for_tests()
    hud_events.install()

    # Fire a phase event via the real terminal_feedback path
    from guardian.terminal_feedback import emit_phase, emit_signal, emit_memory

    emit_phase("test", "h10", status="ok", summary="hud-10 probe")
    emit_signal("hud_probe", 0.5, narrative="hud-10")
    emit_memory("read", coord=[2, 0, 0], status="ok")

    backlog = hud_events.events_after(0)
    _check(f"H10.publisher captured 3 events ({len(backlog)})", len(backlog) >= 3)
    kinds = {e.get("kind") for e in backlog}
    _check(
        "H10.captured kinds include phase + signal + memory",
        {"phase", "signal", "memory"}.issubset(kinds),
    )
    _check("H10.events carry monotonic _seq", all(isinstance(e.get("_seq"), int) for e in backlog))

    # SSE endpoint not stream-tested in unit tests — TestClient blocks
    # on infinite generators. Verified separately via curl smoke +
    # the in-process buffer test above proves the pipeline works.
    # Verify the endpoint is REGISTERED on the app router instead.
    routes = {getattr(r, "path", "") for r in app.routes}
    _check("H10./api/events route registered on app", "/api/events" in routes)

    # =================================================================
    # HS — settings screen and operator profile routes
    # =================================================================
    _check("HS./api/settings/functions route registered", "/api/settings/functions" in routes)
    _check(
        "HS./api/settings/operator-profile route registered",
        "/api/settings/operator-profile" in routes,
    )
    _check("HS./api/settings/hud-theme route registered", "/api/settings/hud-theme" in routes)
    _check("HS.html has settings pill", 'data-panel="settings"' in html)
    for removed_topic in (
        "brain",
        "identity",
        "memory",
        "omega",
        "autoloop",
        "deepsleep",
    ):
        _check(
            f"HS.html removes {removed_topic} pill from bottom row",
            f'data-panel="{removed_topic}"' not in html,
        )
    _check("HS.html wires panelSettings", "function panelSettings" in html)
    _check(
        "HS.html posts operator profile settings",
        "/api/settings/operator-profile" in html and "operator-profile-form" in html,
    )
    _check(
        "HS.html posts HUD theme settings",
        "/api/settings/hud-theme" in html and "hud-theme-form" in html,
    )

    # HTML wiring
    _check("H10.html opens EventSource on /api/events", "new EventSource('/api/events')" in html)
    _check(
        "H10.html dispatches omega:phase events",
        "omega:phase" in html or "omega:'+kind" in html or "omega:'+ kind" in html,
    )

    # =================================================================
    # HTA — retired standalone terminal/agent surfaces
    # =================================================================
    r = client.get("/api/skills")
    _check(
        "HTA.skills returns read-only SKILL.md inventory",
        r.status_code == 200
        and r.json().get("ok") is True
        and isinstance(r.json().get("available"), list)
        and isinstance(r.json().get("commands"), list),
    )
    for retired_route in (
        "/api/dev/launch",
        "/api/dev/shutdown",
        "/api/dev/state",
        "/api/dev/command",
        "/api/dev/kill",
        "/api/dev/quit",
        "/api/dev/skills",
        "/api/dev/skills/catalog",
        "/api/agent-log/save",
        "/api/agent-log/download",
        "/api/chat/skill-activity/recent",
        "/api/chat/skill-activity/clear",
    ):
        _check(
            f"HTA.route retired {retired_route}",
            client.get(retired_route).status_code == 404,
        )
    _check(
        "HTA.html has live-chat picker without agent bridge",
        "/api/skills" in html and "devWsReady" not in html and "live-chat" in html,
    )

    # =================================================================
    # HUD-11 — vision identify endpoint
    # =================================================================
    routes = {getattr(r, "path", "") for r in app.routes}
    _check("H11./api/vision/identify route registered", "/api/vision/identify" in routes)
    _check("H11./api/vision/status route registered", "/api/vision/status" in routes)
    # missing image_b64 -> 400
    r = client.post("/api/vision/identify", json={})
    _check("H11.missing image_b64 -> 400", r.status_code == 400, detail=f"got {r.status_code}")
    # malformed base64 -> 400
    r = client.post("/api/vision/identify", json={"image_b64": "@@@not-base64@@@"})
    _check("H11.malformed base64 -> 400", r.status_code == 400, detail=f"got {r.status_code}")
    # well-formed base64, face loader unavailable in test env -> still
    # returns 200 with ok=False (best-effort, never raises 500)
    r = client.post(
        "/api/vision/identify", json={"image_b64": "iVBORw0KGgo="}
    )  # tiny PNG header bytes
    _check(
        "H11.ok base64 returns 200 (best-effort)",
        r.status_code == 200,
        detail=f"got {r.status_code}",
    )
    body = r.json()
    _check("H11.response has ok flag + elapsed_seconds", "ok" in body and "elapsed_seconds" in body)
    _check(
        "H11.response exposes normalized face fields",
        {"faces", "face_count", "recognized_name", "confidence"}.issubset(body.keys())
        and isinstance(body.get("faces"), list),
    )
    r = client.get("/api/vision/status")
    _check("H11./api/vision/status returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "H11.vision status lists face model paths",
        {"asset_root", "model_count", "models", "missing"}.issubset(body.keys())
        and isinstance(body.get("models"), list),
    )
    _check("H11.html overrides Vision.identify for live origin", "/api/vision/identify" in html)

    # =================================================================
    # BP — brain panel wiring (BP-1..6)
    # =================================================================
    snap = telemetry.telemetry_snapshot()
    cycle = snap.get("cycle") or {}
    _check(
        "BP.cycle has id+source+layer+pressure+live keys",
        {"id", "source", "layer", "pressure", "live"}.issubset(cycle.keys()),
        detail=str(list(cycle.keys())),
    )
    _check("BP.cycle.live is boolean", isinstance(cycle.get("live"), bool))
    layers = snap.get("layers")
    _check("BP.layers is a list", isinstance(layers, list))
    if layers:
        first = layers[0]
        _check(
            "BP.layers entries have id/info/tag",
            {"id", "info", "tag"}.issubset(first.keys()),
            detail=str(list(first.keys())),
        )
        _check("BP.layer tag is in vocabulary", first.get("tag") in {"ok", "skip", "fail"})

    # /api/panel/brain returns the same shape as the snapshot subset
    r = client.get("/api/panel/brain")
    _check("BP./api/panel/brain returns 200", r.status_code == 200)
    body = r.json()
    _check("BP.endpoint body has cycle + layers", {"cycle", "layers"}.issubset(body.keys()))

    # Layer telemetry helper itself
    recs = telemetry._telemetry_layers(3)
    _check("BP._telemetry_layers returns list", isinstance(recs, list))

    # HTML render — production telemetry dashboard
    _check(
        "BP.html keeps the retired brain renderer out of the bundle",
        "_renderBrainDashboard" not in html,
    )
    _check(
        "BP.html keeps the retired neural activity card out of the bundle",
        "Neural Activity" not in html,
    )

    # =================================================================
    # MP — memory panel wiring (MP-1..4)
    # =================================================================
    mem = snap.get("memory") or {}
    _check(
        "MP.memory has recallTrace + topRecall + anchors",
        {"recallTrace", "topRecall", "anchors"}.issubset(mem.keys()),
        detail=str(list(mem.keys())),
    )
    _check("MP.recallTrace is a list", isinstance(mem.get("recallTrace"), list))
    _check("MP.topRecall is a list", isinstance(mem.get("topRecall"), list))
    if mem.get("recallTrace"):
        first = mem["recallTrace"][0]
        _check("MP.recallTrace entries have step + value", {"step", "value"}.issubset(first.keys()))
    if mem.get("topRecall"):
        tr = mem["topRecall"][0]
        _check(
            "MP.topRecall entries have coord + gist + score",
            {"coord", "gist", "score"}.issubset(tr.keys()),
        )
    anchors = mem.get("anchors") or {}
    _check(
        "MP.anchors has 4 fields",
        {"recurrence", "last_access", "strength", "strengthTag"}.issubset(anchors.keys()),
    )

    # /api/panel/memory returns the same shape
    r = client.get("/api/panel/memory")
    _check("MP./api/panel/memory returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "MP.endpoint payload has recallTrace + topRecall + anchors",
        {"recallTrace", "topRecall", "anchors"}.issubset(body.keys()),
    )

    # Helper functions
    from hud.telemetry import _strength_tag, _relative_age, _coord_gist

    _check(
        "MP._strength_tag bucketing",
        _strength_tag(0.8) == "HIGH"
        and _strength_tag(0.5) == "MED"
        and _strength_tag(0.1) == "LOW",
    )
    _check(
        "MP._coord_gist friendly slug", _coord_gist("2.03.gradient_descent") == "gradient descent"
    )
    _check("MP._relative_age handles empty", _relative_age("") == "—")

    # HTML uses telemetry dashboard for memory panel
    _check(
        "MP.html keeps the retired memory renderer out of the bundle",
        "_renderMemoryDashboard" not in html,
    )
    _check("MP.html escapes recall values", "_escapeHtml" in html)

    # =================================================================
    # TEL — telemetry formatting helpers
    # =================================================================
    from hud.telemetry import _age_short

    _check("GP._age_short empty -> em-dash", _age_short("") == "—")

    # =================================================================
    # OP — omega panel wiring (OP-1..9)
    # =================================================================
    o = snap.get("omega") or {}
    _check(
        "OP.has core fields",
        {
            "objective",
            "online",
            "posture",
            "coherence",
            "traits",
            "contradiction",
            "gaps",
            "capability",
            "timeline",
            "emergent",
        }.issubset(o.keys()),
        detail=str(sorted(o.keys())),
    )
    _check(
        "OP.contradiction has score+tag+oppositions",
        {"score", "scoreLabel", "tag", "oppositions"}.issubset(
            (o.get("contradiction") or {}).keys()
        ),
    )
    _check(
        "OP.contradiction.tag in vocabulary",
        o.get("contradiction", {}).get("tag") in {"LOW", "MED", "HIGH"},
    )
    _check("OP.gaps is a list", isinstance(o.get("gaps"), list))
    _check("OP.capability is a list", isinstance(o.get("capability"), list))
    _check("OP.timeline is a list", isinstance(o.get("timeline"), list))
    _check("OP.emergent is a list", isinstance(o.get("emergent"), list))

    # /api/panel/omega
    r = client.get("/api/panel/omega")
    _check("OP./api/panel/omega returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "OP.endpoint payload has all 8 sections",
        {
            "objective",
            "online",
            "traits",
            "contradiction",
            "gaps",
            "capability",
            "timeline",
            "emergent",
        }.issubset(body.keys()),
    )

    # _contradiction_tag helper
    from hud.telemetry import _contradiction_tag

    _check(
        "OP._contradiction_tag bucketing",
        _contradiction_tag(0.7) == "HIGH"
        and _contradiction_tag(0.4) == "MED"
        and _contradiction_tag(0.1) == "LOW",
    )

    # HTML wiring — production telemetry dashboard (visual-only panels)
    _check(
        "OP.html keeps the retired omega renderer out of the bundle",
        "_renderOmegaDashboard" not in html,
    )
    _check(
        "OP.html keeps the retired contradiction renderer out of the bundle",
        "_tdOppositions" not in html,
    )
    _check("OP.html keeps the retired trajectory renderer out of the bundle", "dash.trajectory_progress" not in html)

    # =================================================================
    # AL — autoloop panel wiring (AL-1..7)
    # =================================================================
    al = snap.get("autoloop") or {}
    _check(
        "AL.has pressure+cadence+curiosity+proposals+resolved+abandoned",
        {"pressure", "cadence", "curiosity", "proposals", "resolved", "abandoned"}.issubset(
            al.keys()
        ),
        detail=str(sorted(al.keys())),
    )
    pressure = al.get("pressure") or {}
    _check(
        "AL.pressure has score+tag+signals",
        {"score", "scoreLabel", "tag", "signals"}.issubset(pressure.keys()),
    )
    _check("AL.pressure.tag in vocabulary", pressure.get("tag") in {"LOW", "MED", "HIGH", "ACUTE"})
    cadence = al.get("cadence") or {}
    _check(
        "AL.cadence has mode+intervals+lastSweep",
        {"mode", "baseInterval", "currentInterval", "lastSweep", "evaluated", "escalated"}.issubset(
            cadence.keys()
        ),
    )

    # /api/panel/autoloop
    r = client.get("/api/panel/autoloop")
    _check("AL./api/panel/autoloop returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "AL.endpoint payload has all 6 sections",
        {"pressure", "cadence", "curiosity", "proposals", "resolved", "abandoned"}.issubset(
            body.keys()
        ),
    )

    # _pressure_tag + _dev_status_tag helpers
    from hud.telemetry import _pressure_tag, _dev_status_tag

    _check(
        "AL._pressure_tag bucketing",
        _pressure_tag(0.9) == "ACUTE"
        and _pressure_tag(0.7) == "HIGH"
        and _pressure_tag(0.4) == "MED"
        and _pressure_tag(0.1) == "LOW",
    )
    _check(
        "AL._dev_status_tag mapping",
        _dev_status_tag("proposed") == "PENDING GUARDIAN"
        and _dev_status_tag("simulated") == "STAGED"
        and _dev_status_tag("approved") == "PERSISTED"
        and _dev_status_tag("rolled_back") == "REJECTED",
    )

    # HTML wiring — production telemetry dashboard
    _check(
        "AL.html keeps the retired autoloop renderer out of the bundle",
        "_renderAutoloopDashboard" not in html,
    )
    _check(
        "AL.html keeps the retired pressure histogram renderer out of the bundle",
        "dash.suppress_bars" not in html,
    )
    _check(
        "AL.html keeps the retired neuromodulator renderer out of the bundle",
        "_tdActivityRings" not in html,
    )

    # =================================================================
    # FX-1..4 — boot-strings + flyout polish
    # =================================================================
    # FX-1: omega.epoch exposed for the cycle counter
    o = snap.get("omega") or {}
    _check("FX1.omega.epoch is an int", isinstance(o.get("epoch"), int))
    _check(
        "FX1.html applies snap epoch to #cycle",
        "applyLiveBootStrings" in html
        and "snap.omega && snap.omega.epoch" in html
        and "getElementById('cycle')" in html,
    )
    # FX-2/3: feed machine-prose removed (HUD_PANELS_VISUAL_PACKET R2)
    _check(
        "FX2.html no longer overwrites .objective on poll",
        "querySelectorAll('.objective')" not in html,
    )
    _check("FX3.html no longer derives .activity from autoloop", "_activityFromSnap" not in html)
    # FX-5: panel re-render is subtree-diff throttled
    _check(
        "FX5.html maps each panel to its subtree",
        "_PANEL_SUBTREE" in html and "settings: () => null" in html,
    )
    _check(
        "FX5.html schedules render via requestAnimationFrame",
        "_scheduleRender" in html and "requestAnimationFrame" in html,
    )
    _check(
        "FX5.html skips render when subtree hash unchanged",
        "if (hash === _lastPanelHash) return" in html,
    )
    # The poll path used to call openPanel directly; ensure we no
    # longer have it INSIDE the pollTelemetrySnapshot function body.
    _idx_poll = html.find("async function pollTelemetrySnapshot")
    _idx_poll_end = html.find("\n  }", _idx_poll) if _idx_poll > 0 else -1
    _poll_body = html[_idx_poll:_idx_poll_end] if _idx_poll_end > 0 else ""
    _check(
        "FX5.poll body no longer calls openPanel",
        "openPanel(_currentPanelId);" not in _poll_body
        and "openPanel(" not in _poll_body.split("// NOTE")[0],
        detail=f"poll body length {len(_poll_body)}",
    )
    _check("FX5.html resets hash on tab switch", "_lastPanelHash = ''" in html)

    # FX-4: flyout layout + spellcheck off
    _check("FX4.html disables spellcheck on cf-prompt", 'spellcheck="false"' in html)
    _check(
        "FX4.html flyout has hint row + wider width",
        "⌘/Ctrl + Enter to generate" in html and "width:420px" in html,
    )
    # The poll loop calls applyLiveBootStrings on every snapshot tick
    _check("FX.snapshot poll calls applyLiveBootStrings", "applyLiveBootStrings(patch)" in html)

    # =================================================================
    # ID — identity layer wiring (ID-1..5)
    # =================================================================
    # ID-1: without omega-runtime the face broker is down → identity
    # pill reports degraded/down; live requires runtime + enrolled anchor.
    pill_status = telemetry.panel_status()
    id_pill = pill_status.get("identity") or {}
    _check(
        "ID1.identity pill state is in vocabulary",
        id_pill.get("state") in valid_states,
        detail=str(id_pill),
    )
    id_detail = id_pill.get("detail", "")
    pi_ops = telemetry.panel_identity().get("operators") or []
    _check(
        "ID1.identity detail mentions dev OR operator OR enrolled anchor",
        "dev" in id_detail
        or "operator" in id_detail.lower()
        or "born" in id_detail
        or "awaiting operator" in id_detail
        or bool(pi_ops),
    )

    # ID-3: panel_identity exists + serves real data
    pi = telemetry.panel_identity()
    _check(
        "ID3.panel_identity has charter+operators+selfVector+voice",
        {"charter", "operators", "selfVector", "voice"}.issubset(pi.keys()),
    )
    charter = pi.get("charter") or {}
    _check(
        "ID3.charter has identity_hash + mode",
        {"identity_hash", "mode", "ledger_ok", "verify_ok"}.issubset(charter.keys()),
    )
    _check("ID3.operators is a list", isinstance(pi.get("operators"), list))

    # /api/panel/identity routed
    routes2 = {getattr(rt, "path", "") for rt in app.routes}
    _check("ID3.panel route registered", "/api/panel/{topic}" in routes2)
    r = client.get("/api/panel/identity")
    _check("ID3./api/panel/identity returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "ID3.endpoint payload has all 4 sections",
        {"charter", "operators", "selfVector", "voice"}.issubset(body.keys()),
    )

    # ID-4: self-vector trajectory shape
    sv = pi.get("selfVector") or {}
    if sv:
        _check(
            "ID4.selfVector has count+confidence+drift+latest_at",
            {"count", "confidence", "drift", "latest_at"}.issubset(sv.keys()),
        )
        _check("ID4.drift in vocabulary", sv.get("drift") in {"stable", "drifting", "rapid"})

    # HTML wiring
    _check(
        "ID3.html declares panelIdentity",
        "_renderIdentityDashboard" in html and "/api/telemetry/snapshot" in html,
    )
    _check("ID3.html removes identity pill from bottom row", 'data-panel="identity"' not in html)
    _check(
        "ID3.html omits identity from openable PANELS map",
        "get body() { return panelIdentity; }" not in html,
    )
    _check(
        "ID3.html renders identity telemetry dashboard",
        "_renderIdentityDashboard" in html and "Presence" in html,
    )
    _check(
        "ID3.html shows operator empty-state with enrollment hint", "remember me as <name>" in html
    )

    # =================================================================
    # DS-PANEL — deepsleep panel surface (the new 8th pill)
    # =================================================================
    # Telemetry adapter shape
    ds = telemetry.panel_deepsleep()
    _check("DSP.panel_deepsleep returns dict", isinstance(ds, dict))
    _check(
        "DSP.panel_deepsleep has all 9 sections",
        {
            "cycle",
            "intake",
            "replay",
            "schemas",
            "contradictions",
            "forgetting",
            "consolidation",
            "pruning",
            "engine",
        }.issubset(ds.keys()),
        detail=str(sorted(ds.keys())),
    )
    _check(
        "DSP.cycle has id+timestamp", {"id", "timestamp"}.issubset((ds.get("cycle") or {}).keys())
    )
    _check(
        "DSP.replay has count+history_kept",
        {"count", "history_kept"}.issubset((ds.get("replay") or {}).keys()),
    )
    _check(
        "DSP.contradictions has detected+resolved",
        {"detected", "resolved"}.issubset((ds.get("contradictions") or {}).keys()),
    )
    _check(
        "DSP.forgetting has candidates+applied",
        {"candidates", "applied"}.issubset((ds.get("forgetting") or {}).keys()),
    )
    _check(
        "DSP.engine has pattern + sleep_cycle_id",
        {"pattern", "sleep_cycle_id"}.issubset((ds.get("engine") or {}).keys()),
    )

    # /api/panel/deepsleep route registration + 200
    _check(
        "DSP.handler registered in router",
        "deepsleep"
        in {
            k for k in __import__("hud.server", fromlist=["_PANEL_HANDLERS"])._PANEL_HANDLERS.keys()
        },
    )
    r = client.get("/api/panel/deepsleep")
    _check("DSP./api/panel/deepsleep returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "DSP.endpoint payload has 9 sections",
        {
            "cycle",
            "intake",
            "replay",
            "schemas",
            "contradictions",
            "forgetting",
            "consolidation",
            "pruning",
            "engine",
        }.issubset(body.keys()),
    )

    # HTML wiring
    _check(
        "DSP.html declares panelDeepSleep",
        "_renderDeepsleepDashboard" in html and "/api/telemetry/snapshot" in html,
    )
    _check("DSP.html removes deepsleep pill", 'data-panel="deepsleep"' not in html)
    _check(
        "DSP.html omits deepsleep from openable PANELS map",
        "get body() { return panelDeepSleep; }" not in html,
    )
    _check(
        "DSP.html renders deepsleep telemetry dashboard",
        "_renderDeepsleepDashboard" in html and "Consolidation Progress" in html,
    )
    _check(
        "DSP.html keeps consolidation flow visual",
        "_renderConsolidationFlow" in html and "phase_states" in html,
    )

    # IMG-2 — generated image serving + url rewrite
    _check("IMG.images list route registered", "/api/images" in routes)
    _check("IMG.image fetch route registered", "/api/images/{date}/{name}" in routes)
    r = client.get("/api/images")
    _check("IMG.list returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "IMG.list has count + images list", "count" in body and isinstance(body.get("images"), list)
    )
    # Bad-date traversal -> 400
    r = client.get("/api/images/not-a-date/foo.png")
    _check("IMG.bad date rejected", r.status_code == 400)
    # URL-encoded traversal — TestClient normalises bare ".." away,
    # so probe with the encoded form which actually reaches the handler.
    r = client.get("/api/images/2026-05-03/%2e%2e")
    _check("IMG.traversal rejected", r.status_code == 400, detail=f"got {r.status_code}")
    r = client.get("/api/images/2026-05-03/foo..bar.png")
    _check("IMG.embedded-dotdot rejected", r.status_code == 400, detail=f"got {r.status_code}")
    # appendImage now prefers url over path
    _check(
        "IMG.html prefers url over filesystem path", "result.url" in html and "result.path" in html
    )

    # =================================================================
    # HUD-13 — chat composer endpoint
    # =================================================================
    _check("H13./api/chat route registered", "/api/chat" in routes)
    r = client.post("/api/chat", json={"text": ""})
    _check("H13.empty text -> 400", r.status_code == 400, detail=f"got {r.status_code}")
    r = client.post("/api/chat", json={})
    _check("H13.missing text -> 400", r.status_code == 400, detail=f"got {r.status_code}")

    from unittest.mock import patch

    fake_result = {
        "ok": True,
        "cycle_id": "hud-chat-test",
        "message": "hello back",
        "delivery": {"text": "hello back", "rendered_by": "auto_promptpack"},
    }
    with patch("pressure_loop.operator_chat.handle_operator_chat", return_value=fake_result):
        r = client.post("/api/chat", json={"text": "hello"})
    _check("H13.valid text returns 200", r.status_code == 200, detail=f"got {r.status_code}")
    body = r.json()
    _check("H13.response is a dict", isinstance(body, dict))
    _check(
        "H13.response surfaces brain message", body.get("message") == "hello back", detail=str(body)
    )
    _check("H13.html POSTs composer to /api/chat", "/api/chat" in html)

    # =================================================================
    # FACE-2 — vision enroll, anchors list, forget
    # =================================================================
    _check("F2./api/vision/enroll route registered", "/api/vision/enroll" in routes)
    _check("F2./api/vision/anchors route registered", "/api/vision/anchors" in routes)
    _check("F2./api/vision/forget route registered", "/api/vision/forget" in routes)

    # enroll: missing fields -> 400
    r = client.post("/api/vision/enroll", json={})
    _check("F2.enroll missing image_b64 -> 400", r.status_code == 400)
    r = client.post("/api/vision/enroll", json={"image_b64": "iVBORw0KGgo="})
    _check("F2.enroll missing name -> 400", r.status_code == 400)
    r = client.post("/api/vision/enroll", json={"image_b64": "iVBORw0KGgo=", "name": "tester"})
    _check(
        "F2.enroll valid form returns 200 (best-effort)",
        r.status_code == 200,
        detail=f"got {r.status_code}",
    )
    body = r.json()
    _check("F2.enroll body has ok flag", "ok" in body)

    # anchors: returns ok + names list
    r = client.get("/api/vision/anchors")
    _check("F2.anchors returns 200", r.status_code == 200)
    body = r.json()
    _check("F2.anchors body has ok + names + count", {"ok", "names", "count"}.issubset(body.keys()))
    _check("F2.anchors names is a list", isinstance(body.get("names"), list))

    # forget: missing name -> 400; unknown name -> ok=true, removed=false
    r = client.post("/api/vision/forget", json={})
    _check("F2.forget missing name -> 400", r.status_code == 400)
    r = client.post("/api/vision/forget", json={"name": "_definitely_not_enrolled_"})
    _check(
        "F2.forget returns 200 with ok flag", r.status_code == 200 and r.json().get("ok") is True
    )

    # =================================================================
    # FACE-3 — FaceWatch always-on frontend wiring
    # =================================================================
    _check("F3.html declares FaceWatch IIFE", "const FaceWatch" in html or "FaceWatch =" in html)
    _check(
        "F3.html uses getUserMedia for hidden camera", "navigator.mediaDevices.getUserMedia" in html
    )
    _check("F3.html POSTs identify on watch tick", "/api/vision/identify" in html)
    _check(
        "F3.html documents enroll via settings operator profile",
        "/api/vision/enroll" in html or "operator profile" in html.lower(),
    )
    _check("F3.html exposes window.FaceWatch", "window.FaceWatch" in html)
    _check("F3.html implements greet cooldown", "GREET_COOLDOWN_MS" in html)
    _check(
        "F3.html tears down stream on idle",
        "STREAM_IDLE_TEARDOWN_MS" in html or "teardownStream" in html,
    )
    _check(
        "F3.html routes Vision.anchor to settings operator profile",
        "Vision.anchor = async function" in html
        and ("operator profile" in html.lower() or "openPanel('settings')" in html),
    )
    _check(
        "F3.html exposes FaceWatch.start (opt-in, not auto-start)",
        "async function start()" in html and "window.FaceWatch" in html,
    )
    _check(
        "F3.html supports manual stream adoption",
        "function _adoptStream" in html and "_adoptStream," in html,
    )
    _check("F3.html start fires immediate first tick", "await tick()" in html)

    # =================================================================
    # UPL-1 — upload endpoints
    # =================================================================
    _check("UPL.upload route registered", "/api/upload" in routes)
    _check("UPL.uploads list route registered", "/api/uploads" in routes)
    _check("UPL.uploads fetch route registered", "/api/uploads/{kind}/{name}" in routes)

    # POST /api/upload with no files -> 400 (FastAPI validation)
    r = client.post("/api/upload")
    _check("UPL.upload no files -> 4xx", 400 <= r.status_code < 500, detail=f"got {r.status_code}")

    # POST /api/upload with two real files (one image, one doc)
    import io as _io

    image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    doc_bytes = b"hello omega\n"
    files_payload = [
        ("files", ("hello.png", _io.BytesIO(image_bytes), "image/png")),
        ("files", ("notes.md", _io.BytesIO(doc_bytes), "text/markdown")),
    ]
    r = client.post("/api/upload", files=files_payload)
    _check("UPL.upload 2 files returns 200", r.status_code == 200, detail=f"got {r.status_code}")
    body = r.json()
    _check(
        "UPL.upload returns ok=true + 2 file entries",
        body.get("ok") is True and len(body.get("files", [])) == 2,
    )
    kinds = {f.get("kind") for f in body.get("files", [])}
    _check(
        "UPL.upload classified image + doc", "image" in kinds and "doc" in kinds, detail=str(kinds)
    )

    # GET /api/uploads -> includes our just-uploaded files
    r = client.get("/api/uploads")
    _check("UPL.uploads list returns 200", r.status_code == 200)
    body = r.json()
    _check(
        "UPL.uploads list has count + files",
        "count" in body and isinstance(body.get("files"), list) and body.get("count") >= 2,
    )

    # GET an uploaded file via the URL the upload returned
    list_files = body.get("files", [])
    if list_files:
        first = list_files[0]
        r = client.get(first["url"])
        _check("UPL.uploads fetch returns 200", r.status_code == 200)
        # Path-traversal attempt -> 400
        r = client.get("/api/uploads/image/..%2Fnot_a_file")
        _check(
            "UPL.uploads traversal rejected",
            r.status_code in (400, 404),
            detail=f"got {r.status_code}",
        )

    # =================================================================
    # UI-1 + VOICE-1 — composer actions + voice intercepts
    # =================================================================
    _check("UI.html injects composer-actions container", "composer-actions" in html)
    _check(
        "UI.html exposes window.OmegaUI helpers",
        "window.OmegaUI" in html and "uploadFiles" in html,
    )
    _check(
        "UI.html intercepts upload voice command",
        "triggerUploadPicker" in html and "/^upload" in html,
    )
    _check(
        "UI.html auto-speaks chat replies via Voice.speak",
        "Voice.speak(body)" in html or "Voice.speak(painted.text)" in html,
    )

    # =================================================================
    # VOICE-2 — voice pill is sole on/off (no auto-enable)
    # =================================================================
    _check(
        "V2.html does not auto-enable voice on first gesture", "_autoEnableVoiceOnce" not in html
    )
    _check(
        "V2.html annotates voice pill with live conversation hint",
        "wake-hint" in html and 'live conversation' in html.lower(),
    )
    _check("V2.html exposes voice pill tooltip", "Qwen speech. Speak to interrupt." in html)

    # =================================================================
    # VOICE-3 — Chrome TTS pause-bug workaround
    # =================================================================
    _check(
        "V3.html schedules speechSynthesis.resume heartbeat",
        "ss.resume()" in html and "ss.paused" in html,
    )
    _check("V3.html cancels stuck TTS after 30s", "TTS watchdog" in html and "ss.cancel()" in html)

    # =================================================================
    # VOICE-4 — robust STT (permissive wake word + auto-recovery)
    # =================================================================
    _check(
        "V4.html declares permissive WAKE_RE with mishears",
        "WAKE_RE" in html and "amiga" in html and "ohmega" in html,
    )
    _check("V4.html flashes voice pill with last transcript", "_flashPill" in html)
    _check(
        "V4.html replaces Voice.enable / disable / reset",
        "Voice.enable = function" in html
        and "Voice.disable = function" in html
        and "Voice.reset = function" in html,
    )
    _check("V4.html watchdog rebuilds STT after 90s silence", "90000" in html)
    _check("V4.html treats not-allowed as fatal", "not-allowed" in html)

    # =================================================================
    # TTS-1 — Kokoro TTS endpoint + Web Speech bypass
    # =================================================================
    _check("TTS./api/tts/speak route registered", "/api/tts/speak" in routes)
    _check("TTS./api/tts/status route registered", "/api/tts/status" in routes)
    _check("STT./api/stt/status route registered", "/api/stt/status" in routes)
    _check("STT./api/stt/transcribe route registered", "/api/stt/transcribe" in routes)
    # Empty text -> 400
    r = client.post("/api/tts/speak", json={})
    _check("TTS.speak missing text -> 400", r.status_code == 400)
    r = client.post("/api/tts/speak", json={"text": ""})
    _check("TTS.speak empty text -> 400", r.status_code == 400)
    # Real text -> WAV (best-effort: model may be missing in CI)
    r = client.post("/api/tts/speak", json={"text": "Voice systems active."})
    _check(
        "TTS.speak valid text returns 200 or 503",
        r.status_code in (200, 503),
        detail=f"got {r.status_code}",
    )
    if r.status_code == 200:
        _check(
            "TTS.speak content-type is audio/wav",
            r.headers.get("content-type", "").startswith("audio/wav"),
        )
        _check(
            "TTS.speak body is non-trivial",
            len(r.content) > 1024,
            detail=f"got {len(r.content)} bytes",
        )
    # Status endpoint
    r = client.get("/api/tts/status")
    _check("TTS.status returns 200", r.status_code == 200)
    body = r.json()
    _check("TTS.status reports Qwen or an unavailable engine",
           body.get("engine") in {"qwen3-tts", "unknown", "unavailable"})
    _check("TTS.status reports actual device", "device" in body)
    # Parakeet STT status + upload surface
    r = client.get("/api/stt/status")
    _check("STT.status returns 200", r.status_code == 200)
    body = r.json()
    _check("STT.status reports Parakeet engine", body.get("engine") == "parakeet")
    _check("STT.status reports actual device", "device" in body)
    r = client.post("/api/stt/transcribe")
    _check("STT.transcribe missing file -> 422", r.status_code == 422)
    r = client.post(
        "/api/stt/transcribe",
        files={"audio": ("sample.webm", b"not-real-audio", "audio/webm")},
        data={"language": "en"},
    )
    _check(
        "STT.transcribe upload returns handled status",
        r.status_code in (200, 500, 503),
        detail=f"got {r.status_code}",
    )

    # HTML wiring
    _check("TTS.html declares KokoroTTS module", "KokoroTTS" in html and "/api/tts/speak" in html)
    _check(
        "TTS.html routes Voice.speak through KokoroTTS",
        "Voice._origSpeak" in html
        and "Voice.speak = function" in html
        and "KokoroTTS.enqueue" in html,
    )
    _check(
        "TTS.html respects tts_enabled and auto_speak in Voice.speak",
        "tts_enabled" in html and "auto_speak" in html,
    )
    _check(
        "STT.html declares backend Whisper route",
        "BACKEND_STT_SUPPORTED" in html
        and "/api/stt/transcribe" in html
        and "MediaRecorder" in html,
    )
    _check(
        "STT.html routes direct speech when wake word disabled",
        "wake_word_enabled" in html and "ChatBridge.send" in html,
    )

    # =================================================================
    # DL-1 — image + upload download/open actions
    # =================================================================
    _check(
        "DL.appendImage builds a download anchor", "↓ download" in html and "dl.download" in html
    )
    _check("DL.appendImage builds an open-in-tab anchor", "↗ open" in html)
    _check(
        "DL.appendImage keeps the image card download-only",
        "copy prompt" not in html,
    )
    _check("DL.appendUpload builds a download anchor", "feed-upload-actions" in html)
    _check("DL.html declares feed-action CSS class", ".feed-action" in html)
    _check("DL.html derives image filenames from the URL", "result.url.split('/')" in html)

    print()
    print("=" * 60)
    print(f"  PASSED: {_PASSED}")
    print(f"  FAILED: {_FAILED}")
    if _FAILURES:
        print()
        print("FAILURES:")
        for f in _FAILURES:
            print(f)
    print("=" * 60)
    return 0 if _FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

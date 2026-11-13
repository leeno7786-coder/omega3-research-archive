"""
hud/presence_test.py — OPERATOR-PRESENCE LIVE TEST SURFACE (S2 verify)
=====================================================================

A thin, operator-run harness to live-verify the "Windows Hello" /
"the system sees the operator" behaviour: it drives the REAL face
models (runtime /face/identify) into the REAL S2 state machine
(`sensory.operator_presence.PresenceMachine`) and shows the latch live.

WHY THIS EXISTS
    The S2 contract delivers `PresenceMachine` + `run_watcher` as
    CODE ONLY; "operator verifies live" was reserved. This module IS
    that reserved live-verification surface, built on operator
    instruction. It is NOT the cognitive loop, NOT S3's closed
    reflective loop, NOT auto-speech. It only *visualises* presence.

CONVERGENT, NOT PARALLEL (anti-spaghetti)
    This module reimplements NOTHING. Recognition = the same
    `runtime.face.loader.get_loader().identify(bytes)` the production
    `/api/vision/identify` uses. Presence = the same
    `sensory.operator_presence.PresenceMachine` / `Observation` /
    `enrolled_operator_name` the S2 contract specifies (gate 19/0).
    Frames come from the browser webcam instead of a server cv2 loop
    (so: no OpenCV dep, no server camera) — that is the ONLY
    difference from `run_watcher`. Wire format mirrors the proven
    `hud.server` vision contract exactly: {"image_b64": "..."} with a
    data-URL prefix tolerated.

QUARANTINE
    Pure test/diagnostic surface. No Omega boot, no autonomous loop,
    no proactive speech. Explicit operator enrollment enters the same
    durable chat queue as the HUD; DeepSleep publishes its face projection.

The PresenceMachine private fields (`_acquire_count`,
`_sub_release_since`, `_stable`) are READ (never mutated) here purely
to render the live hysteresis to the operator — acceptable for an
in-process diagnostic surface.

ASCII only. Launch:  .\\.venv-brain\\Scripts\\python.exe -m hud.standalone --presence-test
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

# Convergent reuse — the EXACT S2 contract objects (no reimpl):
from sensory.operator_presence import (
    Observation,
    PresenceMachine,
    _emit_presence_event,
    enrolled_operator_name,
    presence_cfg,
)

# Convergent reuse — the EXACT production recognition path:
from runtime.face.recognition import identify
from pressure_loop.operator_vision import queue_face_memory

# Convergent reuse — the EXACT brain-side persistent anchor store that
# enrolled_operator_name() reads (the closed-world gate). Enrollment
# must land HERE, not only in the runtime db, or presence can never
# latch (recognition works runtime-side while operator_name stays None).
from runtime.face.anchor_store import get_anchor_store

log = logging.getLogger("omega.hud.presence_test")

app = FastAPI(
    title="Omega Operator-Presence Live Test Surface",
    description="S2 live verification — real models -> real PresenceMachine",
)

# Process-singleton machine (one operator, one latch). /reset re-arms.
_MACHINE = PresenceMachine()


def _utcnow() -> datetime:
    """Time seam — production uses the wall clock; the headless gate
    monkeypatches this to drive deterministic release timing without
    a request-payload backdoor (mirrors operator_presence._now)."""
    return datetime.now(timezone.utc)


def _decode_image_b64(payload: Dict[str, Any], *, field: str = "image_b64") -> bytes:
    """Mirror the proven hud.server vision contract: tolerate a
    'data:image/...;base64,' prefix, fail loudly on bad input."""
    b64 = payload.get(field) or ""
    if not isinstance(b64, str) or not b64:
        raise HTTPException(status_code=400, detail=f"{field} required")
    try:
        return base64.b64decode(b64.split(",", 1)[-1])
    except Exception as exc:  # noqa: BLE001 - explicit 400, no silent pass
        raise HTTPException(status_code=400, detail=f"{field} decode failed: {exc}") from exc


def _snapshot(event: Optional[str], raw_identify: Dict[str, Any]) -> Dict[str, Any]:
    """Render the live machine state for the operator. Reads private
    hysteresis fields READ-ONLY to visualise acquire/release."""
    cfg = presence_cfg()
    sub_since = _MACHINE._sub_release_since  # noqa: SLF001 - diag read only
    release_remaining: Optional[float] = None
    if sub_since is not None:
        elapsed = (_utcnow() - sub_since).total_seconds()
        release_remaining = max(0.0, float(cfg["release_seconds"]) - elapsed)
    return {
        "state": _MACHINE.state,
        "event": event,
        "stable": _MACHINE._stable,  # noqa: SLF001 - diag read only
        "acquire_count": _MACHINE._acquire_count,  # noqa: SLF001
        "acquire_frames": int(cfg["acquire_frames"]),
        "release_remaining": release_remaining,
        "enrolled_operator": enrolled_operator_name(),
        "recognized_name": raw_identify.get("recognized_name"),
        "confidence": float(raw_identify.get("confidence") or 0.0),
        "degraded": bool(raw_identify.get("degraded")),
        "thresholds": {
            "acquire": float(cfg["acquire_threshold"]),
            "release": float(cfg["release_threshold"]),
            "acquire_frames": int(cfg["acquire_frames"]),
            "release_seconds": float(cfg["release_seconds"]),
        },
        # Full raw model output — verify-don't-trust: show exactly what
        # the runtime returned (e.g. confidence-key mismatches surface
        # here instead of being silently swallowed).
        "raw_identify": raw_identify,
    }


@app.get("/api/presence/config")
async def api_presence_config() -> JSONResponse:
    cfg = presence_cfg()
    return JSONResponse(
        {
            "ok": True,
            "enrolled_operator": enrolled_operator_name(),
            "thresholds": {
                "acquire": float(cfg["acquire_threshold"]),
                "release": float(cfg["release_threshold"]),
                "acquire_frames": int(cfg["acquire_frames"]),
                "release_seconds": float(cfg["release_seconds"]),
            },
            "state": _MACHINE.state,
        }
    )


@app.post("/api/presence/reset")
async def api_presence_reset() -> JSONResponse:
    """Re-arm the latch for a fresh test run (e.g. before re-testing
    acquire from ABSENT). Does not touch enrollment / anchor store."""
    global _MACHINE
    _MACHINE = PresenceMachine()
    return JSONResponse({"ok": True, "state": _MACHINE.state})


def _canonical_name(name: str) -> str:
    """Case-canonical operator name. If an anchor already exists whose
    name matches case-insensitively, REUSE that exact stored casing —
    so 'noah' never spawns a second anchor next to 'Noah' (which would
    desync the closed-world operator gate). Otherwise use as-typed."""
    want = name.strip()
    try:
        for existing in get_anchor_store().names() or []:
            if existing.casefold() == want.casefold():
                return existing
    except Exception as exc:  # noqa: BLE001
        log.debug("presence_test canonical-name lookup failed (%s)", exc)
    return want


@app.post("/api/presence/enroll")
async def api_presence_enroll(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """Validate one sample and enqueue it through the production memory lane.

    Unusable frames return good:false. Accepted samples return queued:true;
    only a completed DeepSleep drain changes the derived presence cache.
    Existing anchor spelling is reused when resolving the requested name.
    """
    raw = _decode_image_b64(payload)
    if not (payload.get("name") or "").strip():
        raise HTTPException(status_code=400, detail="name required")
    cname = _canonical_name(payload["name"])

    def _bad(reason: str, **extra: Any) -> JSONResponse:
        return JSONResponse(
            {
                "ok": False,
                "good": False,
                "reason": reason,
                "name": cname,
                "samples": None,
                "enrolled_operator": enrolled_operator_name(),
                **extra,
            },
            status_code=200,
        )

    try:
        ident = identify(raw)
    except Exception as exc:  # noqa: BLE001 - surface, never silent-pass
        log.debug("presence_test enroll identify raised: %s", exc)
        return _bad(f"identify raised: {exc}")
    if not isinstance(ident, dict) or ident.get("degraded"):
        return _bad("runtime face inference degraded for this frame (skipped — trying more frames)")
    emb = ident.get("embedding")
    if not (isinstance(emb, list) and len(emb) == 512):
        return _bad(
            "no usable face in this frame (center your face, good light) — trying more frames",
            embedding_dims=(len(emb) if isinstance(emb, list) else None),
        )

    result = queue_face_memory(name=cname, embedding=emb,
                               image_b64=base64.b64encode(raw).decode("ascii"),
                               bbox=ident.get("bbox"))
    return JSONResponse(
        {**result, "good": bool(result.get("queued")), "name": cname,
         "enrolled_operator": enrolled_operator_name(),
         "recognized_before": ident.get("recognized_name")},
        status_code=202 if result.get("queued") else 400,
    )

@app.post("/api/presence/tick")
async def api_presence_tick(payload: Dict[str, Any] = Body(...)) -> JSONResponse:
    """One presence frame. The EXACT S2 path:
        loader.identify(bytes) -> Observation.from_identify
        -> PresenceMachine.observe(operator_name=enrolled_operator_name())
    Emits the same metadata-only Guardian event on a real transition."""
    raw = _decode_image_b64(payload)
    try:
        identify_result = identify(raw)
    except Exception as exc:  # noqa: BLE001
        # Fail-CLOSED, exactly like run_watcher: an error is degraded
        # (hold), never a confident operator match.
        log.debug("presence_test identify raised: %s — degraded", exc)
        identify_result = {
            "recognized_name": None,
            "confidence": 0.0,
            "degraded": True,
            "reason": f"identify_raised: {type(exc).__name__}",
        }
    obs = Observation.from_identify(identify_result)
    state, event = _MACHINE.observe(obs, operator_name=enrolled_operator_name(), now=_utcnow())
    if event:
        _emit_presence_event(event, summary=f"[presence-test] state={state}", state=state)
    return JSONResponse(_snapshot(event, identify_result))


# ── Minimal live UI (vanilla JS, browser webcam, ASCII only) ─────────
_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Omega Presence Live Test</title>
<style>
 body{font:14px/1.5 system-ui,Arial,sans-serif;margin:0;background:#0c0f14;color:#dde}
 header{padding:10px 16px;background:#11161f;border-bottom:1px solid #222b3a}
 h1{font-size:16px;margin:0;color:#9fd}
 .wrap{display:flex;gap:16px;padding:16px;flex-wrap:wrap}
 .col{flex:1;min-width:320px}
 video{width:100%;max-width:480px;border:1px solid #2a3446;border-radius:6px;background:#000}
 .banner{font-size:34px;font-weight:700;text-align:center;padding:18px;
   border-radius:8px;margin:8px 0;letter-spacing:2px}
 .PRESENT{background:#0f3d20;color:#7CFFB0;border:1px solid #1c7a3f}
 .ABSENT{background:#3d1414;color:#ff9b9b;border:1px solid #7a1c1c}
 .UNKNOWN{background:#2a2a2a;color:#cfcfcf;border:1px solid #555}
 .row{display:flex;justify-content:space-between;padding:4px 0;
   border-bottom:1px solid #1b2230}
 .k{color:#8aa} .v{font-weight:600}
 .bar{height:10px;background:#1b2230;border-radius:5px;overflow:hidden;margin-top:3px}
 .barf{height:100%;background:#3aa0ff;width:0%}
 button{background:#1c2a3f;color:#cde;border:1px solid #2f4a6e;
   border-radius:6px;padding:8px 14px;cursor:pointer;
   font-size:13px;margin-right:8px}
 button:hover{background:#274062}
 input{background:#0c121c;color:#dde;border:1px solid #2f4a6e;border-radius:6px;
   padding:7px 10px;font-size:13px;width:160px}
 #log{height:170px;overflow:auto;background:#0a0e15;border:1px solid #1b2230;
   border-radius:6px;padding:8px;font-family:Consolas,monospace;font-size:12px}
 .enter{color:#7CFFB0}.exit{color:#ff9b9b}.muted{color:#7788a0}
 .note{color:#88a;font-size:12px;margin-top:8px}
</style></head><body>
<header><h1>Omega &mdash; Operator Presence Live Test (S2 / "system sees operator")</h1></header>
<div class="wrap">
 <div class="col">
   <video id="vid" autoplay playsinline muted></video>
   <canvas id="cv" style="display:none"></canvas>
   <div style="margin-top:10px">
     <input id="nm" placeholder="operator name" value="Operator">
     <button onclick="enroll()">Enroll my face</button>
     <button onclick="resetM()">Reset latch</button>
   </div>
   <div class="note">Enroll once, then sit still: watch it acquire
     N/6 frames then latch <b>PRESENT</b> (PRESENCE_ENTER). Walk away:
     after the release window it drops to <b>ABSENT</b> (PRESENCE_EXIT).
     Cover part of your face (hysteresis band): it should HOLD, no flap.
     Kill the runtime: it should go <b>UNKNOWN</b> and HOLD (no false exit).</div>
 </div>
 <div class="col">
   <div id="banner" class="banner UNKNOWN">UNKNOWN</div>
   <div class="row"><span class="k">Enrolled operator</span><span class="v" id="op">-</span></div>
   <div class="row"><span class="k">Recognized</span><span class="v" id="rec">-</span></div>
   <div class="row"><span class="k">Confidence</span><span class="v" id="cf">-</span></div>
   <div class="bar"><div class="barf" id="cfb"></div></div>
   <div class="row"><span class="k">Acquire progress</span><span class="v" id="acq">-</span></div>
   <div class="row"><span class="k">Release in</span><span class="v" id="rel">-</span></div>
   <div class="row"><span class="k">Degraded (runtime down)</span><span class="v" id="dg">-</span></div>
   <div class="row"><span class="k">Thresholds</span><span class="v" id="th">-</span></div>
   <div style="margin-top:10px;color:#8aa">Event log</div>
   <div id="log"></div>
 </div>
</div>
<script>
const vid=document.getElementById('vid'),cv=document.getElementById('cv');
let last="UNKNOWN";
function logln(t,cls){const d=document.getElementById('log');
 const e=document.createElement('div');if(cls)e.className=cls;
 e.textContent="["+new Date().toLocaleTimeString()+"] "+t;
 d.appendChild(e);d.scrollTop=d.scrollHeight;}
async function start(){
 try{const s=await navigator.mediaDevices.getUserMedia({video:{width:480}});
   vid.srcObject=s;logln("camera started","muted");}
 catch(e){logln("camera DENIED/failed: "+e,"exit");return;}
 const c=await (await fetch('/api/presence/config')).json();
 document.getElementById('th').textContent=
   "acq>="+c.thresholds.acquire+" rel<"+c.thresholds.release+
   " /"+c.thresholds.acquire_frames+"fr /"+c.thresholds.release_seconds+"s";
 document.getElementById('op').textContent=c.enrolled_operator||"NOT ENROLLED";
 setInterval(tick,1000);
}
function grab(){cv.width=vid.videoWidth||480;cv.height=vid.videoHeight||360;
 cv.getContext('2d').drawImage(vid,0,0,cv.width,cv.height);
 return cv.toDataURL('image/jpeg',0.7);}
async function tick(){
 if(!vid.videoWidth)return;
 let r;try{r=await (await fetch('/api/presence/tick',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({image_b64:grab()})})).json();}catch(e){return;}
 const b=document.getElementById('banner');
 b.textContent=r.state;b.className="banner "+r.state;
 document.getElementById('op').textContent=r.enrolled_operator||"NOT ENROLLED";
 document.getElementById('rec').textContent=r.recognized_name||"-";
 document.getElementById('cf').textContent=r.confidence.toFixed(3);
 document.getElementById('cfb').style.width=Math.min(100,r.confidence*100)+"%";
 document.getElementById('acq').textContent=r.acquire_count+" / "+r.acquire_frames;
 document.getElementById('rel').textContent=
   r.release_remaining==null?"-":r.release_remaining.toFixed(1)+"s";
 document.getElementById('dg').textContent=r.degraded?"YES (HOLD)":"no";
 if(r.event==="PRESENCE_ENTER")logln("PRESENCE_ENTER  -> operator present","enter");
 if(r.event==="PRESENCE_EXIT")logln("PRESENCE_EXIT  -> operator absent","exit");
 if(r.state!==last){logln("state "+last+" -> "+r.state,"muted");last=r.state;}
}
let enrolling=false;
async function enroll(){
 if(enrolling)return; enrolling=true;
 const btn=event&&event.target; if(btn)btn.disabled=true;
 const nm=document.getElementById('nm').value.trim()||"Operator";
 const TARGET=5, MAXTRY=18; let good=0, tries=0, lastOp="-", lastErr="";
 logln("enrolling '"+nm+"' - hold still, good light...","muted");
 while(good<TARGET && tries<MAXTRY){
   tries++;
   let r; try{ r=await (await fetch('/api/presence/enroll',{method:'POST',
     headers:{'Content-Type':'application/json'},
     body:JSON.stringify({image_b64:grab(),name:nm})})).json(); }
   catch(e){ lastErr="net "+e; await sleep(250); continue; }
   if(r.good){ good=(r.samples!=null?r.samples:good+1);
     lastOp=r.enrolled_operator||lastOp;
     logln("  sample "+good+"/"+TARGET+" queued (try "+tries+")","enter"); }
   else { lastErr=r.reason||r.error||"bad frame";
     logln("  skip (try "+tries+"): "+lastErr,"muted"); }
   await sleep(260);
 }
 const okFinal=good>=TARGET;
 logln("enroll '"+nm+"' -> "+(okFinal?"QUEUED FOR MEMORY":"INCOMPLETE")+
   " ("+good+"/"+TARGET+" samples, "+tries+" tries) enrolled_operator="+
   lastOp+(okFinal?"":" last:"+lastErr), okFinal?"enter":"exit");
 if(btn)btn.disabled=false; enrolling=false;
}
function sleep(ms){return new Promise(r=>setTimeout(r,ms));}
async function resetM(){await fetch('/api/presence/reset',{method:'POST'});
 last="UNKNOWN";logln("latch reset","muted");}
start();
</script></body></html>"""


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(_PAGE)

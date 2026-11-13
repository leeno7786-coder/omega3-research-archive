// Vision capture + FaceWatch (hud/ui-src/core/vision-runtime.js)
// Depends on: Telemetry, Voice, OperatorIdentity, OmegaUI, openPanel
// Sections: vision (script 1), facewatch (LIVE WIRING script block)

// SECTION:vision
// ─────────────────────────────────────────────────────────────────
// Vision — getUserMedia capture + face-recognition handshake
// ─────────────────────────────────────────────────────────────────
// Camera access is Guardian-territory: every flow is consent-gated,
// every capture is operator-initiated, and a denied consent is itself
// a recordable Guardian event. Per the txt: "Done wrong = creepy.
// Done correctly = unforgettable." The unforgettable side is the
// "Welcome back, <operator>." beat — the recognition flash already exists
// in the boot sequence; this IIFE drives it on every Vision.identify().
//
// Two flows:
//   • identify()  → consent → frame → POST /vision/identify → flash
//   • anchor(name)→ consent → frame → POST /vision/anchor → confirm
//
// Mock fallback: if no backend is reachable, identify() returns the
// operator default; anchor() records the name locally. Demo never
// breaks even when running file:// with no service.
const Vision = (() => {
  const supported = typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === 'function';
  const ENDPOINT = 'http://localhost:13317/omega/vision';

  let mediaStream = null;
  let consent = false;

  function setState(state, last_event) {
    Telemetry.update({ system: { vision: { state, supported, consent, last_event } } });
  }

  // Build + show the consent modal. Resolves to true if the operator
  // approved, false if denied. Records the decision either way so
  // Guardian has an audit trail.
  function requestConsent() {
    if (consent) return Promise.resolve(true);
    return new Promise((resolve) => {
      const modal = document.createElement('div');
      modal.className = 'vision-consent-modal';
      modal.innerHTML = `
    <div class="vision-consent-frame">
      <div class="vision-consent-label">guardian · consent required</div>
      <div class="vision-consent-title">vision <em>access</em></div>
      <div class="vision-consent-body">
        Omega is requesting <em>camera access</em> for identity
        recognition. Captures are processed locally; no frames
        leave the device unless you explicitly anchor an identity.
        Guardian governs all writes.
      </div>
      <div class="vision-consent-buttons">
        <button class="verb deny">Deny</button>
        <button class="verb approve">Allow</button>
      </div>
    </div>`;
      document.body.appendChild(modal);
      // Force reflow so the .show transition fires
      void modal.offsetWidth;
      modal.classList.add('show');

      const finish = (granted) => {
        modal.classList.remove('show');
        setTimeout(() => modal.parentNode && modal.parentNode.removeChild(modal), 320);
        consent = granted;
        setState(granted ? 'consent-granted' : 'consent-denied',
          'operator ' + (granted ? 'approved' : 'denied'));
        resolve(granted);
      };
      modal.querySelector('.verb.approve').addEventListener('click', () => finish(true));
      modal.querySelector('.verb.deny').addEventListener('click', () => finish(false));
    });
  }

  // Open camera, settle one beat for autoexposure, draw a single
  // frame onto a canvas, return as JPEG dataURL. Throws on unsupported
  // / consent-denied / getUserMedia rejection.
  async function captureFrame() {
    if (!supported) { setState('unsupported', 'no getUserMedia'); throw new Error('unsupported'); }
    const granted = await requestConsent();
    if (!granted) throw new Error('consent-denied');
    setState('opening-camera', 'getUserMedia');
    if (!mediaStream) {
      mediaStream = await navigator.mediaDevices.getUserMedia({ video: true });
    }
    const video = document.createElement('video');
    video.srcObject = mediaStream;
    video.muted = true;
    await video.play();
    await new Promise(r => setTimeout(r, 350));   // autoexposure settle
    const canvas = document.createElement('canvas');
    canvas.width = video.videoWidth || 640;
    canvas.height = video.videoHeight || 480;
    canvas.getContext('2d').drawImage(video, 0, 0);
    setState('captured', canvas.width + 'x' + canvas.height);
    return canvas.toDataURL('image/jpeg', 0.85);
  }

  async function postOrMock(path, payload, mockResult, mockState) {
    setState('posting', path);
    try {
      const res = await fetch(ENDPOINT + path, {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error('http ' + res.status);
      const data = await res.json();
      setState('ok', path);
      return data;
    } catch (e) {
      setState(mockState, 'no backend; ' + e.message);
      return mockResult;
    }
  }

  // Trigger the existing identity-flash beat with a (possibly new)
  // name. The flash element starts the boot sequence with the operator's
  // display name (resolved via OperatorIdentity at boot); we update
  // its <em> on the fly so re-anchored or recognised operators get
  // the right name.
  function welcomeBack(name) {
    if (typeof Voice !== 'undefined') Voice.speak('Welcome back, ' + name + '.');
    const flash = document.getElementById('identity-flash');
    if (!flash) return;
    const em = flash.querySelector('em');
    if (em) em.textContent = name;
    flash.classList.remove('show');
    void flash.offsetWidth;       // restart the keyframe cycle
    flash.classList.add('show');
  }

  return {
    init() {
      setState(supported ? 'idle' : 'unsupported',
        supported ? 'init' : 'no getUserMedia');
    },
    requestConsent,
    captureFrame,
    async identify() {
      const frame = await captureFrame();
      // Mock fallback uses the operator's display name when /identify is
      // unreachable so demo flows don't hardcode any specific person.
      const mockName = (typeof OperatorIdentity !== 'undefined')
        ? OperatorIdentity.getName() : 'operator';
      return postOrMock('/identify', { frame },
        { name: mockName, confidence: 0.93, mock: true }, 'mock-identify');
    },
    async anchor(name) {
      const frame = await captureFrame();
      return postOrMock('/anchor', { name, frame },
        { name, anchored: true, mock: true }, 'mock-anchor');
    },
    welcomeBack,
    isSupported: () => supported,
    hasConsent: () => consent,
  };
})();
// SECTION:facewatch
(function () {
  'use strict';
// ── FACE-3: Opt-in face watch (Windows-Hello-style) ─────────
//
// Single hidden <video> + offscreen <canvas>. Once the operator has
// approved camera access (via Vision.requestConsent() — already
// wired in the design), FaceWatch:
//   • keeps the stream alive
//   • captures one frame every WATCH_INTERVAL_MS (8s default)
//   • POSTs to /api/vision/identify
//   • on recognised name with confidence >= threshold AND last
//     greet for that name was > GREET_COOLDOWN_MS ago, fires the
//     identity-flash + Voice.speak("Welcome back, <name>.")
//   • exposes FaceWatch.rememberMe(name, {context}) which captures
//     + POSTs to /api/vision/remember-face so the face/name lands in
//     vision-lobe memory (7,1) and the persona graph
//
// Resource discipline: pauses when document.hidden; tears down the
// stream after 5 idle minutes; resumes on visibility-change.
const FaceWatch = (function () {
  const WATCH_INTERVAL_MS = 8000;
  const GREET_COOLDOWN_MS = 5 * 60 * 1000;   // 5 min per name
  const STREAM_IDLE_TEARDOWN_MS = 5 * 60 * 1000;

  const supported = typeof navigator !== 'undefined' &&
    !!navigator.mediaDevices &&
    typeof navigator.mediaDevices.getUserMedia === 'function';

  let video = null;
  let canvas = null;
  let stream = null;
  let watching = false;
  let watchTimer = null;
  let lastGreetByName = Object.create(null);   // name -> ms
  let lastActivityAt = 0;
  let inflight = false;                         // single concurrent /identify
  let lastResult = null;                        // most recent identify response

  function setState(state, last_event) {
    if (typeof Telemetry !== 'undefined') {
      Telemetry.update({
        system: {
          face_watch: {
            state, supported, last_event,
            known_names: lastResult ? lastResult.candidates : [],
            last_recognized: lastResult && lastResult.recognized_name || null,
            last_confidence: lastResult && lastResult.confidence || 0,
          }
        }
      });
    }
  }

  function ensureElements() {
    if (!video) {
      video = document.createElement('video');
      video.style.cssText = 'position:fixed;width:1px;height:1px;' +
        'opacity:0;pointer-events:none;left:-10px;top:-10px;';
      video.muted = true; video.autoplay = true; video.playsInline = true;
      document.body.appendChild(video);
    }
    if (!canvas) {
      canvas = document.createElement('canvas');
      canvas.width = 320; canvas.height = 240;   // small for fast POST
    }
  }

  // Heuristic to skip Windows "Phone Link" / paired-phone cameras.
  // When Windows pairs a phone, the OS surfaces the phone camera as
  // a video device with names like "Phone Link Camera" / "<Model>
  // Camera". Picking that triggers the "trying to connect to my
  // phone" taskbar pop. We blacklist those by name so getUserMedia
  // never chooses them.
  function _isPhoneCamera(label) {
    const l = (label || '').toLowerCase();
    return /(phone\s*link|virtual\s*camera.*phone|connected\s*phone|android\s*phone|iphone\s*camera|ios\s*camera|continuity\s*camera)/.test(l)
      || /\bphone\b.*\bcamera\b/.test(l);
  }
  async function _pickWebcam() {
    // Probe the device list. Some browsers return empty `label`
    // until the user has granted ANY video permission once. So:
    //   1) enumerate
    //   2) if no labels, request a tiny throwaway stream just to
    //      reveal the labels, then enumerate again
    //   3) pick first non-phone videoinput
    let devices = await navigator.mediaDevices.enumerateDevices();
    let cams = devices.filter(d => d.kind === 'videoinput');
    const haveLabels = cams.some(c => c.label && c.label.length);
    if (!haveLabels && cams.length) {
      try {
        const probe = await navigator.mediaDevices.getUserMedia({ video: true });
        probe.getTracks().forEach(t => t.stop());
        devices = await navigator.mediaDevices.enumerateDevices();
        cams = devices.filter(d => d.kind === 'videoinput');
      } catch (_) { }
    }
    if (!cams.length) return null;
    // Prefer first non-phone camera. If everything looks like a
    // phone (unlikely), fall back to the first device anyway so
    // the operator can at least try.
    const real = cams.find(c => !_isPhoneCamera(c.label));
    const picked = real || cams[0];
    console.log('[FaceWatch] cameras:',
      cams.map(c => `${c.label || '(unlabeled)'} -> ${_isPhoneCamera(c.label) ? 'PHONE' : 'webcam'}`),
      'picked:', picked.label || picked.deviceId);
    return picked.deviceId;
  }
  let _cameraId = null;          // remember chosen webcam across teardowns

  async function openStream() {
    if (!supported) { setState('unsupported', 'no getUserMedia'); return false; }
    ensureElements();
    if (stream) return true;
    setState('opening', 'getUserMedia');
    // Resolve the camera once; reuse the deviceId on subsequent opens
    // so we don't re-probe (which can re-pop the OS prompt on some
    // browsers).
    if (!_cameraId) {
      try { _cameraId = await _pickWebcam(); }
      catch (e) { /* enumerateDevices itself is rare to fail */ }
    }
    const constraints = _cameraId
      ? {
        video: {
          deviceId: { exact: _cameraId },
          width: 640, height: 480
        }
      }
      : { video: { width: 640, height: 480, facingMode: 'user' } };
    try {
      stream = await navigator.mediaDevices.getUserMedia(constraints);
    } catch (e) {
      // Fallback path: if exact-deviceId failed (device unplugged,
      // changed id), retry with default constraints once.
      if (_cameraId) {
        _cameraId = null;
        try {
          stream = await navigator.mediaDevices.getUserMedia({
            video: { width: 640, height: 480, facingMode: 'user' },
          });
        } catch (e2) {
          setState('denied', 'getUserMedia rejected: ' + e2.message);
          return false;
        }
      } else {
        setState('denied', 'getUserMedia rejected: ' + e.message);
        return false;
      }
    }
    video.srcObject = stream;
    try { await video.play(); } catch (_) { }
    // Settle a beat so the first capture isn't pitch black
    await new Promise(r => setTimeout(r, 400));
    const label = stream.getVideoTracks()[0].label || 'camera';
    setState('streaming', label);
    // Defensive: if Windows still gave us the phone (deviceId
    // matched something we thought was a webcam but the runtime
    // label says otherwise), shut it down so we don't leak frames
    // to a phone the operator didn't intend.
    if (_isPhoneCamera(label)) {
      teardownStream('rejected-phone-camera');
      setState('denied', 'phone camera selected; install/enable a webcam or use FaceWatch.useCamera(deviceId)');
      return false;
    }
    lastActivityAt = Date.now();
    return true;
  }

  // Operator-facing helpers (DevTools): list cameras, pick one.
  async function listCameras() {
    const devs = await navigator.mediaDevices.enumerateDevices();
    return devs.filter(d => d.kind === 'videoinput')
      .map(d => ({
        deviceId: d.deviceId, label: d.label,
        isPhone: _isPhoneCamera(d.label)
      }));
  }
  async function useCamera(deviceId) {
    _cameraId = deviceId;
    teardownStream('camera-switch');
    return openStream();
  }

  function teardownStream(reason) {
    if (stream) {
      try { stream.getTracks().forEach(t => t.stop()); } catch (_) { }
      stream = null;
    }
    if (video) { video.srcObject = null; }
    setState('idle', 'teardown: ' + (reason || 'unknown'));
  }

  function captureBase64() {
    if (!video || !video.videoWidth) return null;
    const ctx = canvas.getContext('2d');
    // Letterbox preserve aspect to canvas size (320x240).
    const sw = video.videoWidth, sh = video.videoHeight;
    const scale = Math.min(canvas.width / sw, canvas.height / sh);
    const dw = sw * scale, dh = sh * scale;
    const dx = (canvas.width - dw) / 2, dy = (canvas.height - dh) / 2;
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(video, dx, dy, dw, dh);
    // 0.7 quality JPEG keeps payload < ~30 KB.
    const url = canvas.toDataURL('image/jpeg', 0.7);
    return url.split(',', 2).pop();
  }

  async function tick() {
    if (inflight) return;
    if (document.hidden) return;
    if (!stream && !(await openStream())) return;
    if (Date.now() - lastActivityAt > STREAM_IDLE_TEARDOWN_MS) {
      teardownStream('idle-timeout');
      return;
    }
    const b64 = captureBase64();
    if (!b64) return;
    inflight = true;
    try {
      const r = await fetch('/api/vision/identify', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ image_b64: b64 }),
      });
      if (r.ok) {
        // /api/vision/identify returns the SERVER shape directly
        // (ok, faces, ...). The HUD-11 patch above wraps Vision.identify
        // to map the first face — but here we want the raw response so
        // we can read recognized_name / confidence / face_count.
        const data = await r.json();
        // Two response shapes possible:
        //   { ok, faces: [{recognized_name, confidence, ...}], ... }
        //   { ok, recognized_name, confidence, face_count, ... }   (recognizer)
        let recognized = null, confidence = 0;
        if (data && data.ok) {
          if (data.recognized_name !== undefined) {
            recognized = data.recognized_name; confidence = data.confidence || 0;
          } else if (Array.isArray(data.faces) && data.faces[0]) {
            recognized = data.faces[0].recognized_name || null;
            confidence = data.faces[0].confidence || 0;
          }
          lastResult = {
            recognized_name: recognized,
            confidence,
            candidates: data.candidates || [],
          };
          setState('streaming', 'identify ok');
          if (recognized) {
            maybeGreet(recognized, confidence);
          }
        }
      }
    } catch (e) {
      setState('streaming', 'identify error: ' + e.message);
    } finally {
      inflight = false;
      lastActivityAt = Date.now();
    }
  }

  function maybeGreet(name, confidence) {
    const last = lastGreetByName[name] || 0;
    if (Date.now() - last < GREET_COOLDOWN_MS) return;
    lastGreetByName[name] = Date.now();
    // Use the design's existing welcomeBack flow (identity-flash +
    // TTS via Voice.speak). Never throw.
    try {
      if (typeof Vision !== 'undefined' && Vision.welcomeBack) {
        Vision.welcomeBack(name);
      } else if (typeof Voice !== 'undefined') {
        Voice.speak('Welcome back, ' + name + '.');
      }
    } catch (_) { }
  }

  async function start() {
    if (watching) return;
    watching = true;
    setState('starting', 'face-watch start');
    try { await tick(); } catch (_) { }
    watchTimer = setInterval(tick, WATCH_INTERVAL_MS);
  }

  function stop() {
    watching = false;
    if (watchTimer) { clearInterval(watchTimer); watchTimer = null; }
    teardownStream('explicit-stop');
  }

  // Visibility handling — when the operator returns to the tab,
  // resume watching but don't hammer the camera if they were just
  // checking another window briefly.
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) lastActivityAt = Date.now();
  });

  // Adopt a pre-acquired stream for manual diagnostics without
  // making FaceWatch own a second getUserMedia prompt.
  function _adoptStream(s) {
    if (stream) {
      try { s.getTracks().forEach(t => t.stop()); } catch (_) { }
      return;
    }
    ensureElements();
    stream = s;
    const label = (s.getVideoTracks()[0] || {}).label || 'camera';
    if (_isPhoneCamera(label)) {
      teardownStream('rejected-phone-camera');
      setState('denied', 'phone camera selected; use FaceWatch.useCamera');
      return;
    }
    video.srcObject = stream;
    try { video.play(); } catch (_) { }
    setState('streaming', label);
    lastActivityAt = Date.now();
  }

  return {
    isSupported: () => supported,
    start, stop,
    _adoptStream,
    // Camera selection — for "Windows tried to use my phone" rescue.
    // Open DevTools and run:
    //   await FaceWatch.listCameras()       -> [{deviceId, label, isPhone}]
    //   await FaceWatch.useCamera('<id>')   -> switch to a specific cam
    listCameras, useCamera,
    // For console diagnostics
    _state: () => ({
      watching, hasStream: !!stream,
      cameraId: _cameraId,
      lastResult, lastGreetByName
    }),
  };
})();
window.FaceWatch = FaceWatch;

if (typeof Vision !== 'undefined') {
  Vision.anchor = async function () {
    if (typeof window.OmegaUI !== 'undefined') {
      window.OmegaUI.appendLine('system',
        'Face memory is saved in Settings → operator profile. Open that panel to upload a photo or use the chat webcam button.');
    }
    if (typeof openPanel === 'function') openPanel('settings');
    return { ok: false, error: 'use settings operator profile' };
  };
}
})();

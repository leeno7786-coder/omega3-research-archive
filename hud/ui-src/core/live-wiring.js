// Live HUD wiring — clock, SSE, atmosphere, vision patches (hud/ui-src/core/live-wiring.js)
// Depends on: Telemetry, Voice, Vision, ChatBridge, openPanel, appendLine
// Sections: chrome (script 1), sse/atmos/vision (LIVE WIRING IIFE)

// SECTION:chrome
// ─────────────────────────────────────────────────────────────────
// Clock + cycle counter
// ─────────────────────────────────────────────────────────────────
function pad(n) { return n < 10 ? '0' + n : n; }
function tickClock() {
  const d = new Date();
  document.getElementById('clock').textContent =
    `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())} utc`;
}
// Cycle counter — live epoch from Telemetry (refreshed every 2s by
// startPolling()'s pollTelemetrySnapshot at the bottom of this file).
// Stays at '—' when the runtime isn't booted (standalone HUD, dev
// preview). Replaces the prior mock that incremented a random number.
//
// TDZ NOTE: Telemetry is declared with `const` ~200 lines below.
// The first immediate tickCycle() call below runs BEFORE Telemetry's
// IIFE — `typeof Telemetry` does NOT save us for let/const in the
// Temporal Dead Zone (it throws ReferenceError, halting all
// subsequent script execution and breaking every panel + chat).
// try/catch is the surgical fix; the 7s setInterval picks up the
// real value once Telemetry initializes.
function tickCycle() {
  let epoch = null;
  try {
    const t = Telemetry.snapshot();
    epoch = t && t.omega && t.omega.epoch;
  } catch (_) { /* Telemetry in TDZ on first call; fall through */ }
  document.getElementById('cycle').textContent = (epoch != null && epoch !== 0) ? String(epoch) : '—';
}
setInterval(tickClock, 1000); tickClock();
setInterval(tickCycle, 7000); tickCycle();

// ─────────────────────────────────────────────────────────────────
// Voice toggle (intentional invocation: click or press 'V')
// ─────────────────────────────────────────────────────────────────
// Bare-letter hotkeys must never fire while the operator is TYPING anywhere —
// the old guard only excluded <input>, so every textarea (system-messages
// answer box, create/doc flyout prompts, settings notes) leaked: typing 'c'
// popped the webcam face panel mid-sentence (operator bug 2026-07-11). Also
// ignore modifier chords (Ctrl+C = copy, not camera).
function isTypingContext(el) {
  if (!el) return false;
  if (el.isContentEditable) return true;
  return !!(el.matches && el.matches('input, textarea, select, [contenteditable]'));
}
function bareKey(e, key) {
  return e.key === key && !e.ctrlKey && !e.metaKey && !e.altKey
    && !isTypingContext(document.activeElement);
}
const voice = document.getElementById('voice');
voice.addEventListener('click', toggleVoice);
voice.addEventListener('keypress', e => { if (e.key === 'Enter') toggleVoice(); });
window.addEventListener('keydown', e => {
  if (bareKey(e, 'v')) toggleVoice();
});

const camera = document.getElementById('camera');
camera.addEventListener('click', openFaceMemoryFromWebcam);
camera.addEventListener('keypress', e => { if (e.key === 'Enter') openFaceMemoryFromWebcam(); });
window.addEventListener('keydown', e => {
  if (bareKey(e, 'c')) openFaceMemoryFromWebcam();
});
function openFaceMemoryFromWebcam() {
  if (typeof window.OmegaUI !== 'undefined' && window.OmegaUI.openFaceMemoryFlyout) {
    window.OmegaUI.openFaceMemoryFlyout();
    return;
  }
  if (typeof openPanel === 'function') openPanel('settings');
  if (typeof appendLine === 'function') {
    appendLine('system', 'Open Settings → operator profile to save face memory.');
  }
}
const shutdownBtn = document.getElementById('btn-shutdown');
if (shutdownBtn) {
  shutdownBtn.addEventListener('click', () => {
    if (!confirm('Are you sure you want to gracefully shutdown the substrate?')) return;
    fetch('/api/shutdown', { method: 'POST' })
      .then(r => r.json())
      .then(res => {
        if (res.ok) {
          if (typeof appendLine === 'function') appendLine('system', 'Shutdown initiated.');
          shutdownBtn.style.pointerEvents = 'none';
          shutdownBtn.style.opacity = '0.5';
        } else {
          if (typeof appendLine === 'function') appendLine('system', 'Shutdown failed: ' + res.error);
        }
      })
      .catch(err => {
        if (typeof appendLine === 'function') appendLine('system', 'Shutdown request failed: ' + err.message);
      });
  });
}
function toggleVoice() {
  const nowActive = !voice.classList.contains('active');
  // Check voice settings before enabling
  if (nowActive && typeof currentVoiceSettings === 'function') {
    const settings = currentVoiceSettings();
    if (!settings.stt_enabled) {
      // Show feedback that STT is disabled in settings
      if (typeof appendLine === 'function') {
        appendLine('system', 'voice input disabled in settings · enable in Settings → voice');
      }
      return;
    }
  }
  voice.classList.toggle('active');
  voice.setAttribute('aria-pressed', String(nowActive));
  // The pill toggles intent; the Voice IIFE owns whether the
  // browser actually has microphone access. If unsupported or
  // blocked, the visual stays toggled but listening stays off.
  if (typeof Voice !== 'undefined') {
    if (nowActive) Voice.enable();
    else Voice.disable();
  }
}

// SECTION:composer
// ─────────────────────────────────────────────────────────────────
// Composer — the single live-chat route
// ─────────────────────────────────────────────────────────────────
// Esc halts a build in progress (operator 2026-09-15): the turn in flight and the agent jobs
// behind it stop at their next safe boundary, and the files already written stay.
window.addEventListener('keydown', async e => {
  if (e.key !== 'Escape' || e.ctrlKey || e.metaKey || e.altKey) return;
  // Stop local playback immediately while the durable work-halt request is in flight.
  try {
    if (typeof Voice !== 'undefined' && typeof Voice.cancel === 'function') Voice.cancel();
  } catch (_) { }
  const pendingCycles = (typeof window !== 'undefined' && window._pendingChatResponses)
    ? Array.from(window._pendingChatResponses.keys())
    : [];
  const announce = message => {
    if (typeof appendLine === 'function') appendLine('system', message);
  };
  announce('Stop requested…');
  try {
    const response = await fetch('/api/agent/halt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cycle_ids: pendingCycles }),
    });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const result = await response.json();
    const stopped = Array.isArray(result.stopped_jobs) ? result.stopped_jobs : [];
    const unresolved = Array.isArray(result.unresolved_stops) ? result.unresolved_stops : [];
    if (result.stop_recorded === false || unresolved.length) {
      announce('Stop reached ' + stopped.length + ' job(s), but durable stop confirmation failed.');
    } else if (stopped.length) {
      announce('Stopped ' + stopped.length + ' running job(s).');
    } else {
      announce('No running work was found.');
    }
  } catch (error) {
    announce('Stop request failed: ' + (error && error.message ? error.message : String(error)));
  }
});

const input = document.getElementById('input');
let composerSending = false;
input.addEventListener('keydown', async e => {
  if (e.key !== 'Enter' || e.shiftKey) return;
  const hasUploads = !!(
    window.OmegaUI
    && window.OmegaUI.hasComposerUploads
    && window.OmegaUI.hasComposerUploads()
  );
  if (!input.value.trim() && !hasUploads) return;
  e.preventDefault();
  if (composerSending) return;
  composerSending = true;
  input.disabled = true;
  try {
    const text = input.value.trim();
    let uploads = [];
    if (hasUploads && window.OmegaUI.prepareComposerUploads) {
      uploads = await window.OmegaUI.prepareComposerUploads();
    }
    const body = window.OmegaUI && window.OmegaUI.composeMessageWithUploads
      ? window.OmegaUI.composeMessageWithUploads(text, uploads)
      : text;
    const displayText = window.OmegaUI && window.OmegaUI.composerUploadDisplayText
      ? window.OmegaUI.composerUploadDisplayText(text, uploads)
      : body;
    const firstUploadRef = uploads.length && window.OmegaUI && window.OmegaUI.uploadRefFor
      ? window.OmegaUI.uploadRefFor(uploads[0])
      : '';
    // ONE PIPELINE — chat bar straight to the brain's classifier (operator 2026-06-29). Every
    // operator line ships via ChatBridge.send -> POST /api/chat, where the classifier runs and the
    // brain decides everything. Nothing from the chat bar reaches a model except through
    // the classifier.
    appendLine('user', displayText);
    ChatBridge.send({
      kind: 'chat.user_message',
      source: 'user',
      body: body,
      uploads: uploads,
      upload_ref: firstUploadRef,
    });
    input.value = '';
    if (uploads.length && window.OmegaUI && window.OmegaUI.clearComposerUploads) {
      window.OmegaUI.clearComposerUploads();
    }
    pinFeedToBottom();
  } catch (err) {
    if (typeof appendLine === 'function') {
      appendLine('system', 'upload failed: ' + (err && err.message ? err.message : String(err)));
    }
  } finally {
    input.disabled = false;
    composerSending = false;
    input.focus();
  }
});

// SECTION:sse
(function () {
'use strict';
// ── HUD-10: SSE event stream ──────────────────────────────────
// Subscribes to /api/events; dispatches each phase/signal/memory
// event onto a global window event bus so panels can react. The
// browser auto-reconnects on disconnect with the Last-Event-ID
// header so we never miss events across server restarts.
let _es = null;
function startEventStream() {
  if (typeof EventSource === 'undefined') return;
  try {
    _es = new EventSource('/api/events');
    ['phase', 'signal', 'memory', 'inference', 'data'].forEach(kind => {
      _es.addEventListener(kind, (e) => {
        let payload = null;
        try { payload = JSON.parse(e.data); } catch { return; }
        window.dispatchEvent(new CustomEvent('omega:' + kind, { detail: payload }));
      });
    });
    _es.onerror = () => { /* browser auto-reconnects */ };
  } catch (e) { /* ignore — SSE optional */ }
}
startEventStream();

function _refreshOmegaMindFromSse(payload) {
  if (typeof window.refreshOmegaMindFromLiveEvent !== 'function') return;
  window.refreshOmegaMindFromLiveEvent(payload);
}
['omega:phase', 'omega:data', 'omega:inference'].forEach((eventName) => {
  window.addEventListener(eventName, (ev) => {
    try {
      _refreshOmegaMindFromSse((ev && ev.detail) || {});
    } catch (_) { /* non-fatal */ }
  });
});

window.addEventListener('omega:phase', (ev) => {
  try {
    const payload = (ev && ev.detail) || {};
    const loop = String(payload.loop || payload.system || '').toLowerCase();
    if (loop === 'deepsleep' && _currentPanelId === 'deepsleep'
      && typeof _refreshDeepsleepPanel === 'function') {
      _refreshDeepsleepPanel();
    }
  } catch (_) { /* non-fatal */ }
});

// Proactive pinboard: refresh immediately for every producer/store transition.
window.addEventListener('omega:data', (ev) => {
  try {
    const payload = (ev && ev.detail) || {};
    const phase = String(payload.phase || '');
    if (
      phase !== 'system_message_appended'
      && phase !== 'system_message_responded'
      && phase !== 'proactive_chat_appended'
      && phase !== 'result_presented_to_operator'
    ) {
      return;
    }
    if (typeof _refreshProactivePinboard === 'function') {
      _refreshProactivePinboard();
    }
  } catch (_) { /* non-fatal */ }
});

// ── Proactive / system / autonomous replies → right pinboard ──
// The center feed is operator conversation only. Every non-user reply is handed
// to the proactive side surface; its HTTP poll later deduplicates and acknowledges
// persisted PROACTIVE_RESPONSE notices without creating a second chat bubble.
window.addEventListener('omega:signal', (e) => {
  const sig = (e && e.detail) || null;
  if (!sig || sig.name !== 'chat.assistant_reply') return;
  const outreachId = String(sig.outreach_id || '').trim();
  const isOutreach = !!outreachId || !!sig.global_operator_outreach;
  if (sig.intake_source === 'user' && !isOutreach) return;
  const text = (sig.narrative || '').toString().trim();
  if (!text) return;
  try {
    if (typeof window._appendProactiveNotice === 'function') {
      window._appendProactiveNotice({
        id: outreachId || sig.message_id || '',
        narrative: text,
        episode_id: sig.episode_id || '',
        ts: sig.ts || new Date().toISOString(),
      });
    }
  } catch (_) { /* proactive surface not ready yet */ }
});

})();

// SECTION:atmos
// ── P3-S07: cross-panel atmosphere effects ──────────────────────
// Toggles body classes based on v2 substrate state so chrome
// shows pressure-loop-paused / presence-held / tick-suppressed
// ambient feedback regardless of which panel is open. Called from
// pollV2Fast after every successful update (state-driven, not
// animation-loop driven).
function _applyV2AtmosphereEffects() {
  const snap = Telemetry.snapshot();
  const v2 = (snap.system || {}).v2 || {};
  const body = document.body;
  if (!body) return;
  // Pressure-loop pause tint
  const paused = (v2.pressure_loop || {}).paused === true;
  body.classList.toggle('v2-pressure-paused', paused);
  // Presence-held edge-glow (anything other than PRESENT)
  const presence = (v2.presence || {}).state || '';
  const held = (presence === 'ABSENT' || presence === 'UNKNOWN');
  body.classList.toggle('v2-presence-held', held);
  // Tick-suppressed amber pulse (one cycle). Read suppress_reason
  // from the last pressure tick; flash for ~600ms then auto-clear.
  const tick = v2.pressure_last_tick || v2.cuedesk_last_tick || {};
  const suppress = String(tick.suppress_reason || '');
  // Empty string OR "pressure_loop_paused" — the latter is
  // already shown via the pause tint, so skip the redundant
  // amber flash on those ticks.
  const interestingSuppress = (
    suppress && suppress !== 'pressure_loop_paused'
  );
  if (interestingSuppress) {
    // Use a tick-id signature so we only flash ONCE per new
    // suppress event (the same suppress_reason could persist
    // across multiple polls).
    const sig = `${tick.decided_at || ''}:${suppress}`;
    if (_lastSuppressSig !== sig) {
      _lastSuppressSig = sig;
      body.classList.add('v2-tick-suppressed');
      clearTimeout(_suppressFlashTimer);
      _suppressFlashTimer = setTimeout(() => {
        body.classList.remove('v2-tick-suppressed');
      }, 600);
    }
  }
}
let _lastSuppressSig = '';
let _suppressFlashTimer = null;

// Signal ripple — temporary fading ring spawned at a pill's
// location when significant SSE events fire. Operator gets
// peripheral-vision awareness of substrate activity.
const _SIGNAL_RIPPLE_NAMES = new Set([
]);
const _SIGNAL_TO_PILL = {
};
function _spawnSignalRipple(pillSel) {
  const pill = document.querySelector(pillSel);
  if (!pill) return;
  const rect = pill.getBoundingClientRect();
  const ripple = document.createElement('span');
  ripple.className = 'v2-signal-ripple';
  ripple.style.left = (rect.left + rect.width / 2 - 3) + 'px';
  ripple.style.top = (rect.top + rect.height / 2 - 3) + 'px';
  document.body.appendChild(ripple);
  // Auto-cleanup after the 600ms animation completes; defensive
  // 800ms removal even if animationend doesn't fire (reduced-motion).
  ripple.addEventListener('animationend', () => {
    if (ripple.parentNode) ripple.parentNode.removeChild(ripple);
  }, { once: true });
  setTimeout(() => {
    if (ripple.parentNode) ripple.parentNode.removeChild(ripple);
  }, 800);
}
// Subscribe to /api/events signal stream. The omega:signal event
// bus is set up earlier (line 12112) — we just add a listener for
// the ripple-worthy names.
window.addEventListener('omega:signal', (e) => {
  const sig = (e && e.detail) || null;
  if (!sig || !sig.name) return;
  if (!_SIGNAL_RIPPLE_NAMES.has(sig.name)) return;
  const sel = _SIGNAL_TO_PILL[sig.name];
  if (sel) _spawnSignalRipple(sel);
});

function _latestAutonomicScore(samples) {
  if (!Array.isArray(samples) || samples.length === 0) return null;
  for (let i = samples.length - 1; i >= 0; i--) {
    const n = Number(samples[i] && samples[i].v);
    if (Number.isFinite(n)) return n;
  }
  return null;
}

function _autonomicTrendValues(autonomic, keys) {
  const series = keys
    .map(k => Array.isArray(autonomic[k]) ? autonomic[k] : [])
    .filter(a => a.length > 0);
  if (!series.length) return [];
  const count = Math.min(
    24,
    Math.max.apply(null, series.map(a => a.length))
  );
  const values = [];
  for (let i = 0; i < count; i++) {
    const row = [];
    series.forEach(samples => {
      const idx = samples.length - count + i;
      if (idx < 0) return;
      const n = Number(samples[idx] && samples[idx].v);
      if (Number.isFinite(n)) row.push(n);
    });
    if (row.length) {
      values.push(row.reduce((a, b) => a + b, 0) / row.length);
    }
  }
  return values;
}

function _renderAutonomicPillBadge(autonomic) {
  autonomic = autonomic || {};
  const defs = [
    ['dopamine', 'D', 'dopamine'],
    ['plasticity', 'P', 'plasticity'],
    ['strength', 'S', 'strength'],
    ['stress', 'T', 'stress'],
  ];
  const latest = defs.map(d => [
    d[0],
    d[1],
    d[2],
    _latestAutonomicScore(autonomic[d[0]]),
  ]);
  if (!latest.some(row => row[3] !== null)) return '';
  const values = _autonomicTrendValues(autonomic, defs.map(d => d[0]));
  const W = 32;
  const H = 10;
  let pts = '0,5 32,5';
  if (values.length >= 2) {
    const minV = Math.min.apply(null, values);
    const maxV = Math.max.apply(null, values);
    const rng = Math.max(0.0001, maxV - minV);
    const stepX = W / Math.max(1, values.length - 1);
    pts = values.map((v, i) => {
      const x = (i * stepX).toFixed(2);
      const y = (H - ((v - minV) / rng) * (H - 2) - 1).toFixed(2);
      return x + ',' + y;
    }).join(' ');
  }
  const scoreHtml = latest.map(row => {
    if (row[3] === null) return '';
    return '<span class="autonomic-pill-score ' + row[2] + '">' +
      row[1] + row[3].toFixed(2) + '</span>';
  }).join('');
  const title = latest.map(row => {
    return row[0] + ': ' + (row[3] === null ? '—' : row[3].toFixed(2));
  }).join(' · ');
  return '<span class="autonomic-pill" title="' + _escapeHtml(title) +
    '"><svg viewBox="0 0 32 10" preserveAspectRatio="none">' +
    '<polyline points="' + pts + '"></polyline></svg>' +
    '<span class="autonomic-pill-scores">' + scoreHtml + '</span></span>';
}

function _applyV2PillBadges() {
  // The HUD has no under-chat execution badges.
}

// SECTION:vision
// Runs inside HUD LIVE WIRING IIFE; `_liveOrigin` is in enclosing scope.

// HUD-11: vision identify/anchor -> /api/vision/identify
// The design's Vision.identify() captures a frame and posts it.
// Override its postOrMock target by patching identify/anchor to use
// the FastAPI endpoint directly.
if (_liveOrigin && typeof Vision !== 'undefined' && Vision.captureFrame) {
  const _origIdentify = Vision.identify;
  Vision.identify = async function () {
    const frame = await Vision.captureFrame();
    // strip "data:image/jpeg;base64," prefix — server tolerates
    // either form but cleaner to send just the payload.
    const b64 = (frame || '').split(',', 2).pop();
    try {
      const r = await fetch('/api/vision/identify', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ image_b64: b64 }),
      });
      if (!r.ok) throw new Error('http ' + r.status);
      const data = await r.json();
      if (typeof Telemetry !== 'undefined') {
        Telemetry.update({
          system: {
            vision: {
              state: 'ok', last_event: '/api/vision/identify',
            }
          }
        });
      }
      // Server returns {ok, faces:[...], elapsed_seconds}. Map the
      // first recognised face to the design's expected {name, ...}.
      if (data && data.ok && Array.isArray(data.faces) && data.faces.length) {
        const f = data.faces[0] || {};
        return {
          name: f.recognized_name || f.name || 'unknown',
          confidence: f.confidence || 1.0,
          faces: data.faces,
        };
      }
      return { name: 'unknown', confidence: 0.0, faces: [] };
    } catch (e) {
      // Backend failure — fall back to original mock so demo path holds.
      if (typeof Telemetry !== 'undefined') {
        Telemetry.update({
          system: {
            vision: {
              state: 'mock-identify', last_event: 'no backend; ' + e.message,
            }
          }
        });
      }
      return _origIdentify.call(Vision);
    }
  };
}

window.Build = {
  async generateDocument(prompt, opts) {
    opts = opts || {};
    const render = opts.render !== false;
    let result;
    try {
      const r = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          text: prompt,
          intent: 'DOCUMENT_CREATE',
          format: opts.format || 'md',
          template: opts.template || 'generic',
          upload_ref: opts.upload_ref || undefined,
        }),
      });
      const text = await r.text();
      try { result = JSON.parse(text); } catch { result = { ok: false, error: text }; }
      if (!r.ok && result.ok !== false) {
        result = { ok: false, error: 'http ' + r.status + ': ' + text.slice(0, 200) };
      }
    } catch (err) {
      result = { ok: false, error: 'network: ' + err.message };
    }
    if (result && result.queued === true) {
      if (render && typeof renderChatResult === 'function') {
        renderChatResult(result, null);
      }
      return result;
    }
    if (render) {
      if (result && result.ok && result.document && result.document.url) {
        appendDocumentMessage(result);
      } else if (typeof appendLine === 'function') {
        appendLine('assistant',
          '[document_gen failed] ' + (result && (result.error || JSON.stringify(result)) || 'unknown error'));
      }
    }
    return result;
  },
};

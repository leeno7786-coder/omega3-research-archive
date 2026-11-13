// Voice + Qwen streaming TTS + Parakeet STT (hud/ui-src/core/voice-runtime.js)
// Depends on: Telemetry, ChatBridge, Vision, OmegaUI, appendLine, currentVoiceSettings
// Sections: voice (script 1), kokoro/stt/wake (LIVE WIRING script block)

// SECTION:voice
// ─────────────────────────────────────────────────────────────────
// Voice — Web Speech STT + TTS, intentional invocation only
// ─────────────────────────────────────────────────────────────────
// Two halves with one IIFE:
//
//   STT (SpeechRecognition): always-on listener that ONLY acts on
//     phrases starting with "Omega" — the txt explicitly rejects a
//     wake-word ("hey omega" feels like a smart speaker). Saying
//     "Omega open memory" routes the same way the typed command
//     does. Anything not prefixed with "Omega" is ignored.
//
//   TTS (speechSynthesis): calm, slightly slower-than-default rate
//     and slightly lower pitch — the calibrated "system that has its
//     own posture" voice the txt asks for. Used by the boot sequence
//     to voice the system online; an external caller can speak any
//     line via Voice.speak(text).
//
// Listening requires a user gesture; calling Voice.enable() after a
// click works, calling it from page-load does not (browser refuses).
// Initial state therefore sits at 'idle' even when supported.
const Voice = (() => {
  // 2026-05-09: browser SpeechRecognition + speechSynthesis disabled.
  // Sensory I/O = ONLY the runtime's Kokoro (TTS) and Whisper (STT).
  // The dual-engine setup (browser + runtime) caused skip+repeat
  // playback when both fired in parallel (Edge Natural voice + SAPI
  // default doubled audio). Forcing both to false here makes every
  // existing browser-speech check bail out cleanly while preserving
  // the Voice.speak() / Voice.cancel() / Voice.start() API surface
  // (KokoroTTS replaces speak() with the runtime path below).
  const SR = false;
  const TTS = false;
  const supported = false;

  let recognition = null;
  let listening = false;
  const queue = [];
  let speaking = false;
  let lastSpokenText = '';
  let lastSpokenAt = 0;
  let _voiceWaitTimer = null;
  let _waitingForVoiceSince = 0;

  function setState(state, last_event) {
    Telemetry.update({ system: { voice: { state, supported, last_event } } });
  }

  // Pick a single, stable English voice. Cached for the whole session
  // so every utterance uses the same engine — no mid-stream voice
  // swaps. CRITICAL: only LOCAL voices are eligible. "Online" /
  // "Natural" / "Cloud" voices (Microsoft Aria Online (Natural), etc)
  // trigger a Chromium bug where Edge plays the cloud voice AND the
  // SAPI default in parallel — producing the "two voices" symptom.
  let _cachedVoice = null;
  function pickVoice() {
    if (!TTS) return null;
    if (_cachedVoice) return _cachedVoice;
    const voices = window.speechSynthesis.getVoices() || [];
    if (!voices.length) return null;       // not loaded yet
    // Reject any voice whose name screams "cloud" — these are the
    // Edge "Natural" voices and they double-play under Chromium.
    const isCloud = (v) => /\b(online|natural|cloud|wavenet|neural|edge)\b/i
      .test((v.name || '') + ' ' + (v.voiceURI || ''));
    const isEn = (v) => (v.lang || '').toLowerCase().startsWith('en');
    const local = voices.filter(v => isEn(v) && !isCloud(v));
    if (!local.length) {
      // No local English voice installed — fall back to ANY local
      // voice (skip cloud) so we never trigger the double-play bug.
      const anyLocal = voices.filter(v => !isCloud(v));
      if (anyLocal.length) { _cachedVoice = anyLocal[0]; return anyLocal[0]; }
      return null;
    }
    // FIRST CHOICE: any custom voice the operator installed locally.
    // These are usually third-party SAPI voices that take
    // absolute precedence over the stock Microsoft set.
    const operatorVoices = ['Friday', 'FRIDAY', 'Vader',
      'Cortana', 'HAL'];
    for (const name of operatorVoices) {
      const v = local.find(vc => (vc.name || '').toLowerCase()
        .includes(name.toLowerCase()));
      if (v) { _cachedVoice = v; return v; }
    }
    // SECOND CHOICE: the SYSTEM DEFAULT voice (voice.default === true).
    // This is critical to avoid Chromium's "double-play" bug — when
    // u.voice is set to a non-default voice, Chrome plays BOTH the
    // picked voice AND the system default. Picking the default itself
    // means there's nothing for the bug to double up.
    const sysDefault = local.find(v => v.default === true);
    if (sysDefault) { _cachedVoice = sysDefault; return sysDefault; }
    // THIRD CHOICE: prefer SAPI defaults that ship with Windows.
    const prefer = ['Microsoft Zira', 'Microsoft David', 'Microsoft Mark',
      'Microsoft Hazel', 'Samantha', 'Karen', 'Alex', 'Daniel',
      'Google US English'];
    for (const name of prefer) {
      const v = local.find(vc => (vc.name || '').includes(name));
      if (v) { _cachedVoice = v; return v; }
    }
    // Last resort within the local-en pool: en-US > any en-*
    const enUS = local.find(v => (v.lang || '').toLowerCase() === 'en-us');
    if (enUS) { _cachedVoice = enUS; return enUS; }
    _cachedVoice = local[0];
    return local[0];
  }
  // Refresh cache when voice list changes (some browsers populate async).
  // Also drain any speak() calls that were held while voices loaded —
  // boot lines that called speak before voices were ready will fire
  // here with the correct voice instead of Windows default).
  if (TTS) {
    try {
      window.speechSynthesis.addEventListener('voiceschanged', () => {
        _cachedVoice = null;
        if (queue.length && !speaking) processQueue();
      });
    } catch (_) { }
    // Trigger the async voice-list load immediately so it's hot
    // by the time the first speak() fires.
    try { window.speechSynthesis.getVoices(); } catch (_) { }
  }

  function processQueue() {
    if (!TTS || speaking) return;
    if (!queue.length) return;
    // BOOT FIX: voices populate async on Chromium. If the list is
    // empty (or Kokoro isn't reachable yet), DO NOT speak with the
    // Windows default — instead, hold the queue until voiceschanged
    // delivers the real list. The voiceschanged handler kicks
    // processQueue again. Boot lines therefore wait ~50-300ms for
    // Kokoro to be ready, then play with the correct voice.
    const voicesReady = (window.speechSynthesis.getVoices() || []).length > 0;
    if (!voicesReady) {
      // First call to getVoices() triggers async load on Chromium.
      // We've already attached a voiceschanged handler that re-fires
      // processQueue. Just wait.
      return;
    }
    let v = pickVoice();
    // If a preferred operator voice (Friday, etc.) hasn't
    // surfaced yet but Microsoft defaults exist, hold the queue —
    // we'd rather have the preferred voice speak the boot lines than the Windows
    // default. SAPI voices sometimes arrive in batches over the
    // first few seconds. Wait up to 5 s for the preferred voice.
    const haveOperatorVoice = (() => {
      const all = window.speechSynthesis.getVoices() || [];
      return all.some(vc => /\b(friday|vader|cortana|hal)\b/i
        .test(vc.name || ''));
    })();
    const waitedFor = _waitingForVoiceSince ? (Date.now() - _waitingForVoiceSince) : 0;
    if (!_waitingForVoiceSince) _waitingForVoiceSince = Date.now();
    // If we have voices but no operator voice AND we haven't waited
    // 5 s yet, hold for a re-check.
    const hasOperatorPreference = haveOperatorVoice ||
      /\b(friday|vader|cortana|hal)\b/i.test((v && v.name) || '');
    if (!v || (!hasOperatorPreference && waitedFor < 5000)) {
      if (!_voiceWaitTimer) {
        _voiceWaitTimer = setTimeout(() => {
          _voiceWaitTimer = null; processQueue();
        }, 250);
      }
      return;
    }
    if (v && !v._announced) {
      v._announced = true;
      console.log('[Voice] using:', v.name, '(' + v.lang + ')',
        v.voiceURI ? 'uri=' + v.voiceURI : '');
    }
    _voiceWaitTimer = null;
    _waitingForVoiceSince = 0;

    const next = queue.shift();
    speaking = true;
    setState('speaking', 'utterance: ' + next.text.slice(0, 40));
    // Hard reset the engine before each utterance.
    try { window.speechSynthesis.cancel(); } catch (_) { }
    const u = new SpeechSynthesisUtterance(next.text);
    if (v) {
      u.voice = v;
      // Some Chromium builds honour voiceURI even when voice gets
      // dropped. Setting both is belt + braces.
      if (v.voiceURI) u.voiceURI = v.voiceURI;
      // CRITICAL: align u.lang to u.voice.lang. When they mismatch,
      // Chromium falls back to SAPI default IN ADDITION to the
      // picked voice — same content, two voices.
      u.lang = v.lang || 'en-US';
    } else {
      u.lang = 'en-US';
    }
    u.rate = next.opts.rate != null ? next.opts.rate : 0.96;
    u.pitch = next.opts.pitch != null ? next.opts.pitch : 0.92;
    u.volume = next.opts.volume != null ? next.opts.volume : 1.0;
    const finish = () => {
      speaking = false;
      setState(listening ? 'listening' : 'idle', 'utterance-end');
      processQueue();
    };
    u.onend = finish;
    u.onerror = finish;
    // Longer drain after cancel() — 50ms races on some Chromium
    // builds and lets the previous utterance bleed into the next.
    // 200ms is comfortably past the engine reset; still imperceptible.
    setTimeout(() => {
      try { window.speechSynthesis.speak(u); }
      catch (e) { finish(); }
    }, 200);
  }

  // Route a recognised "Omega ..." phrase the same way the composer
  // routes a typed line. Returns true if handled.
  function dispatch(transcript) {
    const t = transcript.trim();
    if (typeof showToast === 'function') showToast(t);
    const m = t.match(/^omega,?\s*(?:open|switch to|show)\s+(help|settings)/i);
    if (m) {
      const id = m[1].toLowerCase();
      document.querySelectorAll('.pill').forEach(b => b.classList.toggle('active', b.dataset.panel === id));
      if (typeof openPanel === 'function') openPanel(id);
      return true;
    }
    if (/^omega,?\s*(exit|close|done|stand\s*down)/i.test(t)) {
      if (typeof closePanel === 'function') closePanel();
      return true;
    }
    // Vision: identify ("Omega scan me" / "Omega who am I")
    if (/^omega,?\s*(scan\s+me|who\s+am\s+i)/i.test(t)) {
      if (typeof Vision !== 'undefined') {
        Vision.identify().then(r => {
          if (r && r.name) Vision.welcomeBack(r.name);
        }).catch(() => { /* consent denied or no support — already logged */ });
      }
      return true;
    }
    // Vision: anchor ("Omega remember me as <name>")
    const am = t.match(/^omega,?\s*remember\s+me\s+as\s+(.+)$/i);
    if (am && typeof Vision !== 'undefined') {
      Vision.anchor(am[1].trim().replace(/[.!?]$/, '')).catch(() => { });
      return true;
    }
    // Otherwise treat as a chat utterance — strip the "Omega" prefix
    // before shipping (the prefix is invocation, not content).
    const stripped = t.replace(/^omega,?\s*/i, '').trim();
    if (stripped && typeof ChatBridge !== 'undefined') {
      if (typeof appendLine === 'function') appendLine('user', stripped);
      ChatBridge.send({ kind: 'chat.user_message', source: 'user', body: stripped });
      return true;
    }
    return false;
  }

  return {
    init() {
      if (!supported) {
        setState('unsupported',
          !SR ? 'no SpeechRecognition'
            : 'no speechSynthesis');
        return;
      }
      // Some browsers populate voices async; trigger a load attempt.
      try { window.speechSynthesis.getVoices(); } catch (_) { }
      setState('idle', 'init');
    },
    enable() {
      if (!supported) return false;
      // Check voice settings - respect stt_enabled setting
      const voiceSettings = (typeof currentVoiceSettings === 'function') ? currentVoiceSettings() : null;
      if (voiceSettings && !voiceSettings.stt_enabled) {
        setState('disabled', 'stt disabled in settings');
        return false;
      }
      if (listening) return true;
      try {
        recognition = new SR();
        recognition.continuous = true;
        recognition.interimResults = false;
        // Use language from voice settings if available
        recognition.lang = (voiceSettings && voiceSettings.stt_language) ? voiceSettings.stt_language.replace('_', '-') : 'en-US';
        recognition.addEventListener('result', (ev) => {
          const last = ev.results[ev.results.length - 1];
          if (!last || !last.isFinal) return;
          const transcript = (last[0] && last[0].transcript) || '';
          // Intentional invocation guard — per the txt, no wake-word.
          if (!/^omega\b/i.test(transcript)) return;
          dispatch(transcript);
        });
        recognition.addEventListener('end', () => {
          // Chrome auto-ends sessions every ~60s. Restart while
          // we're still in listening intent.
          if (listening) { try { recognition.start(); } catch (_) { } }
        });
        recognition.addEventListener('error', (e) => {
          setState('error', 'recognition: ' + (e.error || 'unknown'));
        });
        recognition.start();
        listening = true;
        setState('listening', 'enabled by user');
        return true;
      } catch (e) {
        setState('error', 'start-failed: ' + e.message);
        return false;
      }
    },
    disable() {
      if (!listening) return;
      listening = false;
      try { recognition && recognition.stop(); } catch (_) { }
      setState('idle', 'disabled by user');
    },
    speak(text, opts = {}) {
      if (!TTS || !text) return;
      // Check voice settings - respect tts_enabled setting
      const voiceSettings = (typeof currentVoiceSettings === 'function') ? currentVoiceSettings() : null;
      if (voiceSettings && !voiceSettings.tts_enabled) return;
      const s = String(text);
      // Dedup: if the SAME text was queued or spoken in the last
      // 1.5 s, drop the duplicate. Prevents two callers (FaceWatch
      // greet + boot greet, chat-reply auto-TTS + a stray welcomeBack,
      // etc.) from saying the same line twice in close succession.
      const now = Date.now();
      if (queue.some(q => q.text === s)) return;
      if (lastSpokenText === s && now - lastSpokenAt < 1500) return;
      lastSpokenText = s; lastSpokenAt = now;
      // Merge voice settings into opts (speed, pitch)
      const mergedOpts = { ...opts };
      if (voiceSettings) {
        if (voiceSettings.tts_speed != null) mergedOpts.rate = voiceSettings.tts_speed;
        if (voiceSettings.tts_pitch != null) mergedOpts.pitch = voiceSettings.tts_pitch;
      }
      queue.push({ text: s, opts: mergedOpts });
      processQueue();
    },
    cancel() {
      // Hard reset of the TTS queue + browser engine. Used by
      // VOICE-3/4 watchdogs and Voice.disable.
      queue.length = 0;
      try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (_) { }
      speaking = false;
    },
    isListening: () => listening,
    isSupported: () => supported,
  };
})();
// SECTION:kokoro
(function () {
  'use strict';
// ── TTS-1: Route Voice.speak through the runtime TTS endpoint ─
// The design's Voice.speak fires Web Speech API (browser TTS),
// which on Windows triggers the duplicate-voice + Microsoft-default
// problems. We have runtime-owned TTS server-side, so we bypass Web Speech
// entirely: live deltas use /api/tts/stream (PCM), ordinary replies use
// /api/tts/speak (WAV) for broad browser compatibility.
// blob -> HTML Audio. ONE engine, ONE voice.
//
// Falls back to the original Voice.speak (Web Speech) if the
// endpoint is unreachable so file:// previews still beep out a
// voice during demo iteration.
const KokoroTTS = (function () {
  const QUEUE = [];
  const _jobs = new Set();
  let playing = false;
  let lastText = ''; let lastAt = 0;
  let _audio = null;             // currently playing HTMLAudioElement
  let _activeUrl = null;         // object URL owned by the active audio element
  let _prefetch = null;          // one synthesis request ahead of playback
  let _streamController = null;  // aborts the active raw-PCM fetch
  let _streamContext = null;     // reused WebAudio context for live chunks
  let _streamSources = [];       // scheduled sources stopped on barge-in
  let _streamNextTime = 0;       // next 24 kHz frame presentation time
  let _generation = 0;           // invalidates async synthesis after cancel()
  function _finishJob(job, completed) {
    if (!job || job.finished) return;
    job.finished = true;
    _jobs.delete(job);
    job.resolve(!!completed);
  }
  function _finishItem(item, completed) {
    if (!item || item.finished) return;
    item.finished = true;
    if (!completed) {
      _finishJob(item.job, false);
      return;
    }
    item.job.remaining -= 1;
    if (item.job.remaining === 0) _finishJob(item.job, true);
  }
  function _finishAllJobs(completed) {
    Array.from(_jobs).forEach(job => _finishJob(job, completed));
  }
  // Backend-availability COOLDOWN (operator bug 2026-07-12: "it never once
  // spoke and I had the volume all the way up"). The old latch was permanent:
  // ONE 5xx/404/network failure — near-guaranteed at session start, because
  // Kokoro is a hotswap model that is rarely resident when the first speak
  // fires — silently muted the ENTIRE session; the only re-arm was cancel(),
  // which runs on pill OFF (so voice ON stayed dead until toggled off+on).
  // Now a failure opens a bounded cooldown window: enqueues drop while it is
  // open (boot-banner flood protection preserved), and the next speak after
  // expiry retries naturally — a warm runtime gets voice back without any
  // operator ritual. The first trip per session says so in the chat feed
  // (honest signal, not a console-only whisper).
  const UNAVAILABLE_COOLDOWN_MS = 45000;
  let _unavailableUntil = 0;
  let _notifiedUnavailable = false;
  function _coolingDown() { return Date.now() < _unavailableUntil; }
  function _tripUnavailable() {
    _unavailableUntil = Date.now() + UNAVAILABLE_COOLDOWN_MS;
    QUEUE.length = 0;
    _prefetch = null;
    _finishAllJobs(false);
    if (!_notifiedUnavailable) {
      _notifiedUnavailable = true;
      try {
        if (typeof appendLine === 'function') {
          appendLine('system',
            'voice output unavailable — TTS runtime not ready; will retry automatically');
        }
      } catch (_) { /* honest signal is best-effort */ }
    }
  }
  function _ttsPayload(item) {
    const text = item && typeof item === 'object' ? item.text : item;
    const opts = item && typeof item === 'object' && item.opts ? item.opts : {};
    const payload = { text: String(text || '') };
    if (opts.voice != null) payload.voice = opts.voice;
    if (opts.speed != null) payload.speed = opts.speed;
    else if (opts.rate != null) payload.speed = opts.rate;
    if (opts.pitch != null) payload.pitch = opts.pitch;
    return payload;
  }
  function _requestAudio(next) {
    return fetch('/api/tts/speak', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(_ttsPayload(next)),
    }).then(r => {
      if (r.status >= 500 || r.status === 404) {
        throw new Error('tts unavailable (http ' + r.status + ') — cooling down');
      }
      if (!r.ok) throw new Error('http ' + r.status);
      return r.blob();
    });
  }

  // Live mode consumes qwentts.cpp's raw s16le stream directly.  AudioBuffer
  // accepts an explicit 24 kHz rate and the browser resamples to the device,
  // so this remains correct on 44.1/48 kHz Windows outputs.
  async function _requestStream(next) {
    const generation = _generation;
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx || !window.fetch || !window.AbortController) {
      throw new Error('tts stream unsupported by this browser');
    }
    const controller = new AbortController();
    const stale = () => controller.signal.aborted || generation !== _generation;
    _streamController = controller;
    return fetch('/api/tts/stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(_ttsPayload(next)),
      signal: controller.signal,
    }).then(async r => {
      if (!r.ok) throw new Error('tts stream unavailable (http ' + r.status + ')');
      if (!r.body || !r.body.getReader) throw new Error('tts stream has no readable body');
      if (!_streamContext) _streamContext = new AudioCtx();
      const ctx = _streamContext;
      if (ctx.state === 'suspended' && ctx.resume) {
        try { await ctx.resume(); } catch (_) { /* playback will report on source */ }
      }
      const reader = r.body.getReader();
      try {
        if (stale()) return;
        let carry = new Uint8Array(0);
        let cursor = Math.max(ctx.currentTime + 0.03, _streamNextTime || 0);
        let sawAudio = false;
        for (;;) {
          const part = await reader.read();
          if (stale()) return;
          if (part.done) break;
          const incoming = part.value instanceof Uint8Array
            ? part.value : new Uint8Array(part.value || []);
          if (!incoming.length) continue;
          const bytes = new Uint8Array(carry.length + incoming.length);
          bytes.set(carry, 0); bytes.set(incoming, carry.length);
          const usable = bytes.length - (bytes.length % 2);
          carry = usable < bytes.length ? bytes.slice(usable) : new Uint8Array(0);
          if (!usable) continue;
          const samples = new Float32Array(usable / 2);
          for (let i = 0; i < samples.length; i++) {
            const lo = bytes[i * 2];
            const hi = bytes[i * 2 + 1];
            let value = lo | (hi << 8);
            if (value & 0x8000) value -= 0x10000;
            samples[i] = value / 32768;
          }
          const buffer = ctx.createBuffer(1, samples.length, 24000);
          buffer.getChannelData(0).set(samples);
          const source = ctx.createBufferSource();
          source.buffer = buffer;
          source.connect(ctx.destination);
          cursor = Math.max(cursor, ctx.currentTime + 0.03);
          source.start(cursor);
          next.streamPending += 1;
          _streamSources.push(source);
          source.onended = () => {
            _streamSources = _streamSources.filter(item => item !== source);
            try { source.disconnect(); } catch (_) { }
            next.streamPending -= 1;
            if (next.streamDone && next.streamPending === 0) _finishItem(next, true);
          };
          cursor += samples.length / 24000;
          _streamNextTime = cursor;
          sawAudio = true;
        }
        // Advance synthesis while this PCM is still scheduled. The shared cursor
        // orders sentences, and isSpeaking includes the remaining audio sources.
        if (!sawAudio) throw new Error('tts stream produced empty audio');
        if (carry.length) throw new Error('tts stream ended with an incomplete PCM sample');
      } finally {
        try { await reader.cancel(); } finally { reader.releaseLock(); }
      }
    }).finally(() => {
      if (_streamController === controller) _streamController = null;
    });
  }
  function _settledAudio(next) {
    return _requestAudio(next).then(
      blob => ({ blob: blob, error: null }),
      error => ({ blob: null, error: error })
    );
  }
  function _primePrefetch() {
    if (_prefetch || _coolingDown() || !QUEUE.length) return;
    const item = QUEUE[0];
    if (item.opts && item.opts.live) return;
    _prefetch = { item: item, promise: _settledAudio(item) };
  }
  function _takeAudio(next) {
    if (_prefetch && _prefetch.item === next) {
      const promise = _prefetch.promise;
      _prefetch = null;
      return promise;
    }
    return _settledAudio(next);
  }
  function _drain() {
    if (playing || _coolingDown()) return;
    const next = QUEUE.shift();
    if (!next) return;
    playing = true;
    const generation = _generation;
    if (next.opts && next.opts.live) {
      next.streamPending = 0;
      next.streamDone = false;
      _requestStream(next).then(() => {
        if (generation !== _generation) return;
        next.streamDone = true;
        if (next.streamPending === 0) _finishItem(next, true);
        playing = false;
        _drain();
      }).catch(err => {
        if (generation !== _generation) return;
        _finishItem(next, false);
        const msg = err && err.message || String(err);
        // A partial stream must never be replayed or mistaken for completion.
        _tripUnavailable();
        playing = false;
        console.warn('[TTS] /api/tts/stream failed:', msg);
      });
      return;
    }
    _takeAudio(next).then(result => {
      if (generation !== _generation) return;
      if (result.error) throw result.error;
      const blob = result.blob;
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      _activeUrl = url;
      _audio = audio;
      let cleaned = false;
      const cleanup = (completed) => {
        if (cleaned) return;
        cleaned = true;
        if (_activeUrl === url) {
          URL.revokeObjectURL(url);
          _activeUrl = null;
        }
        if (_audio === audio) _audio = null;
        if (generation !== _generation) return;
        _finishItem(next, completed);
        playing = false;
        _drain();
      };
      audio.onended = () => cleanup(true);
      audio.onerror = () => cleanup(false);
      audio.play().catch(() => cleanup(false));
      // Synthesize the following sentence while this one is audible, so
      // sequential chunks do not inherit the full backend latency as silence.
      _primePrefetch();
    }).catch(err => {
      if (generation !== _generation) return;
      const msg = err && err.message || String(err);
      // Network errors (server down) → also cool down.
      if (/Failed to fetch|NetworkError|tts unavailable/i.test(msg)) {
        _tripUnavailable();
      } else {
        _finishItem(next, false);
      }
      console.warn('[TTS] /api/tts/speak failed:', msg);
      playing = false;
      _drain();
    });
  }
  return {
    // Live conversation (voice-live.js): audible or queued means "speaking" —
    // the VAD raises its threshold and speech over it is a barge-in.
    isSpeaking() { return !!playing || QUEUE.length > 0 || _streamSources.length > 0; },
    enqueue(text, opts = {}) {
      if (_coolingDown()) return Promise.resolve(false);
      if (!text || !String(text).trim()) return Promise.resolve(false);
      const parts = (typeof OmegaSpeechText !== 'undefined' && OmegaSpeechText)
        ? OmegaSpeechText.chunks(String(text), { maxChars: 280 })
        : [String(text).trim()];
      if (!parts.length) return Promise.resolve(false);
      const s = parts.join(' ');
      const now = Date.now();
      // Match the design queue's dedup semantics so callers don't
      // need to think about it.
      if (QUEUE.some(item => item && item.sourceText === s)) return Promise.resolve(false);
      if (lastText === s && now - lastAt < 1500) return Promise.resolve(false);
      lastText = s; lastAt = now;
      let resolveJob;
      const promise = new Promise(resolve => { resolveJob = resolve; });
      const job = { remaining: parts.length, resolve: resolveJob, promise, finished: false };
      _jobs.add(job);
      for (const part of parts) {
        QUEUE.push({ text: part, sourceText: s, opts: opts || {}, job, finished: false });
      }
      _drain();
      return promise;
    },
    cancel() {
      _generation += 1;
      _finishAllJobs(false);
      lastText = ''; lastAt = 0;
      QUEUE.length = 0;
      _prefetch = null;
      if (_audio) {
        _audio.onended = null;
        _audio.onerror = null;
        try { _audio.pause(); } catch (_) { }
        _audio = null;
      }
      if (_streamController) {
        try { _streamController.abort(); } catch (_) { }
        _streamController = null;
      }
      _streamSources.forEach(source => {
        try { source.onended = null; source.stop(); } catch (_) { }
        try { source.disconnect(); } catch (_) { }
      });
      _streamSources = [];
      _streamNextTime = 0;
      if (_activeUrl) {
        URL.revokeObjectURL(_activeUrl);
        _activeUrl = null;
      }
      playing = false;
      // Re-arm the backend cooldown — operator may have started the
      // runtime after a previous failure.
      _unavailableUntil = 0;
    },
    // Pill-ON re-arm (2026-07-12): turning voice ON must clear a prior
    // cooldown immediately — the operator's toggle IS the retry gesture.
    resetAvailability() { _unavailableUntil = 0; },
    isAvailable: true,
  };
})();
window.KokoroTTS = KokoroTTS;

function warmupKokoroTTS() {
  // Fire-and-forget: eager-load Kokoro before boot narration speaks.
  fetch('/api/tts/warmup', { method: 'POST' }).catch(() => { });
}
window.warmupKokoroTTS = warmupKokoroTTS;

// Route Voice.speak through runtime TTS (/api/tts/stream for live PCM,
// /api/tts/speak for ordinary replies → HTML Audio).
// Browser speechSynthesis stays disabled (SR/TTS=false above) so we
// never double-play with Windows SAPI / Edge Natural voices.
if (typeof Voice !== 'undefined' && Voice.speak) {
  Voice._origSpeak = Voice.speak;
  Voice.speak = function (text, opts) {
    const awaitPlayback = !!(opts && opts.awaitPlayback);
    const skipped = () => (awaitPlayback ? Promise.resolve(false) : undefined);
    if (!text) return skipped();
    // Master gate: HUD voice pill OFF → no Whisper input AND no Kokoro output.
    // opts.force cannot bypass the pill (settings still apply when pill is ON).
    //
    // ONE exception, opts.speakAlways (operator 2026-09-22): the boot greeting. The pill boots OFF
    // by default, which is not a considered choice, and that utterance is the audible proof that
    // the talker is resident on the GPU from boot — silence has to mean something is broken, not
    // that a default was left alone. An explicit tts_enabled:false IS a considered choice and still
    // silences it, two lines below.
    const speakAlways = !!(opts && opts.speakAlways);
    if (!speakAlways && typeof Voice.isListening === 'function' && !Voice.isListening()) return skipped();
    const voiceSettings = (typeof currentVoiceSettings === 'function')
      ? currentVoiceSettings() : null;
    if (voiceSettings && !voiceSettings.tts_enabled) return skipped();
    if (voiceSettings && voiceSettings.auto_speak === false
      && !(opts && opts.force)) return skipped();
    // Live conversation: sentence deltas were spoken as they arrived, so the
    // finished reply speaks only what is left (nothing twice, nothing missed).
    if (!(opts && opts.live) && typeof OmegaLive !== 'undefined' && OmegaLive && OmegaLive.alreadySpoken) {
      const verdict = OmegaLive.alreadySpoken(String(text), opts && opts.cycleId);
      if (verdict.skip) return skipped();
      if (verdict.remainder) text = verdict.remainder;
    }
    const ttsOpts = { ...(opts || {}) };
    if (awaitPlayback) {
      // Delivery acknowledgement needs a real playback end event. The finite
      // audio route reports that boundary; the streaming route reports when
      // bytes arrive, before its scheduled audio has finished.
      ttsOpts.live = false;
    } else if (!voiceSettings || voiceSettings.live_mode !== false) {
      ttsOpts.live = true;
    }
    if (voiceSettings) {
      if (voiceSettings.tts_voice != null) ttsOpts.voice = voiceSettings.tts_voice;
      if (voiceSettings.tts_speed != null) ttsOpts.speed = voiceSettings.tts_speed;
      if (voiceSettings.tts_pitch != null) ttsOpts.pitch = voiceSettings.tts_pitch;
    }
    const playback = KokoroTTS.enqueue(String(text), ttsOpts);
    return awaitPlayback ? playback : undefined;
  };
  Voice._kokoroEnqueue = (text, opts) => KokoroTTS.enqueue(text, opts);
  if (Voice.cancel) {
    const _origCancel = Voice.cancel;
    Voice.cancel = function () {
      KokoroTTS.cancel();
      try { _origCancel(); } catch (_) { }
    };
  } else {
    Voice.cancel = function () { KokoroTTS.cancel(); };
  }
}

// ── VOICE-3: Chrome TTS pause-bug workaround + watchdog ───────
// Chrome silently pauses speechSynthesis after ~15 s of a long
// utterance, after switching tabs, or sometimes for no visible
// reason — leaving the queue's `speaking` flag stuck and every
// subsequent reply silent. Workarounds:
//   (a) Every 5 s while speechSynthesis claims it's speaking, call
//       resume() — a no-op when actually speaking, the unstuck path
//       when paused.
//   (b) Stuck-watchdog: if speechSynthesis.speaking has been true
//       for more than 30 s with no progress, cancel() to drain the
//       queue + log it (single utterance shouldn't take that long).
if (typeof window !== 'undefined' && window.speechSynthesis) {
  const ss = window.speechSynthesis;
  let lastTickWasSpeaking = false;
  let speakingSince = 0;
  setInterval(() => {
    try {
      const now = Date.now();
      if (ss.speaking) {
        if (!lastTickWasSpeaking) speakingSince = now;
        lastTickWasSpeaking = true;
        if (ss.paused) ss.resume();
        if (now - speakingSince > 30000) {
          try { ss.cancel(); } catch (_) { }
          speakingSince = 0;
          console.warn('[Voice] TTS watchdog: cancelled stuck utterance');
        }
      } else {
        lastTickWasSpeaking = false;
        speakingSince = 0;
      }
    } catch (_) { }
  }, 5000);
}
})();
// SECTION:stt
(function () {
  'use strict';
// ── VOICE-4/STT-1: backend Whisper STT + diagnostics ───────────
// Preferred path is backend sensory input:
//   getUserMedia(audio) -> MediaRecorder full utterance blob
//   -> /api/stt/transcribe -> Whisper -> wake-command router.
// Web Speech remains a fallback only when MediaRecorder/backend STT
// is unavailable. Each backend window creates a fresh MediaRecorder
// and stops it before POSTing so the blob is a complete container,
// not an undecodable continuation fragment.
if (typeof Voice !== 'undefined') {
  // Web SpeechRecognition removed 2026-05-12 (operator directive:
  // backend Whisper is sole STT). The constant is kept as `null`
  // so any legacy reference fails fast instead of silently falling
  // back to browser STT.
  const SR = null;
  const BACKEND_STT_SUPPORTED = !!(navigator.mediaDevices &&
    navigator.mediaDevices.getUserMedia &&
    typeof MediaRecorder !== 'undefined');
  let _stt = null;
  let _audioStream = null;
  let _wantListening = false;
  let _lastResultAt = 0;
  let _usingBackendSTT = false;
  let _backendFailed = false;

  // Wake-word regex tolerates the most common Web Speech
  // mistranscriptions of "omega" we see in the wild.
  const WAKE_RE = /^(?:omega|amiga|amigo|i'?m a|ohmega|home a|olmega|aamiga|imega|im a|oma|amiya)\b\s*[,.:;-]?\s*/i;

  function _flashPill(text) {
    const pill = document.getElementById('voice');
    if (!pill) return;
    const orig = pill.dataset._orig || (pill.dataset._orig = pill.innerHTML);
    pill.innerHTML = '<span class="voice-dot"></span>' + text;
    clearTimeout(pill._flashT);
    pill._flashT = setTimeout(() => {
      pill.innerHTML = pill.dataset._orig;
    }, 2200);
  }

  function _routeWakeCommand(transcript, activeRegex) {
    // Strip the wake word + any leading punctuation
    const stripped = transcript.replace(activeRegex || WAKE_RE, '').trim();
    if (!stripped) return;
    // Lightweight intent routing — same shape as the design's
    // dispatch, but image/upload commands flow through OmegaUI.
    const lower = stripped.toLowerCase();
    console.log('[Voice] route:', JSON.stringify(stripped));

    // Remaining panel open command names.
    const PANEL_NAMES = ['help', 'settings'];
    const PANEL_ALIASES = {
      'preferences': 'settings', 'profile': 'settings',
    };
    const ALL_NAMES = PANEL_NAMES.concat(Object.keys(PANEL_ALIASES));
    // Build alternation: longer aliases first (so "build mode" wins
    // over "build"), then panel names.
    ALL_NAMES.sort((a, b) => b.length - a.length);
    const panelRe = new RegExp(
      '^\\s*(?:(?:open|show|switch(?:\\s+to)?|go(?:\\s+to)?|bring(?:\\s+up)?|take\\s+me\\s+(?:to|over\\s+to)|see|view|let\\s+me\\s+see)\\s+)?'
      + '(?:(?:the|me|us|that)\\s+)?'
      + '(' + ALL_NAMES.map(n => n.replace(/ /g, '\\s+')).join('|') + ')'
      + '\\b',
      'i'
    );
    const panelM = lower.match(panelRe);
    if (panelM) {
      const heard = panelM[1].toLowerCase().replace(/\s+/g, ' ');
      const id = PANEL_ALIASES[heard] || heard;
      document.querySelectorAll('.pill').forEach(b =>
        b.classList.toggle('active', b.dataset.panel === id));
      if (typeof openPanel === 'function') openPanel(id);
      return;
    }
    if (/^(close|exit|done|stand\s*down|never\s*mind|cancel)\b/.test(lower)) {
      if (typeof closePanel === 'function') closePanel();
      return;
    }
    // Vision commands
    if (/^(scan\s+me|who\s+am\s+i)\b/.test(lower)) {
      if (typeof Vision !== 'undefined') {
        Vision.identify().then(r => {
          if (r && r.name) Vision.welcomeBack(r.name);
        }).catch(() => { });
      }
      return;
    }
    const am = stripped.match(/^remember\s+me\s+as\s+(.+)$/i);
    if (am && typeof Vision !== 'undefined') {
      Vision.anchor(am[1].trim().replace(/[.!?]$/, '')).catch(() => { });
      return;
    }
    // Otherwise ship through ChatBridge (which routes to /api/chat or
    // to OmegaUI.triggerUploadPicker via the upload intercept below).
    if (typeof appendLine === 'function') appendLine('user', stripped);
    if (typeof ChatBridge !== 'undefined') {
      ChatBridge.send({ kind: 'chat.user_message', source: 'user', body: stripped });
    }
  }

  async function _blobIsSilent(blob, rmsThreshold) {
    const threshold = (typeof rmsThreshold === 'number') ? rmsThreshold : 0.008;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return false;
      const ctx = new Ctx();
      try {
        const audio = await ctx.decodeAudioData(await blob.arrayBuffer());
        const ch = audio.getChannelData(0);
        if (!ch || !ch.length) return true;
        let sum = 0;
        const step = 4;
        for (let i = 0; i < ch.length; i += step) sum += ch[i] * ch[i];
        const rms = Math.sqrt(sum / Math.max(1, Math.ceil(ch.length / step)));
        return rms < threshold;
      } finally {
        try { await ctx.close(); } catch (_) { }
      }
    } catch (_) {
      return false;
    }
  }

  function _audioBufferToWavBlob(audio, targetSampleRate) {
    const sampleRate = targetSampleRate || 16000;
    const sourceRate = audio.sampleRate || sampleRate;
    const channels = Math.max(1, audio.numberOfChannels || 1);
    const sampleCount = Math.max(1, Math.floor((audio.duration || (audio.length / sourceRate)) * sampleRate));
    const pcm = new Int16Array(sampleCount);

    for (let i = 0; i < sampleCount; i++) {
      const srcPos = i * sourceRate / sampleRate;
      const srcIdx = Math.floor(srcPos);
      const frac = srcPos - srcIdx;
      let sample = 0;
      for (let ch = 0; ch < channels; ch++) {
        const data = audio.getChannelData(ch);
        const a = data[Math.min(srcIdx, data.length - 1)] || 0;
        const b = data[Math.min(srcIdx + 1, data.length - 1)] || a;
        sample += a + (b - a) * frac;
      }
      sample = Math.max(-1, Math.min(1, sample / channels));
      pcm[i] = sample < 0 ? sample * 0x8000 : sample * 0x7fff;
    }

    const buffer = new ArrayBuffer(44 + pcm.length * 2);
    const view = new DataView(buffer);
    const writeString = (offset, value) => {
      for (let i = 0; i < value.length; i++) {
        view.setUint8(offset + i, value.charCodeAt(i));
      }
    };
    writeString(0, 'RIFF');
    view.setUint32(4, 36 + pcm.length * 2, true);
    writeString(8, 'WAVE');
    writeString(12, 'fmt ');
    view.setUint32(16, 16, true);
    view.setUint16(20, 1, true);
    view.setUint16(22, 1, true);
    view.setUint32(24, sampleRate, true);
    view.setUint32(28, sampleRate * 2, true);
    view.setUint16(32, 2, true);
    view.setUint16(34, 16, true);
    writeString(36, 'data');
    view.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) {
      view.setInt16(44 + i * 2, pcm[i], true);
    }
    return new Blob([buffer], { type: 'audio/wav' });
  }

  async function _toWhisperUploadBlob(blob) {
    if ((blob.type || '').includes('wav')) return blob;
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return blob;
      const ctx = new Ctx();
      try {
        const audio = await ctx.decodeAudioData(await blob.arrayBuffer());
        const wav = _audioBufferToWavBlob(audio, 16000);
        return wav;
      } finally {
        try { await ctx.close(); } catch (_) { }
      }
    } catch (_) {
      return blob;
    }
  }

  async function _postBackendAudio(blob) {
    if (!blob || blob.size < 512) return;
    const settings = (typeof currentVoiceSettings === 'function')
      ? currentVoiceSettings() : {};
    
    if (settings.voice_activity_detection !== false) {
      const silent = await _blobIsSilent(blob);
      if (silent) return;
    }
    
    const uploadBlob = await _toWhisperUploadBlob(blob);
    const fd = new FormData();
    const ext = (uploadBlob.type || '').includes('ogg') ? 'ogg'
      : (uploadBlob.type || '').includes('wav') ? 'wav' : 'webm';
    fd.append('audio', uploadBlob, 'omega-stt.' + ext);
    fd.append('language', settings.stt_language || 'auto');
    if (settings.stt_task) fd.append('task', settings.stt_task);
    const r = await fetch('/api/stt/transcribe', {
      method: 'POST',
      body: fd,
    });
    const rawResponse = await r.text();
    let data = {};
    try { data = rawResponse ? JSON.parse(rawResponse) : {}; } catch (_) { data = { error: rawResponse }; }
    if (!r.ok) throw new Error('http ' + r.status + ((data && (data.detail || data.error)) ? ': ' + (data.detail || data.error) : ''));
    const transcript = ((data && data.text) || '').trim();
    if (!transcript) return;
    _lastResultAt = Date.now();
    _flashPill('"' + transcript.slice(0, 28) + '"');
    const wakeEnabled = !!settings.wake_word_enabled;
    if (wakeEnabled) {
      let wakeRegex = WAKE_RE;
      if (settings.wake_word) {
        // Create a safe regex from the wake word, allowing common misspellings if it's "omega"
        if (settings.wake_word.toLowerCase().includes('omega')) {
          wakeRegex = WAKE_RE;
        } else {
          // Escape regex characters in custom wake word
          const escaped = settings.wake_word.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
          wakeRegex = new RegExp('^' + escaped + '\\b\\s*[,.:;-]?\\s*', 'i');
        }
      }
      if (wakeRegex.test(transcript)) {
        // Strip the wake word using the active regex
        const stripped = transcript.replace(wakeRegex, '').trim();
        if (stripped) {
          _routeWakeCommand(transcript, wakeRegex);
        } else {
          // Just the wake word was heard, maybe ping back? For now, ignore empty.
        }
      }
      return;
    }
    const stripped = transcript.replace(WAKE_RE, '').trim() || transcript;
    if (typeof appendLine === 'function') appendLine('user', stripped);
    if (typeof ChatBridge !== 'undefined') {
      ChatBridge.send({
        kind: 'chat.user_message',
        source: 'user',
        body: stripped,
      });
    }
  }

  async function _startBackendWindow() {
    if (!_wantListening || !_usingBackendSTT || window._serverShutdown) return;
    try {
      if (!_audioStream) {
        const settings = (typeof currentVoiceSettings === 'function')
          ? currentVoiceSettings() : {};
        const nc = settings.noise_cancellation !== false;
        _audioStream = await navigator.mediaDevices.getUserMedia({ 
          audio: {
            echoCancellation: nc,
            noiseSuppression: nc,
            autoGainControl: true
          } 
        });
      }
      const chunks = [];
      const mime = MediaRecorder.isTypeSupported &&
        MediaRecorder.isTypeSupported('audio/webm;codecs=opus')
        ? 'audio/webm;codecs=opus' : '';
      const rec = mime
        ? new MediaRecorder(_audioStream, { mimeType: mime })
        : new MediaRecorder(_audioStream);
      _stt = rec;
      rec.ondataavailable = ev => {
        if (ev.data && ev.data.size) chunks.push(ev.data);
      };
      rec.onerror = ev => {
        console.warn('[Voice] backend recorder error:', ev && ev.error);
      };
      rec.onstop = () => {
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/webm' });
        _postBackendAudio(blob).catch(err => {
          // Backend (runtime Whisper via /transcribe) failed.
          // Per operator directive 2026-05-12: NO web SpeechRecognition
          // fallback. Backend Whisper is the sole STT path; if it's down, voice
          // listening goes dormant and the operator must restart the
          // runtime. Two-engine setup was producing audio doubling and
          // the constant /transcribe hammer visible in lite's log.
          console.warn('[Voice] /api/stt/transcribe failed:', err.message);
          _backendFailed = true;
          _wantListening = false;
          Telemetry.update({
            system: {
              voice: {
                state: 'error',
                supported: true,
                last_event: 'backend stt unavailable — listening stopped',
              }
            }
          });
        }).finally(() => {
          if (_wantListening && _usingBackendSTT && !_backendFailed && !window._serverShutdown) {
            setTimeout(_startBackendWindow, 80);
          }
        });
      };
      rec.start();
      _lastResultAt = Date.now();
      Telemetry.update({
        system: {
          voice: {
            state: 'listening',
            supported: true,
            last_event: 'backend whisper listening',
          }
        }
      });
      setTimeout(() => {
        if (_wantListening && _usingBackendSTT && rec.state === 'recording') {
          try { rec.stop(); } catch (_) { }
        }
      }, 3600);
    } catch (e) {
      const code = (e && (e.name || e.message)) || 'unknown';
      if (code === 'NotAllowedError' || /not-allowed/i.test(code)) {
        _wantListening = false;
        const pill = document.getElementById('voice');
        if (pill) pill.classList.remove('active');
        Telemetry.update({
          system: {
            voice: {
              state: 'error',
              supported: true,
              last_event: 'not-allowed',
            }
          }
        });
        console.warn('[Voice] mic permission denied');
        return;
      }
      // getUserMedia or MediaRecorder failed for a non-permission
      // reason. Per operator directive: NO web SpeechRecognition
      // fallback. Latch unavailable + go dormant; operator can
      // re-enable after diagnosing the audio device.
      _backendFailed = true;
      _wantListening = false;
      Telemetry.update({
        system: {
          voice: {
            state: 'error',
            supported: true,
            last_event: 'audio capture failed (' + code + ')',
          }
        }
      });
      console.warn('[Voice] audio capture failed:', code);
    }
  }

  // ── Web SpeechRecognition fallback REMOVED 2026-05-12 ────────
  // Previously this module had `_newRecognition` and
  // `_startBrowserSpeechFallback` as a fallback path when backend
  // /transcribe was unavailable. Operator directive 2026-05-12:
  // remove all web TTS/STT fallbacks — backend Whisper
  // + Kokoro via runtime) is the sole sensory I/O. The dual-engine
  // setup was producing browser-side recognition firing in parallel
  // with server-side capture, audio doubling on TTS, and the
  // hammering of /transcribe visible in the runtime log.
  //
  // When backend STT is down, voice listening goes dormant (state='error',
  // _wantListening=false). Operator must restart the runtime and
  // toggle the voice pill again.

  // Watchdog: if we INTEND to be listening but no result has come
  // in for 90 s, the recording has likely silently died. Restart
  // the backend window.
  setInterval(() => {
    if (!_wantListening || !_usingBackendSTT || _backendFailed || window._serverShutdown) return;
    // Live capture is silent by design between utterances — no result is not a death.
    if (typeof OmegaLive !== 'undefined' && OmegaLive && OmegaLive.isRunning && OmegaLive.isRunning()) return;
    if (Date.now() - _lastResultAt < 90000) return;
    if (_usingBackendSTT && !_backendFailed) {
      try { _stt && _stt.state === 'recording' && _stt.stop(); } catch (_) { }
      setTimeout(_startBackendWindow, 200);
    }
    // else: _backendFailed latched — let the operator re-enable manually.
  }, 30000);

  // ── Live conversation capture (2026-09-05) ─────────────────────────
  // Raw PCM + endpointing (voice-live.js) instead of fixed 3.6 s windows:
  // an utterance ships the moment the operator stops talking, and speech
  // over Kokoro is a barge-in. Falls back to the window path only when raw
  // capture cannot start.
  async function _startLiveCapture() {
    if (!_wantListening || !_usingBackendSTT || window._serverShutdown) return false;
    if (typeof OmegaLive === 'undefined' || !OmegaLive || !OmegaLive.supported()) return false;
    try {
      if (!_audioStream) {
        const settings = (typeof currentVoiceSettings === 'function')
          ? currentVoiceSettings() : {};
        const nc = settings.noise_cancellation !== false;
        _audioStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: nc, noiseSuppression: nc, autoGainControl: true }
        });
      }
    } catch (e) {
      const code = (e && (e.name || e.message)) || 'unknown';
      if (code === 'NotAllowedError' || /not-allowed/i.test(code)) {
        _wantListening = false;
        const pill = document.getElementById('voice');
        if (pill) pill.classList.remove('active');
        Telemetry.update({ system: { voice: { state: 'error', supported: true, last_event: 'not-allowed' } } });
        console.warn('[Voice] mic permission denied');
        return true;  // handled: do not fall back into a second permission prompt
      }
      return false;
    }
    const ok = OmegaLive.start(_audioStream, {
      isSpeaking: () => (typeof KokoroTTS !== 'undefined' && KokoroTTS && KokoroTTS.isSpeaking)
        ? KokoroTTS.isSpeaking() : false,
      onUtterance: blob => {
        _lastResultAt = Date.now();
        _postBackendAudio(blob).catch(err => {
          console.warn('[Voice] /api/stt/transcribe failed:', err.message);
          _backendFailed = true;
          _wantListening = false;
          try { OmegaLive.stop(); } catch (_) { }
          Telemetry.update({
            system: { voice: { state: 'error', supported: true, last_event: 'backend stt unavailable — listening stopped' } }
          });
        });
      },
    });
    if (ok) {
      _lastResultAt = Date.now();
      Telemetry.update({ system: { voice: { state: 'listening', supported: true, last_event: 'live conversation listening' } } });
    }
    return ok;
  };

  Voice.enable = function () {
    if (!BACKEND_STT_SUPPORTED) return false;
    if (_wantListening) return true;
    _wantListening = true;
    _backendFailed = false;
    _usingBackendSTT = true;
    // Pill ON is the operator's retry gesture (2026-07-12): clear any TTS
    // cooldown from a cold-start failure so the next reply actually speaks —
    // previously only pill OFF re-armed, leaving voice-ON sessions mute.
    try {
      if (typeof KokoroTTS !== 'undefined' && KokoroTTS && KokoroTTS.resetAvailability) {
        KokoroTTS.resetAvailability();
      }
    } catch (_) { }
    // Warm Kokoro while mic arms so the first reply's TTS is not cold-start lag.
    try {
      if (typeof warmupKokoroTTS === 'function') warmupKokoroTTS();
    } catch (_) { }
    const liveSettings = (typeof currentVoiceSettings === 'function') ? currentVoiceSettings() : {};
    if (liveSettings && liveSettings.live_mode === false) {
      _startBackendWindow();
    } else {
      _startLiveCapture().then(ok => { if (!ok && _wantListening) _startBackendWindow(); });
    }
    return true;
  };
  Voice.disable = function () {
    _wantListening = false;
    try { if (typeof OmegaLive !== 'undefined' && OmegaLive && OmegaLive.stop) OmegaLive.stop(); } catch (_) { }
    try {
      if (_stt && _stt.state === 'recording') _stt.stop();
      else if (_stt && _stt.stop) _stt.stop();
    } catch (_) { }
    if (_audioStream) {
      try { _audioStream.getTracks().forEach(t => t.stop()); } catch (_) { }
    }
    _audioStream = null;
    _stt = null;
    // Hard reset BOTH engines — Kokoro is the live TTS path; speechSynthesis
    // is legacy. Voice OFF must silence everything immediately.
    try { if (typeof Voice.cancel === 'function') Voice.cancel(); } catch (_) { }
    try {
      if (typeof KokoroTTS !== 'undefined' && KokoroTTS && KokoroTTS.cancel) {
        KokoroTTS.cancel();
      }
    } catch (_) { }
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (_) { }
  };
  Voice.isListening = function () { return !!_wantListening; };
  Voice.isSupported = function () { return BACKEND_STT_SUPPORTED; };
  Voice.reset = function () {
    try { window.speechSynthesis && window.speechSynthesis.cancel(); } catch (_) { }
    Voice.disable();
    setTimeout(() => Voice.enable(), 200);
  };
}
})();
// SECTION:wake
(function () {
  'use strict';
// ── VOICE-2: Voice pill is the sole on/off for Whisper + Kokoro ─
// Do NOT auto-enable on first gesture — that fought the operator's
// OFF toggle and left mic/TTS live when the pill looked inactive.
// Annotate the pill so wake-word usage is discoverable once they turn it on.
function _annotateVoicePill() {
  const pill = document.getElementById('voice');
  if (!pill) return;
  pill.title = 'Voice ON: Parakeet input + Qwen speech. Speak to interrupt.';
  if (!pill.querySelector('.wake-hint')) {
    const hint = document.createElement('em');
    hint.className = 'wake-hint';
    hint.textContent = ' · live conversation';
    hint.style.cssText = 'font-style:normal;color:var(--whisper);' +
      'margin-left:4px;font-size:.85em;letter-spacing:.04em';
    pill.appendChild(hint);
  }
}
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _annotateVoicePill);
} else {
  _annotateVoicePill();
}

// Chat reply TTS is kicked from renderChatResult (same turn as the bubble
// paint) so Kokoro synthesis starts immediately — not after a lagged
// omega:chat-rendered listener. Image/tool/upload intercepts stay below.
if (typeof ChatBridge !== 'undefined' && ChatBridge.send) {
  const _patched = ChatBridge.send;
  ChatBridge.send = function (envelope) {
    const env = Object.assign({
      kind: 'chat.user_message', source: 'user',
      cycle_id: null, ts: new Date().toISOString(),
    }, envelope || {});
    const text = (env.body || '').toString();

    // WIRE-6: Operator-driven tool synthesis. Phrasings like
    //   "create a tool that ..."
    //   "build me a tool to ..."
    //   "make a tool for ..."
    // route to /api/omega/request_tool which creates a curiosity
    // entry the autonomous engine will pick up on its next sweep.
    const toolRe = /^(?:create|build|make|design)\s+(?:me\s+)?(?:an?\s+|the\s+)?tool\s+(?:that|to|for|which)\s+(.+)$/i;
    const tm = text.match(toolRe);
    if (tm && tm[1] && tm[1].trim().length > 3) {
      if (typeof appendLine === 'function') appendLine('user', text);
      window.OmegaUI.requestTool(tm[1].trim());
      return;
    }

    // Upload trigger by voice ("Omega upload" / "upload some files")
    if (/^upload(\s+(some|a)\s+)?(files?|docs?|pictures?|photos?|images?)?\.?$/i.test(text)) {
      if (typeof appendLine === 'function') appendLine('user', text);
      window.OmegaUI.triggerUploadPicker();
      return;
    }

    _patched.call(ChatBridge, env);
  };
}
})();

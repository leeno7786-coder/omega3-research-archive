// Live conversation loop (hud/ui-src/core/voice-live.js) — operator 2026-09-05:
// "i want it all tts/stt run so we need a realtime convo loop setup as well".
// Depends on: KokoroTTS, Voice, ChatBridge, Telemetry, currentVoiceSettings
//
// Three things, one module:
//   LISTEN  — continuous capture with endpointing. Raw PCM off the mic (Web
//             Audio), downsampled to 16 kHz, an RMS voice-activity gate with an
//             adaptive noise floor: ~120 ms of speech opens an utterance (with
//             320 ms of pre-roll so the first syllable is not clipped), 700 ms
//             of silence closes it, and the utterance ships as one WAV to
//             /api/stt/transcribe (Parakeet, warm in ~0.3 s). No fixed windows,
//             no chopped words.
//   SPEAK   — native Qwen PCM streaming. chat.reply_delta text accumulates and
//             every completed sentence is enqueued the moment it is complete,
//             so speech starts before the reply is finished. The final
//             chat.reply is the authority: whatever it carries beyond what was
//             already spoken is spoken then, nothing twice.
//   BARGE-IN — speech detected while Qwen is audible cancels playback and
//             sends chat.interrupt for the cycle being answered; the lane
//             stops between passes and the bubble records "(stopped)".
//
// Fail-honest: if raw capture cannot start (no AudioContext), start() returns
// false and voice-runtime falls back to its fixed-window capture.
const OmegaLive = (() => {
  'use strict';
  const CFG = {
    targetRate: 16000,
    frameMs: 30,
    onsetFrames: 4,        // ~120 ms of speech opens an utterance
    bargeInFrames: 7,      // ~210 ms of speech over playback counts as barge-in
    hangoverMs: 700,       // silence that closes an utterance
    minUtteranceMs: 300,   // shorter is a click, not a word
    maxUtteranceMs: 20000, // hard stop
    preRollMs: 320,        // audio kept before onset
    floor: 0.012,          // absolute RMS floor
    noiseMult: 2.5,        // onset threshold = max(floor, noise * mult)
    speakingMult: 2.0,     // extra margin while speech is audible (echo)
    noiseAdapt: 0.05,      // EMA weight for the noise floor while idle
  };
  const state = {
    running: false, inSpeech: false, noise: 0.004, speechFrames: 0, silenceMs: 0,
    utterMs: 0, utterances: 0, bargeIns: 0, lastRms: 0, dropped: 0,
  };
  let _ctx = null, _source = null, _proc = null, _hooks = {};
  let _carry = new Float32Array(0);
  let _preRoll = [];          // Float32Array frames kept before onset
  let _utter = [];            // Float32Array frames of the open utterance
  let _frameLen = Math.round(CFG.targetRate * CFG.frameMs / 1000);
  let _preRollFrames = Math.ceil(CFG.preRollMs / CFG.frameMs);

  function supported() {
    return typeof window !== 'undefined'
      && !!(window.AudioContext || window.webkitAudioContext)
      && typeof Blob !== 'undefined';
  }

  // ── PCM helpers ───────────────────────────────────────────────────────
  function downsample(input, fromRate, toRate) {
    if (fromRate === toRate) return Float32Array.from(input);
    const ratio = fromRate / toRate;
    const outLen = Math.floor(input.length / ratio);
    const out = new Float32Array(outLen);
    for (let i = 0; i < outLen; i++) {
      const start = Math.floor(i * ratio);
      const end = Math.min(input.length, Math.floor((i + 1) * ratio));
      let sum = 0, n = 0;
      for (let j = start; j < end; j++) { sum += input[j]; n++; }
      out[i] = n ? sum / n : 0;
    }
    return out;
  }
  function rmsOf(frame) {
    let sum = 0;
    for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
    return Math.sqrt(sum / Math.max(1, frame.length));
  }
  function framesToWav(frames, rate) {
    let total = 0;
    for (const f of frames) total += f.length;
    const pcm = new Int16Array(total);
    let o = 0;
    for (const f of frames) {
      for (let i = 0; i < f.length; i++) {
        const s = Math.max(-1, Math.min(1, f[i]));
        pcm[o++] = s < 0 ? s * 0x8000 : s * 0x7fff;
      }
    }
    const buffer = new ArrayBuffer(44 + pcm.length * 2);
    const view = new DataView(buffer);
    const str = (off, v) => { for (let i = 0; i < v.length; i++) view.setUint8(off + i, v.charCodeAt(i)); };
    str(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); str(8, 'WAVE');
    str(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    str(36, 'data'); view.setUint32(40, pcm.length * 2, true);
    for (let i = 0; i < pcm.length; i++) view.setInt16(44 + i * 2, pcm[i], true);
    return new Blob([buffer], { type: 'audio/wav' });
  }

  // ── the endpointing state machine (pure: fed frames, emits utterances) ─
  function speakingNow() {
    try { return !!(_hooks.isSpeaking && _hooks.isSpeaking()); } catch (_) { return false; }
  }
  function processFrame(frame) {
    const rms = rmsOf(frame);
    state.lastRms = rms;
    const speaking = speakingNow();
    const thr = Math.max(CFG.floor, state.noise * CFG.noiseMult) * (speaking ? CFG.speakingMult : 1);
    if (!state.inSpeech) {
      _preRoll.push(frame);
      if (_preRoll.length > _preRollFrames) _preRoll.shift();
      if (rms > thr) {
        state.speechFrames += 1;
      } else {
        state.speechFrames = 0;
        state.noise = state.noise * (1 - CFG.noiseAdapt) + rms * CFG.noiseAdapt;
      }
      const need = speaking ? CFG.bargeInFrames : CFG.onsetFrames;
      if (state.speechFrames >= need) {
        state.inSpeech = true;
        state.silenceMs = 0;
        // voiced time counts from the first onset frame; the pre-roll is kept in
        // the audio but never counted, so a click can not pass as a word.
        state.utterMs = state.speechFrames * CFG.frameMs;
        _utter = _preRoll.slice();
        _preRoll = [];
        if (speaking) bargeIn();
        emit('speech_start');
      }
      return;
    }
    _utter.push(frame);
    state.utterMs += CFG.frameMs;
    if (rms > thr) state.silenceMs = 0; else state.silenceMs += CFG.frameMs;
    if (state.silenceMs >= CFG.hangoverMs || state.utterMs >= CFG.maxUtteranceMs) {
      const voiced = state.utterMs - Math.min(state.silenceMs, state.utterMs);
      const frames = _utter;
      _utter = [];
      state.inSpeech = false;
      state.speechFrames = 0;
      state.silenceMs = 0;
      state.utterMs = 0;
      if (voiced >= CFG.minUtteranceMs) {
        state.utterances += 1;
        const blob = framesToWav(frames, CFG.targetRate);
        emit('utterance', { ms: voiced, bytes: blob.size });
        try { if (_hooks.onUtterance) _hooks.onUtterance(blob, { ms: voiced }); } catch (e) { console.warn('[Live] onUtterance failed:', e && e.message); }
      } else {
        state.dropped += 1;
      }
    }
  }
  function feed(samples, rate) {
    const mono = downsample(samples, rate || CFG.targetRate, CFG.targetRate);
    const joined = new Float32Array(_carry.length + mono.length);
    joined.set(_carry, 0);
    joined.set(mono, _carry.length);
    let offset = 0;
    while (joined.length - offset >= _frameLen) {
      processFrame(joined.subarray(offset, offset + _frameLen));
      offset += _frameLen;
    }
    _carry = joined.slice(offset);
  }

  // ── capture plumbing ──────────────────────────────────────────────────
  function start(stream, hooks) {
    if (state.running) return true;
    if (!supported() || !stream) return false;
    _hooks = hooks || {};
    try {
      const Ctx = window.AudioContext || window.webkitAudioContext;
      _ctx = new Ctx();
      _source = _ctx.createMediaStreamSource(stream);
      // ScriptProcessorNode: deprecated but universal; the worklet variant needs
      // a module URL the single-file HUD cannot serve. 4096 frames ≈ 85 ms @ 48 k.
      _proc = _ctx.createScriptProcessor(4096, 1, 1);
      _proc.onaudioprocess = ev => {
        if (!state.running) return;
        try { feed(ev.inputBuffer.getChannelData(0), _ctx.sampleRate); } catch (e) { console.warn('[Live] frame failed:', e && e.message); }
      };
      _source.connect(_proc);
      _proc.connect(_ctx.destination);  // keeps the node alive; it outputs silence
      state.running = true;
      reset();
      emit('listening');
      return true;
    } catch (e) {
      console.warn('[Live] capture start failed:', e && e.message);
      stop();
      return false;
    }
  }
  function stop() {
    state.running = false;
    try { if (_proc) { _proc.onaudioprocess = null; _proc.disconnect(); } } catch (_) { }
    try { if (_source) _source.disconnect(); } catch (_) { }
    try { if (_ctx) _ctx.close(); } catch (_) { }
    _proc = null; _source = null; _ctx = null;
    reset();
  }
  function reset() {
    state.inSpeech = false; state.speechFrames = 0; state.silenceMs = 0; state.utterMs = 0;
    _carry = new Float32Array(0); _preRoll = []; _utter = [];
  }
  function emit(event, extra) {
    try {
      if (typeof Telemetry !== 'undefined' && Telemetry && Telemetry.update) {
        Telemetry.update({ system: { voice: { live: Object.assign({ event: event, utterances: state.utterances, barge_ins: state.bargeIns }, extra || {}) } } });
      }
    } catch (_) { }
  }

  // ── speaking side: sentence streaming + the final-reply reconciliation ─
  const spoken = { cycle: '', text: '', upTo: 0, done: false };
  const interruptedCycles = new Set();
  const norm = s => String(s || '').replace(/\s+/g, ' ').trim();
  function lastSentenceEnd(s) {
    // index just past the last sentence terminator that is followed by space/end
    const re = /[.!?]["')\]]*(?=\s|$)/g;
    let end = 0, m;
    while ((m = re.exec(s)) !== null) end = m.index + m[0].length;
    return end;
  }
  function speakChunk(text) {
    const chunk = norm(text);
    if (!chunk) return;
    try { if (typeof Voice !== 'undefined' && Voice.speak) Voice.speak(chunk, { live: true }); } catch (e) { console.warn('[Live] speak failed:', e && e.message); }
  }
  function flushSentences(final) {
    const pending = spoken.text.slice(spoken.upTo);
    const cut = final ? pending.length : lastSentenceEnd(pending);
    if (cut <= 0) return;
    speakChunk(pending.slice(0, cut));
    spoken.upTo += cut;
  }
  function onDelta(env) {
    const cycle = String(env.cycle_id || '');
    if (!cycle || interruptedCycles.has(cycle)) return;
    if (spoken.cycle !== cycle || Number(env.seq || 0) === 0) {
      if (spoken.cycle !== cycle && spoken.cycle && !spoken.done) flushSentences(true);
      spoken.cycle = cycle; spoken.text = ''; spoken.upTo = 0; spoken.done = false;
    }
    spoken.text += String(env.text || '');
    flushSentences(false);
  }
  function onReply(env) {
    const cycle = String(env.cycle_id || '');
    if (!cycle || spoken.cycle !== cycle) return;   // nothing streamed: chat-feed speaks it whole
    const delivery = env.delivery || {};
    if (interruptedCycles.has(cycle)) {
      spoken.text = String(env.text || '');
      spoken.upTo = spoken.text.length;
      spoken.done = true;
      return;
    }
    if (delivery.interrupted) { spoken.done = true; return; }
    // The final text is the authority. Keep what was streamed as the spoken
    // prefix; chat-feed's Voice.speak(fullText) will ask alreadySpoken() and
    // speak only the remainder.
    const full = String(env.text || '');
    if (norm(full).startsWith(norm(spoken.text.slice(0, spoken.upTo)))) spoken.text = full;
    spoken.done = true;
  }
  // Called by Voice.speak for every NON-live request: decide whether the text was
  // already spoken via deltas ({skip:true}) or partly ({remainder}).
  function alreadySpoken(text, cycle) {
    if (cycle && interruptedCycles.has(String(cycle))) return { skip: true };
    if (cycle && String(cycle) !== spoken.cycle) return { skip: false };
    if (!spoken.cycle || spoken.upTo <= 0) return { skip: false };
    const t = norm(text);
    const s = norm(spoken.text.slice(0, spoken.upTo));
    if (!s || !t) return { skip: false };
    if (t === s) return { skip: true };
    if (t.startsWith(s)) {
      spoken.upTo = spoken.text.length;
      return { skip: false, remainder: t.slice(s.length).trim() };
    }
    return { skip: false };
  }
  function bargeIn() {
    state.bargeIns += 1;
    try { if (typeof KokoroTTS !== 'undefined' && KokoroTTS.cancel) KokoroTTS.cancel(); } catch (_) { }
    const cycle = spoken.cycle;
    if (cycle) interruptedCycles.add(cycle);
    if (cycle && !spoken.done) {
      spoken.done = true;
      spoken.upTo = spoken.text.length;
      sendInterrupt(cycle);
    }
    emit('barge_in', { cycle_id: cycle });
  }
  function sendInterrupt(cycle) {
    try {
      if (typeof ChatBridge !== 'undefined' && ChatBridge.__socketOpen && ChatBridge.__socketOpen()) {
        ChatBridge.__sendRaw({ kind: 'chat.interrupt', cycle_id: cycle });
        return;
      }
    } catch (_) { }
    try {
      fetch('/api/chat/barge-in', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ cycle_id: cycle }),
      }).catch(() => { });
    } catch (_) { }
  }
  if (typeof ChatBridge !== 'undefined' && ChatBridge.onMessage) {
    ChatBridge.onMessage(env => {
      if (!env || typeof env !== 'object') return;
      if (env.kind === 'chat.reply_delta') onDelta(env);
      else if (env.kind === 'chat.reply') onReply(env);
    });
  }

  return {
    CFG, state, supported, start, stop, feed, processFrame,
    isRunning() { return !!state.running; },
    onDelta, onReply, alreadySpoken, bargeIn, sendInterrupt, framesToWav, lastSentenceEnd,
    _reset() { reset(); interruptedCycles.clear(); spoken.cycle = ''; spoken.text = ''; spoken.upTo = 0; spoken.done = false; state.utterances = 0; state.bargeIns = 0; state.dropped = 0; state.noise = 0.004; },
  };
})();
window.OmegaLive = OmegaLive;

// Runtime behaviour of hud/ui-src/core/voice-live.js in a Node VM:
// endpointing on synthetic PCM, sentence-streamed speech, final-reply
// reconciliation, and barge-in. Run: node tests/hud/test_voice_live.mjs
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(new URL('../../hud/ui-src/core/voice-live.js', import.meta.url), 'utf8');

function loadLive({ speaking = () => false, socketOpen = true } = {}) {
  const spoken = [], interrupts = [], utterances = [], requests = [];
  const cancels = { n: 0 };
  const handlers = new Set();
  const fake = {
    sampleRate: 16000, destination: {}, proc: null,
    createMediaStreamSource() { return { connect() { }, disconnect() { } }; },
    createScriptProcessor() { fake.proc = { onaudioprocess: null, connect() { }, disconnect() { } }; return fake.proc; },
    close() { },
  };
  const context = {
    console,
    Blob: class { constructor(parts, opts) { this.size = parts.reduce((n, p) => n + (p.byteLength || 0), 0); this.type = opts && opts.type; } },
    Telemetry: { update() { } },
    KokoroTTS: { cancel() { cancels.n += 1; }, isSpeaking: speaking },
    Voice: { speak(text, opts) { spoken.push({ text, opts }); } },
    ChatBridge: {
      onMessage(fn) { handlers.add(fn); return () => handlers.delete(fn); },
      __socketOpen() { return socketOpen; },
      __sendRaw(obj) { interrupts.push(obj); },
    },
    AudioContext: function () { return fake; },
    fetch: async (url, options) => { requests.push({ url, options }); return { ok: true }; },
    window: null,
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context, { filename: 'voice-live.js' });
  const live = context.OmegaLive;
  assert.equal(live.supported(), true);
  assert.equal(live.start({ fake: 'stream' }, {
    isSpeaking: speaking,
    onUtterance: (blob, info) => utterances.push({ bytes: blob.size, ms: info.ms }),
  }), true);
  assert.equal(typeof fake.proc.onaudioprocess, 'function', 'capture is wired to the processor');
  return { live, spoken, interrupts, utterances, requests, cancels, fake, push: env => handlers.forEach(h => h(env)) };
}

// The HTTP fallback must raise the same speech-only verb as the websocket.
// `/api/chat/interrupt` also halts the work behind the response.
{
  const { live, requests } = loadLive({ socketOpen: false });
  live.sendInterrupt('cyc-http');
  await Promise.resolve();
  assert.equal(requests.length, 1);
  assert.equal(requests[0].url, '/api/chat/barge-in');
  assert.equal(requests[0].options.method, 'POST');
  assert.deepEqual(JSON.parse(requests[0].options.body), { cycle_id: 'cyc-http' });
}

// Objects born inside the VM realm carry that realm's Object prototype, so
// strict deepEqual rejects them; compare shape by JSON instead.
const same = (actual, expected, msg) => assert.equal(JSON.stringify(actual), JSON.stringify(expected), msg);

function tone(ms, amplitude, rate = 16000) {
  const n = Math.round(rate * ms / 1000);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = amplitude * Math.sin(i * 0.3);
  return out;
}

// ── endpointing ─────────────────────────────────────────────────────────
{
  const { live, utterances, fake } = loadLive();
  live.feed(tone(600, 0.001), 16000);   // silence — the noise floor settles low
  assert.equal(live.state.inSpeech, false);
  live.feed(tone(900, 0.2), 16000);     // speech
  assert.equal(live.state.inSpeech, true, 'speech opens an utterance');
  live.feed(tone(400, 0.001), 16000);   // 400 ms silence: not yet closed
  assert.equal(live.state.inSpeech, true);
  live.feed(tone(400, 0.001), 16000);   // 800 ms total: closed
  assert.equal(live.state.inSpeech, false);
  assert.equal(utterances.length, 1, 'one utterance shipped');
  assert.ok(utterances[0].ms >= 850 && utterances[0].ms < 1100, `voiced ms ${utterances[0].ms}`);
  // WAV: 44-byte header + 16-bit samples of ~(320 ms pre-roll + 900 ms speech + ~700 ms hangover)
  assert.ok(utterances[0].bytes > 44 + 16000 * 2 * 1.5, `bytes ${utterances[0].bytes}`);
  // a click opens (150 ms > the 120 ms onset) but is dropped as too short to be a word
  live.feed(tone(150, 0.5), 16000);
  live.feed(tone(800, 0.001), 16000);
  assert.equal(utterances.length, 1, 'a 150 ms click is not an utterance');
  assert.equal(live.state.dropped, 1);
  // 48 kHz input is downsampled before framing
  live.feed(tone(900, 0.2, 48000), 48000);
  live.feed(tone(800, 0.001, 48000), 48000);
  assert.equal(utterances.length, 2, 'a 48 kHz utterance ships too');
  // frames arrive through the processor callback in the real path
  fake.proc.onaudioprocess({ inputBuffer: { getChannelData: () => tone(90, 0.001) } });
  live.stop();
  assert.equal(live.isRunning(), false);
}

// ── barge-in: speech over playback cancels Kokoro and interrupts the cycle ─
{
  let speaking = false;
  const { live, interrupts, cancels, push } = loadLive({ speaking: () => speaking });
  push({ kind: 'chat.reply_delta', cycle_id: 'cyc-1', seq: 0, text: 'The weather is mild. ' });
  speaking = true;
  live.feed(tone(600, 0.001), 16000);
  live.feed(tone(400, 0.3), 16000);
  assert.equal(cancels.n, 1, 'Kokoro cancelled on barge-in');
  same(interrupts, [{ kind: 'chat.interrupt', cycle_id: 'cyc-1' }]);
  assert.equal(live.state.bargeIns, 1);
  // no second interrupt for the same cycle
  live.feed(tone(800, 0.001), 16000);
  live.feed(tone(400, 0.3), 16000);
  assert.equal(interrupts.length, 1);
}

// ── sentence streaming + final reconciliation ───────────────────────────
{
  const { live, spoken, push } = loadLive();
  push({ kind: 'chat.reply_delta', cycle_id: 'c2', seq: 0, text: 'It is 14:02. Anything' });
  assert.deepEqual(spoken.map(s => s.text), ['It is 14:02.']);
  assert.equal(spoken[0].opts.live, true);
  push({ kind: 'chat.reply_delta', cycle_id: 'c2', seq: 1, text: ' else? Maybe' });
  assert.deepEqual(spoken.map(s => s.text), ['It is 14:02.', 'Anything else?']);
  push({ kind: 'chat.reply', cycle_id: 'c2', text: 'It is 14:02. Anything else? Maybe later.', delivery: { rendered_by: 'omega_async' } });
  // chat-feed then calls Voice.speak(fullText): only the tail is left
  const verdict = live.alreadySpoken('It is 14:02. Anything else? Maybe later.');
  same(verdict, { skip: false, remainder: 'Maybe later.' });
  // and asking again is a no-op (everything accounted for)
  same(live.alreadySpoken('It is 14:02. Anything else? Maybe later.'), { skip: true });
}
{
  const { live, spoken, push } = loadLive();
  push({ kind: 'chat.reply_delta', cycle_id: 'c3', seq: 0, text: 'Done. ' });
  push({ kind: 'chat.reply', cycle_id: 'c3', text: 'Done.', delivery: { rendered_by: 'omega_async' } });
  same(live.alreadySpoken('Done.'), { skip: true }, 'fully streamed reply is not spoken twice');
  same(live.alreadySpoken('Something unrelated.'), { skip: false });
  assert.equal(spoken.length, 1);
}
{
  // a seq-0 delta restarts the transcript (think-on retry) instead of appending
  const { spoken, push } = loadLive();
  push({ kind: 'chat.reply_delta', cycle_id: 'c4', seq: 0, text: 'First try. ' });
  push({ kind: 'chat.reply_delta', cycle_id: 'c4', seq: 0, text: 'Second try. ' });
  assert.deepEqual(spoken.map(s => s.text), ['First try.', 'Second try.']);
}
{
  // an interrupted reply is acknowledged, not spoken
  const { live, spoken, push } = loadLive();
  push({ kind: 'chat.reply_delta', cycle_id: 'c5', seq: 0, text: 'Starting the' });
  push({ kind: 'chat.reply', cycle_id: 'c5', text: '(stopped)', delivery: { rendered_by: 'omega_async', interrupted: true } });
  assert.equal(spoken.length, 0);
  same(live.alreadySpoken('(stopped)'), { skip: false });
}
{
  const { live } = loadLive();
  assert.equal(live.lastSentenceEnd('Hello there. How are'), 'Hello there.'.length);
  assert.equal(live.lastSentenceEnd('No end yet'), 0);
  assert.equal(live.lastSentenceEnd('Really?! "Yes." ok'), 'Really?! "Yes."'.length);
}

// Frames already in flight must never resurrect an interrupted response.
{
  const { live, spoken, push } = loadLive();
  push({ kind: 'chat.reply_delta', cycle_id: 'interrupted', seq: 0, text: 'First sentence. ' });
  live.bargeIn();
  push({ kind: 'chat.reply_delta', cycle_id: 'interrupted', seq: 1, text: 'Late sentence. ' });
  push({ kind: 'chat.reply_delta', cycle_id: 'interrupted', seq: 0, text: 'Late retry. ' });
  assert.deepEqual(spoken.map(s => s.text), ['First sentence.']);
  push({ kind: 'chat.reply', cycle_id: 'interrupted', text: 'First sentence. Late sentence.' });
  same(live.alreadySpoken('First sentence. Late sentence.'), { skip: true });
  push({ kind: 'chat.reply_delta', cycle_id: 'next', seq: 0, text: 'Next response. ' });
  assert.equal(spoken.at(-1).text, 'Next response.');
  same(live.alreadySpoken('First sentence. Late sentence.', 'interrupted'), { skip: true });
}

// A different turn can legitimately answer with the same words.
{
  const { live, push } = loadLive();
  push({ kind: 'chat.reply_delta', cycle_id: 'old', seq: 0, text: 'Yes. ' });
  push({ kind: 'chat.reply', cycle_id: 'old', text: 'Yes.' });
  same(live.alreadySpoken('Yes.', 'new'), { skip: false });
}

console.log('voice-live: all runtime behaviour checks passed');

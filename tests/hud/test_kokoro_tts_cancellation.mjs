import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const fullSource = fs.readFileSync(
  new URL('../../hud/ui-src/core/voice-runtime.js', import.meta.url),
  'utf8',
);
const start = fullSource.indexOf('const KokoroTTS = (function () {');
const endMarker = 'window.KokoroTTS = KokoroTTS;';
const end = fullSource.indexOf(endMarker, start) + endMarker.length;
assert.ok(start >= 0 && end > start, 'KokoroTTS runtime section must be present');
const kokoroSource = fullSource.slice(start, end);
const speechSource = fs.readFileSync(
  new URL('../../hud/ui-src/core/speech-text.js', import.meta.url),
  'utf8',
);

function flushAsync() {
  return new Promise(resolve => setTimeout(resolve, 0));
}

function loadKokoro(fetchImpl, extras = {}) {
  const calls = { plays: 0, pauses: 0, created: [], revoked: [], audios: [] };
  let urlIndex = 0;
  class AudioStub {
    constructor(url) {
      this.url = url;
      this.onended = null;
      this.onerror = null;
      calls.audios.push(this);
    }
    play() {
      calls.plays += 1;
      return new Promise(() => {});
    }
    pause() { calls.pauses += 1; }
  }
  const context = {
    console,
    fetch: fetchImpl,
    Audio: AudioStub,
    URL: {
      createObjectURL() {
        const url = `blob:tts-${++urlIndex}`;
        calls.created.push(url);
        return url;
      },
      revokeObjectURL(url) { calls.revoked.push(url); },
    },
    window: null,
    ...extras,
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(speechSource, context, { filename: 'speech-text.js' });
  vm.runInContext(kokoroSource, context, { filename: 'voice-runtime-kokoro.js' });
  return { kokoro: context.KokoroTTS, calls, context };
}

{
  const urls = [];
  const { kokoro, calls, context } = loadKokoro(async (url, init) => {
    urls.push(url);
    return {
      ok: true,
      status: 200,
      blob: async () => ({ type: 'audio/wav' }),
    };
  });
  let voiceSettings = { tts_enabled: true, auto_speak: true, live_mode: true };
  context.Voice = { speak() {}, isListening: () => false, cancel() {} };
  context.currentVoiceSettings = () => voiceSettings;
  const wrapperStart = fullSource.indexOf(
    "if (typeof Voice !== 'undefined' && Voice.speak) {",
    end,
  );
  const wrapperEnd = fullSource.indexOf('// ── VOICE-3:', wrapperStart);
  assert.ok(wrapperStart >= end && wrapperEnd > wrapperStart);
  vm.runInContext(fullSource.slice(wrapperStart, wrapperEnd), context, {
    filename: 'voice-runtime-speak-wrapper.js',
  });

  const playback = context.Voice.speak('The greeting is audible.', {
    speakAlways: true,
    force: true,
    awaitPlayback: true,
  });
  let completed = null;
  playback.then(value => { completed = value; });
  await flushAsync();
  assert.equal(urls[0], '/api/tts/speak', 'completion tracking uses a playable audio element');
  assert.equal(completed, null, 'enqueue is not playback completion');
  assert.equal(calls.plays, 1);
  calls.audios[0].onended();
  assert.equal(await playback, true, 'the completion promise resolves after audio ends');

  voiceSettings = { ...voiceSettings, tts_enabled: false };
  assert.equal(
    await context.Voice.speak('Explicitly muted.', { speakAlways: true, awaitPlayback: true }),
    false,
    'an explicit TTS off choice is not acknowledged as speech',
  );
  kokoro.cancel();
}

{
  const payloads = [];
  const { kokoro } = loadKokoro(async (_url, init) => {
    payloads.push(JSON.parse(init.body));
    return {
      ok: true,
      status: 200,
      blob: async () => ({ type: 'audio/wav' }),
    };
  });
  const oneThought = [
    'This response explains the architecture clearly and preserves the operator request while',
    'validating every downstream contract so Kokoro receives one coherent thought with enough',
    'context to choose natural sentence prosody.',
  ].join(' ');

  kokoro.enqueue(oneThought);
  await flushAsync();
  await flushAsync();
  await flushAsync();

  assert.equal(payloads.length, 1, 'one coherent thought should require one synthesis request');
  assert.equal(payloads[0].text, oneThought);
  kokoro.cancel();
}

{
  let resolveFetch;
  const { kokoro, calls } = loadKokoro(() => new Promise(resolve => {
    resolveFetch = resolve;
  }));
  kokoro.enqueue('first sentence');
  kokoro.enqueue('second sentence');
  await flushAsync();
  kokoro.cancel();
  resolveFetch({
    ok: true,
    status: 200,
    blob: async () => ({ type: 'audio/wav' }),
  });
  await flushAsync();
  await flushAsync();

  assert.equal(calls.plays, 0, 'cancel must invalidate in-flight synthesis');
  assert.deepEqual(calls.created, [], 'stale synthesis must not allocate an audio URL');
}

{
  const { kokoro, calls } = loadKokoro(async () => ({
    ok: true,
    status: 200,
    blob: async () => ({ type: 'audio/wav' }),
  }));
  kokoro.enqueue('currently playing sentence');
  await flushAsync();
  await flushAsync();
  assert.equal(calls.plays, 1);
  assert.equal(calls.created.length, 1);

  kokoro.cancel();

  assert.equal(calls.pauses, 1);
  assert.deepEqual(calls.revoked, calls.created, 'cancel must revoke active audio URL');
}

console.log('Kokoro cancellation generation checks passed');

{
  let requests = 0;
  const { kokoro } = loadKokoro(() => { requests++; return new Promise(() => {}); });
  kokoro.enqueue('Yes.');
  kokoro.cancel();
  kokoro.enqueue('Yes.');
  assert.equal(requests, 2, 'a new turn can repeat text from the interrupted turn');
  kokoro.cancel();
}

{
  const enqueues = [], reconciled = [];
  const context = vm.createContext({
    Voice: { speak() {}, isListening: () => true },
    KokoroTTS: { enqueue: (text, opts) => enqueues.push({ text, opts }) },
    currentVoiceSettings: () => ({ tts_enabled: true, live_mode: true, tts_voice: 'Ryan' }),
    OmegaLive: { alreadySpoken: (text, cycle) => { reconciled.push(cycle); return { remainder: 'The remaining words.' }; } },
  });
  const wrapperStart = fullSource.indexOf("if (typeof Voice !== 'undefined' && Voice.speak) {");
  const wrapperEnd = fullSource.indexOf('// ── VOICE-3:', wrapperStart);
  vm.runInContext(fullSource.slice(wrapperStart, wrapperEnd), context);
  context.Voice.speak('Already spoken. The remaining words.', { cycleId: 'turn-one' });
  assert.equal(enqueues[0].opts.live, true, 'the final remainder must use native PCM in live conversation');
  assert.equal(enqueues[0].opts.voice, 'Ryan');
  assert.equal(enqueues[0].text, 'The remaining words.');
  assert.deepEqual(reconciled, ['turn-one']);
}

{
  const urls = [], sources = [];
  const { kokoro } = loadKokoro(async url => {
    urls.push(url);
    let read = false;
    return { ok: true, body: { getReader: () => ({
      read: async () => read ? { done: true } : (read = true, { done: false, value: new Uint8Array(48000) }),
      cancel: async () => {}, releaseLock() {},
    }) } };
  }, {
    AbortController, setTimeout() {},
    AudioContext: function () {
      return { currentTime: 0, destination: {},
        createBuffer: (_channels, n) => ({ getChannelData: () => new Float32Array(n) }),
        createBufferSource: () => {
          const source = { connect() {}, disconnect() {}, stop() {}, start(at) { this.at = at; } };
          sources.push(source);
          return source;
        },
      };
    },
  });
  kokoro.enqueue('sentence one', { live: true });
  kokoro.enqueue('sentence two', { live: true });
  await flushAsync();
  await flushAsync();
  assert.equal(urls.length, 2, 'synthesize the next sentence while the current audio is scheduled');
  assert.equal(sources[1].at, sources[0].at + 1, 'sentence PCM must remain ordered without overlap');
  assert.equal(kokoro.isSpeaking(), true, 'VAD must see scheduled playback after HTTP completes');
  sources.forEach(source => source.onended());
  assert.equal(kokoro.isSpeaking(), false);
}

{
  let resolveRead;
  let starts = 0, readerCancels = 0;
  const urls = [];
  const { kokoro } = loadKokoro(async (url) => {
    urls.push(url);
    return { ok: true, body: { getReader: () => ({
      read: () => new Promise(resolve => { resolveRead = resolve; }),
      cancel: async () => { readerCancels++; }, releaseLock() {},
    }) } };
  }, {
    AbortController, setTimeout,
    AudioContext: function () {
      return { currentTime: 0, destination: {},
        createBuffer: () => ({ getChannelData: () => new Float32Array(1) }),
        createBufferSource: () => ({ connect() {}, disconnect() {}, stop() {}, start() { starts++; } }),
      };
    },
  });
  kokoro.enqueue('streaming sentence', { live: true });
  await flushAsync();
  kokoro.cancel();
  resolveRead({ done: false, value: new Uint8Array([0, 0]) });
  await flushAsync();
  assert.equal(starts, 0, 'PCM arriving after cancel must not schedule audio');
  assert.equal(readerCancels, 1, 'cancel releases the body reader');
  assert.deepEqual(urls, ['/api/tts/stream']);
}

{
  const urls = [];
  const { kokoro } = loadKokoro(async url => {
    urls.push(url);
    return { ok: false, status: 502 };
  }, { AbortController, AudioContext: function () {}, setTimeout });
  kokoro.enqueue('failed sentence', { live: true });
  await flushAsync();
  await flushAsync();
  assert.deepEqual(urls, ['/api/tts/stream'], 'stream failures must not replay as WAV');
  assert.equal(kokoro.isSpeaking(), false);
}

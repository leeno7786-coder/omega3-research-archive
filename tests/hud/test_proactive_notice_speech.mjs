import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(
  new URL('../../hud/ui-src/panels/system_messages.js', import.meta.url),
  'utf8',
);

let finishSpeech;
let ackCount = 0;
const spoken = [];
const row = {
  id: 'greeting-1',
  message: 'Hello, operator.',
  status: 'pending',
  opened_by: 'boot_greeting',
  payload: { speak_always: true, boot_greeting: true },
};
const context = {
  console,
  Date,
  Map,
  Promise,
  Set,
  URL,
  window: null,
  document: {
    hidden: false,
    visibilityState: 'visible',
    querySelector() { return null; },
  },
  setInterval() { return 1; },
  Voice: {
    speak(text, opts) {
      spoken.push({ text, opts });
      return new Promise(resolve => { finishSpeech = resolve; });
    },
  },
  fetch: async (url, opts = {}) => {
    if (url === '/api/proactive/notices') {
      return { ok: true, json: async () => ({ messages: [row] }) };
    }
    if (url.endsWith('/delivered') && opts.method === 'POST') {
      ackCount += 1;
      return { ok: true, json: async () => ({ ok: true }) };
    }
    throw new Error(`unexpected fetch ${url}`);
  },
};
context.window = context;
context.globalThis = context;
vm.createContext(context);
vm.runInContext(
  `${source}\n;globalThis.__refreshNoticesForTest = _refreshProactiveNotices;`,
  context,
  { filename: 'system_messages.js' },
);

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

const pendingRefresh = context.__refreshNoticesForTest();
await flush();
assert.equal(spoken.length, 1, 'the pending greeting is sent to speech');
assert.equal(spoken[0].opts.speakAlways, true, 'boot greeting keeps its speak override');
assert.equal(spoken[0].opts.awaitPlayback, true, 'speech caller requests a completion signal');
assert.equal(ackCount, 0, 'a queued utterance is not acknowledged as heard');

finishSpeech(false);
await pendingRefresh;
await flush();
assert.equal(ackCount, 0, 'a rejected or cancelled utterance stays pending');

const retryRefresh = context.__refreshNoticesForTest();
await flush();
assert.equal(spoken.length, 2, 'the still-pending notice is retried on the next poll');
finishSpeech(true);
await retryRefresh;
await flush();
assert.equal(ackCount, 1, 'delivery is acknowledged after speech completes');

import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import {fileURLToPath} from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
  path.resolve(here, '../../hud/ui-src/core/live-wiring.js'), 'utf8');
const start = source.indexOf("window.addEventListener('keydown', async e => {", source.indexOf('// Esc halts'));
const end = source.indexOf('\n});', start) + 4;
assert.ok(start >= 0 && end > start, 'the live Escape listener exists');

const messages = [];
let cancelCount = 0;
let release;
let listener;
const context = {
  window: {
    _pendingChatResponses: new Map([['cycle-live', {}]]),
    addEventListener(_type, callback) { listener = callback; },
  },
  Voice: {cancel() { cancelCount += 1; }},
  appendLine(role, text) { messages.push({role, text}); },
  fetch(_url, options) {
    assert.deepEqual(JSON.parse(options.body), {cycle_ids: ['cycle-live']});
    return new Promise(resolve => { release = resolve; });
  },
  Array, JSON, String, Error,
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context, {filename: 'live-wiring.js'});

const pending = listener({key: 'Escape', ctrlKey: false, metaKey: false, altKey: false});
assert.equal(cancelCount, 1, 'Escape silences active speech before waiting on the halt request');
assert.deepEqual(messages, [{role: 'system', text: 'Stop requested…'}]);
release({ok: true, status: 200, json: async () => ({ok: true, stopped_jobs: ['job-live'], stop_recorded: true})});
await pending;
assert.deepEqual(messages.map(row => row.text), [
  'Stop requested…', 'Stopped 1 running job(s).',
]);

context.fetch = async () => { throw new Error('offline'); };
await listener({key: 'Escape', ctrlKey: false, metaKey: false, altKey: false});
assert.equal(cancelCount, 2, 'Escape always cancels current playback');
assert.equal(messages.at(-1).text, 'Stop request failed: offline');

console.log('live Escape halt feedback tests passed');

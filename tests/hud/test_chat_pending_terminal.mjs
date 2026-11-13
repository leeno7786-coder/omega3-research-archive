import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(
  new URL('../../hud/ui-src/core/chat-feed.js', import.meta.url),
  'utf8',
);

let now = 1_800_000_000_000;
let response = { found: false };
let fetchCount = 0;
const state = { textContent: '' };
const title = { textContent: 'Work ended after ' };
const block = {
  querySelector(selector) {
    if (selector === '.thinking-state') return state;
    if (selector === '.thinking-title-label') return title;
    return null;
  },
};
const updates = [];
const lines = [];
const lateResults = [];
const context = {
  console,
  Date: { now: () => now },
  Math,
  JSON,
  Number,
  String,
  document: { hidden: false },
  setInterval() { return 1; },
  fetch: async () => { fetchCount += 1; return { ok: true, json: async () => response }; },
  updateThinkingBlock(target, result, status) { updates.push({ target, result, status }); },
  appendLine(_role, text) { const line = { text, classList: { add() {} } }; lines.push(line); return line; },
  _scrollFeedToBottom() {},
};
context.window = context;
context.globalThis = context;
vm.createContext(context);
vm.runInContext(
  `${source}\n;globalThis.__resolvePending = _resolvePendingChatResponses;\n` +
  `globalThis.__pendingSince = _pendingChatSince;\n` +
  `globalThis.__pendingResponses = _pendingChatResponses;\n` +
  `globalThis.__maxPendingAge = _PENDING_CHAT_MAX_AGE_MS;`,
  context,
  { filename: 'chat-feed.js' },
);
context.updateThinkingBlock = (target, result, status) => updates.push({ target, result, status });
context.appendLine = (_role, text) => {
  const line = { text, classList: { add() {} } };
  lines.push(line);
  return line;
};

context.renderChatResult({ queued: true, cycle_id: 'stalled-turn', message: '' }, block);
context.__pendingSince.set('stalled-turn', now - context.__maxPendingAge - 1);
assert.equal(context.__pendingResponses.has('stalled-turn'), true, 'queued response remains registered');
assert.equal(context.__pendingSince.get('stalled-turn'), now - context.__maxPendingAge - 1);
assert.equal(vm.runInContext('Date.now()', context), now);

await context.__resolvePending();
assert.equal(fetchCount, 1, 'the registered queued response is polled');
assert.equal(updates.length, 1, 'the waiting card reaches a visible warning once');
assert.equal(updates[0].status, 'degraded');
assert.equal(updates[0].result.error, 'answer still pending after 7 minutes');
assert.equal(title.textContent, 'Still waiting after ');
assert.match(state.textContent, /no answer after 7 minutes/);
assert.match(lines[0].text, /keep checking for a late result/);
assert.equal(context.__pendingResponses.has('stalled-turn'), true,
  'durable polling stays registered so a late answer can still arrive');

now += 4_000;
await context.__resolvePending();
assert.equal(updates.length, 1, 'the terminal notice is not repeated every poll');
assert.equal(lines.length, 1);

response = { found: true, cycle_id: 'stalled-turn', message: 'late but durable reply' };
context.renderChatResult = (result, target) => lateResults.push({ result, target });
await context.__resolvePending();
assert.equal(lateResults.length, 1, 'a late durable response still resolves the same bubble');
assert.equal(lateResults[0].target, block);

console.log('chat pending warning stays honest and retains late recovery');

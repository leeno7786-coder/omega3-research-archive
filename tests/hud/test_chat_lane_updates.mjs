import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const wiringSource = fs.readFileSync(
  path.resolve(here, '../../hud/ui-src/core/chat-wiring.js'), 'utf8');
const feedSource = fs.readFileSync(
  path.resolve(here, '../../hud/ui-src/core/chat-feed.js'), 'utf8');

function load(fetchHandler, options = {}) {
  let onMessage = null;
  const lines = [];
  const timers = [];
  const loadListeners = [];
  const ctx = {
    console,
    _liveOrigin: true,
    ChatBridge: {
      send() {},
      onMessage(fn) { onMessage = fn; return () => {}; },
      __socketOpen: () => true,
      __sendRaw() { return true; },
    },
    document: { hidden: false, readyState: options.readyState || 'loading', getElementById: () => null },
    feed: { appendChild() {}, scrollTo() {}, scrollHeight: 0 },
    appendLine(role, text) {
      const line = { role, text, classList: { add() {} } };
      lines.push(line);
      return line;
    },
    appendThinkingBlock: () => ({ querySelector: () => null }),
    renderChatResult: () => ({ ok: true }),
    appendChatError() {},
    updateThinkingBlock() {},
    _scrollFeedToBottom() {},
    crypto: { randomUUID: () => 'client-cycle' },
    fetch: fetchHandler,
    setTimeout(fn) { timers.push(fn); return timers.length; },
    clearTimeout() {},
    requestAnimationFrame(fn) { fn(); return 0; },
    setInterval() { return 0; },
    addEventListener(type, fn) {
      if (type === 'load') loadListeners.push(fn);
    },
    CustomEvent: class {},
    Date, Math, JSON,
  };
  if (options.noLoadListener) delete ctx.addEventListener;
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.dispatchEvent = () => true;
  vm.createContext(ctx);
  vm.runInContext(feedSource, ctx);
  vm.runInContext(wiringSource, ctx);
  return {
    ctx, lines, timers, loadListeners,
    receive: frame => onMessage(frame),
  };
}

// Wiring injected after the document's load event still recovers active lanes.
{
  const requested = [];
  const h = load(async url => {
    requested.push(String(url));
    return { ok: true, json: async () => ({ lanes: [] }) };
  }, { readyState: 'complete', noLoadListener: true });
  await new Promise(resolve => globalThis.setImmediate(resolve));
  assert.deepEqual(requested, ['/api/chat/active-lanes']);
}

// A target lane can finish before AUTO paints the queued acknowledgement. Its
// update stays buffered so the one shared chat rail reads in causal order.
{
  const h = load(async () => ({
    ok: true,
    json: async () => ({ found: true, updates: [], done: false, last_seq: 0 }),
  }));
  h.receive({
    kind: 'chat.update', cycle_id: 'turn-1', update_id: 'browser-ep:progress',
    seq: 1, lane: 'browser', status: 'progress', text: 'Browser opened the results page.',
  });
  h.receive({
    kind: 'chat.update', cycle_id: 'turn-1', update_id: 'browser-ep:progress',
    seq: 1, lane: 'browser', status: 'progress', text: 'Browser opened the results page.',
  });
  assert.deepEqual(h.lines.map(line => line.text), [],
    'specialist work cannot appear before the chat acknowledgement');
  h.receive({
    kind: 'chat.reply', cycle_id: 'turn-1', text: 'Browser work is queued.',
    lane_work: { lane: 'browser', handoff_id: 'browser-handoff', status: 'queued' },
  });

  assert.deepEqual(h.lines.map(line => line.text), [
    'Browser work is queued.', 'Browser opened the results page.',
  ]);
  const started = [];
  h.ctx.startChatLaneUpdatePolling = (...args) => started.push(args);
  h.ctx.renderChatResult({
    cycle_id: 'turn-post',
    lane_work: { lane: 'agent', handoff_id: 'handoff' },
    message: 'Coding Agent queued.',
    delivery: { text: 'Coding Agent queued.', rendered_by: 'omega_async' },
  });
  assert.deepEqual(started, [['turn-post', 0]],
    'POST and orphan recovery replies also start specialist update polling');
}

// A later socket event cannot advance the durable cursor past an earlier update
// that has not arrived yet; the poll replays the missing prefix in order.
{
  const h = load(async url => ({
    ok: true,
    json: async () => ({ found: true, status: 'progress', done: false, updates: [
      { seq: 1, update_id: 'agent:1', lane: 'agent', text: 'First update.' },
      { seq: 2, update_id: 'agent:2', lane: 'agent', text: 'Second update.' },
    ] }),
  }));
  h.ctx.chatLaneAcknowledgementRendered('turn-order');
  h.receive({ kind: 'chat.update', cycle_id: 'turn-order', seq: 2,
    update_id: 'agent:2', lane: 'agent', status: 'progress', text: 'Second update.' });
  h.receive({ kind: 'chat.update', cycle_id: 'turn-order', seq: 1,
    update_id: 'agent:1', lane: 'agent', status: 'progress', text: 'First update.' });
  await new Promise(resolve => globalThis.setImmediate(resolve));

  assert.deepEqual(h.lines.map(line => line.text), ['First update.', 'Second update.']);
}

// Reload recovery discovers active specialist work and resumes its durable FIFO
// of correlated updates from the last delivered sequence.
{
  const requested = [];
  const h = load(async url => {
    requested.push(String(url));
    if (String(url) === '/api/chat/active-lanes') {
      return { ok: true, json: async () => ({ lanes: [
        { cycle_id: 'turn-reload', lane: 'agent', status: 'queued', last_seq: 4,
          acknowledged: true, acknowledged_seq: 0 },
      ] }) };
    }
    if (String(url) === '/api/chat/updates/ack') {
      return { ok: true, json: async () => ({ ok: true, acknowledged_seq: 1 }) };
    }
    if (String(url).startsWith('/api/chat/updates?')) {
      return { ok: true, json: async () => ({
        found: true, cycle_id: 'turn-reload', lane: 'agent', status: 'completed',
        done: true, last_seq: 1, updates: [{
          seq: 1, update_id: 'agent-ep:completed', status: 'completed',
          lane: 'agent', text: 'Coding Agent changed the parser and completed verification.',
        }],
      }) };
    }
    throw new Error('unexpected request: ' + url);
  });

  assert.equal(h.loadListeners.length, 1, 'the chat UI registers reload recovery');
  h.loadListeners[0]();
  await new Promise(resolve => globalThis.setImmediate(resolve));

  assert.ok(requested.includes('/api/chat/active-lanes'));
  assert.ok(requested.includes('/api/chat/updates?cycle_id=turn-reload&after_seq=0'),
    'reload replays durable updates because no client cursor survived the reload: '
      + JSON.stringify(requested));
  assert.ok(requested.includes('/api/chat/updates/ack'));
  assert.deepEqual(h.lines.map(line => line.text), [
    'Coding Agent changed the parser and completed verification.',
  ]);
}

console.log('chat specialist lane update tests passed');

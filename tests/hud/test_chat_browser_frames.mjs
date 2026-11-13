import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { Element } from './plugin_dom_shim.mjs';

// The browser agent's screenshot frames land under the action's trace row, in order,
// so the operator watches the work happen in the chat panel (operator 2026-09-15).

Element.prototype.remove = function remove() {
  if (!this.parent) return;
  const index = this.parent.children.indexOf(this);
  if (index >= 0) this.parent.children.splice(index, 1);
};
Object.defineProperty(Element.prototype, 'style', {value: {}, writable: true});

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.resolve(here, '../../hud/ui-src/core/chat-feed.js'), 'utf8');

const feed = new Element('div');
feed.scrollHeight = 0;
feed.scrollTo = () => {};

const context = {
  console, Date, Math, JSON, Number, String,
  document: {
    hidden: false,
    addEventListener() {},
    createElement(tag) { const element = new Element(tag); element.style = {}; return element; },
  },
  feed,
  requestAnimationFrame(fn) { fn(); },
  setTimeout() { return 0; },
  setInterval() { return 0; },
  fetch() { return Promise.resolve({ok: false}); },
  _clipChatText(value) { return String(value || ''); },
  _escapeHtml(value) { return String(value || ''); },
  _renderMarkdown(value) { return String(value || ''); },
  _thinkingFromResult() { return {status: 'resolved', steps: [], memory: {status: 'skipped', count: 0, scope: [], entries: [], snippets: []}}; },
};
context.window = context;
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, {filename: 'chat-feed.js'});

const block = context.appendThinkingBlock('open Hugging Face');
const row = context.traceToolCall(block, {call_id: 'act-1', tool: 'navigate', args: {url: 'https://huggingface.co/'}});
assert.ok(row, 'the action has a trace row');
const progress = context.traceToolProgress(block, {
  call_id: 'act-1', tool: 'navigate', message: 'still running · 5s elapsed', elapsed_s: 5,
});
assert.equal(progress, row, 'command progress updates its pending tool row');
assert.match(row.querySelector('.thinking-tool-status').textContent, /running · 5s/);
assert.match(row.querySelector('.thinking-tool-body').textContent, /still running · 5s elapsed/);
context.traceToolResult(block, {
  call_id: 'act-1', tool: 'navigate', ok: true, output: 'Navigated.',
  browser_setup: {status: 'startup_required', action: 'restart_omega'},
});
assert.match(row.querySelector('.thinking-tool-body').textContent, /Browser setup: startup required · restart Omega/);

context.traceToolResult(block, {
  call_id: 'write-1', tool: 'write_file', ok: true, output: 'wrote file',
  dev_artifact_hold: {status: 'unavailable', held_count: 0, reason: 'no open SRAM bucket'},
});
const heldRow = block.querySelectorAll('.thinking-tool').find(item => item.dataset.callId === 'write-1');
assert.match(heldRow.querySelector('.thinking-tool-body').textContent,
  /Could not record changed files in episode memory: no open SRAM bucket/);

const frame = context.traceBrowserFrame(block, {
  call_id: 'act-1', step: 1, tool: 'navigate', ok: true, url: 'https://huggingface.co/',
  title: 'Hugging Face', image: 'data:image/jpeg;base64,/9j/4AAQ', width: 720, height: 405,
});
assert.ok(frame, 'a frame with an image renders');
const body = row.querySelector('.thinking-tool-body');
assert.equal(body.hidden, false, 'the row body opens to show the screenshot');
assert.ok(body.children.includes(frame), 'the frame sits under its action row');
const img = frame.querySelector('img');
assert.equal(img.src, 'data:image/jpeg;base64,/9j/4AAQ');
assert.match(frame.querySelector('figcaption').textContent, /step 1 · navigate · Hugging Face/);

// A frame without a matching row still appends to the trace; one without pixels draws nothing.
const loose = context.traceBrowserFrame(block, {call_id: 'act-9', step: 2, tool: 'click', ok: false, image: 'data:image/jpeg;base64,x'});
assert.ok(loose.className.includes('is-failed'));
assert.equal(context.traceBrowserFrame(block, {call_id: 'act-1', step: 3, tool: 'scroll', image: null}), null);

const frames = block.querySelectorAll('.thinking-browser-frame');
assert.equal(frames.length, 2);
assert.deepEqual(frames.map(f => f.dataset.step), ['1', '2']);

console.log('chat browser frame rendering tests passed');

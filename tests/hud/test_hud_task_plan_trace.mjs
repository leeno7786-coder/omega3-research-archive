// The task's plan is the one thing the operator most wants to see while a build runs.
//
// The agent lane runs one tool batch per episode and carries its plan on the job
// record (2026-09-20). The plan is therefore real structured state, not prose — so the
// trace renders it as a checklist with its own progress count, instead of dumping the
// host-rendered text into a code block where "[done]"/"[open]" is all the operator gets.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { Element, esc } from './plugin_dom_shim.mjs';

Element.prototype.remove = function remove() {
  if (this.parent) this.parent.removeChild(this);
};
// Elements the shim parses out of innerHTML carry no style object of their own.
Object.defineProperty(Element.prototype, 'style', { value: {}, writable: true });

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(path.resolve(here, '../../hud/ui-src/core/chat-feed.js'), 'utf8');

const feed = new Element('div');
feed.scrollHeight = 0;
feed.clientHeight = 0;
feed.style = {};
feed.scrollTo = () => {};

const context = {
  console, Date, Math, JSON, Number, String, Array, Object, Boolean, Map, Set,
  document: {
    hidden: false,
    addEventListener() {},
    createElement(tag) { const el = new Element(tag); el.style = {}; return el; },
  },
  feed,
  requestAnimationFrame(fn) { fn(); },
  setTimeout() { return 0; },
  setInterval() { return 0; },
  fetch() { return Promise.resolve({ ok: false }); },
  _clipChatText(value) { return String(value || ''); },
  _escapeHtml: esc,
  _renderMarkdown(value) { return String(value || ''); },
  appendLine: () => null,
  _thinkingFromResult() {
    return { status: 'resolved', steps: [], memory: { status: 'skipped', count: 0, scope: [], entries: [], snippets: [] } };
  },
};
context.window = context;
context.globalThis = context;
vm.createContext(context);
vm.runInContext(source, context, { filename: 'chat-feed.js' });

const block = context.appendThinkingBlock('build a todo app');
const row = context.traceToolResult(block, {
  call_id: 'plan-1',
  tool: 'task_plan',
  ok: true,
  output: 'TASK PLAN\n1. [done] scaffold the project\n2. [open] write the <todo> model\nDo the next open step.',
  plan: [
    { step: 1, text: 'scaffold the project', done: true },
    { step: 2, text: 'write the <todo> model', done: false },
    { step: 3, text: "keep the operator's wording", done: false },
  ],
  duration_ms: 12,
});
assert.ok(row, 'the plan call has a trace row');

const checklist = row.querySelector('.thinking-plan');
assert.ok(checklist, 'a plan result renders a checklist, not a raw text block');
const steps = row.querySelectorAll('.thinking-plan-step');
assert.equal(steps.length, 3, 'one row per planned step');
assert.equal(steps[0].classList.contains('is-done'), true, 'a cleared step reads as done');
assert.equal(steps[1].classList.contains('is-done'), false, 'an open step does not');
assert.match(row.querySelector('.thinking-plan-head').textContent, /1\s*\/\s*3/,
  'the head counts the progress the operator is watching for');

// The step text is data, escaped, and never re-derived from the host's rendering.
assert.match(steps[1].textContent, /write the <todo> model/);
assert.match(steps[2].textContent, /keep the operator's wording/);
assert.equal(row.querySelectorAll('.chat-code-block').length, 0,
  'the host-rendered plan text is not ALSO dumped as a code block');

// A plan that is finished says so, so the next thing the operator expects is finish_task.
const done = context.traceToolResult(block, {
  call_id: 'plan-2', tool: 'task_plan', ok: true, output: 'TASK PLAN\n1. [done] ship it',
  plan: [{ step: 1, text: 'ship it', done: true }],
});
assert.ok(done.querySelector('.thinking-plan').classList.contains('is-complete'),
  'a complete plan is marked complete');

// Every other tool still renders its output the way it did.
const other = context.traceToolResult(block, {
  call_id: 'w-1', tool: 'write_file', ok: true, output: 'wrote app.py',
});
assert.equal(other.querySelectorAll('.thinking-plan').length, 0);
assert.match(other.querySelector('.thinking-tool-body').textContent, /wrote app\.py/);

// ── a build's trace stays open ─────────────────────────────────────────────
// Every resolved thinking block collapsed itself, which is right for a chat turn whose
// only content is memory hits and wrong for a build: it hid the plan, the tool calls and
// the diffs the operator had just been watching. A turn that called tools stays open.
{
  const chat = context.appendThinkingBlock('who am I');
  context.updateThinkingBlock(chat, { ok: true, thinking: { steps: [] } }, 'resolved');
  assert.equal(chat.querySelector('.thinking-toggle').open, false,
    'a turn with no tool work still collapses to its one-line summary');
  assert.match(chat.querySelector('.thinking-title-label').textContent, /Thought for/);

  const build = context.appendThinkingBlock('build a todo app');
  context.updateThinkingBlock(build, {
    ok: true,
    thinking: { steps: [{ call_id: 'p1', tool: 'task_plan', ok: true, output: 'TASK PLAN' }] },
  }, 'resolved');
  assert.equal(build.querySelector('.thinking-toggle').open, true,
    'a turn that called tools keeps its trace on screen');
  assert.match(build.querySelector('.thinking-title-label').textContent, /Worked for/,
    'and it reads as work, not as thought');

  // The operator's own collapse still wins.
  const closed = context.appendThinkingBlock('build something else');
  closed.querySelector('.thinking-toggle').dataset.userToggled = 'true';
  closed.querySelector('.thinking-toggle').open = false;
  context.updateThinkingBlock(closed, {
    ok: true, thinking: { steps: [{ call_id: 'w1', tool: 'write_file', ok: true }] },
  }, 'resolved');
  assert.equal(closed.querySelector('.thinking-toggle').open, false,
    'a block the operator collapsed stays collapsed');
}

console.log('hud task plan trace tests passed');

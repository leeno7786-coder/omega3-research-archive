import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
  path.resolve(here, '../../hud/ui-src/core/chat-wiring.js'), 'utf8');
const feedSource = fs.readFileSync(
  path.resolve(here, '../../hud/ui-src/core/chat-feed.js'), 'utf8');

function load(extraGlobals, scriptSource = source) {
  const rendered = [];
  const blocks = [];
  const sent = [];
  const fetched = [];
  let onMessage = null;
  let nextId = 1;

  const ChatBridge = {
    send() { throw new Error('chat-wiring.js did not take over ChatBridge.send'); },
    onMessage(fn) { onMessage = fn; return () => { onMessage = null; }; },
    __socketOpen: () => true,
    __sendRaw(obj) { sent.push(obj); return true; },
  };
  const ctx = {
    console, ChatBridge,
    _liveOrigin: true,
    crypto: {
      randomUUID() {
        return `00000000-0000-4000-8000-${String(nextId++).padStart(12, '0')}`;
      },
    },
    appendThinkingBlock(seed) {
      const copy = { textContent: '' };
      const state = { textContent: '' };
      const pipeline = { textContent: '', hidden: true };
      const block = {
        seed, copy, state, pipeline, resolved: false,
        querySelector(selector) {
          if (selector === '.thinking-pending-copy') return copy;
          if (selector === '.thinking-state') return state;
          if (selector === '.thinking-pipeline') return pipeline;
          return null;
        },
      };
      blocks.push(block);
      return block;
    },
    renderChatResult(result, block) {
      rendered.push({ result, block });
      if (block && !result.queued) block.resolved = true;
      return { text: (result && result.message) || '', ok: result && result.ok !== false };
    },
    appendChatError: () => null,
    updateThinkingBlock: () => null,
    _scrollFeedToBottom: () => null,
    document: { getElementById: () => null },
    fetch(...args) { fetched.push(args); return Promise.reject(new Error('unexpected POST')); },
    CustomEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    Date, Math, JSON,
  };
  Object.assign(ctx, extraGlobals || {});
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.dispatchEvent = () => true;
  vm.createContext(ctx);
  vm.runInContext(scriptSource, ctx);

  return {
    rendered, blocks, sent, fetched,
    send: text => ChatBridge.send({ body: text }),
    recv: env => onMessage(env),
  };
}

// Live source and the shipped bundle both pass the durable interaction payload
// through to the existing question-card renderer.
{
  const bundle = fs.readFileSync(path.resolve(here, '../../hud/omega_interface.html'), 'utf8');
  const built = bundle.match(/\/\/ HUD_UI_SRC:INJECT_CHAT_WIRING_START([\s\S]*?)\/\/ HUD_UI_SRC:INJECT_CHAT_WIRING_END/);
  assert.ok(built, 'the shipped bundle must carry the chat wiring section');
  for (const [label, wiring] of [['source', source], ['bundle', built[1]]]) {
    const h = load(undefined, wiring);
    h.send('Choose the report format');
    const question = { question: 'Which format?', options: ['Markdown', 'HTML'], job_id: 'report-job' };
    const assignment = { project_root: 'workspace/report', job_id: 'report-job' };
    h.recv({
      kind: 'chat.reply', cycle_id: h.sent[0].cycle_id, text: question.question,
      operator_question: question, workspace_assignment: assignment,
    });
    assert.deepEqual(h.rendered[1].result.operator_question, question, `${label}: preserve question and choices`);
    assert.deepEqual(h.rendered[1].result.workspace_assignment, assignment, `${label}: preserve assigned project`);
  }
}

// Sending binds the block immediately to the client-minted cycle. There is no
// server acknowledgement frame and no operator-facing queued response.
{
  const h = load();
  h.send('Run the tool');

  assert.equal(h.sent.length, 1);
  assert.equal(h.sent[0].kind, 'chat.user_message');
  assert.equal(h.sent[0].cycle_id, '00000000-0000-4000-8000-000000000001');
  assert.equal(h.fetched.length, 0);
  assert.equal(h.rendered.length, 1, 'local queued state registers durable response recovery');
  assert.equal(h.rendered[0].result.queued, true);
  assert.equal(h.blocks[0].resolved, false);

  h.recv({
    kind: 'chat.accepted',
    cycle_id: h.sent[0].cycle_id,
    delivery: { text: 'On it.', rendered_by: 'operator_command_ack' },
  });
  assert.equal(h.rendered.length, 1, 'retired acknowledgement frames are ignored');
  assert.equal(h.blocks[0].resolved, false);

  h.recv({
    kind: 'chat.reply',
    cycle_id: h.sent[0].cycle_id,
    text: 'The real AUTO result.',
    delivery: { text: 'The real AUTO result.', rendered_by: 'omega_async' },
  });
  assert.equal(h.rendered.length, 2);
  assert.equal(h.rendered[1].block, h.blocks[0]);
  assert.equal(h.rendered[1].result.message, 'The real AUTO result.');
  assert.equal(h.blocks[0].resolved, true);
}

// A rendered reply is acknowledged by its exact cycle, and only after it rendered:
// the server marks it delivered on this, never on the socket write alone.
{
  const h = load();
  h.send('ack me');
  const cycle = h.sent[0].cycle_id;
  assert.equal(h.sent.length, 1, 'nothing is acknowledged before a reply renders');
  h.recv({ kind: 'chat.reply', cycle_id: cycle, text: 'rendered' });
  assert.deepEqual(JSON.parse(JSON.stringify(h.sent[1])), { kind: 'chat.ack', cycle_id: cycle });

  const broken = load({
    renderChatResult(result) {
      if (!result.queued) throw new Error('render failed');
      return { text: '', ok: true };
    },
  });
  broken.send('never rendered');
  assert.throws(() => broken.recv({ kind: 'chat.reply', cycle_id: broken.sent[0].cycle_id, text: 'x' }));
  assert.equal(broken.sent.filter(frame => frame.kind === 'chat.ack').length, 0,
    'a reply that failed to render is not acknowledged, so the orphan sweep recovers it');
}

// Cycle ownership, not arrival order, resolves concurrent turns.
{
  const h = load();
  h.send('first');
  h.send('second');
  const first = h.sent[0].cycle_id;
  const second = h.sent[1].cycle_id;

  h.recv({ kind: 'chat.reply', cycle_id: second, text: 'second result' });
  h.recv({ kind: 'chat.reply', cycle_id: first, text: 'first result' });

  assert.equal(h.rendered[2].block, h.blocks[1]);
  assert.equal(h.rendered[3].block, h.blocks[0]);
}

// Physical status is decoration; cycle ownership alone selects the bubble.
{
  const h = load();
  h.send('pipeline zero');
  h.send('pipeline three');
  const first = h.sent[0].cycle_id;
  const second = h.sent[1].cycle_id;

  h.recv({ kind: 'chat.reasoning_delta', cycle_id: second, pipeline_id: 3, event_seq: 2, text: 'second trace' });
  h.recv({ kind: 'chat.reasoning_delta', cycle_id: first, pipeline_id: 0, event_seq: 1, text: 'first trace' });
  h.recv({ kind: 'chat.reply', cycle_id: second, pipeline_id: 3, event_seq: 3, text: 'second done' });
  h.recv({ kind: 'chat.reply', cycle_id: first, pipeline_id: 0, event_seq: 4, text: 'first done' });

  assert.equal(h.rendered[2].block, h.blocks[1]);
  assert.equal(h.rendered[3].block, h.blocks[0]);
  assert.equal(h.blocks[1].pipeline.textContent, 'P3');
  assert.equal(h.blocks[0].pipeline.textContent, 'P0');
}

// Reply streaming is transport progress only. It must not duplicate assistant
// prose inside the thinking block; the completed response paints the answer
// once, while its thinking payload paints memory hits.
{
  const h = load();
  h.send('stream this');
  const cycleId = h.sent[0].cycle_id;

  h.recv({ kind: 'chat.reply_delta', cycle_id: cycleId, seq: 0, text: 'partial' });

  assert.equal(h.blocks[0].copy.textContent, '');
  assert.equal(h.blocks[0].state.textContent, 'answering');
}

// Admission failures terminate the same waiting block visibly.
{
  const h = load();
  h.send('reject this');
  h.recv({ kind: 'chat.rejected', cycle_id: h.sent[0].cycle_id, error: 'chat_refused' });

  assert.equal(h.rendered.length, 2);
  assert.equal(h.rendered[1].block, h.blocks[0]);
  assert.equal(h.rendered[1].result.ok, false);
  assert.equal(h.blocks[0].resolved, true);
}

// Tool progress remains available as structured status, but model prose is
// never copied into the thinking box.
{
  const trace = { narration: [], reasoning: [], calls: [], progress: [], results: [] };
  const h = load({
    _renderMarkdown: text => '<md>' + text + '</md>',
    traceNarration: (block, text) => trace.narration.push({ block, text }),
    traceReasoning: (block, text) => trace.reasoning.push({ block, text }),
    traceToolCall: (block, env) => trace.calls.push({ block, env }),
    traceToolProgress: (block, env) => trace.progress.push({ block, env }),
    traceToolResult: (block, env) => trace.results.push({ block, env }),
  });
  h.send('write hello.txt');
  const cycleId = h.sent[0].cycle_id;
  const block = h.blocks[0];

  h.recv({ kind: 'chat.reasoning_delta', cycle_id: cycleId, seq: 0, text: 'need a file' });
  assert.equal(trace.reasoning.length, 0);
  assert.equal(block.state.textContent, 'searching memory');

  h.recv({ kind: 'chat.reply_delta', cycle_id: cycleId, seq: 0, text: 'Let me write it. ' });
  assert.equal(block.copy.textContent, '', 'streamed prose stays out of the thinking box');
  assert.equal(block.state.textContent, 'answering');

  h.recv({
    kind: 'chat.tool_call', cycle_id: cycleId, seq: 1, call_id: 'c1',
    tool: 'write_file', args: { path: 'hello.txt', content: 'hi' },
  });
  assert.equal(trace.narration.length, 0, 'the streamed prose never becomes thought narration');
  assert.equal(trace.calls.length, 1);
  assert.equal(trace.calls[0].env.tool, 'write_file');
  assert.equal(block.copy.textContent, 'working: write_file');
  assert.equal(block.state.textContent, 'using write_file');

  h.recv({
    kind: 'chat.tool_result', cycle_id: cycleId, seq: 2, call_id: 'c1', tool: 'write_file',
    ok: true, output: 'wrote hello.txt', diff: '--- a/hello.txt\n+++ b/hello.txt\n@@ -0,0 +1 @@\n+hi', duration_ms: 3,
  });
  assert.equal(trace.results.length, 1);
  assert.equal(trace.results[0].env.diff.split('\n').pop(), '+hi');

  h.recv({
    kind: 'chat.tool_call', cycle_id: cycleId, seq: 3, call_id: 'c2', tool: 'run_command',
    args: { command: 'run_tests' },
  });
  h.recv({
    kind: 'chat.tool_progress', cycle_id: cycleId, seq: 4, call_id: 'c2', tool: 'run_command',
    message: 'still running · 5s elapsed', elapsed_s: 5,
  });
  assert.equal(trace.progress.length, 1);
  assert.equal(trace.progress[0].env.message, 'still running · 5s elapsed');
  h.recv({
    kind: 'chat.tool_result', cycle_id: cycleId, seq: 5, call_id: 'c2', tool: 'run_command',
    ok: true, output: 'checks passed', duration_ms: 5200,
  });

  h.recv({
    kind: 'chat.tool_call', cycle_id: cycleId, seq: 6, call_id: 'c3', tool: 'browser_control',
    args: { action: 'connect' },
  });
  h.recv({
    kind: 'chat.tool_result', cycle_id: cycleId, seq: 7, call_id: 'c3', tool: 'browser_control',
    ok: false, error: 'browser startup required',
    browser_setup: { status: 'startup_required', action: 'restart_omega' },
  });
  assert.equal(trace.results.length, 3);
  assert.deepEqual(trace.results[2].env.browser_setup, { status: 'startup_required', action: 'restart_omega' });
  assert.equal(block.copy.textContent, 'Browser setup: startup required · restart Omega');
  assert.equal(block.state.textContent, 'browser setup: startup required');

  h.recv({
    kind: 'chat.browser_foldback', cycle_id: cycleId, seq: 8,
    message: 'Omega: browser observation received; checking the next step.',
  });
  assert.equal(block.copy.textContent, 'Omega: browser observation received; checking the next step.');
  assert.equal(block.state.textContent, 'browser: evaluating next step');

  // the next pass restarts at seq 0: it is a fresh utterance, not a continuation
  h.recv({ kind: 'chat.reply_delta', cycle_id: cycleId, seq: 0, text: 'Done: ' });
  h.recv({ kind: 'chat.reply_delta', cycle_id: cycleId, seq: 1, text: '`hello.txt` written.' });
  assert.equal(block.copy.textContent, 'Omega: browser observation received; checking the next step.');

  h.recv({
    kind: 'chat.reply', cycle_id: cycleId, text: 'Done: `hello.txt` written.',
    delivery: { text: 'Done: `hello.txt` written.', rendered_by: 'omega_async' },
    thinking: { steps: [{ call_id: 'c1', tool: 'write_file', ok: true }], reasoning: 'need a file' },
  });
  assert.equal(h.rendered.length, 2);
  assert.equal(h.rendered[1].result.thinking.steps[0].tool, 'write_file');
  assert.equal(block.resolved, true);

  // frames for a cycle this client does not own are ignored, never thrown on
  h.recv({ kind: 'chat.tool_call', cycle_id: 'someone-else', call_id: 'zz', tool: 'run_command' });
  assert.equal(trace.calls.length, 3);
}

console.log('chat socket single-response tests passed');

// A server-authorized continuation gets its own block in the same feed.
{
  const h = load();
  h.recv({ kind: 'chat.reply_delta', cycle_id: 'next', job_id: 'job-a',
    job_continuation: true, pipeline_id: 3, seq: 0, text: 'continuing' });
  assert.equal(h.blocks.length, 1);
  assert.match(h.blocks[0].seed, /job-a/);
  assert.equal(h.blocks[0].copy.textContent, '');
  assert.equal(h.blocks[0].pipeline.textContent, 'P3');
  h.recv({ kind: 'chat.reply', cycle_id: 'next', job_id: 'job-a',
    job_continuation: true, text: 'done' });
  assert.equal(h.rendered.at(-1).block, h.blocks[0]);
  h.recv({ kind: 'chat.tool_call', cycle_id: 'next', job_id: 'job-a',
    job_continuation: true, tool: 'late duplicate' });
  assert.equal(h.blocks.length, 1, 'late progress must not recreate a terminal block');
}

// The actual shared renderer owns the terminal cycle claim. A socket final can
// win while a durable poll is in flight; the later poll and a repeated socket
// final must neither repaint nor speak. Poll recovery also applies P2 at this
// common boundary.
{
  const lines = [];
  const spoken = [];
  const sent = [];
  let onMessage = null;
  let nextCycle = 99;
  let resolvePoll;
  const pollResponse = new Promise(resolve => { resolvePoll = resolve; });
  const block = {
    pipeline: { textContent: '', hidden: true },
    classList: { add() {}, remove() {} },
    querySelector(selector) {
      if (selector === '.thinking-pipeline') return this.pipeline;
      if (selector === '.thinking-state') return { textContent: '' };
      if (selector === '.thinking-pending-copy') return { remove() {} };
      return null;
    },
  };
  const ChatBridge = {
    send() {},
    onMessage(fn) { onMessage = fn; return () => {}; },
    __socketOpen: () => true,
    __sendRaw(obj) { sent.push(obj); return true; },
  };
  const ctx = {
    console, ChatBridge, _liveOrigin: true,
    crypto: { randomUUID: () => `00000000-0000-4000-8000-${String(nextCycle++).padStart(12, '0')}` },
    document: { hidden: false, getElementById: () => null },
    feed: { appendChild() {}, scrollTo() {}, scrollHeight: 0 },
    appendLine(role, text) { const line = { role, text, classList: { add() {} } }; lines.push(line); return line; },
    appendImageMessage: () => false,
    appendDocumentMessage: () => false,
    appendChatError() {},
    updateThinkingBlock() {},
    appendThinkingBlock: () => block,
    _renderMarkdown: text => text,
    _scrollFeedToBottom() {},
    _emitChatRendered() {},
    Voice: { speak(text) { spoken.push(text); } },
    setInterval() { return 0; },
    clearInterval() {},
    requestAnimationFrame(fn) { fn(); return 0; },
    setTimeout() { return 0; },
    fetch(url) {
      if (String(url).startsWith('/api/chat/responses?')) return pollResponse;
      return Promise.reject(new Error('unexpected fetch ' + url));
    },
    CustomEvent: class {}, Date, Math, JSON,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.dispatchEvent = () => true;
  vm.createContext(ctx);
  vm.runInContext(feedSource, ctx);
  ctx.appendThinkingBlock = () => block;
  ctx.updateThinkingBlock = () => {};
  vm.runInContext(source, ctx);

  ChatBridge.send({ body: 'race the recovery poll' });
  const cycleId = sent.at(-1).cycle_id;
  const pending = ctx._resolvePendingChatResponses();
  onMessage({
    kind: 'chat.reply', cycle_id: cycleId, pipeline_id: 2,
    text: 'one answer', delivery: { text: 'one answer', rendered_by: 'omega_async' },
  });
  onMessage({
    kind: 'chat.reply', cycle_id: cycleId, pipeline_id: 2,
    text: 'one answer', delivery: { text: 'one answer', rendered_by: 'omega_async' },
  });
  resolvePoll({
    ok: true,
    json: async () => ({
      found: true, ok: true, cycle_id: cycleId, pipeline_id: 2,
      message: 'one answer', delivery: { text: 'one answer', rendered_by: 'omega_async' },
    }),
  });
  await pending;

  assert.equal(lines.length, 1);
  assert.equal(spoken.length, 1);
  assert.equal(block.pipeline.textContent, 'P2');

  // A permission card is an in-flight transition, not the terminal answer.
  ctx.renderPluginPermissionCard = () => ({ parentNode: true });
  ChatBridge.send({ body: 'permission transition' });
  const permissionCycle = sent.at(-1).cycle_id;
  onMessage({
    kind: 'chat.reply', cycle_id: permissionCycle,
    plugin_permission_request: { request_id: 'request-1' },
  });
  onMessage({
    kind: 'chat.reply', cycle_id: permissionCycle, pipeline_id: 1, text: 'permission resolved',
    delivery: { text: 'permission resolved', rendered_by: 'omega_async' },
  });
  assert.equal(lines.length, 2);
  assert.equal(lines[1].text, 'permission resolved');

  // Local age must end the eternal spinner without claiming cognition is terminal;
  // keep recovery alive so the actual late reply can still resolve the turn.
  let now = 1000;
  ctx.Date = class extends Date { static now() { return now; } };
  ChatBridge.send({ body: 'long build' });
  const longCycle = sent.at(-1).cycle_id;
  ctx.fetch = async () => ({ ok: true, json: async () => ({ found: false }) });
  now += 8 * 60 * 1000;
  await ctx._resolvePendingChatResponses();
  assert.equal(lines.length, 3, 'an overdue spinner gets a visible degraded status');
  assert.match(lines[2].text, /keep checking for a late result/);
  const final = { kind: 'chat.reply', cycle_id: longCycle, text: 'late real result',
    delivery: { text: 'late real result', rendered_by: 'omega_async' } };
  onMessage(final);
  ctx.renderChatResult({ ok: true, cycle_id: longCycle, message: 'late real result', delivery: final.delivery }, block);
  assert.equal(lines.length, 4);
  assert.equal(lines[3].text, 'late real result');

  ChatBridge.send({ body: 'a cascade that fails before AUTO' });
  const failedCycle = sent.at(-1).cycle_id;
  const spokenBeforeFailure = spoken.length;
  const failed = ctx.renderChatResult({
    found: true,
    ok: false,
    cycle_id: failedCycle,
    message: 'I could not complete this turn (IntentMissing).',
    delivery: {
      text: 'I could not complete this turn (IntentMissing).',
      rendered_by: 'omega_async',
      failed: true,
    },
  }, block);
  assert.equal(failed.ok, false);
  assert.equal(lines.at(-1).text, 'I could not complete this turn (IntentMissing).');
  assert.equal(ctx._pendingChatResponses.has(failedCycle), false,
    'a stored cascade failure resolves the durable pending bubble');
  assert.equal(spoken.length, spokenBeforeFailure,
    'a failed cascade message is not spoken as a successful answer');

  ChatBridge.send({ body: 'a failed cascade reaches the live socket' });
  const socketFailedCycle = sent.at(-1).cycle_id;
  const spokenBeforeSocketFailure = spoken.length;
  onMessage({
    kind: 'chat.reply',
    cycle_id: socketFailedCycle,
    text: 'I could not complete this turn (MetaBFailure).',
    delivery: {
      text: 'I could not complete this turn (MetaBFailure).',
      rendered_by: 'omega_async',
      failed: true,
    },
  });
  assert.equal(lines.at(-1).text, 'I could not complete this turn (MetaBFailure).');
  assert.equal(ctx._pendingChatResponses.has(socketFailedCycle), false);
  assert.equal(spoken.length, spokenBeforeSocketFailure,
    'the live socket renderer recognizes the persisted failed state');

  const before = lines.length;
  onMessage({ kind: 'chat.reply_delta', cycle_id: 'real-continuation', job_id: 'job-a',
    job_continuation: true, text: 'work', seq: 0 });
  onMessage({ kind: 'chat.reply', cycle_id: 'real-continuation', job_id: 'job-a',
    job_continuation: true, text: 'continued result' });
  onMessage({ kind: 'chat.reply', cycle_id: 'real-continuation', job_id: 'job-a',
    job_continuation: true, text: 'continued result' });
  assert.equal(lines.length, before + 1, 'actual renderer claims continuation terminal once');
}

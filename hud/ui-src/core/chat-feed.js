// Chat feed helpers + queued response polling (hud/ui-src/core/chat-feed.js)
// Depends on: feed, appendLine, _clipChatText, _chatLabel, _thinkingFromResult

// ── the feed is a FIFO ─────────────────────────────────────────────────────
// Operator, 2026-09-20: "they need to append to the hud as a fifo setup." Nothing ever
// left the feed, so a long session accumulated every entry it had ever painted and the
// scroll container grew without limit, dragging each repaint with it. The cap is on
// ENTRIES, not pixels: appendToFeed is the one door every writer uses, and the oldest
// entries leave as new ones arrive.
const CHAT_FEED_MAX_ENTRIES = 120;
// A lexical `const` is not a window property, and the cap is the number the harness
// and any future panel must read rather than restate.
if (typeof window !== 'undefined') window.CHAT_FEED_MAX_ENTRIES = CHAT_FEED_MAX_ENTRIES;
// The single "older messages left" line, kept at the top of the feed.
let _feedTrimNote = null;

function _feedEntries(root) {
  return Array.prototype.filter.call(
    root.children || [], node => !(node && node.dataset && node.dataset.feedNotice === 'true'));
}

// A turn still waiting for its answer is never evicted: its block is the paint target
// both the reply rail and the response poll hold, and a detached one paints nowhere.
function _feedEntryIsEvictable(node) {
  if (!node) return false;
  if (String(node.className || '').split(/\s+/).indexOf('is-pending') >= 0) return false;
  for (const block of _pendingChatResponses.values()) if (block === node) return false;
  return true;
}

// Eviction is visible. A feed that silently drops history looks like a feed that LOST
// it, so one line says what left and that memory still holds the turn.
function _noteFeedTrim(root, count) {
  const attached = _feedTrimNote
    && Array.prototype.indexOf.call(root.children || [], _feedTrimNote) >= 0;
  if (!attached) {
    _feedTrimNote = document.createElement('div');
    _feedTrimNote.className = 'feed-trimmed';
    _feedTrimNote.dataset.feedNotice = 'true';
    _feedTrimNote.dataset.feedTrimmed = '0';
  }
  if (root.children && root.children[0] !== _feedTrimNote) {
    root.insertBefore(_feedTrimNote, root.children[0] || null);
  }
  const total = (Number(_feedTrimNote.dataset.feedTrimmed) || 0) + count;
  _feedTrimNote.dataset.feedTrimmed = String(total);
  _feedTrimNote.textContent = total === 1
    ? '1 earlier message left the feed — the turn itself is in memory.'
    : total + ' earlier messages left the feed — the turns themselves are in memory.';
}

function trimChatFeed(target) {
  const root = target || feed;
  if (!root || !root.children) return 0;
  let evicted = 0;
  for (;;) {
    const entries = _feedEntries(root);
    if (entries.length <= CHAT_FEED_MAX_ENTRIES) break;
    // Oldest first, skipping live turns — a stuck pending block must not freeze the cap.
    const oldest = entries.find(_feedEntryIsEvictable);
    if (!oldest) break;
    root.removeChild(oldest);
    evicted += 1;
  }
  if (evicted) _noteFeedTrim(root, evicted);
  return evicted;
}

// The one door into the chat feed. Every module appends through this.
function appendToFeed(node, target) {
  const root = target || feed;
  if (!root || !node) return node;
  root.appendChild(node);
  trimChatFeed(root);
  return node;
}

function appendThinkingBlock(seedText) {
  const block = document.createElement('div');
  block.className = 'thinking-block is-pending';
  block.dataset.thinkingStartedAt = String(Date.now());
  block.innerHTML = [
    '<details class="thinking-toggle" open>',
    '<summary class="thinking-toggle-summary">',
    '<span class="thinking-chevron" aria-hidden="true"></span>',
    '<span class="thinking-title"><span class="thinking-pulse"></span><span class="thinking-title-label">thinking</span><span class="thinking-duration"></span><span class="thinking-pipeline" hidden></span></span>',
    '<span class="thinking-state">searching memory</span>',
    '</summary>',
    '<div class="thinking-body">',
    '<div class="thinking-pending-copy">looking for recalled matches…</div>',
    // Structured tool work may appear here while the answer is running. Model
    // prose never does: the completed response supplies verified memory hits
    // below, and the assistant answer is painted once in the chat feed.
    '<div class="thinking-trace" hidden></div>',
    '<details class="thinking-details" open>',
    '<summary class="thinking-summary">Memory hits used</summary>',
    '<div class="thinking-memories"><div class="thinking-empty">checking live memory</div></div>',
    '</details>',
    '</div>',
    '</details>',
  ].join('');
  if (seedText) block.dataset.query = _clipChatText(seedText, 160);
  appendToFeed(block);

  const detailsEl = block.querySelector('.thinking-details');
  if (detailsEl) {
    detailsEl.addEventListener('toggle', () => {
      _scrollFeedToBottom();
    });
  }

  const toggleEl = block.querySelector('.thinking-toggle');
  if (toggleEl) {
    toggleEl.addEventListener('toggle', () => {
      toggleEl.dataset.userToggled = 'true';
      _scrollFeedToBottom();
    });
  }

  _scrollFeedToBottom();
  return block;
}

function _thinkingDuration(block) {
  const started = Number(block && block.dataset && block.dataset.thinkingStartedAt);
  if (!Number.isFinite(started) || started <= 0) return '0.00';
  return Math.max(0, (Date.now() - started) / 1000).toFixed(2);
}

function _scrollFeedToBottom() {
  // Pin synchronously, then refine after layout settles. Temporarily forcing
  // `scrollBehavior:auto` avoids the smooth-scroll animation that Edge may
  // throttle while the HUD is backgrounded; requestAnimationFrame is only a
  // refinement, never the sole pin.
  if (!feed) return;
  const pin = () => {
    const height = Number(feed.scrollHeight) || 0;
    const viewport = Number(feed.clientHeight) || 0;
    const top = Math.max(0, height - viewport);
    const style = feed.style;
    const previousBehavior = style && style.scrollBehavior;
    if (style) style.scrollBehavior = 'auto';
    try {
      if (typeof feed.scrollTo === 'function') {
        feed.scrollTo({ top, behavior: 'auto' });
      } else {
        feed.scrollTop = top;
      }
    } catch (_) {
      feed.scrollTop = top;
    } finally {
      if (style) style.scrollBehavior = previousBehavior || '';
    }
  };
  pin();
  // Re-pin once layout settles — markdown, images and the thinking block all
  // grow after the first paint.
  requestAnimationFrame(() => {
    pin();
    requestAnimationFrame(pin);
  });
  // Timers DO fire while hidden; this is the guarantee when rAF never runs.
  setTimeout(pin, 60);
}

// ── the trace: structured tool calls and their results ─────────────────────
// Everything here is display: the durable record is the finished reply, whose
// thinking.steps repaints the same rows when the live frames were missed.
function _traceEl(block, reveal) {
  if (!block || typeof block.querySelector !== 'function') return null;
  const trace = block.querySelector('.thinking-trace');
  if (trace && reveal) trace.hidden = false;
  return trace;
}

function _traceMarkdown(text) {
  const raw = String(text == null ? '' : text);
  return (typeof _renderMarkdown === 'function') ? _renderMarkdown(raw) : _escapeHtml(raw);
}

function _traceArgs(args) {
  if (!args || typeof args !== 'object') return '';
  const parts = [];
  for (const key of Object.keys(args)) {
    let value = args[key];
    if (typeof value !== 'string') { try { value = JSON.stringify(value); } catch (_) { value = String(value); } }
    value = String(value).replace(/\s+/g, ' ');
    if (value.length > 160) value = value.slice(0, 157) + '…';
    parts.push(key + '=' + value);
  }
  return parts.join('  ');
}

// The task's plan, as a checklist. The agent lane runs one tool batch per episode and
// carries its plan on the job record, so the plan is real structured state: the rows
// ride the tool result (auto_loop/execute/run.py) and are drawn, never re-parsed out of
// the host's rendered text.
function _renderPlanChecklist(plan) {
  const rows = (Array.isArray(plan) ? plan : []).filter(row => row && row.text);
  if (!rows.length) return '';
  const done = rows.filter(row => row.done).length;
  const complete = done === rows.length;
  let html = '<div class="thinking-plan' + (complete ? ' is-complete' : '') + '">'
    + '<div class="thinking-plan-head">task plan · ' + done + ' / ' + rows.length + ' done</div>'
    + '<ol class="thinking-plan-list">';
  for (const row of rows) {
    html += '<li class="thinking-plan-step' + (row.done ? ' is-done' : '') + '">'
      + '<span class="thinking-plan-mark" aria-hidden="true">' + (row.done ? '✓' : '○') + '</span>'
      + '<span class="thinking-plan-text">' + _escapeHtml(String(row.text)) + '</span></li>';
  }
  return html + '</ol></div>';
}

function traceToolCall(block, call) {
  const trace = _traceEl(block, true);
  if (!trace || !call) return null;
  const row = document.createElement('div');
  row.className = 'thinking-tool is-pending';
  row.dataset.callId = String(call.call_id || '');
  const name = String(call.tool || 'tool');
  const argsText = _traceArgs(call.args);
  row.innerHTML = '<div class="thinking-tool-head">'
    + '<span class="thinking-tool-arrow">→</span>'
    + '<span class="thinking-tool-name"></span>'
    + '<span class="thinking-tool-args"></span>'
    + '<span class="thinking-tool-status">running</span>'
    + '</div>'
    + '<div class="thinking-tool-body" hidden></div>';
  row.querySelector('.thinking-tool-name').textContent = name;
  row.querySelector('.thinking-tool-args').textContent = argsText;
  trace.appendChild(row);
  _scrollFeedToBottom();
  return row;
}

function traceToolResult(block, result) {
  const trace = _traceEl(block, true);
  if (!trace || !result) return null;
  const id = String(result.call_id || '');
  let row = null;
  const rows = trace.querySelectorAll('.thinking-tool');
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    if (rows[i].dataset.callId === id) { row = rows[i]; break; }
  }
  if (!row) row = traceToolCall(block, { call_id: id, tool: result.tool, args: result.args });
  if (!row) return null;
  const ok = result.ok !== false;
  row.classList.remove('is-pending');
  row.classList.add(ok ? 'is-ok' : 'is-failed');
  const status = row.querySelector('.thinking-tool-status');
  if (status) {
    const ms = Number(result.duration_ms || 0);
    status.textContent = (ok ? 'done' : 'failed') + (ms ? ' · ' + (ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : ms + 'ms') : '');
  }
  const body = row.querySelector('.thinking-tool-body');
  if (body) {
    let html = '';
    const error = String(result.error || '').trim();
    if (error) html += '<div class="thinking-tool-error">' + _escapeHtml(error) + '</div>';
    const diff = String(result.diff || '').trim();
    if (diff) {
      html += (typeof _renderCodeBlock === 'function')
        ? _renderCodeBlock(diff, 'diff')
        : '<pre class="thinking-tool-output">' + _escapeHtml(diff) + '</pre>';
    }
    const plan = _renderPlanChecklist(result.plan);
    if (plan) html += plan;
    const setup = result.browser_setup;
    if (setup && typeof setup === 'object') {
      const statusText = String(setup.status || 'progress').replace(/_/g, ' ');
      const rawAction = String(setup.action || '').trim();
      const actionText = rawAction === 'restart_omega'
        ? 'restart Omega'
        : rawAction.replace(/_/g, ' ');
      const label = 'Browser setup: ' + statusText + (actionText ? ' · ' + actionText : '');
      html += '<div class="thinking-tool-progress">' + _escapeHtml(label) + '</div>';
    }
    const artifactHold = result.dev_artifact_hold;
    if (artifactHold && typeof artifactHold === 'object' && artifactHold.status !== 'not_needed') {
      const holdStatus = String(artifactHold.status || 'unknown');
      const heldCount = Number(artifactHold.held_count || 0);
      const reason = String(artifactHold.reason || '').trim();
      const label = holdStatus === 'held'
        ? 'Recorded ' + heldCount + ' file snapshot' + (heldCount === 1 ? '' : 's') + ' in episode memory.'
        : holdStatus === 'empty'
          ? 'No file snapshots were eligible for episode memory.'
          : 'Could not record changed files in episode memory' + (reason ? ': ' + reason : '.');
      html += '<div class="thinking-tool-progress">' + _escapeHtml(label) + '</div>';
    }
    const output = String(result.output || '').trim();
    // The plan REPLACES the output: the same list twice, once as a checklist and once
    // as the host's `1. [open] …` text, is noise.
    if (output && !diff && !plan) {
      html += (typeof _renderCodeBlock === 'function')
        ? _renderCodeBlock(output, 'text')
        : '<pre class="thinking-tool-output">' + _escapeHtml(output) + '</pre>';
    }
    body.innerHTML = html;
    body.hidden = !html;
  }
  _scrollFeedToBottom();
  return row;
}

function traceToolProgress(block, progress) {
  const trace = _traceEl(block, true);
  if (!trace || !progress) return null;
  const id = String(progress.call_id || '');
  let row = null;
  const rows = trace.querySelectorAll('.thinking-tool');
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    if (rows[i].dataset.callId === id) { row = rows[i]; break; }
  }
  if (!row || !row.classList.contains('is-pending')) return null;
  const message = String(progress.message || '').trim();
  if (!message) return row;
  const elapsed = Number(progress.elapsed_s || 0);
  const status = row.querySelector('.thinking-tool-status');
  if (status) status.textContent = elapsed > 0 ? 'running · ' + elapsed + 's' : 'running';
  const body = row.querySelector('.thinking-tool-body');
  if (body) {
    const line = document.createElement('div');
    line.className = 'thinking-tool-progress';
    line.textContent = message;
    body.innerHTML = '';
    body.appendChild(line);
    body.hidden = false;
  }
  _scrollFeedToBottom();
  return row;
}

// A browser-agent screenshot frame: the page as it looked after the action whose
// trace row shares the call id (or a standalone frame when no row matches).
function traceBrowserFrame(block, frame) {
  const trace = _traceEl(block, true);
  if (!trace || !frame || !frame.image) return null;
  const id = String(frame.call_id || '');
  let row = null;
  const rows = trace.querySelectorAll('.thinking-tool');
  for (let i = rows.length - 1; i >= 0; i -= 1) {
    if (rows[i].dataset.callId === id) { row = rows[i]; break; }
  }
  const card = document.createElement('figure');
  card.className = 'thinking-browser-frame' + (frame.ok === false ? ' is-failed' : '');
  card.dataset.step = String(frame.step || 0);
  const img = document.createElement('img');
  img.src = String(frame.image);
  img.alt = 'browser after ' + String(frame.tool || 'action');
  img.loading = 'lazy';
  if (frame.width) img.width = Number(frame.width);
  if (frame.height) img.height = Number(frame.height);
  card.appendChild(img);
  const caption = document.createElement('figcaption');
  const where = String(frame.title || frame.url || '').trim();
  caption.textContent = 'step ' + String(frame.step || 0) + ' · ' + String(frame.tool || 'action')
    + (where ? ' · ' + where : '');
  if (frame.url) caption.title = String(frame.url);
  card.appendChild(caption);
  if (row) {
    const body = row.querySelector('.thinking-tool-body');
    if (body) { body.appendChild(card); body.hidden = false; }
    else row.appendChild(card);
  } else {
    trace.appendChild(card);
  }
  _scrollFeedToBottom();
  return card;
}

// The finished reply repaints the safe summary and trace from thinking.steps
// only where the live frames did not already draw it (same call ids).
function _traceFromThinking(block, thinking) {
  if (!block || !thinking) return;
  const steps = Array.isArray(thinking.steps) ? thinking.steps : [];
  const trace = _traceEl(block, false);
  if (!trace) return;
  steps.forEach(step => {
    if (!step || !step.tool) return;
    const id = String(step.call_id || '');
    const drawn = Array.from(trace.querySelectorAll('.thinking-tool')).some(r => r.dataset.callId === id && !r.classList.contains('is-pending'));
    if (drawn) return;
    traceToolResult(block, {
      call_id: id, tool: step.tool, args: step.args, ok: step.ok !== false,
      output: step.output, error: step.error, diff: step.diff, duration_ms: step.duration_ms,
      plan: step.plan, browser_setup: step.browser_setup,
      dev_artifact_hold: step.dev_artifact_hold,
    });
  });
  // any row still pending when the reply lands was never answered: say so
  trace.querySelectorAll('.thinking-tool.is-pending').forEach(row => {
    row.classList.remove('is-pending');
    row.classList.add('is-failed');
    const status = row.querySelector('.thinking-tool-status');
    if (status) status.textContent = 'no result';
  });
}

function updateThinkingBlock(block, result, state) {
  if (!block) return;
  const payload = result && result.thinking ? _thinkingFromResult(result) : {
    status: state || 'degraded',
    steps: [],
    memory: { status: 'unavailable', count: 0, scope: [], snippets: [], entries: [] },
  };
  const degraded = state === 'degraded' || (result && result.ok === false);
  block.classList.remove('is-pending', 'is-resolved', 'is-degraded');
  block.classList.add(degraded ? 'is-degraded' : 'is-resolved');

  const stateEl = block.querySelector('.thinking-state');
  if (stateEl) {
    const count = payload.memory && payload.memory.count ? payload.memory.count : 0;
    const tools = result && result.thinking && Array.isArray(result.thinking.steps)
      ? result.thinking.steps.filter(s => s && s.tool).length : 0;
    const bits = [];
    if (tools) bits.push(tools + (tools === 1 ? ' tool call' : ' tool calls'));
    bits.push(count ? count + (count === 1 ? ' memory hit' : ' memory hits') : 'no memory hits');
    stateEl.textContent = bits.join(' · ');
  }
  const titleEl = block.querySelector('.thinking-title-label');
  const durationEl = block.querySelector('.thinking-duration');
  const toggleEl = block.querySelector('.thinking-toggle');
  // A turn that called tools is a build, and its trace — the plan, the calls, the diffs
  // the operator was just watching — stays on screen. Collapsing it hid all of that
  // behind a one-line summary the moment the work finished. A turn with no tool work
  // has nothing in there but memory hits, so that one still folds away.
  const workedTools = result && result.thinking && Array.isArray(result.thinking.steps)
    ? result.thinking.steps.filter(step => step && step.tool).length : 0;
  if (titleEl) {
    titleEl.textContent = degraded
      ? (workedTools ? 'Work ended after ' : 'Thought ended after ')
      : (workedTools ? 'Worked for ' : 'Thought for ');
  }
  if (durationEl) durationEl.textContent = _thinkingDuration(block) + ' seconds';
  if (toggleEl && !toggleEl.dataset.userToggled) toggleEl.open = workedTools > 0;
  const pendingCopyEl = block.querySelector('.thinking-pending-copy');
  if (pendingCopyEl) pendingCopyEl.remove();
  if (result && result.thinking) _traceFromThinking(block, result.thinking);

  const memoriesEl = block.querySelector('.thinking-memories');
  if (!memoriesEl) return;
  memoriesEl.innerHTML = '';
  const memory = payload.memory || {};
  const entries = memory.entries || memory.snippets || [];
  const scope = Array.isArray(memory.scope) ? memory.scope : [];
  const detailsEl = block.querySelector('.thinking-details');
  const summaryEl = block.querySelector('.thinking-summary');

  if (!entries.length) {
    if (detailsEl) detailsEl.style.display = 'none';
    const empty = document.createElement('div');
    empty.className = 'thinking-empty';
    empty.textContent = degraded ? 'no memory context available' : 'no recalled hits';
    memoriesEl.appendChild(empty);
    return;
  }

  if (detailsEl) detailsEl.style.display = 'block';
  if (summaryEl) summaryEl.textContent = entries.length === 1
    ? 'Memory hits used · 1 hit'
    : `Memory hits used · ${entries.length} hits`;
  entries.forEach(snippet => {
    const row = document.createElement('div');
    row.className = 'thinking-memory';
    const label = document.createElement('b');
    const labelText = snippet.label || snippet.coord || 'memory';
    label.textContent = _clipChatText(labelText, 58);
    row.appendChild(label);
    const preview = document.createElement('span');
    preview.className = 'thinking-memory-preview';
    preview.textContent = snippet.preview || '';
    row.appendChild(preview);
    memoriesEl.appendChild(row);
  });
  _scrollFeedToBottom();
}

function appendChatError(message, thinkingBlock, detail) {
  if (detail) console.warn('[HUD chat]', detail);
  updateThinkingBlock(thinkingBlock, null, 'degraded');
  const line = appendLine('assistant', message || 'I could not get a clean response from the cognition chain.');
  if (line) line.classList.add('is-soft-error');
  return line;
}

// Chat turns whose episode is QUEUED on send (an autonomous episode was
// running): cycle_id -> the live "thinking" block awaiting the async answer.
// Self-contained (plain fetch + its own timer) — shares this module's scope
// with renderChatResult.
const _pendingChatResponses = new Map();
// First-seen timestamp per pending cycle_id, so a spinner that never receives an
// answer (a chat turn that resolved into an action with no reply, or a lost
// cascade) is expired instead of hanging forever (operator 2026-07-21).
const _pendingChatSince = new Map();
const _expiredChatCycles = new Set();
if (typeof window !== 'undefined') {
  window._pendingChatResponses = _pendingChatResponses;
  window._pendingChatSince = _pendingChatSince;
}
const _PENDING_CHAT_MAX_AGE_MS = 420000; // 7 min — covers a turn queued behind a long window reflection

const _fetchingChatResponses = new Set();
// One terminal paint/speech per operator cycle across socket, direct poll, and
// orphan recovery. Process-local by design: durable delivery owns reconnects.
const _completedChatCycles = new Set();
function _claimCompletedChatCycle(cycleId) {
  if (_completedChatCycles.has(cycleId)) return false;
  _completedChatCycles.add(cycleId);
  if (_completedChatCycles.size > 2048) {
    _completedChatCycles.delete(_completedChatCycles.values().next().value);
  }
  return true;
}
async function _resolvePendingChatResponses() {
  if (!_pendingChatResponses.size) return;
  for (const [cycleId, block] of Array.from(_pendingChatResponses.entries())) {
    if (_fetchingChatResponses.has(cycleId)) continue;
    _fetchingChatResponses.add(cycleId);
    try {
      const r = await fetch('/api/chat/responses?cycle_id=' + encodeURIComponent(cycleId), { cache: 'no-store' });
      if (!r.ok) {
        _fetchingChatResponses.delete(cycleId);
        continue;
      }
      const data = await r.json();
      if (data && data.found) {
        _fetchingChatResponses.delete(cycleId);
        renderChatResult(data, block);  // paints + speaks in one turn
      } else {
        _fetchingChatResponses.delete(cycleId);
        const since = _pendingChatSince.get(cycleId) || 0;
        if (since && Date.now() - since > _PENDING_CHAT_MAX_AGE_MS) {
          // Stop showing an eternal spinner while retaining recovery for a genuinely late
          // durable answer. The visible failure is emitted once; if a final arrives later,
          // renderChatResult can still replace this state because the cycle is not claimed.
          if (!_expiredChatCycles.has(cycleId)) {
            _expiredChatCycles.add(cycleId);
            updateThinkingBlock(block, { error: 'answer still pending after 7 minutes' }, 'degraded');
            const titleEl = block && block.querySelector('.thinking-title-label');
            if (titleEl) titleEl.textContent = 'Still waiting after ';
            const stateEl = block && block.querySelector('.thinking-state');
            if (stateEl) stateEl.textContent = 'no answer after 7 minutes · still checking';
            const line = appendLine(
              'assistant',
              'No answer has arrived after 7 minutes. I will keep checking for a late result.'
            );
            if (line) line.classList.add('is-soft-error');
          }
        }
      }
    } catch (_e) {
      _fetchingChatResponses.delete(cycleId);
      /* non-fatal — the next tick retries */
    }
  }
}
// Orphan sweep (operator ruling 2026-07-19): a queued reply whose bubble
// registration was lost (HUD reload, hidden tab, send path with no thinking
// block) must STILL fold through to the chat window. The server returns
// undelivered rows past a 10s age floor — the bubble rail wins the race for
// live turns; the sweep catches everything it dropped.
async function _sweepOrphanChatResponses() {
  try {
    const r = await fetch('/api/chat/responses/orphans', { cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    for (const rec of (data && data.responses) || []) {
      const block = _pendingChatResponses.get(rec.cycle_id) || null;
      renderChatResult(rec, block); // null block paints a plain assistant line
    }
  } catch (_e) { /* non-fatal — the next tick retries */ }
}

setInterval(() => {
  if (document.hidden) return;
  _resolvePendingChatResponses();
  _sweepOrphanChatResponses();
}, 4000);

function isChatResultTerminal(result) {
  const permissionRequest = result && result.plugin_permission_request;
  const pendingPermission = !!(result && /pending_input/i.test(String(result.error || '')));
  return !(permissionRequest || pendingPermission);
}
if (typeof window !== 'undefined') window.isChatResultTerminal = isChatResultTerminal;

function renderChatResult(result, thinkingBlock) {
  const delivery = (result && result.delivery) || {};
  const textOut = (delivery && delivery.text) || (result && result.message) || '';
  const hasArtifact = !!(result && (result.image || result.document));
  // The acknowledgement can arrive through the socket, direct POST, or orphan
  // recovery. Each rail starts the same correlated specialist update stream.
  if (result && result.cycle_id && result.lane_work
      && typeof window !== 'undefined'
      && typeof window.startChatLaneUpdatePolling === 'function') {
    window.startChatLaneUpdatePolling(result.cycle_id, 0);
  }
  // QUEUED: the message ignites the NEXT episode (an autonomous one was
  // mid-run). Keep the clean "thinking" spinner and poll on the cycle_id
  // until the answer lands — NEVER fall through to the degraded/error state.
  if (result && result.queued === true && result.cycle_id && !textOut && !hasArtifact) {
    if (!_pendingChatSince.has(result.cycle_id)) _pendingChatSince.set(result.cycle_id, Date.now());
    if (thinkingBlock) {
      const stateEl = thinkingBlock.querySelector('.thinking-state');
      if (stateEl) stateEl.textContent = 'waiting for answer';
      _pendingChatResponses.set(result.cycle_id, thinkingBlock);
    } else {
      // No bubble to resolve (proactive-answer sends, panel paths): still
      // register — the poll claims the answer and paints a plain assistant
      // line. Dropping it here orphaned replies (live miss 2026-07-19).
      _pendingChatResponses.set(result.cycle_id, null);
    }
    return { text: '', ok: true, queued: true };
  }
  const permissionRequest = result && result.plugin_permission_request;
  const pendingPermission = !isChatResultTerminal(result);
  if ((permissionRequest || pendingPermission) && typeof renderPluginPermissionCard === 'function') {
    const attachCard = request => {
      if (!request) return;
      const card = renderPluginPermissionCard(request);
      if (feed && card && !card.parentNode) appendToFeed(card);
      if (request.request_id && typeof bindPluginPermissionTask === 'function') {
        bindPluginPermissionTask(request.request_id, result.cycle_id, thinkingBlock);
      }
      if (typeof _scrollFeedToBottom === 'function') _scrollFeedToBottom();
    };
    if (permissionRequest) attachCard(permissionRequest);
    else if (pendingPermission && typeof refreshPendingPluginPermissions === 'function') {
      Promise.resolve(refreshPendingPluginPermissions());
    }
    return { text: '', ok: true, queued: true };
  }
  // A pushed WebSocket reply and both durable recovery rails are transports for
  // the same completed response. Claim at their common render boundary so an
  // already in-flight poll cannot repaint or speak after the socket wins.
  if (result && result.cycle_id) {
    if (!_claimCompletedChatCycle(result.cycle_id)) {
      return { text: '', ok: true, duplicate: true };
    }
    _pendingChatResponses.delete(result.cycle_id);
    _pendingChatSince.delete(result.cycle_id);
    _expiredChatCycles.delete(result.cycle_id);
    _fetchingChatResponses.delete(result.cycle_id);
  }
  if (thinkingBlock && result && Number.isInteger(result.pipeline_id)) {
    const badge = thinkingBlock.querySelector('.thinking-pipeline');
    if (badge) {
      badge.hidden = false;
      badge.textContent = 'P' + String(result.pipeline_id);
    }
  }
  if (result && result.cycle_id && typeof settlePluginPermissionByCycle === 'function') {
    settlePluginPermissionByCycle(result.cycle_id, result);
  }
  const renderedBy = (delivery && delivery.rendered_by) || '';
  if (renderedBy === 'trusted_folders' && typeof window !== 'undefined'
      && window.OmegaTrustedFolders && typeof window.OmegaTrustedFolders.refresh === 'function') {
    // /trust, /cd and /untrust from the chat land in the Settings workspace list at once.
    try { window.OmegaTrustedFolders.refresh(); } catch (_) { /* the panel may not be open */ }
  }
  const apiOk = !(result && (result.ok === false || delivery.failed === true));
  const errMsg = (result && result.error) || '';
  updateThinkingBlock(thinkingBlock, result, apiOk ? 'resolved' : 'degraded');
  let painted = null;
  let artifactPainted = false;
  let paintedLine = null;
  if (textOut) {
    if (window._lastStreamedLine && window._lastStreamedLine.parentNode) {
      const line = window._lastStreamedLine;
      line.className = 'line assistant';
      line.innerHTML = _renderMarkdown(textOut);
      if (!apiOk || renderedBy === 'fallback') line.classList.add('is-soft-error');
      window._lastStreamedLine = null;
      _scrollFeedToBottom();
      painted = { text: textOut, ok: apiOk, renderedBy };
      paintedLine = line;
    } else {
      const line = appendLine('assistant', textOut, true);
      if (!apiOk || renderedBy === 'fallback') line.classList.add('is-soft-error');
      _scrollFeedToBottom();
      painted = { text: textOut, ok: apiOk, renderedBy };
      paintedLine = line;
    }
    // The agent's own question, with the choices it offered: clicking one sends it as the
    // operator's answer, which resumes the paused task. Typing an answer in the composer works
    // just as well — the answer is a chat turn either way (operator 2026-09-15).
    const agentQuestion = result && result.operator_question;
    if (agentQuestion && Array.isArray(agentQuestion.options) && agentQuestion.options.length
        && typeof document !== 'undefined') {
      const card = document.createElement('div');
      card.className = 'agent-question-card';
      const list = document.createElement('ol');
      list.className = 'agent-question-options';
      agentQuestion.options.forEach(option => {
        const item = document.createElement('li');
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'agent-question-option';
        button.textContent = String(option);
        button.addEventListener('click', () => {
          const answer = String(option);
          card.remove();
          appendLine('user', answer);
          if (typeof window !== 'undefined' && window.ChatBridge && window.ChatBridge.send) {
            window.ChatBridge.send({ kind: 'chat.user_message', source: 'user', body: answer });
          }
          _scrollFeedToBottom();
        });
        item.appendChild(button);
        list.appendChild(item);
      });
      card.appendChild(list);
      const own = document.createElement('div');
      own.className = 'agent-question-own';
      own.textContent = 'or type your own answer below';
      card.appendChild(own);
      if (feed) appendToFeed(card);
      _scrollFeedToBottom();
    }
    // Speak in the SAME turn as the bubble paint so Kokoro synthesis starts
    // immediately (not after a lagged event listener / second poll tick).
    // Voice pill OFF → Voice.speak no-ops (master gate in voice-runtime).
    if (painted.ok && painted.renderedBy && painted.renderedBy !== 'fallback'
        && painted.text
        && !(delivery && delivery.interrupted)
        && typeof Voice !== 'undefined' && Voice.speak) {
      try { Voice.speak(painted.text, { cycleId: result.cycle_id }); } catch (_tts) { /* TTS optional */ }
    }
  }
  if (result && result.image && typeof appendImageMessage === 'function') {
    artifactPainted = !!appendImageMessage(result) || artifactPainted;
  }
  if (result && result.document && typeof appendDocumentMessage === 'function') {
    artifactPainted = !!appendDocumentMessage(result) || artifactPainted;
  }
  if (painted) {
    if (artifactPainted) painted.artifact = true;
    if (result && result.lane_work && result.cycle_id
        && typeof window !== 'undefined'
        && typeof window.chatLaneAcknowledgementRendered === 'function') {
      window.chatLaneAcknowledgementRendered(result.cycle_id);
    }
    return painted;
  }
  if (artifactPainted) {
    if (result && result.lane_work && result.cycle_id
        && typeof window !== 'undefined'
        && typeof window.chatLaneAcknowledgementRendered === 'function') {
      window.chatLaneAcknowledgementRendered(result.cycle_id);
    }
    return { text: '', ok: apiOk, renderedBy, artifact: true };
  }
  appendChatError('I could not get a clean response from the cognition chain.', thinkingBlock, errMsg);
  return { text: '', ok: false, renderedBy };
}

// C2 rehydration RETIRED (operator 2026-07-12): even the notices-only replay
// cluttered the main panel with prior-session ✅/⛔ lines on every boot — and
// the ring itself is now cleared at boot (boot.py). The chat panel is
// IN-SESSION ONLY; durable outcomes and autonomous notices live in the right
// proactive pinboard, while build progress remains on the live chat rails.

// Live ChatBridge override — operator chat enters the one durable cognition queue.
// Depends on: appendThinkingBlock, renderChatResult, appendChatError, appendLine, Telemetry
// Runs inside HUD LIVE WIRING IIFE; `_liveOrigin` is in enclosing scope.
//
// The same live-chat route owns both input and output: composer text enters `/api/chat`,
// the durable FIFO runs Mistral AUTO, and the response rail carries the final result.
(function () {
  function _emitChatRendered(rendered, meta) {
    if (typeof window === 'undefined' || typeof window.dispatchEvent !== 'function') return;
    try {
      window.dispatchEvent(new CustomEvent('omega:chat-rendered', {
        detail: { rendered: rendered || null, meta: meta || {} },
      }));
    } catch (_) { /* non-fatal */ }
  }
  try {
    const input = document.getElementById('input');
    if (input && !input.dataset.defaultPlaceholder) {
      input.dataset.defaultPlaceholder = input.getAttribute('placeholder') || '';
    }
  } catch (_) { /* composer optional */ }
  // ── one queued chat turn (AGENTS.md §4.0) ─────────────────────────────────
  // The HUD mints the cycle_id before transport and binds the thinking block
  // immediately. No acknowledgement frame is needed to name the turn, and the
  // only frame allowed to resolve it is the actual reply from AUTO.
  const _blockByCycle = new Map();

  function _claimBlock(cycleId) {
    if (cycleId && _blockByCycle.has(cycleId)) {
      const block = _blockByCycle.get(cycleId);
      _blockByCycle.delete(cycleId);
      return block;
    }
    return null;
  }

  function _blockForResult(cycleId, result) {
    if (typeof isChatResultTerminal === 'function' && !isChatResultTerminal(result)) {
      return _blockByCycle.get(cycleId) || null;
    }
    return _claimBlock(cycleId);
  }

  // ── the reply as it is being written ─────────────────────────────────────
  // `chat.reply_delta` is transport progress only. The final `chat.reply`
  // remains the authority for the answer and its memory-hit payload; painting
  // deltas into the thought box duplicates the answer beside the final line.
  const _finishedContinuationCycles = new Set();
  const _streamLines = new Map();

  // Specialist work remains in the one operator conversation. Socket updates
  // are the live rail; this durable poll resumes updates after a reconnect or
  // HUD reload and deduplicates the two transports by update_id.
  const _laneUpdatePolls = new Map();
  const _closedLaneCycles = new Set();

  function _lanePoll(cycleId) {
    const cycle = String(cycleId || '');
    if (!cycle || _closedLaneCycles.has(cycle)) return null;
    let poll = _laneUpdatePolls.get(cycle);
    if (!poll) {
      poll = {
        afterSeq: 0, timer: null, inFlight: false, done: false,
        ackRendered: false, ackSentSeq: 0, ackInFlight: false, ackTimer: null,
        seen: new Set(), received: new Map(), withheld: [], latestStatus: 'queued',
      };
      _laneUpdatePolls.set(cycle, poll);
      if (_laneUpdatePolls.size > 2048) {
        for (const [oldCycle, oldPoll] of _laneUpdatePolls) {
          if (oldPoll.done && oldPoll.ackRendered && oldPoll.ackSentSeq >= oldPoll.afterSeq) {
            _laneUpdatePolls.delete(oldCycle);
            _rememberClosedLaneCycle(oldCycle);
          }
          if (_laneUpdatePolls.size <= 2048) break;
        }
      }
    }
    return poll;
  }

  function _paintLaneUpdate(row, fallbackLane) {
    const text = String(row.text || '').trim();
    if (!text || typeof appendLine !== 'function') return;
    const line = appendLine('assistant', text);
    if (line && line.dataset) {
      line.dataset.lane = String(row.lane || fallbackLane || '');
      line.dataset.laneUpdateId = String(row.update_id || '');
    }
  }

  function _rememberClosedLaneCycle(cycle) {
    _closedLaneCycles.add(cycle);
    if (_closedLaneCycles.size > 4096) {
      _closedLaneCycles.delete(_closedLaneCycles.values().next().value);
    }
  }

  function _ackLaneUpdates(cycleId, poll) {
    if (!poll || !poll.ackRendered || poll.ackInFlight || poll.afterSeq <= poll.ackSentSeq) return;
    const throughSeq = poll.afterSeq;
    poll.ackInFlight = true;
    let acknowledged = false;
    fetch('/api/chat/updates/ack', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ cycle_id: cycleId, through_seq: throughSeq }),
    }).then(response => {
      if (!response.ok) throw new Error('chat_lane_update_ack_failed');
      poll.ackSentSeq = Math.max(poll.ackSentSeq, throughSeq);
      acknowledged = true;
      if (poll.done && poll.ackSentSeq >= poll.afterSeq) _rememberClosedLaneCycle(cycleId);
    }).catch(() => {
      poll.retryMs = Math.min(30000, Math.max(2000, (poll.retryMs || 1000) * 2));
      if (poll.ackTimer === null && typeof setTimeout === 'function') {
        poll.ackTimer = setTimeout(() => {
          poll.ackTimer = null;
          _ackLaneUpdates(cycleId, poll);
        }, poll.retryMs);
      }
    }).finally(() => {
      poll.ackInFlight = false;
      if (acknowledged && poll.afterSeq > poll.ackSentSeq) _ackLaneUpdates(cycleId, poll);
    });
  }

  function _markLaneAcknowledgementRendered(cycleId) {
    const cycle = String(cycleId || '');
    const poll = _lanePoll(cycle);
    if (!poll) return;
    poll.ackRendered = true;
    for (const row of poll.withheld.splice(0)) _paintLaneUpdate(row, '');
    _ackLaneUpdates(cycle, poll);
    if (poll.done && poll.ackSentSeq >= poll.afterSeq) _rememberClosedLaneCycle(cycle);
  }

  function _consumeLaneUpdates(cycleId, payload) {
    const cycle = String(cycleId || '');
    if (!cycle || !payload || typeof payload !== 'object') return;
    const poll = _lanePoll(cycle);
    if (!poll || poll.done) return;
    const rows = Array.isArray(payload.updates) ? payload.updates : [];
    for (const row of rows) {
      if (!row || typeof row !== 'object') continue;
      const seq = Number(row.seq) || 0;
      if (seq > poll.afterSeq) poll.received.set(seq, row);
    }
    while (poll.received.has(poll.afterSeq + 1)) {
      const row = poll.received.get(poll.afterSeq + 1);
      poll.received.delete(poll.afterSeq + 1);
      poll.afterSeq += 1;
      const updateId = String(row.update_id || '');
      poll.latestStatus = String(row.status || poll.latestStatus).toLowerCase();
      if (!updateId || poll.seen.has(updateId)) continue;
      poll.seen.add(updateId);
      if (poll.ackRendered) _paintLaneUpdate(row, payload.lane);
      else poll.withheld.push(row);
    }
    _ackLaneUpdates(cycle, poll);
    const status = String(payload.status || poll.latestStatus || '').toLowerCase();
    const lastSeq = Number(payload.last_seq)
      || Math.max(poll.afterSeq, ...rows.map(row => Number(row && row.seq) || 0));
    if ((payload.done === true || ['completed', 'needs_input', 'failed'].includes(status))
        && poll.afterSeq >= lastSeq) {
      poll.done = true;
      if (poll.timer !== null && typeof clearTimeout === 'function') clearTimeout(poll.timer);
      if (poll.ackRendered && poll.ackSentSeq >= poll.afterSeq) _rememberClosedLaneCycle(cycle);
      return;
    }
    _scheduleLaneUpdatePoll(cycle, poll);
  }

  function _scheduleLaneUpdatePoll(cycleId, poll) {
    if (!poll || poll.done || poll.timer !== null || typeof setTimeout !== 'function') return;
    poll.retryMs = Math.min(30000, Math.max(2000, (poll.retryMs || 1000) * 2));
    poll.timer = setTimeout(() => {
      poll.timer = null;
      _pollLaneUpdates(cycleId);
    }, poll.retryMs);
  }

  async function _pollLaneUpdates(cycleId) {
    const cycle = String(cycleId || '');
    const poll = _lanePoll(cycle);
    if (!poll || poll.done || poll.inFlight) return;
    poll.inFlight = true;
    try {
      const url = '/api/chat/updates?cycle_id=' + encodeURIComponent(cycle)
        + '&after_seq=' + String(poll.afterSeq);
      const response = await fetch(url, { cache: 'no-store' });
      if (!response.ok) throw new Error('chat_lane_update_poll_failed');
      const data = await response.json();
      if (data && data.found) {
        poll.retryMs = 1000;
        _consumeLaneUpdates(cycle, data);
      }
      else _scheduleLaneUpdatePoll(cycle, poll);
    } catch (_error) {
      _scheduleLaneUpdatePoll(cycle, poll);
    } finally {
      poll.inFlight = false;
    }
  }

  function _startLaneUpdatePolling(cycleId, afterSeq) {
    const poll = _lanePoll(cycleId);
    if (!poll || poll.done) return;
    poll.afterSeq = Math.max(poll.afterSeq, Number(afterSeq) || 0);
    poll.ackSentSeq = Math.max(poll.ackSentSeq, Number(afterSeq) || 0);
    _pollLaneUpdates(cycleId);
  }

  function _recoverActiveLaneUpdates() {
    fetch('/api/chat/active-lanes', { cache: 'no-store' }).then(response => {
      if (!response.ok) throw new Error('chat_active_lanes_unavailable');
      return response.json();
    }).then(data => {
      for (const lane of (data && data.lanes) || []) {
        if (!lane || !lane.cycle_id) continue;
        if (lane.acknowledged === true) _markLaneAcknowledgementRendered(lane.cycle_id);
        _startLaneUpdatePolling(lane.cycle_id, lane.acknowledged_seq || 0);
      }
    }).catch(() => { /* the next load or server event retries durable recovery */ });
  }

  if (typeof window !== 'undefined') {
    window.startChatLaneUpdatePolling = _startLaneUpdatePolling;
    window.chatLaneAcknowledgementRendered = _markLaneAcknowledgementRendered;
    if (typeof document !== 'undefined' && document.readyState === 'complete') {
      _recoverActiveLaneUpdates();
    } else if (typeof window.addEventListener === 'function') {
      window.addEventListener('load', _recoverActiveLaneUpdates);
    }
  }

  function _ensureContinuationBlock(env) {
    const cycle = String(env.cycle_id || '');
    if (env.job_continuation !== true || !cycle || !env.job_id) return;
    if (_finishedContinuationCycles.has(cycle) ||
        (typeof _completedChatCycles !== 'undefined' && _completedChatCycles.has(cycle))) return;
    if (!_blockByCycle.has(cycle) && typeof appendThinkingBlock === 'function') {
      const block = appendThinkingBlock('Continuing job ' + String(env.job_id));
      _blockByCycle.set(cycle, block);
      // Register this real continuation with the existing durable recovery rail.
      if (typeof renderChatResult === 'function') {
        renderChatResult({ ok: true, queued: true, cycle_id: cycle }, block);
      }
    }
  }

  function _applyPipelineBadge(block, env) {
    if (!block || !Number.isInteger(env.pipeline_id)) return;
    const badge = block.querySelector('.thinking-pipeline');
    if (!badge) return;
    badge.hidden = false;
    badge.textContent = 'P' + String(env.pipeline_id);
  }

  function _applyReplyDelta(env) {
    const cycleId = String(env.cycle_id || '');
    if (!cycleId) return;
    const block = _blockByCycle.get(cycleId);
    if (block) {
      _applyPipelineBadge(block, env);
      const stateEl = block.querySelector('.thinking-state');
      if (stateEl) stateEl.textContent = 'answering';
    }
    if (typeof appendLine === 'function') {
      const seq = Number(env.seq) || 0;
      let line = _streamLines.get(cycleId);
      const piece = String(env.text || '');
      if (!line || !line.parentNode || seq === 0) {
        line = appendLine('assistant', piece, false);
        _streamLines.set(cycleId, line);
        if (typeof window !== 'undefined') window._lastStreamedLine = line;
      } else {
        line.textContent = String(line.textContent || '') + piece;
      }
    }
    _scrollFeedToBottom();
  }

  // ── the work behind the reply, live ──────────────────────────────────────
  // chat.tool_call / chat.tool_result update the structured tool trace.
  // chat.reasoning_delta only updates the memory-search status; no model
  // reasoning prose crosses into the thinking box.
  function _applyToolCall(env) {
    const cycleId = String(env.cycle_id || '');
    const block = _blockByCycle.get(cycleId);
    if (!block) return;
    _applyPipelineBadge(block, env);
    const copyEl = block.querySelector('.thinking-pending-copy');
    if (copyEl) {
      if (copyEl.classList && typeof copyEl.classList.remove === 'function') copyEl.classList.remove('is-streaming');
      copyEl.textContent = 'working: ' + String(env.tool || 'tool');
    }
    const stateEl = block.querySelector('.thinking-state');
    if (stateEl) stateEl.textContent = 'using ' + String(env.tool || 'a tool');
    if (typeof traceToolCall === 'function') traceToolCall(block, env);
  }

  function _applyToolResult(env) {
    const block = _blockByCycle.get(String(env.cycle_id || ''));
    if (!block) return;
    _applyPipelineBadge(block, env);
    if (typeof traceToolResult === 'function') traceToolResult(block, env);
    const stateEl = block.querySelector('.thinking-state');
    if (stateEl) stateEl.textContent = env.ok === false ? String(env.tool || 'tool') + ' failed' : 'thinking';
    const setup = env.browser_setup;
    if (setup && typeof setup === 'object') {
      const status = String(setup.status || 'progress').replace(/_/g, ' ');
      const rawAction = String(setup.action || '').trim();
      const action = rawAction === 'restart_omega' ? 'restart Omega' : rawAction.replace(/_/g, ' ');
      const copyEl = block.querySelector('.thinking-pending-copy');
      if (copyEl) copyEl.textContent = 'Browser setup: ' + status + (action ? ' · ' + action : '');
      if (stateEl) stateEl.textContent = 'browser setup: ' + status;
    }
  }

  function _applyToolProgress(env) {
    const block = _blockByCycle.get(String(env.cycle_id || ''));
    if (!block) return;
    _applyPipelineBadge(block, env);
    if (typeof traceToolProgress === 'function') traceToolProgress(block, env);
    const stateEl = block.querySelector('.thinking-state');
    if (stateEl) stateEl.textContent = String(env.tool || 'tool') + ' · ' + String(env.message || 'running');
    _scrollFeedToBottom();
  }

  function _applyReasoningDelta(env) {
    const block = _blockByCycle.get(String(env.cycle_id || ''));
    if (!block) return;
    _applyPipelineBadge(block, env);
    // every pass's think trace appends: the trace is a record, not a bubble
    const stateEl = block.querySelector('.thinking-state');
    if (stateEl && stateEl.textContent !== 'answering') stateEl.textContent = 'searching memory';
  }

  // chat.browser_frame: the screenshot Omega's browser agent took after an action,
  // appended under that action's trace row so the operator watches the work happen
  // (operator 2026-09-15: "append those screenshots in the chat panel").
  function _applyBrowserFrame(env) {
    const block = _blockByCycle.get(String(env.cycle_id || ''));
    if (block) {
      _applyPipelineBadge(block, env);
      if (typeof traceBrowserFrame === 'function') traceBrowserFrame(block, env);
      const stateEl = block.querySelector('.thinking-state');
      if (stateEl) stateEl.textContent = 'browser: step ' + String(env.step || 0) + (env.ok === false ? ' failed' : '');
    }
    if ((!block || !block.classList || !block.classList.contains('is-pending'))
        && env.image && typeof appendToFeed === 'function') {
      const card = document.createElement('figure');
      card.className = 'line browser-frame' + (env.ok === false ? ' is-failed' : '');
      const img = document.createElement('img');
      img.src = String(env.image);
      img.alt = 'browser after ' + String(env.tool || 'action');
      card.appendChild(img);
      const caption = document.createElement('figcaption');
      caption.textContent = 'step ' + String(env.step || 0) + ' · ' + String(env.tool || 'action')
        + (env.title || env.url ? ' · ' + String(env.title || env.url) : '');
      card.appendChild(caption);
      appendToFeed(card);
    }
    _scrollFeedToBottom();
  }

  function _applyBrowserFoldback(env) {
    const block = _blockByCycle.get(String(env.cycle_id || ''));
    if (!block) return;
    _applyPipelineBadge(block, env);
    const copyEl = block.querySelector('.thinking-pending-copy');
    if (copyEl) {
      if (copyEl.classList && typeof copyEl.classList.remove === 'function') copyEl.classList.remove('is-streaming');
      copyEl.textContent = String(env.message || 'Omega: checking the next browser step…');
    }
    const stateEl = block.querySelector('.thinking-state');
    if (stateEl) stateEl.textContent = 'browser: evaluating next step';
    _scrollFeedToBottom();
  }

  function _chatSocketReady() {
    return typeof ChatBridge !== 'undefined'
      && typeof ChatBridge.__socketOpen === 'function'
      && ChatBridge.__socketOpen();
  }

  if (_liveOrigin && typeof ChatBridge !== 'undefined' && ChatBridge.onMessage) {
    ChatBridge.onMessage(env => {
      if (!env || typeof env !== 'object') return;
      if (env.kind === 'pong') return;
      _ensureContinuationBlock(env);
      if (env.kind === 'chat.rejected') {
        const result = {
          ok: false,
          cycle_id: env.cycle_id,
          error: env.error || 'chat_refused',
          plugin_permission_request: env.plugin_permission_request,
        };
        const block = _blockForResult(String(env.cycle_id || ''), result);
        if (typeof renderChatResult === 'function') {
          renderChatResult(result, block);
        }
        return;
      }
      if (env.kind === 'chat.reply_delta') {
        _applyReplyDelta(env);
        return;
      }
      if (env.kind === 'chat.tool_call') { _applyToolCall(env); return; }
      if (env.kind === 'chat.tool_progress') { _applyToolProgress(env); return; }
      if (env.kind === 'chat.tool_result') { _applyToolResult(env); return; }
      if (env.kind === 'chat.browser_frame') { _applyBrowserFrame(env); return; }
      if (env.kind === 'chat.browser_foldback') { _applyBrowserFoldback(env); return; }
      if (env.kind === 'chat.reasoning_delta') { _applyReasoningDelta(env); return; }
      if (env.kind === 'chat.update') {
        const cycleId = String(env.cycle_id || '');
        _consumeLaneUpdates(cycleId, { updates: [env], status: env.status });
        return;
      }
      if (env.kind !== 'chat.reply') return;
      // Shape it exactly like the POST result so renderChatResult needs no
      // special case — the push and the poll deliver the same thing.
      const result = {
        ok: true,
        cycle_id: env.cycle_id,
        pipeline_id: env.pipeline_id,
        job_id: env.job_id,
        message: env.text || '',
        delivery: env.delivery || { text: env.text || '', rendered_by: 'chat_socket' },
        prompt_pack: env.prompt_pack || {},
      };
      if (env.lane_work && typeof env.lane_work === 'object') {
        result.lane_work = env.lane_work;
        _startLaneUpdatePolling(env.cycle_id, 0);
      }
      if (env.thinking) result.thinking = env.thinking;
      if (env.image) result.image = env.image;
      if (env.document) result.document = env.document;
      if (env.plugin_permission_request) result.plugin_permission_request = env.plugin_permission_request;
      if (env.operator_question) result.operator_question = env.operator_question;
      if (env.workspace_assignment) result.workspace_assignment = env.workspace_assignment;
      const block = _blockForResult(String(env.cycle_id || ''), result);
      _applyPipelineBadge(block, env);
      if (typeof renderChatResult !== 'function') return;
      const rendered = renderChatResult(result, block);
      if (env.job_continuation === true &&
          (typeof isChatResultTerminal !== 'function' || isChatResultTerminal(result))) {
        _finishedContinuationCycles.add(String(env.cycle_id || ''));
        if (_finishedContinuationCycles.size > 2048) {
          _finishedContinuationCycles.delete(_finishedContinuationCycles.values().next().value);
        }
      }
      _emitChatRendered(rendered, { source: 'chat_socket' });
      // The server marks a reply delivered only on this acknowledgement of the exact
      // cycle, after it rendered. A reply that never renders stays on the orphan sweep,
      // which is also the recovery if this send fails, so a failed send is not retried here.
      if (typeof ChatBridge.__sendRaw === 'function' && ChatBridge.__socketOpen && ChatBridge.__socketOpen()) {
        try { ChatBridge.__sendRaw({ kind: 'chat.ack', cycle_id: String(env.cycle_id || '') }); }
        catch (_) { /* the reply stays pending and the orphan sweep delivers it */ }
      }
      const feedEl = document.getElementById('feed');
      if (feedEl) _scrollFeedToBottom();
    });
  }

  if (_liveOrigin && typeof ChatBridge !== 'undefined' && ChatBridge.send) {
    ChatBridge.send = function (envelope) {
      const env = Object.assign({
        kind: 'chat.user_message', source: 'user',
        cycle_id: null, ts: new Date().toISOString(),
      }, envelope || {});
      const text = (env.body || '').toString();
      if (!text.trim()) return;
      const uploads = Array.isArray(env.uploads) ? env.uploads : [];
      const uploadRef = (env.upload_ref || '').toString()
        || (uploads.length && window.OmegaUI && window.OmegaUI.uploadRefFor
          ? window.OmegaUI.uploadRefFor(uploads[0])
          : '');
      let cycleId = '';
      try {
        cycleId = crypto.randomUUID();
      } catch (e) {
        if (typeof appendChatError === 'function') {
          appendChatError('The chat lane could not create a turn identifier.', null, e && e.message);
        }
        return;
      }
      const payload = { text, cycle_id: cycleId };
      const composerInput = document.getElementById('input');
      const selectedPluginId = String(composerInput?.dataset.pluginId || '').trim();
      if (selectedPluginId) {
        payload.plugin_id = selectedPluginId;
        delete composerInput.dataset.pluginId;
      }
      if (uploadRef) payload.upload_ref = uploadRef;
      if (uploads.length) {
        payload.uploads = uploads.map(item => ({
          filename: item && (item.filename || item.name) || 'upload',
          kind: item && item.kind || 'file',
          size: item && item.size || 0,
          url: item && item.url || '',
          upload_ref: window.OmegaUI && window.OmegaUI.uploadRefFor
            ? window.OmegaUI.uploadRefFor(item)
            : '',
        }));
      }
      const thinkingBlock = (typeof appendThinkingBlock === 'function')
        ? appendThinkingBlock(text)
        : null;
      _blockByCycle.set(cycleId, thinkingBlock);
      if (typeof renderChatResult === 'function') {
        renderChatResult({
          ok: true,
          queued: true,
          cycle_id: cycleId,
          delivery: { text: '', rendered_by: 'queued' },
        }, thinkingBlock);
      }

      const dispatch = () => {
        // The shared cognition gate first (AGENTS.md §4.0). The socket carries the turn to
        // the SAME gate this POST does — handle_operator_chat -> the durable
        // cognition queue — so this is one road with two on-ramps, never a second
        // path into ignition. The win is the reply: it is PUSHED back on
        // chat.reply instead of polled from /api/chat/responses, so TTS can start
        // the moment the answer lands rather than on the next poll tick.
        if (_chatSocketReady()) {
          try {
            ChatBridge.__sendRaw(Object.assign({ kind: 'chat.user_message', text }, payload));
            return;
          } catch (e) {
            // Socket died between the readyState check and the send — fall
            // through to POST rather than dropping the operator's turn.
          }
        }

        // ONE input gate: POST /api/chat admits the turn to the shared durable FIFO.
        // Mistral AUTO alone decides whether to answer or invoke a tool after
        // the shared queue admits the turn.
        fetch('/api/chat', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(payload),
        }).then(r => r.json()).then(result => {
          if (typeof renderChatResult !== 'function') return;
          const rendered = renderChatResult(result, thinkingBlock);
          _emitChatRendered(rendered, { source: 'api_chat' });
          const feedEl = document.getElementById('feed');
          if (feedEl) _scrollFeedToBottom();
        }).catch(err => {
          if (typeof Telemetry !== 'undefined') {
            Telemetry.update({
              system: {
                bridge: {
                  state: 'fallback', last_event: 'POST /api/chat failed: ' + err.message,
                }
              }
            });
          }
          if (typeof appendChatError === 'function') {
            appendChatError('The chat bridge lost contact with the HUD server.', thinkingBlock, err && err.message);
            const feedEl = document.getElementById('feed');
            if (feedEl) _scrollFeedToBottom();
          }
        });
      };

      dispatch();
    };
  }
})();

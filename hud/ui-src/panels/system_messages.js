// Right proactive pinboard — active dialogue plus operator-cleared notices.
// Durable history is rendered separately inside Settings.
function _formatRelativeTs(iso) {
  if (!iso) return '—';
  try {
    const t = new Date(iso).getTime();
    if (!Number.isFinite(t)) return '—';
    const sec = Math.max(0, Math.floor((Date.now() - t) / 1000));
    if (sec < 60) return sec + 's ago';
    if (sec < 3600) return Math.floor(sec / 60) + 'm ago';
    if (sec < 86400) return Math.floor(sec / 3600) + 'h ago';
    return Math.floor(sec / 86400) + 'd ago';
  } catch (_e) {
    return '—';
  }
}

async function _systemMessagesFetch() {
  const response = await fetch('/api/system-messages', { cache: 'no-store' });
  if (!response.ok) throw new Error('HTTP ' + response.status);
  return response.json();
}

async function _proactiveNoticesFetch() {
  const response = await fetch('/api/proactive/notices', { cache: 'no-store' });
  if (!response.ok) throw new Error('HTTP ' + response.status);
  return response.json();
}

const _proactiveNoticeCache = new Map();
const _proactiveSpeechAttempts = new Map();
const _knownSystemMessageIds = new Set();
let _latestSystemMessageData = { messages: [], unread: 0 };
let _systemMessageAnnouncementsReady = false;
let _proactiveNoticeNonce = 0;
let _proactivePinnedRenderSig = null;
let _proactiveRefreshInFlight = null;

function _noticeText(row) {
  return String(
    (row && row.delivery && row.delivery.text)
      || (row && row.narrative)
      || (row && row.message)
      || (row && row.text)
      || ''
  ).trim();
}

async function _ackProactiveNotice(notice) {
  const id = String((notice && notice.id) || '').trim();
  if (!id) return false;
  const response = await fetch(
    '/api/chat/proactive/' + encodeURIComponent(id) + '/delivered',
    { method: 'POST', cache: 'no-store' },
  );
  if (!response.ok) return false;
  notice.status = 'delivered';
  return true;
}

function _tryDeliverSpokenNotice(row, notice) {
  const id = String((notice && notice.id) || '').trim();
  if (!id || notice.status !== 'pending') return;
  let attempt = _proactiveSpeechAttempts.get(id);
  if (attempt && attempt.promise) return;
  if (!attempt) {
    const payload = (row && row.payload) || {};
    const speakAlways = !!(payload.speak_always || payload.boot_greeting);
    if (typeof Voice === 'undefined' || !Voice || !Voice.speak) return;
    let speech;
    try {
      speech = Voice.speak(notice.text, {
        ...(speakAlways ? { speakAlways: true, force: true } : {}),
        awaitPlayback: true,
      });
    } catch (_e) {
      return;
    }
    // The runtime returns a promise only when it accepted an audible request
    // and can report playback completion. A skipped or legacy fire-and-forget
    // call has no evidence to acknowledge.
    if (!speech || typeof speech.then !== 'function') return;
    attempt = { heard: false, promise: null };
    _proactiveSpeechAttempts.set(id, attempt);
    attempt.promise = Promise.resolve(speech).then(async completed => {
      if (completed !== true) return false;
      attempt.heard = true;
      return _ackProactiveNotice(notice);
    }).catch(() => false).finally(() => {
      attempt.promise = null;
      if (!attempt.heard || notice.status === 'delivered') {
        _proactiveSpeechAttempts.delete(id);
      }
    });
    return;
  }
  // Speech completed but the acknowledgement request failed. Retry the ack
  // without speaking the same notice again.
  if (attempt.heard) {
    attempt.promise = _ackProactiveNotice(notice).catch(() => false).finally(() => {
      attempt.promise = null;
      if (notice.status === 'delivered') _proactiveSpeechAttempts.delete(id);
    });
  }
}

function _rememberProactiveNotice(row, announce) {
  const text = _noticeText(row);
  if (!text) return { notice: null, isNew: false };
  const suppliedId = String(
    (row && row.id) || (row && row.outreach_id) || ''
  ).trim();
  const id = suppliedId || `signal-${Date.now()}-${++_proactiveNoticeNonce}`;
  const existing = _proactiveNoticeCache.get(id);
  if (existing) {
    existing.text = text;
    existing.ts = String((row && row.ts) || existing.ts || new Date().toISOString());
    existing.episode_id = String((row && row.episode_id) || existing.episode_id || '');
    existing.status = String((row && row.status) || existing.status || 'pending');
    existing.payload = { ...((row && row.payload) || existing.payload || {}) };
    existing.transient = !suppliedId;
    if (announce) _tryDeliverSpokenNotice(row, existing);
    return { notice: existing, isNew: false };
  }
  const notice = {
    id,
    text,
    ts: String((row && row.ts) || new Date().toISOString()),
    episode_id: String((row && row.episode_id) || ''),
    status: String((row && row.status) || 'pending'),
    payload: { ...((row && row.payload) || {}) },
    seen_at: Date.now(),
    transient: !suppliedId,
  };
  _proactiveNoticeCache.set(id, notice);
  if (announce) {
    // Dual-surface delivery (operator 2026-07-18): proactive text lands in
    // the MAIN CHAT feed too, not just the pinboard. Id-dedup above keeps
    // this to one bubble per notice; Voice.speak stays pill-gated.
    try {
      if (window.OmegaUI && window.OmegaUI.appendLine) {
        window.OmegaUI.appendLine('system', text);
      }
    } catch (_e) { /* chat surface optional */ }
    _tryDeliverSpokenNotice(row, notice);
  }
  return { notice, isNew: true };
}

function _announceNewSystemMessages(messages) {
  const rows = messages || [];
  if (!_systemMessageAnnouncementsReady) {
    rows.forEach((message) => {
      const id = String((message && message.id) || '');
      if (id) _knownSystemMessageIds.add(id);
    });
    _systemMessageAnnouncementsReady = true;
    return;
  }
  rows.forEach((message) => {
    const id = String((message && message.id) || '');
    if (!id || _knownSystemMessageIds.has(id)) return;
    _knownSystemMessageIds.add(id);
    const text = String(message.text || message.seed_question || '').trim();
    if (!text) return;
    // Dual-surface delivery (operator 2026-07-18): system asks and result
    // presentations appear in the MAIN CHAT feed as well as this window.
    // First-batch suppression above keeps reloads from replaying history.
    try {
      if (window.OmegaUI && window.OmegaUI.appendLine) {
        window.OmegaUI.appendLine('system', text);
      }
    } catch (_e) { /* chat surface optional */ }
    try {
      if (typeof Voice !== 'undefined' && Voice && Voice.speak) Voice.speak(text);
    } catch (_e) { /* TTS is optional */ }
  });
}

async function _refreshProactiveNotices() {
  const data = await _proactiveNoticesFetch();
  const rows = (data && data.messages) || [];
  const serverIds = new Set();
  rows.forEach((row) => {
    const id = String((row && row.id) || '').trim();
    if (id) serverIds.add(id);
    _rememberProactiveNotice(row, String(row && row.status) === 'pending');
  });
  const now = Date.now();
  for (const [id, notice] of _proactiveNoticeCache.entries()) {
    if (serverIds.has(id)) continue;
    if (notice.transient && now - Number(notice.seen_at || 0) < 30000) continue;
    _proactiveNoticeCache.delete(id);
  }
  return rows;
}

window._appendProactiveNotice = function (payload) {
  const remembered = _rememberProactiveNotice(payload || {}, true);
  if (remembered.notice) _paintProactivePinned(_latestSystemMessageData);
  return remembered.notice;
};

function _bindProactivePinboardActions(root) {
  if (!root || root.dataset.proactiveBound) return;
  root.dataset.proactiveBound = '1';
  root.addEventListener('click', async (event) => {
    const button = event.target.closest('.sysmsg-send');
    if (!button || !root.contains(button)) return;
    const messageId = String(button.dataset.msgId || '').trim();
    const card = messageId && root.querySelector(
      '.sysmsg-card[data-msg-id="' + messageId + '"]'
    );
    const input = card && card.querySelector('.sysmsg-respond-input');
    // A verdict button states the operator's OWN verdict on a presented result; the plain send
    // states none, and the host records "unknown" rather than reading one out of the words.
    const verdict = String(button.dataset.verdict || '').trim();
    const stated = String(button.dataset.statement || '').trim();
    const text = String((input && input.value) || '').trim() || stated;
    if (!messageId || !text) return;
    button.disabled = true;
    try {
      const isNotice = card.dataset.messageKind === 'notice';
      if (isNotice) {
        if (typeof window.ChatBridge !== 'undefined' && window.ChatBridge.send) {
          window.ChatBridge.send({ kind: 'chat.user_message', source: 'user', body: text });
        } else {
          await fetch('/api/chat', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ text }),
          });
        }
        input.value = '';
      } else {
        const response = await fetch(
          '/api/system-messages/' + encodeURIComponent(messageId) + '/respond',
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(verdict ? { text, verdict } : { text }),
        }
      );
      if (!response.ok) return;
      _proactivePinnedRenderSig = null;
      await _refreshProactivePinboard();
      }
    } finally {
      button.disabled = false;
    }
  });
}

function _bindProactiveClearAction() {
  const button = document.querySelector('[data-proactive-clear]');
  if (!button || button.dataset.proactiveClearBound) return;
  button.dataset.proactiveClearBound = '1';
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await fetch('/api/proactive/notices/clear', {
        method: 'POST',
        cache: 'no-store',
      });
      if (!response.ok) return;
      _proactiveNoticeCache.clear();
      _proactivePinnedRenderSig = null;
      await _refreshProactivePinboard();
    } finally {
      button.disabled = false;
    }
  });
}

function _systemMessageCard(message) {
  const E = _escapeHtml;
  const isResult = String(message.kind || '') === 'result_presentation';
  const text = E(
    String(message.text || message.seed_question || '').trim()
      || '(awaiting message text)'
  );
  const seed = message.seed_question && message.seed_question !== message.text
    ? `<div class="sysmsg-seed"><span class="k">why</span>${E(message.seed_question)}</div>`
    : '';
  const prompt = isResult ? 'grade this result…' : 'type your answer…';
  const sendLabel = isResult ? 'grade' : 'send';
  const headline = isResult ? 'Omega reports' : 'Omega asks';
  const state = isResult ? 'Awaiting grade' : 'Awaiting response';
  return `<article class="sysmsg-card sysmsg-card-pinned" data-msg-id="${E(message.id)}" data-message-kind="${isResult ? 'result' : 'question'}">
    <div class="sysmsg-headline">
      <span class="sysmsg-kind">${headline} · ${E(_formatRelativeTs(message.ts))}</span>
      <span class="sysmsg-state is-pending">${state}</span>
    </div>
    <div class="sysmsg-text">${text}</div>
    ${seed}
    <div class="sysmsg-actions">
      <textarea class="sysmsg-respond-input" data-msg-id="${E(message.id)}" placeholder="${prompt}"></textarea>
      <div class="sysmsg-action-row">
        ${isResult ? `<button type="button" class="sysmsg-btn sysmsg-send" data-msg-id="${E(message.id)}" data-verdict="supports" data-statement="it worked">it worked</button>
        <button type="button" class="sysmsg-btn sysmsg-send" data-msg-id="${E(message.id)}" data-verdict="contradicts" data-statement="it didn't work">it didn't work</button>` : ''}
        <button type="button" class="sysmsg-btn sysmsg-send" data-msg-id="${E(message.id)}">${sendLabel}</button>
      </div>
    </div>
  </article>`;
}

function _proactiveNoticeCard(notice) {
  const E = _escapeHtml;
  const episode = String(notice.episode_id || '').trim();
  const context = episode
    ? `<div class="sysmsg-seed"><span class="k">episode</span>${E(episode)}</div>`
    : '';
  return `<article class="sysmsg-card sysmsg-card-pinned proactive-notice" data-msg-id="${E(notice.id)}" data-proactive-notice-id="${E(notice.id)}" data-message-kind="notice">
    <div class="sysmsg-headline">
      <span class="sysmsg-kind">Omega updates · ${E(_formatRelativeTs(notice.ts))}</span>
      <span class="sysmsg-state is-complete">Pinned</span>
    </div>
    <div class="sysmsg-text">${E(notice.text)}</div>
    ${context}
    <div class="sysmsg-actions">
      <textarea class="sysmsg-respond-input" data-msg-id="${E(notice.id)}" placeholder="type your reply…"></textarea>
      <div class="sysmsg-action-row">
        <button type="button" class="sysmsg-btn sysmsg-send" data-msg-id="${E(notice.id)}">send</button>
      </div>
    </div>
  </article>`;
}

function _sysmsgSignature(messages) {
  return JSON.stringify((messages || []).map((message) => [
    String(message.id || ''), String(message.status || ''),
    String(message.text || ''), String(message.seed_question || ''),
  ]));
}

function _captureSysmsgDrafts(root) {
  const drafts = {};
  root.querySelectorAll('.sysmsg-respond-input').forEach((textarea) => {
    if (textarea.dataset.msgId && textarea.value) {
      drafts[textarea.dataset.msgId] = textarea.value;
    }
  });
  return drafts;
}

function _restoreSysmsgDrafts(root, drafts) {
  root.querySelectorAll('.sysmsg-respond-input').forEach((textarea) => {
    const value = drafts[textarea.dataset.msgId];
    if (value != null) textarea.value = value;
  });
}

function _paintProactivePinned(data) {
  const pinnedRoot = document.querySelector('[data-proactive-pinned-list]');
  if (!pinnedRoot) return;
  const activeDialogue = [...(data.messages || [])]
    .filter((message) => message.status === 'pending');
  const dialogue = activeDialogue.map((message) => ({
    surface: 'dialogue', ts: message.ts, value: message,
  }));
  const notices = [..._proactiveNoticeCache.values()].map((notice) => ({
    surface: 'notice', ts: notice.ts, value: notice,
  }));
  const items = dialogue.concat(notices).sort((a, b) => {
    const bt = Date.parse(b.ts || '') || 0;
    const at = Date.parse(a.ts || '') || 0;
    return bt - at;
  });
  const count = document.querySelector('[data-proactive-pinned-count]');
  if (count) count.textContent = String(items.length);
  const clearButton = document.querySelector('[data-proactive-clear]');
  if (clearButton) clearButton.disabled = notices.length === 0;
  const signature = _sysmsgSignature(activeDialogue) + '|' + JSON.stringify(
    notices.map((row) => [row.value.id, row.value.text, row.value.ts, row.value.status])
  );
  if (signature === _proactivePinnedRenderSig && pinnedRoot.children.length) return;
  const drafts = _captureSysmsgDrafts(pinnedRoot);
  _proactivePinnedRenderSig = signature;
  if (!items.length) {
    pinnedRoot.innerHTML = '<div class="proactive-empty">No open questions, teach-backs, or pinned notices.</div>';
    return;
  }
  pinnedRoot.innerHTML = items.map((item) => (
    item.surface === 'notice'
      ? _proactiveNoticeCard(item.value)
      : _systemMessageCard(item.value)
  )).join('');
  _bindProactivePinboardActions(pinnedRoot);
  _restoreSysmsgDrafts(pinnedRoot, drafts);
}

async function _refreshProactivePinboard() {
  if (document.visibilityState !== 'visible') return [];
  if (_proactiveRefreshInFlight) return _proactiveRefreshInFlight;
  _proactiveRefreshInFlight = (async () => {
    try {
      const [dialogue] = await Promise.all([
        _systemMessagesFetch(),
        _refreshProactiveNotices(),
      ]);
      const pending = [...(dialogue.messages || [])]
        .filter((message) => message.status === 'pending');
      _latestSystemMessageData = { ...dialogue, messages: pending };
      _announceNewSystemMessages(pending);
      _paintProactivePinned(_latestSystemMessageData);
      return pending;
    } catch (_e) {
      return [];
    } finally {
      _proactiveRefreshInFlight = null;
    }
  })();
  return _proactiveRefreshInFlight;
}

_bindProactiveClearAction();
setInterval(() => { if (!document.hidden) _refreshProactivePinboard(); }, 6000);

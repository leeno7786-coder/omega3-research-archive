// Boot sequence, legacy feed renderer fallback, feed preamble, ChatBridge mock (hud/ui-src/core/boot.js)
// Depends on: DOM #feed #banner; OperatorIdentity, Telemetry, Voice (runtime); initHudThemeSettings

const feed = document.getElementById('feed');
const banner = document.getElementById('banner');

const STATUS_GLYPHS = [
  // Visible top HUD glyphs only. The top chrome now leaves only
  // semantic core in this cluster; other substrate signals stay
  // backend/API data and are not rendered as top glyphs.
  'g-l0',
];


// ── Markdown renderer (safe innerHTML) ─────────────────────────────
// Lightweight, safe markdown → HTML converter. Renders bold, italic,
// inline/multiline code (with basic syntax highlighting), links, and
// unordered lists. Output is sanitised — only controlled HTML tags are
// emitted (no raw <, >, & pass through without escaping).
function _legacyRenderMarkdown(text) {
  if (!text) return '';
  let html = String(text)
    // Escape HTML entities first
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  // Code blocks (```lang\\n...\\n```) — process BEFORE inline code
  html = html.replace(/```(\w*)\s*\n?([\s\S]*?)```/g, function (_, lang, code) {
    const langClass = lang ? ' class="lang-' + _escapeHtml(lang) + '"' : '';
    const highlighted = _highlightCode(code, lang);
    return '<pre><code' + langClass + '>' + highlighted + '</code></pre>';
  });

  // Inline code (`code`)
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>');

  // Bold (**text** or __text__)
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');

  // Italic (*text* or _text_)
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/_(.+?)_/g, '<em>$1</em>');

  // Unordered lists (- item or * item)
  html = html.replace(/^[\s]*[-*]\s+(.+)$/gm, '<li>$1</li>');
  html = html.replace(/((?:<li>.*<\/li>\n?)+)/g, '<ul>$1</ul>');

  // Links [text](url) — validate the scheme; javascript:/data:/vbscript: etc. are dropped
  // to plain text so model/telemetry output can't inject an executable URI (stored XSS).
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, function (_m, label, url) {
    const safe = _safeUrl(url);
    if (!safe) return label;  // unsafe scheme -> render the link text only, no anchor
    return '<a href="' + _escapeHtml(safe) + '" target="_blank" rel="noopener">' + label + '</a>';
  });

  // Line breaks — collapse consecutive newlines into paragraphs
  html = html.replace(/\n{2,}/g, '</p><p>');
  html = html.replace(/\n/g, '<br>');

  // Wrap in paragraph if not already wrapped
  if (!html.startsWith('<pre') && !html.startsWith('<p') && !html.startsWith('<ul') && !html.startsWith('<li')) {
    html = '<p>' + html + '</p>';
  }
  return html;
}

// Basic syntax highlighting for common languages
function _highlightCode(code, lang) {
  let h = _escapeHtml(code);
  lang = (lang || '').toLowerCase();
  // Keywords common across most languages
  const keywords = lang === 'python' ? [
    '\\bdef\\b', '\\bclass\\b', '\\breturn\\b', '\\bif\\b', '\\belif\\b',
    '\\belse\\b', '\\bfor\\b', '\\bwhile\\b', '\\bimport\\b', '\\bfrom\\b',
    '\\bas\\b', '\\btry\\b', '\\bexcept\\b', '\\braise\\b', '\\bwith\\b',
    '\\bpass\\b', '\\byield\\b', '\\blambda\\b', '\\bNone\\b', '\\bTrue\\b',
    '\\bFalse\\b', '\\bself\\b', '\\bin\\b', '\\bnot\\b', '\\band\\b', '\\bor\\b',
    '\\basync\\b', '\\bawait\\b', '\\bprint\\b', '\\blen\\b', '\\brange\\b',
    '\\btype\\b', '\\bstr\\b', '\\bint\\b', '\\bfloat\\b', '\\blist\\b',
    '\\bdict\\b', '\\bset\\b', '\\btuple\\b', '\\bopen\\b', '\\bassert\\b',
  ].join('|') : [
    '\\bfunction\\b', '\\bconst\\b', '\\blet\\b', '\\bvar\\b', '\\bif\\b',
    '\\belse\\b', '\\bfor\\b', '\\bwhile\\b', '\\bdo\\b', '\\bswitch\\b',
    '\\bcase\\b', '\\bbreak\\b', '\\bcontinue\\b', '\\breturn\\b', '\\bimport\\b',
    '\\bexport\\b', '\\bfrom\\b', '\\bclass\\b', '\\bextends\\b', '\\bnew\\b',
    '\\bthis\\b', '\\bsuper\\b', '\\btry\\b', '\\bcatch\\b', '\\bfinally\\b',
    '\\bthrow\\b', '\\basync\\b', '\\bawait\\b', '\\byield\\b', '\\bnull\\b',
    '\\bundefined\\b', '\\btrue\\b', '\\bfalse\\b', '\\btypeof\\b', '\\binstanceof\\b',
    '\\bin\\b', '\\bof\\b', '\\brequire\\b', '\\bmodule\\b', '\\bconsole\\b',
    '\\bPromise\\b', '\\bMap\\b', '\\bSet\\b', '\\bArray\\b', '\\bObject\\b',
  ].join('|');

  // Keywords
  h = h.replace(new RegExp('(' + keywords + ')', 'g'), '<span class="syn-kw">$1</span>');
  // Strings (single/double quoted)
  h = h.replace(/(["'`])(?:(?!\1|\\\\).|\\.)*\1/g, '<span class="syn-str">$&</span>');
  // Comments (# … and // …)
  h = h.replace(/(#.*$|\/\/.*$)/gm, '<span class="syn-cm">$1</span>');
  // Numbers
  h = h.replace(/\b(\d+\.?\d*)\b/g, '<span class="syn-num">$1</span>');
  return h;
}

// ── Collapsible block ─────────────────────────────────────────────
// Creates a <details>/<summary> element. `contentEl` is any DOM node
// (already populated). Returns the <details> element.
function _makeCollapsible(summaryText, contentEl) {
  const details = document.createElement('details');
  details.className = 'collapsible-block';
  const summary = document.createElement('summary');
  summary.textContent = summaryText;
  details.appendChild(summary);
  details.appendChild(contentEl);
  // Auto-open on creation (user can collapse manually)
  details.open = true;
  return details;
}

function _escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// Allow only http(s) / mailto / relative URLs in rendered markdown links. Returns the URL
// when safe, or null for any scheme that could execute (javascript:, data:, vbscript:, …).
// Strips control chars + the markdown pass's &amp; so a scheme can't be smuggled past.
function _safeUrl(url) {
  const raw = String(url == null ? '' : url).replace(/&amp;/g, '&');
  const probe = raw.replace(/\s+/g, '').toLowerCase();  // strip ws/tab/newline so schemes can't be smuggled
  const m = probe.match(/^([a-z][a-z0-9+.\-]*):/);
  if (m && m[1] !== 'http' && m[1] !== 'https' && m[1] !== 'mailto') return null;
  return raw;
}


// Canonical "pin the chat feed to the newest message". Force the element's
// own scroll behavior to `auto` for this operation, then restore the
// presentation style. This stays synchronous when Edge throttles hidden-tab
// animations and uses the actual maximum scroll offset.
function pinFeedToBottom(el) {
  const target = el || feed;
  if (!target) return;
  const height = Number(target.scrollHeight) || 0;
  const viewport = Number(target.clientHeight) || 0;
  const top = Math.max(0, height - viewport);
  const style = target.style;
  const previousBehavior = style && style.scrollBehavior;
  if (style) style.scrollBehavior = 'auto';
  try {
    if (typeof target.scrollTo === 'function') {
      target.scrollTo({ top, behavior: 'auto' });
    } else {
      target.scrollTop = top;
    }
  } catch (_) {
    target.scrollTop = top;
  } finally {
    if (style) style.scrollBehavior = previousBehavior || '';
  }
}

function appendLine(cls, text, renderMarkdown) {
  const el = document.createElement('div');
  el.className = 'line ' + cls;
  el.dataset.chatRole = cls.indexOf('assistant') >= 0
    ? 'assistant'
    : (cls.indexOf('user') >= 0 ? 'user' : String(cls || 'system'));
  el.dataset.chatTime = new Date().toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
  if (renderMarkdown && (cls === 'assistant' || cls.indexOf('assistant') >= 0)) {
    el.innerHTML = _renderMarkdown(text);
  } else {
    el.textContent = text;
  }
  appendToFeed(el);
  pinFeedToBottom();
  return el;
}

function _chatLabel(value) {
  return String(value == null ? '' : value)
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}

function _clipChatText(value, limit) {
  const text = String(value == null ? '' : value).replace(/\s+/g, ' ').trim();
  if (!text || text.length <= limit) return text;
  return text.slice(0, Math.max(0, limit - 1)).trim() + '...';
}

function _thinkingFromResult(result) {
  const thinking = (result && result.thinking) || {};
  const delivery = (result && result.delivery) || {};
  const route = (result && result.route) || {};
  const prompt = (result && result.prompt_pack) || {};
  const steps = Array.isArray(thinking.steps) && thinking.steps.length
    ? thinking.steps
    : [
      { label: 'intent', value: route.intent || route.action || 'listening' },
      { label: 'memory', value: prompt.memory_block_hash ? 'recalled' : 'quiet' },
      { label: 'shape', value: delivery.response_mode || prompt.response_mode || 'response' },
    ];
  const memory = thinking.memory || {};
  const reasoningSteps = prompt.response_mode === 'reasoning' && Array.isArray(thinking.reasoning_steps)
    ? thinking.reasoning_steps.filter(step => step && typeof step === 'object').map(step => ({
      label: String(step.label || 'Reasoning'),
      text: String(step.text || ''),
    })).filter(step => step.text.trim())
    : [];
  return {
    status: thinking.status || (result && result.ok === false ? 'degraded' : 'ready'),
    steps,
    reasoning_summary: String(thinking.reasoning_summary || ''),
    reasoning_steps: reasoningSteps,
    memory: {
      status: memory.status || 'unknown',
      count: Number(memory.count || 0),
      scope: Array.isArray(memory.scope) ? memory.scope : [],
      snippets: Array.isArray(memory.snippets) ? memory.snippets : [],
      entries: Array.isArray(memory.entries)
        ? memory.entries
        : (Array.isArray(memory.snippets) ? memory.snippets : []),
    },
    prompt: {
      prompt_type: prompt.prompt_type || '',
      response_mode: prompt.response_mode || '',
    },
  };
}

// Normalize /api/chat image-create responses (nested `image`) and legacy
// direct-generate payloads (flat `url`) into one render shape.
function normalizeImageResult(result) {
  if (!result) return { ok: false, error: 'no result' };
  const img = result.image;
  if (img && img.url) {
    return {
      ok: result.ok !== false,
      url: img.url,
      prompt: img.prompt || result.message || '',
      width: img.width,
      height: img.height,
      filename: img.filename,
      sha256: img.sha256,
      error: result.error,
    };
  }
  if (result.url) return result;
  return {
    ok: false,
    error: result.error || result.message || 'no image produced',
  };
}

// Render an image_gen result inline in the feed with a download button.
// Accepts /api/chat `{image:{url,...}}` or legacy flat `{url,...}`.
// Renders nothing on ok:false or missing url — caller can still surface
// the error via appendLine if desired.
function appendImageMessage(result) {
  const normalized = normalizeImageResult(result);
  if (!normalized || normalized.ok === false || !normalized.url) {
    return null;
  }
  result = normalized;
  const card = document.createElement('div');
  card.className = 'line assistant image-card';
  const img = document.createElement('img');
  img.src = result.url || result.path;
  img.alt = result.prompt || 'generated image';
  img.loading = 'lazy';
  card.appendChild(img);
  // Meta row: dimensions + steps + latency, plus a download link
  const meta = document.createElement('div');
  meta.className = 'image-meta';
  const stats = document.createElement('span');
  const w = result.width || (result.size && result.size[0]) || '';
  const h = result.height || (result.size && result.size[1]) || '';
  const steps = result.steps != null ? `· ${result.steps} steps` : '';
  const latency = result.latency_ms != null ? `· ${(result.latency_ms / 1000).toFixed(1)}s` : '';
  stats.textContent = (w && h) ? `${w}×${h} ${steps} ${latency}`.trim() : '';
  meta.appendChild(stats);
  const dl = document.createElement('a');
  dl.className = 'image-download';
  dl.href = result.url;
  // download attribute hints a filename + forces download in same-origin
  const fname = (result.url.split('/').pop()) || 'omega-image.png';
  dl.setAttribute('download', fname);
  dl.textContent = '↓ download';
  meta.appendChild(dl);
  card.appendChild(meta);
  // Prompt as a small italic footer (clipped if too long via CSS line-height)
  if (result.prompt) {
    const p = document.createElement('div');
    p.className = 'image-prompt';
    p.textContent = '"' + String(result.prompt).slice(0, 240) +
      (String(result.prompt).length > 240 ? '…' : '') + '"';
    card.appendChild(p);
  }
  appendToFeed(card);
  pinFeedToBottom();
  return card;
}

function appendDocumentMessage(result) {
  const doc = (result && result.document) || result;
  if (!doc || doc.ok === false || !doc.url) {
    return null;
  }
  const card = document.createElement('div');
  card.className = 'line assistant document-card';
  const title = document.createElement('div');
  title.className = 'document-title';
  title.textContent = doc.title || doc.filename || 'generated document';
  card.appendChild(title);
  const meta = document.createElement('div');
  meta.className = 'document-meta';
  const badge = document.createElement('span');
  badge.className = 'document-format';
  badge.textContent = (doc.format || 'md').toUpperCase();
  meta.appendChild(badge);
  const dl = document.createElement('a');
  dl.className = 'document-download';
  dl.href = doc.url;
  dl.setAttribute('download', doc.filename || doc.url.split('/').pop() || 'omega-document');
  dl.textContent = '↓ download';
  meta.appendChild(dl);
  card.appendChild(meta);
  if (doc.prompt) {
    const p = document.createElement('div');
    p.className = 'document-prompt';
    p.textContent = '"' + String(doc.prompt).slice(0, 240) +
      (String(doc.prompt).length > 240 ? '…' : '') + '"';
    card.appendChild(p);
  }
  const saved = document.createElement('div');
  saved.className = 'document-saved-note';
  saved.textContent = 'Saved to Settings → Created Documents.';
  card.appendChild(saved);
  appendToFeed(card);
  pinFeedToBottom();
  return card;
}

// Append a system line whose specified word is a clickable pivot to a
// depth panel. Same calm appearance as a regular system line; the
// pivot word gets a hairline copper underline + opens the panel on click.
function appendPivotLine(text, pivotWord, panelId) {
  const el = document.createElement('div');
  el.className = 'line system';
  appendToFeed(el);
  pinFeedToBottom();
  // typewriter into a temp text node first, then on completion replace
  // with the pivoted markup so the click target only exists once the
  // line has finished revealing.
  return typewriter(el, text, 14).then(() => {
    if (!pivotWord) return;
    const safe = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    const idx = text.indexOf(pivotWord);
    if (idx === -1) return;
    el.innerHTML =
      safe(text.slice(0, idx)) +
      `<span class="pivot" data-panel="${panelId}" tabindex="0" role="button">${safe(pivotWord)}</span>` +
      safe(text.slice(idx + pivotWord.length));
    el.querySelector('.pivot').addEventListener('click', () => {
      if (typeof openPanel === 'function') openPanel(panelId);
    });
  });
}

function _operatorProfileIsUnset() {
  const identity = window.OperatorIdentity;
  if (!identity) return true;
  // Onboarded (profile saved + locked) -> the onboarding prompt never shows again.
  if (identity.isOnboarded && identity.isOnboarded()) return false;
  return !String(identity.getRawName && identity.getRawName() || '').trim();
}

function _openOperatorProfileSettings() {
  if (typeof openPanel !== 'function') return;
  openPanel('settings');
  setTimeout(() => {
    const firstInput = document.getElementById('op-name');
    if (firstInput && typeof firstInput.focus === 'function') firstInput.focus();
  }, 80);
}

function appendFirstBootOperatorProfileChat() {
  if (!_operatorProfileIsUnset()) return;
  const text = 'Hello. I am Omega. Before we work together, open Settings and complete your operator profile — including your occupation and work topics — so I can recognize you, remember the right context, and form a learning agenda; idle learning starts paused until you enable it.';
  const pivotWord = 'operator profile';
  const el = document.createElement('div');
  el.className = 'line assistant';
  el.setAttribute('data-onboarding-profile-prompt', 'true');
  appendToFeed(el);
  pinFeedToBottom();
  typewriter(el, text, 12).then(() => {
    const idx = text.indexOf(pivotWord);
    if (idx !== -1) {
      const safe = (s) => _escapeHtml(s);
      el.innerHTML =
        safe(text.slice(0, idx)) +
        `<span class="pivot" data-panel="settings" tabindex="0" role="button">${safe(pivotWord)}</span>` +
        safe(text.slice(idx + pivotWord.length));
      const pivot = el.querySelector('.pivot');
      if (pivot) {
        pivot.addEventListener('click', _openOperatorProfileSettings);
        pivot.addEventListener('keydown', (e) => {
          if (e.key === 'Enter' || e.key === ' ') {
            e.preventDefault();
            _openOperatorProfileSettings();
          }
        });
      }
    }
  });
}

function clearFirstBootOperatorProfileChat() {
  if (!feed) return;
  feed.querySelectorAll('[data-onboarding-profile-prompt="true"]').forEach(el => el.remove());
}
window.clearFirstBootOperatorProfileChat = clearFirstBootOperatorProfileChat;

function typewriter(el, text, speed = 18) {
  return new Promise(resolve => {
    el.textContent = '';
    let i = 0;
    const tick = () => {
      if (i <= text.length) {
        el.textContent = text.slice(0, i++);
        setTimeout(tick, speed + (Math.random() * 22));
      } else resolve();
    };
    tick();
  });
}

async function bootSequence() {
  // Reveal the brand first (CSS animation handles the rise)
  banner.classList.remove('boot-hidden');

  await new Promise(r => setTimeout(r, 1100));

  // Status glyphs come online one-by-one (live state)
  for (const id of STATUS_GLYPHS) {
    const glyph = document.getElementById(id);
    if (!glyph) continue;
    glyph.classList.remove('dim');
    glyph.classList.add('live');
    await new Promise(r => setTimeout(r, 220));
  }

  // Feed machine-prose boot narration removed (HUD_PANELS_VISUAL_PACKET R2).
  // Banner + status glyphs only; conversation stays in user/assistant lines.
  await OperatorIdentity.fetchIdentity().catch(() => { });
  const _opName = OperatorIdentity.getName();
  const flash = document.getElementById('identity-flash');
  const flashEm = flash && flash.querySelector('em');
  if (flashEm) flashEm.textContent = _opName;
  flash.classList.add('show');
  await new Promise(r => setTimeout(r, 1600));
  flash.classList.remove('show');
}

function synthesizeResponse(t) {
  // Tiny calm responder — no chatbot fluff. Operator name comes from
  // OperatorIdentity (Settings tab persona); falls through to "operator".
  const _op = (typeof OperatorIdentity !== 'undefined') ? OperatorIdentity.getName() : 'operator';
  if (/hello|hi|hey/i.test(t)) return 'Hello, ' + _op + '.';
  if (/who.*are.*you/i.test(t)) return 'I am Omega — a portable cognitive substrate. Per-user, offline-first, identity-bearing.';
  if (/how.*feel|how.*are.*you/i.test(t)) return 'Calibration nominal. Memory cohesive. Awaiting direction.';
  if (/help|what can you do/i.test(t)) return 'Chat, memory, vision, and settings are available. Open settings for operator profile, voice, memory graph, skills, and runtime helpers.';
  return 'Acknowledged. Routing through brain.retrieve · memory.api · brain.respond.';
}

const ChatBridge = (() => {
  // Chat's own lane — SAME ORIGIN, so it needs no port of its own and works
  // wherever the HUD is served from (AGENTS.md §4.0; hud/server.py
  // @app.websocket("/omega/chat")).
  //
  // This used to point at ws://localhost:13317/omega/bridge, which never had a
  // server behind it — the HUD binds 13310 by default (OMEGA_HUD_PORT). Chat
  // therefore rode POST /api/chat with the reply POLLED from
  // /api/chat/responses, and that poll is why speech could not start until a
  // reply was fully assembled AND a tick happened to catch it.
  const URI = (typeof location !== 'undefined' && location.host)
    ? ((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/omega/chat')
    : 'ws://127.0.0.1:13310/omega/chat';
  const CONNECT_TIMEOUT_MS = 1500;
  const BACKOFF_MS = [2000, 4000, 8000, 16000];

  let ws = null;
  let backoffIdx = 0;
  let connectTimer = null;
  let messageHandlers = new Set();

  function setState(state, last_event) {
    Telemetry.update({
      system: {
        bridge: {
          state, uri: state === 'fallback' ? null : URI, last_event,
        }
      }
    });
  }

  function notify(envelope) {
    for (const fn of messageHandlers) {
      try { fn(envelope); } catch (e) { /* swallow */ }
    }
  }

  function connect() {
    setState('connecting', 'attempting ' + URI);
    try {
      ws = new WebSocket(URI);
    } catch (e) {
      // Some browsers throw synchronously on bad URI / file:// origin
      goFallback('ws-construct-failed: ' + e.message);
      return;
    }

    connectTimer = setTimeout(() => {
      // Timed out before open — give up and fall back. Keep trying
      // periodically in case the backend comes online later.
      try { ws && ws.close(); } catch (_) { }
      goFallback('connect-timeout');
      scheduleReconnect();
    }, CONNECT_TIMEOUT_MS);

    ws.addEventListener('open', () => {
      clearTimeout(connectTimer);
      backoffIdx = 0;
      setState('open', 'connected');
    });
    ws.addEventListener('message', (ev) => {
      let env;
      try { env = JSON.parse(ev.data); }
      catch (e) { env = { kind: 'chat.assistant_reply', source: 'assistant', body: String(ev.data), cycle_id: null }; }
      notify(env);
    });
    ws.addEventListener('close', () => {
      clearTimeout(connectTimer);
      // Only schedule reconnect if we're not already in fallback —
      // fallback already runs the reconnect timer.
      if (Telemetry.snapshot().system.bridge.state !== 'fallback') {
        setState('reconnecting', 'socket-closed');
        scheduleReconnect();
      }
    });
    ws.addEventListener('error', () => {
      // Errors are followed by a close event; let close handle state.
    });
  }

  function scheduleReconnect() {
    const delay = BACKOFF_MS[Math.min(backoffIdx, BACKOFF_MS.length - 1)];
    backoffIdx++;
    setTimeout(connect, delay);
  }

  function goFallback(reason) {
    setState('fallback', reason);
  }

  // Public API
  return {
    init() {
      // Skip live attempt when not on http(s) — file:// previews and
      // the like would pile up failed connect attempts forever. Mock
      // fallback is the right behaviour there.
      if (typeof window === 'undefined' ||
        (location.protocol !== 'http:' && location.protocol !== 'https:')) {
        goFallback('non-http-origin');
        return;
      }
      // The WS server IS wired now — hud/server.py @app.websocket("/omega/chat")
      // — so connect for real. Between 2026-05-12 and 2026-08-01 this went
      // straight to fallback because no server had ever listened at the old
      // :13317/omega/bridge address. If the socket cannot be reached we still
      // fall back to POST /api/chat on the same gate (see chat-wiring.js): one
      // road, two on-ramps — never a second path into ignition.
      connect();
    },
    send(envelope) {
      // Always notify locally first so the user sees their own line
      // (the chat UI owns the user-message echo). Then either ship
      // to backend or synthesise a fallback reply.
      const env = Object.assign({
        kind: 'chat.user_message', source: 'user',
        cycle_id: null, ts: new Date().toISOString(),
      }, envelope);

      if (Telemetry.snapshot().system.bridge.state === 'open' && ws && ws.readyState === 1) {
        try { ws.send(JSON.stringify(env)); }
        catch (e) { /* drop to fallback synth on send failure */
          fallbackReply(env);
        }
      } else {
        fallbackReply(env);
      }
    },
    onMessage(fn) {
      messageHandlers.add(fn);
      return () => messageHandlers.delete(fn);
    },
    // Raw socket access for chat's own lane. `send` above is REPLACED by
    // chat-wiring.js on a live origin (it owns routing between the socket and
    // the POST on the same gate), so these expose the transport underneath it.
    __socketOpen() { return !!(ws && ws.readyState === 1); },
    __sendRaw(obj) {
      if (!(ws && ws.readyState === 1)) throw new Error('chat socket not open');
      ws.send(JSON.stringify(obj));
      return true;
    },
    status() { return Telemetry.snapshot().system.bridge; },
  };

  function fallbackReply(userEnv) {
    // Local synthesis path — same envelope shape as a real backend
    // reply, so onMessage handlers don't need a special case.
    setTimeout(() => {
      notify({
        kind: 'chat.assistant_reply',
        source: 'assistant',
        body: synthesizeResponse(userEnv.body),
        cycle_id: null,
        ts: new Date().toISOString(),
      });
    }, 480);
  }
})();

// Bridge replies → assistant lines. Single subscriber covers both
// real backend pushes and the fallback synth path.
ChatBridge.onMessage((env) => {
  if (env.kind === 'chat.assistant_reply') {
    const body = env.body;
    appendLine('assistant', body, true);
    Voice.speak(body);
    pinFeedToBottom();
  }
  // chat.system autonomous narration suppressed (HUD_PANELS_VISUAL_PACKET R2)
});

// ─────────────────────────────────────────────────────────────────
// Kick off the boot sequence + bridges
// ─────────────────────────────────────────────────────────────────
window.addEventListener('load', () => {
  initHudThemeSettings();
  initVoiceSettings();
  setTimeout(() => {
    if (typeof warmupKokoroTTS === 'function') warmupKokoroTTS();
  }, 120);
  setTimeout(() => {
    bootSequence().then(() => {
      setTimeout(appendFirstBootOperatorProfileChat, 220);
    }).catch((err) => console.warn('HUD boot onboarding failed:', err));
  }, 320);
  // Attempt the chat bridge after a short delay so the boot animation
  // has the screen during its first beats; the bridge will quietly
  // fall to mock if no backend is reachable.
  setTimeout(() => ChatBridge.init(), 800);
  // Voice subsystem: probes feature support and registers state.
  // Listening still requires a user gesture (click voice pill or
  // press 'V') — browsers refuse mic access from page-load.
  setTimeout(() => Voice.init(), 900);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && typeof refreshPendingPluginPermissions === 'function') {
      refreshPendingPluginPermissions();
    }
  });
  if (typeof refreshPendingPluginPermissions === 'function') {
    refreshPendingPluginPermissions();
  }
});

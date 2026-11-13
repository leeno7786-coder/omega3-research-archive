import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
// The fuller DOM shim loadLifecyclePanel() below needs (borrowed from
// tests/hud/test_plugin_marketplace_review.mjs's own harness -- see the
// comment above loadLifecyclePanel) is the shared shim, follow-up refactor
// wave W2. Aliased to this file's own pre-existing Dom-prefixed names so
// every call site below is unchanged.
import {
  VOID, focusTracker, ClassList, datasetProxy,
  Element as DomElement, matchesSelector as matchesDomSelector,
  decodeEntities as decodeDomEntities, parseHtml as parseDomHtml,
} from './plugin_dom_shim.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.resolve(here, '../../hud/ui-src/core/plugin-permissions.js');
const chatFeedPath = path.resolve(here, '../../hud/ui-src/core/chat-feed.js');
const bootPath = path.resolve(here, '../../hud/ui-src/core/boot.js');
// core/plugin-views.js carries the panel's former private view builders
// (follow-up refactor wave W1) -- it must load before settings-plugins.js
// wherever this file loads the panel below, exactly as it does in every
// real build (the chat_feed core inject slot, ahead of panels, right after
// plugin-permissions.js).
const viewsPath = path.resolve(here, '../../hud/ui-src/core/plugin-views.js');
const settingsPath = path.resolve(here, '../../hud/ui-src/panels/settings-plugins.js');

const source = fs.readFileSync(sourcePath, 'utf8');
const chatFeed = fs.readFileSync(chatFeedPath, 'utf8');
const boot = fs.readFileSync(bootPath, 'utf8');
const views = fs.readFileSync(viewsPath, 'utf8');
const settings = fs.readFileSync(settingsPath, 'utf8');

const REQUEST = {
  request_id: 'a'.repeat(48),
  revision: 'b'.repeat(64),
  display: {
    plugin_apps: [
      {
        plugin_id: 'google.workspace.drive',
        plugin_name: 'Workspace Drive',
        app_id: 'google.workspace.drive.mcp',
        app_name: 'Workspace Drive Mcp',
      },
    ],
    effect_summary: 'Create data',
    ordered_operations: ['Create file'],
    audience: 'team inbox',
    destination: 'shared drive',
    crosses_providers: true,
    field_names: ['name'],
    risks: ['send_publish', 'destructive'],
  },
};

const SECRET = 'secret-argument-value';
const FORBIDDEN = [
  SECRET,
  'ya29.token',
  'https://oauth.example/callback',
  'C:\\Users\\leeno',
  'access_token',
  'refresh_token',
  'input_schema',
];

function element(tag) {
  const listeners = {};
  const node = {
    tagName: String(tag).toUpperCase(),
    children: [],
    attrs: {},
    dataset: {},
    style: {},
    className: '',
    hidden: false,
    disabled: false,
    textContent: '',
    innerHTML: '',
    parentNode: null,
    scrollHeight: 0,
    scrollTo() {},
    classList: {
      add(...names) {
        const set = new Set(node.className.split(/\s+/).filter(Boolean));
        names.forEach(name => set.add(name));
        node.className = [...set].join(' ');
      },
      remove(...names) {
        const set = new Set(node.className.split(/\s+/).filter(Boolean));
        names.forEach(name => set.delete(name));
        node.className = [...set].join(' ');
      },
      contains(name) {
        return node.className.split(/\s+/).includes(name);
      },
    },
    setAttribute(name, value) {
      node.attrs[name] = String(value);
      if (name.startsWith('data-')) {
        const key = name.slice(5).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
        node.dataset[key] = String(value);
      }
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(node.attrs, name) ? node.attrs[name] : null;
    },
    appendChild(child) {
      child.parentNode = node;
      node.children.push(child);
      return child;
    },
    remove() {
      if (!node.parentNode) return;
      node.parentNode.children = node.parentNode.children.filter(item => item !== node);
      node.parentNode = null;
    },
    addEventListener(type, fn) {
      listeners[type] = listeners[type] || [];
      listeners[type].push(fn);
    },
    click() {
      (listeners.click || []).forEach(fn => fn({ currentTarget: node, target: node, preventDefault() {} }));
    },
    querySelector(sel) {
      return queryAll(node, sel)[0] || null;
    },
    querySelectorAll(sel) {
      return queryAll(node, sel);
    },
  };
  return node;
}

function matches(node, sel) {
  if (sel.startsWith('.')) return node.classList.contains(sel.slice(1));
  if (sel.startsWith('[')) {
    const body = sel.slice(1, -1);
    const [name, raw] = body.split('=');
    if (raw == null) return node.getAttribute(name) != null || Object.prototype.hasOwnProperty.call(node.dataset, camel(name.replace(/^data-/, '')));
    const value = raw.replace(/^"|"$/g, '').replace(/^'|'$/g, '');
    return node.getAttribute(name) === value;
  }
  return node.tagName === sel.toUpperCase();
}

function camel(value) {
  return value.replace(/-([a-z])/g, (_, ch) => ch.toUpperCase());
}

function queryAll(node, sel) {
  const parts = sel.split(/(\.[a-z0-9_-]+|\[[^\]]+\])/i).filter(Boolean);
  const found = [];
  const walk = current => {
    if (parts.every(part => matches(current, part) || part === current.tagName)) {
      if (parts.some(part => matches(current, part))) found.push(current);
    } else if (parts.length === 1 && matches(current, sel)) {
      found.push(current);
    }
    current.children.forEach(walk);
  };
  node.children.forEach(walk);
  if (parts.length === 1) {
    const exact = [];
    const scan = current => {
      if (matches(current, sel)) exact.push(current);
      current.children.forEach(scan);
    };
    node.children.forEach(scan);
    return exact;
  }
  return found;
}

function textTree(node) {
  return [node.textContent, ...node.children.map(textTree)].join(' ');
}

function htmlTree(node) {
  const attrs = Object.entries(node.attrs).map(([k, v]) => `${k}="${v}"`).join(' ');
  return `<${node.tagName} ${attrs}>${node.innerHTML}${node.children.map(htmlTree).join('')}</${node.tagName}>`;
}

function load(fetchImpl) {
  const feed = element('div');
  feed.id = 'feed';
  const posts = [];
  const visibility = { hidden: false, listeners: [] };
  const ctx = {
    console,
    document: {
      hidden: false,
      body: element('body'),
      getElementById(id) { return id === 'feed' ? feed : null; },
      createElement: element,
      addEventListener(type, fn) {
        if (type === 'visibilitychange') visibility.listeners.push(fn);
      },
    },
    window: {},
    location: { protocol: 'http:' },
    fetch: (...args) => {
      posts.push(args);
      return fetchImpl(...args);
    },
    encodeURIComponent,
    setTimeout: fn => { fn(); return 0; },
    setInterval: () => 0,
    requestAnimationFrame: fn => fn(),
    _escapeHtml(value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },
    // The chat feed's one append door (chat-feed.js). A real build always has it;
    // loading plugin-permissions.js alone does not, and its own try/catch would
    // swallow the ReferenceError as "no pending requests".
    appendToFeed(node, target) { (target || feed).appendChild(node); return node; },
    appendLine() { return element('div'); },
    appendChatError() { return element('div'); },
    updateThinkingBlock(block) { if (block) block.resolved = true; },
    _scrollFeedToBottom() {},
    _renderMarkdown: text => text,
    CustomEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    Date, Math, JSON, Map, Set, Array, Object, Promise,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.document.hidden = false;
  vm.createContext(ctx);
  vm.runInContext(source, ctx, { filename: sourcePath });
  return { ctx, feed, posts, visibility };
}

function loadHud(fetchImpl) {
  const loaded = load(fetchImpl);
  loaded.ctx.feed = loaded.feed;
  vm.runInContext(chatFeed, loaded.ctx, { filename: chatFeedPath });
  return loaded;
}

function hudRequest() {
  return JSON.parse(JSON.stringify(REQUEST));
}

async function flush() {
  for (let i = 0; i < 10; i++) await Promise.resolve();
}

{
  const { ctx, feed } = load(async () => ({ ok: true, json: async () => ({}) }));
  const card = ctx.renderPluginPermissionCard(hudRequest());
  feed.appendChild(card);
  const text = textTree(card);
  assert.match(text, /Workspace Drive/);
  assert.match(text, /Workspace Drive Mcp/);
  assert.match(text, /Create data/);
  assert.match(text, /Create file/);
  assert.match(text, /shared drive/);
  assert.match(text, /team inbox/);
  assert.match(text, /name/);
  assert.match(text, /cross/i);
  assert.match(text, /Sends or publishes information/);
  assert.match(text, /Destructive action/);
  const allow = card.querySelector('.plugin-permission-allow');
  const cancel = card.querySelector('.plugin-permission-cancel');
  assert.equal(allow.getAttribute('data-request-id'), REQUEST.request_id);
  assert.equal(allow.getAttribute('data-revision'), REQUEST.revision);
  assert.equal(cancel.getAttribute('data-request-id'), REQUEST.request_id);
  assert.equal(cancel.getAttribute('data-revision'), REQUEST.revision);
  const markup = htmlTree(card);
  for (const token of FORBIDDEN) {
    assert.equal(markup.includes(token), false, token);
    assert.equal(text.includes(token), false, token);
  }
  assert.equal(Object.keys(allow.dataset).sort().join(','), 'requestId,revision');
  console.log('PASS permission card text is exact and secret-free');
}

{
  let pendingCalls = 0;
  const { ctx, feed } = load(async (url) => {
    if (String(url).includes('/api/plugins/permissions/pending')) {
      pendingCalls += 1;
      return { ok: true, json: async () => ({ ok: true, requests: [hudRequest()] }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  await ctx.refreshPendingPluginPermissions();
  assert.equal(pendingCalls, 1);
  assert.equal(feed.querySelectorAll('.plugin-permission-card').length, 1);
  console.log('PASS refresh recovers pending permission cards');
}

{
  const posts = [];
  const { ctx, feed } = load(async (url, options = {}) => {
    posts.push({ url, options });
    if (String(url).includes('/approve')) {
      return { ok: true, json: async () => ({ ok: true, status: 'queued', request_id: REQUEST.request_id, task_id: 'logical-plugin-task' }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  const card = ctx.renderPluginPermissionCard(hudRequest());
  feed.appendChild(card);
  const allow = card.querySelector('.plugin-permission-allow');
  const cancel = card.querySelector('.plugin-permission-cancel');
  allow.click();
  allow.click();
  await flush();
  assert.equal(allow.disabled, true);
  assert.equal(cancel.disabled, true);
  const approvePosts = posts.filter(item => String(item.url).includes('/approve'));
  assert.equal(approvePosts.length, 1);
  console.log('PASS allow disables both controls and posts once');
}

{
  let cancelPosts = 0;
  const { ctx, feed } = load(async (url) => {
    if (String(url).includes('/cancel')) {
      cancelPosts += 1;
      return { ok: true, json: async () => ({ ok: true, status: 'queued', request_id: REQUEST.request_id, task_id: 'logical-plugin-task' }) };
    }
    if (String(url).includes('/api/chat/responses')) {
      return { ok: true, json: async () => ({ found: true, ok: true, cycle_id: 'logical-plugin-task', message: 'cancelled' }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  const card = ctx.renderPluginPermissionCard(hudRequest());
  feed.appendChild(card);
  card.querySelector('.plugin-permission-cancel').click();
  await flush();
  await ctx.settlePluginPermissionCard(REQUEST.request_id, { found: true, ok: true, cycle_id: 'logical-plugin-task' });
  assert.equal(cancelPosts, 1);
  assert.equal(feed.querySelectorAll('.plugin-permission-card').length, 0);
  console.log('PASS cancel removes the card after the queued denial folds');
}

{
  const thinking = element('div');
  thinking.resolved = false;
  const { ctx } = load(async (url) => {
    if (String(url).includes('/approve')) {
      return { ok: true, json: async () => ({ ok: true, status: 'queued', request_id: REQUEST.request_id, task_id: 'logical-plugin-task' }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  ctx.bindPluginPermissionTask(REQUEST.request_id, 'original-cycle', thinking);
  const card = ctx.renderPluginPermissionCard(hudRequest());
  card.querySelector('.plugin-permission-allow').click();
  await flush();
  ctx.settlePluginPermissionCard(REQUEST.request_id, {
    found: true,
    ok: true,
    cycle_id: 'logical-plugin-task',
    message: 'file created',
  });
  assert.equal(thinking.resolved, true);
  assert.equal(ctx.pluginPermissionBoundCycle(REQUEST.request_id), 'logical-plugin-task');
  console.log('PASS successful continuation resolves the original task thinking bubble');
}

{
  const { ctx, feed } = load(async () => ({
    ok: true,
    json: async () => ({ ok: false, error: 'plugin_permission_expired' }),
  }));
  const card = ctx.renderPluginPermissionCard(hudRequest());
  feed.appendChild(card);
  card.querySelector('.plugin-permission-allow').click();
  await flush();
  const text = textTree(card);
  assert.match(text, /expired|no longer valid/i);
  for (const token of FORBIDDEN) assert.equal(text.includes(token), false);
  console.log('PASS failure renders a stable content-free message');
}

assert.equal(source.includes('innerHTML'), false);
assert.match(boot, /visibilitychange/);
assert.match(boot, /refreshPendingPluginPermissions/);
assert.match(chatFeed, /renderPluginPermissionCard|plugin_permission_request|plugin-permission/);
// The "When Omega may act" radio labels are core/plugin-views.js's
// permissionsEditorMarkup's own literal text now (follow-up refactor wave
// W1, the permissions editor markup moved out of the panel) -- not the
// panel's own source.
assert.match(views, /run selected scopes directly in chat/);
assert.match(views, /ask before changes \(reads run directly\)/);
console.log('PASS source contracts for boot, chat feed, and settings review');

function extractInject(html, name) {
  const startMark = `// HUD_UI_SRC:INJECT_${name}_START`;
  const endMark = `// HUD_UI_SRC:INJECT_${name}_END`;
  const start = html.indexOf(startMark);
  const end = html.indexOf(endMark);
  assert.notEqual(start, -1, startMark);
  assert.notEqual(end, -1, endMark);
  return html.slice(start, end);
}

function extractModule(bundle, modulePath) {
  const marker = `// @module ${modulePath}`;
  const start = bundle.indexOf(marker);
  assert.notEqual(start, -1, marker);
  const from = start + marker.length;
  const nextModule = bundle.indexOf('// @module ', from);
  const nextInject = bundle.indexOf('// HUD_UI_SRC:INJECT_', from);
  let end = bundle.length;
  if (nextModule !== -1) end = Math.min(end, nextModule);
  if (nextInject !== -1) end = Math.min(end, nextInject);
  return bundle.slice(from, end);
}

{
  const html = fs.readFileSync(path.resolve(here, '../../hud/omega_interface.html'), 'utf8');
  const core = extractInject(html, 'CORE');
  const chatFeedBundle = extractInject(html, 'CHAT_FEED');
  assert.equal(
    core.includes('@module core/plugin-permissions.js'),
    false,
    'plugin-permissions.js must not be trapped in the CORE use-strict IIFE',
  );
  assert.equal(
    chatFeedBundle.includes('@module core/plugin-permissions.js'),
    true,
    'plugin-permissions.js must share the chat-feed inject slot',
  );

  const feed = element('div');
  feed.id = 'feed';
  const pending = new Map();
  const since = new Map();
  let approvePosts = 0;
  const ctx = {
    console,
    document: {
      hidden: false,
      body: element('body'),
      getElementById(id) { return id === 'feed' ? feed : null; },
      createElement: element,
      addEventListener() {},
      querySelector() { return null; },
    },
    window: {},
    location: { protocol: 'http:' },
    fetch: async (url) => {
      if (String(url).includes('/approve')) {
        approvePosts += 1;
        return {
          ok: true,
          json: async () => ({
            ok: true,
            status: 'queued',
            request_id: REQUEST.request_id,
            task_id: 'logical-plugin-task',
          }),
        };
      }
      return { ok: true, json: async () => ({}) };
    },
    encodeURIComponent,
    setTimeout: fn => { fn(); return 0; },
    setInterval: () => 0,
    clearInterval() {},
    requestAnimationFrame: fn => fn(),
    _escapeHtml(value) { return String(value == null ? '' : value); },
    // The chat feed's one append door (chat-feed.js). A real build always has it;
    // loading plugin-permissions.js alone does not, and its own try/catch would
    // swallow the ReferenceError as "no pending requests".
    appendToFeed(node, target) { (target || feed).appendChild(node); return node; },
    appendLine() { return element('div'); },
    appendChatError() { return element('div'); },
    updateThinkingBlock(block) { if (block) block.resolved = true; },
    _scrollFeedToBottom() {},
    _renderMarkdown: text => text,
    _clipChatText: text => text,
    _chatLabel: () => 'omega',
    _thinkingFromResult: () => '',
    CustomEvent: class { constructor(type, init) { this.type = type; Object.assign(this, init); } },
    Date, Math, JSON, Map, Set, Array, Object, Promise, parseInt, Number, String, Boolean,
    feed,
    _pendingChatResponses: pending,
    _pendingChatSince: since,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(extractModule(chatFeedBundle, 'core/chat-feed.js'), ctx, { filename: 'chat-feed.js' });
  vm.runInContext(
    extractModule(chatFeedBundle, 'core/plugin-permissions.js'),
    ctx,
    { filename: 'plugin-permissions.js' },
  );
  vm.runInContext(
    `(function () { 'use strict';
      if (typeof renderPluginPermissionCard !== 'function') throw new Error('chat-feed cannot see renderPluginPermissionCard');
      if (typeof refreshPendingPluginPermissions !== 'function') throw new Error('boot cannot see refreshPendingPluginPermissions');
      if (typeof bindPluginPermissionTask !== 'function') throw new Error('chat-feed cannot see bindPluginPermissionTask');
      if (typeof settlePluginPermissionByCycle !== 'function') throw new Error('chat-feed cannot see settlePluginPermissionByCycle');
      if (typeof window.renderPluginPermissionCard !== 'function') throw new Error('window.renderPluginPermissionCard missing');
    })();`,
    ctx,
  );

  const thinking = element('div');
  ctx.bindPluginPermissionTask(REQUEST.request_id, 'original-cycle', thinking);
  const card = ctx.renderPluginPermissionCard(hudRequest());
  feed.appendChild(card);
  card.querySelector('.plugin-permission-allow').click();
  await flush();
  assert.equal(approvePosts, 1);
  assert.equal(ctx.window._pendingChatResponses.has('logical-plugin-task'), true);
  assert.equal(ctx.window._pendingChatResponses.get('logical-plugin-task'), thinking);
  console.log('PASS bundled script split exposes helpers and original-task store');
}

{
  const older = hudRequest();
  older.request_id = '1'.repeat(48);
  const newest = hudRequest();
  newest.request_id = '2'.repeat(48);
  const { ctx, feed } = loadHud(async (url) => {
    if (String(url).includes('/api/plugins/permissions/pending')) {
      return { ok: true, json: async () => ({ ok: true, requests: [older, newest] }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  ctx.renderChatResult(
    { ok: false, error: 'pending_input', cycle_id: 'cycle-current' },
    element('div'),
  );
  await flush();
  assert.equal(ctx.pluginPermissionBoundCycle(newest.request_id), undefined);
  assert.equal(ctx.pluginPermissionBoundCycle(older.request_id), undefined);
  assert.equal(feed.querySelectorAll('.plugin-permission-card').length, 2);

  const thinking = element('div');
  ctx.renderChatResult(
    { ok: true, cycle_id: 'cycle-current', plugin_permission_request: older },
    thinking,
  );
  await flush();
  assert.equal(ctx.pluginPermissionBoundCycle(older.request_id), 'cycle-current');
  assert.notEqual(ctx.pluginPermissionBoundCycle(newest.request_id), 'cycle-current');
  console.log('PASS pending-input attaches the current HUD dict, not the newest pending card');
}

{
  const stale = hudRequest();
  stale.request_id = '3'.repeat(48);
  const live = hudRequest();
  let pending = [stale, live];
  const { ctx, feed } = load(async (url) => {
    if (String(url).includes('/api/plugins/permissions/pending')) {
      return { ok: true, json: async () => ({ ok: true, requests: pending }) };
    }
    return { ok: true, json: async () => ({}) };
  });
  await ctx.refreshPendingPluginPermissions();
  assert.equal(feed.querySelectorAll('.plugin-permission-card').length, 2);
  pending = [live];
  await ctx.refreshPendingPluginPermissions();
  assert.equal(feed.querySelectorAll('.plugin-permission-card').length, 1);
  assert.equal(feed.querySelector('.plugin-permission-card').querySelector('.plugin-permission-allow').dataset.requestId, live.request_id);
  console.log('PASS refresh drops cards that are no longer pending');
}

{
  const { ctx, feed } = load(async () => {
    const err = new Error('network down');
    throw err;
  });
  const card = ctx.renderPluginPermissionCard(hudRequest());
  feed.appendChild(card);
  const allow = card.querySelector('.plugin-permission-allow');
  const cancel = card.querySelector('.plugin-permission-cancel');
  allow.click();
  await flush();
  assert.equal(allow.disabled, false);
  assert.equal(cancel.disabled, false);

  const { ctx: ctx503, feed: feed503 } = load(async () => ({
    ok: false,
    status: 503,
    json: async () => ({ ok: false, error: 'plugin_permission_unavailable' }),
  }));
  const card503 = ctx503.renderPluginPermissionCard(hudRequest());
  feed503.appendChild(card503);
  const allow503 = card503.querySelector('.plugin-permission-allow');
  allow503.click();
  await flush();
  assert.equal(allow503.disabled, false);
  assert.equal(card503.querySelector('.plugin-permission-cancel').disabled, false);

  const { ctx: ctx409, feed: feed409 } = load(async () => ({
    ok: false,
    status: 409,
    json: async () => ({ ok: false, error: 'plugin_permission_expired' }),
  }));
  const card409 = ctx409.renderPluginPermissionCard(hudRequest());
  feed409.appendChild(card409);
  const allow409 = card409.querySelector('.plugin-permission-allow');
  allow409.click();
  await flush();
  assert.equal(allow409.disabled, true);
  console.log('PASS 503/network re-enables controls; expired stays disabled');
}

// ===========================================================================
// Task 6: closed lifecycle view + provider-neutral error vocabulary.
// core/plugin-permissions.js's CANONICAL_PLUGIN_STATES/lifecycleView/
// readablePluginError are pure functions -- no DOM/fetch needed, so they
// are exercised directly through the existing `load()` helper above (which
// already loads only plugin-permissions.js's own source).
// ===========================================================================

let failures = 0;

async function test(name, fn) {
  try {
    await fn();
    console.log('PASS ' + name);
  } catch (err) {
    failures += 1;
    console.log('FAIL ' + name);
    console.error(err);
  }
}

function plugin(overrides = {}) {
  return {
    plugin_id: 'weather',
    title: 'Weather',
    installed: true,
    next_required_app_id: null,
    ...overrides,
  };
}

const lifecycleCtx = load(async () => ({ ok: true, json: async () => ({}) })).ctx;

await test('every canonical lifecycle state has one action and message', () => {
  for (const state of lifecycleCtx.CANONICAL_PLUGIN_STATES) {
    const view = lifecycleCtx.lifecycleView(plugin({ state }));
    assert.ok(view.label);
    assert.ok(view.primaryAction || ['connected', 'incompatible'].includes(state));
  }
});

await test('saved-state errors explain recovery instead of suggesting a restart', () => {
  for (const code of ['mcp_app_registry_unreadable', 'plugin_registry_unreadable', 'plugin_registry_reconciliation_failed', 'plugin_legacy_recovery_required']) {
    const message = lifecycleCtx.readablePluginError({ code });
    assert.match(message, /recovery/i);
    assert.doesNotMatch(message, /restart/i);
    assert.doesNotMatch(message, /have not been removed/i);
  }
});

await test('provider-neutral errors never collapse to service unavailable', () => {
  const issuerError = lifecycleCtx.readablePluginError({ code: 'oauth_issuer_mismatch' });
  assert.match(issuerError, /sign-in.*identity/i);
  assert.doesNotMatch(issuerError, /restart|service is unavailable/i);
  assert.equal(lifecycleCtx.readablePluginError({ code: 'plugin_offline' }), 'This plugin is offline.');
  assert.equal(
    lifecycleCtx.readablePluginError({ code: 'plugin_permission_changed' }),
    'This plugin changed its requested permissions. Review the changes to continue.',
  );
  assert.notEqual(lifecycleCtx.readablePluginError({ code: 'plugin_offline' }), 'The plugin service is unavailable.');
});

await test('CANONICAL_PLUGIN_STATES is exactly the 14-member closed set', () => {
  // Array.from(...) copies the vm-context array's elements into this
  // realm's own Array -- assert/strict's deepEqual otherwise rejects two
  // structurally-identical arrays from different vm realms as unequal
  // (different [[Prototype]]).
  assert.deepEqual(Array.from(lifecycleCtx.CANONICAL_PLUGIN_STATES), [
    'available', 'review_required', 'installing', 'installed', 'setup_required',
    'connecting', 'connected', 'offline', 'update_available', 'update_required',
    'permission_changed', 'revocation_pending', 'blocked', 'incompatible',
  ]);
});

await test('an unrecognized lifecycle state fails visibly, never a silent default', () => {
  const view = lifecycleCtx.lifecycleView(plugin({ state: 'not_a_real_state' }));
  assert.equal(view.primaryAction, null);
  assert.match(view.label, /not_a_real_state/);
});

await test('revocation_pending disables the actions that would conflict with the pending disconnect', () => {
  const view = lifecycleCtx.lifecycleView(plugin({ state: 'revocation_pending' }));
  assert.equal(view.primaryAction.type, 'retry');
  assert.equal(view.primaryAction.label, 'Retry');
  assert.deepEqual(Array.from(view.disabledActions), ['disconnect', 'uninstall', 'permissions']);
});

await test('permission_changed opens the permissions diff, not Connect', () => {
  const view = lifecycleCtx.lifecycleView(plugin({ state: 'permission_changed' }));
  assert.equal(view.primaryAction.type, 'permissions');
  assert.notEqual(view.primaryAction.type, 'connect');
});

await test('update_available and update_required both show the update review, never Connect', () => {
  for (const state of ['update_available', 'update_required']) {
    const view = lifecycleCtx.lifecycleView(plugin({ state }));
    assert.equal(view.primaryAction.type, 'update_review');
  }
});

await test('an unknown error cause renders the cause itself, visibly', () => {
  const message = lifecycleCtx.readablePluginError({ code: 'plugin_totally_unmapped_cause' });
  assert.match(message, /plugin_totally_unmapped_cause/);
});

await test('every stable cause this task names has a distinct, closed message', () => {
  const named = [
    'plugin_offline', 'plugin_permission_changed', 'plugin_revision_conflict',
    'plugin_update_required', 'plugin_review_required', 'plugin_connection_state_invalid',
    'oauth_registration_unsupported', 'oauth_registration_invalid',
    'oauth_client_registration_rejected',
  ];
  for (const code of named) {
    const message = lifecycleCtx.readablePluginError({ code });
    assert.notEqual(message, 'The plugin service is unavailable.');
    assert.notEqual(message, 'Plugin connection failed.');
    assert.ok(message.length > 0);
  }
});

await test('dynamic registration failures give actionable provider-neutral recovery', () => {
  assert.equal(
    lifecycleCtx.readablePluginError({ code: 'oauth_registration_unsupported' }),
    'This plugin’s provider does not support automatic sign-in setup yet. It cannot be connected.',
  );
  assert.equal(
    lifecycleCtx.readablePluginError({ code: 'oauth_registration_invalid' }),
    'The plugin provider returned an invalid sign-in setup response. Retry, or try again later.',
  );
  assert.equal(
    lifecycleCtx.readablePluginError({ code: 'oauth_client_registration_rejected' }),
    'The plugin provider rejected automatic sign-in setup. Retry later, or contact the provider if this continues.',
  );
});

assert.equal(/github device flow/i.test(source), false, 'plugin-permissions.js must not name a provider-specific OAuth flow');
assert.equal(/google|github/i.test(source), false, 'plugin-permissions.js must not name a provider');
assert.equal(/github device flow/i.test(settings), false, 'settings-plugins.js must not name a provider-specific OAuth flow');
console.log('PASS source contracts: no GitHub Device Flow sentence, no provider substring in the lifecycle/error module');

// ===========================================================================
// Task 6: lifecycle-action runtime tests -- disconnect keeps the installed
// card, uninstall's in-panel destructive review removes it only once the
// server confirms, revocation_pending shows Retry with conflicting actions
// disabled, permission_changed opens the permissions editor instead of
// Connect, and update_required opens the update review instead of Connect.
// This needs a fuller DOM shim than this file's own `element()` factory
// above (which does not parse innerHTML into real child nodes) -- borrowed
// from tests/hud/test_plugin_marketplace_review.mjs's own harness, which
// drives the same real _renderSettingsPlugins.
// ===========================================================================

const REVISION_A = 'a'.repeat(64);
const storeSource = fs.readFileSync(
  path.resolve(here, '../../hud/ui-src/core/plugin-store.js'),
  'utf8',
);


function domUnused(code) {
  return async () => { const err = new Error(code || 'unused'); err.code = code || 'unused'; throw err; };
}

function installedFixture(overrides = {}) {
  return {
    plugin_id: 'weather',
    state: 'connected',
    title: 'Weather',
    summary: 'Live forecasts for any city.',
    trust_tier: 'omega_verified',
    registry_revision: REVISION_A,
    package_version: '1.0.0',
    apps: [],
    next_required_app_id: null,
    enabled: true,
    error_code: null,
    ...overrides,
  };
}

function loadLifecyclePanel({
  installedImpl, catalogImpl, disconnectImpl, uninstallImpl, permissionsImpl, detailsImpl, updateReviewImpl,
  savePermissionsImpl, statusImpl, setupImpl,
} = {}) {
  const windowListeners = new Map();
  const ctx = {
    console,
    CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    Object, Array, Error, Promise, String, Number, Boolean, Map, Set, JSON, Math, Date,
    _escapeHtml(value) {
      return String(value == null ? '' : value)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
    },
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  ctx.setTimeout = fn => { fn(); return 0; };
  ctx.clearTimeout = () => {};
  ctx.window.setTimeout = ctx.setTimeout;
  ctx.window.clearTimeout = ctx.clearTimeout;
  ctx.document = {
    createElement: tag => new DomElement(tag),
    getElementById: () => null,
    addEventListener: () => {},
  };
  Object.defineProperty(ctx.document, 'activeElement', { get: () => focusTracker.current });
  ctx.window.location = { origin: 'http://127.0.0.1:13310', assign() {}, replace() {} };
  ctx.window.open = () => null;
  ctx.window.confirm = () => { throw new Error('window.confirm must never be used for uninstall'); };
  ctx.window.addEventListener = (type, fn) => {
    if (!windowListeners.has(type)) windowListeners.set(type, []);
    windowListeners.get(type).push(fn);
  };
  ctx.window.removeEventListener = (type, fn) => {
    const list = windowListeners.get(type) || [];
    windowListeners.set(type, list.filter(f => f !== fn));
  };
  ctx.window.dispatchEvent = event => {
    (windowListeners.get(event.type) || []).forEach(fn => fn(event));
    return true;
  };
  // Every method returns exactly the impl's own promise, never an extra
  // async-function wrapper layer around it -- matching
  // tests/hud/test_plugin_marketplace_review.mjs's loadPanelContext exactly,
  // since an extra microtask hop on only some methods shifts the relative
  // settle order of the panel's own concurrent refreshInstalled()/
  // loadCatalog() calls at startup.
  const defaultCatalogImpl = async () => ({
    ok: true, revision: REVISION_A, status: 'ok',
    plugins: [{ plugin_id: 'weather', title: 'Weather', description: 'Live forecasts.', categories: ['weather'], icon_asset_path: null }],
    next_cursor: null, total_plugins: 1,
  });
  const defaultInstalledImpl = async () => ({ ok: true, revision: REVISION_A, plugins: [] });
  const defaultDetailsImpl = async pluginId => ({ ok: true, revision: REVISION_A, plugin: { plugin_id: pluginId, title: 'Weather' } });
  const calls = { disconnect: [], uninstall: [], permissions: [], savePermissions: [], status: [] };
  const setupCalls = [];
  ctx.window.OmegaPluginApi = {
    catalog: () => (catalogImpl || defaultCatalogImpl)(),
    installed: () => (installedImpl || defaultInstalledImpl)(),
    review: domUnused('unused'),
    install: domUnused('unused'),
    update: domUnused('unused'),
    updateReview: (pluginId, version, revision) => (updateReviewImpl || domUnused('unused'))(pluginId, version, revision),
    details: pluginId => (detailsImpl || defaultDetailsImpl)(pluginId),
    startConnection: domUnused('unused'),
    pollConnection: domUnused('unused'),
    status: pluginId => {
      calls.status.push({ pluginId });
      return (statusImpl || domUnused('unused'))(pluginId);
    },
    permissions: (pluginId, appId) => {
      calls.permissions.push({ pluginId, appId });
      return (permissionsImpl || domUnused('unused'))(pluginId, appId);
    },
    savePermissions: (pluginId, revision, settings, appId) => {
      calls.savePermissions.push({ pluginId, revision, settings, appId });
      return (savePermissionsImpl || domUnused('unused'))(pluginId, revision, settings, appId);
    },
    tryInChat: domUnused('unused'),
    disconnect: (pluginId, revision) => {
      calls.disconnect.push({ pluginId, revision });
      return (disconnectImpl || domUnused('unused'))(pluginId, revision);
    },
    uninstall: (pluginId, revision) => {
      calls.uninstall.push({ pluginId, revision });
      return (uninstallImpl || domUnused('unused'))(pluginId, revision);
    },
    sync: domUnused('unused'),
  };
  // The "Save and (re)connect" permissions-review submit path now drives
  // the exact same window.OmegaPluginSetup machine the Connect/Continue-
  // setup button uses (final fix wave W2) -- a controllable stub (never
  // the real core/plugin-setup.js source, which test_plugin_marketplace_
  // setup.mjs already covers exhaustively) records every call so a test
  // can prove the shared machine was genuinely invoked.
  ctx.window.OmegaPluginSetup = {
    start: async (snapshot, appId, controls) => {
      setupCalls.push({ snapshot, appId, controls });
      return (setupImpl || (async () => snapshot))(snapshot, appId, controls);
    },
  };
  vm.createContext(ctx);
  vm.runInContext(storeSource, ctx, { filename: 'plugin-store.js' });
  // The panel now calls window.lifecycleView/window.readablePluginError
  // directly (final fix wave W1; task-6 fix round 1 finding 2) -- this
  // module must load before settings-plugins.js here, exactly as it does
  // in every real build (the chat_feed core inject slot, ahead of panels)
  // and in every other .mjs harness that exercises this panel's runtime.
  vm.runInContext(source, ctx, { filename: sourcePath });
  vm.runInContext(views, ctx, { filename: 'plugin-views.js' });
  vm.runInContext(settings, ctx, { filename: 'settings-plugins.js' });
  const panel = new DomElement('div');
  ctx._renderSettingsPlugins(panel);
  return { ctx, panel, calls, setupCalls };
}

await test('disconnect removes connection authority but keeps the installed card', async () => {
  let state = 'connected';
  const ui = loadLifecyclePanel({
    installedImpl: () => ({ ok: true, revision: REVISION_A, plugins: [installedFixture({ state })] }),
    disconnectImpl: () => { state = 'installed'; return { ok: true, revision: REVISION_A }; },
  });
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  card.querySelector('[data-plugin-disconnect]').dispatchClick();
  await flush();
  assert.equal(ui.calls.disconnect.length, 1);
  assert.equal(ui.calls.disconnect[0].revision, REVISION_A);
  const cardAfter = ui.panel.querySelector('[data-plugin-id="weather"]');
  assert.ok(cardAfter, 'the installed card is still present after disconnect');
  assert.match(cardAfter.querySelector('[data-plugin-status]').textContent, /installed/);
});

await test('uninstall opens an in-panel destructive review, then removes the card only once the server confirms', async () => {
  let installedPlugins = [installedFixture({ state: 'connected' })];
  const ui = loadLifecyclePanel({
    installedImpl: () => ({ ok: true, revision: REVISION_A, plugins: installedPlugins }),
    uninstallImpl: () => { installedPlugins = []; return { ok: true, revision: REVISION_A, status: 'uninstalled', plugin_id: 'weather' }; },
  });
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  card.querySelector('[data-plugin-uninstall]').dispatchClick();
  await flush();
  // No browser confirm() -- and no server call yet, either -- until the
  // in-panel review's own confirm control is clicked.
  assert.equal(ui.calls.uninstall.length, 0);
  const cancelButton = ui.panel.querySelector('[data-plugin-uninstall-cancel]');
  const confirmButton = ui.panel.querySelector('[data-plugin-uninstall-confirm]');
  assert.ok(cancelButton, 'a cancel control exists in the destructive review');
  assert.ok(confirmButton, 'a confirm control exists in the destructive review');
  confirmButton.dispatchClick();
  await flush();
  assert.equal(ui.calls.uninstall.length, 1);
  assert.equal(ui.calls.uninstall[0].revision, REVISION_A);
  const cardAfter = ui.panel.querySelector('[data-plugin-id="weather"]');
  assert.equal(cardAfter.querySelector('[data-plugin-action]').textContent, 'Install');
  assert.equal(
    ui.panel.querySelector('[data-plugin-installed-list]').textContent.includes('Weather'),
    false,
    'the installed strip no longer lists the uninstalled plugin',
  );
});

await test('a cancelled uninstall review submits nothing', async () => {
  const ui = loadLifecyclePanel({
    installedImpl: () => ({ ok: true, revision: REVISION_A, plugins: [installedFixture({ state: 'connected' })] }),
  });
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  card.querySelector('[data-plugin-uninstall]').dispatchClick();
  await flush();
  ui.panel.querySelector('[data-plugin-uninstall-cancel]').dispatchClick();
  await flush();
  assert.equal(ui.calls.uninstall.length, 0);
});

await test('revocation_pending shows Retry and disables Permissions/Disconnect/Uninstall', async () => {
  const ui = loadLifecyclePanel({
    installedImpl: () => ({ ok: true, revision: REVISION_A, plugins: [installedFixture({ state: 'revocation_pending' })] }),
  });
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const actionButton = card.querySelector('[data-plugin-action]');
  assert.equal(actionButton.textContent, 'Retry');
  assert.equal(actionButton.disabled, false);
  assert.equal(card.querySelector('[data-plugin-authority]').disabled, true);
  assert.equal(card.querySelector('[data-plugin-disconnect]').disabled, true);
  assert.equal(card.querySelector('[data-plugin-uninstall]').disabled, true);
});

await test('permission_changed opens the permissions editor instead of Connect', async () => {
  const ui = loadLifecyclePanel({
    installedImpl: () => ({ ok: true, revision: REVISION_A, plugins: [installedFixture({ state: 'permission_changed' })] }),
    permissionsImpl: () => ({
      ok: true,
      revision: REVISION_A,
      plugin_id: 'weather',
      app_id: 'weather.app',
      status: 'permission_changed',
      offered: { provider_scopes: ['scope-a'], capability_ids: ['tool:forecast'], execution_policies: ['direct_turn', 'confirm_each'] },
      selected: { provider_scopes: [], capability_ids: [], execution_policy: 'confirm_each', source_constraints: {}, destination_constraints: {} },
    }),
  });
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const actionButton = card.querySelector('[data-plugin-action]');
  assert.equal(actionButton.textContent, 'Review permissions');
  actionButton.dispatchClick();
  await flush();
  assert.equal(ui.calls.permissions.length, 1, 'the permissions diff is opened');
  const detail = card.querySelector('[data-plugin-review-panel]');
  assert.equal(detail.hidden, false);
  assert.match(detail.textContent, /Provider scope/);
});

await test('update_required opens the update review, never a bare Connect', async () => {
  const ui = loadLifecyclePanel({
    installedImpl: () => ({ ok: true, revision: REVISION_A, plugins: [installedFixture({ state: 'update_required', package_version: '1.0.0' })] }),
    detailsImpl: () => ({ ok: true, revision: REVISION_A, plugin: { plugin_id: 'weather', title: 'Weather', plugin_version: '2.0.0' } }),
    updateReviewImpl: () => ({
      ok: true,
      revision: REVISION_A,
      review: { plugin_id: 'weather', from_version: '1.0.0', to_version: '2.0.0', permission_changes: [], sha256: 'u'.repeat(64) },
    }),
  });
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const actionButton = card.querySelector('[data-plugin-action]');
  assert.equal(actionButton.textContent, 'Update');
  actionButton.dispatchClick();
  await flush();
  const confirmButton = ui.panel.querySelector('[data-plugin-review-confirm]');
  assert.ok(confirmButton, 'the update review drawer opens with a confirm control');
  assert.equal(confirmButton.textContent, 'Update');
});

// ===========================================================================
// Final fix wave W2: one setup machine. The permissions editor's "Save and
// (re)connect" submit path used to drive its own ad hoc popup/device/
// postMessage implementation (completeAuthorizationStart/
// _pluginAuthorizationAction/pollDeviceConnection/navigateAuthorization) --
// deleted entirely. After a successful savePermissions, the panel re-fetches
// the connection status and drives window.OmegaPluginSetup.start(...)
// exactly as Connect/Continue-setup do (core/plugin-setup.js itself is
// exhaustively covered by tests/hud/test_plugin_marketplace_setup.mjs; this
// stub only proves the panel's own wiring calls it).
// ===========================================================================

await test('Save and (re)connect on a browser setup_action drives window.OmegaPluginSetup.start, never a private popup/device implementation', async () => {
  const freshStatus = {
    ok: true, revision: REVISION_A, plugin_id: 'weather', state: 'setup_required',
    setup_action: 'browser', next_required_app_id: 'weather.app',
  };
  const finalSnapshot = installedFixture({ state: 'connected' });
  let installedPlugins = [installedFixture({ state: 'permission_changed' })];
  const ui = loadLifecyclePanel({
    installedImpl: () => ({ ok: true, revision: REVISION_A, plugins: installedPlugins }),
    permissionsImpl: () => ({
      ok: true,
      revision: REVISION_A,
      plugin_id: 'weather',
      app_id: 'weather.app',
      status: 'permission_changed',
      offered: { provider_scopes: ['scope-a'], capability_ids: ['tool:forecast'] },
      selected: { provider_scopes: [], capability_ids: [], execution_policy: 'confirm_each', source_constraints: {}, destination_constraints: {} },
    }),
    savePermissionsImpl: () => ({ ok: true, revision: REVISION_A, status: 'setup_required' }),
    statusImpl: () => freshStatus,
    setupImpl: async () => {
      installedPlugins = [finalSnapshot];
      return finalSnapshot;
    },
  });
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  card.querySelector('[data-plugin-action]').dispatchClick();
  await flush();
  // The review form is built via document.createElement('form') + a plain
  // `.className =` assignment (not this test file's own classList/attrs
  // shim), so it is found by tag name here, not a class selector.
  const form = card.querySelector('form');
  assert.ok(form, 'the permissions review form must be rendered');
  await form.dispatchSubmit();
  await flush();
  assert.equal(ui.calls.savePermissions.length, 1, 'savePermissions must be called exactly once');
  assert.equal(ui.calls.status.length, 1, 'the panel must re-fetch GET .../connection after saving');
  assert.equal(ui.setupCalls.length, 1, 'window.OmegaPluginSetup.start must be invoked exactly once');
  assert.equal(ui.setupCalls[0].snapshot.setup_action, 'browser');
  assert.equal(ui.setupCalls[0].appId, 'weather.app');
  assert.equal(typeof ui.setupCalls[0].controls.api.startConnection, 'function');
  assert.equal(typeof ui.setupCalls[0].controls.popup !== 'undefined', true);
});

if (failures > 0) {
  console.error(`${failures} test(s) failed`);
  process.exit(1);
}
console.log('plugin permission HUD tests passed');

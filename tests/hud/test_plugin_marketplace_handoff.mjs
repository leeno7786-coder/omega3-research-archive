// Runtime behavior test for the marketplace-to-chat handoff (task-7): a
// connected card's "Try in chat" button (hud/ui-src/panels/settings-plugins.js)
// must obtain the current server introduction, hand it to
// hud/ui-src/core/plugin-composer.js's acceptTryInChat, and that must close
// Settings, select the plugin pill, append exactly one local assistant line,
// leave the composer empty, and focus it -- never submitting chat itself
// (controller ruling 2). A lifecycle error from the server must keep
// Settings open, render the exact new state, and append nothing (ruling 1).
//
// No jsdom dependency exists in this repo, so this file uses the shared
// small but real DOM shim (tests/hud/plugin_dom_shim.mjs, follow-up
// refactor wave W2) -- the same approach
// tests/hud/test_plugin_marketplace_panel.mjs and
// tests/hud/test_plugin_marketplace_review.mjs already established --
// whose Element already carries the document-level getElementById/
// querySelector-adjacent append/replaceChildren/insertBefore this file
// needs, since plugin-composer.js's own mount() manipulates real DOM nodes
// (the composer chip) rather than going through _renderSettingsPlugins.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import {
  esc, VOID, focusTracker, ClassList, datasetProxy, Element, matchesSelector,
  decodeEntities, parseHtml, okCatalogPage, okInstalled,
} from './plugin_dom_shim.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, '../..');
const storeSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-store.js'), 'utf8');
const permissionsSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-permissions.js'), 'utf8');
// core/plugin-views.js carries the panel's former private view builders
// (follow-up refactor wave W1) -- it loads before settings-plugins.js here,
// exactly as it does in every real build (the chat_feed core inject slot,
// ahead of the chat_wiring slot plugin-composer.js loads in, ahead of panels).
const viewsSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-views.js'), 'utf8');
const composerSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-composer.js'), 'utf8');
const panelSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/panels/settings-plugins.js'), 'utf8');

// ---------------------------------------------------------------------------
// Harness
// ---------------------------------------------------------------------------
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

function unused(code) {
  return async () => { const err = new Error(code || 'unused'); err.code = code || 'unused'; throw err; };
}

async function flush(n = 12) {
  for (let i = 0; i < n; i += 1) await Promise.resolve();
}

// refreshInstalled() and loadCatalog() fire as two independent,
// uncoordinated async chains at panel init; refreshInstalled() re-renders
// the catalog section itself once installed data resolves (task-8
// controller ruling 2), so a plain render-and-flush always reaches a real
// "connected" card with no extra interaction (e.g. a search keystroke)
// required.
async function renderAndSettleConnectedCard(context, panel) {
  context._renderSettingsPlugins(panel);
  await flush(30);
  const strip = panel.querySelector('[data-plugin-installed-list]');
  assert.doesNotMatch(strip.textContent, /Loading installed plugins/, 'the installed snapshot must be settled');
}

// The one real composer plugin used by every case here: installed, enabled,
// currently connected -- eligible for both the card's Try in chat button and
// the composer's own eligible-plugin filter.
function connectedInstalledPlugin(overrides = {}) {
  return {
    plugin_id: 'weather',
    publisher_id: 'google',
    state: 'connected',
    title: 'Weather',
    summary: 'Live forecasts for any city.',
    trust_tier: 'omega_verified',
    registry_revision: 'b'.repeat(64),
    package_version: '1.0.0',
    apps: [],
    next_required_app_id: null,
    enabled: true,
    error_code: null,
    ...overrides,
  };
}

function catalogEntry(overrides = {}) {
  return {
    plugin_id: 'weather',
    publisher_id: 'google',
    title: 'Weather',
    description: 'Live forecasts for any city.',
    categories: [],
    icon_asset_path: null,
    ...overrides,
  };
}

// Builds a real document (a composer element containing the plugins action
// button and the #input control it inserts its chip before, exactly as the
// real shell.html markup does) so plugin-composer.js's own mount() -- which
// manipulates that DOM directly, independent of _renderSettingsPlugins --
// runs for real rather than degrading to its no-op "not mounted" branch.
function loadHandoffContext({ catalogImpl, installedImpl, tryInChatImpl } = {}) {
  const windowListeners = new Map();
  const context = {
    console,
    CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    Object, Array, Error, Promise, String, Number, Boolean, Map, Set, JSON, Math, Date,
    _escapeHtml: esc,
  };
  context.window = context;
  context.globalThis = context;
  context.setTimeout = (fn) => { fn(); return 0; };
  context.clearTimeout = () => {};
  context.window.setTimeout = context.setTimeout;
  context.window.clearTimeout = context.clearTimeout;

  const documentRoot = new Element('document');
  const body = new Element('body');
  documentRoot.appendChild(body);
  const composerEl = new Element('div');
  composerEl.setAttribute('class', 'composer');
  const actionEl = new Element('button');
  actionEl.setAttribute('data-composer-action', 'plugins');
  const inputEl = new Element('input');
  inputEl.setAttribute('id', 'input');
  composerEl.appendChild(actionEl);
  composerEl.appendChild(inputEl);
  body.appendChild(composerEl);

  context.document = {
    createElement: (tag) => new Element(tag),
    getElementById: (id) => documentRoot.querySelector('#' + id),
    querySelector: (sel) => documentRoot.querySelector(sel),
    querySelectorAll: (sel) => documentRoot.querySelectorAll(sel),
    addEventListener: () => {},
    removeEventListener: () => {},
    readyState: 'complete',
  };
  Object.defineProperty(context.document, 'activeElement', { get: () => focusTracker.current });

  context.window.location = { origin: 'http://127.0.0.1:13310', assign() {}, replace() {} };
  context.window.open = () => null;
  context.window.confirm = () => true;
  context.window.addEventListener = (type, fn) => {
    if (!windowListeners.has(type)) windowListeners.set(type, []);
    windowListeners.get(type).push(fn);
  };
  context.window.removeEventListener = (type, fn) => {
    const list = windowListeners.get(type) || [];
    windowListeners.set(type, list.filter(f => f !== fn));
  };
  const dispatchedEvents = [];
  context.window.dispatchEvent = (event) => {
    dispatchedEvents.push(event);
    (windowListeners.get(event.type) || []).forEach(fn => fn(event));
    return true;
  };

  // closePanel/appendLine are bare globals in the real bundle (panel-fx.js /
  // boot.js), never namespaced under window.* explicitly by their callers --
  // plugin-composer.js calls them the same way (`typeof closePanel ===
  // 'function'`), so they must be set directly on the shared context object
  // (context.window === context here) for that lookup to resolve them.
  const calls = {
    catalog: [], installed: 0, tryInChat: [], closePanel: 0, appendLine: [],
  };
  context.closePanel = () => { calls.closePanel += 1; };
  context.appendLine = (role, text, renderMarkdown) => {
    calls.appendLine.push({ role, text, renderMarkdown });
  };

  context.window.OmegaPluginApi = {
    catalog: (params) => { calls.catalog.push(params); return catalogImpl(params); },
    installed: () => { calls.installed += 1; return installedImpl(); },
    tryInChat: (pluginId) => { calls.tryInChat.push(pluginId); return tryInChatImpl(pluginId); },
    review: unused('unused'),
    install: unused('unused'),
    updateReview: unused('unused'),
    update: unused('unused'),
    startConnection: unused('unused'),
    pollConnection: unused('unused'),
    permissions: unused('unused'),
    savePermissions: unused('unused'),
    disconnect: unused('unused'),
    uninstall: unused('unused'),
    details: unused('unused'),
    sync: unused('unused'),
    status: unused('unused'),
  };

  vm.createContext(context);
  vm.runInContext(storeSource, context, { filename: 'plugin-store.js' });
  vm.runInContext(permissionsSource, context, { filename: 'plugin-permissions.js' });
  vm.runInContext(viewsSource, context, { filename: 'plugin-views.js' });
  // Real build order (manifest.json): core/plugin-composer.js loads before
  // panels/settings-plugins.js.
  vm.runInContext(composerSource, context, { filename: 'plugin-composer.js' });
  vm.runInContext(panelSource, context, { filename: 'settings-plugins.js' });
  return { context, calls, dispatchedEvents, composerEl, inputEl };
}

// ===========================================================================
await test(
  'Try in chat: closes Settings, inserts the installed-plugin slash command, appends one assistant line, focuses the composer, and never submits chat',
  async () => {
    const introText = 'Weather is connected. I can help you:\n\n' +
      '- Check today\'s forecast.\n\nChoose an option or describe what you want to do.';
    const { context, calls, composerEl, inputEl } = loadHandoffContext({
      catalogImpl: async () => okCatalogPage({ plugins: [catalogEntry()] }),
      installedImpl: async () => okInstalled([connectedInstalledPlugin()]),
      tryInChatImpl: async (pluginId) => {
        assert.equal(pluginId, 'weather');
        return { ok: true, revision: 'a'.repeat(64), plugin_id: 'weather', title: 'Weather', intro_text: introText };
      },
    });
    const panel = new Element('div');
    await renderAndSettleConnectedCard(context, panel);

    // Prove the stale draft is replaced by the installed-plugin command.
    inputEl.value = 'stale draft text';
    const tryButton = panel.querySelector('[data-plugin-try]');
    assert.ok(tryButton, 'a connected card renders a Try in chat button');

    tryButton.dispatchClick();
    await flush();

    assert.equal(calls.tryInChat.length, 1, 'the current server introduction is fetched exactly once');
    assert.equal(calls.closePanel, 1, 'Settings closes exactly once');
    assert.equal(calls.appendLine.length, 1, 'exactly one local line is appended');
    assert.deepEqual(
      calls.appendLine[0],
      { role: 'assistant', text: introText, renderMarkdown: true },
      'the appended line carries the server\'s own intro_text verbatim',
    );
    assert.equal(inputEl.value, '/plugin:weather ', 'the composer carries the installed-plugin command');
    assert.equal(inputEl.dataset.pluginId, 'weather', 'the payload keeps only the verified plugin id');
    assert.equal(context.document.activeElement, inputEl, 'the composer is focused');

    // No chat-sending capability exists anywhere in this context (no
    // ChatBridge, no fetch('/api/chat', ...)) -- had the handoff itself
    // tried to submit chat, that call would have thrown inside the click
    // handler and this test would have failed above instead of reaching
    // this assertion.
    assert.equal(typeof context.window.ChatBridge, 'undefined', 'no chat-submission surface was ever touched');
  },
);

await test(
  'a lifecycle error (plugin_update_required) keeps Settings open, renders the exact new state, and appends nothing',
  async () => {
    let installedCall = 0;
    const { context, calls } = loadHandoffContext({
      catalogImpl: async () => okCatalogPage({ plugins: [catalogEntry()] }),
      installedImpl: async () => {
        installedCall += 1;
        if (installedCall === 1) return okInstalled([connectedInstalledPlugin()]);
        // The server's next authoritative snapshot after the failed
        // handoff: the plugin drifted and now needs an update before it
        // can be used again -- never the stale "connected" snapshot the
        // card was showing before the click.
        return okInstalled([connectedInstalledPlugin({ state: 'update_required', registry_revision: 'c'.repeat(64) })]);
      },
      tryInChatImpl: async () => {
        const err = new Error('plugin_update_required');
        err.code = 'plugin_update_required';
        throw err;
      },
    });
    const panel = new Element('div');
    await renderAndSettleConnectedCard(context, panel);
    assert.equal(calls.installed, 1);

    const tryButton = panel.querySelector('[data-plugin-try]');
    assert.ok(tryButton, 'a connected card renders a Try in chat button');
    tryButton.dispatchClick();
    await flush();

    assert.equal(calls.closePanel, 0, 'Settings stays open');
    assert.equal(calls.appendLine.length, 0, 'nothing is appended to chat');
    assert.equal(
      calls.installed,
      2,
      'the panel re-fetches so the card reflects the plugin\'s real current state, never a stale connected card',
    );

    const freshCard = panel.querySelector('[data-plugin-id="weather"]');
    assert.ok(freshCard, 'the card is re-rendered from the fresh installed snapshot');
    assert.equal(
      freshCard.querySelector('[data-plugin-try]'),
      null,
      'a plugin that is no longer connected does not offer Try in chat',
    );
    const status = freshCard.querySelector('[data-plugin-status]');
    assert.equal(
      status.textContent,
      context.readablePluginError({ code: 'plugin_update_required' }),
      'the exact new state renders via the shared error vocabulary',
    );
    assert.equal(status.classList.contains('is-error'), true);
  },
);

if (failures > 0) {
  console.error(`${failures} test(s) failed`);
  process.exit(1);
}
console.log('plugin marketplace handoff tests passed');

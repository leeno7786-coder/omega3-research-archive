// Runtime behavior test for hud/ui-src/panels/settings-plugins.js. No jsdom
// dependency exists in this repo, so this file uses the shared small but
// real DOM shim (HTML parser, querySelector[All]/closest/classList/dataset,
// event delegation -- tests/hud/plugin_dom_shim.mjs, follow-up refactor
// wave W2) sufficient to actually run _renderSettingsPlugins(panel) against
// a mocked window.OmegaPluginApi and the *real* window.OmegaPluginStore
// (loaded from its real source, not reimplemented).
//
// This exists because task-3 review finding 3 found no committed test
// exercised the panel's actual runtime behavior -- only source-contract
// regex/substring checks did. See task-3-report.md's fix-round-1 section.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import {
  esc, VOID, focusTracker, ClassList, datasetProxy, Element, matchesSelector,
  decodeEntities, parseHtml, okCatalogPage, okInstalled, makeFakeTimers, deferred,
} from './plugin_dom_shim.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, '../..');
const storeSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-store.js'), 'utf8');
// The panel now calls window.readablePluginError directly (task-6 fix
// round 1 finding 2: one shared error vocabulary, no private cause table)
// -- core/plugin-permissions.js must load before settings-plugins.js here,
// exactly as it does in every real build (the chat_feed core inject slot,
// ahead of panels).
const permissionsSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-permissions.js'), 'utf8');
// core/plugin-views.js carries the panel's former private catalog card/
// installed tile/drawer body/permissions editor view builders (follow-up
// refactor wave W1) -- it must also load before settings-plugins.js here,
// exactly as it does in every real build (the chat_feed core inject slot,
// ahead of panels, right after plugin-permissions.js).
const viewsSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-views.js'), 'utf8');
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

// vm.createContext() sandboxed code has its own Array/Object intrinsics, so
// a value returned from inside that context is not deepStrictEqual-comparable
// to an outer-realm literal even when structurally identical. Round-tripping
// through JSON (executed here, in the outer realm) normalizes it.
function xrealm(value) { return JSON.parse(JSON.stringify(value)); }

function loadPanelContext({ catalogImpl, installedImpl } = {}) {
  const windowListeners = new Map();
  const context = {
    console,
    CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    Object, Array, Error, Promise, String, Number, Boolean, Map, Set, JSON, Math, Date,
    _escapeHtml: esc,
  };
  context.window = context;
  context.globalThis = context;
  const fakeTimers = makeFakeTimers();
  context.setTimeout = fakeTimers.setTimeout;
  context.clearTimeout = fakeTimers.clearTimeout;
  context.window.setTimeout = fakeTimers.setTimeout;
  context.window.clearTimeout = fakeTimers.clearTimeout;
  context.document = {
    createElement: (tag) => new Element(tag),
    getElementById: () => null,
    addEventListener: () => {},
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
  const calls = { catalog: [], installed: 0 };
  context.window.OmegaPluginApi = {
    catalog: (params) => { calls.catalog.push(params); return catalogImpl(params); },
    installed: () => { calls.installed += 1; return installedImpl(); },
    review: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    install: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    startConnection: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    pollConnection: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    permissions: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    savePermissions: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    tryInChat: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    disconnect: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    update: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    uninstall: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    details: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    sync: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
    status: async () => { throw Object.assign(new Error('unused'), { code: 'unused' }); },
  };
  vm.createContext(context);
  vm.runInContext(storeSource, context, { filename: 'plugin-store.js' });
  vm.runInContext(permissionsSource, context, { filename: 'plugin-permissions.js' });
  vm.runInContext(viewsSource, context, { filename: 'plugin-views.js' });
  vm.runInContext(panelSource, context, { filename: 'settings-plugins.js' });
  return { context, calls, dispatchedEvents, fakeTimers };
}

async function flush(n = 4) {
  for (let i = 0; i < n; i += 1) await Promise.resolve();
}

// The shim's querySelectorAll has no "*" wildcard support -- this walks
// every real element (not text node) in document order for tests that need
// to inspect the whole rendered tree (task-8 keyboard-reachability check).
function walkAllElements(root) {
  const out = [];
  const visit = node => {
    node.children.forEach(child => {
      if (child.nodeType !== 'text') {
        out.push(child);
        visit(child);
      }
    });
  };
  visit(root);
  return out;
}

// ===========================================================================
// Direct unit cases for pure, module-scope helpers
// ===========================================================================

{
  // Load the panel source once into a throwaway vm context just to reach
  // its module-scope pure helpers directly (no rendering involved).
  const ctx = { console, Object, Array, Error, Map, Set, String, Boolean, _escapeHtml: esc };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(storeSource, ctx, { filename: 'plugin-store.js' });
  vm.runInContext(permissionsSource, ctx, { filename: 'plugin-permissions.js' });
  vm.runInContext(viewsSource, ctx, { filename: 'plugin-views.js' });
  vm.runInContext(panelSource, ctx, { filename: 'settings-plugins.js' });

  await test('_pluginTrustedIconPath: accepts a plain same-origin asset path', () => {
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/demo/icon.svg'), true);
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/demo/assets/icon.min.svg'), true);
  });

  await test('_pluginTrustedIconPath: rejects directory traversal', () => {
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/../secret'), false);
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/foo/../../secret'), false);
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/./x'), false);
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/foo/./x'), false);
  });

  await test('_pluginTrustedIconPath: rejects a scheme', () => {
    assert.equal(ctx._pluginTrustedIconPath('https://evil.example/api/plugins/x'), false);
    assert.equal(ctx._pluginTrustedIconPath('javascript:alert(1)'), false);
  });

  await test('_pluginTrustedIconPath: rejects a protocol-relative host', () => {
    assert.equal(ctx._pluginTrustedIconPath('//evil.example/api/plugins/x'), false);
  });

  await test('_pluginTrustedIconPath: rejects a query string', () => {
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/x?y=1'), false);
  });

  await test('_pluginTrustedIconPath: rejects a fragment', () => {
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/x#y'), false);
  });

  await test('_pluginTrustedIconPath: rejects percent-encoded traversal', () => {
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/%2e%2e/secret'), false);
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins/demo/%2e%2e'), false);
  });

  await test('_pluginTrustedIconPath: rejects an empty segment (double slash)', () => {
    assert.equal(ctx._pluginTrustedIconPath('/api/plugins//x'), false);
  });

  await test('_pluginTrustedIconPath: rejects non-string, null, and out-of-scope paths', () => {
    assert.equal(ctx._pluginTrustedIconPath(null), false);
    assert.equal(ctx._pluginTrustedIconPath(undefined), false);
    assert.equal(ctx._pluginTrustedIconPath(42), false);
    assert.equal(ctx._pluginTrustedIconPath('/api/other/x'), false);
  });

  await test('_pluginIconMarkup: renders <img> only for a trusted path, glyph otherwise', () => {
    const trusted = ctx._pluginIconMarkup({ icon_asset_path: '/api/plugins/demo/icon.svg' }, esc);
    assert.match(trusted, /<img /);
    const traversal = ctx._pluginIconMarkup({ icon_asset_path: '/api/plugins/../secret' }, esc);
    assert.doesNotMatch(traversal, /<img /);
    const missing = ctx._pluginIconMarkup({ icon_asset_path: null }, esc);
    assert.doesNotMatch(missing, /<img /);
  });

  await test('_pluginIconMarkup: uses the authoritative host provider mark', () => {
    const google = ctx._pluginIconMarkup({ publisher_id: 'google', icon_asset_path: null }, esc);
    assert.match(google, /provider-icon-google/);
    assert.match(google, /aria-label="Google provider"/);
    const notion = ctx._pluginIconMarkup({ provider_id: 'com.notion.mcp', icon_asset_path: null }, esc);
    assert.match(notion, /provider-icon-notion/);
    const unknown = ctx._pluginIconMarkup({ plugin_id: 'google.workspace.drive', icon_asset_path: null }, esc);
    assert.doesNotMatch(unknown, /provider-icon-google/);
  });

  await test('_pluginGroupByCategory: groups by first category, preserving server order within each group', () => {
    const groups = ctx._pluginGroupByCategory([
      { plugin_id: 'a', categories: ['Productivity'] },
      { plugin_id: 'b', categories: ['Utilities'] },
      { plugin_id: 'c', categories: ['Productivity', 'Extra'] },
      { plugin_id: 'd', categories: [] },
    ]);
    assert.deepEqual(xrealm(groups.map(g => g.category)), ['Productivity', 'Utilities', 'Uncategorized']);
    assert.deepEqual(xrealm(groups[0].items.map(i => i.plugin_id)), ['a', 'c']);
    assert.deepEqual(xrealm(groups[1].items.map(i => i.plugin_id)), ['b']);
    assert.deepEqual(xrealm(groups[2].items.map(i => i.plugin_id)), ['d']);
  });

  await test('_pluginGroupByCategory: a non-array categories field is treated as uncategorized', () => {
    const groups = ctx._pluginGroupByCategory([{ plugin_id: 'a', categories: 'not-an-array' }]);
    assert.deepEqual(xrealm(groups.map(g => g.category)), ['Uncategorized']);
  });

  await test('_pluginBuildCatalogParams: defaults with no cursor or filters', () => {
    const params = ctx._pluginBuildCatalogParams({ tab: 'public', query: '', filters: {} }, null);
    assert.deepEqual(xrealm(params), { q: '', page_size: 24 });
  });

  await test('_pluginBuildCatalogParams: query/category/state filters are forwarded', () => {
    const params = ctx._pluginBuildCatalogParams(
      { tab: 'public', query: 'mail', filters: { category: 'Productivity', state: 'connected' } },
      null,
    );
    assert.equal(params.q, 'mail');
    assert.equal(params.category, 'Productivity');
    assert.equal(params.state, 'connected');
    assert.equal(params.trust, undefined);
  });

  await test('_pluginBuildCatalogParams: the Personal tab always forces trust=personal', () => {
    const params = ctx._pluginBuildCatalogParams(
      { tab: 'personal', query: '', filters: { trust: 'omega_verified' } },
      null,
    );
    assert.equal(params.trust, 'personal');
  });

  await test('_pluginBuildCatalogParams: the Public tab forwards an explicit trust filter', () => {
    const params = ctx._pluginBuildCatalogParams(
      { tab: 'public', query: '', filters: { trust: 'publisher_verified' } },
      null,
    );
    assert.equal(params.trust, 'publisher_verified');
  });

  await test('_pluginBuildCatalogParams: a cursor is only sent when appending', () => {
    const withoutCursor = ctx._pluginBuildCatalogParams({ tab: 'public', query: '', filters: {} }, null);
    assert.equal(withoutCursor.cursor, undefined);
    const withCursor = ctx._pluginBuildCatalogParams({ tab: 'public', query: '', filters: {} }, 'page-2');
    assert.equal(withCursor.cursor, 'page-2');
  });
}

// ===========================================================================
// Runtime rendering behavior (real store + real render function + fake DOM)
// ===========================================================================

await test('initial render: category sections, server total count, load-more, installed strip', async () => {
  const { context, calls } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({
      plugins: [
        { plugin_id: 'demo.one', title: 'Demo One', description: 'd1', categories: ['Productivity'], icon_asset_path: null },
        { plugin_id: 'demo.two', title: 'Demo Two', description: 'd2', categories: ['Productivity'], icon_asset_path: null },
        { plugin_id: 'demo.three', title: 'Demo Three', description: 'd3', categories: ['Utilities'], icon_asset_path: '/api/plugins/demo.three/icon.svg' },
      ],
      next_cursor: 'cursor-2',
      total_plugins: 5,
    }),
    installedImpl: async () => okInstalled([
      { plugin_id: 'demo.one', state: 'connected', title: 'Demo One', summary: 'd1', trust_tier: 'omega_verified', publisher_id: 'google', registry_revision: 'b'.repeat(64), package_version: '1.0.0', apps: [], next_required_app_id: null, enabled: true, error_code: null },
    ]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  const cards = panel.querySelectorAll('[data-plugin-id]');
  assert.equal(cards.length, 3);
  const categoryTitles = panel.querySelectorAll('.plugins-category-title').map(el => el.textContent);
  assert.deepEqual(categoryTitles.sort(), ['Productivity', 'Utilities']);
  assert.equal(panel.querySelector('[data-plugin-count]').textContent, '5');
  assert.equal(panel.querySelector('[data-plugin-load-more]').hidden, false);
  const installedList = panel.querySelector('[data-plugin-installed-list]');
  const installedText = installedList.textContent;
  assert.match(installedText, /Demo One/);
  assert.match(installedText, /Connected/);
  assert.ok(
    installedList.querySelector('.provider-icon-google'),
    'verified installed provider identity renders its provider icon',
  );
  assert.equal(calls.catalog.length, 1);
  assert.equal(calls.installed, 1);

  const demoThree = cards.find(c => c.getAttribute('data-plugin-id') === 'demo.three');
  assert.ok(demoThree.querySelector('img'), 'a trusted icon_asset_path renders <img>');
  const demoOne = cards.find(c => c.getAttribute('data-plugin-id') === 'demo.one');
  assert.equal(demoOne.querySelector('img'), null, 'no icon_asset_path -- glyph only');
});

await test('installed plugin absent from the loaded catalog page still renders its real title', async () => {
  // The catalog page carries only demo.one; demo.two is installed but not
  // on this page (or in any catalog source at all -- e.g. a Personal
  // plugin). Its title must still come from the /installed payload itself.
  const { context } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({
      plugins: [{ plugin_id: 'demo.one', title: 'Demo One', description: 'd1', categories: [], icon_asset_path: null }],
    }),
    installedImpl: async () => okInstalled([
      { plugin_id: 'demo.one', state: 'connected', title: 'Demo One', summary: 'd1', trust_tier: 'omega_verified', registry_revision: 'b'.repeat(64), package_version: '1.0.0', apps: [], next_required_app_id: null, enabled: true, error_code: null },
      { plugin_id: 'demo.two', state: 'connected', title: 'Personal Two', summary: 'd2', trust_tier: 'personal', registry_revision: 'c'.repeat(64), package_version: '1.0.0', apps: [], next_required_app_id: null, enabled: true, error_code: null },
    ]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  const installedText = panel.querySelector('[data-plugin-installed-list]').textContent;
  assert.match(installedText, /Personal Two/, 'installed strip must show the real title, not a raw plugin_id');
  assert.doesNotMatch(installedText, /demo\.two/, 'must not fall back to the raw plugin_id when a title is available');
});

await test('composer sync (omega:plugin-directory) carries titled installed plugins independent of the catalog page', async () => {
  const { context, dispatchedEvents } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({ plugins: [] }),
    installedImpl: async () => okInstalled([
      { plugin_id: 'demo.two', state: 'connected', title: 'Personal Two', summary: 'd2', trust_tier: 'personal', registry_revision: 'c'.repeat(64), package_version: '1.0.0', apps: [], next_required_app_id: null, enabled: true, error_code: null },
    ]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  const directoryEvents = dispatchedEvents.filter(e => e.type === 'omega:plugin-directory');
  assert.ok(directoryEvents.length >= 1, 'omega:plugin-directory must be dispatched so the chat composer stays in sync');
  const latest = directoryEvents[directoryEvents.length - 1];
  const synced = latest.detail.plugins.find(p => p.plugin_id === 'demo.two');
  assert.ok(synced, 'the composer payload must include the installed plugin');
  assert.equal(synced.title, 'Personal Two');
  assert.equal(synced.status, 'connected');
  assert.equal(synced.installed, true);
});

await test('load more appends and de-duplicates, hiding once the cursor is exhausted', async () => {
  let page = 1;
  const { context } = loadPanelContext({
    catalogImpl: async (params) => {
      if (page === 1) {
        page += 1;
        return okCatalogPage({
          plugins: [{ plugin_id: 'demo.one', title: 'Demo One', description: 'd', categories: [], icon_asset_path: null }],
          next_cursor: 'cursor-2',
          total_plugins: 2,
        });
      }
      assert.equal(params.cursor, 'cursor-2', 'load-more must send the prior next_cursor');
      return okCatalogPage({
        plugins: [{ plugin_id: 'demo.two', title: 'Demo Two', description: 'd', categories: [], icon_asset_path: null }],
        next_cursor: null,
        total_plugins: 2,
      });
    },
    installedImpl: async () => okInstalled([]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();
  assert.equal(panel.querySelectorAll('[data-plugin-id]').length, 1);

  const loadMore = panel.querySelector('[data-plugin-load-more]');
  loadMore.dispatchClick();
  await flush();

  const ids = panel.querySelectorAll('[data-plugin-id]').map(el => el.getAttribute('data-plugin-id'));
  assert.deepEqual(ids.sort(), ['demo.one', 'demo.two']);
  assert.equal(loadMore.hidden, true, 'the button hides once next_cursor is null');
});

await test('tab switch forces trust=personal and renders the exact zero-item text', async () => {
  const { context } = loadPanelContext({
    catalogImpl: async (params) => {
      if (params.trust === 'personal') return okCatalogPage({ plugins: [] });
      return okCatalogPage({ plugins: [{ plugin_id: 'demo.one', title: 'Demo One', description: 'd', categories: [], icon_asset_path: null }] });
    },
    installedImpl: async () => okInstalled([]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  const personalTab = panel.querySelector('[data-plugin-tab="personal"]');
  personalTab.dispatchClick();
  await flush();

  assert.equal(personalTab.getAttribute('aria-selected'), 'true');
  assert.match(panel.querySelector('[data-plugin-public-list]').textContent, /No plugins match this search/);
});

await test('search is debounced: rapid keystrokes coalesce into exactly one request with no stale cursor', async () => {
  const { context, calls, fakeTimers } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({ plugins: [] }),
    installedImpl: async () => okInstalled([]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();
  const callsBeforeSearch = calls.catalog.length;

  const search = panel.querySelector('[data-plugin-search]');
  search.value = 'de';
  search.fire('input');
  assert.equal(fakeTimers.pendingCount(), 1, 'the first keystroke schedules one debounce timer');
  search.value = 'demo';
  search.fire('input');
  assert.equal(fakeTimers.pendingCount(), 1, 'a second keystroke clears the first timer and schedules a new one, not two');
  assert.equal(calls.catalog.length, callsBeforeSearch, 'no request fires before the debounce timer runs');

  fakeTimers.flush();
  await flush();

  assert.equal(calls.catalog.length, callsBeforeSearch + 1, 'exactly one request fires after debounce');
  const sent = calls.catalog[calls.catalog.length - 1];
  assert.equal(sent.q, 'demo', 'the coalesced request carries the latest value');
  assert.equal(sent.cursor, undefined, 'a fresh search must not carry a stale cursor');
});

await test('tab-switch stale-response race: an out-of-order late response cannot overwrite the fresher one', async () => {
  const responders = [];
  const { context } = loadPanelContext({
    catalogImpl: async (params) => {
      const d = deferred();
      responders.push({ tab: params.trust === 'personal' ? 'personal' : 'public', deferred: d });
      return d.promise;
    },
    installedImpl: async () => okInstalled([]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();
  // responders[0] is the initial Public load; resolve it immediately so it
  // does not interfere with the race below.
  responders[0].deferred.resolve(okCatalogPage({ plugins: [] }));
  await flush();

  const personalTab = panel.querySelector('[data-plugin-tab="personal"]');
  const publicTab = panel.querySelector('[data-plugin-tab="public"]');

  personalTab.dispatchClick(); // request A for 'personal' (older)
  publicTab.dispatchClick();   // request B for 'public' (irrelevant tab)
  personalTab.dispatchClick(); // request C for 'personal' (newer)

  // The initial load was the Public tab (already resolved above), so every
  // Personal-tab responder from here on is one of the two racing requests.
  const personalRequests = responders.filter(r => r.tab === 'personal');
  assert.equal(personalRequests.length, 2, 'exactly two Personal-tab requests should be in flight');
  const [requestA, requestC] = personalRequests;

  // Resolve out of order: the newer request (C) resolves first, the
  // stale older one (A) resolves after it.
  requestC.deferred.resolve(okCatalogPage({
    plugins: [{ plugin_id: 'fresh', title: 'Fresh Result', description: 'd', categories: [], icon_asset_path: null }],
    total_plugins: 1,
  }));
  await flush();
  requestA.deferred.resolve(okCatalogPage({
    plugins: [{ plugin_id: 'stale', title: 'Stale Result', description: 'd', categories: [], icon_asset_path: null }],
    total_plugins: 1,
  }));
  await flush();

  const ids = panel.querySelectorAll('[data-plugin-id]').map(el => el.getAttribute('data-plugin-id'));
  assert.deepEqual(ids, ['fresh'], 'the stale, later-arriving response must not overwrite the fresher one');
});

await test('degraded catalog status shows the source banner', async () => {
  const { context } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({
      status: 'degraded',
      plugins: [{ plugin_id: 'demo.one', title: 'Demo One', description: 'd', categories: [], icon_asset_path: null }],
      total_plugins: 1,
    }),
    installedImpl: async () => okInstalled([]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  assert.equal(panel.querySelector('[data-plugin-source-banner]').hidden, false);
});

await test('a thrown catalog error renders its precise, server-mapped recovery message', async () => {
  const { context } = loadPanelContext({
    catalogImpl: async () => {
      const err = new Error('plugin_catalog_unavailable');
      err.code = 'plugin_catalog_unavailable';
      throw err;
    },
    installedImpl: async () => okInstalled([]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  const errorBox = panel.querySelector('[data-plugin-directory-error]');
  assert.equal(errorBox.hidden, false);
  assert.match(errorBox.textContent, /signed plugin catalog is unavailable/i);
});

await test('installed strip renders one visible failure row for an unrecognized lifecycle state, never hiding it', async () => {
  const { context } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({ plugins: [] }),
    installedImpl: async () => okInstalled([
      { plugin_id: 'demo.bad', state: 'not_a_real_state', title: 'Bad Plugin', summary: '', trust_tier: 'omega_verified', registry_revision: 'b'.repeat(64), package_version: '1.0.0', apps: [], next_required_app_id: null, enabled: true, error_code: null },
    ]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  const installedText = panel.querySelector('[data-plugin-installed-list]').textContent;
  assert.match(installedText, /unrecognized state/i);
});

await test('plugin_permission_changed uses the shared vocabulary\'s exact wording, not a drifted private one', async () => {
  // Task-6 fix round 1 finding 2: the panel's own now-deleted private map
  // told the user to "try Connect again" for plugin_permission_changed,
  // contradicting controller ruling 4 (which opens the diff, never
  // Connect). window.readablePluginError (core/plugin-permissions.js) is
  // the one shared vocabulary now -- this proves the panel actually calls
  // it, not a drifted copy.
  const { context } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({ plugins: [] }),
    installedImpl: async () => {
      const err = new Error('plugin_permission_changed');
      err.code = 'plugin_permission_changed';
      throw err;
    },
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  const installedText = panel.querySelector('[data-plugin-installed-list]').textContent;
  assert.equal(
    installedText.includes(context.readablePluginError({ code: 'plugin_permission_changed' })),
    true,
  );
  assert.equal(installedText.includes('try Connect again'), false);
});

// ===========================================================================
// Task 8: final accessibility / responsive / source-of-truth gate
// ===========================================================================

await test(
  'a connected plugin always paints its management row, even when the catalog resolves before installed data (controller ruling 2)',
  async () => {
    // Deterministic RED-first regression for controller ruling 2: hold
    // /installed pending with a real, externally-resolved promise so the
    // catalog fetch settles and renders (its first, only) card *before*
    // any installed data exists -- exactly the ordering the real two
    // independent fetch() calls can produce in a browser, made
    // deterministic here instead of depending on microtask race luck.
    const installedDeferred = deferred();
    const { context } = loadPanelContext({
      catalogImpl: async () => okCatalogPage({
        plugins: [{ plugin_id: 'demo.one', title: 'Demo One', description: 'd', categories: [], icon_asset_path: null }],
      }),
      installedImpl: () => installedDeferred.promise,
    });
    const panel = new Element('div');
    context._renderSettingsPlugins(panel);
    await flush();

    const beforeInstalled = panel.querySelector('[data-plugin-id="demo.one"]');
    assert.ok(beforeInstalled, 'the catalog card renders even before installed data arrives');
    assert.equal(
      beforeInstalled.querySelector('.plugins-card-management'),
      null,
      'not yet known to be installed, so no management row is expected yet',
    );

    installedDeferred.resolve(okInstalled([
      { plugin_id: 'demo.one', state: 'connected', title: 'Demo One', summary: 'd', trust_tier: 'omega_verified', registry_revision: 'b'.repeat(64), package_version: '1.0.0', apps: [], next_required_app_id: null, enabled: true, error_code: null },
    ]));
    await flush();

    // No extra interaction (e.g. a search keystroke) forces this
    // re-render -- refreshInstalled() itself re-renders the catalog
    // section once installed data is known.
    const card = panel.querySelector('[data-plugin-id="demo.one"]');
    assert.ok(card, 'the card still renders once installed data has arrived');
    const management = card.querySelector('.plugins-card-management');
    assert.ok(management, 'a connected card always paints its management row once installed data is known');
    assert.ok(card.querySelector('[data-plugin-try]'), 'the management row includes Try in chat for a connected plugin');
  },
);

await test('a single catalog page over the ceiling never renders more than the ceiling', async () => {
  const oversized = Array.from({length: 30}, (_, i) => ({
    plugin_id: 'demo.' + i, title: 'Demo ' + i, description: 'd', categories: [], icon_asset_path: null,
  }));
  const { context } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({plugins: oversized, total_plugins: oversized.length}),
    installedImpl: async () => okInstalled([]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  const cards = panel.querySelectorAll('[data-plugin-id]');
  assert.equal(cards.length, 24, 'a page carrying 30 items must still render only the 24-item ceiling');
});

await test('a connecting plugin can disconnect and return to authoritative setup without uninstalling', async () => {
  let state = 'connecting';
  const { context } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({plugins: [{
      plugin_id: 'demo.one', title: 'Demo One', description: 'd', categories: [], icon_asset_path: null,
    }]}),
    installedImpl: async () => okInstalled([{
      plugin_id: 'demo.one', state, title: 'Demo One', summary: 'd',
      trust_tier: 'omega_verified', registry_revision: 'b'.repeat(64), package_version: '1.0.0',
      apps: [], next_required_app_id: 'demo.one.mcp', enabled: false, error_code: null,
    }]),
  });
  context.OmegaPluginApi.disconnect = async pluginId => {
    assert.equal(pluginId, 'demo.one');
    state = 'setup_required';
  };
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();
  const card = panel.querySelector('[data-plugin-id="demo.one"]');
  const disconnect = card.querySelector('[data-plugin-disconnect]');
  assert.equal(disconnect.disabled, false, 'unfinished sign-ins must have a reachable cancellation');
  disconnect.dispatchClick();
  await flush(12);
  const remaining = panel.querySelector('[data-plugin-id="demo.one"]');
  assert.ok(remaining, 'disconnect must preserve the installed package');
  assert.equal(remaining.querySelector('[data-plugin-action]').textContent, 'Continue setup');
  assert.equal(remaining.querySelector('[data-plugin-action]').disabled, false);
});

await test('keyboard reachability: every clickable control is a native-focusable tag, and no positive tabindex disrupts tab order', async () => {
  const { context } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({
      plugins: [{ plugin_id: 'demo.one', title: 'Demo One', description: 'd', categories: [], icon_asset_path: null }],
    }),
    installedImpl: async () => okInstalled([
      { plugin_id: 'demo.one', state: 'connected', title: 'Demo One', summary: 'd', trust_tier: 'omega_verified', registry_revision: 'b'.repeat(64), package_version: '1.0.0', apps: [], next_required_app_id: null, enabled: true, error_code: null },
    ]),
  });
  const panel = new Element('div');
  context._renderSettingsPlugins(panel);
  await flush();

  const NATIVE_FOCUSABLE = new Set(['button', 'a', 'input', 'select', 'textarea']);
  // data-plugin-retry is excluded: it only renders inside the directory
  // error banner (a different, mutually exclusive render state from the
  // healthy connected-card fixture this test uses), not because it is
  // exempt from the same focusability rule -- it is still a <button> in
  // the source (see _pluginDrawerShellMarkup's sibling errorBox markup).
  const ACTION_ATTRS = [
    'data-plugin-tab', 'data-plugin-details', 'data-plugin-more', 'data-plugin-action',
    'data-plugin-disconnect', 'data-plugin-uninstall', 'data-plugin-authority',
    'data-plugin-try', 'data-plugin-filter', 'data-plugin-load-more', 'data-plugin-settings',
  ];
  const all = walkAllElements(panel);
  const actionable = all.filter(el => ACTION_ATTRS.some(attr => el.hasAttribute(attr)));
  // The fixture (a connected, installed plugin plus a full toolbar) must
  // actually exercise every one of the closed action attributes above --
  // otherwise this test would pass vacuously for whichever ones never
  // rendered.
  ACTION_ATTRS.forEach(attr => {
    assert.ok(all.some(el => el.hasAttribute(attr)), `fixture never rendered an element carrying ${attr}`);
  });
  actionable.forEach(el => {
    assert.ok(
      NATIVE_FOCUSABLE.has(el.tagName),
      `a clickable control (<${el.tagName}> with one of ${ACTION_ATTRS.join(', ')}) must be a native-focusable tag, never a mouse-only element`,
    );
    assert.notEqual(el.getAttribute('tabindex'), '-1', `a clickable <${el.tagName}> control must stay reachable by Tab`);
  });

  // Tab order sanity: nothing anywhere in the rendered panel uses a
  // positive tabindex to jump the natural DOM order.
  all.forEach(el => {
    const tabindex = el.getAttribute('tabindex');
    if (tabindex !== null) {
      assert.ok(
        tabindex === '0' || tabindex === '-1',
        `unexpected tabindex="${tabindex}" on <${el.tagName}> -- only "0" or "-1" are allowed`,
      );
    }
  });
});

// ===========================================================================
// Final fix wave W1: exactly one state->action/label/tone/disabled-actions
// table. The panel's private _pluginPrimaryAction/_PLUGIN_LIFECYCLE_STATE_
// LABELS/_PLUGIN_LIFECYCLE_STATE_TONE literals are gone -- renderCard now
// calls window.lifecycleView(plugin) directly. Swapping that shared
// function for a spy that returns a distinctive, state-tagged view for
// every canonical state proves genuine runtime delegation (not merely two
// independently-maintained tables that happen to still agree): a rendered
// card can only show the spy's own sentinel values if the panel truly
// calls window.lifecycleView at render time.
// ===========================================================================

await test('every canonical lifecycle state renders exactly the view window.lifecycleView returns', async () => {
  const STATES = [
    'available', 'review_required', 'installing', 'installed', 'setup_required',
    'connecting', 'connected', 'offline', 'update_available', 'update_required',
    'permission_changed', 'revocation_pending', 'blocked', 'incompatible',
  ];
  const NO_ACTION_STATES = new Set(['connected', 'incompatible']);
  const pluginId = state => 'plugin.' + state;
  const { context } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({
      plugins: STATES.map(state => ({
        plugin_id: pluginId(state), title: 'Plugin ' + state, description: 'd',
        categories: ['Sentinel'], icon_asset_path: null,
      })),
    }),
    installedImpl: async () => okInstalled(
      STATES.filter(state => state !== 'available').map(state => ({
        plugin_id: pluginId(state), state, title: 'Plugin ' + state, summary: 'd',
        trust_tier: 'omega_verified', registry_revision: 'b'.repeat(64), package_version: '1.0.0',
        apps: [], next_required_app_id: null, enabled: true, error_code: null,
      })),
    ),
  });
  const originalLifecycleView = context.lifecycleView;
  const seenStates = [];
  const spy = plugin => {
    const state = plugin && plugin.state != null ? plugin.state : (plugin && plugin.status);
    seenStates.push(state);
    return {
      state,
      label: 'SENTINEL-LABEL-' + state,
      tone: 'is-sentinel-' + state,
      primaryAction: NO_ACTION_STATES.has(state) ? null : {
        type: 'sentinel',
        label: 'SENTINEL-ACTION-' + state,
        disabled: state === 'installing' || state === 'connecting',
      },
      disabledActions: state === 'revocation_pending' ? ['disconnect', 'uninstall', 'permissions'] : [],
    };
  };
  context.lifecycleView = spy;
  context.window.lifecycleView = spy;
  try {
    const panel = new Element('div');
    context._renderSettingsPlugins(panel);
    await flush();
    STATES.forEach(state => {
      const card = panel.querySelector('[data-plugin-id="' + pluginId(state) + '"]');
      assert.ok(card, 'no card rendered for state ' + state);
      const button = card.querySelector('[data-plugin-action]');
      if (NO_ACTION_STATES.has(state)) {
        assert.equal(button.textContent, 'SENTINEL-LABEL-' + state, 'state ' + state + ' must fall back to lifecycleView\'s own label when primaryAction is null');
        assert.equal(button.disabled, true, 'state ' + state + ' has no primary action and must render disabled');
      } else {
        assert.equal(button.textContent, 'SENTINEL-ACTION-' + state, 'state ' + state + ' did not render lifecycleView\'s own action label');
        assert.equal(button.disabled, state === 'installing' || state === 'connecting', 'state ' + state + ' did not render lifecycleView\'s own disabled flag');
      }
    });
    const revokingCard = panel.querySelector('[data-plugin-id="' + pluginId('revocation_pending') + '"]');
    assert.equal(revokingCard.querySelector('[data-plugin-authority]').disabled, true, 'disabledActions from lifecycleView must disable Permissions');
    assert.equal(revokingCard.querySelector('[data-plugin-uninstall]').disabled, true, 'disabledActions from lifecycleView must disable Uninstall');
    assert.deepEqual([...new Set(seenStates)].sort(), [...STATES].sort(), 'every one of the 14 canonical states must actually reach window.lifecycleView');
  } finally {
    context.lifecycleView = originalLifecycleView;
    context.window.lifecycleView = originalLifecycleView;
  }
});

await test('an installed setup failure can review an available update without hiding its connection state', async () => {
  for (const state of ['setup_required', 'connecting', 'offline', 'installed']) {
    const { context } = loadPanelContext({
      catalogImpl: async () => okCatalogPage({plugins: [{
        plugin_id: 'demo.one', title: 'Demo One', description: 'd', categories: [],
        plugin_version: '2.0.0', update_state: 'available',
      }]}),
      installedImpl: async () => okInstalled([{
        plugin_id: 'demo.one', state, title: 'Demo One', summary: 'd',
        trust_tier: 'omega_verified', registry_revision: 'b'.repeat(64), package_version: '1.0.0',
        apps: [], next_required_app_id: null, enabled: false, error_code: null,
      }]),
    });
    const reviews = [];
    context.OmegaPluginApi.updateReview = async (...args) => {
      reviews.push(args);
      throw Object.assign(new Error('review fixture'), {code: 'plugin_install_review_changed'});
    };
    const panel = new Element('div');
    context._renderSettingsPlugins(panel);
    await flush();
    const card = panel.querySelector('[data-plugin-id="demo.one"]');
    const button = card.querySelector('[data-plugin-update-review]');
    assert.ok(button, state + ' must expose the signed update review');
    assert.equal(card.querySelector('[data-plugin-status]').textContent, context.lifecycleView({state}).label.toLowerCase());
    button.dispatchClick();
    await flush(12);
    assert.equal(reviews.length, 1);
    assert.equal(reviews[0][0], 'demo.one');
    assert.equal(reviews[0][1], '2.0.0');
  }
});

if (failures > 0) {
  console.error(`${failures} test(s) failed`);
  process.exit(1);
}
console.log('plugin marketplace panel runtime tests passed');

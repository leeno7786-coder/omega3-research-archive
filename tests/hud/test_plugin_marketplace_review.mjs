// Runtime behavior test for the details/trust-evidence/review/install/update
// surfaces added to hud/ui-src/panels/settings-plugins.js. No jsdom
// dependency exists in this repo, so this file uses the shared small but
// real DOM shim (HTML parser, querySelector[All]/closest/classList/dataset,
// event delegation, and a focus/activeElement tracker for the drawer's
// accessibility requirements -- tests/hud/plugin_dom_shim.mjs, follow-up
// refactor wave W2) the same approach
// tests/hud/test_plugin_marketplace_panel.mjs already established.
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
// The panel now calls window.readablePluginError directly (task-6 fix
// round 1 finding 2: one shared error vocabulary, no private cause table)
// -- core/plugin-permissions.js must load before settings-plugins.js here,
// exactly as it does in every real build (the chat_feed core inject slot,
// ahead of panels).
const permissionsSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-permissions.js'), 'utf8');
// core/plugin-views.js carries the panel's former private details/trust
// badge/review-summary/update-diff/drawer-shell view builders (follow-up
// refactor wave W1) -- it must also load before settings-plugins.js here,
// exactly as it does in every real build (the chat_feed core inject slot,
// ahead of panels, right after plugin-permissions.js).
const viewsSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-views.js'), 'utf8');
// core/plugin-setup.js (window.OmegaPluginSetup) is the one setup machine the
// panel drives once the operator submits an app's consent form -- loaded here
// exactly as in every real build (a core module, ahead of panels), so a
// confirmed update's sign-in runs the real machine, never a missing global.
const setupSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-setup.js'), 'utf8');
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

function loadPanelContext({
  catalogImpl, installedImpl, reviewImpl, installImpl, updateImpl, detailsImpl, updateReviewImpl,
  statusImpl, startConnectionImpl, permissionsImpl, savePermissionsImpl,
} = {}) {
  const windowListeners = new Map();
  const context = {
    console,
    CustomEvent: class CustomEvent { constructor(type, init) { this.type = type; this.detail = init && init.detail; } },
    Object, Array, Error, Promise, String, Number, Boolean, Map, Set, JSON, Math, Date,
    // The panel builds its post-sign-in return URL with the browser's URL.
    URL,
    _escapeHtml: esc,
  };
  context.window = context;
  context.globalThis = context;
  context.setTimeout = (fn) => { fn(); return 0; };
  context.clearTimeout = () => {};
  context.window.setTimeout = context.setTimeout;
  context.window.clearTimeout = context.clearTimeout;
  // A real listener-capable node (not a stub) so a test can register a
  // document-level Escape listener standing in for register.js's global
  // `window.addEventListener('keydown', ...)` handler and observe (via
  // real bubble-phase dispatch, see Element#fireKeydown) whether the
  // drawer's own Escape handling reaches it.
  const documentNode = new Element('#document');
  documentNode.createElement = (tag) => new Element(tag);
  documentNode.getElementById = () => null;
  context.document = documentNode;
  Object.defineProperty(context.document, 'activeElement', { get: () => focusTracker.current });
  // Every same-window navigation and every popup reservation is recorded, so
  // a test can prove exactly when (and whether) a sign-in window is opened
  // and where the setup machine sends the operator.
  const navigations = [];
  const popupOpens = [];
  context.window.location = {
    origin: 'http://127.0.0.1:13310',
    assign(url) { navigations.push({ kind: 'assign', url }); },
    replace(url) { navigations.push({ kind: 'replace', url }); },
  };
  context.window.open = (...args) => { popupOpens.push(args); return null; };
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
  const calls = {
    catalog: [], installed: 0,
    reviewCalls: [], installCalls: [], updateCalls: [], detailsCalls: [], updateReviewCalls: [],
    statusCalls: [], startConnectionCalls: [], permissionsCalls: [], savePermissionsCalls: [],
    navigations, popupOpens,
  };
  context.window.OmegaPluginApi = {
    catalog: (params) => { calls.catalog.push(params); return catalogImpl(params); },
    installed: () => { calls.installed += 1; return installedImpl(); },
    review: (pluginId, revision) => {
      calls.reviewCalls.push({ pluginId, revision });
      return reviewImpl(pluginId, revision);
    },
    install: (pluginId, revision, confirmation) => {
      const record = { pluginId, revision, confirmation };
      calls.installCalls.push(record);
      return installImpl(pluginId, revision, confirmation);
    },
    update: (pluginId, revision, version, confirmation) => {
      const record = { pluginId, revision, version, confirmation };
      calls.updateCalls.push(record);
      return updateImpl(pluginId, revision, version, confirmation);
    },
    updateReview: (pluginId, version, revision) => {
      const record = { pluginId, version, revision };
      calls.updateReviewCalls.push(record);
      return updateReviewImpl(pluginId, version, revision);
    },
    details: (pluginId) => {
      calls.detailsCalls.push(pluginId);
      return detailsImpl(pluginId);
    },
    startConnection: startConnectionImpl
      ? (pluginId, revision, appId) => {
          calls.startConnectionCalls.push({ pluginId, revision, appId });
          return startConnectionImpl(pluginId, revision, appId);
        }
      : unused('unused'),
    pollConnection: unused('unused'),
    permissions: permissionsImpl
      ? (pluginId, appId) => {
          calls.permissionsCalls.push({ pluginId, appId });
          return permissionsImpl(pluginId, appId);
        }
      : unused('unused'),
    savePermissions: savePermissionsImpl
      ? (pluginId, revision, settings, appId) => {
          calls.savePermissionsCalls.push({ pluginId, revision, settings, appId });
          return savePermissionsImpl(pluginId, revision, settings, appId);
        }
      : unused('unused'),
    tryInChat: unused('unused'),
    disconnect: unused('unused'),
    uninstall: unused('unused'),
    sync: unused('unused'),
    status: statusImpl
      ? (pluginId) => { calls.statusCalls.push(pluginId); return statusImpl(pluginId); }
      : unused('unused'),
  };
  vm.createContext(context);
  vm.runInContext(storeSource, context, { filename: 'plugin-store.js' });
  vm.runInContext(setupSource, context, { filename: 'plugin-setup.js' });
  vm.runInContext(permissionsSource, context, { filename: 'plugin-permissions.js' });
  vm.runInContext(viewsSource, context, { filename: 'plugin-views.js' });
  vm.runInContext(panelSource, context, { filename: 'settings-plugins.js' });
  return { context, calls, dispatchedEvents };
}

async function flush(n = 12) {
  for (let i = 0; i < n; i += 1) await Promise.resolve();
}

// Flushes microtasks until `predicate()` holds, failing with `what` if it
// never does -- for multi-hop async chains (save -> status -> setup machine)
// whose exact hop count is not this test's business.
async function flushUntil(predicate, what) {
  for (let i = 0; i < 400; i += 1) {
    if (predicate()) return;
    await Promise.resolve();
  }
  assert.fail('never observed: ' + what);
}

// ===========================================================================
// renderDetails/plugin(...) -- pure, source-contract cases for the trust
// badge and details sections (no store, no API, no drawer wiring needed).
// ===========================================================================

const detailsCtx = (() => {
  const ctx = { console, Object, Array, Error, Map, Set, String, Boolean, _escapeHtml: esc };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(storeSource, ctx, { filename: 'plugin-store.js' });
  vm.runInContext(permissionsSource, ctx, { filename: 'plugin-permissions.js' });
  vm.runInContext(viewsSource, ctx, { filename: 'plugin-views.js' });
  vm.runInContext(panelSource, ctx, { filename: 'settings-plugins.js' });
  return ctx;
})();

function plugin(overrides = {}) {
  return {
    plugin_id: 'weather',
    title: 'Weather',
    summary: 'Live forecasts for any city.',
    publisher_id: 'acme',
    plugin_version: '2.0.0',
    categories: ['weather'],
    distribution: { kind: 'remote_mcp', remote_origin: 'https://weather.example.com' },
    offline_capable: false,
    support_url: 'https://weather.example.com/support',
    source_url: 'https://weather.example.com',
    release_history: [],
    ...overrides,
  };
}

function renderDetails(details) {
  const el = new Element('div');
  el.innerHTML = detailsCtx._pluginDetailsSectionsMarkup(details, esc);
  return el;
}

await test('Omega Verified renders with the entry-supplied evidence link', () => {
  const detail = renderDetails(plugin({
    trust_tier: 'omega_verified',
    source_url: 'https://weather.example.com/verify',
  }));
  assert.match(detail.textContent, /Omega Verified/);
  const evidenceLink = detail.querySelectorAll('a')
    .find(link => link.getAttribute('href') === 'https://weather.example.com/verify');
  assert.ok(evidenceLink, 'an evidence link to the entry\'s own source_url is rendered');
  assert.equal(evidenceLink.getAttribute('target'), '_blank');
  assert.equal(evidenceLink.getAttribute('rel'), 'noopener noreferrer');
});

await test('Personal renders the Personal label, never Official', () => {
  const detail = renderDetails(plugin({ trust_tier: 'personal' }));
  assert.match(detail.textContent, /Personal/);
  assert.doesNotMatch(detail.textContent, /Official/);
});

await test('an unrecognized trust tier is a visible failure, not a silent default', () => {
  const detail = renderDetails(plugin({ trust_tier: 'not_a_real_tier' }));
  const alertNode = detail.querySelector('[role="alert"]');
  assert.ok(alertNode, 'an alert-role element renders the failure');
  assert.match(alertNode.textContent, /not_a_real_tier/);
});

await test('external support/source links carry rel="noopener noreferrer" and target="_blank"', () => {
  const detail = renderDetails(plugin({
    trust_tier: 'omega_verified',
    source_url: 'https://weather.example.com',
    support_url: 'https://weather.example.com/support',
  }));
  const links = detail.querySelectorAll('a');
  assert.ok(links.length >= 2, 'both a support and a source link render');
  links.forEach(link => {
    assert.equal(link.getAttribute('rel'), 'noopener noreferrer');
    assert.equal(link.getAttribute('target'), '_blank');
  });
});

// ===========================================================================
// reviewHarness -- drives the real _renderSettingsPlugins against a mocked
// OmegaPluginApi and the real OmegaPluginStore, exactly like
// test_plugin_marketplace_panel.mjs's runtime scenarios.
// ===========================================================================

function reviewHarness(options = {}) {
  const revision = options.revision || 'a'.repeat(64);
  const pluginId = options.pluginId || 'weather';
  const installed = options.installed !== false;

  // The install-review shape (POST .../review's own `review` field --
  // PluginInstallReview.to_dict()) -- used when `installed: false`.
  const installReviewObj = {
    plugin_id: pluginId,
    publisher_id: 'acme',
    plugin_version: '2.0.0',
    kind: 'mcp_backed',
    catalog_sha256: 'c'.repeat(64),
    manifest_sha256: 'm'.repeat(64),
    package_sha256: 'p'.repeat(64),
    signing_key_id: 'acme-key',
    expected_registry_revision: revision,
    required_app_ids: ['weather.app'],
    optional_app_ids: [],
    dependencies: [],
    sha256: 's'.repeat(64),
    ...(options.installReview || {}),
  };

  // The update-review shape (GET .../update-review's own `review` field --
  // PluginUpdateReview.to_dict()) -- used when `installed: true` (default).
  // permission_changes defaults to one real endpoint change so tests
  // exercising the diff have something concrete to assert on.
  const updateReviewObj = {
    plugin_id: pluginId,
    from_version: '1.0.0',
    to_version: '2.0.0',
    permission_changes: ['endpoint_changed:weather.app'],
    sha256: 'u'.repeat(64),
    ...(options.review || {}),
  };

  const reviewObj = installed ? updateReviewObj : installReviewObj;

  const detailsObj = {
    plugin_id: pluginId,
    title: 'Weather',
    summary: 'Live forecasts for any city.',
    publisher_id: 'acme',
    trust_tier: 'omega_verified',
    plugin_version: '2.0.0',
    categories: ['weather'],
    distribution: { kind: 'remote_mcp', remote_origin: 'https://weather.example.com' },
    offline_capable: false,
    support_url: 'https://weather.example.com/support',
    source_url: 'https://weather.example.com',
    release_history: [],
    installed_authority: installed ? {
      capabilities: ['tool:forecast'],
      apps: [{
        app_id: 'weather.app',
        authentication_kind: 'none',
        requested_scopes: [],
        remote_origin: 'https://old.weather.example.com',
      }],
      network_origins: ['https://old.weather.example.com'],
      filesystem_access: false,
    } : undefined,
    ...(options.details || {}),
  };

  const installedPlugins = options.installedPlugins !== undefined ? options.installedPlugins : (
    installed ? [{
      plugin_id: pluginId,
      state: 'update_available',
      title: 'Weather',
      summary: 'Live forecasts for any city.',
      trust_tier: 'omega_verified',
      registry_revision: revision,
      package_version: '1.0.0',
      apps: [],
      next_required_app_id: null,
      enabled: true,
      error_code: null,
    }] : []
  );

  const catalogPlugins = options.catalogPlugins || [
    { plugin_id: pluginId, title: 'Weather', description: 'Live forecasts.', categories: ['weather'], icon_asset_path: null },
  ];

  const reviewImpl = options.reviewImpl || (async () => ({ ok: true, revision, review: installReviewObj }));
  const updateReviewImpl = options.updateReviewImpl || (async () => ({ ok: true, revision, review: updateReviewObj }));
  const detailsImpl = options.detailsImpl || (async () => ({ ok: true, revision, plugin: detailsObj }));
  const installImpl = options.installImpl || (async () => (
    { ok: true, revision, plugin_id: pluginId, registry_revision: revision, dependency_receipt_id: 'receipt-1' }
  ));
  const updateImpl = options.updateImpl || (async () => (
    { ok: true, revision, plugin_id: pluginId, state: 'connected' }
  ));

  const { context, calls } = loadPanelContext({
    catalogImpl: async () => okCatalogPage({ plugins: catalogPlugins, total_plugins: catalogPlugins.length, revision }),
    installedImpl: async () => okInstalled(installedPlugins, revision),
    reviewImpl,
    updateReviewImpl,
    detailsImpl,
    installImpl,
    updateImpl,
    statusImpl: options.statusImpl,
    startConnectionImpl: options.startConnectionImpl,
    permissionsImpl: options.permissionsImpl,
    savePermissionsImpl: options.savePermissionsImpl,
  });
  const panel = new Element('div');
  // Links the panel into the document's bubble-phase chain so
  // Element#fireKeydown's walk up through .parent can reach a
  // document-level listener, exactly as a real dialog's Escape keydown
  // bubbles up through document to window.
  panel.parent = context.document;
  context._renderSettingsPlugins(panel);

  const openAction = async (id) => {
    await flush();
    const card = panel.querySelector('[data-plugin-id="' + id + '"]');
    const actionButton = card.querySelector('[data-plugin-action]');
    actionButton.dispatchClick();
    await flush();
  };

  return {
    context,
    api: calls,
    revision,
    review: reviewObj,
    details: detailsObj,
    panel,
    openUpdate: openAction,
    openInstall: openAction,
    async confirmReview() {
      const confirmButton = panel.querySelector('[data-plugin-review-confirm]');
      assert.ok(confirmButton, 'a confirm control must exist inside the loaded review');
      confirmButton.dispatchClick();
      await flush();
    },
    async cancelReview() {
      const cancelButton = panel.querySelector('[data-plugin-review-cancel]');
      assert.ok(cancelButton, 'a cancel control must exist inside the loaded review');
      cancelButton.dispatchClick();
      await flush();
    },
  };
}

// --- The brief's two verbatim Step-1 tests ---------------------------------

await test('publisher verified is never labeled Official', () => {
  const detail = renderDetails(plugin({ trust_tier: 'publisher_verified' }));
  assert.match(detail.textContent, /Publisher verified/);
  assert.doesNotMatch(detail.textContent, /Official/);
});

await test('changed update cannot be installed without exact review digest', async () => {
  const ui = reviewHarness();
  await ui.openUpdate('weather');
  assert.equal(ui.api.updateCalls.length, 0);
  await ui.confirmReview();
  assert.deepEqual(ui.api.updateCalls[0], {
    pluginId: 'weather', revision: ui.revision, version: '2.0.0', confirmation: ui.review.sha256,
  });
});

// --- Additional coverage the brief names ------------------------------------

await test('the install review confirm control submits the exact review digest and current revision', async () => {
  const ui = reviewHarness({ installed: false });
  await ui.openInstall('weather');
  assert.equal(ui.api.installCalls.length, 0, 'no install call before confirming');
  const confirmButton = ui.panel.querySelector('[data-plugin-review-confirm]');
  assert.equal(confirmButton.textContent, 'Install');
  await ui.confirmReview();
  assert.deepEqual(ui.api.installCalls[0], {
    pluginId: 'weather', revision: ui.revision, confirmation: ui.review.sha256,
  });
});

await test('the confirm control does not exist while the review is still loading', async () => {
  const deferredReview = (() => {
    let resolve;
    const promise = new Promise(res => { resolve = res; });
    return { promise, resolve };
  })();
  const ui = reviewHarness({
    installed: false,
    reviewImpl: async () => deferredReview.promise,
  });
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  card.querySelector('[data-plugin-action]').dispatchClick();
  await flush();
  assert.equal(
    ui.panel.querySelector('[data-plugin-review-confirm]'),
    null,
    'the confirm control must not exist before the review has loaded',
  );
  deferredReview.resolve({ ok: true, revision: ui.revision, review: ui.review });
  await flush();
  assert.ok(
    ui.panel.querySelector('[data-plugin-review-confirm]'),
    'the confirm control appears once the review has loaded',
  );
});

await test('cancelling the review clears it and submits nothing', async () => {
  const ui = reviewHarness();
  await ui.openUpdate('weather');
  assert.ok(ui.panel.querySelector('[data-plugin-drawer]'), 'the review drawer is open');
  await ui.cancelReview();
  assert.equal(ui.panel.querySelector('[data-plugin-drawer]'), null, 'the drawer closes');
  assert.equal(ui.api.updateCalls.length, 0);
  assert.equal(ui.api.installCalls.length, 0);
});

await test('an install review revision conflict re-fetches and re-renders instead of retrying blindly', async () => {
  let reviewAttempts = 0;
  const ui = reviewHarness({
    installed: false,
    reviewImpl: async () => {
      reviewAttempts += 1;
      const error = new Error('plugin_revision_conflict');
      error.code = 'plugin_revision_conflict';
      throw error;
    },
  });
  const installedCallsBefore = ui.api.installed;
  const catalogCallsBefore = ui.api.catalog.length;
  await ui.openInstall('weather');
  assert.equal(reviewAttempts, 1);
  assert.equal(ui.panel.querySelector('[data-plugin-drawer]'), null, 'the drawer closes on a revision conflict');
  assert.ok(ui.api.installed > installedCallsBefore, 'the installed list is re-fetched, not retried blindly');
  assert.ok(ui.api.catalog.length > catalogCallsBefore, 'the catalog page is re-fetched, not retried blindly');
  assert.equal(ui.api.installCalls.length, 0, 'no install is ever attempted from a stale review');
});

await test('a confirm-time revision conflict also re-fetches and re-renders', async () => {
  const ui = reviewHarness({
    installed: false,
    installImpl: async () => {
      const error = new Error('plugin_revision_conflict');
      error.code = 'plugin_revision_conflict';
      throw error;
    },
  });
  await ui.openInstall('weather');
  const installedCallsBefore = ui.api.installed;
  await ui.confirmReview();
  assert.equal(ui.panel.querySelector('[data-plugin-drawer]'), null, 'the drawer closes on a revision conflict');
  assert.ok(ui.api.installed > installedCallsBefore, 'the installed list is re-fetched after a confirm-time conflict');
});

await test('changed endpoint renders as a highlighted side-by-side diff row in the update review', async () => {
  const ui = reviewHarness();
  await ui.openUpdate('weather');
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  assert.ok(dialog, 'the update review dialog is open');
  assert.equal(ui.api.updateReviewCalls.length, 1);
  assert.deepEqual(ui.api.updateReviewCalls[0], { pluginId: 'weather', version: '2.0.0', revision: ui.revision });
  const changedRows = dialog.querySelectorAll('.plugins-diff-row.is-changed');
  const endpointRow = changedRows.find(row => /Endpoint/.test(row.textContent));
  assert.ok(endpointRow, 'the changed endpoint (real server permission_changes code) is rendered as a highlighted diff row');
  assert.match(endpointRow.textContent, /old\.weather\.example\.com/);
  assert.match(endpointRow.textContent, /weather\.example\.com/);
  // A category with no matching permission_changes code (this fixture's
  // permission_changes carries only an endpoint change) must render as an
  // unchanged, visually collapsed row -- never marked changed.
  const scopesRow = dialog.querySelectorAll('.plugins-diff-row').find(row => /Provider scopes/.test(row.textContent));
  assert.ok(scopesRow);
  assert.equal(scopesRow.classList.contains('is-changed'), false);
  assert.equal(scopesRow.classList.contains('is-unchanged'), true);
  assert.match(scopesRow.textContent, /No change/);
});

await test('a scope-only change renders exactly one changed row (Provider scopes) and no others', async () => {
  const ui = reviewHarness({
    review: { permission_changes: ['scopes_changed:weather.app'] },
  });
  await ui.openUpdate('weather');
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  const changedLabels = dialog.querySelectorAll('.plugins-diff-row.is-changed')
    .map(row => row.querySelector('.plugins-diff-label').textContent.replace(/\s*\(changed\)$/, ''));
  assert.deepEqual(changedLabels, ['Version', 'Provider scopes'], 'Version always changes; only Provider scopes matches the server code');
  const endpointRow = dialog.querySelectorAll('.plugins-diff-row').find(row => /^Endpoint/.test(row.textContent));
  assert.equal(endpointRow.classList.contains('is-changed'), false);
});

await test('a capability-schema-only change renders exactly Version and Capability schema/effects as changed', async () => {
  const ui = reviewHarness({
    review: { permission_changes: ['capabilities_changed:weather.app'] },
  });
  await ui.openUpdate('weather');
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  const changedLabels = dialog.querySelectorAll('.plugins-diff-row.is-changed')
    .map(row => row.querySelector('.plugins-diff-label').textContent.replace(/\s*\(changed\)$/, ''));
  assert.deepEqual(changedLabels, ['Version', 'Capability schema/effects']);
  const authRow = dialog.querySelectorAll('.plugins-diff-row').find(row => /^Authentication kind/.test(row.textContent));
  assert.equal(authRow.classList.contains('is-changed'), false);
});

await test('a metadata-only update (no permission_changes) marks only Version as changed', async () => {
  const ui = reviewHarness({ review: { permission_changes: [] } });
  await ui.openUpdate('weather');
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  const changedLabels = dialog.querySelectorAll('.plugins-diff-row.is-changed')
    .map(row => row.querySelector('.plugins-diff-label').textContent.replace(/\s*\(changed\)$/, ''));
  assert.deepEqual(changedLabels, ['Version']);
  const unchangedCount = dialog.querySelectorAll('.plugins-diff-row.is-unchanged').length;
  assert.equal(unchangedCount, 10, 'every other named category renders collapsed as unchanged');
});

await test('the update review confirm button submits the exact digest and target version', async () => {
  const ui = reviewHarness();
  await ui.openUpdate('weather');
  const confirmButton = ui.panel.querySelector('[data-plugin-review-confirm]');
  assert.equal(confirmButton.textContent, 'Update');
  await ui.confirmReview();
  assert.equal(ui.api.updateCalls.length, 1);
  assert.equal(ui.api.installCalls.length, 0);
});

await test('opening the review drawer moves focus in; closing restores focus to the trigger', async () => {
  const ui = reviewHarness();
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const actionButton = card.querySelector('[data-plugin-action]');
  actionButton.dispatchClick();
  await flush();
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  assert.ok(dialog, 'a dialog renders');
  assert.equal(dialog.getAttribute('role'), 'dialog');
  assert.equal(dialog.getAttribute('aria-modal'), 'true');
  assert.equal(ui.context.document.activeElement, dialog, 'focus moved into the dialog on open');
  const closeButton = dialog.querySelector('[data-plugin-drawer-close]');
  closeButton.dispatchClick();
  await flush();
  assert.equal(ui.panel.querySelector('[data-plugin-drawer]'), null, 'the drawer closed');
  assert.equal(ui.context.document.activeElement, actionButton, 'focus restored to the trigger on close');
});

await test('Escape closes the drawer and restores focus to the trigger', async () => {
  // Task-4 fix round 1, finding 3.
  const ui = reviewHarness();
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const detailsButton = card.querySelector('[data-plugin-details]');
  detailsButton.dispatchClick();
  await flush();
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  assert.ok(dialog, 'a dialog renders');
  const prevented = dialog.fireKeydown('Escape');
  assert.equal(prevented, true, 'Escape must not also trigger a page-level default action');
  await flush();
  assert.equal(ui.panel.querySelector('[data-plugin-drawer]'), null, 'Escape closed the drawer');
  assert.equal(ui.context.document.activeElement, detailsButton, 'focus restored to the trigger');
});

await test('Escape inside the drawer never reaches the global (document-level) Escape handler', async () => {
  // Smoke-found regression: with the drawer open, Escape closed the drawer
  // AND fell through to register.js's global Escape handler (window
  // keydown, ~lines 166-179), closing the whole Settings overlay too. The
  // drawer's own keydown handler is scoped to the dialog and must stop the
  // event from bubbling past it -- a document-level listener stands in for
  // the global handler here (both are bubble-phase; see Element#fireKeydown
  // and the documentNode wiring above).
  const ui = reviewHarness();
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const detailsButton = card.querySelector('[data-plugin-details]');
  detailsButton.dispatchClick();
  await flush();
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  assert.ok(dialog, 'a dialog renders');

  let globalEscapeCount = 0;
  ui.context.document.addEventListener('keydown', event => {
    if (event.key === 'Escape') globalEscapeCount += 1;
  });

  const prevented = dialog.fireKeydown('Escape');
  await flush();
  assert.equal(prevented, true, 'Escape default is prevented inside the drawer');
  assert.equal(ui.panel.querySelector('[data-plugin-drawer]'), null, 'Escape closed the drawer');
  assert.equal(ui.context.document.activeElement, detailsButton, 'focus restored to the trigger');
  assert.equal(
    globalEscapeCount, 0,
    'the drawer-open Escape must not reach the document-level (global) Escape handler',
  );

  // A second Escape, now that the drawer is closed, must reach it (the
  // global handler is otherwise untouched by this fix).
  ui.panel.fireKeydown('Escape');
  await flush();
  assert.equal(globalEscapeCount, 1, 'a second Escape after the drawer closes reaches the global handler');
});

await test('Escape cancels a loaded review without submitting anything', async () => {
  const ui = reviewHarness();
  await ui.openUpdate('weather');
  assert.ok(ui.panel.querySelector('[data-plugin-drawer]'), 'the review drawer is open with a loaded review');
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  dialog.fireKeydown('Escape');
  await flush();
  assert.equal(ui.panel.querySelector('[data-plugin-drawer]'), null, 'Escape closed the drawer');
  assert.equal(ui.api.updateCalls.length, 0, 'Escape must never submit the loaded review');
  assert.equal(ui.api.installCalls.length, 0);
});

await test('a non-Escape key does not close the drawer', async () => {
  const ui = reviewHarness();
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const detailsButton = card.querySelector('[data-plugin-details]');
  detailsButton.dispatchClick();
  await flush();
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  dialog.fireKeydown('Enter');
  await flush();
  assert.ok(ui.panel.querySelector('[data-plugin-drawer]'), 'a non-Escape key must not close the drawer');
});

await test('the read-only Details view has no confirm control and never mutates anything', async () => {
  const ui = reviewHarness();
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const detailsButton = card.querySelector('[data-plugin-details]');
  assert.ok(detailsButton);
  detailsButton.dispatchClick();
  await flush();
  const dialog = ui.panel.querySelector('[data-plugin-drawer]');
  assert.ok(dialog, 'the details dialog renders');
  assert.equal(dialog.getAttribute('role'), 'dialog');
  assert.equal(dialog.getAttribute('aria-modal'), 'true');
  assert.equal(ui.panel.querySelector('[data-plugin-review-confirm]'), null, 'no confirm control in view mode');
  assert.equal(ui.api.reviewCalls.length, 0, 'viewing details never loads a review or submits anything');
  assert.equal(ui.api.installCalls.length, 0);
  assert.equal(ui.api.updateCalls.length, 0);
});

await test('the "..." inline review (data-plugin-more) sends the known revision, never an undefined plugin.revision', async () => {
  // Regression coverage for task-4 fix round 1 finding 1: loadReview used
  // to send `plugin && plugin.revision`, which is always undefined for
  // entries built by _pluginCatalogPageEntries -- every real "..." click
  // would 422 once /review started requiring `revision`. The existing
  // reviewHarness()-based tests all mock api.review to resolve or reject
  // outright, which cannot catch a wrong *argument* -- this test inspects
  // the actual recorded call.
  const ui = reviewHarness();
  await flush();
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const moreButton = card.querySelector('[data-plugin-more]');
  assert.ok(moreButton);
  moreButton.dispatchClick();
  await flush();
  assert.ok(ui.api.reviewCalls.length >= 1, 'the "..." button must load a review');
  const call = ui.api.reviewCalls[ui.api.reviewCalls.length - 1];
  assert.equal(call.pluginId, 'weather');
  assert.equal(call.revision, ui.revision, 'must send the known server revision, not undefined');
});

await test('the "..." inline review hides and re-fetches on a revision conflict instead of showing a stale review', async () => {
  let reviewAttempts = 0;
  const ui = reviewHarness({
    reviewImpl: async () => {
      reviewAttempts += 1;
      const error = new Error('plugin_revision_conflict');
      error.code = 'plugin_revision_conflict';
      throw error;
    },
  });
  await flush();
  const installedCallsBefore = ui.api.installed;
  const catalogCallsBefore = ui.api.catalog.length;
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  const moreButton = card.querySelector('[data-plugin-more]');
  moreButton.dispatchClick();
  await flush();
  assert.equal(reviewAttempts, 1);
  const detail = card.querySelector('[data-plugin-review-panel]');
  assert.equal(detail.hidden, true, 'the stale/conflicting inline panel must not be shown');
  assert.ok(ui.api.installed > installedCallsBefore, 'the installed list is re-fetched');
  assert.ok(ui.api.catalog.length > catalogCallsBefore, 'the catalog page is re-fetched');
});

await test('a confirmed update that needs a sign-in carries straight into that app\'s own consent form, and only submitting it signs in', async () => {
  // Promoting a re-signed package retires the old connection, so the update
  // answers 202 with the app still to authorize. The panel used to throw that
  // answer away, close the drawer and re-render -- leaving a plugin that needs
  // a sign-in and nothing saying so, until the operator noticed the card had
  // changed to "Continue setup" and clicked it themselves. Under per-app
  // operator consent the carry lands on that exact app's consent form: the
  // operator sees it immediately, nothing connects until it is submitted, and
  // submitting it drives the sign-in through the one setup machine.
  const revision = 'a'.repeat(64);
  const installedPlugins = [{
    plugin_id: 'weather',
    state: 'update_available',
    title: 'Weather',
    summary: 'Live forecasts for any city.',
    trust_tier: 'omega_verified',
    registry_revision: revision,
    package_version: '1.0.0',
    apps: [],
    next_required_app_id: null,
    enabled: true,
    error_code: null,
  }];
  const statusQueue = [
    {
      ok: true, revision, plugin_id: 'weather', state: 'setup_required', setup_action: 'browser',
      next_required_app_id: 'weather.app',
      apps: [{ app_id: 'weather.app', required: true, state: 'setup_required' }],
    },
    {
      ok: true, revision, plugin_id: 'weather', state: 'connected', setup_action: 'connected',
      registry_revision: revision, package_version: '2.0.0', apps: [], next_required_app_id: null,
      enabled: true, error_code: null,
    },
  ];
  const ui = reviewHarness({
    installedPlugins,
    updateImpl: async () => {
      // The server's own next /installed answer after a pending update.
      installedPlugins[0] = {
        ...installedPlugins[0], state: 'setup_required', package_version: '2.0.0',
        next_required_app_id: 'weather.app',
      };
      return {
        ok: true, revision, plugin_id: 'weather', status: 'setup_required',
        app_ids: ['weather.app'], next_required_app_id: 'weather.app',
      };
    },
    // The candidate's own consent offer for exactly the app needing sign-in.
    permissionsImpl: async (pluginId, appId) => ({
      ok: true, revision, plugin_id: pluginId, app_id: appId,
      status: 'candidate_setup_required', candidate: true,
      offered: {
        provider_scopes: ['forecast.read'],
        capability_ids: ['tool:forecast'],
        execution_policies: ['direct_turn', 'confirm_each'],
      },
      selected: {
        provider_scopes: [], capability_ids: [], execution_policy: 'confirm_each',
        source_constraints: {}, destination_constraints: {},
      },
    }),
    // PUT .../permissions starts the consented app's connection itself.
    savePermissionsImpl: async (pluginId, _revision, _settings, appId) => ({
      ok: true, revision, plugin_id: pluginId, app_id: appId, status: 'connecting',
      authorization_url: 'https://weather.example.com/authorize', request_id: 'req-1',
    }),
    statusImpl: async () => {
      const snapshot = statusQueue.shift();
      if (snapshot.state === 'connected') {
        // The server's own next /installed answer once the sign-in finished.
        installedPlugins[0] = { ...installedPlugins[0], state: 'connected', next_required_app_id: null };
      }
      return snapshot;
    },
    // The save already started this app under the submitted selection; a
    // second, separate start would be a start the operator never submitted.
    startConnectionImpl: async () => { throw new Error('connection/start must not be called'); },
  });
  await ui.openUpdate('weather');
  await ui.confirmReview();
  assert.equal(ui.api.updateCalls.length, 1, 'the update itself still went out exactly once');

  // The operator immediately sees the consent form for exactly weather.app.
  await flushUntil(
    () => ui.panel.querySelector('[data-plugin-id="weather"]')?.querySelector('form'),
    'the consent form for the app the update left needing sign-in',
  );
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  assert.deepEqual(
    ui.api.permissionsCalls,
    [{ pluginId: 'weather', appId: 'weather.app' }],
    'the consent form is loaded for the exact app the update named, never an app choice or a guess',
  );
  const detail = card.querySelector('[data-plugin-review-panel]');
  assert.equal(detail.hidden, false, 'the consent form is shown, not left collapsed');
  const form = detail.querySelector('form');
  assert.ok(form, 'the consent form is rendered inside the card');
  assert.ok(
    form.querySelector('[data-plugin-provider-scope="forecast.read"]'),
    'the form offers that app\'s own provider scope',
  );
  assert.ok(
    form.querySelector('[data-plugin-capability="tool:forecast"]'),
    'the form offers that app\'s own capability',
  );
  // Nothing connects without the operator's explicit selection.
  assert.equal(ui.api.savePermissionsCalls.length, 0, 'no selection is saved before the operator submits');
  assert.equal(ui.api.statusCalls.length, 0, 'no setup is driven before the operator submits');
  assert.equal(ui.api.startConnectionCalls.length, 0, 'no connection is started before the operator submits');
  assert.equal(ui.api.popupOpens.length, 0, 'no sign-in window is opened before the operator submits');
  assert.deepEqual(ui.api.navigations, [], 'the operator is not sent to the provider before submitting');
  assert.equal(
    card.querySelector('[data-plugin-status]').className.includes('is-error'),
    false,
    'the carry is not an error',
  );

  // Submitting the form is what drives the sign-in, for that exact app.
  const submitted = form.dispatchSubmit();
  await flushUntil(
    () => ui.api.navigations.length > 0,
    'the setup machine sending the operator to the provider',
  );
  assert.equal(ui.api.savePermissionsCalls.length, 1);
  assert.equal(ui.api.savePermissionsCalls[0].appId, 'weather.app');
  assert.equal(ui.api.popupOpens.length, 1, 'the submit gesture reserves the one sign-in window');
  assert.deepEqual(ui.api.statusCalls, ['weather'], 'the setup action is read from the server after saving');
  assert.equal(ui.api.startConnectionCalls.length, 0, 'the saved selection\'s own start is reused, never a second start');
  assert.deepEqual(ui.api.navigations, [{ kind: 'assign', url: 'https://weather.example.com/authorize' }]);

  // The provider's callback completes the sign-in.
  ui.context.window.dispatchEvent({
    type: 'message',
    origin: 'http://127.0.0.1:13310',
    data: { type: 'omega:plugin-connected', pluginId: 'weather', nextAppId: null },
  });
  await submitted;
  await flush();
  assert.deepEqual(ui.api.statusCalls, ['weather', 'weather'], 'the resolved state comes from the server');
  assert.deepEqual(
    ui.api.navigations[ui.api.navigations.length - 1],
    { kind: 'replace', url: 'http://127.0.0.1:13310/#settings/plugins' },
    'a finished sign-in returns the operator to plugin settings',
  );
  const connectedCard = ui.panel.querySelector('[data-plugin-id="weather"]');
  const connectedStatus = connectedCard.querySelector('[data-plugin-status]');
  assert.equal(connectedStatus.className.includes('is-error'), false, connectedStatus.textContent);
  assert.match(connectedStatus.textContent, /connected/, 'the card re-renders the server\'s connected state');
});

await test('a confirmed update that needs no sign-in opens no consent form and drives no connection setup', async () => {
  // The carry is conditional on the server naming an app: an update that lands
  // connected must not open a setup the operator never needed.
  const ui = reviewHarness({
    updateImpl: async () => ({
      ok: true,
      revision: 'a'.repeat(64),
      plugin_id: 'weather',
      state: 'connected',
    }),
    permissionsImpl: async () => { throw new Error('permissions must not be requested'); },
    statusImpl: async () => { throw new Error('status must not be called'); },
  });
  await ui.openUpdate('weather');
  await ui.confirmReview();
  await flush();
  assert.equal(ui.api.updateCalls.length, 1);
  assert.equal(ui.api.permissionsCalls.length, 0, 'no consent form was opened');
  assert.equal(ui.api.statusCalls.length, 0, 'no sign-in was driven');
  assert.equal(ui.api.popupOpens.length, 0, 'no sign-in window was opened');
  const card = ui.panel.querySelector('[data-plugin-id="weather"]');
  assert.equal(card.querySelector('form'), null);
});

// The exit gate runs after every test above, so each one can fail this
// harness (it previously sat before the update sign-in tests, which could
// therefore never fail it).
if (failures > 0) {
  console.error(`${failures} test(s) failed`);
  process.exit(1);
}
console.log('plugin marketplace review tests passed');

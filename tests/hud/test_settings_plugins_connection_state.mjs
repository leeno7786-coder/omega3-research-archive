import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.resolve(here, '../../hud/ui-src/panels/settings-plugins.js');
const source = fs.readFileSync(sourcePath, 'utf8');
// core/plugin-views.js carries _pluginExactAppId/_pluginExactAppIds and the
// rest of the panel's former private view builders (follow-up refactor
// wave W1) -- _pluginConnectionBody/_pluginPermissionsUrl/
// _pluginCatalogPageEntries below (which stayed in the panel) call the
// former; it must load before settings-plugins.js here, exactly as it does
// in every real build (the chat_feed core inject slot, ahead of panels).
const viewsPath = path.resolve(here, '../../hud/ui-src/core/plugin-views.js');
const viewsSource = fs.readFileSync(viewsPath, 'utf8');
const context = { console };
context.window = context;
context.globalThis = context;
vm.createContext(context);
vm.runInContext(viewsSource, context, { filename: viewsPath });
vm.runInContext(source, context, { filename: sourcePath });

assert.equal(
  typeof context._pluginConnectionAction,
  'function',
  'the plugin panel must expose one status classifier used by the connection flow',
);
assert.equal(context._pluginConnectionAction('installed_disconnected'), 'start');
assert.equal(context._pluginConnectionAction('authorization_required'), 'start');
assert.equal(context._pluginConnectionAction('connected'), 'connected');
assert.equal(context._pluginConnectionAction('authorized'), 'connected');
assert.equal(context._pluginConnectionAction('unexpected-provider-state'), 'error');

// _pluginAuthorizationAction (the "Save and (re)connect" permissions-review
// path's own private authorization-result classifier) was deleted in the
// final fix wave (W2): that path now drives the exact same
// window.OmegaPluginSetup machine Connect/Continue-setup use, whose closed
// setup_action dispatch (browser/device/secret_form/none/connected) is
// exhaustively covered by tests/hud/test_plugin_marketplace_setup.mjs, and
// whose panel-side wiring is proven by
// tests/hud/test_plugin_permissions.mjs's "Save and (re)connect on a
// browser setup_action drives window.OmegaPluginSetup.start" test.

assert.deepEqual(
  context._pluginExactAppIds({
    error_code: 'plugin_app_selection_required',
    app_ids: ['office.mail', 'office.calendar'],
  }),
  ['office.mail', 'office.calendar'],
);
assert.equal(
  JSON.stringify(context._pluginConnectionBody('office.mail', 'a'.repeat(64))),
  '{"app_id":"office.mail","revision":"' + 'a'.repeat(64) + '"}',
);
assert.equal(
  context._pluginPermissionsUrl('office.bundle', 'office.mail'),
  '/api/plugins/office.bundle/permissions?app_id=office.mail',
);
assert.throws(
  () => context._pluginExactAppIds({
    error_code: 'plugin_app_selection_required',
    app_ids: ['office.mail', 'office.mail'],
  }),
  /plugin_connection_state_invalid/,
);
assert.equal(
  typeof context._pluginCatalogPageEntries,
  'function',
  'the panel must project every verified entry returned on a catalog page',
);
const catalogIds = [
  'github.official',
  'google.workspace.drive',
  'google.workspace.gmail',
  'google.workspace.calendar',
  'northwind.docs',
  'contoso.crm',
  'fabrikam.analytics',
];
const projectedPage = context._pluginCatalogPageEntries(
  catalogIds.map(plugin_id => ({plugin_id, title: plugin_id})),
  [{plugin_id: 'northwind.docs', state: 'connected'}],
);
assert.deepEqual(
  Array.from(projectedPage, plugin => plugin.plugin_id),
  catalogIds,
  'catalog page projection must not filter to a built-in provider list',
);
assert.equal(projectedPage[4].installed, true);
assert.equal(projectedPage[4].status, 'connected');

// _pluginCatalogAllEntries (the old eager fetch-every-page-then-merge
// helper, plus its cursor-traversal/cycle-detection/revision-drift-retry
// coverage) was removed from settings-plugins.js -- task 3's rewrite onto
// window.OmegaPluginStore-driven, server-paginated browsing left it with
// no caller in the live render path, and task-4 fix round 1 deleted the
// dead code rather than leaving it in place just because this file still
// called it directly. _pluginCatalogPageEntries (tested just above) is
// still the panel's real per-page projection and remains covered.

// _pluginCallbackMessage/_pluginCanonicalSetupTarget (the standing
// panel-lifetime `message` listener's own origin/type/pluginId/nextAppId
// validators) were deleted in the final fix wave (W2) along with that
// listener itself: every in-flight setup call now registers its own,
// per-call message listener through window.OmegaPluginSetup's
// addMessageListener control, whose equivalent (and stricter -- it also
// rejects a self-referential nextAppId) origin/type/pluginId/appId
// validation is exhaustively covered by
// tests/hud/test_plugin_marketplace_setup.mjs (see plugin-setup.js's
// _pluginSetupAwaitCallback).
// The "Save and (re)connect" submit labels are core/plugin-views.js's
// permissionsEditorMarkup's own literal text now (follow-up refactor wave
// W1, the permissions editor markup moved out of this panel) -- not this
// panel's own source.
assert.match(viewsSource, /Save and connect/);
assert.match(viewsSource, /Save and reconnect/);

const registerPath = path.resolve(here, '../../hud/ui-src/panels/register.js');
const registerSource = fs.readFileSync(registerPath, 'utf8');
const listeners = new Map();
const element = () => ({
  innerHTML: '',
  className: '',
  classList: {add() {}, remove() {}, contains() { return false; }},
  appendChild() {},
  addEventListener() {},
  querySelectorAll() { return []; },
});
const elements = new Map();
const pluginTab = {clicks: 0, click() { this.clicks += 1; }};
const registerDocument = {
  readyState: 'complete',
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  },
  querySelector(selector) {
    if (selector === '.stage') return element();
    if (selector === '[data-settings-view="plugins"]') return pluginTab;
    return null;
  },
  querySelectorAll() { return []; },
  createElement() { return element(); },
};
const registerContext = {
  console,
  document: registerDocument,
  location: {hash: '', pathname: '/'},
  history: {replaceState() { registerContext.location.hash = ''; }},
  setTimeout(callback) { callback(); },
  addEventListener(type, callback) { listeners.set(type, callback); },
  panelSettings() {},
  _PANEL_LIVE_REFRESH: {},
  _escapeHtml: String,
};
registerContext.window = registerContext;
registerContext.globalThis = registerContext;
vm.createContext(registerContext);
vm.runInContext(registerSource, registerContext, {filename: registerPath});
assert.equal(typeof listeners.get('hashchange'), 'function');
registerContext.location.hash = '#settings/plugins';
listeners.get('hashchange')();
assert.equal(pluginTab.clicks, 1, 'a callback hash change must reopen exact plugin settings');

console.log('PASS plugin connection states fail closed and disconnected starts OAuth');

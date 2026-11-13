// Runtime behavior tests for hud/ui-src/core/plugin-setup.js -- the generic
// multi-app authentication and setup driver (task 5). No jsdom dependency
// exists in this repo; this module needs no DOM at all (rendering is the
// caller's job through `controls`), so these tests load the real source
// into a vm context and drive window.OmegaPluginSetup.start() with fixture
// controls -- never a real network call, never a provider name. The
// FakeElement/fakeDom used for the two pure secret-field-form helper cases
// below are the shared, deliberately minimal shapes in
// tests/hud/plugin_dom_shim.mjs (follow-up refactor wave W2).
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';
import { FakeElement, fakeDom } from './plugin_dom_shim.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(here, '../..');
const source = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-setup.js'), 'utf8');
// The panel now calls window.readablePluginError directly (task-6 fix
// round 1 finding 2: one shared error vocabulary, no private cause table)
// -- core/plugin-permissions.js must load before settings-plugins.js here,
// exactly as it does in every real build (the chat_feed core inject slot,
// ahead of panels).
const permissionsSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-permissions.js'), 'utf8');
// core/plugin-views.js carries the panel's former private view builders
// (follow-up refactor wave W1) -- it must also load before
// settings-plugins.js here, exactly as it does in every real build (the
// chat_feed core inject slot, ahead of panels, right after
// plugin-permissions.js).
const viewsSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/core/plugin-views.js'), 'utf8');
const panelSource = fs.readFileSync(path.join(REPO, 'hud/ui-src/panels/settings-plugins.js'), 'utf8');

function loadModule() {
  const context = {console};
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context, {filename: 'plugin-setup.js'});
  return context;
}

function loadPanelHelpers() {
  const context = {console, document: fakeDom()};
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(permissionsSource, context, {filename: 'plugin-permissions.js'});
  vm.runInContext(viewsSource, context, {filename: 'plugin-views.js'});
  vm.runInContext(panelSource, context, {filename: 'settings-plugins.js'});
  return context;
}

const REVISION = 'r'.repeat(64);
// Two further distinct revisions, standing in for the server's lifecycle
// revision changing after each completed app (task-5 fix round 1, finding
// 1) -- used by compositeFixture() to prove the driver carries the fresh
// revision into each subsequent app's startConnection call, not the
// revision the whole composite setup began with.
const REVISION_2 = 's'.repeat(64);
const REVISION_3 = 't'.repeat(64);
const ORIGIN = 'http://127.0.0.1:8090';

// vm.createContext() sandboxed code has its own Array/Object intrinsics, so
// a value returned from inside that context (e.g. controls.onDeviceCode's
// argument, built by an object literal in plugin-setup.js) is not
// deepStrictEqual-comparable to an outer-realm literal even when
// structurally identical. Round-tripping through JSON (executed here, in
// the outer realm) normalizes it -- see test_plugin_marketplace_panel.mjs.
function xrealm(value) { return JSON.parse(JSON.stringify(value)); }

// `revision` here is the top-level lifecycle/catalog revision GET
// .../connection actually returns alongside the snapshot fields
// (`{"ok": true, "revision": ..., **PluginLifecycleSnapshot.to_dict()}` --
// see hud/server.py's api_plugin_connection_status) -- distinct from the
// snapshot's own `registry_revision` field. Defaults to REVISION so a
// single-step fixture needs no override; a composite fixture overrides it
// per step to model the server's revision changing after every completed
// app (task-5 fix round 1, finding 1).
function connectedSnapshot(pluginId, overrides) {
  return Object.assign(
    {
      plugin_id: pluginId,
      state: 'connected',
      revision: REVISION,
      registry_revision: REVISION,
      package_version: '1.0.0',
      apps: [],
      next_required_app_id: null,
      enabled: true,
      error_code: null,
      setup_action: 'connected',
    },
    overrides || {},
  );
}

function setupRequiredSnapshot(pluginId, nextAppId, setupAction, overrides) {
  return Object.assign(
    {
      plugin_id: pluginId,
      state: 'setup_required',
      revision: REVISION,
      registry_revision: REVISION,
      package_version: '1.0.0',
      apps: [],
      next_required_app_id: nextAppId,
      enabled: true,
      error_code: 'plugin_setup_required',
      setup_action: setupAction,
    },
    overrides || {},
  );
}

// ---------------------------------------------------------------------------
// Fixture harness
// ---------------------------------------------------------------------------
// `harness(fixture)` wraps window.OmegaPluginSetup.start() with fixture
// controls, and records everything a test needs to assert on:
//   .start()        -- drives the fixture's own initial snapshot/app id
//   .source         -- the raw plugin-setup.js source (provider-substring checks)
//   .startedAppIds  -- every app_id api.startConnection() was called with, in order
//   .waits          -- every ms argument passed to controls.wait()
//   .navigated      -- every URL passed to controls.navigateSameWindow()
//   .finishCalls    -- every {popup, connected} controls.finish() was called with
//   .statusTexts    -- every controls.onStatus() text
//   .deviceCodes    -- every controls.onDeviceCode() payload
//   .submittedSecrets -- every {appId, values} controls.api.submitSecretFields() received
//   .clearedCount   -- how many times controls.clearSecretFields() was called
//   .snapshots      -- every controls.onSnapshot() snapshot, in order
//   .fireMessage(msg) -- delivers one postMessage-shaped event to every
//                        currently registered controls.addMessageListener() handler
//   .nextConsentRound(snapshot) -- the panel's next per-app consent round
//                        (settings-plugins.js: the operator submits the next
//                        app's own consent form, and driveConnectionSetup
//                        calls start() again from a fresh GET .../connection
//                        snapshot). A fresh controls object, as the panel
//                        builds one per round, whose revision is that
//                        snapshot's own (driveConnectionSetup's
//                        `knownRevision = initial.revision`) and whose popup
//                        is freshly reserved in that round's own gesture; it
//                        records into the same arrays as .start().
function harness(fixture) {
  const ctx = loadModule();
  const startedAppIds = [];
  const startedRevisions = [];
  const polledRevisions = [];
  const waits = [];
  const navigated = [];
  const finishCalls = [];
  const statusTexts = [];
  const deviceCodes = [];
  const submittedSecrets = [];
  const snapshots = [];
  let clearedCount = 0;
  let statusIndex = 0;
  const statusQueue = fixture.statusQueue || [];
  const listeners = new Set();
  let cancelFlag = false;
  let nowValue = typeof fixture.now === 'number' ? fixture.now : 0;

  const controls = {
    pluginId: fixture.pluginId,
    revision: REVISION,
    api: {
      startConnection: async (pluginId, revision, appId) => {
        assert.equal(pluginId, fixture.pluginId);
        assert.equal(typeof revision, 'string');
        startedRevisions.push(revision);
        startedAppIds.push(appId);
        const build = fixture.startResponses[appId];
        if (!build) throw new Error('unexpected app id in test fixture: ' + appId);
        return build();
      },
      pollConnection: async (pluginId, revision, requestId) => {
        assert.equal(pluginId, fixture.pluginId);
        polledRevisions.push(revision);
        assert.equal(typeof requestId, 'string');
        const responses = fixture.pollResponses || [];
        const next = responses[Math.min(statusIndexForPoll(), responses.length - 1)];
        advancePoll();
        return typeof next === 'function' ? next() : next;
      },
      status: async pluginId => {
        assert.equal(pluginId, fixture.pluginId);
        const snapshot = statusQueue[Math.min(statusIndex, statusQueue.length - 1)];
        statusIndex += 1;
        return typeof snapshot === 'function' ? snapshot() : snapshot;
      },
      submitSecretFields: fixture.noSecretRoute ? undefined : async (pluginId, revision, appId, values) => {
        submittedSecrets.push({appId, values: Object.assign({}, values)});
        return fixture.afterSecretSnapshot();
      },
    },
    popup: 'popup' in fixture ? fixture.popup : {closed: false, location: {replace() {}}, focus() {}},
    origin: ORIGIN,
    addMessageListener: handler => {
      listeners.add(handler);
      (fixture.messages || []).forEach((message, index) => {
        setTimeout(() => { if (listeners.has(handler)) handler(message); }, index);
      });
      return () => listeners.delete(handler);
    },
    navigateSameWindow: url => navigated.push(url),
    finish: (popup, connected) => finishCalls.push({popup, connected}),
    onDeviceCode: info => deviceCodes.push(info),
    onStatus: text => statusTexts.push(text),
    isCancelled: () => (typeof fixture.isCancelled === 'function' ? fixture.isCancelled() : cancelFlag),
    wait: ms => { waits.push(ms); return Promise.resolve(); },
    now: () => nowValue,
    collectSecretFields: fixture.collectSecretFields || (async () => fixture.secretValues || {}),
    clearSecretFields: () => { clearedCount += 1; },
    onSnapshot: snapshot => snapshots.push(snapshot),
  };

  let pollIndex = 0;
  function statusIndexForPoll() { return pollIndex; }
  function advancePoll() { pollIndex += 1; }

  return {
    ctx,
    controls,
    start: () => ctx.OmegaPluginSetup.start(fixture.initialSnapshot, fixture.initialAppId, controls),
    nextConsentRound: snapshot => ctx.OmegaPluginSetup.start(
      snapshot,
      snapshot.next_required_app_id,
      Object.assign({}, controls, {
        revision: snapshot.revision,
        popup: {closed: false, location: {replace() {}}, focus() {}},
      }),
    ),
    source,
    startedAppIds,
    startedRevisions,
    polledRevisions,
    waits,
    navigated,
    finishCalls,
    statusTexts,
    deviceCodes,
    submittedSecrets,
    get clearedCount() { return clearedCount; },
    snapshots,
    fireMessage: message => { listeners.forEach(handler => handler(message)); },
    setCancelled: value => { cancelFlag = value; },
    advanceNow: seconds => { nowValue = seconds; },
  };
}

// ---------------------------------------------------------------------------
// Fixtures (brief Step 1)
// ---------------------------------------------------------------------------
function pkceFixture() {
  const pluginId = 'demo.mail';
  return {
    name: 'PKCE',
    pluginId,
    initialAppId: 'mail',
    initialSnapshot: setupRequiredSnapshot(pluginId, 'mail', 'browser'),
    startResponses: {
      mail: () => ({
        plugin_id: pluginId, app_id: 'mail', status: 'connecting',
        authorization_url: 'https://provider.example/authorize',
        request_id: 'req-1', expires_at: null, authorization_flow: 'authorization_code_pkce',
        user_code: null, verification_uri: null, poll_interval_seconds: null,
      }),
    },
    messages: [
      {origin: ORIGIN, data: {type: 'omega:plugin-connected', pluginId, nextAppId: null}},
    ],
    statusQueue: [connectedSnapshot(pluginId)],
  };
}

function deviceFixture() {
  const pluginId = 'demo.calendar';
  return {
    name: 'device',
    pluginId,
    initialAppId: 'calendar',
    initialSnapshot: setupRequiredSnapshot(pluginId, 'calendar', 'device'),
    startResponses: {
      calendar: () => ({
        plugin_id: pluginId, app_id: 'calendar', status: 'authorizing',
        authorization_url: 'https://provider.example/device',
        request_id: 'req-2', expires_at: 1000, authorization_flow: 'device_code',
        user_code: 'ABCD-1234', verification_uri: 'https://provider.example/device',
        poll_interval_seconds: 1,
      }),
    },
    pollResponses: [
      {status: 'authorizing', request_id: 'req-2', retry_after_seconds: null},
      connectedSnapshot(pluginId),
    ],
    statusQueue: [connectedSnapshot(pluginId)],
  };
}

function secretFixture() {
  const pluginId = 'demo.crm';
  const fields = [
    {field_id: 'api_key', label: 'API key', secret: true, required: true},
    {field_id: 'account', label: 'Account name', secret: false, required: false},
  ];
  return {
    name: 'secret',
    pluginId,
    initialAppId: 'crm',
    initialSnapshot: setupRequiredSnapshot(pluginId, 'crm', 'secret_form', {secret_fields: fields}),
    startResponses: {},
    secretValues: {api_key: 'sk-live-demo', account: 'acme'},
    afterSecretSnapshot: () => connectedSnapshot(pluginId),
  };
}

function noAuthFixture() {
  const pluginId = 'demo.status';
  return {
    name: 'no auth',
    pluginId,
    initialAppId: 'status',
    initialSnapshot: setupRequiredSnapshot(pluginId, 'status', 'none'),
    startResponses: {
      status: () => ({plugin_id: pluginId, app_id: 'status', status: 'connected'}),
    },
    statusQueue: [connectedSnapshot(pluginId)],
  };
}

function compositeFixture(appIds) {
  const pluginId = 'demo.office';
  const [first, second] = appIds;
  return {
    name: 'composite',
    pluginId,
    initialAppId: first,
    initialSnapshot: setupRequiredSnapshot(pluginId, first, 'none'),
    startResponses: {
      [first]: () => ({plugin_id: pluginId, app_id: first, status: 'connected'}),
      [second]: () => ({plugin_id: pluginId, app_id: second, status: 'connected'}),
    },
    // Each step's status() response carries a *new* revision -- exactly
    // what the real server does (PluginLifecycleService.start_connection
    // rejects a stale revision with plugin_permission_changed once grants/
    // authority/vault/permissions changed underneath it). This is what
    // proves the driver hands the fresh revision back when it stops after an
    // app, so the next app's own consent round starts with it rather than
    // with the revision the first round began with.
    statusQueue: [
      setupRequiredSnapshot(pluginId, second, 'none', {revision: REVISION_2}),
      connectedSnapshot(pluginId, {revision: REVISION_3}),
    ],
  };
}

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

// ---------------------------------------------------------------------------
// Step 1: the brief's matrix, verbatim in spirit
// ---------------------------------------------------------------------------
for (const fixtureFactory of [pkceFixture, deviceFixture, secretFixture, noAuthFixture]) {
  const fixture = fixtureFactory();
  await test('setup completes ' + fixture.name + ' without provider branching', async () => {
    const setup = harness(fixture);
    const result = await setup.start();
    assert.equal(result.state, 'connected');
    assert.equal(setup.source.toLowerCase().includes('github'), false);
    assert.equal(setup.source.toLowerCase().includes('google'), false);
  });
}

// ---------------------------------------------------------------------------
// Per-app consent across composite setup (2af18bdf): each app owns its own
// provider scopes and capabilities, so once one app connects and the
// snapshot names another next_required_app_id, start() stops and hands that
// snapshot back -- it never starts the next app under the previous app's
// consent. The panel then shows the next app's own consent form, and that
// app is started by a separate start() round.
// ---------------------------------------------------------------------------
await test('composite setup stops after one app and hands the next required app back for its own consent', async () => {
  const setup = harness(compositeFixture(['mail', 'calendar']));
  const first = await setup.start();
  // Only the app the operator consented to was started; 'calendar' has a
  // start response wired, so an auto-advance would have recorded it here.
  assert.deepEqual(setup.startedAppIds, ['mail']);
  assert.equal(first.state, 'setup_required');
  assert.equal(first.next_required_app_id, 'calendar');
  assert.equal(setup.snapshots.length, 1);
  assert.equal(setup.snapshots[0].next_required_app_id, 'calendar');
  // The round ends (the panel closes the unused popup and re-renders) with
  // connected=false -- never reported as a finished connection.
  assert.equal(setup.finishCalls.length, 1);
  assert.equal(setup.finishCalls[0].connected, false);

  // The next app's own consent round completes the composite plugin.
  const second = await setup.nextConsentRound(first);
  assert.deepEqual(setup.startedAppIds, ['mail', 'calendar']);
  assert.equal(second.state, 'connected');
  assert.equal(setup.finishCalls.length, 2);
  assert.equal(setup.finishCalls[1].connected, true);
});

// ---------------------------------------------------------------------------
// Revision drift across composite setup (task-5 fix round 1, finding 1):
// PluginLifecycleService.start_connection re-checks the caller's held
// revision against the server's current one before every app and rejects a
// stale one with plugin_permission_changed, because a completed connection
// changes the composite lifecycle revision (grants/authority/vault/
// permissions all shifted). A composite plugin's second and later required
// app must be started with the *fresh* revision the prior step's own
// snapshot reported, never the revision the whole setup began with -- so the
// snapshot start() hands back when it stops (and reports via onSnapshot,
// which the panel's knownRevision tracks) must carry that fresh revision.
// ---------------------------------------------------------------------------
await test('composite setup hands back the fresh revision, and the next app starts with it, never the stale one the first round began with', async () => {
  const setup = harness(compositeFixture(['mail', 'calendar']));
  const first = await setup.start();
  // 'mail' is started with the revision setup.start() began with.
  assert.deepEqual(setup.startedRevisions, [REVISION]);
  // The returned snapshot and the onSnapshot report both carry REVISION_2
  // -- the revision api.status() reported once 'mail' finished -- never
  // the original REVISION.
  assert.equal(first.revision, REVISION_2);
  assert.equal(setup.snapshots[0].revision, REVISION_2);

  const second = await setup.nextConsentRound(first);
  assert.equal(second.state, 'connected');
  // 'calendar' is started with REVISION_2, never the original REVISION.
  assert.deepEqual(setup.startedRevisions, [REVISION, REVISION_2]);
});

await test('a device-flow app inside a composite chain also hands the next app its fresh revision', async () => {
  // Mirrors the test above but through the device-poll completion path
  // (startDeviceAuthorization's own api.status() call), not just the direct
  // 'none' path -- proves the stop-and-hand-back is in the shared outer
  // loop, not duplicated per-handler by coincidence.
  const pluginId = 'demo.office3';
  const fixture = {
    name: 'composite-device',
    pluginId,
    initialAppId: 'mail',
    initialSnapshot: setupRequiredSnapshot(pluginId, 'mail', 'device'),
    startResponses: {
      mail: () => ({
        plugin_id: pluginId, app_id: 'mail', status: 'authorizing',
        authorization_url: 'https://provider.example/device',
        request_id: 'req-9', expires_at: 1000, authorization_flow: 'device_code',
        user_code: 'DEVC-0001', verification_uri: 'https://provider.example/device',
        poll_interval_seconds: 1,
      }),
      calendar: () => ({plugin_id: pluginId, app_id: 'calendar', status: 'connected'}),
    },
    pollResponses: [setupRequiredSnapshot(pluginId, 'calendar', 'none', {revision: REVISION_2})],
    statusQueue: [
      setupRequiredSnapshot(pluginId, 'calendar', 'none', {revision: REVISION_2}),
      connectedSnapshot(pluginId, {revision: REVISION_3}),
    ],
  };
  const setup = harness(fixture);
  const first = await setup.start();
  // The device app finishes and the driver stops for the next app's own
  // consent -- 'calendar' is never started under 'mail''s consent.
  assert.deepEqual(setup.startedAppIds, ['mail']);
  assert.deepEqual(setup.startedRevisions, [REVISION]);
  assert.equal(first.state, 'setup_required');
  assert.equal(first.next_required_app_id, 'calendar');
  assert.equal(first.revision, REVISION_2);
  assert.equal(setup.snapshots[0].revision, REVISION_2);
  assert.equal(setup.finishCalls.length, 1);
  assert.equal(setup.finishCalls[0].connected, false);

  const second = await setup.nextConsentRound(first);
  assert.equal(second.state, 'connected');
  assert.deepEqual(setup.startedAppIds, ['mail', 'calendar']);
  assert.deepEqual(setup.startedRevisions, [REVISION, REVISION_2]);
});

// ---------------------------------------------------------------------------
// The closed SETUP_ACTIONS set and unknown-action handling
// ---------------------------------------------------------------------------
await test('SETUP_ACTIONS is exactly the closed brief set', () => {
  const ctx = loadModule();
  const keys = Object.keys(ctx.OmegaPluginSetup.SETUP_ACTIONS).sort();
  assert.deepEqual(keys, ['browser', 'connected', 'device', 'none', 'secret_form']);
  assert.equal(Object.isFrozen(ctx.OmegaPluginSetup.SETUP_ACTIONS), true);
  assert.equal(Object.isFrozen(ctx.OmegaPluginSetup), true);
});

await test('an unknown setup action throws plugin_connection_state_invalid', async () => {
  const fixture = noAuthFixture();
  fixture.initialSnapshot = setupRequiredSnapshot(fixture.pluginId, 'status', 'oauth_popup_v2');
  const setup = harness(fixture);
  await assert.rejects(setup.start(), /plugin_connection_state_invalid/);
});

await test('the connected action returns the snapshot as-is with no API call', async () => {
  const fixture = noAuthFixture();
  fixture.initialSnapshot = connectedSnapshot(fixture.pluginId);
  fixture.initialAppId = null;
  const setup = harness(fixture);
  const result = await setup.start();
  assert.equal(result.state, 'connected');
  assert.deepEqual(setup.startedAppIds, []);
});

// ---------------------------------------------------------------------------
// Callback message origin / plugin_id / app_id validation
// ---------------------------------------------------------------------------
await test('a callback message from the wrong origin is ignored', async () => {
  const fixture = pkceFixture();
  fixture.messages = [
    {origin: 'https://evil.example', data: {type: 'omega:plugin-connected', pluginId: fixture.pluginId, nextAppId: null}},
    {origin: ORIGIN, data: {type: 'omega:plugin-connected', pluginId: fixture.pluginId, nextAppId: null}},
  ];
  const setup = harness(fixture);
  const result = await setup.start();
  assert.equal(result.state, 'connected');
});

await test('a callback message for a different plugin_id is ignored', async () => {
  const fixture = pkceFixture();
  fixture.messages = [
    {origin: ORIGIN, data: {type: 'omega:plugin-connected', pluginId: 'someone.else', nextAppId: null}},
    {origin: ORIGIN, data: {type: 'omega:plugin-connected', pluginId: fixture.pluginId, nextAppId: null}},
  ];
  const setup = harness(fixture);
  const result = await setup.start();
  assert.equal(result.state, 'connected');
});

await test('a setup-required callback naming the app just authorized (not a real next app) is ignored', async () => {
  const fixture = pkceFixture();
  fixture.messages = [
    {origin: ORIGIN, data: {type: 'omega:plugin-setup-required', pluginId: fixture.pluginId, nextAppId: 'mail'}},
    {origin: ORIGIN, data: {type: 'omega:plugin-connected', pluginId: fixture.pluginId, nextAppId: null}},
  ];
  const setup = harness(fixture);
  const result = await setup.start();
  assert.equal(result.state, 'connected');
});

await test('a setup-required callback with a malformed nextAppId is ignored', async () => {
  const fixture = pkceFixture();
  fixture.messages = [
    {origin: ORIGIN, data: {type: 'omega:plugin-setup-required', pluginId: fixture.pluginId, nextAppId: '../etc'}},
    {origin: ORIGIN, data: {type: 'omega:plugin-connected', pluginId: fixture.pluginId, nextAppId: null}},
  ];
  const setup = harness(fixture);
  const result = await setup.start();
  assert.equal(result.state, 'connected');
});

await test('a well-formed setup-required callback for a genuine next app resolves the wait and hands that app back for its own consent', async () => {
  const fixture = pkceFixture();
  fixture.pluginId = 'demo.office2';
  fixture.messages = [
    {origin: ORIGIN, data: {type: 'omega:plugin-setup-required', pluginId: fixture.pluginId, nextAppId: 'calendar'}},
  ];
  fixture.startResponses.calendar = () => ({
    plugin_id: fixture.pluginId, app_id: 'calendar', status: 'connected',
  });
  fixture.statusQueue = [
    setupRequiredSnapshot(fixture.pluginId, 'calendar', 'none'),
    connectedSnapshot(fixture.pluginId),
  ];
  const setup = harness(fixture);
  // The callback is accepted (start() settles instead of waiting forever),
  // and the resolved state still comes from api.status(), not the message.
  const first = await setup.start();
  assert.equal(first.state, 'setup_required');
  assert.equal(first.next_required_app_id, 'calendar');
  // 'calendar' is never started under 'mail''s consent.
  assert.deepEqual(setup.startedAppIds, ['mail']);
  assert.equal(setup.finishCalls.length, 1);
  assert.equal(setup.finishCalls[0].connected, false);

  // Its own consent round then connects it.
  const second = await setup.nextConsentRound(first);
  assert.equal(second.state, 'connected');
  assert.deepEqual(setup.startedAppIds, ['mail', 'calendar']);
});

// ---------------------------------------------------------------------------
// Popup-blocked same-window fallback
// ---------------------------------------------------------------------------
await test('a null popup falls back to same-window navigation with the exact authorization URL', async () => {
  const fixture = pkceFixture();
  fixture.popup = null;
  const setup = harness(fixture);
  const resultPromise = setup.start();
  await new Promise(resolve => setTimeout(resolve, 5));
  setup.fireMessage({origin: ORIGIN, data: {type: 'omega:plugin-connected', pluginId: fixture.pluginId, nextAppId: null}});
  await resultPromise;
  assert.deepEqual(setup.navigated, ['https://provider.example/authorize']);
});

await test('an already-closed popup also falls back to same-window navigation', async () => {
  const fixture = pkceFixture();
  fixture.popup = {closed: true, location: {replace() { throw new Error('must not navigate a closed popup'); }}, focus() {}};
  const setup = harness(fixture);
  const resultPromise = setup.start();
  await new Promise(resolve => setTimeout(resolve, 5));
  setup.fireMessage({origin: ORIGIN, data: {type: 'omega:plugin-connected', pluginId: fixture.pluginId, nextAppId: null}});
  await resultPromise;
  assert.deepEqual(setup.navigated, ['https://provider.example/authorize']);
});

await test('a live popup is navigated in place, never the same-window fallback', async () => {
  const fixture = pkceFixture();
  const replaced = [];
  fixture.popup = {closed: false, location: {replace: url => replaced.push(url)}, focus() {}};
  const setup = harness(fixture);
  const resultPromise = setup.start();
  await new Promise(resolve => setTimeout(resolve, 5));
  setup.fireMessage({origin: ORIGIN, data: {type: 'omega:plugin-connected', pluginId: fixture.pluginId, nextAppId: null}});
  await resultPromise;
  assert.deepEqual(replaced, ['https://provider.example/authorize']);
  assert.deepEqual(setup.navigated, []);
});

await test('finish() is called once with the popup and the final connected flag', async () => {
  const fixture = pkceFixture();
  const setup = harness(fixture);
  const result = await setup.start();
  assert.equal(result.state, 'connected');
  assert.equal(setup.finishCalls.length, 1);
  assert.equal(setup.finishCalls[0].connected, true);
});

// ---------------------------------------------------------------------------
// Device: slow-down, expiry, cancellation
// ---------------------------------------------------------------------------
await test('device slow-down widens the poll interval and never narrows it back', async () => {
  const fixture = deviceFixture();
  fixture.pollResponses = [
    {status: 'authorizing', request_id: 'req-2', retry_after_seconds: 30},
    {status: 'authorizing', request_id: 'req-2', retry_after_seconds: 10},
    connectedSnapshot(fixture.pluginId),
  ];
  const setup = harness(fixture);
  await setup.start();
  // Interval starts at started.poll_interval_seconds (1), the first
  // slow-down widens it to 30, and a smaller later retry_after_seconds (10)
  // must never narrow it back below the widest value already observed.
  assert.deepEqual(setup.waits, [1000, 30000, 30000]);
});

await test('device flow throws oauth_device_authorization_expired once expires_at has passed', async () => {
  const fixture = deviceFixture();
  fixture.startResponses.calendar = () => ({
    plugin_id: fixture.pluginId, app_id: 'calendar', status: 'authorizing',
    authorization_url: 'https://provider.example/device',
    request_id: 'req-3', expires_at: 5, authorization_flow: 'device_code',
    user_code: 'WXYZ-9999', verification_uri: 'https://provider.example/device',
    poll_interval_seconds: 1,
  });
  const setup = harness(fixture);
  // now() returns milliseconds (Date.now() semantics); expires_at is in
  // seconds -- 5000ms == 5s, at/past the deadline.
  setup.advanceNow(5000);
  await assert.rejects(setup.start(), /oauth_device_authorization_expired/);
});

await test('device flow can be cancelled mid-poll', async () => {
  const fixture = deviceFixture();
  fixture.pollResponses = [
    {status: 'authorizing', request_id: 'req-2', retry_after_seconds: null},
    connectedSnapshot(fixture.pluginId),
  ];
  const setup = harness(fixture);
  let calls = 0;
  fixture.isCancelled = () => { calls += 1; return calls > 1; };
  await assert.rejects(setup.start(), /plugin_connection_cancelled/);
});

await test('device onDeviceCode receives the exact user code and verification URL', async () => {
  const fixture = deviceFixture();
  const setup = harness(fixture);
  await setup.start();
  assert.deepEqual(xrealm(setup.deviceCodes), [{
    userCode: 'ABCD-1234', verificationUri: 'https://provider.example/device', expiresAt: 1000,
  }]);
});

// ---------------------------------------------------------------------------
// secret_form: required/secret rendering, submit-once, and no retention
// ---------------------------------------------------------------------------
await test('secret_form rejects a missing required field before ever submitting', async () => {
  const fixture = secretFixture();
  fixture.secretValues = {account: 'acme'}; // api_key (required) omitted
  const setup = harness(fixture);
  await assert.rejects(setup.start(), /plugin_setup_secret_field_required/);
  assert.equal(setup.submittedSecrets.length, 0);
});

await test('secret_form submits every declared field exactly once and clears them immediately after', async () => {
  const fixture = secretFixture();
  const setup = harness(fixture);
  const result = await setup.start();
  assert.equal(result.state, 'connected');
  assert.equal(setup.submittedSecrets.length, 1);
  assert.deepEqual(setup.submittedSecrets[0], {
    appId: 'crm',
    values: {api_key: 'sk-live-demo', account: 'acme'},
  });
  assert.equal(setup.clearedCount, 1);
});

await test('secret_form clears collected values from the object passed to it even when submission fails', async () => {
  const fixture = secretFixture();
  const collected = {api_key: 'sk-live-demo', account: 'acme'};
  fixture.collectSecretFields = async () => collected;
  fixture.noSecretRoute = true; // no api.submitSecretFields wired -- ruling 2
  const setup = harness(fixture);
  await assert.rejects(setup.start(), /plugin_setup_credentials_unavailable/);
  assert.deepEqual(collected, {});
  assert.equal(setup.clearedCount, 1);
});

await test('a secret_form step with no declared fields is a visible failure, not a blank form', async () => {
  const fixture = secretFixture();
  fixture.initialSnapshot = setupRequiredSnapshot(fixture.pluginId, 'crm', 'secret_form', {secret_fields: []});
  const setup = harness(fixture);
  await assert.rejects(setup.start(), /plugin_connection_state_invalid/);
});

// ---------------------------------------------------------------------------
// No secret retention anywhere else -- the module never stores a field's
// value on itself; the fixture-owned `values` object handed to
// collectSecretFields is the only place a value ever exists, and it is
// cleared before start() resolves or rejects (proven above). This is an
// additional, direct proof that the *returned* snapshot never carries a
// value back either.
// ---------------------------------------------------------------------------
await test('the resolved snapshot after a secret_form step carries no secret value', async () => {
  const fixture = secretFixture();
  const setup = harness(fixture);
  const result = await setup.start();
  assert.equal(JSON.stringify(result).includes('sk-live-demo'), false);
});

// ---------------------------------------------------------------------------
// The real panel's secret-field controls (hud/ui-src/panels/settings-plugins.js
// _pluginCollectSecretFieldsUi/_pluginClearSecretFieldsUi -- the production
// wiring startConnection() passes as controls.collectSecretFields/
// clearSecretFields) render real label/required/secret controls and leave
// no secret value behind in the DOM once cleared, exercised against the
// real functions and a real (minimal) DOM -- not simulated.
// ---------------------------------------------------------------------------
function buildCard() {
  const card = new FakeElement('article');
  const detail = new FakeElement('div');
  detail.setAttribute('data-plugin-review-panel', '');
  detail.hidden = true;
  card.appendChild(detail);
  return card;
}

await test('the secret-field form renders label, required, and secret (password) controls from the declared fields', () => {
  const ctx = loadPanelHelpers();
  const card = buildCard();
  const fields = [
    {field_id: 'api_key', label: 'API key', secret: true, required: true},
    {field_id: 'account', label: 'Account name', secret: false, required: false},
  ];
  ctx._pluginCollectSecretFieldsUi(card, fields);
  const detail = card.querySelector('[data-plugin-review-panel]');
  assert.equal(detail.hidden, false);
  const inputs = detail.querySelectorAll('[data-plugin-secret-field]');
  assert.equal(inputs.length, 2);
  const apiKeyInput = detail.querySelector('[data-plugin-secret-field="api_key"]');
  assert.equal(apiKeyInput.type, 'password');
  assert.equal(apiKeyInput.required, true);
  const accountInput = detail.querySelector('[data-plugin-secret-field="account"]');
  assert.equal(accountInput.type, 'text');
  assert.equal(accountInput.required, false);
});

await test('submitting the secret-field form resolves with exactly the entered values', async () => {
  const ctx = loadPanelHelpers();
  const card = buildCard();
  const fields = [{field_id: 'api_key', label: 'API key', secret: true, required: true}];
  const promise = ctx._pluginCollectSecretFieldsUi(card, fields);
  const detail = card.querySelector('[data-plugin-review-panel]');
  const input = detail.querySelector('[data-plugin-secret-field="api_key"]');
  input.value = 'sk-live-demo';
  const form = detail.querySelector('[data-plugin-secret-form]');
  form.dispatchSubmit();
  const values = await promise;
  assert.deepEqual(xrealm(values), {api_key: 'sk-live-demo'});
});

await test('clearing the secret-field form removes every input value and the form itself from the DOM', async () => {
  const ctx = loadPanelHelpers();
  const card = buildCard();
  const fields = [{field_id: 'api_key', label: 'API key', secret: true, required: true}];
  const promise = ctx._pluginCollectSecretFieldsUi(card, fields);
  const detail = card.querySelector('[data-plugin-review-panel]');
  const input = detail.querySelector('[data-plugin-secret-field="api_key"]');
  input.value = 'sk-live-demo';
  detail.querySelector('[data-plugin-secret-form]').dispatchSubmit();
  await promise;

  ctx._pluginClearSecretFieldsUi(card);

  assert.equal(detail.querySelector('[data-plugin-secret-form]'), null);
  assert.equal(detail.querySelector('[data-plugin-secret-field]'), null);
  // The <input> node itself (detached from the DOM, but still referenced by
  // this test) must also have had its value cleared before removal.
  assert.equal(input.value, '');
});

await test('clearing before anything was ever rendered is a safe no-op', () => {
  const ctx = loadPanelHelpers();
  const card = buildCard();
  ctx._pluginClearSecretFieldsUi(card); // must not throw
});

await test('device completion consumes the lifecycle snapshot returned by the poll route', async () => {
  const fixture = deviceFixture();
  fixture.pollResponses = [
    connectedSnapshot(fixture.pluginId),
    () => { throw new Error('polled again after the server reported connected'); },
  ];
  const setup = harness(fixture);
  const result = await setup.start();
  assert.equal(result.state, 'connected');
  assert.equal(setup.polledRevisions.length, 1);
});

await test('a failed callback ends the exact pending browser attempt without publishing connected', async () => {
  const fixture = pkceFixture();
  fixture.messages.unshift({origin: ORIGIN, data: {
    type: 'omega:plugin-authorization-failed', requestId: 'req-1', errorCode: 'plugin_auth_expired',
  }});
  const setup = harness(fixture);
  await assert.rejects(setup.start(), /plugin_auth_expired/);
  assert.equal(setup.snapshots.length, 0);
});

await test('a failure for an old browser attempt cannot cancel the current attempt', async () => {
  const fixture = pkceFixture();
  fixture.messages.unshift({origin: ORIGIN, data: {
    type: 'omega:plugin-authorization-failed', requestId: 'req-old', errorCode: 'plugin_auth_expired',
  }});
  const setup = harness(fixture);
  assert.equal((await setup.start()).state, 'connected');
});

await test('device completion hands the spent popup back for closing without reusing it', async () => {
  const fixture = deviceFixture();
  const popup = {closed: false, location: {replace() {}}, focus() {}};
  fixture.popup = popup;
  const setup = harness(fixture);
  await setup.start();
  assert.equal(setup.controls.popup, null);
  assert.equal(setup.finishCalls[0].popup, popup);
});

if (failures > 0) {
  console.error(failures + ' test(s) failed');
  process.exit(1);
}
console.log('plugin marketplace setup tests passed');

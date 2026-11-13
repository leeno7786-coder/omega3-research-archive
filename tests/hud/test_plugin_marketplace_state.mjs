import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.resolve(here, '../../hud/ui-src/core/plugin-store.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function load() {
  const ctx = {
    console,
    JSON, Object, Array, Error, Promise, String, Number, Boolean, Map, Set,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(source, ctx, { filename: sourcePath });
  return ctx.window.OmegaPluginStore;
}

const OmegaPluginStore = load();

function createStore(initial) {
  return OmegaPluginStore.create(initial);
}

function plugin(id, state, revision) {
  return {
    plugin_id: id,
    state: state === undefined ? 'available' : state,
    registry_revision: revision || 'a'.repeat(64),
    package_version: '1.0.0',
    apps: [],
    next_required_app_id: null,
    enabled: true,
    error_code: null,
  };
}

let failures = 0;

function test(name, fn) {
  try {
    fn();
    console.log('PASS ' + name);
  } catch (err) {
    failures += 1;
    console.log('FAIL ' + name);
    console.error(err);
  }
}

// --- Brief Step 1, verbatim -------------------------------------------------

test('stale responses cannot overwrite a newer catalog revision', () => {
  const store = createStore();
  store.dispatch({type: 'catalogLoaded', request: 2, revision: 'b'.repeat(64), items: [plugin('new')]});
  store.dispatch({type: 'catalogLoaded', request: 1, revision: 'a'.repeat(64), items: [plugin('old')]});
  assert.deepEqual(store.snapshot().catalog.items.map(item => item.plugin_id), ['new']);
});

test('unknown lifecycle state is visible instead of connected', () => {
  const store = createStore();
  assert.throws(() => store.dispatch({type: 'pluginChanged', plugin: plugin('x', 'mystery')}), /plugin_state_invalid/);
});

// --- Closed event/state sets --------------------------------------------

test('EVENT_TYPES is exactly the brief\'s closed set', () => {
  const expected = [
    'catalogLoading', 'catalogLoaded', 'catalogFailed', 'installedLoaded',
    'tabChanged', 'queryChanged', 'filtersChanged', 'detailsOpened',
    'detailsClosed', 'mutationStarted', 'pluginChanged', 'mutationFailed',
  ];
  assert.deepEqual([...OmegaPluginStore.EVENT_TYPES].sort(), [...expected].sort());
  assert.equal(OmegaPluginStore.EVENT_TYPES.size, expected.length);
});

test('LIFECYCLE_STATES mirrors mcp_platform/plugin_state.py exactly', () => {
  // Mirrors PluginLifecycleState in mcp_platform/plugin_state.py verbatim.
  const expected = [
    'available', 'review_required', 'installing', 'installed', 'setup_required',
    'connecting', 'connected', 'offline', 'update_available', 'update_required',
    'permission_changed', 'revocation_pending', 'blocked', 'incompatible',
  ];
  assert.deepEqual([...OmegaPluginStore.LIFECYCLE_STATES].sort(), [...expected].sort());
  assert.equal(OmegaPluginStore.LIFECYCLE_STATES.length, expected.length);
});

test('dispatching an event type outside the closed set throws', () => {
  const store = createStore();
  assert.throws(() => store.dispatch({type: 'somethingElse'}), /plugin_store_event_unknown/);
});

test('installedLoaded also rejects an unknown lifecycle state', () => {
  const store = createStore();
  assert.throws(
    () => store.dispatch({type: 'installedLoaded', items: [plugin('y', 'not_a_real_state')]}),
    /plugin_state_invalid/,
  );
});

// --- Frozen, immutable snapshots ----------------------------------------

test('snapshot() is frozen and never mutated by a later dispatch', () => {
  const store = createStore();
  const before = store.snapshot();
  assert.equal(Object.isFrozen(before), true);
  assert.equal(Object.isFrozen(before.catalog), true);
  assert.equal(Object.isFrozen(before.catalog.items), true);
  assert.equal(Object.isFrozen(before.installed), true);
  store.dispatch({type: 'catalogLoaded', request: 1, revision: 'a'.repeat(64), items: [plugin('one')]});
  assert.deepEqual(before.catalog.items, []);
  const after = store.snapshot();
  assert.notEqual(before, after);
  assert.deepEqual(after.catalog.items.map(item => item.plugin_id), ['one']);
});

test('subscribe delivers each new snapshot and unsubscribe stops delivery', () => {
  const store = createStore();
  const seen = [];
  const unsubscribe = store.subscribe(snapshot => seen.push(snapshot));
  store.dispatch({type: 'tabChanged', tab: 'personal'});
  assert.equal(seen.length, 1);
  assert.equal(seen[0].tab, 'personal');
  unsubscribe();
  store.dispatch({type: 'tabChanged', tab: 'public'});
  assert.equal(seen.length, 1);
});

test('a no-op dispatch (stale catalog response) does not notify subscribers', () => {
  const store = createStore();
  store.dispatch({type: 'catalogLoaded', request: 2, revision: 'a'.repeat(64), items: []});
  let calls = 0;
  store.subscribe(() => { calls += 1; });
  store.dispatch({type: 'catalogLoaded', request: 1, revision: 'z'.repeat(64), items: [plugin('stale')]});
  assert.equal(calls, 0);
});

// --- Public/Personal tab isolation ---------------------------------------

test('Public and Personal catalogs are isolated: loading one never touches the other', () => {
  const store = createStore();
  store.dispatch({type: 'catalogLoaded', tab: 'public', request: 1, revision: 'a'.repeat(64), items: [plugin('pub-1')]});
  store.dispatch({type: 'catalogLoaded', tab: 'personal', request: 1, revision: 'a'.repeat(64), items: [plugin('per-1')]});
  const snap = store.snapshot();
  assert.deepEqual(snap.catalogs.public.items.map(item => item.plugin_id), ['pub-1']);
  assert.deepEqual(snap.catalogs.personal.items.map(item => item.plugin_id), ['per-1']);
  // The active tab's `catalog` alias follows the active tab, not either bucket.
  assert.deepEqual(snap.catalog.items.map(item => item.plugin_id), ['pub-1']);
  store.dispatch({type: 'tabChanged', tab: 'personal'});
  assert.deepEqual(store.snapshot().catalog.items.map(item => item.plugin_id), ['per-1']);
});

test('each tab tracks its own monotonic request id independently', () => {
  const store = createStore();
  store.dispatch({type: 'catalogLoaded', tab: 'public', request: 5, revision: 'a'.repeat(64), items: [plugin('pub-new')]});
  // Personal's own sequence starts independently at a lower number than
  // Public's -- this must not be rejected as "stale" against Public's id.
  store.dispatch({type: 'catalogLoaded', tab: 'personal', request: 1, revision: 'a'.repeat(64), items: [plugin('per-new')]});
  const snap = store.snapshot();
  assert.deepEqual(snap.catalogs.personal.items.map(item => item.plugin_id), ['per-new']);
  assert.deepEqual(snap.catalogs.public.items.map(item => item.plugin_id), ['pub-new']);
});

// --- Query reset -----------------------------------------------------------

test('changing the query resets the active tab catalog', () => {
  const store = createStore();
  store.dispatch({
    type: 'catalogLoaded', request: 1, revision: 'a'.repeat(64),
    items: [plugin('one')], cursor: 'tok',
  });
  assert.equal(store.snapshot().catalog.items.length, 1);
  store.dispatch({type: 'queryChanged', query: 'notes'});
  const snap = store.snapshot();
  assert.deepEqual(snap.catalog.items, []);
  assert.equal(snap.catalog.cursor, null);
  assert.equal(snap.query, 'notes');
});

test('changing the query only resets the active tab, not the other tab', () => {
  const store = createStore();
  store.dispatch({type: 'catalogLoaded', tab: 'personal', request: 1, revision: 'a'.repeat(64), items: [plugin('per-1')]});
  store.dispatch({type: 'catalogLoaded', tab: 'public', request: 1, revision: 'a'.repeat(64), items: [plugin('pub-1')]});
  store.dispatch({type: 'queryChanged', query: 'inbox'});
  const snap = store.snapshot();
  assert.deepEqual(snap.catalogs.public.items, []);
  assert.deepEqual(snap.catalogs.personal.items.map(item => item.plugin_id), ['per-1']);
});

// --- Cursor append de-duplication ------------------------------------------

test('cursor-appended catalog pages de-duplicate by plugin_id', () => {
  const store = createStore();
  store.dispatch({
    type: 'catalogLoaded', request: 1, revision: 'a'.repeat(64),
    items: [plugin('a'), plugin('b')], cursor: 'page-2',
  });
  store.dispatch({
    type: 'catalogLoaded', request: 2, revision: 'a'.repeat(64),
    items: [plugin('b'), plugin('c')], cursor: null, append: true,
  });
  const snap = store.snapshot();
  assert.deepEqual(snap.catalog.items.map(item => item.plugin_id), ['a', 'b', 'c']);
  assert.equal(snap.catalog.cursor, null);
});

// --- Revision update ---------------------------------------------------

test('pluginChanged replaces a plugin\'s stored revision on each update', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', plugin: plugin('p1', 'installed', 'a'.repeat(64))});
  assert.equal(store.snapshot().installed.p1.registry_revision, 'a'.repeat(64));
  store.dispatch({type: 'pluginChanged', plugin: plugin('p1', 'installed', 'b'.repeat(64))});
  assert.equal(store.snapshot().installed.p1.registry_revision, 'b'.repeat(64));
});

// --- Per-plugin mutation locking --------------------------------------

test('mutation locking is exclusive per plugin and independent across plugins', () => {
  const store = createStore();
  store.dispatch({type: 'mutationStarted', pluginId: 'p1', mutationId: 'm1'});
  // A second mutation for the SAME plugin while one is pending is a no-op:
  // the original lock holder (m1) is kept.
  store.dispatch({type: 'mutationStarted', pluginId: 'p1', mutationId: 'm2'});
  store.dispatch({type: 'mutationStarted', pluginId: 'p2', mutationId: 'm3'});
  let snap = store.snapshot();
  assert.equal(snap.pendingMutations.p1, 'm1');
  assert.equal(snap.pendingMutations.p2, 'm3');

  store.dispatch({type: 'pluginChanged', plugin: plugin('p1', 'installed')});
  snap = store.snapshot();
  assert.equal(snap.pendingMutations.p1, undefined);
  assert.equal(snap.pendingMutations.p2, 'm3');

  store.dispatch({type: 'mutationStarted', pluginId: 'p1', mutationId: 'm4'});
  assert.equal(store.snapshot().pendingMutations.p1, 'm4');
});

test('mutationFailed clears the lock and records the error for that plugin only', () => {
  const store = createStore();
  store.dispatch({type: 'mutationStarted', pluginId: 'p1', mutationId: 'm1'});
  store.dispatch({type: 'mutationStarted', pluginId: 'p2', mutationId: 'm2'});
  store.dispatch({type: 'mutationFailed', pluginId: 'p1', error: 'plugin_install_confirmation_stale'});
  const snap = store.snapshot();
  assert.equal(snap.pendingMutations.p1, undefined);
  assert.equal(snap.mutationErrors.p1, 'plugin_install_confirmation_stale');
  assert.equal(snap.pendingMutations.p2, 'm2');
  assert.equal(snap.mutationErrors.p2, undefined);
});

// --- permission_changed precedence over connected -----------------------

test('permission_changed overrides a previously connected state for the same plugin', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', plugin: plugin('p1', 'connected')});
  assert.equal(store.snapshot().installed.p1.state, 'connected');
  store.dispatch({type: 'pluginChanged', plugin: plugin('p1', 'permission_changed')});
  assert.equal(store.snapshot().installed.p1.state, 'permission_changed');
});

test('permission_changed is not stuck: a later confirmed connected still applies', () => {
  // The reducer trusts each fully-resolved pluginChanged snapshot as-is
  // (it never invents a "sticky" precedence of its own) -- so a plugin
  // that drifted into permission_changed can move back to connected once
  // the server confirms a fresh, re-reviewed connection.
  const store = createStore();
  store.dispatch({type: 'pluginChanged', plugin: plugin('p1', 'permission_changed')});
  store.dispatch({type: 'pluginChanged', plugin: plugin('p1', 'connected')});
  assert.equal(store.snapshot().installed.p1.state, 'connected');
});

// --- pluginChanged ordering guard (task-6 controller ruling 1, fix round 1) -
// A pushed or polled event can arrive out of network order. Ordering is
// never derived from registry_revision -- it is a content digest of the
// whole registry document and legitimately repeats on a byte-identical
// registry (a connect -> disconnect -> reconnect cycle toggling only
// `enabled` can reproduce an earlier digest exactly). Instead, exactly
// like the catalog's own `request` id above, the caller assigns a
// per-plugin monotonic `request` at dispatch time, and an event whose
// `request` is older than the latest one already accepted for that plugin
// is dropped as a whole no-op -- an identical revision arriving with an
// in-order `request` is accepted, never mistaken for a stale replay.

test('an in-order recurrence of the same revision content is accepted, never treated as stale', () => {
  // connect(R1) -> save(R2) -> save returning R1's content again, in order.
  // A content-keyed guard would wrongly reject the third dispatch (it has
  // already "seen" R1); the request-id guard must accept it, since request
  // 3 is newer than the last accepted request (2).
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'connecting', 'a'.repeat(64))});
  store.dispatch({type: 'pluginChanged', request: 2, plugin: plugin('p1', 'permission_changed', 'b'.repeat(64))});
  store.dispatch({type: 'pluginChanged', request: 3, plugin: plugin('p1', 'connected', 'a'.repeat(64))});
  assert.equal(store.snapshot().installed.p1.state, 'connected');
  assert.equal(store.snapshot().installed.p1.registry_revision, 'a'.repeat(64));
});

test('a late response for an older request id cannot regress a newer permission_changed', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'connecting', 'a'.repeat(64))});
  store.dispatch({type: 'pluginChanged', request: 2, plugin: plugin('p1', 'permission_changed', 'b'.repeat(64))});
  // A delayed response for the earlier request (1) finally arrives -- it
  // must not regress the newer permission_changed the store already holds,
  // regardless of what revision/state it carries.
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'connected', 'c'.repeat(64))});
  assert.equal(store.snapshot().installed.p1.state, 'permission_changed');
  assert.equal(store.snapshot().installed.p1.registry_revision, 'b'.repeat(64));
});

test('a late response for an older request id cannot regress a newer revocation_pending', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'connecting', 'a'.repeat(64))});
  store.dispatch({type: 'pluginChanged', request: 2, plugin: plugin('p1', 'revocation_pending', 'b'.repeat(64))});
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'connected', 'c'.repeat(64))});
  assert.equal(store.snapshot().installed.p1.state, 'revocation_pending');
  assert.equal(store.snapshot().installed.p1.registry_revision, 'b'.repeat(64));
});

test('a stale-request pluginChanged is a genuine no-op: no subscriber notification', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'connecting', 'a'.repeat(64))});
  store.dispatch({type: 'pluginChanged', request: 2, plugin: plugin('p1', 'permission_changed', 'b'.repeat(64))});
  let notifications = 0;
  store.subscribe(() => { notifications += 1; });
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'connected', 'c'.repeat(64))});
  assert.equal(notifications, 0);
});

test('event.force bypasses the ordering guard for an explicit resync, without regressing the tracked watermark', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'connecting', 'a'.repeat(64))});
  store.dispatch({type: 'pluginChanged', request: 5, plugin: plugin('p1', 'permission_changed', 'b'.repeat(64))});
  // Forced: an older request id is still accepted this once...
  store.dispatch({type: 'pluginChanged', request: 2, plugin: plugin('p1', 'connected', 'c'.repeat(64)), force: true});
  assert.equal(store.snapshot().installed.p1.state, 'connected');
  // ...but the watermark stays at the highest request ever seen (5), so a
  // later normal (non-forced) event still can't regress behind request 5.
  store.dispatch({type: 'pluginChanged', request: 3, plugin: plugin('p1', 'setup_required', 'd'.repeat(64))});
  assert.equal(store.snapshot().installed.p1.state, 'connected');
});

test('an event with no request field is never ordering-guarded (backward compatible)', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 5, plugin: plugin('p1', 'permission_changed', 'b'.repeat(64))});
  // No `request` field at all -- every pre-existing caller and test uses
  // this shape; it must always apply, last-write-wins, exactly as before
  // this task's ordering guard existed.
  store.dispatch({type: 'pluginChanged', plugin: plugin('p1', 'connected', 'a'.repeat(64))});
  assert.equal(store.snapshot().installed.p1.state, 'connected');
});

test('the ordering guard is independent per plugin', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'connecting', 'a'.repeat(64))});
  store.dispatch({type: 'pluginChanged', request: 2, plugin: plugin('p1', 'permission_changed', 'b'.repeat(64))});
  // p2 has never been dispatched before -- request 1 is brand new for it
  // and must be accepted, even though request 1 is already stale for p1.
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p2', 'connected', 'a'.repeat(64))});
  assert.equal(store.snapshot().installed.p2.state, 'connected');
});

// --- installedRequest watermark pruning (final fix wave W3) ---------------
// installedRequest is never projected into snapshot() (it is pure reducer
// bookkeeping), so these tests prove pruning indirectly: an un-pruned,
// stale-but-still-tracked watermark would reject a lower `request` a
// plugin's next installedLoaded/pluginChanged legitimately arrives with
// (e.g. after a fresh uninstall+reinstall's own new request sequence);
// once pruned, that same lower request must be accepted.

test('installedLoaded prunes the watermark for a plugin no longer present', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 5, plugin: plugin('p1', 'connected')});
  // A fresh /installed response that no longer names p1 at all (uninstalled
  // elsewhere, or simply never installed in this snapshot).
  store.dispatch({type: 'installedLoaded', items: [plugin('other', 'connected')]});
  assert.equal(store.snapshot().installed.p1, undefined);
  // p1 is reinstalled and reconnected with a fresh, independent request
  // sequence starting back at 1 -- an un-pruned watermark (still at 5)
  // would wrongly reject this as stale.
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'installed')});
  assert.equal(store.snapshot().installed.p1.state, 'installed');
});

test('installedLoaded keeps the watermark for a plugin still present', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 5, plugin: plugin('p1', 'connected')});
  store.dispatch({type: 'installedLoaded', items: [plugin('p1', 'connected')]});
  // p1 is still present -- its watermark (5) must still guard against a
  // late, older-request straggler.
  store.dispatch({type: 'pluginChanged', request: 2, plugin: plugin('p1', 'offline')});
  assert.equal(store.snapshot().installed.p1.state, 'connected');
});

test('a pluginChanged reporting available (post-uninstall) drops that plugin\'s own watermark', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 5, plugin: plugin('p1', 'connected')});
  store.dispatch({type: 'pluginChanged', request: 6, plugin: plugin('p1', 'available')});
  assert.equal(store.snapshot().installed.p1.state, 'available');
  // p1 is reinstalled with a fresh, independent request sequence -- an
  // un-pruned watermark (still at 6) would wrongly reject this as stale.
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'installed')});
  assert.equal(store.snapshot().installed.p1.state, 'installed');
});

test('watermark pruning is independent per plugin', () => {
  const store = createStore();
  store.dispatch({type: 'pluginChanged', request: 5, plugin: plugin('p1', 'connected')});
  store.dispatch({type: 'pluginChanged', request: 5, plugin: plugin('p2', 'connected')});
  store.dispatch({type: 'pluginChanged', request: 6, plugin: plugin('p1', 'available')});
  // p1's watermark is pruned; p2's is untouched and must still reject a
  // stale, older-request straggler.
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p2', 'offline')});
  assert.equal(store.snapshot().installed.p2.state, 'connected');
  store.dispatch({type: 'pluginChanged', request: 1, plugin: plugin('p1', 'installed')});
  assert.equal(store.snapshot().installed.p1.state, 'installed');
});

// --- Details open/close ----------------------------------------------------

test('detailsOpened/detailsClosed track the selected plugin id', () => {
  const store = createStore();
  store.dispatch({type: 'detailsOpened', pluginId: 'p1'});
  assert.equal(store.snapshot().detailsId, 'p1');
  store.dispatch({type: 'detailsClosed'});
  assert.equal(store.snapshot().detailsId, null);
});

// --- catalogFailed preserves last-good items --------------------------

test('catalogFailed records the error without discarding already-loaded items', () => {
  const store = createStore();
  store.dispatch({type: 'catalogLoaded', request: 1, revision: 'a'.repeat(64), items: [plugin('one')]});
  store.dispatch({type: 'catalogFailed', request: 2, error: 'plugin_catalog_signature_invalid'});
  const snap = store.snapshot();
  assert.deepEqual(snap.catalog.items.map(item => item.plugin_id), ['one']);
  assert.equal(snap.catalog.error, 'plugin_catalog_signature_invalid');
  assert.equal(snap.catalog.loading, false);
});

if (failures > 0) {
  console.error(`${failures} test(s) failed`);
  process.exit(1);
}
console.log('plugin marketplace state tests passed');

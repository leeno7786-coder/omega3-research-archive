import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const sourcePath = path.resolve(here, '../../hud/ui-src/core/plugin-api.js');
const source = fs.readFileSync(sourcePath, 'utf8');

function load(fetchImpl) {
  const calls = [];
  const ctx = {
    console,
    fetch: async (url, options) => {
      calls.push({ url, options });
      return fetchImpl(url, options);
    },
    encodeURIComponent,
    JSON, Object, Array, Error, Promise, String, Number, Boolean, Map, Set,
  };
  ctx.window = ctx;
  ctx.globalThis = ctx;
  vm.createContext(ctx);
  vm.runInContext(source, ctx, { filename: sourcePath });
  return { ctx, calls, api: ctx.window.OmegaPluginApi };
}

function bodyOf(call) {
  return JSON.parse(call.options.body);
}

const REVISION = 'a'.repeat(64);

{
  const { api } = load(async () => ({ json: async () => ({ ok: true }) }));
  assert.equal(Object.isFrozen(api), true, 'OmegaPluginApi must be frozen');
  for (const name of [
    'catalog', 'details', 'installed', 'sync', 'review', 'install', 'status',
    'startConnection', 'pollConnection', 'permissions', 'savePermissions',
    'tryInChat', 'disconnect', 'update', 'uninstall', 'updateReview',
  ]) {
    assert.equal(typeof api[name], 'function', `missing method ${name}`);
  }
  console.log('PASS OmegaPluginApi is frozen and exposes the mandated method set');
}

{
  const { api, calls } = load(async (url) => {
    assert.equal(url, '/api/plugins/installed');
    return { json: async () => ({ ok: true, revision: REVISION, plugins: [] }) };
  });
  const result = await api.installed();
  assert.deepEqual(result, { ok: true, revision: REVISION, plugins: [] });
  assert.equal(calls[0].options.cache, 'no-store');
  console.log('PASS requestJson returns the full parsed body on ok:true');
}

{
  const { api } = load(async () => ({
    json: async () => ({ ok: false, error: 'plugin_not_installed' }),
  }));
  await assert.rejects(api.installed(), (err) => {
    assert.equal(err.code, 'plugin_not_installed');
    return true;
  });
  console.log('PASS ok:false preserves the stable server error in error.code');
}

{
  for (const bad of [null, 42, 'x', ['a'], true]) {
    const { api } = load(async () => ({ json: async () => bad }));
    await assert.rejects(
      api.installed(),
      (err) => typeof err.code === 'string' && err.code.length > 0,
      `non-object body ${JSON.stringify(bad)} must be rejected`,
    );
  }
  console.log('PASS non-object response bodies are rejected outright, never treated as {}');
}

{
  const { api } = load(async () => ({
    json: async () => {
      throw new SyntaxError('Unexpected token in JSON');
    },
  }));
  await assert.rejects(api.installed(), (err) => {
    assert.equal(err.code, 'plugin_api_response_invalid');
    return true;
  });
  console.log('PASS a non-JSON response body throws instead of silently becoming {}');
}

{
  const { api } = load(async () => {
    throw new TypeError('Failed to fetch');
  });
  await assert.rejects(api.installed(), (err) => {
    assert.equal(err.code, 'plugin_api_network_unavailable');
    return true;
  });
  console.log('PASS a network failure (fetch rejection) propagates as a thrown error');
}

{
  const { api } = load(async () => ({ json: async () => ({ plugins: [] }) }));
  await assert.rejects(api.installed(), (err) => {
    assert.equal(err.code, 'plugin_api_response_invalid');
    return true;
  });
  console.log('PASS a response missing a boolean ok field is rejected');
}

{
  // hud/server.py's /connection/start route (allowed={"app_id","revision"},
  // required={"revision"}) and PUT /permissions (allowed={"revision","settings","app_id"})
  // are the two mutating routes in this task's set that actually accept revision.
  const { api, calls } = load(async () => ({ json: async () => ({ ok: true, revision: REVISION }) }));
  await api.startConnection('office.bundle', REVISION, 'office.mail');
  assert.deepEqual(bodyOf(calls[0]), { revision: REVISION, app_id: 'office.mail' });
  assert.equal(calls[0].options.method, 'POST');
  calls.length = 0;

  await api.startConnection('office.bundle', REVISION);
  assert.deepEqual(bodyOf(calls[0]), { revision: REVISION });
  calls.length = 0;

  await api.savePermissions('office.bundle', REVISION, { execution_policy: 'confirm_each' }, 'office.mail');
  assert.deepEqual(bodyOf(calls[0]), {
    revision: REVISION,
    settings: { execution_policy: 'confirm_each' },
    app_id: 'office.mail',
  });
  assert.equal(calls[0].options.method, 'PUT');
  calls.length = 0;

  await api.savePermissions('office.bundle', REVISION, { execution_policy: 'confirm_each' });
  assert.deepEqual(bodyOf(calls[0]), { revision: REVISION, settings: { execution_policy: 'confirm_each' } });

  console.log('PASS startConnection and savePermissions send the exact revision-bearing keys');
}

{
  // review, install, update, disconnect, and uninstall are the mutations
  // task-4/task-6 controller ruling 2 requires to accept and check the
  // client's held revision; the panel now sends it and a stale/missing one
  // is a stable server-side plugin_revision_conflict/
  // invalid_plugin_lifecycle_request cause (see hud/server.py's
  // _strict_plugin_install_json/_strict_plugin_update_json and the
  // /review, /disconnect, /uninstall routes). connection/poll and
  // tryInChat are still guarded by a strict parser that admits no
  // "revision" key at all -- sending it there would 422 every one of these
  // calls (verified against hud/server.py; see task-1-report.md).
  const { api, calls } = load(async () => ({ json: async () => ({ ok: true, revision: REVISION }) }));

  await api.review('office.bundle', REVISION);
  assert.deepEqual(bodyOf(calls[0]), { revision: REVISION });
  calls.length = 0;

  await api.install('office.bundle', REVISION, 'b'.repeat(64));
  assert.deepEqual(bodyOf(calls[0]), { confirmation: 'b'.repeat(64), revision: REVISION });
  calls.length = 0;

  await api.pollConnection('office.bundle', REVISION, 'c'.repeat(64));
  assert.deepEqual(bodyOf(calls[0]), { request_id: 'c'.repeat(64) });
  calls.length = 0;

  await api.disconnect('office.bundle', REVISION);
  assert.deepEqual(bodyOf(calls[0]), { revision: REVISION });
  calls.length = 0;

  await api.uninstall('office.bundle', REVISION);
  assert.deepEqual(bodyOf(calls[0]), { revision: REVISION });
  calls.length = 0;

  await api.update('office.bundle', REVISION, '1.2.3', 'd'.repeat(64));
  assert.deepEqual(bodyOf(calls[0]), { version: '1.2.3', confirmation: 'd'.repeat(64), revision: REVISION });
  calls.length = 0;

  await api.tryInChat('office.bundle');
  assert.deepEqual(bodyOf(calls[0]), {});

  console.log(
    "PASS review/install/update/disconnect/uninstall send the client's " +
    "held revision; poll/tryInChat bodies still match hud/server.py's " +
    "strict parsers exactly (no stray revision key)",
  );
}

{
  const { api, calls } = load(async () => ({
    json: async () => ({ ok: true, revision: REVISION, plugins: [], next_cursor: null, total_plugins: 0 }),
  }));
  await api.catalog({ q: 'a b', category: 'productivity', cursor: 'tok en' });
  assert.equal(calls[0].url, '/api/plugins/catalog?q=a%20b&category=productivity&cursor=tok%20en');
  calls.length = 0;

  await api.catalog();
  assert.equal(calls[0].url, '/api/plugins/catalog?');
  calls.length = 0;

  await api.details('office bundle');
  assert.equal(calls[0].url, '/api/plugins/catalog/office%20bundle');
  calls.length = 0;

  await api.permissions('office bundle');
  assert.equal(calls[0].url, '/api/plugins/office%20bundle/permissions');
  calls.length = 0;

  await api.permissions('office bundle', 'app one');
  assert.equal(calls[0].url, '/api/plugins/office%20bundle/permissions?app_id=app%20one');
  calls.length = 0;

  await api.review('a/b', REVISION);
  assert.equal(calls[0].url, '/api/plugins/a%2Fb/review');
  calls.length = 0;

  await api.updateReview('office bundle', '2.0.0', REVISION);
  assert.equal(
    calls[0].url,
    '/api/plugins/office%20bundle/update-review?version=2.0.0&revision=' + REVISION,
  );
  assert.equal(calls[0].options.method, undefined, 'updateReview is a plain GET, no body');
  assert.equal(calls[0].options.body, undefined);

  console.log('PASS encodeQuery and pluginPath percent-encode every component');
}

{
  // No GET /api/plugins/{plugin_id}/connection route exists server-side yet
  // (see task-1-report.md "Server routes to add"). A default framework 404
  // body ({"detail": "Not Found"}) has no boolean `ok`, so requestJson's
  // strict contract check fails closed rather than fabricating success.
  const { api } = load(async (url) => {
    assert.equal(url, '/api/plugins/office.bundle/connection');
    return { json: async () => ({ detail: 'Not Found' }) };
  });
  await assert.rejects(api.status('office.bundle'), (err) => {
    assert.equal(err.code, 'plugin_api_response_invalid');
    return true;
  });
  console.log('PASS status() fails closed against the not-yet-added GET .../connection route');
}

console.log('plugin API client tests passed');

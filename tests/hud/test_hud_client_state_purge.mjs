// The HUD keeps appearance, voice and drawer state in the operator's browser, which is
// not on the SSD — so a factory reset on disk cannot reach it. The settings response is
// the only signal the client gets: stored:false means "never configured", and every
// cached omega.hud.* key must go, including the side drawer's, which has no server copy.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const source = fs.readFileSync(
  path.resolve(here, '../../hud/ui-src/panels/settings-theme.js'), 'utf8');

function makeStorage(seed) {
  const map = new Map(Object.entries(seed));
  return {
    get length() { return map.size; },
    key(i) { return Array.from(map.keys())[i] ?? null; },
    getItem(k) { return map.has(k) ? map.get(k) : null; },
    setItem(k, v) { map.set(k, String(v)); },
    removeItem(k) { map.delete(k); },
    _map: map,
  };
}

function load(storage, response) {
  const root = { dataset: {} };
  const context = {
    console, JSON, Date, Math, Number, String, Object, Array, Promise, window: {},
    document: {
      documentElement: root,
      addEventListener() {},
      querySelector() { return null; },
      querySelectorAll() { return []; },
      createElement() { return { dataset: {}, style: {}, appendChild() {}, setAttribute() {} }; },
    },
    localStorage: storage,
    location: { protocol: 'http:', hostname: '127.0.0.1' },
    // Provided by a sibling UI module in the concatenated bundle.
    _liveHttp() { return true; },
    fetch() { return Promise.resolve({ ok: true, json: () => Promise.resolve(response) }); },
    setTimeout(fn) { if (typeof fn === 'function') fn(); return 0; },
    clearTimeout() {},
  };
  context.window = context;
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  return { context, root };
}

const SEEDED = {
  'omega.hud.theme': JSON.stringify({ theme: 'sunset_amber' }),
  'omega.hud.voice': JSON.stringify({ tts_enabled: false }),
  'omega.hud.sideDrawer.left': 'open',
  'omega.hud.sideDrawer.right': 'open',
  'unrelated.app.key': 'keep me',
};

// 1. purgeHudClientState clears the HUD's keys and nothing else.
{
  const storage = makeStorage(SEEDED);
  const { context } = load(storage, { ok: true, stored: true, settings: {} });
  const removed = context.purgeHudClientState();
  assert.equal(removed, 4, 'every omega.hud.* key is removed');
  assert.deepEqual(Array.from(storage._map.keys()), ['unrelated.app.key'],
    'a foreign key is never touched');
}

// 2. A factory-reset response (stored:false) purges and does NOT re-cache the defaults.
{
  const storage = makeStorage(SEEDED);
  const response = { ok: true, stored: false, settings: { theme: 'paper' } };
  const { context, root } = load(storage, response);
  context.initHudThemeSettings();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(Array.from(storage._map.keys()), ['unrelated.app.key'],
    `cached HUD state survived a reset: ${Array.from(storage._map.keys())}`);
  assert.equal(root.dataset.theme, 'paper', 'the served defaults are applied');
}

// 3. Ordinary boot (stored:true) still caches the server's settings.
{
  const storage = makeStorage({ 'unrelated.app.key': 'keep me' });
  const response = { ok: true, stored: true, settings: { theme: 'night_garden' } };
  const { context, root } = load(storage, response);
  context.initHudThemeSettings();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(storage.getItem('omega.hud.theme'), 'stored settings are cached for the next load');
  assert.equal(JSON.parse(storage.getItem('omega.hud.theme')).theme, 'night_garden');
  assert.equal(root.dataset.theme, 'night_garden');
}

// 4. A response with no `stored` field (an older server) keeps the previous behaviour.
{
  const storage = makeStorage({});
  const response = { ok: true, settings: { theme: 'paper' } };
  const { context } = load(storage, response);
  context.initHudThemeSettings();
  await new Promise(resolve => setImmediate(resolve));
  assert.ok(storage.getItem('omega.hud.theme'), 'absent stored flag is not read as a reset');
}

console.log('hud client state purge: ok');

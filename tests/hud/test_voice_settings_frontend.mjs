import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

const source = fs.readFileSync(
  new URL('../../hud/ui-src/panels/settings-theme.js', import.meta.url),
  'utf8',
);
const wireSource = fs.readFileSync(
  new URL('../../hud/ui-src/panels/settings-wire.js', import.meta.url),
  'utf8',
);

const storage = new Map();
const calls = { cancel: 0, reset: 0, warmup: 0, enqueues: [] };
let fetchImpl = async () => ({
  ok: true,
  json: async () => ({ settings: {} }),
});

const context = {
  console,
  document: {
    documentElement: { dataset: {} },
    getElementById() { return null; },
  },
  FormData: class FormDataStub {
    constructor(form) { this.values = form.values; }
    get(key) { return this.values[key] ?? null; }
  },
  localStorage: {
    getItem(key) { return storage.has(key) ? storage.get(key) : null; },
    setItem(key, value) { storage.set(key, String(value)); },
  },
  _liveHttp: () => false,
  fetch: (...args) => fetchImpl(...args),
  KokoroTTS: {
    cancel() { calls.cancel += 1; },
    resetAvailability() { calls.reset += 1; },
    enqueue(text, opts) { calls.enqueues.push({ text, opts }); },
  },
  warmupKokoroTTS() { calls.warmup += 1; },
};
vm.createContext(context);
vm.runInContext(
  `${source}\n;globalThis.__voiceSettingsTest = {\n` +
  `  normalizeVoiceSettings, applyVoiceSettings, currentVoiceSettings,\n` +
  `  hydrateVoiceForm, setVoiceForm, readLocalVoiceSettings,\n` +
  `  voiceSettingsRevision: typeof voiceSettingsRevision === 'function' ? voiceSettingsRevision : null,\n` +
  `  advanceVoiceSettingsRevision: typeof advanceVoiceSettingsRevision === 'function' ? advanceVoiceSettingsRevision : null,\n` +
  `  previewVoiceSettings: typeof previewVoiceSettings === 'function' ? previewVoiceSettings : null,\n` +
  `};`,
  context,
);
vm.runInContext(
  `${wireSource}\n;globalThis.__voiceSettingsTest.wireVoiceSettingsForm = ` +
  `(typeof wireVoiceSettingsForm === 'function' ? wireVoiceSettingsForm : null);`,
  context,
);

const api = context.__voiceSettingsTest;
const failures = [];

async function check(name, fn) {
  try {
    await fn();
    console.log(`PASS ${name}`);
  } catch (error) {
    failures.push(name);
    console.error(`FAIL ${name}: ${error.message}`);
  }
}

function flushAsync() {
  return new Promise(resolve => setTimeout(resolve, 0));
}

function voiceFormHarness() {
  const values = {
    tts_enabled: 'true',
    tts_voice: 'Aiden',
    tts_speed: '1.0',
    tts_pitch: '1.0',
    stt_enabled: 'true',
    stt_language: 'en',
    stt_task: 'transcribe',
    stt_model: 'parakeet_tdt_06b_v3',
    wake_word_enabled: 'false',
    wake_word: 'omega',
    auto_speak: 'true',
    voice_activity_detection: 'true',
    noise_cancellation: 'true',
  };
  const listeners = new Map();
  const fields = new Map();
  const submit = { disabled: false };
  const form = {
    values,
    dataset: {},
    addEventListener(type, listener) { listeners.set(type, listener); },
    querySelector(selector) {
      if (selector === '[type="submit"]') return submit;
      const match = selector.match(/^\[name="([^"]+)"\]$/);
      if (!match) return null;
      const name = match[1];
      if (!fields.has(name)) {
        fields.set(name, {
          get value() { return String(values[name] ?? ''); },
          set value(value) { values[name] = String(value); },
          get checked() { return values[name] === true || values[name] === 'true'; },
          set checked(value) { values[name] = value ? 'true' : 'false'; },
        });
      }
      return fields.get(name);
    },
  };
  const statusClasses = new Set();
  const status = {
    className: '',
    textContent: '',
    classList: { add(value) { statusClasses.add(value); } },
  };
  const previewListeners = new Map();
  const preview = {
    addEventListener(type, listener) { previewListeners.set(type, listener); },
  };
  return { form, status, preview, submit, values, listeners, previewListeners };
}

await check('normalizes explicit false strings without enabling TTS', () => {
  const value = api.normalizeVoiceSettings({
    tts_enabled: 'false',
    auto_speak: 'false',
  });
  assert.equal(value.tts_enabled, false);
  assert.equal(value.auto_speak, false);
});

await check('falls back from unsafe numeric values', () => {
  const value = api.normalizeVoiceSettings({ tts_speed: 'nan', tts_pitch: -2 });
  assert.equal(value.tts_speed, 1.0);
  assert.equal(value.tts_pitch, 1.0);
});

await check('migrates legacy persisted Kokoro ids to Qwen speakers', () => {
  assert.equal(api.normalizeVoiceSettings({ tts_voice: 'af_bella' }).tts_voice, 'Aiden');
  storage.set('omega.hud.voice', JSON.stringify({ tts_voice: 'am_adam' }));
  const migrated = api.readLocalVoiceSettings();
  assert.equal(migrated.tts_voice, 'Ryan');
  assert.equal(JSON.parse(storage.get('omega.hud.voice')).tts_voice, 'Ryan');
});

await check('hydrates numeric defaults into canonical select option ids', () => {
  const fields = new Map();
  const form = {
    querySelector(selector) {
      if (!fields.has(selector)) fields.set(selector, { value: '' });
      return fields.get(selector);
    },
  };
  api.setVoiceForm(form, { tts_speed: 1.0, tts_pitch: 1.0 });
  assert.equal(fields.get('[name="tts_speed"]').value, '1.0');
  assert.equal(fields.get('[name="tts_pitch"]').value, '1.0');
});

await check('turning TTS off cancels current and queued audio', () => {
  calls.cancel = 0;
  api.applyVoiceSettings({ tts_enabled: true, auto_speak: true });
  api.applyVoiceSettings({ tts_enabled: false, auto_speak: true });
  assert.equal(calls.cancel, 1);
});

await check('turning TTS back on re-arms and warms Kokoro', () => {
  calls.reset = 0;
  calls.warmup = 0;
  api.applyVoiceSettings({ tts_enabled: false, auto_speak: true });
  api.applyVoiceSettings({ tts_enabled: true, auto_speak: true });
  assert.equal(calls.reset, 1);
  assert.equal(calls.warmup, 1);
});

await check('explicit preview works while auto-speak is off', () => {
  assert.equal(typeof api.previewVoiceSettings, 'function');
  calls.enqueues.length = 0;
  api.applyVoiceSettings({
    tts_enabled: true,
    auto_speak: false,
    tts_voice: 'Dylan',
    tts_speed: 0.9,
    tts_pitch: 0.95,
  });
  assert.equal(api.previewVoiceSettings('Omega voice preview.'), true);
  assert.equal(calls.enqueues.length, 1);
  assert.equal(calls.enqueues[0].text, 'Omega voice preview.');
  assert.equal(calls.enqueues[0].opts.voice, 'Dylan');
  assert.equal(calls.enqueues[0].opts.speed, 0.9);
  assert.equal(calls.enqueues[0].opts.pitch, 0.95);
});

await check('delayed hydration does not overwrite a dirty form', async () => {
  context._liveHttp = () => true;
  let resolveFetch;
  fetchImpl = () => new Promise(resolve => { resolveFetch = resolve; });
  const form = { querySelector() { return null; } };
  const status = { className: '', textContent: '' };
  let dirty = false;
  const hydration = api.hydrateVoiceForm(form, status, () => dirty);
  api.applyVoiceSettings({ tts_voice: 'Ryan' });
  dirty = true;
  resolveFetch({
    ok: true,
    json: async () => ({ settings: { tts_voice: 'Vivian' } }),
  });
  await hydration;
  assert.equal(api.currentVoiceSettings().tts_voice, 'Ryan');
  assert.match(status.textContent, /edited|unsaved/i);
});

await check('wire blocks duplicate submits and preserves edits made during save', async () => {
  assert.equal(typeof api.wireVoiceSettingsForm, 'function');
  context._liveHttp = () => true;
  let resolvePost;
  let postCount = 0;
  fetchImpl = (url, options = {}) => {
    if ((options.method || 'GET') === 'GET') {
      return Promise.resolve({
        ok: true,
        json: async () => ({ settings: { tts_voice: 'Aiden' } }),
      });
    }
    postCount += 1;
    return new Promise(resolve => { resolvePost = resolve; });
  };
  const harness = voiceFormHarness();
  api.wireVoiceSettingsForm(harness.form, harness.status, harness.preview);
  await flushAsync();

  harness.values.tts_voice = 'Eric';
  harness.listeners.get('input')();
  const firstSave = harness.listeners.get('submit')({ preventDefault() {} });
  const duplicateSave = harness.listeners.get('submit')({ preventDefault() {} });
  assert.equal(postCount, 1);
  assert.equal(harness.submit.disabled, true);

  harness.values.tts_voice = 'Dylan';
  harness.listeners.get('input')();
  resolvePost({
    ok: true,
    json: async () => ({ ok: true, settings: { tts_voice: 'Eric' } }),
  });
  await Promise.all([firstSave, duplicateSave]);

  assert.equal(api.currentVoiceSettings().tts_voice, 'Dylan');
  assert.match(harness.status.textContent, /save current changes/i);
  assert.equal(harness.submit.disabled, false);
});

await check('older hydration cannot overwrite a completed save', async () => {
  assert.equal(typeof api.wireVoiceSettingsForm, 'function');
  assert.equal(typeof api.voiceSettingsRevision, 'function');
  assert.equal(typeof api.advanceVoiceSettingsRevision, 'function');
  context._liveHttp = () => true;
  let resolveGet;
  fetchImpl = (url, options = {}) => {
    if ((options.method || 'GET') === 'GET') {
      return new Promise(resolve => { resolveGet = resolve; });
    }
    return Promise.resolve({
      ok: true,
      json: async () => ({ ok: true, settings: { tts_voice: 'Eric' } }),
    });
  };
  const harness = voiceFormHarness();
  api.wireVoiceSettingsForm(harness.form, harness.status, harness.preview);
  harness.values.tts_voice = 'Eric';
  harness.listeners.get('input')();
  await harness.listeners.get('submit')({ preventDefault() {} });
  assert.equal(api.currentVoiceSettings().tts_voice, 'Eric');

  resolveGet({
    ok: true,
    json: async () => ({ settings: { tts_voice: 'Vivian' } }),
  });
  await flushAsync();

  assert.equal(api.currentVoiceSettings().tts_voice, 'Eric');
  assert.match(harness.status.textContent, /saved/i);
});

await check('save response from an older panel cannot overwrite a newer panel', async () => {
  context._liveHttp = () => true;
  let resolveOldPost;
  fetchImpl = (url, options = {}) => {
    if ((options.method || 'GET') === 'GET') {
      return Promise.resolve({
        ok: true,
        json: async () => ({ settings: { tts_voice: 'Aiden' } }),
      });
    }
    return new Promise(resolve => { resolveOldPost = resolve; });
  };

  const oldPanel = voiceFormHarness();
  api.wireVoiceSettingsForm(oldPanel.form, oldPanel.status, oldPanel.preview);
  await flushAsync();
  oldPanel.values.tts_voice = 'Eric';
  oldPanel.listeners.get('input')();
  const oldSave = oldPanel.listeners.get('submit')({ preventDefault() {} });

  const newPanel = voiceFormHarness();
  api.wireVoiceSettingsForm(newPanel.form, newPanel.status, newPanel.preview);
  await flushAsync();
  newPanel.values.tts_voice = 'Dylan';
  newPanel.listeners.get('input')();

  resolveOldPost({
    ok: true,
    json: async () => ({ ok: true, settings: { tts_voice: 'Eric' } }),
  });
  await oldSave;

  assert.equal(api.currentVoiceSettings().tts_voice, 'Dylan');
  assert.equal(JSON.parse(storage.get('omega.hud.voice')).tts_voice, 'Dylan');
});

if (failures.length) {
  console.error(`${failures.length} frontend voice settings checks failed`);
  process.exitCode = 1;
}

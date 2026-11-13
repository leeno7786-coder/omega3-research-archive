// Settings: HUD theme + voice persistence (localStorage + /api/settings/*).
// Boot: initHudThemeSettings / initVoiceSettings on window load.
const HUD_THEME_DEFAULTS = {
  theme: 'industrial', density: 'comfortable', motion: 'full', glow: 'balanced', panel_depth: 'standard'
};
const HUD_THEME_ALLOWED = {
  theme: ['industrial', 'paper', 'night_garden', 'high_contrast', 'ocean_deep', 'sunset_amber', 'arctic_mist', 'midnight_violet'],
  density: ['comfortable', 'compact', 'spacious'],
  motion: ['full', 'reduced'],
  glow: ['balanced', 'quiet', 'vivid'],
  panel_depth: ['standard', 'flat', 'floating'],
};
const HUD_THEME_STORAGE_KEY = 'omega.hud.theme';
let _hudThemeSettings = { ...HUD_THEME_DEFAULTS };

function normalizeHudThemeSettings(settings) {
  const out = { ...HUD_THEME_DEFAULTS };
  const src = settings || {};
  for (const key of Object.keys(HUD_THEME_DEFAULTS)) {
    const value = String(src[key] || '');
    if (HUD_THEME_ALLOWED[key].includes(value)) out[key] = value;
  }
  return out;
}
function currentHudThemeSettings() { return normalizeHudThemeSettings(_hudThemeSettings); }
function saveLocalHudThemeSettings(settings) {
  try { localStorage.setItem(HUD_THEME_STORAGE_KEY, JSON.stringify(normalizeHudThemeSettings(settings))); } catch (_) { }
}
function readLocalHudThemeSettings() {
  try {
    const raw = localStorage.getItem(HUD_THEME_STORAGE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (_) { return null; }
}
// A factory reset clears guardian/state, so the server answers stored:false — "never
// configured", not "configured to the defaults". The operator's browser profile is NOT on
// the SSD, so no wipe there can reach these keys; this response is the only moment the HUD
// can drop the previous operator's appearance, voice and drawer state. The purge covers
// every omega.hud.* key, including the side drawer's, which has no server copy at all.
// It lands on the settings response, so a drawer opened from cache on the very first
// post-reset load closes from the next one.
function purgeHudClientState() {
  try {
    if (!window.localStorage) return 0;
    const doomed = [];
    for (let i = 0; i < localStorage.length; i += 1) {
      const key = localStorage.key(i);
      if (key && key.indexOf('omega.hud.') === 0) doomed.push(key);
    }
    doomed.forEach(key => localStorage.removeItem(key));
    return doomed.length;
  } catch (_) { return 0; }
}
function applyHudThemeSettings(settings) {
  const s = normalizeHudThemeSettings(settings);
  _hudThemeSettings = s;
  const root = document.documentElement;
  root.dataset.theme = s.theme;
  root.dataset.hudDensity = s.density;
  root.dataset.hudMotion = s.motion;
  root.dataset.hudGlow = s.glow;
  root.dataset.panelDepth = s.panel_depth;
}
function setHudThemeForm(form, settings) {
  if (!form) return;
  const s = normalizeHudThemeSettings(settings);
  const theme = form.querySelector(`input[name="theme"][value="${s.theme}"]`);
  if (theme) theme.checked = true;
  for (const key of ['density', 'motion', 'glow', 'panel_depth']) {
    const field = form.querySelector(`[name="${key}"]`);
    if (field) field.value = s[key] || HUD_THEME_DEFAULTS[key];
  }
}
function collectHudThemeForm(form) {
  const data = new FormData(form);
  return {
    theme: String(data.get('theme') || HUD_THEME_DEFAULTS.theme),
    density: String(data.get('density') || HUD_THEME_DEFAULTS.density),
    motion: String(data.get('motion') || HUD_THEME_DEFAULTS.motion),
    glow: String(data.get('glow') || HUD_THEME_DEFAULTS.glow),
    panel_depth: String(data.get('panel_depth') || HUD_THEME_DEFAULTS.panel_depth),
  };
}
async function hydrateHudThemeForm(form, status) {
  const local = readLocalHudThemeSettings();
  if (local) {
    applyHudThemeSettings(local);
    setHudThemeForm(form, local);
    if (status) status.textContent = 'local';
  }
  if (!_liveHttp()) {
    if (status) status.textContent = 'local only';
    return;
  }
  try {
    const r = await fetch('/api/settings/hud-theme', { cache: 'no-store' });
    if (!r.ok) throw new Error('theme settings unavailable');
    const result = await r.json();
    const settings = result.settings || HUD_THEME_DEFAULTS;
    if (result.stored === false) purgeHudClientState();
    applyHudThemeSettings(settings);
    setHudThemeForm(form, settings);
    if (result.stored !== false) saveLocalHudThemeSettings(settings);
    if (status) { status.className = 'settings-status ok'; status.textContent = 'loaded'; }
  } catch (_) {
    if (status) { status.className = 'settings-status fail'; status.textContent = 'using local'; }
  }
}
function initHudThemeSettings() {
  const local = readLocalHudThemeSettings();
  applyHudThemeSettings(local || HUD_THEME_DEFAULTS);
  if (_liveHttp()) {
    fetch('/api/settings/hud-theme', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(result => {
        if (!result || !result.settings) return;
        if (result.stored === false) purgeHudClientState();
        applyHudThemeSettings(result.settings);
        if (result.stored !== false) saveLocalHudThemeSettings(result.settings);
      })
      .catch(() => { });
  }
}

const HUD_VOICE_DEFAULTS = {
  tts_enabled: true,
  tts_voice: 'Aiden',
  tts_speed: 1.0,
  tts_pitch: 1.0,
  stt_enabled: true,
  stt_language: 'auto',
  stt_task: 'transcribe',
  stt_model: 'parakeet_tdt_06b_v3',
  wake_word_enabled: false,
  wake_word: 'hey omega',
  auto_speak: true,
  voice_activity_detection: true,
  noise_cancellation: true,
  live_mode: true,
};
const HUD_VOICE_ALLOWED = {
  tts_voice: ['Aiden', 'Ryan', 'Vivian', 'Serena', 'Uncle_Fu', 'Dylan', 'Eric', 'Ono_Anna', 'Sohee'],
  stt_language: ['auto', 'en', 'es', 'fr', 'de', 'ja', 'zh', 'ko', 'pt', 'it', 'hi'],
  stt_task: ['transcribe', 'translate'],
  stt_model: ['parakeet_tdt_06b_v3'],
  tts_speed: ['0.75', '0.9', '1.0', '1.15', '1.3'],
  tts_pitch: ['0.85', '0.95', '1.0', '1.05', '1.15'],
};
// Upgrade-only conversion for values written by the removed Kokoro roster.
// These IDs are never offered or sent to the Qwen server.
const LEGACY_TTS_VOICE_MIGRATIONS = {
  af_bella: 'Aiden', af_alloy: 'Aiden', af_nova: 'Aiden', af_sky: 'Aiden',
  am_adam: 'Ryan', am_echo: 'Ryan', am_onyx: 'Ryan', bf_alice: 'Aiden',
  bm_fable: 'Ryan', jarvis: 'Ryan', alloy: 'Aiden', echo: 'Ryan',
  fable: 'Ryan', onyx: 'Ryan', nova: 'Aiden', shimmer: 'Aiden',
};
const HUD_VOICE_STORAGE_KEY = 'omega.hud.voice';
let _hudVoiceSettings = { ...HUD_VOICE_DEFAULTS };
let _hudVoiceSettingsRevision = 0;

function voiceSettingsRevision() {
  return _hudVoiceSettingsRevision;
}

function advanceVoiceSettingsRevision() {
  _hudVoiceSettingsRevision += 1;
  return _hudVoiceSettingsRevision;
}

function voiceBooleanValue(raw, fallback) {
  if (typeof raw === 'boolean') return raw;
  if (typeof raw === 'number' && Number.isFinite(raw) && (raw === 0 || raw === 1)) {
    return raw === 1;
  }
  if (typeof raw === 'string') {
    const cleaned = raw.trim().toLowerCase();
    if (['true', '1', 'on', 'yes'].includes(cleaned)) return true;
    if (['false', '0', 'off', 'no'].includes(cleaned)) return false;
  }
  return fallback;
}

function voiceOptionId(key, value) {
  const allowed = HUD_VOICE_ALLOWED[key] || [];
  const raw = String(value);
  if (allowed.includes(raw)) return raw;
  if (key === 'tts_speed' || key === 'tts_pitch') {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) {
      const match = allowed.find(option => Number(option) === numeric);
      if (match) return match;
    }
  }
  return String(HUD_VOICE_DEFAULTS[key]);
}

function normalizeVoiceSettings(settings) {
  const out = { ...HUD_VOICE_DEFAULTS };
  const src = settings || {};
  for (const key of Object.keys(HUD_VOICE_DEFAULTS)) {
    const raw = src[key];
    if (raw === null || raw === undefined) continue;
    // Boolean fields
    if (typeof out[key] === 'boolean') {
      out[key] = voiceBooleanValue(raw, out[key]);
      continue;
    }
    // Numeric fields
    if (typeof out[key] === 'number') {
      const option = voiceOptionId(key, raw);
      const num = Number(option);
      if (Number.isFinite(num)) out[key] = num;
      continue;
    }
    // String / enum fields
    let value = String(raw);
    if (key === 'tts_voice') {
      value = LEGACY_TTS_VOICE_MIGRATIONS[value.toLowerCase()] || value;
    }
    if (HUD_VOICE_ALLOWED[key] && HUD_VOICE_ALLOWED[key].includes(value)) {
      out[key] = value;
    } else if (!HUD_VOICE_ALLOWED[key]) {
      out[key] = value;
    }
  }
  return out;
}
function currentVoiceSettings() { return normalizeVoiceSettings(_hudVoiceSettings); }
function saveLocalVoiceSettings(settings) {
  try { localStorage.setItem(HUD_VOICE_STORAGE_KEY, JSON.stringify(normalizeVoiceSettings(settings))); } catch (_) { }
}
function readLocalVoiceSettings() {
  try {
    const raw = localStorage.getItem(HUD_VOICE_STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    const normalized = normalizeVoiceSettings(parsed);
    if (parsed && parsed.tts_voice !== normalized.tts_voice) {
      localStorage.setItem(HUD_VOICE_STORAGE_KEY, JSON.stringify(normalized));
    }
    return normalized;
  } catch (_) { return null; }
}
function applyVoiceSettings(settings) {
  const previous = _hudVoiceSettings;
  const s = normalizeVoiceSettings(settings);
  _hudVoiceSettings = s;
  const kokoro = globalThis.KokoroTTS;
  const stoppedOutput = (
    (previous.tts_enabled !== false && s.tts_enabled === false)
    || (previous.auto_speak !== false && s.auto_speak === false)
  );
  if (stoppedOutput && kokoro && typeof kokoro.cancel === 'function') {
    kokoro.cancel();
  }
  if (previous.tts_enabled === false && s.tts_enabled === true) {
    if (kokoro && typeof kokoro.resetAvailability === 'function') {
      kokoro.resetAvailability();
    }
    if (typeof globalThis.warmupKokoroTTS === 'function') {
      globalThis.warmupKokoroTTS();
    }
  }
}

function previewVoiceSettings(text = 'Omega voice preview. These settings are active.') {
  const settings = currentVoiceSettings();
  const kokoro = globalThis.KokoroTTS;
  if (!settings.tts_enabled || !kokoro || typeof kokoro.enqueue !== 'function') return false;
  if (typeof kokoro.resetAvailability === 'function') kokoro.resetAvailability();
  kokoro.enqueue(String(text), {
    voice: settings.tts_voice,
    speed: settings.tts_speed,
    pitch: settings.tts_pitch,
  });
  return true;
}
function setVoiceForm(form, settings) {
  if (!form) return;
  const s = normalizeVoiceSettings(settings);
  // Boolean fields
  for (const key of ['tts_enabled', 'stt_enabled', 'wake_word_enabled', 'auto_speak', 'voice_activity_detection', 'noise_cancellation', 'live_mode']) {
    const field = form.querySelector(`[name="${key}"]`);
    if (field) field.value = String(s[key]);
  }
  // String/enum fields
  for (const key of ['tts_voice', 'stt_language', 'stt_task', 'stt_model']) {
    const field = form.querySelector(`[name="${key}"]`);
    if (field) field.value = String(s[key] || HUD_VOICE_DEFAULTS[key]);
  }
  for (const key of ['tts_speed', 'tts_pitch']) {
    const field = form.querySelector(`[name="${key}"]`);
    if (field) field.value = voiceOptionId(key, s[key]);
  }
  // Text field
  const wakeWord = form.querySelector('[name="wake_word"]');
  if (wakeWord) wakeWord.value = s.wake_word || HUD_VOICE_DEFAULTS.wake_word;
}
function collectVoiceForm(form) {
  const data = new FormData(form);
  const parseBool = (v) => v === 'true' || v === true;
  return {
    tts_enabled: parseBool(data.get('tts_enabled')),
    tts_voice: String(data.get('tts_voice') || HUD_VOICE_DEFAULTS.tts_voice),
    tts_speed: parseFloat(data.get('tts_speed') || HUD_VOICE_DEFAULTS.tts_speed),
    tts_pitch: parseFloat(data.get('tts_pitch') || HUD_VOICE_DEFAULTS.tts_pitch),
    stt_enabled: parseBool(data.get('stt_enabled')),
    stt_language: String(data.get('stt_language') || HUD_VOICE_DEFAULTS.stt_language),
    stt_task: String(data.get('stt_task') || HUD_VOICE_DEFAULTS.stt_task),
    stt_model: String(data.get('stt_model') || HUD_VOICE_DEFAULTS.stt_model),
    wake_word_enabled: parseBool(data.get('wake_word_enabled')),
    wake_word: String(data.get('wake_word') || HUD_VOICE_DEFAULTS.wake_word),
    auto_speak: parseBool(data.get('auto_speak')),
    voice_activity_detection: parseBool(data.get('voice_activity_detection')),
    noise_cancellation: parseBool(data.get('noise_cancellation')),
    live_mode: parseBool(data.get('live_mode')),
  };
}
async function hydrateVoiceForm(form, status, isDirty = () => false) {
  const local = readLocalVoiceSettings();
  if (local) {
    applyVoiceSettings(local);
    setVoiceForm(form, local);
    if (status) status.textContent = 'local';
  }
  if (!_liveHttp()) {
    if (status) status.textContent = 'local only';
    return;
  }
  const requestRevision = advanceVoiceSettingsRevision();
  try {
    const r = await fetch('/api/settings/voice', { cache: 'no-store' });
    if (!r.ok) throw new Error('voice settings unavailable');
    const result = await r.json();
    const settings = result.settings || HUD_VOICE_DEFAULTS;
    if (isDirty()) {
      if (status) {
        status.className = 'settings-status';
        status.textContent = 'edited · save to apply';
      }
      return;
    }
    if (voiceSettingsRevision() !== requestRevision) return;
    if (result.stored === false) purgeHudClientState();
    applyVoiceSettings(settings);
    setVoiceForm(form, settings);
    if (result.stored !== false) saveLocalVoiceSettings(settings);
    if (status) { status.className = 'settings-status ok'; status.textContent = 'loaded'; }
  } catch (_) {
    if (status) { status.className = 'settings-status fail'; status.textContent = 'using local'; }
  }
}
function initVoiceSettings() {
  const local = readLocalVoiceSettings();
  applyVoiceSettings(local || HUD_VOICE_DEFAULTS);
  if (_liveHttp()) {
    const requestRevision = advanceVoiceSettingsRevision();
    fetch('/api/settings/voice', { cache: 'no-store' })
      .then(r => r.ok ? r.json() : null)
      .then(result => {
        if (!result || !result.settings) return;
        if (voiceSettingsRevision() !== requestRevision) return;
        if (result.stored === false) purgeHudClientState();
        applyVoiceSettings(result.settings);
        if (result.stored !== false) saveLocalVoiceSettings(result.settings);
      })
      .catch(() => { });
  }
}

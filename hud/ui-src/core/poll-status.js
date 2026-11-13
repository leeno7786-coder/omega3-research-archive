// Status + boot banner polls (hud/ui-src/core/poll-status.js)
// Depends on: Telemetry (indirect via applyLiveBootStrings), DOM pill glyphs
// Exports: window.pollStatus, window.pollBootState, window.applyLiveBootStrings

// ── Top status glyph -> backend state -> CSS class mappings ───
// Retired panel-adjacent top items (identity, memory, pressure)
// stay in /api/status for backend consumers, but are no longer
// rendered in the HUD chrome.
const PILL_IDS = [
  'voice', 'l0',
  'presence',
  'embed', 'classify', 'face', 'coding_model',
];
const PILL_TO_GLYPH = {
  voice: 'g-voice', l0: 'g-l0',
  presence: 'g-presence',
  embed: 'g-embed', classify: 'g-classify',
  face: 'g-face', coding_model: 'g-llm',
};
// 4 backend states -> 4 CSS classes the design already provides.
// 'down' uses 'warn' (brick) so operators see it; 'loading' stays
// 'dim' (gray) since loading is benign / expected during startup.
const STATE_TO_CLASS = {
  live: 'live',
  loading: 'dim',
  degraded: 'warn',
  down: 'warn',
};

let _serverWasUp = false;
let _failCount = 0;
async function pollStatus() {
  try {
    const r = await fetch('/api/status', { cache: 'no-store' });
    if (!r.ok) return;
    _serverWasUp = true;
    _failCount = 0;
    const status = await r.json();
    for (const pill of PILL_IDS) {
      const cell = status[pill];
      if (!cell) continue;
      const el = document.getElementById(PILL_TO_GLYPH[pill]);
      if (!el) continue;
      // Reset state classes; preserve the base 'status-glyph' class.
      el.classList.remove('dim', 'live', 'warn', 'copper');
      el.classList.add(STATE_TO_CLASS[cell.state] || 'dim');
      // Surface the detail string via the parent's title (tooltip).
      const parent = el.parentElement;
      if (parent && cell.detail) {
        parent.setAttribute('title', `${cell.label}: ${cell.detail}`);
      }
    }
  } catch (e) {
    // Server probably not up yet; next poll will retry. Don't log:
    // the operator's console shouldn't fill with retry noise.
    if (_serverWasUp) {
      _failCount++;
      if (_failCount >= 3) {
        window._serverShutdown = true;
        document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;color:#666;font-family:sans-serif;">Server shutdown. You may close this tab.</div>';
        window.close();
      }
    }
  }
}

// ── HUD-3: boot banner fade ───────────────────────────────────
// Once /api/boot/state reports phase='ready', re-add 'boot-hidden'
// to #banner so it fades out (visibility:hidden). The mock boot
// sequence in the original design briefly shows the banner; HUD-3
// ensures it stays gone once the real substrates report live.
let _bannerFaded = false;
async function pollBootState() {
  try {
    const r = await fetch('/api/boot/state', { cache: 'no-store' });
    if (!r.ok) return;
    const st = await r.json();
    const banner = document.getElementById('banner');
    if (!banner) return;
    if (st.phase === 'ready' && !_bannerFaded) {
      banner.classList.add('boot-hidden');
      _bannerFaded = true;
    }
    // Surface the boot summary as the banner's title for hover
    // diagnostics (operator can mouse over to see what's pending).
    if (st.summary) {
      banner.setAttribute('title', `boot: ${st.phase} — ${st.summary}`);
    }
  } catch (e) { /* server not up yet; retry on next tick */ }
}

// ── FX-1/2/3: real cycle counter + objective + activity ──────
// The design ships with a mock cycle counter (random + auto-increment)
// and hardcoded objective + rotating-array activity. Once real
// telemetry is available we override all three from a single
// snapshot poll so the boot beat reads as live system state.
let _cycleIntervalKilled = false;
let _cycleObserver = null;
let _liveEpoch = 0;
function applyLiveBootStrings(snap) {
  if (!snap) return;
  // FX-1: cycle counter from omega.epoch
  const epoch = (snap.omega && snap.omega.epoch) || 0;
  if (epoch > 0) {
    _liveEpoch = epoch;
    const cyc = document.getElementById('cycle');
    if (cyc) {
      cyc.textContent = String(epoch);
      // The design's tickCycle setInterval keeps writing a random
      // ~7140 value into #cycle every 7s. We can't clearInterval
      // a timer we don't own — so we watch for stale writes and
      // immediately revert them. Install once.
      if (!_cycleObserver && typeof MutationObserver !== 'undefined') {
        _cycleObserver = new MutationObserver(() => {
          const t = cyc.textContent;
          if (t !== String(_liveEpoch)) {
            cyc.textContent = String(_liveEpoch);
          }
        });
        _cycleObserver.observe(cyc, {
          childList: true, characterData: true,
          subtree: true
        });
        _cycleIntervalKilled = true;
      }
    }
  }
  // Feed machine-prose (objective/activity/voice-state) intentionally
  // not updated — HUD_PANELS_VISUAL_PACKET R2.
}

window.pollStatus = pollStatus;
window.pollBootState = pollBootState;
window.applyLiveBootStrings = applyLiveBootStrings;

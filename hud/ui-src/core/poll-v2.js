// v2 substrate endpoint polls + pill badge helper (hud/ui-src/core/poll-v2.js)
// Depends on: Telemetry, _applyV2PillBadges, _applyV2AtmosphereEffects,
//   optional panel-local refresh helpers when their panels are bundled
// Exports: window.pollV2Fast, window.pollV2Medium, window.pollV2Slow, window._setPillBadge

// ── P3-S06: v2 substrate endpoint polls ──────────────────────────
// Cadences per HUD_PILL_TELEMETRY_V2.md §3 + §5:
//   2s — pressure-loop, presence, pressure_last_tick (fast-changing)
//   5s — broker snapshot, response state (capability state + talkativity)
//   10s — runtime health (rarely changes; one /health call)
// All folded into Telemetry.system.v2 so panels + pill badges
// can read from a single canonical slot.
// P3-S06 review fix I2: per-request timeout via AbortController.
// Without this, a single slow endpoint stalls the entire 2s tier
// because Promise.all waits for the slowest. 1500ms cap is well
// under the 2s poll interval so a stuck endpoint can't pile up
// requests.
async function _grabJson(url, timeoutMs) {
  const ms = (typeof timeoutMs === 'number') ? timeoutMs : 1500;
  let ctl = null;
  let timer = null;
  try {
    if (typeof AbortController !== 'undefined') {
      ctl = new AbortController();
      timer = setTimeout(() => { try { ctl.abort(); } catch (_) { } }, ms);
    }
    const r = await fetch(url, {
      cache: 'no-store',
      signal: ctl ? ctl.signal : undefined,
    });
    if (!r.ok) return null;
    return await r.json();
  } catch (e) {
    return null;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function pollV2Fast() {
  const [pressureLoop, presence, pressureTick] = await Promise.all([
    _grabJson('/api/pressure-loop/state'),
    _grabJson('/api/presence/state'),
    _grabJson('/api/cuedesk/last_tick'),
  ]);
  if (pressureLoop === null && presence === null && pressureTick === null) return;
  const lastTick = pressureTick && pressureTick.tick;
  Telemetry.update({
    system: {
      v2: {
        pressure_loop: pressureLoop,
        presence: presence,
        pressure_last_tick: lastTick,
        // Compatibility for older panel helpers still reading this key.
        cuedesk_last_tick: lastTick,
        ts_fast: Date.now(),
      }
    }
  });
  _applyV2PillBadges();
  _applyV2AtmosphereEffects();
  if (typeof _refreshPresenceOrbState === 'function') _refreshPresenceOrbState();
  if (typeof _refreshRespiratoryPauseState === 'function') _refreshRespiratoryPauseState();
  if (typeof _refreshCycleWheelState === 'function') _refreshCycleWheelState();
}

async function pollV2Medium() {
  try {
    if (typeof _refreshProactivePinboard === 'function') {
      await _refreshProactivePinboard();
    }
  } catch (_e) { /* badge refresh is independent of broker/outreach */ }

  const [broker, responseState, outreach] = await Promise.all([
    _grabJson('/api/broker/snapshot'),
    _grabJson('/api/response/state'),
    _grabJson('/api/global-operator-outreach/active'),
  ]);
  if (broker === null && responseState === null && outreach === null) return;
  Telemetry.update({
    system: {
      v2: {
        broker: broker && broker.capabilities,
        response: responseState,
        global_operator_outreach: outreach,
        ts_medium: Date.now(),
      }
    }
  });
  _applyV2PillBadges();
  _applyV2AtmosphereEffects();
  if (typeof _refreshPresenceOrbState === 'function') _refreshPresenceOrbState();
  if (typeof _refreshRespiratoryPauseState === 'function') _refreshRespiratoryPauseState();
  if (typeof _refreshCycleWheelState === 'function') _refreshCycleWheelState();
}

async function pollV2Slow() {
  const runtime = await _grabJson('/api/runtime/health');
  if (!runtime) return;
  Telemetry.update({
    system: {
      v2: {
        runtime: runtime,
        ts_slow: Date.now(),
      }
    }
  });
}

// ── P3-S06: per-pill v2 badges ───────────────────────────────────
// Adjacent indicators on top-level nav pills.
//   identity pill → presence glyph (• PRESENT / ○ ABSENT / ? UNKNOWN)
//   autoloop pill → pause indicator (⏸ when paused)
//   omega pill    → tool-synthesis queue depth count (when > 0)
// Read from Telemetry.system.v2 — set by the polls above. Pure
// DOM mutation; no fetches inside this function (called from the
// poll handlers after Telemetry.update fires).
function _setPillBadge(pillSel, badgeId, html, extraClass) {
  const pill = document.querySelector(pillSel);
  if (!pill) return;
  let badge = Array.from(pill.querySelectorAll('.pill-v2-badge'))
    .find(el => el.dataset.badgeId === badgeId);
  if (!html) {
    if (badge) badge.remove();
    return;
  }
  if (!badge) {
    badge = document.createElement('span');
    badge.className = 'pill-v2-badge';
    // Inline minimal styling — keeps the badge unobtrusive on
    // any theme. Operator can override via theme CSS.
    badge.style.cssText =
      'display:inline-block;margin-left:6px;padding:1px 5px;' +
      'font:9px/1.3 var(--mono,monospace);' +
      'border:1px solid var(--hairline,#444);border-radius:2px;' +
      'color:var(--whisper,#888);background:transparent;' +
      'letter-spacing:.04em;vertical-align:middle;';
    pill.appendChild(badge);
  }
  badge.dataset.badgeId = badgeId;
  badge.innerHTML = html;
  // P3-S06 review fix I1: extraClass now actually applies a CSS
  // class so theme rules can target it (e.g., dev_paused_pulse
  // animation below). Previously stored on dataset only — no
  // CSS responded so the dev-pulse promise was inert.
  // Strip all existing v2-* classes before applying the new one
  // so state transitions clear the previous state cleanly.
  for (const c of Array.from(badge.classList)) {
    if (c.startsWith('v2-')) badge.classList.remove(c);
  }
  if (extraClass) {
    badge.classList.add('v2-' + extraClass);
    badge.dataset.v2Class = extraClass;
  } else if (badge.dataset.v2Class) {
    delete badge.dataset.v2Class;
  }
}
window.pollV2Fast = pollV2Fast;
window.pollV2Medium = pollV2Medium;
window.pollV2Slow = pollV2Slow;
window._setPillBadge = _setPillBadge;

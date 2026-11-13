// Canvas live refresh + operator identity (hud/ui-src/core/canvas-live.js)
// Depends on: Telemetry, _escapeHtml, _currentPanelId
// Exports: OperatorIdentity, _refreshCycleWheelState, _renderRecallSnapshot

function _liveHttp() {
  return location.protocol === 'http:' || location.protocol === 'https:';
}
window._liveHttp = _liveHttp;

// ─────────────────────────────────────────────────────────────────
// Telemetry adapter — single source of truth for all panel data
// ─────────────────────────────────────────────────────────────────
// Every panel reads from Telemetry.snapshot() instead of inlining
// literals. The mock provider at the bottom of this script slowly
// mutates a handful of values to prove the wiring; a real backend
// drop-in just replaces the provider — no panel code changes.
//
// Contract:
//   Telemetry.snapshot()    → state object (read at render time)
//   Telemetry.update(patch) → deep-merge patch + notify subscribers
//   Telemetry.subscribe(fn) → fn(state) on every update; returns unsub
//
// Cadence policy (per the txt's restraint discipline): only values
// that genuinely *evolve* on a sub-minute scale tick. Identity
// coherence, posture, becoming-objective stay still — they don't
// move in seconds. Pressure, cycle interval, approval ages do.

// ─────────────────────────────────────────────────────────────────
// OperatorIdentity — dynamic operator display name
//
// Replaces hardcoded "Noah" references throughout the HUD. Reads
// from `GET /api/operator/identity` (which queries the operator
// persona memory at lobe 1, coord [1,0]). Empty until the operator
// fills out the Settings tab; falls back to neutral "operator" for
// rendering. The system's IDENTITY of creator (immutable in
// omega_identity.json + creator_noah.json) is unaffected — that's
// substrate-level identity, separate from per-operator display.
//
// API:
//   await OperatorIdentity.fetchIdentity()  → loads from backend, true on success
//   OperatorIdentity.getName()              → "<name>" or "operator" if unset
//   OperatorIdentity.getRawName()           → "<name>" or "" (no fallback)
//   OperatorIdentity.getRelationship()      → "<role>" or ""
//   OperatorIdentity.subscribe(cb)          → cb(name, relationship) on update
// ─────────────────────────────────────────────────────────────────
const OperatorIdentity = (function () {
  let _name = '';
  let _relationship = '';
  let _onboarded = false;
  let _loaded = false;
  const _subs = [];
  function _notify() {
    const n = _name || 'operator';
    _subs.slice().forEach(cb => { try { cb(n, _relationship); } catch (_) { } });
  }
  async function fetchIdentity() {
    try {
      const r = await fetch('/api/operator/identity', { cache: 'no-store' });
      if (!r.ok) { _loaded = true; return false; }
      const data = await r.json();
      _name = String((data && data.name) || '').trim();
      _relationship = String((data && data.relationship) || '').trim();
      // onboarded -> the profile is saved + locked (form disabled, onboarding prompt suppressed).
      _onboarded = Boolean(data && data.onboarded) || !!_name;
      _loaded = true;
      _notify();
      return true;
    } catch (_) {
      _loaded = true;
      return false;
    }
  }
  return {
    fetchIdentity,
    getName: () => _name || 'operator',
    getRawName: () => _name,
    getRelationship: () => _relationship,
    isLoaded: () => _loaded,
    isOnboarded: () => _onboarded,
    subscribe: (cb) => { if (typeof cb === 'function') _subs.push(cb); },
  };
})();
window.OperatorIdentity = OperatorIdentity;

// P3-S10: lightweight cycle-wheel refresh — flip data-suppressed
// on the wrapper when the pressure-tick suppress reason changes,
// and update the band attribute when pressure shifts a threshold.
// Same lightweight pattern as the orb/respiratory refresh helpers.
function _refreshCycleWheelState() {
  // P3-S17 gate: skip entirely unless brain panel is open.
  if (_currentPanelId !== 'brain') return;
  try {
    const host = document.getElementById('brain-dashboard-host');
    if (!host) return;
    const wrap = host.querySelector('.cycle-wheel-wrap');
    if (!wrap) return;
    const snap = (window.Telemetry && Telemetry.snapshot)
      ? Telemetry.snapshot() : {};
    // Pressure suppress reason lives in v2.pressure_last_tick after
    // pollV2Fast — same data source the wrap initially read.
    const v2 = (snap.system && snap.system.v2) || {};
    const tick = v2.pressure_last_tick || v2.cuedesk_last_tick || {};
    const sr = String(tick.suppress_reason || '').trim();
    const nextSup = sr ? 'true' : 'false';
    if (wrap.dataset.suppressed !== nextSup) {
      wrap.dataset.suppressed = nextSup;
    }
    // Pressure band from snapshot.cycle.pressure ("0.42"-style).
    const cyc = snap.cycle || {};
    const p = parseFloat(cyc.pressure);
    const band = Number.isFinite(p)
      ? (p >= 0.66 ? 'hi' : p >= 0.33 ? 'med' : 'lo')
      : 'lo';
    if (wrap.dataset.band !== band) {
      wrap.dataset.band = band;
    }
  } catch (_e) { /* non-fatal — panel not mounted */ }
}

// P3-S14: Recall Snapshot hero render (§2.2). The killer §9.1
// Bloom (animated lobe field with pulse-on-write, co-recall
// edges, particle background) is deferred to a follow-up slice;
// this slice ships the recall snapshot hero + a Bloom-lite
// heatmap card showing per-lobe write density.
function _renderRecallSnapshot(m) {
  const E = _escapeHtml;
  const recalls = m.topRecall || [];
  const mode = String(m.recall_mode || 'semantic').toLowerCase();
  const modeCls = (mode === 'lexical_fallback' || mode === 'lexical')
    ? 'lexical' : 'semantic';
  const l0 = m.l0_state || {};
  const embedUp = ['up', 'available', 'ok', 'ready']
    .includes(String(l0.embed || '').toLowerCase());
  const classifyUp = ['up', 'available', 'ok', 'ready']
    .includes(String(l0.classify || '').toLowerCase());
  const slice = recalls.slice(0, 6);
  const barH = 18;
  const gap = 10;
  const svgH = slice.length ? (8 + slice.length * (barH + gap)) : 40;
  const bars = slice.length === 0
    ? `<line x1="0" y1="20" x2="300" y2="20" class="recall-bar-empty"/>`
    : slice.map((r, i) => {
      const score = parseFloat(r.score) || 0;
      const w = Math.max(6, score * 292);
      const y = 8 + i * (barH + gap);
      const tier = score >= 0.8 ? 'high' : score >= 0.5 ? 'mid' : 'low';
      const tip = `${r.coord || '—'} · ${score.toFixed(3)}`;
      return (
        `<rect class="recall-bar ${tier}" x="0" y="${y}" ` +
        `width="${w.toFixed(1)}" height="${barH}" rx="2">` +
        `<title>${E(tip)}</title></rect>`
      );
    }).join('');
  return (
    `<div class="memory-recall-wrap memory-recall-visual" ` +
    `data-mode="${modeCls}" data-embed="${embedUp ? 'up' : 'down'}" ` +
    `data-classify="${classifyUp ? 'up' : 'down'}">` +
    `<svg class="recall-bars-svg" viewBox="0 0 300 ${svgH}" ` +
    `role="img" aria-label="Recall similarity · ${slice.length} hits">` +
    bars +
    `</svg>` +
    `<div class="recall-mode-glyph" aria-hidden="true">` +
    `<span class="dot embed"></span>` +
    `<span class="dot classify"></span>` +
    `<span class="dot mode"></span>` +
    `</div></div>`
  );
}

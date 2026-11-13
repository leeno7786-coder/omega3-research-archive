// Panel-local canvas renderers (hud/ui-src/core/canvas-panels.js)
// Depends on: _escapeHtml, _traitVisualWeight, _render*Dashboard, _PANEL_LIVE_REFRESH, _refresh*Panel

// NOTE: _escapeHtml is defined once, earlier in this script (it escapes & < > " and ').
// A second definition here previously OMITTED the single-quote escape and, via function
// hoisting, silently overrode the complete one — letting a ' break out of single-quoted
// attributes. Removed; the canonical _escapeHtml above is the sole definition.

// P3-S08: read current presence state from Telemetry snapshot if
// available. Returns "PRESENT" / "ABSENT" / "UNKNOWN".
function _currentPresenceState() {
  try {
    const snap = (window.Telemetry && Telemetry.snapshot) ? Telemetry.snapshot() : {};
    const v2 = (snap.system && snap.system.v2) || {};
    const ps = v2.presence;
    if (ps && typeof ps === 'object' && ps.state) {
      return String(ps.state).toUpperCase();
    }
  } catch (_e) { /* fall through */ }
  return 'UNKNOWN';
}

// P3-S08: Presence Orb render — SVG hero visual (§9.3). Caller
// passes the identity payload + presence state; receives an HTML
// string ready to set as innerHTML on the wrapper container.
function _renderPresenceOrb(identityData, presenceState) {
  const E = _escapeHtml;
  const state = String(presenceState || 'UNKNOWN').toUpperCase();
  const charter = identityData.charter || {};
  const operators = identityData.operators || [];
  const sv = identityData.selfVector || {};
  const traits = identityData.traits || [];

  const idHashShort = String(charter.identity_hash || 'omega').slice(0, 6).toUpperCase();

  // Operators on the inner ring (max 5 — beyond that legibility breaks).
  const ops = operators.slice(0, 5);
  const orbitR = 108;
  const anchorPieces = [];
  ops.forEach((op, i) => {
    const ang = (360 / Math.max(1, ops.length)) * i - 90;
    const rad = ang * Math.PI / 180;
    const cx = 160 + orbitR * Math.cos(rad);
    const cy = 160 + orbitR * Math.sin(rad);
    const r = 22;
    const nm = E((op.name || '?').slice(0, 8));
    // Each anchor: backing circle (always) + optional thumbnail
    // <image> via clipPath + name label arc'd below.
    const thumbUrl = op.thumbnail_url || '';
    let pic = '';
    if (thumbUrl) {
      const clipId = `orb-clip-${i}`;
      pic =
        `<defs><clipPath id="${clipId}">` +
        `<circle cx="${cx}" cy="${cy}" r="${r - 1}"/></clipPath></defs>` +
        `<image href="${E(thumbUrl)}" x="${cx - r}" y="${cy - r}" ` +
        `width="${r * 2}" height="${r * 2}" clip-path="url(#${clipId})" ` +
        `preserveAspectRatio="xMidYMid slice"/>`;
    }
    anchorPieces.push(
      `<g class="presence-orb-anchor">` +
      `<circle class="presence-orb-anchor-circle" cx="${cx}" cy="${cy}" r="${r}"/>` +
      pic +
      `<text class="presence-orb-anchor-label" x="${cx}" y="${cy + r + 10}">${nm}</text>` +
      `</g>`
    );
  });

  // Trait moons on the outer ring (max 12). Score magnitude scales dot radius.
  const moonPieces = traits.slice(0, 12).map((t, i) => {
    const total = Math.max(1, Math.min(12, traits.length));
    const ang = (360 / total) * i - 90;
    const rad = ang * Math.PI / 180;
    const cx = 160 + 142 * Math.cos(rad);
    const cy = 160 + 142 * Math.sin(rad);
    const dotR = 2 + Math.min(4, _traitVisualWeight(t) * 6);
    return `<circle class="presence-orb-moon" cx="${cx}" cy="${cy}" r="${dotR.toFixed(1)}"/>`;
  });

  // Drift heartbeat: if there's measurable drift velocity, spin the ring.
  const driftVel = Number(sv.drift_velocity || 0);
  const ringActive = driftVel > 0.01;
  const ringClass = 'presence-orb-ring' + (ringActive ? ' presence-orb-ring-active' : '');

  const emptyHint = (ops.length === 0)
    ? `<span class="orb-empty-hint orb-empty-glyph" aria-hidden="true"></span>`
    : '';

  return (
    `<div class="presence-orb-wrap" data-state="${E(state)}">` +
    `<svg class="presence-orb-svg" viewBox="0 0 320 320" ` +
    `role="img" aria-label="Presence Orb — ${E(state)}">` +
    `<circle class="presence-orb-halo" cx="160" cy="160" r="148"/>` +
    `<circle class="${ringClass}" cx="160" cy="160" r="124"/>` +
    `<g class="presence-orb-traits">${moonPieces.join('')}</g>` +
    `<g class="presence-orb-anchors">${anchorPieces.join('')}</g>` +
    `<circle class="presence-orb-core" cx="160" cy="160" r="38"/>` +
    `<text class="presence-orb-core-label" x="160" y="160">${E(idHashShort)}</text>` +
    `</svg>` +
    `<div class="presence-orb-caption">` +
    `<span class="orb-state-glyph" data-state="${E(state)}" aria-hidden="true"></span>` +
    emptyHint +
    `</div>` +
    `</div>`
  );
}

function _renderIdentityStatusRing(d) {
  const voice = d.voice || {};
  const sv = d.selfVector || {};
  const cw = d.closedWorld || {};
  const drift = String(sv.drift || '');
  const voiceLive = !!(voice.engine && voice.voice_resident);
  const driftLive = drift === 'drifting' || drift === 'rapid';
  const driftAlert = drift === 'rapid';
  const sealLive = !!cw.sealed;
  const ledgerLive = !!cw.ledger_ok;
  return (
    `<div class="identity-status-ring" aria-hidden="true">` +
    `<span class="sig voice${voiceLive ? ' live' : ''}"></span>` +
    `<span class="sig drift${driftLive ? (driftAlert ? ' alert live' : ' live') : ''}"></span>` +
    `<span class="sig seal${sealLive ? ' live' : ''}"></span>` +
    `<span class="sig ledger${ledgerLive ? ' live' : ''}"></span>` +
    `</div>`
  );
}

function _renderIdentityCanvas(d, presence) {
  return (
    `<div class="panel-visual-stack panel-visual-identity">` +
    _renderIdentityDashboard(d, presence) +
    `</div>`
  );
}

function _renderTraitStrip(traits) {
  const slice = (traits || []).slice(0, 12);
  if (!slice.length) return '';
  const segs = slice.map(t => {
    const w = _traitVisualWeight(t);
    return `<span class="trait-seg" style="--trait-weight:${w.toFixed(3)}" ` +
      `title="${_escapeHtml(t.name || '')}"></span>`;
  }).join('');
  return `<div class="omega-trait-strip" aria-hidden="true">${segs}</div>`;
}

function _renderDeepsleepCanvas(d) {
  return (
    `<div class="panel-visual-stack panel-visual-deepsleep">` +
    _renderDeepsleepDashboard(d) +
    `</div>`
  );
}
// Incremental panel updates — telemetry patches live visual canvases.
if (typeof _refreshIdentityPanel === 'function') _PANEL_LIVE_REFRESH.identity = _refreshIdentityPanel;
if (typeof _refreshDeepsleepPanel === 'function') _PANEL_LIVE_REFRESH.deepsleep = _refreshDeepsleepPanel;

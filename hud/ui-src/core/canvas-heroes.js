// Signature SVG heroes — respiratory field, star map, consolidation flow
// P3-S18: Consolidation Flow hero renderer (§9.6 signature visual).
// 4 phase nodes laid out horizontally (P1 intake → P2 reactivation
// → P3 reconciliation → P4 persistence). Each node colored per
// spec, sized 28px. Connecting lines between nodes use dashed
// strokes — animated "marching" dashes when the flow is active,
// static when idle. Completed nodes carry a check mark. Active
// node pulses (CSS keyframe). Full particle-stream animation
// between phases (§9.6 "Flowing particles") deferred — this
// slice ships static layout + state-driven node fill + pulse
// + completed check.
function _renderConsolidationFlow(data) {
  const E = _escapeHtml;
  const phases = data.phase_states || [];
  const currentPhase = data.current_phase;
  const cycleId = (data.cycle && data.cycle.id) || '—';

  if (phases.length === 0) {
    return (
      `<div class="consolidation-flow-wrap">` +
      `<div class="consolidation-flow-empty consolidation-flow-empty-glyph" ` +
      `aria-hidden="true"></div>` +
      `</div>`
    );
  }

  // Layout: 4 nodes evenly spaced across an 800-wide viewBox.
  // Node radius 22, label below, summary chip further below.
  const W = 800, H = 140;
  const nodeR = 22;
  const yMid = 56;
  const lineY = yMid;
  const positions = phases.map((_, i) => {
    const x = 80 + (i / 3) * (W - 160);
    return x;
  });

  // Connecting lines between consecutive nodes.
  const linePieces = [];
  for (let i = 0; i < positions.length - 1; i++) {
    const x1 = positions[i] + nodeR + 2;
    const x2 = positions[i + 1] - nodeR - 2;
    const aDone = phases[i].status === 'completed';
    const bDone = phases[i + 1].status === 'completed';
    const liveCls = (aDone && bDone) ? ' live' : '';
    linePieces.push(
      `<line class="consolidation-flow-line${liveCls}" ` +
      `x1="${x1.toFixed(1)}" y1="${lineY}" ` +
      `x2="${x2.toFixed(1)}" y2="${lineY}"/>`
    );
  }

  // Nodes — circle + label + summary chip + optional check.
  const nodePieces = phases.map((p, i) => {
    const x = positions[i];
    const cls = `consolidation-flow-node phase-${E(p.phase)} status-${E(p.status)}`;
    const checkPieces = (p.status === 'completed')
      ? `<text class="consolidation-flow-check" x="${x}" y="${yMid + 1}">✓</text>`
      : '';
    return (
      `<g>` +
      `<circle class="${cls}" cx="${x}" cy="${yMid}" r="${nodeR}"/>` +
      checkPieces +
      `<text class="consolidation-flow-label" x="${x}" y="${yMid + nodeR + 16}">` +
      `${E(p.name || '')}</text>` +
      `</g>`
    );
  }).join('');

  return (
    `<div class="consolidation-flow-wrap">` +
    `<svg class="consolidation-flow-svg" viewBox="0 0 ${W} ${H}" ` +
    `preserveAspectRatio="xMidYMid meet" ` +
    `role="img" aria-label="Consolidation Flow · ${E(cycleId)}">` +
    linePieces.join('') +
    nodePieces +
    `</svg>` +
    `<div class="consolidation-flow-legend">` +
    `<span class="phase-intake"><span class="dot"></span></span>` +
    `<span class="phase-reactivation"><span class="dot"></span></span>` +
    `<span class="phase-reconciliation"><span class="dot"></span></span>` +
    `<span class="phase-persistence"><span class="dot"></span></span>` +
    `</div>` +
    `</div>`
  );
}

// P3-S15: Trajectory Star Map renderer (§9.5 signature visual).
// SVG constellation of recent epochs. Stars positioned along a
// chronological rail (left=oldest, right=newest), size scales
// with coherence, color from direction tag (stable cool-blue /
// changing warm-orange / contradiction_rich red / consolidation
// gold). Current epoch pulses. Edges connect consecutive epochs
// with gradient strokes. Open contradictions surface as red
// asterisks near their epoch. Tool-synthesis queue items render
// as small comets entering from the right edge.
function _renderTrajectoryStarMap(omegaData) {
  const E = _escapeHtml;
  const epochs = omegaData.recent_epochs || [];
  const contradictions = ((omegaData.contradiction || {}).oppositions) || [];
  const synthQueue = ((omegaData.tool_synthesis || {}).queue) || [];

  if (epochs.length === 0) {
    return (
      `<div class="star-map-wrap">` +
      `<div class="star-map-empty star-map-empty-glyph" aria-hidden="true"></div>` +
      `</div>`
    );
  }

  // SVG viewBox: 800 wide × 280 tall. Margins 40 left/right + top/bottom.
  const W = 800, H = 280;
  const marginX = 40, marginY = 50;
  const innerW = W - 2 * marginX;
  const innerH = H - 2 * marginY;

  const n = epochs.length;
  const positioned = epochs.map((ep, i) => {
    const x = marginX + (i / Math.max(1, n - 1)) * innerW;
    const coh = _parseTelemetryNum(ep.coherence);
    const yFrac = 0.55 - (coh - 0.5) * 0.7;
    const jitter = ((ep.epoch * 17) % 7 - 3) * 4;
    const y = marginY + Math.max(0, Math.min(1, yFrac)) * innerH + jitter;
    const activity = (Number(ep.applied) || 0) + (Number(ep.rejected) || 0);
    const r = 3 + Math.min(7, activity * 0.5 + coh * 4);
    return { ep, x, y, r };
  });

  const edgePieces = [];
  for (let i = 0; i < positioned.length - 1; i++) {
    const a = positioned[i], b = positioned[i + 1];
    const gradId = `star-edge-${i}`;
    const colorA = _starMapDirColor(a.ep.direction);
    const colorB = _starMapDirColor(b.ep.direction);
    edgePieces.push(
      `<defs><linearGradient id="${gradId}" ` +
      `x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" ` +
      `x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" ` +
      `gradientUnits="userSpaceOnUse">` +
      `<stop offset="0%" stop-color="${colorA}" stop-opacity="0.65"/>` +
      `<stop offset="100%" stop-color="${colorB}" stop-opacity="0.65"/>` +
      `</linearGradient></defs>` +
      `<line class="star-map-edge" ` +
      `x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" ` +
      `x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" ` +
      `stroke="url(#${gradId})"/>`
    );
  }

  const starPieces = positioned.map((p, i) => {
    const isCurrent = (i === positioned.length - 1);
    const dirCls = `dir-${p.ep.direction || 'stable'}`;
    const cls = `star-map-star ${dirCls}` + (isCurrent ? ' current star-map-glow' : '');
    return (
      `<circle class="${cls}" ` +
      `cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${p.r.toFixed(1)}">` +
      `<title>epoch ${E(String(p.ep.epoch))} · ` +
      `coherence ${E(String(p.ep.coherence))} · ` +
      `applied ${E(String(p.ep.applied))} · ` +
      `rejected ${E(String(p.ep.rejected))} · ` +
      `${E(p.ep.direction)} · ${E((p.ep.timestamp || '').slice(0, 19))}</title>` +
      `</circle>`
    );
  }).join('');

  const cont = contradictions.slice(0, 4);
  const contPieces = cont.map((c, i) => {
    const last = positioned[positioned.length - 1];
    const cx = last.x - 24 - (i * 14);
    const cy = last.y - 24 - ((i % 2) * 14);
    return (
      `<text class="star-map-contradiction" x="${cx.toFixed(1)}" y="${cy.toFixed(1)}">` +
      `✱<title>${E(c.left || '')} ⇄ ${E(c.right || '')}</title>` +
      `</text>`
    );
  }).join('');

  const queueItems = synthQueue.slice(0, 3);
  const cometPieces = queueItems.map((q, i) => {
    const last = positioned[positioned.length - 1];
    const x1 = W - 10;
    const y1 = 30 + (i * 14);
    const x2 = last.x + 24;
    const y2 = last.y - 8;
    return (
      `<line class="star-map-comet" ` +
      `x1="${x1}" y1="${y1}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}">` +
      `<title>tool synthesis · ${E((q.id || 'pending').slice(0, 24))}</title>` +
      `</line>`
    );
  }).join('');

  const dirCounts = epochs.reduce((acc, e) => {
    acc[e.direction || 'stable'] = (acc[e.direction || 'stable'] || 0) + 1;
    return acc;
  }, {});
  const legendDots = ['stable', 'changing', 'contradiction_rich', 'consolidation']
    .map(d => (
      `<span class="dir-${d}" style="opacity:${Math.min(1, 0.25 + (dirCounts[d] || 0) * 0.15)}">` +
      `<span class="dot"></span></span>`
    )).join('');

  return (
    `<div class="star-map-wrap">` +
    `<div class="star-map-header">` +
    `<span class="epoch-glyph" aria-hidden="true">${E(String(epochs.length))}</span>` +
    `<span class="meta star-map-dir-glyph">${legendDots}</span>` +
    `</div>` +
    `<svg class="star-map-svg" viewBox="0 0 ${W} ${H}" ` +
    `preserveAspectRatio="xMidYMid meet" ` +
    `role="img" aria-label="Trajectory Star Map · ${E(String(epochs.length))} epochs">` +
    edgePieces.join('') +
    cometPieces +
    starPieces +
    contPieces +
    `</svg>` +
    `<div class="star-map-legend">` +
    `<span class="dir-stable"><span class="dot"></span></span>` +
    `<span class="dir-changing"><span class="dot"></span></span>` +
    `<span class="dir-contradiction_rich"><span class="dot"></span></span>` +
    `<span class="dir-consolidation"><span class="dot"></span></span>` +
    `</div>` +
    `</div>`
  );
}

// Color lookup matching the .star-map-star.dir-* CSS classes.
function _starMapDirColor(direction) {
  switch (direction) {
    case 'consolidation': return '#d4af52';
    case 'contradiction_rich': return '#b85b4a';
    case 'changing': return '#d9893f';
    default: return '#5b8fc7';
  }
}
// P3-S09: Respiratory Field renderer (§9.4 signature visual).
// SVG-based hero: breathing pressure dial at the center of an
// orbital field of curiosity particles, autonomic sparkline
// strip beneath, suppress-histogram stripe at top, pause overlay
// when the substrate is paused. Returns an HTML string ready to
// be set as innerHTML on the wrapper.
function _renderRespiratoryField(a) {
  const E = _escapeHtml;
  const pressure = a.pressure || {};
  const cadence = a.cadence || {};
  const curiosity = a.curiosity || [];
  const autonomic = a.autonomic || {};
  const histogram = a.suppress_histogram || {};
  const pauseState = a.pause_state || { paused: false, owners: [] };

  // Pressure score → arc length on the dial.
  const score = Math.max(0, Math.min(1, Number(pressure.score) || 0));
  const band = score >= 0.66 ? 'hi' : score >= 0.33 ? 'med' : 'lo';
  // Dial arc geometry — half-circle gauge, top-half open.
  const arcR = 76;
  const cx = 200;
  const cy = 150;
  const circumference = Math.PI * arcR;          // half-circumference (semicircle path)
  const dashOffset = circumference * (1 - score);
  // Breathing duration from cadence. Parse "5s" → 5, fall back to 4s.
  const intervalStr = String(cadence.currentInterval || '').trim();
  const intervalM = intervalStr.match(/^(\d+(?:\.\d+)?)\s*s$/);
  const breathSec = intervalM ? Math.max(2, Math.min(20, parseFloat(intervalM[1]))) : 4;

  // Suppress histogram — stack of weighted segments.
  const total = Math.max(1, Number(histogram.total) || 0);
  const segOrder = ['proceeded', 'pressure_loop_paused',
    'presence_gate_held', 'autonomy_paused',
    'omega_lock_held', 'below_threshold', 'other'];
  const histStrip = segOrder.map(key => {
    const n = Number(histogram[key]) || 0;
    if (n <= 0) return '';
    const pct = (n / total) * 100;
    return `<span class="seg-${E(key)}" style="flex-basis:${pct.toFixed(2)}%"
            title="${E(key)} · ${n}"></span>`;
  }).join('');

  // Curiosity particles: up to 8, each on its own rotation orbit
  // with a period varied by urgency (urgent = fast). Radius
  // scales inward as pressure rises.
  const baseR = 180 - Math.round(score * 60);   // 180 → 120 as pressure climbs
  const particles = curiosity.slice(0, 8).map((c, i) => {
    const urg = Math.max(0, Math.min(1, Number(c.urgency) || 0));
    // Period: 16s..56s; high urgency = faster orbit.
    const periodSec = Math.round(56 - urg * 40);
    const direction = (i % 2 === 0) ? '' : 'reverse';
    const angle = (360 / 8) * i;
    // Initial offset via rotate transform on a static <g> wrapping
    // the orbit. Particle sits at (cx + r, cy) by default.
    const dotR = 3 + Math.round(urg * 3);
    return (
      `<g class="rf-particle-orbit" ` +
      `style="--orbit-period:${periodSec}s; animation-direction:${direction};" ` +
      `transform="rotate(${angle.toFixed(1)} ${cx} ${cy})">` +
      `<circle class="rf-particle" cx="${cx + baseR}" cy="${cy}" r="${dotR}">` +
      `<title>${E(c.id || '?')} · urgency ${E(c.urgency || '0')}</title>` +
      `</circle>` +
      `</g>`
    );
  }).join('');

  // Autonomic sparklines.
  const sparkline = (key) => {
    const samples = autonomic[key] || [];
    if (samples.length < 2) {
      return `<span class="respiratory-sparkline-empty">no samples yet</span>`;
    }
    const values = samples.map(s => Number(s.v) || 0);
    const minV = Math.min.apply(null, values);
    const maxV = Math.max.apply(null, values);
    const rng = Math.max(0.0001, maxV - minV);
    const W = 200;
    const H = 22;
    const stepX = W / Math.max(1, values.length - 1);
    const pts = values.map((v, i) => {
      const x = (i * stepX).toFixed(2);
      const y = (H - ((v - minV) / rng) * (H - 2) - 1).toFixed(2);
      return `${x},${y}`;
    }).join(' ');
    return (
      `<svg class="respiratory-sparkline ${E(key)}" ` +
      `viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" ` +
      `width="100%" height="22" aria-hidden="true">` +
      `<polyline points="${pts}"/>` +
      `</svg>`
    );
  };

  return (
    `<div class="respiratory-field-wrap" ` +
    `data-paused="${pauseState.paused ? 'true' : 'false'}" ` +
    `style="--rf-breathe-duration:${breathSec}s">` +
    `<div class="respiratory-histogram" ` +
    `title="pressure tick suppress reasons · last hour (${total} ticks)">` +
    histStrip +
    `</div>` +
    `<div class="respiratory-field-body">` +
    `<svg class="respiratory-field-svg" viewBox="0 0 400 300" ` +
    `role="img" aria-label="Respiratory Field — pressure ${E(score.toFixed(2))}">` +
    `<g class="rf-particles">${particles}</g>` +
    `<circle class="respiratory-dial-halo" cx="${cx}" cy="${cy}" r="100"/>` +
    `<g class="respiratory-dial-core">` +
    `<circle class="respiratory-dial-ring" cx="${cx}" cy="${cy}" r="${arcR + 8}"/>` +
    `<circle class="respiratory-dial-ring" cx="${cx}" cy="${cy}" r="${arcR + 18}"/>` +
    `<path class="respiratory-dial-arc" data-band="${E(band)}" ` +
    `d="M ${cx - arcR} ${cy} A ${arcR} ${arcR} 0 0 1 ${cx + arcR} ${cy}" ` +
    `stroke-dasharray="${circumference.toFixed(2)}" ` +
    `stroke-dashoffset="${dashOffset.toFixed(2)}"/>` +
    `<text class="respiratory-dial-value" x="${cx}" y="${cy - 4}">` +
    `${E(pressure.scoreLabel || score.toFixed(2))}` +
    `</text>` +
    `</g>` +
    `</svg>` +
    `</div>` +
    `<div class="respiratory-autonomic-icons">` +
    `<div class="autonomic-cell"><span class="glyph dopamine"></span>${sparkline('dopamine')}</div>` +
    `<div class="autonomic-cell"><span class="glyph plasticity"></span>${sparkline('plasticity')}</div>` +
    `<div class="autonomic-cell"><span class="glyph strength"></span>${sparkline('strength')}</div>` +
    `<div class="autonomic-cell"><span class="glyph stress"></span>${sparkline('stress')}</div>` +
    `</div>` +
    `<div class="respiratory-pause-overlay" aria-hidden="true">` +
    `<span class="pause-glyph"></span>` +
    `</div>` +
    `</div>`
  );
}

// Shared canvas / telemetry dashboard renderers (hud/ui-src/core/canvas.js)
// Depends on: _escapeHtml, _statusClassForTag (monolith shell)

function _simpleHash(s) {
  let h = 0;
  const str = String(s == null ? '' : s);
  for (let i = 0; i < str.length; i++) {
    h = ((h << 5) - h + str.charCodeAt(i)) | 0;
  }
  return String(h);
}
function _ensurePanelRoot(b, panelId) {
  let root = b.querySelector(`[data-panel-root="${panelId}"]`);
  if (!root) {
    root = document.createElement('div');
    root.className = 'panel-mounted-root panel-visual-root';
    root.dataset.panelRoot = panelId;
    b.appendChild(root);
  }
  return root;
}
// Map /api/panel/* and Telemetry.snapshot() shapes onto visual renderers.
function _parseTelemetryNum(v) {
  const n = parseFloat(v);
  return Number.isFinite(n) ? n : 0;
}
function _traitVisualWeight(t) {
  if (!t || typeof t !== 'object') return 0.2;
  if (typeof t.score === 'number') return Math.max(0.05, Math.min(1, Math.abs(t.score)));
  if (typeof t._raw_current === 'number') {
    return Math.max(0.05, Math.min(1, Math.abs(t._raw_current)));
  }
  if (t.value === 'committed') return 0.85;
  if (t.value === 'finalized_uncommitted') return 0.65;
  if (t.value === 'proposed') return 0.45;
  return 0.25;
}
// Telemetry dashboard component library — shared across cognition panels.
function _tdClamp01(v) { return Math.max(0, Math.min(1, Number(v) || 0)); }
function _tdPct(v) { return Math.round(_tdClamp01(v) * 100); }
const _tdMetricPrev = {};
function _tdDeltaAttr(panel, key, value) {
  if (!panel || !key) return '';
  const num = Number(value);
  if (!Number.isFinite(num)) return '';
  const fullKey = String(panel) + '.' + String(key);
  const prev = _tdMetricPrev[fullKey];
  let delta = 'same';
  if (prev !== undefined && Math.abs(num - prev) > 0.0005) {
    delta = num > prev ? 'up' : 'down';
  }
  _tdMetricPrev[fullKey] = num;
  return ` data-td-delta="${delta}"`;
}
function _tdClearMetricDeltas(panel) {
  if (!panel) return;
  const prefix = String(panel) + '.';
  for (const k of Object.keys(_tdMetricPrev)) {
    if (k.startsWith(prefix)) delete _tdMetricPrev[k];
  }
}
function _tdStatusDot(tone, label) {
  const E = _escapeHtml;
  return `<span class="td-status td-status-${E(tone || 'idle')}">` +
    `<span class="td-dot"></span>${E(label || '')}</span>`;
}
function _tdPanelRail(tone, label, sublabel) {
  const E = _escapeHtml;
  return `<div class="td-panel-rail td-span-12">` +
    _tdStatusDot(tone, label) +
    (sublabel ? `<span class="td-panel-rail-sub">${E(sublabel)}</span>` : '') +
    `</div>`;
}
function _tdHeroShell(title, statusHtml, body, spanCls) {
  const E = _escapeHtml;
  return `<div class="td-hero-shell ${spanCls || 'td-span-12'}">` +
    `<div class="td-hero-inner">` +
    `<header class="td-hero-head">` +
    `<span class="td-hero-title">${E(title || '')}</span>${statusHtml || ''}</header>` +
    `<div class="td-hero-body">${body}</div></div></div>`;
}
function _tdDefaultBarDiagnostics(row) {
  const pct = _tdClamp01(row.pct);
  const tag = pct >= 0.66 ? 'warn' : pct >= 0.33 ? 'ok' : 'idle';
  const detail = String(row.detail || row.value || '—');
  return [
    { k: 'channel', v: String(row.label || '—'), t: '' },
    { k: 'load', v: `${_tdPct(pct)}%`, t: tag },
    { k: 'detail', v: detail, t: '' },
  ];
}
function _tdBarDiagnosticsHtml(rows) {
  const E = _escapeHtml;
  return (rows || []).map(d =>
    `<div class="panel-row td-bar-diag-row">` +
    `<span class="k">${E(d.k || '—')}</span>` +
    `<span class="v">${E(d.v || '—')}</span>` +
    `<span class="t ${_statusClassForTag(d.t || '')}">${E(d.t || '')}</span></div>`
  ).join('');
}
function _bindPanelChromeOnce() {
  if (typeof panelBody === 'undefined' || !panelBody || panelBody.dataset.tdChromeBound) return;
  panelBody.dataset.tdChromeBound = '1';
  panelBody.addEventListener('click', (ev) => {
    const tabBtn = ev.target.closest('.td-tab');
    if (tabBtn && panelBody.contains(tabBtn)) {
      const tabsRoot = tabBtn.closest('[data-td-tabs]');
      if (!tabsRoot) return;
      const tabId = tabBtn.getAttribute('data-td-tab');
      tabsRoot.querySelectorAll('.td-tab').forEach(btn => {
        const active = btn === tabBtn;
        btn.classList.toggle('is-active', active);
        btn.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      tabsRoot.querySelectorAll('[data-td-tab-panel]').forEach(panel => {
        const active = panel.getAttribute('data-td-tab-panel') === tabId;
        panel.classList.toggle('is-active', active);
      });
      return;
    }
    const barHit = ev.target.closest('.td-bar-hit');
    if (barHit && panelBody.contains(barHit)) {
      const expanded = barHit.getAttribute('aria-expanded') === 'true';
      const next = !expanded;
      barHit.setAttribute('aria-expanded', next ? 'true' : 'false');
      const drill = barHit.parentElement
        && barHit.parentElement.querySelector('.td-bar-drill');
      if (drill) drill.hidden = !next;
    }
  });
}
_bindPanelChromeOnce();
function _tdCard(title, statusHtml, body, spanCls) {
  const E = _escapeHtml;
  return `<div class="td-shell ${spanCls || 'td-span-6'}">` +
    `<article class="td-card">` +
    `<header class="td-card-head"><span class="td-card-title">${E(title)}</span>` +
    `${statusHtml || ''}</header><div class="td-card-body">${body}</div></article></div>`;
}
function _tdDonut(pct, label, sublabel, footers, panel, metricKey) {
  const E = _escapeHtml;
  const p = _tdClamp01(pct);
  const r = 54, cx = 70, cy = 70, circ = 2 * Math.PI * r;
  const offset = circ * (1 - p);
  const pctDelta = _tdDeltaAttr(panel, metricKey, p);
  const footer = (footers || []).map(f =>
    `<div class="td-metric"><span class="td-metric-k">${E(f.k)}</span>` +
    `<span class="td-metric-v td-tone-${E(f.tone || 'neutral')}">${E(f.v)}</span></div>`
  ).join('');
  return `<div class="td-donut-wrap">` +
    `<svg viewBox="0 0 140 140" class="td-donut-svg" role="img" aria-label="${E(label)} ${_tdPct(p)}%">` +
    `<circle cx="${cx}" cy="${cy}" r="${r}" class="td-donut-track"/>` +
    `<circle cx="${cx}" cy="${cy}" r="${r}" class="td-donut-arc" ` +
    `stroke-dasharray="${circ.toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"/>` +
    `<text x="${cx}" y="${cy - 4}" class="td-donut-pct"${pctDelta}>${_tdPct(p)}%</text>` +
    `<text x="${cx}" y="${cy + 14}" class="td-donut-label">${E(label)}</text>` +
    `</svg>` +
    (sublabel ? `<div class="td-donut-sub">${E(sublabel)}</div>` : '') +
    `<div class="td-metric-row">${footer}</div></div>`;
}
function _tdWaveform(series, live, subtitle) {
  const pts = (series && series.length) ? series : Array(28).fill(0.04);
  const W = 360, H = 72, pad = 4;
  const step = (W - pad * 2) / Math.max(1, pts.length - 1);
  const path = pts.map((v, i) => {
    const x = pad + i * step;
    const y = H - pad - _tdClamp01(v) * (H - pad * 2);
    return `${i ? 'L' : 'M'}${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(' ');
  return `<div class="td-wave-wrap">` +
    _tdStatusDot(live ? 'live' : 'idle', live ? 'LIVE' : 'IDLE') +
    `<svg viewBox="0 0 ${W} ${H}" class="td-wave-svg"><path d="${path}" class="td-wave-path"/></svg>` +
    (subtitle ? `<div class="td-wave-sub">${_escapeHtml(subtitle)}</div>` : '') +
    `</div>`;
}
function _tdBarList(rows, panel, keyPrefix) {
  const E = _escapeHtml;
  if (!rows || !rows.length) return `<div class="td-empty">no channel data yet</div>`;
  return `<div class="td-bars" data-td-bars="${E(panel || '')}">${rows.map((row, i) => {
    const pct = _tdPct(row.pct);
    const barKey = keyPrefix ? `${keyPrefix}:${row.label || i}` : '';
    const barDelta = _tdDeltaAttr(panel, barKey, _tdClamp01(row.pct));
    const detail = row.detail || row.value || '';
    const diag = row.diagnostics || _tdDefaultBarDiagnostics(
      Object.assign({}, row, { detail })
    );
    return `<div class="td-bar-group">` +
      `<button type="button" class="td-bar-hit" aria-expanded="false" ` +
      `aria-label="Inspect ${E(row.label || 'channel')}">` +
      `<span class="td-bar-label">${E(row.label || '—')}</span>` +
      `<span class="td-bar-detail">${E(detail)}</span>` +
      `<div class="td-bar-track"><div class="td-bar-fill"${barDelta} ` +
      `style="width:${pct}%"></div></div>` +
      `</button>` +
      `<div class="td-bar-drill" hidden>${_tdBarDiagnosticsHtml(diag)}</div>` +
      `</div>`;
  }).join('')}</div>`;
}

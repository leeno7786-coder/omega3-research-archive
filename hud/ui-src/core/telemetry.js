// Telemetry singleton + panel re-render subscriber (hud/ui-src/core/telemetry.js)
// Depends on: PANELS, openPanel, _currentPanelId, panelBody,
// _tdClearMetricDeltas (monolith shell + canvas module)

const Telemetry = (() => {
  // All slots start empty — every value is filled by /api/telemetry/
  // snapshot polls. The `system` slot keeps real pre-init sentinels
  // ('fallback' / 'unknown' / 'pre-init') because ChatBridge / Voice /
  // Vision init read `system.bridge.state` etc. without a `|| {}`
  // guard. Panel renderers all use `Telemetry.snapshot().<slot> || {}`
  // so empty objects/arrays render as "—" / "no X yet" placeholders.
  const state = {
    cycle: {},
    layers: [],
    brain: {},
    memory: {},
    omega: {},
    autoloop: {},
    deepsleep: {},
    identity: {},
    system: {
      bridge: { state: 'fallback', uri: null, last_event: 'boot' },
      voice: { state: 'unknown', supported: null, last_event: 'pre-init' },
      vision: { state: 'unknown', supported: null, last_event: 'pre-init' },
    },
  };

  const subs = new Set();
  // deep-merge: objects merge by key; arrays + primitives replace.
  // Arrays-as-replacement is intentional — when "approvals" changes,
  // the whole list changes, you don't append-merge.
  const deepMerge = (dst, src) => {
    for (const k of Object.keys(src)) {
      const sv = src[k];
      if (sv && typeof sv === 'object' && !Array.isArray(sv)
        && dst[k] && typeof dst[k] === 'object' && !Array.isArray(dst[k])) {
        deepMerge(dst[k], sv);
      } else {
        dst[k] = sv;
      }
    }
  };
  return {
    snapshot: () => state,
    subscribe: (fn) => { subs.add(fn); return () => subs.delete(fn); },
    update: (patch) => {
      deepMerge(state, patch);
      for (const fn of subs) { try { fn(state); } catch (e) { /* swallow per-sub errors */ } }
    },
  };
})();

// Re-render the open panel on Telemetry updates — but ONLY when the
// relevant subtree actually changed. The naive "rebuild on every
// update" approach destroys 90+ approval cards every 2 s, kills
// click handlers, loses scroll position, and on heavy panels can
// take a Chromium tab down. We map each panel to its subtree, hash
// the subtree, and skip the rebuild if the hash matches the last
// render.
const _PANEL_SUBTREE = {
  meta: s => s.meta,
  global: s => s.global,
  settings: () => null,
};
let _lastPanelHash = '';
let _lastRenderAt = 0;
let _renderScheduled = false;
const _PANEL_LIVE_REFRESH = {};  // populated after panel paint helpers
const _MIN_RENDER_INTERVAL_MS = 1500;   // protect click handlers from
//   being torn down mid-click

// Interactive-pause: suppress panel rebuild when the operator's
// pointer is over the panel OR they've clicked anything in it within
// the last 8s. Without this, panels with actionable rows (settings
// actionable settings rows rebuild every 1.5s on telemetry
// hash churn — the rebuild lands between mousedown and click and the
// click never reaches a live DOM element. Operator can't approve.
let _panelHover = false;
let _panelLastClickAt = 0;
const _PANEL_INTERACTION_HOLD_MS = 8000;
if (typeof panelBody !== 'undefined' && panelBody) {
  panelBody.addEventListener('mouseenter', () => { _panelHover = true; });
  panelBody.addEventListener('mouseleave', () => { _panelHover = false; });
  // Capture-phase so we mark the click before any handler that may
  // mutate state. Use 'pointerdown' so the hold engages the instant
  // the operator presses, not after the up event.
  panelBody.addEventListener('pointerdown',
    () => { _panelLastClickAt = Date.now(); },
    true);
}
function _panelInteractionActive() {
  if (_panelHover) return true;
  if (_panelLastClickAt && Date.now() - _panelLastClickAt < _PANEL_INTERACTION_HOLD_MS) return true;
  return false;
}

function _hashFor(panelId) {
  const slice = _PANEL_SUBTREE[panelId] || (() => null);
  try { return JSON.stringify(slice(Telemetry.snapshot())); }
  catch (_) { return ''; }
}
function _scheduleRender() {
  if (_renderScheduled) return;
  _renderScheduled = true;
  requestAnimationFrame(() => {
    _renderScheduled = false;
    if (!_currentPanelId) return;
    const p = PANELS[_currentPanelId];
    if (!p || !p.body) return;
    if (_panelInteractionActive()) return;
    const now = Date.now();
    if (now - _lastRenderAt < _MIN_RENDER_INTERVAL_MS) return;
    const hash = _hashFor(_currentPanelId);
    if (hash === _lastPanelHash) return;
    _lastPanelHash = hash;
    _lastRenderAt = now;
    const liveRefresh = _PANEL_LIVE_REFRESH[_currentPanelId];
    if (liveRefresh) {
      try { liveRefresh(); } catch (e) {
        console.error('[panel live refresh]', _currentPanelId, e);
      }
      return;
    }
    if (_currentPanelId === 'settings' && window._memoryGraphDestroy) {
      window._memoryGraphDestroy();
    }
    panelBody.innerHTML = '';
    try { p.body(panelBody); }
    catch (e) {
      console.error('[panel render]', _currentPanelId, e);
      panelBody.innerHTML = `<div class="panel-row"><span class="k">!</span>
        <span class="v">render failed: ${(e && e.message) || e}</span>
        <span class="t">FAIL</span></div>`;
    }
  });
}
// Subscribe + pre-warm the hash whenever a panel opens so the first
// poll after the open doesn't immediately rebuild and tear down the
// just-built click handlers.
Telemetry.subscribe(_scheduleRender);
const _origOpenPanelHash = openPanel;
window.openPanel = function (id) {
  const r = _origOpenPanelHash(id);
  if (typeof _tdClearMetricDeltas === 'function') _tdClearMetricDeltas(id);
  // Pre-warm hash + render timestamp so subsequent _scheduleRender
  // calls within the throttle window skip the rebuild. This protects
  // click handlers bound during the manual render in openPanel.
  _lastPanelHash = _hashFor(id);
  _lastRenderAt = Date.now();
  if (typeof _stopDeepsleepPoll === 'function') _stopDeepsleepPoll();
  return r;
};
// Same for the script-scope binding so design code that calls bare
// `openPanel(id)` also goes through the wrapper.
openPanel = window.openPanel;

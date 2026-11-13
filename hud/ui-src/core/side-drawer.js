// HUD slide-out rails + left cognition visuals (hud/ui-src/core/side-drawer.js)
// Depends on: DOM #side-drawer, #side-drawer-toggle

const SIDE_DRAWER_STORAGE_KEY = 'omega.hud.sideDrawer';
const COGNITION_SCOPE_SIZE = 12;
const COGDASH_HEATMAP_SIZE = 56;
const OMEGA_MIND_LIMIT = 9;
let _lastCognitionLiveTick = null;
let _cognitionTelemetryUnsubscribe = null;
let _omegaMindSeq = 0;
let _omegaMindState = {
  episode: {}, cascade: {}, ignition: {}, tool: {}, autonomy: {}, idleLearning: {}, action: '',
};
const _cognitionScopes = {
  winner: Array(COGNITION_SCOPE_SIZE).fill(0.08),
  path: Array(COGNITION_SCOPE_SIZE).fill(0.06),
  motion: Array(COGNITION_SCOPE_SIZE).fill(0.04),
};

function _sideDrawerStorageKey(side) {
  return SIDE_DRAWER_STORAGE_KEY + '.' + (side || 'left');
}

function _sideDrawerStoredState() {
  try {
    return window.localStorage && localStorage.getItem(SIDE_DRAWER_STORAGE_KEY);
  } catch (_e) {
    return null;
  }
}

function _sideDrawerStoredSideState(side) {
  try {
    if (!window.localStorage) return null;
    return localStorage.getItem(_sideDrawerStorageKey(side));
  } catch (_e) {
    return null;
  }
}

function _sideDrawerSaveState(side, open) {
  try {
    if (window.localStorage) {
      localStorage.setItem(_sideDrawerStorageKey(side), open ? 'open' : 'closed');
    }
  } catch (_e) {
    // Storage can be unavailable in hardened browser contexts.
  }
}

function _sideDrawersAreOverlay() {
  return Boolean(window.matchMedia && window.matchMedia('(max-width: 1180px)').matches);
}

function _closeOppositeDrawer(side) {
  if (!_sideDrawersAreOverlay()) return;
  const opposite = side === 'right' ? 'left' : 'right';
  const drawer = document.querySelector('.side-drawer[data-side-drawer-side="' + opposite + '"]');
  if (!drawer || drawer.dataset.sideDrawer !== 'open') return;
  if (typeof drawer._omegaSetOpen === 'function') drawer._omegaSetOpen(false, true);
}

function _cognitionClamp(value, fallback) {
  const fallbackValue = (typeof fallback === 'number') ? fallback : 0;
  const number = Number(value);
  if (!Number.isFinite(number)) return Math.max(0, Math.min(1, fallbackValue));
  return Math.max(0, Math.min(1, number));
}

function _cognitionObject(value) {
  return (value && typeof value === 'object' && !Array.isArray(value)) ? value : {};
}

function _cognitionFirstDefined(source, keys) {
  for (let i = 0; i < keys.length; i++) {
    const value = source[keys[i]];
    if (value != null && value !== '') return value;
  }
  return undefined;
}

function _cognitionFriendlyLabel(value) {
  const text = String(value || '').trim().toLowerCase().replace(/[-\s]+/g, '_');
  if (!text) return 'signal';
  if (text.indexOf('operator') !== -1) return 'operator';
  if (text.indexOf('learn') !== -1) return 'learning';
  if (text.indexOf('curiosity') !== -1 || text.indexOf('question') !== -1 || text.indexOf('clarif') !== -1) return 'inquiry';
  if (text.indexOf('contrad') !== -1 || text.indexOf('conflict') !== -1) return 'contrast';
  if (text.indexOf('verify') !== -1 || text.indexOf('evidence') !== -1) return 'verify';
  if (text.indexOf('memory') !== -1 || text.indexOf('taxonomy') !== -1 || text.indexOf('recall') !== -1) return 'memory';
  if (text.indexOf('response') !== -1 || text.indexOf('reply') !== -1 || text.indexOf('chat') !== -1) return 'reply';
  if (text.indexOf('rollback') !== -1 || text.indexOf('restore') !== -1) return 'restore';
  if (text.indexOf('skill') !== -1 || text.indexOf('build') !== -1 || text.indexOf('tool') !== -1 || text.indexOf('execute') !== -1) return 'act';
  if (text.indexOf('proactive') !== -1 || text.indexOf('outreach') !== -1) return 'outreach';
  return 'signal';
}

function _cognitionMotionLabel(action, intent) {
  const text = (String(action || '') + ' ' + String(intent || '')).trim().toUpperCase();
  if (!text) return 'pending';
  if (text.indexOf('RESPONSE') !== -1 || text.indexOf('RESPOND') !== -1 || text.indexOf('REPLY') !== -1) return 'reply';
  if (text.indexOf('USE_SKILL') !== -1 || text.indexOf('APPLY') !== -1 || text.indexOf('EXECUTE') !== -1 || text.indexOf('BUILD') !== -1) return 'act';
  if (text.indexOf('LEARN') !== -1) return 'learn';
  if (text.indexOf('ASK') !== -1 || text.indexOf('CLARIFY') !== -1) return 'ask';
  if (text.indexOf('IDLE') !== -1 || text.indexOf('NOOP') !== -1 || text.indexOf('HOLD') !== -1) return 'hold';
  return 'select';
}

function _cognitionLivePathLevel(source, trajectory) {
  return _cognitionClamp(
    _cognitionFirstDefined(trajectory, ['progress', 'path_progress', 'confidence'])
      ?? _cognitionFirstDefined(source, ['path_level', 'progress', 'confidence', 'coherence']),
    0.58
  );
}

function _mergeCognitionLiveTick(tick) {
  const previous = _cognitionObject(_lastCognitionLiveTick);
  const next = _cognitionObject(tick);
  const previousVisuals = _cognitionObject(previous.cognition_visuals);
  const nextVisuals = _cognitionObject(next.cognition_visuals);
  const mergedVisuals = {};

  ['winner', 'path', 'motion'].forEach((key) => {
    const priorPart = _cognitionObject(previousVisuals[key]);
    const nextPart = _cognitionObject(nextVisuals[key]);
    const mergedPart = _mergeCognitionVisualPart(key, priorPart, nextPart);
    if (Object.keys(mergedPart).length) mergedVisuals[key] = mergedPart;
  });

  const merged = Object.assign({}, previous, next);
  if (Object.keys(mergedVisuals).length) merged.cognition_visuals = mergedVisuals;
  _lastCognitionLiveTick = merged;
  return merged;
}

function _cognitionVisualPartIsLive(key, part) {
  const source = _cognitionObject(part);
  const label = String(source.label || '').trim().toLowerCase();
  const state = String(source.state || '').trim().toLowerCase();
  if (!Object.keys(source).length) return false;
  if (state === 'active' || state === 'selected') return true;
  if (key === 'winner') return Boolean(label && label !== 'signal' && label !== 'pending');
  if (key === 'path') return Boolean(label && label !== 'pending');
  if (key === 'motion') return Boolean(label && label !== 'pending' && label !== 'hold');
  return false;
}

function _mergeCognitionVisualPart(key, priorPart, nextPart) {
  const prior = _cognitionObject(priorPart);
  const next = _cognitionObject(nextPart);
  if (!Object.keys(next).length) return prior;
  if (!_cognitionVisualPartIsLive(key, next) && _cognitionVisualPartIsLive(key, prior)) {
    return prior;
  }
  return Object.assign({}, prior, next);
}

function _cognitionVisualsFromLiveEvent(event) {
  const source = _cognitionObject(event);
  if (!Object.keys(source).length) return null;

  const kind = String(source.kind || '').trim().toLowerCase();
  const loop = String(source.loop || source.system || '').trim().toLowerCase();
  const phase = String(source.phase || source.op || '').trim().toLowerCase();
  const status = String(source.status || '').trim().toLowerCase();
  const signalName = String(source.name || '').trim().toLowerCase();
  const actionSource = Object.assign(
    {},
    _cognitionObject(source.chosen_action),
    _cognitionObject(source.intent_decision),
    _cognitionObject(source.execution_action_pack)
  );
  const trajectory = Object.assign(
    {},
    _cognitionObject(source.trajectory_snapshot),
    _cognitionObject(source.trajectory_update),
    _cognitionObject(source.trajectory_influence)
  );
  const aggregate = _cognitionFirstDefined(
    source,
    ['aggregate_pressure', 'pressure_aggregate', 'aggregate', 'pressure']
  );
  let winner = _cognitionFirstDefined(source, ['opened_by', 'dominant', 'winner', 'driver', 'reason']);
  if (!winner && (kind === 'memory' || loop === 'memory')) winner = 'memory';

  const visuals = {};
  const inactive = status === 'skip' || status === 'fail' || status === 'failed' || status === 'error';
  const hasWinner = winner != null || aggregate != null;
  if (hasWinner) {
    visuals.winner = {
      label: _cognitionFriendlyLabel(winner || loop || phase),
      level: _cognitionClamp(aggregate, 0.64),
      state: inactive ? 'pending' : 'active',
    };
  }

  const hasTrajectory = Boolean(
    Object.keys(trajectory).length
    || source.trajectory_diff
    || source.target_domain
    || source.target_capability
    || phase.indexOf('trajectory') !== -1
    || phase.indexOf('omega_outcome') !== -1
  );
  if (hasTrajectory) {
    visuals.path = {
      label: inactive ? 'pending' : 'active',
      level: _cognitionLivePathLevel(source, trajectory),
      state: inactive ? 'pending' : 'active',
    };
  }

  const action = (
    _cognitionFirstDefined(actionSource, ['action', 'intent', 'decision', 'type'])
    ?? _cognitionFirstDefined(source, ['action', 'intent', 'omega_intent', 'trajectory_decision', 'prompt_type', 'tool', 'decision'])
    ?? (kind === 'signal' ? signalName : '')
  );
  const actionHint = String(action || phase || '').toLowerCase();
  const hasAction = Boolean(
    action
    || phase.indexOf('intent') !== -1
    || phase.indexOf('action') !== -1
    || phase.indexOf('handoff') !== -1
    || actionHint.indexOf('reply') !== -1
  );
  if (hasAction) {
    visuals.motion = {
      label: _cognitionMotionLabel(action || phase, source.intent),
      state: inactive ? 'pending' : 'selected',
    };
  }

  if (!Object.keys(visuals).length) return null;
  const tick = { cognition_visuals: visuals };
  if (winner != null) tick.opened_by = winner;
  if (aggregate != null) tick.aggregate_pressure = aggregate;
  if (hasTrajectory) tick.trajectory_snapshot = trajectory;
  if (action) tick.chosen_action = { action: action };
  return tick;
}

function _cognitionVisualStateFromTick(tick) {
  const source = (tick && typeof tick === 'object') ? tick : {};
  const visuals = (source.cognition_visuals && typeof source.cognition_visuals === 'object')
    ? source.cognition_visuals
    : {};
  const winner = (visuals.winner && typeof visuals.winner === 'object') ? visuals.winner : {};
  const path = (visuals.path && typeof visuals.path === 'object') ? visuals.path : {};
  const motion = (visuals.motion && typeof visuals.motion === 'object') ? visuals.motion : {};
  const trajectory = (source.trajectory_snapshot && typeof source.trajectory_snapshot === 'object')
    ? source.trajectory_snapshot
    : {};
  const pathProgress = trajectory.progress != null
    ? trajectory.progress
    : (trajectory.path_progress != null ? trajectory.path_progress : trajectory.confidence);
  const motionLabel = motion.label || _cognitionMotionLabel(
    (source.chosen_action && source.chosen_action.action) || '',
    (source.chosen_action && source.chosen_action.intent) || ''
  );
  const winnerLabel = winner.label || _cognitionFriendlyLabel(source.opened_by || source.dominant || '');
  const pathActive = Boolean(path.label || Object.keys(trajectory).length);
  return {
    winner: {
      label: String(winnerLabel || 'signal'),
      level: _cognitionClamp(winner.level != null ? winner.level : source.pressure_aggregate, 0),
      state: winner.state || (winnerLabel ? 'active' : 'pending'),
    },
    path: {
      label: String(path.label || (pathActive ? 'active' : 'pending')),
      level: _cognitionClamp(path.level != null ? path.level : pathProgress, pathActive ? 0.5 : 0),
      state: path.state || (pathActive ? 'active' : 'pending'),
    },
    motion: {
      label: String(motionLabel || 'pending'),
      state: motion.state || (motionLabel && motionLabel !== 'pending' ? 'selected' : 'pending'),
    },
  };
}

function _hasCognitionSignal(tick) {
  const source = _cognitionObject(tick);
  if (!Object.keys(source).length) return false;
  const visuals = _cognitionObject(source.cognition_visuals);
  return Boolean(
    Object.keys(visuals).length
    || source.opened_by != null
    || source.dominant != null
    || source.winner != null
    || source.driver != null
    || source.aggregate_pressure != null
    || source.pressure_aggregate != null
    || source.chosen_action != null
    || source.trajectory_snapshot != null
  );
}

function _cognitionTickFromState(state) {
  const snapshot = state && state.system && state.system.v2 ? state.system.v2 : {};
  const pollTick = snapshot.pressure_last_tick || snapshot.cuedesk_last_tick || null;
  if (_hasCognitionSignal(pollTick)) return pollTick;
  if (_hasCognitionSignal(state)) return state;
  if (_hasCognitionSignal(_lastCognitionLiveTick)) return _lastCognitionLiveTick;
  return pollTick || null;
}

function _setCognitionText(root, key, value) {
  const node = root.querySelector('[data-cognition-value="' + key + '"]');
  if (node && node.textContent !== value) node.textContent = value;
}

function _pushCognitionScope(key, value) {
  const scope = _cognitionScopes[key];
  if (!scope) return [];
  scope.push(_cognitionClamp(value, 0));
  while (scope.length > COGNITION_SCOPE_SIZE) scope.shift();
  return scope;
}

function _renderCognitionScope(root, key, value) {
  const bars = root.querySelectorAll('[data-cognition-scope="' + key + '"] .cognition-scope-bar');
  if (!bars.length) return;
  const values = _pushCognitionScope(key, value);
  bars.forEach((bar, index) => {
    const v = values[index] != null ? values[index] : 0;
    bar.style.setProperty('--h', v.toFixed(3));
    bar.style.setProperty('--i', String(index));
    bar.dataset.hot = String(v >= 0.64 || index === bars.length - 1);
  });
}

function _cogdashLiveSnapshot() {
  try {
    if (typeof Telemetry !== 'undefined' && Telemetry && typeof Telemetry.snapshot === 'function') {
      return Telemetry.snapshot() || {};
    }
  } catch (_e) {
    // Telemetry is optional during static tests and early boot.
  }
  return {};
}

function _cogdashMergeBranch(live, patch, key) {
  const base = _cognitionObject(live[key]);
  const next = _cognitionObject(patch[key]);
  const merged = Object.assign({}, base, next);
  if (base.dashboard || next.dashboard) {
    merged.dashboard = Object.assign({}, _cognitionObject(base.dashboard), _cognitionObject(next.dashboard));
  }
  if (key === 'system' && (base.v2 || next.v2)) {
    merged.v2 = Object.assign({}, _cognitionObject(base.v2), _cognitionObject(next.v2));
  }
  return merged;
}

function _cogdashSnapshot(state) {
  const live = _cognitionObject(_cogdashLiveSnapshot());
  const patch = _cognitionObject(state);
  const merged = Object.assign({}, live, patch);
  ['system', 'brain', 'memory', 'identity', 'omega', 'autoloop'].forEach((key) => {
    merged[key] = _cogdashMergeBranch(live, patch, key);
  });
  return merged;
}

function _cogdashNumber(value, fallback) {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  const text = String(value == null ? '' : value).replace(/,/g, '').trim();
  if (!text) return fallback;
  const parsed = parseFloat(text.replace(/[^0-9.\-]/g, ''));
  return Number.isFinite(parsed) ? parsed : fallback;
}

function _cogdashUnit(value, fallback) {
  const fallbackValue = (typeof fallback === 'number') ? fallback : 0;
  const number = _cogdashNumber(value, NaN);
  if (!Number.isFinite(number)) return _cognitionClamp(fallbackValue, 0);
  const scaled = Math.abs(number) > 1 && Math.abs(number) <= 100 ? number / 100 : number;
  return _cognitionClamp(scaled, fallbackValue);
}

function _cogdashPercent(value) {
  return String(Math.round(_cogdashUnit(value, 0) * 100)) + '%';
}

function _cogdashPlainNumber(value, fallback) {
  const number = _cogdashNumber(value, fallback);
  if (!Number.isFinite(number)) return '--';
  if (Math.abs(number) < 10) return number.toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
  return String(Math.round(number));
}

function _cogdashDatumValue(item) {
  if (typeof item === 'number') return item;
  const source = _cognitionObject(item);
  return _cognitionFirstDefined(source, ['pct', 'value', 'level', 'score', 'contrib', 'pressure', 'load', 'v']);
}

function _cogdashSeries(value, fallback) {
  const raw = Array.isArray(value) ? value : [];
  const values = raw
    .map((item) => _cogdashUnit(_cogdashDatumValue(item), NaN))
    .filter((item) => Number.isFinite(item));
  if (!values.length && Number.isFinite(fallback)) values.push(_cognitionClamp(fallback, 0));
  return values.slice(-32);
}

function _cogdashHeatmapValues(value) {
  const raw = Array.isArray(value) ? value : [];
  const numeric = raw.map((item) => _cogdashNumber(_cogdashDatumValue(item), 0));
  const peak = Math.max(3, ...numeric.map((item) => Math.abs(item)));
  const values = numeric.map((item) => _cognitionClamp(item / peak, 0));
  while (values.length < COGDASH_HEATMAP_SIZE) values.push(0);
  return values.slice(0, COGDASH_HEATMAP_SIZE);
}

function _cogdashLabel(value, fallback) {
  const raw = String(value || fallback || '').trim().toLowerCase().replace(/[_-]+/g, ' ');
  if (!raw) return 'signal';
  if (raw.indexOf('phase cadence') !== -1) return 'cadence';
  if (raw.indexOf('latency') !== -1) return 'latency';
  if (raw.indexOf('pressure tick') !== -1) return 'tick';
  if (raw.indexOf('token') !== -1) return 'token';
  if (raw.indexOf('bandwidth') !== -1) return 'io';
  if (raw.indexOf('contrast') !== -1) return 'contrast';
  if (raw.indexOf('loop paused') !== -1) return 'pause';
  if (raw.indexOf('presence gate') !== -1) return 'gate';
  return _cognitionFriendlyLabel(raw);
}

function _cogdashBarEntries(value, defaults) {
  const rows = Array.isArray(value) ? value : [];
  const entries = rows.map((row) => {
    const source = _cognitionObject(row);
    return {
      label: _cogdashLabel(source.label || source.name, 'signal'),
      detail: String(source.detail || source.value || '').replace(/\s+/g, ' ').trim(),
      pct: _cogdashUnit(_cogdashDatumValue(source), 0),
    };
  });
  if (entries.length) return entries;
  return (defaults || []).map((label) => ({ label: label, detail: '', pct: 0 }));
}

function _cogdashLinePoints(values, width, height) {
  if (!values.length) return '';
  const series = values.length === 1 ? [values[0], values[0]] : values;
  const maxIndex = Math.max(1, series.length - 1);
  return series.map((value, index) => {
    const x = (index / maxIndex) * width;
    const y = height - 4 - (_cognitionClamp(value, 0) * (height - 8));
    return x.toFixed(1) + ',' + y.toFixed(1);
  }).join(' ');
}

function _cogdashSetText(root, key, value) {
  const node = root.querySelector('[data-cogdash-value="' + key + '"]');
  if (!node) return;
  const text = String(value == null || value === '' ? '--' : value);
  if (node.textContent !== text) node.textContent = text;
}

function _renderCogdashSparkline(root, key, values) {
  const line = root.querySelector('[data-cogdash-line="' + key + '"]');
  const area = root.querySelector('[data-cogdash-area="' + key + '"]');
  if (!line) return;
  const box = (line.ownerSVGElement && line.ownerSVGElement.viewBox && line.ownerSVGElement.viewBox.baseVal) || {};
  const width = box.width || (key === 'memory' ? 160 : 240);
  const height = box.height || 72;
  const points = _cogdashLinePoints(values, width, height);
  line.setAttribute('points', points);
  if (area) {
    area.setAttribute('points', points ? '0,' + height + ' ' + points + ' ' + width + ',' + height : '');
  }
}

function _renderCogdashRings(root, rings) {
  const holder = root.querySelector('[data-cogdash-rings]');
  if (!holder) return;
  const values = (Array.isArray(rings) ? rings : []).map((row) => _cogdashUnit(_cogdashDatumValue(row), 0));
  const labels = (Array.isArray(rings) ? rings : []).map((row) => (row && row.label) || 'signal');
  while (values.length < 3) values.push(0);
  while (labels.length < 3) labels.push('signal');
  holder.style.setProperty('--ring-a', Math.round(values[0] * 360) + 'deg');
  holder.style.setProperty('--ring-b', Math.round(values[1] * 360) + 'deg');
  holder.style.setProperty('--ring-c', Math.round(values[2] * 360) + 'deg');
  _cogdashSetText(root, 'ring-a', _cogdashPercent(values[0]));
  _cogdashSetText(root, 'ring-b', _cogdashPercent(values[1]));
  _cogdashSetText(root, 'ring-c', _cogdashPercent(values[2]));
  _cogdashSetText(root, 'ring-label-a', labels[0]);
  _cogdashSetText(root, 'ring-label-b', labels[1]);
  _cogdashSetText(root, 'ring-label-c', labels[2]);
}

function _renderCogdashBars(root, key, rows) {
  const target = root.querySelector('[data-cogdash-bars="' + key + '"]');
  if (!target) return;
  const pressure = key === 'pressure';
  target.replaceChildren();
  rows.slice(0, pressure ? 4 : 3).forEach((row) => {
    const item = document.createElement('div');
    item.className = pressure ? 'cogdash-pressure-row' : 'cogdash-bar-row';

    const label = document.createElement('span');
    label.className = pressure ? 'cogdash-pressure-name' : 'cogdash-bar-label';
    label.textContent = row.label || 'signal';
    item.appendChild(label);

    if (pressure) {
      const track = document.createElement('span');
      track.className = 'cogdash-pressure-track';
      const fill = document.createElement('span');
      fill.className = 'cogdash-pressure-fill';
      fill.style.setProperty('--bar-level', _cognitionClamp(row.pct, 0).toFixed(3));
      track.appendChild(fill);
      item.appendChild(track);

      const value = document.createElement('span');
      value.className = 'cogdash-pressure-value';
      value.textContent = _cogdashPercent(row.pct);
      item.appendChild(value);
    } else {
      const value = document.createElement('span');
      value.className = 'cogdash-bar-value';
      value.textContent = row.detail || _cogdashPercent(row.pct);
      item.appendChild(value);

      const track = document.createElement('span');
      track.className = 'cogdash-bar-track';
      const fill = document.createElement('span');
      fill.className = 'cogdash-bar-fill';
      fill.style.setProperty('--bar-level', _cognitionClamp(row.pct, 0).toFixed(3));
      track.appendChild(fill);
      item.appendChild(track);
    }
    target.appendChild(item);
  });
}

function _renderCogdashHeatmap(root, values) {
  const target = root.querySelector('[data-cogdash-heatmap]');
  if (!target) return;
  const cells = values.length ? values : _cogdashHeatmapValues([]);
  target.replaceChildren();
  cells.forEach((value) => {
    const cell = document.createElement('span');
    cell.className = 'cogdash-map-cell';
    cell.style.setProperty('--h', _cognitionClamp(value, 0).toFixed(3));
    target.appendChild(cell);
  });
}

function _cogdashStateFromTelemetry(state) {
  const snap = _cogdashSnapshot(state);
  let tick = _cognitionTickFromState(snap);
  if (_hasCognitionSignal(tick)) tick = _mergeCognitionLiveTick(tick);
  const visuals = _cognitionVisualStateFromTick(tick);
  const brainDash = _cognitionObject(_cognitionObject(snap.brain).dashboard);
  const memoryDash = _cognitionObject(_cognitionObject(snap.memory).dashboard);
  const omegaDash = _cognitionObject(_cognitionObject(snap.omega).dashboard);
  const autoDash = _cognitionObject(_cognitionObject(snap.autoloop).dashboard);
  const systemV2 = _cognitionObject(_cognitionObject(snap.system).v2);

  const load = _cogdashUnit(brainDash.cognitive_load, visuals.winner.level);
  const pressure = _cogdashUnit(
    brainDash.pressure ?? autoDash.pressure ?? systemV2.pressure_aggregate,
    visuals.winner.level
  );
  const coherence = _cogdashUnit(brainDash.coherence ?? omegaDash.coherence, 0);
  const contradictions = _cogdashNumber(
    brainDash.contradictions ?? omegaDash.contradiction_count ?? omegaDash.contradiction_score,
    0
  );
  const rings = Array.isArray(brainDash.activity_rings) && brainDash.activity_rings.length
    ? brainDash.activity_rings
    : (Array.isArray(autoDash.rings) ? autoDash.rings : [
      { value: load },
      { value: pressure },
      { value: coherence },
    ]);
  const memoryUtil = _cogdashUnit(memoryDash.utilization, 0);
  const live = Boolean(
    brainDash.live || memoryDash.live || omegaDash.live || autoDash.live
    || visuals.winner.state === 'active'
    || visuals.path.state === 'active'
    || visuals.motion.state === 'selected'
  );

  return {
    live: live,
    load: load,
    loadStatus: String(brainDash.load_status || omegaDash.posture || (load >= 0.66 ? 'elevated' : 'nominal')).toLowerCase(),
    phase: String(brainDash.active_phase || visuals.path.label || 'standby').toLowerCase().replace(/[_-]+/g, ' '),
    coherence: coherence,
    contradictions: contradictions,
    pressure: pressure,
    pressureStatus: String(autoDash.pressure_tag || omegaDash.contradiction_tag || (pressure >= 0.66 ? 'elevated' : 'steady')).toLowerCase(),
    neuralSeries: _cogdashSeries(brainDash.neural_series, load),
    neuralNote: String(brainDash.active_phase || 'signal').toLowerCase().replace(/[_-]+/g, ' '),
    tick: systemV2.pressure_last_tick && systemV2.pressure_last_tick.tick != null
      ? 't' + String(systemV2.pressure_last_tick.tick)
      : (brainDash.tick != null ? 't' + String(brainDash.tick) : '--'),
    rings: rings,
    memory: memoryUtil,
    memorySeries: _cogdashSeries(memoryDash.write_series, memoryUtil),
    throughput: _cogdashBarEntries(brainDash.throughput, ['cadence', 'latency', 'tick']),
    heatmap: _cogdashHeatmapValues(brainDash.heatmap),
    pressureBars: _cogdashBarEntries(brainDash.pressure_channels || autoDash.suppress_bars, ['signal', 'memory', 'gate', 'pause']),
  };
}

function refreshCognitionDrawer(state) {
  const root = document.getElementById('left-cognition-visuals');
  if (!root) return;
  const dashboard = _cogdashStateFromTelemetry(state);
  const panel = root.querySelector('[data-cogdash]');
  const ring = root.querySelector('[data-cogdash-ring]');
  if (panel) panel.dataset.live = String(dashboard.live);
  if (ring) ring.style.setProperty('--load-arc', Math.round(dashboard.load * 360) + 'deg');

  _cogdashSetText(root, 'load', _cogdashPercent(dashboard.load));
  _cogdashSetText(root, 'load-status', dashboard.loadStatus);
  _cogdashSetText(root, 'phase', dashboard.phase || 'standby');
  _cogdashSetText(root, 'coherence', _cogdashPlainNumber(dashboard.coherence, 0));
  _cogdashSetText(root, 'contradictions', _cogdashPlainNumber(dashboard.contradictions, 0));
  _cogdashSetText(root, 'pressure', _cogdashPercent(dashboard.pressure));
  _cogdashSetText(root, 'activity-status', dashboard.live ? 'live' : 'quiet');
  _cogdashSetText(root, 'neural-note', dashboard.neuralNote || 'signal');
  _cogdashSetText(root, 'tick', dashboard.tick);
  _cogdashSetText(root, 'memory', _cogdashPercent(dashboard.memory));
  _cogdashSetText(root, 'pressure-status', dashboard.pressureStatus);

  _renderCogdashSparkline(root, 'neural', dashboard.neuralSeries);
  _renderCogdashSparkline(root, 'memory', dashboard.memorySeries);
  _renderCogdashRings(root, dashboard.rings);
  _renderCogdashBars(root, 'throughput', dashboard.throughput);
  _renderCogdashHeatmap(root, dashboard.heatmap);
  _renderCogdashBars(root, 'pressure', dashboard.pressureBars);
}

function refreshCognitionDrawerFromLiveEvent(event) {
  const tick = _cognitionVisualsFromLiveEvent(event);
  if (!tick) return;
  const mergedTick = _mergeCognitionLiveTick(tick);
  const tickState = { system: { v2: { pressure_last_tick: mergedTick } } };
  refreshCognitionDrawer(tickState);
}

function _mindSetText(root, selector, value, fallback) {
  const node = root.querySelector(selector);
  if (!node) return;
  const text = String(value == null || value === '' ? (fallback || '--') : value);
  if (node.textContent !== text) node.textContent = text;
}

function _mindSetDocumentText(selector, value, fallback) {
  const node = document.querySelector(selector);
  if (!node) return;
  const text = String(value == null || value === '' ? (fallback || '--') : value);
  if (node.textContent !== text) node.textContent = text;
}

function _mindLabel(value, fallback) {
  const text = String(value || fallback || '').trim().replace(/[_-]+/g, ' ');
  if (!text) return '--';
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function _mindCycleForPhase(loop, phase) {
  const loopKey = String(loop || '').trim().toLowerCase().replace(/[-\s]+/g, '_');
  const phaseKey = String(phase || '').trim().toLowerCase().replace(/[-\s]+/g, '_');
  const direct = ['pressure', 'global_a', 'meta_a', 'auto', 'meta_b', 'global_b', 'omega', 'deepsleep'];
  if (direct.indexOf(loopKey) !== -1) return loopKey;
  if (loopKey === 'global') {
    return ['ignition', 'intent'].indexOf(phaseKey) !== -1 ? 'global_a' : 'global_b';
  }
  if (loopKey === 'meta') {
    return ['intake', 'action', 'tool', 'memory', 'pack', 'skill_selection'].indexOf(phaseKey) !== -1
      ? 'meta_a' : 'meta_b';
  }
  return '';
}

function _mindEventStatus(source) {
  const status = String(source.status || '').trim().toLowerCase();
  if (status === 'enter' || status === 'started' || status === 'running' || status === 'in_progress') return 'active';
  if (status === 'ok' || status === 'success' || status === 'complete' || status === 'completed') return 'done';
  return status || (source.phase || source.loop ? 'active' : 'idle');
}

function _mindRenderCascade(root, cascade) {
  const current = _cognitionObject(cascade);
  const activeCycle = String(current.cycle || _mindCycleForPhase(current.loop, current.phase) || '').toLowerCase();
  const status = _mindEventStatus(current);
  root.dataset.activeCycle = activeCycle || 'none';
  root.querySelectorAll('.mind-cycle-item').forEach((item) => {
    const isMatch = Boolean(activeCycle && item.dataset.cycle === activeCycle);
    item.dataset.active = isMatch ? 'true' : 'false';
    item.dataset.state = isMatch ? status : (item.dataset.state === 'done' ? 'done' : 'standby');
    const stateNode = item.querySelector('.cycle-state');
    if (stateNode) stateNode.textContent = isMatch ? status : (item.dataset.state === 'done' ? 'done' : 'standby');
    const detailNode = item.querySelector('[data-cycle-phase]');
    if (detailNode && isMatch) detailNode.textContent = current.phase || 'phase';
  });
}

function _mindRenderState(root, state) {
  const current = _cognitionObject(state);
  const episode = _cognitionObject(current.episode);
  const cascade = _cognitionObject(current.cascade);
  const ignition = _cognitionObject(current.ignition);
  const tool = _cognitionObject(current.tool);
  const autonomy = _cognitionObject(current.autonomy);
  const idleLearning = _cognitionObject(current.idleLearning);
  const memory = _cognitionObject(current.memory);
  const integrity = _cognitionObject(current.integrity);
  _mindRenderCascade(root, cascade);

  const episodeId = String(episode.id || cascade.episode_id || '').trim();
  _mindSetText(root, '[data-mind-episode]', episodeId || '—', '—');
  _mindSetText(root, '[data-mind-phase-cycle]', _mindLabel(cascade.cycle, 'standby'), 'standby');
  _mindSetText(root, '[data-mind-phase]', _mindLabel(cascade.phase, 'awaiting phase...'), 'awaiting phase...');
  const phaseStatus = root.querySelector('[data-mind-phase-status]');
  if (phaseStatus) {
    phaseStatus.textContent = _mindEventStatus(cascade);
    phaseStatus.dataset.mindPhaseStatus = _mindEventStatus(cascade);
  }

  _mindSetText(root, '[data-mind-ignition-winner]', _mindLabel(ignition.winner, 'pending'), 'pending');
  const aggregate = ignition.aggregate;
  _mindSetText(root, '[data-mind-ignition-score]', aggregate == null ? '--' : _cogdashPercent(aggregate), '--');
  _mindSetText(root, '[data-mind-ignition-source]', _mindLabel(ignition.source, '--'), '--');

  const action = String(current.action || '').trim();
  const actionNode = root.querySelector('[data-mind-action]');
  if (actionNode) actionNode.textContent = action || (tool.tool ? 'USE_TOOL' : 'pending');
  _mindSetText(root, '[data-mind-tool]', _mindLabel(tool.tool, 'none'), 'none');
  _mindSetText(root, '[data-mind-domain]', tool.domain || tool.knowledge_domain || '--', '--');
  _mindSetText(root, '[data-mind-query]', tool.query || 'awaiting tool context...', 'awaiting tool context...');

  const autonomyState = _mindLabel(autonomy.state, 'resting');
  _mindSetText(root, '[data-mind-autonomy-state]', autonomyState, 'resting');
  _mindSetText(root, '[data-mind-autonomy-reason]', autonomy.reason || 'awaiting pressure ignition', 'awaiting pressure ignition');
  _mindSetText(root, '[data-mind-autonomy-continuation]', autonomy.continuation || 'Omega decides successor pressure at foldback', 'Omega decides successor pressure at foldback');
  let idleLabel = String(idleLearning.label || '').trim();
  if (!idleLabel && Number.isFinite(Number(idleLearning.seconds_remaining))) {
    const seconds = Math.max(0, Math.round(Number(idleLearning.seconds_remaining)));
    idleLabel = 'eligible in ' + String(Math.floor(seconds / 60)).padStart(2, '0') + ':' + String(seconds % 60).padStart(2, '0');
  }
  _mindSetText(root, '[data-mind-idle-learning]', idleLabel || 'eligibility unavailable', 'eligibility unavailable');

  const pressureLabel = ignition.aggregate == null
    ? _mindLabel(ignition.winner, 'quiet')
    : (_mindLabel(ignition.winner, 'pressure') + ' · ' + _cogdashPercent(ignition.aggregate));
  const memoryWrites = Number(memory.write_total || 0);
  const memoryLabel = memoryWrites > 0 ? String(memoryWrites) + ' writes' : (memory.live ? 'live' : 'quiet');
  const integrityLabel = integrity.ledger_ok === true
    ? 'ledger verified'
    : (integrity.ledger_ok === false ? 'ledger fault' : 'unverified');
  _mindSetDocumentText('[data-cockpit-episode]', episodeId || 'resting', 'resting');
  _mindSetDocumentText('[data-cockpit-autonomy]', autonomyState, 'resting');
  _mindSetDocumentText('[data-cockpit-pressure]', pressureLabel, 'quiet');
  _mindSetDocumentText('[data-cockpit-memory]', memoryLabel, 'quiet');
  _mindSetDocumentText('[data-cockpit-integrity]', integrityLabel, 'unverified');

  const link = String(tool.learned_link || '').trim();
  const linkNode = root.querySelector('[data-mind-source-link]');
  if (linkNode) {
    const safe = /^https?:\/\//i.test(link);
    linkNode.hidden = !safe;
    if (safe) {
      linkNode.href = link;
      linkNode.textContent = tool.source_name ? 'learned from ' + tool.source_name : link;
      linkNode.title = link;
    } else {
      linkNode.removeAttribute('href');
      linkNode.textContent = '';
      linkNode.removeAttribute('title');
    }
  }
}

// Which lane the inspector is pinned to. '' = follow, i.e. show whichever lane
// is actually doing something (preferring the operator's own chat), which keeps
// the pre-lane behaviour as the default while making the choice explicit.
let _omegaMindLane = '';
const _MIND_LANE_ORDER = ['chat', 'agent', 'meta', 'window'];

function _mindLaneIsLive(lane) {
  const s = String(_cognitionObject(lane).state || '').toUpperCase();
  return Boolean(s) && s !== 'IDLE' && s !== 'COMPLETE';
}

// The lane the body renders when nothing is pinned. Chat first when it is live —
// the operator's own turn outranks background work — then any other live lane in
// priority order, else nothing (which leaves the legacy single-route view).
function _mindFollowLane(lanes) {
  if (_omegaMindLane && lanes[_omegaMindLane]) return _omegaMindLane;
  const live = _MIND_LANE_ORDER.filter((id) => lanes[id] && _mindLaneIsLive(lanes[id]));
  if (live.includes('chat')) return 'chat';
  return live[0] || '';
}

function _mindRenderLanes(root, lanes, selected) {
  const strip = root.querySelector('[data-mind-lanes]');
  if (!strip) return;
  const ids = _MIND_LANE_ORDER.filter((id) => lanes[id]);
  for (const id of Object.keys(lanes).sort()) {
    if (!ids.includes(id)) ids.push(id);
  }
  if (!ids.length) {
    if (strip.dataset.laneSig !== 'empty') {
      strip.dataset.laneSig = 'empty';
      strip.replaceChildren();
    }
    return;
  }
  const sig = ids.map((id) => {
    const l = _cognitionObject(lanes[id]);
    return id + ':' + String(l.state || '') + ':' + String(l.active_episode_id || l.episode_id || '');
  }).join('|') + '#' + selected + '#' + _omegaMindLane;
  if (strip.dataset.laneSig === sig) return;   // no churn between polls
  strip.dataset.laneSig = sig;

  const frag = document.createDocumentFragment();
  for (const id of ids) {
    const lane = _cognitionObject(lanes[id]);
    const state = String(lane.state || '').toUpperCase();
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'mind-lane-tab';
    btn.dataset.laneId = id;
    btn.dataset.laneState = state;
    btn.dataset.laneLive = _mindLaneIsLive(lane) ? 'true' : 'false';
    btn.dataset.lanePinned = (_omegaMindLane === id) ? 'true' : 'false';
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-selected', id === selected ? 'true' : 'false');
    const episode = String(lane.active_episode_id || lane.episode_id || '');
    btn.title = episode ? (id + ' · ' + episode) : (id + ' · no episode');
    const name = document.createElement('span');
    name.textContent = id;
    const st = document.createElement('span');
    st.className = 'mind-lane-tab-state';
    st.textContent = (state || 'unknown').toLowerCase();
    btn.append(name, st);
    // Click pins; clicking the pinned lane again releases back to follow.
    btn.addEventListener('click', () => {
      _omegaMindLane = (_omegaMindLane === id) ? '' : id;
      strip.dataset.laneSig = '';   // force a repaint of the pin marks
      try { refreshRightCognitionDrawer(); } catch (_e) { }
    });
    frag.appendChild(btn);
  }
  strip.replaceChildren(frag);
}

// Overlay one lane's live facts onto the singular inspector state.
//
// snap.cognition is one episode/cascade/ignition by construction — it predates
// the lanes and has no lane dimension, so with four lanes producing it carries
// whichever route wrote last. Rather than rewrite that whole pipeline, the lane
// view supplies the fields it genuinely knows (episode id, opened_by, pressure,
// dominant contributor, phase) and leaves the rest alone. Fields the lane poll
// does not carry — tool selection, autonomy — keep the legacy values and are
// therefore only trustworthy for the followed lane; that is a known limit, not
// an oversight.
function _mindApplyLane(mind, lane) {
  const l = _cognitionObject(lane);
  if (!l.lane) return mind;
  const episodeId = String(l.active_episode_id || l.episode_id || '');
  if (episodeId) mind.episode = Object.assign({}, mind.episode, { id: episodeId });
  if (l.phase) mind.cascade = Object.assign({}, mind.cascade, { phase: l.phase });
  if (episodeId) mind.cascade = Object.assign({}, mind.cascade, { episode_id: episodeId });
  const ign = Object.assign({}, mind.ignition);
  if (l.opened_by) ign.winner = l.opened_by;
  if (l.pressure_aggregate != null) ign.aggregate = l.pressure_aggregate;
  const dom = _cognitionObject(l.dominant_contributor);
  if (dom.source) ign.source = dom.source;
  mind.ignition = ign;
  mind.lane = String(l.lane || '');
  mind.lane_state = String(l.state || '');
  return mind;
}

function _mindStateFromTelemetry(state) {
  const snap = _cogdashSnapshot(state);
  const cognition = _cognitionObject(snap.cognition);
  const system = _cognitionObject(snap.system);
  const v2 = _cognitionObject(system.v2);
  const tick = _cognitionObject(v2.pressure_last_tick);
  const ignition = Object.assign({}, _cognitionObject(cognition.ignition));
  if (!ignition.winner) ignition.winner = tick.opened_by || tick.dominant || '';
  if (ignition.aggregate == null) ignition.aggregate = tick.pressure_aggregate;
  if (!ignition.source) ignition.source = _cognitionObject(tick.dominant_contributor).source || '';
  const cascade = Object.assign({}, _cognitionObject(cognition.cascade));
  if (!cascade.phase) {
    const cycle = _cognitionObject(snap.cycle);
    cascade.phase = cycle.layer || '';
    cascade.cycle = cascade.cycle || _mindCycleForPhase(cycle.source, cascade.phase);
  }
  const tool = Object.assign({}, _cognitionObject(cognition.tool));
  const memoryDashboard = _cognitionObject(_cognitionObject(snap.memory).dashboard);
  const identityDashboard = _cognitionObject(_cognitionObject(snap.identity).dashboard);
  return {
    episode: Object.assign({}, _cognitionObject(cognition.episode)),
    cascade: cascade,
    ignition: ignition,
    tool: tool,
    autonomy: Object.assign({}, _cognitionObject(cognition.autonomy)),
    idleLearning: Object.assign({}, _cognitionObject(cognition.idle_learning)),
    memory: {
      live: Boolean(memoryDashboard.live),
      write_total: memoryDashboard.write_total || 0,
    },
    integrity: { ledger_ok: identityDashboard.ledger_ok },
    action: tool.action || (tool.tool ? 'USE_TOOL' : ''),
  };
}

function refreshRightCognitionDrawer(state) {
  const root = document.getElementById('omega-mind-view');
  if (!root) return;
  _omegaMindState = _mindStateFromTelemetry(state);
  // Lane dimension. The body below renders ONE episode; the strip shows all
  // four so a chat opened behind a running agent build is never invisible, and
  // the followed/pinned lane decides which episode the body describes.
  const lanes = _cognitionObject(_cogdashSnapshot(state).lanes);
  const followed = _mindFollowLane(lanes);
  if (followed && lanes[followed]) {
    _omegaMindState = _mindApplyLane(_omegaMindState, lanes[followed]);
  }
  _mindRenderLanes(root, lanes, followed);
  _mindRenderState(root, _omegaMindState);
  const dashboard = _cogdashStateFromTelemetry(state);
  
  const pressureList = root.querySelector('[data-mind-pressure]');
  if (pressureList && dashboard.pressureBars) {
    pressureList.replaceChildren();
    dashboard.pressureBars.slice(0, 4).forEach((row) => {
      if (!row || !row.label) return;
      
      const item = document.createElement('div');
      item.className = 'mind-pressure-item';
      
      const label = document.createElement('span');
      label.className = 'mind-pressure-label';
      label.textContent = row.label;
      item.appendChild(label);
      
      const score = document.createElement('span');
      score.className = 'mind-pressure-score';
      score.textContent = _cogdashPercent(row.pct);
      item.appendChild(score);
      
      const track = document.createElement('span');
      track.className = 'mind-pressure-bar-track';
      
      const fill = document.createElement('span');
      fill.className = 'mind-pressure-bar-fill';
      fill.style.setProperty('--score-pct', _cognitionClamp(row.pct, 0).toFixed(3));
      track.appendChild(fill);
      
      item.appendChild(track);
      pressureList.appendChild(item);
    });
    if (!pressureList.children.length) {
      const empty = document.createElement('div');
      empty.className = 'mind-thought-empty';
      empty.textContent = 'awaiting signal...';
      pressureList.appendChild(empty);
    }
  }
}

function refreshOmegaMindFromLiveEvent(event) {
  const source = _cognitionObject(event);
  if (!Object.keys(source).length) return;
  
  const root = document.getElementById('omega-mind-view');
  if (!root) return;

  const actionSource = Object.assign(
    {},
    _cognitionObject(source.chosen_action),
    _cognitionObject(source.intent_decision),
    _cognitionObject(source.execution_action_pack)
  );
  
  const kind = String(source.kind || '').trim().toLowerCase();
  const phase = String(source.phase || source.operation || source.op || source.name || '').trim().toLowerCase();
  const action = (
    _cognitionFirstDefined(actionSource, ['action', 'intent', 'decision', 'type'])
    ?? _cognitionFirstDefined(source, ['action', 'intent', 'omega_intent', 'trajectory_decision', 'prompt_type', 'tool', 'decision'])
    ?? (kind === 'signal' ? String(source.name || '').trim().toLowerCase() : '')
  );
  const loop = String(source.loop || source.system || '').trim().toLowerCase();
  const activeCycle = _mindCycleForPhase(loop, phase);
  const next = {
    episode: source.episode_id ? {
      id: String(source.episode_id),
      state: 'active',
      active: true,
    } : {},
    cascade: activeCycle ? {
      cycle: activeCycle,
      loop: loop,
      phase: phase,
      status: _mindEventStatus(source),
      episode_id: source.episode_id || '',
    } : {},
    ignition: (phase === 'episode_ignition' || source.aggregate_pressure != null || source.pressure_aggregate != null) ? {
      winner: source.opened_by || source.dominant || source.winner || '',
      aggregate: source.aggregate_pressure ?? source.pressure_aggregate ?? source.aggregate,
      source: _cognitionObject(source.dominant_contributor).source || source.source || '',
    } : {},
    tool: (phase === 'tool_invocation' || loop === 'tools' || source.tool_name || source.tool) ? {
      tool: source.tool_name || source.tool || '',
      action: action || 'USE_TOOL',
      query: source.query || source.query_preview || '',
      domain: source.domain || '',
      learned_link: source.learned_link || source.source_url || '',
      source_name: source.source_name || source.source || '',
      status: source.ok === false ? 'failed' : 'success',
    } : {},
    action: action || '',
  };
  _omegaMindState = {
    episode: Object.assign({}, _cognitionObject(_omegaMindState.episode), next.episode),
    cascade: Object.assign({}, _cognitionObject(_omegaMindState.cascade), next.cascade),
    ignition: Object.assign({}, _cognitionObject(_omegaMindState.ignition), next.ignition),
    tool: Object.assign({}, _cognitionObject(_omegaMindState.tool), next.tool),
    autonomy: Object.assign({}, _cognitionObject(_omegaMindState.autonomy)),
    idleLearning: Object.assign({}, _cognitionObject(_omegaMindState.idleLearning)),
    memory: Object.assign({}, _cognitionObject(_omegaMindState.memory)),
    integrity: Object.assign({}, _cognitionObject(_omegaMindState.integrity)),
    action: next.action || _omegaMindState.action || '',
  };
  _mindRenderState(root, _omegaMindState);
}

function initSideDrawer() {
  document.querySelectorAll('.side-drawer').forEach((drawer) => {
    const side = drawer.getAttribute('data-side-drawer-side') || 'left';
    const toggle = drawer.querySelector('.side-drawer-toggle');
    if (!toggle || drawer.dataset.sideDrawerBound === 'true') return;
    drawer.dataset.sideDrawerBound = 'true';

    const icon = toggle.querySelector('.side-drawer-toggle-icon');
    const closedIcon = side === 'right' ? '<' : '>';
    const openIcon = side === 'right' ? '>' : '<';

    function setOpen(open, persist) {
      drawer.dataset.sideDrawer = open ? 'open' : 'closed';
      document.body.classList.toggle('side-drawer-' + side + '-open', open);
      toggle.setAttribute('aria-expanded', String(open));
      toggle.setAttribute('aria-label', open ? 'Close ' + side + ' HUD panel' : 'Open ' + side + ' HUD panel');
      toggle.title = open ? 'Close ' + side + ' HUD panel' : 'Open ' + side + ' HUD panel';
      if (icon) icon.textContent = open ? openIcon : closedIcon;
      if (persist) _sideDrawerSaveState(side, open);
    }
    drawer._omegaSetOpen = setOpen;

    toggle.addEventListener('click', () => {
      const willOpen = drawer.dataset.sideDrawer !== 'open';
      if (willOpen) _closeOppositeDrawer(side);
      setOpen(willOpen, true);
    });

    window.addEventListener('keydown', (event) => {
      if (event.key !== 'Escape' || drawer.dataset.sideDrawer !== 'open') return;
      const panel = document.getElementById('panel');
      if (panel && panel.classList.contains('open')) return;
      event.preventDefault();
      setOpen(false, true);
    });

    const stored = _sideDrawerStoredSideState(side);
    const fallback = side === 'left' && stored == null ? _sideDrawerStoredState() : stored;
    setOpen(fallback === 'open', false);
  });
  if (_sideDrawersAreOverlay()) {
    const openDrawers = [...document.querySelectorAll('.side-drawer[data-side-drawer="open"]')];
    openDrawers.slice(1).forEach((drawer) => {
      if (typeof drawer._omegaSetOpen === 'function') drawer._omegaSetOpen(false, true);
    });
  }
  if (!_cognitionTelemetryUnsubscribe
    && typeof Telemetry !== 'undefined'
    && Telemetry
    && typeof Telemetry.subscribe === 'function') {
    _cognitionTelemetryUnsubscribe = Telemetry.subscribe((state) => {
      refreshRightCognitionDrawer(state);
    });
  }
  refreshRightCognitionDrawer(null);
}

function openRightCognitionDrawer() {
  initSideDrawer();
  const drawer = document.getElementById('side-drawer-right');
  if (!drawer) return false;
  _closeOppositeDrawer('right');
  if (typeof drawer._omegaSetOpen === 'function') {
    drawer._omegaSetOpen(true, true);
  }
  const pinboard = drawer.querySelector('[data-proactive-pinned-list]');
  if (pinboard && typeof pinboard.focus === 'function') {
    if (!pinboard.hasAttribute('tabindex')) pinboard.setAttribute('tabindex', '-1');
    pinboard.focus({ preventScroll: true });
  }
  return true;
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initSideDrawer, { once: true });
} else {
  initSideDrawer();
}

window.initSideDrawer = initSideDrawer;
window.openRightCognitionDrawer = openRightCognitionDrawer;
window.refreshCognitionDrawer = refreshCognitionDrawer;
window.refreshCognitionDrawerFromLiveEvent = refreshCognitionDrawerFromLiveEvent;
window.refreshOmegaMindFromLiveEvent = refreshOmegaMindFromLiveEvent;
window._cognitionVisualStateFromTick = _cognitionVisualStateFromTick;
window._cognitionVisualsFromLiveEvent = _cognitionVisualsFromLiveEvent;

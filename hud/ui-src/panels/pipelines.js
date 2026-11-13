// Panel: pipelines — all four physical execution pipelines at once.
// Registry: PANELS.pipelines.body -> panelPipelines
// Data: window.pollPipelines -> Telemetry.pipelines
//
// Exists because the rest of the HUD was built around ONE cascade. With four
// pipelines running concurrently, more than one
// episode can be open at a time, and a reader that keeps "the current episode"
// bounces between them and hides simultaneous work.
//
// This panel never asks "what is the system doing". It asks what each pipeline is
// doing, which is the only question with a stable answer once workers are
// independent.

const _PIPELINE_ORDER = ['0', '1', '2', '3'];

function _pipelineRows(pipelines) {
  const seen = new Set(_PIPELINE_ORDER);
  const ordered = _PIPELINE_ORDER.filter(id => pipelines[id]);
  // Any pipeline the backend reports that this build does not know about still
  // renders — a worker added later must not be invisible just because the HUD
  // predates it.
  for (const id of Object.keys(pipelines).sort()) {
    if (!seen.has(id)) ordered.push(id);
  }
  return ordered;
}

function _pipelineStateClass(state) {
  const s = String(state || '').toUpperCase();
  if (s === 'FAILED') return 'lane-state lane-state-failed';
  if (s === 'FREE' || s === '') return 'lane-state lane-state-idle';
  if (s === 'COMPLETE') return 'lane-state lane-state-done';
  return 'lane-state lane-state-live';
}

function _renderPipelineCard(id, pipeline) {
  const E = _escapeHtml;
  const state = String(pipeline.state || '');
  const episode = String(pipeline.active_episode_id || '');
  const phase = String(pipeline.phase || '');
  const workKind = String(pipeline.work_kind || '');
  const cycle = String(pipeline.cycle_id || '');
  const job = String(pipeline.job_id || '');
  const queue = String(pipeline.queue_id || '');

  const rows = [];
  const row = (k, v, cls) => {
    if (!v) return;
    rows.push(`<div class="line lane-line"><span class="k">${E(k)}</span>`
      + `<span class="v${cls ? ' ' + cls : ''}">${E(v)}</span></div>`);
  };
  row('episode', episode || '—');
  row('work', workKind);
  row('cycle', cycle);
  row('job', job);
  row('queue', queue);
  row('phase', phase);

  return `
        <div class="lane-card" data-lane="${E(id)}" data-lane-state="${E(state.toUpperCase())}">
          <div class="lane-head">
            <span class="lane-name">P${E(id)}</span>
            <span class="${_pipelineStateClass(state)}">${E(state || 'unknown')}</span>
          </div>
          <div class="lane-sub">${E(workKind || 'unassigned')}</div>
          ${rows.join('\n')}
        </div>`;
}

function _renderPipelinesBody(pipelines) {
  const ids = _pipelineRows(pipelines || {});
  if (!ids.length) {
    return `<div class="panel-row"><span class="k">—</span>
<span class="v">no pipeline telemetry yet</span><span class="t"></span></div>`;
  }
  const active = ids.filter(id => {
    const s = String((pipelines[id] || {}).state || '').toUpperCase();
    return s && s !== 'FREE' && s !== 'COMPLETE';
  }).length;
  const header = `<div class="panel-label">${active} of ${ids.length} pipelines active</div>`;
  return header + ids.map(id => _renderPipelineCard(id, pipelines[id] || {})).join('\n');
}

function _pipelinesFromSnapshot() {
  const snap = (window.Telemetry && Telemetry.snapshot) ? Telemetry.snapshot() : {};
  return (snap && snap.pipelines) || {};
}

function _paintPipelinesPanel(root) {
  const html = _renderPipelinesBody(_pipelinesFromSnapshot());
  // Hash-gate the write: this panel repaints on every pipeline poll, and blowing
  // away innerHTML unconditionally is what took heavy panels down before.
  const sig = _simpleHash(html);
  if (root.dataset.pipelinesSig === sig) return;
  root.dataset.pipelinesSig = sig;
  root.innerHTML = html;
}

function _refreshPipelinesPanel() {
  if (typeof _currentPanelId !== 'undefined' && _currentPanelId !== 'pipelines') return;
  const root = panelBody && panelBody.querySelector('[data-panel-root="pipelines"]');
  if (!root || !root.dataset.panelMounted) return;
  _paintPipelinesPanel(root);
}

function panelPipelines(b) {
  const root = _ensurePanelRoot(b, 'pipelines');
  root.dataset.panelMounted = '1';
  _paintPipelinesPanel(root);
  // The 2s pipeline poll drives every later repaint.
  // this first paint just avoids an empty frame on open.
  try { if (typeof window.pollPipelines === 'function') window.pollPipelines(); } catch (_) { }
}
window._refreshPipelinesPanel = _refreshPipelinesPanel;

// Remaining panel dashboards — still inline in monolith until phase 8.
// Depends on core/canvas.js (_td*, _renderTrajectoryStarMap still in monolith).

function _renderIdentityDashboard(d, presence) {
  const dash = d.dashboard || {};
  const state = String(presence || 'UNKNOWN').toUpperCase();
  return `<div class="td-dashboard td-identity td-dashboard-tiered">` +
    _tdPanelRail(
      state === 'PRESENT' ? 'live' : (dash.live ? 'protected' : 'idle'), state,
      `${dash.operator_count || 0} operators · ${dash.trait_count || 0} traits`) +
    _tdCard('Self Vector', '',
      _tdDonut(dash.confidence, 'self vector', dash.drift || 'stable', [
        { k: 'operators', v: String(dash.operator_count || 0), tone: 'neutral' },
        { k: 'traits', v: String(dash.trait_count || 0), tone: 'neutral' },
        { k: 'vectors', v: String(dash.vector_count || 0), tone: 'stable' },
      ], 'identity', 'confidence'), 'td-span-5') +
    _tdCard('Drift Velocity', '',
      _tdWaveform(
        [dash.drift_velocity || 0, dash.confidence || 0, (dash.operator_count || 0) / 10],
        state === 'PRESENT', 'identity stability field'), 'td-span-7') +
    _tdHeroShell('Presence Field', '',
      _renderPresenceOrb(d, presence), 'td-span-8') +
    _tdCard('Closed World', '',
      _tdBarList([
        { label: 'ledger', pct: dash.ledger_ok ? 1 : 0.2, detail: dash.ledger_ok ? 'ok' : 'check' },
        { label: 'seal', pct: dash.sealed ? 1 : 0.15, detail: dash.sealed ? 'sealed' : 'open' },
      ]), 'td-span-4') +
    `</div>`;
}

function _renderDeepsleepDashboard(d) {
  const dash = d.dashboard || {};
  return `<div class="td-dashboard td-deepsleep td-dashboard-tiered">` +
    _tdPanelRail(
      dash.progress >= 1 ? 'protected' : (dash.live ? 'live' : 'idle'),
      String(dash.cycle_id || '—'),
      `intake ${dash.intake_fragments || 0} · replay ${dash.replay_count || 0}`) +
    _tdHeroShell('Consolidation Flow', '',
      _renderConsolidationFlow(d), 'td-span-8') +
    _tdCard('Consolidation Progress', '',
      _tdDonut(dash.progress, 'consolidation', `cycle ${dash.cycle_id || '—'}`, [
        { k: 'intake', v: String(dash.intake_fragments || 0), tone: 'neutral' },
        { k: 'replay', v: String(dash.replay_count || 0), tone: 'neutral' },
        { k: 'resolved', v: String(dash.contradictions_resolved || 0), tone: 'stable' },
      ], 'deepsleep', 'progress'), 'td-span-4') +
    _tdCard('Sleep Metrics', '',
      _tdBarList(dash.phase_bars, 'deepsleep', 'phase'), 'td-span-12') +
    `</div>`;
}

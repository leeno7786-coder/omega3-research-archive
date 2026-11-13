// Physical execution pipeline poll (hud/ui-src/core/poll-pipelines.js)
// Depends on: Telemetry
// Exports: window.pollPipelines

// Four physical workers can run concurrently, so more than one
// episode can be open at a time. The unified snapshot poll cannot show that:
// its backend reader keeps the newest ignition in the whole audit ring
// regardless of pipeline, so whichever worker emitted last owns the panel.
//
// This uncached poll reads a view keyed by numeric physical pipeline.
//
// Deliberately its own poll rather than a subtree of /api/telemetry/snapshot:
// that endpoint is TTL-cached server-side and a lane can open, seat, run and
// close inside one cache window. Serving pipelines from it would reproduce the
// defect this exists to fix.
async function pollPipelines() {
  try {
    const r = await fetch('/api/telemetry/pipelines', { cache: 'no-store' });
    if (!r.ok) return;
    const payload = await r.json();
    if (!payload || typeof payload !== 'object') return;
    if (typeof Telemetry === 'undefined' || typeof Telemetry.update !== 'function') return;
    // Two calls on purpose — this is a REPLACE, not a merge.
    //
    // Telemetry.update deep-merges, so a key that stops being reported keeps
    // its old value forever. A completed worker must not retain old correlation.
    //
    // deepMerge only recurses when BOTH sides are plain objects; anything else
    // is assigned wholesale. So nulling first makes the second call a clean
    // overwrite rather than a union with the previous poll.
    Telemetry.update({ pipelines: null });
    Telemetry.update({ pipelines: payload.pipelines || {} });
    try { if (typeof _refreshPipelinesPanel === 'function') _refreshPipelinesPanel(); } catch (_) { }
  } catch (e) { /* server not up yet; retry on next tick */ }
}
window.pollPipelines = pollPipelines;

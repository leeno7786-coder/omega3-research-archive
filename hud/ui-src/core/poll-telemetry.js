// Unified telemetry snapshot + diagnostics polls (hud/ui-src/core/poll-telemetry.js)
// Depends on: Telemetry, applyLiveBootStrings, _applyV2PillBadges
// Exports: window.pollTelemetrySnapshot, window.pollDiagnostics

// ── HUD-4..9: unified telemetry snapshot ──────────────────────
// The design's Telemetry singleton (created earlier in this file)
// exposes update(patch) with deep-merge semantics. We poll the
// backend's unified snapshot endpoint and feed the patch into
// Telemetry.update() — every panel render that reads from
// Telemetry.snapshot() automatically picks up the live data.
// Empty snapshot slots stay empty until the backend fills them
// (deep-merge preserves un-patched fields).
async function pollTelemetrySnapshot() {
  try {
    const r = await fetch('/api/telemetry/snapshot', { cache: 'no-store' });
    if (!r.ok) return;
    const patch = await r.json();
    if (typeof Telemetry !== 'undefined' && typeof Telemetry.update === 'function') {
      preserveBrowserOwnedSystemTelemetry(patch);
      Telemetry.update(patch);   // -> fires Telemetry.subscribe ->
      //    _scheduleRender (subtree-diffed)
      // FX-1/2/3: refresh the boot-area live strings (cycle counter,
      // current objective, autonomous activity) on every poll.
      try { applyLiveBootStrings(patch); } catch (_) { }
      try { _applyV2PillBadges(); } catch (_) { }
      // NOTE: we used to also call openPanel(_currentPanelId) here.
      // That fired a SECOND destructive rebuild on top of the
      // subscribe handler above and was the cause of panel
      // freeze + browser crash on heavy panels. The Telemetry.
      // subscribe path now re-renders only when the relevant
      // subtree actually changed.
    }
  } catch (e) { /* server not up yet; retry on next tick */ }
}
function preserveBrowserOwnedSystemTelemetry(patch) {
  if (!patch || !patch.system || typeof Telemetry === 'undefined') return;
  const current = (Telemetry.snapshot && Telemetry.snapshot().system) || {};
  for (const key of ['vision', 'face_watch']) {
    const local = current[key];
    if (!local || !patch.system[key]) continue;
    const localEvent = String(local.last_event || '');
    const localState = String(local.state || '');
    if (localEvent && localEvent !== 'pre-init' && localState !== 'unavailable') {
      delete patch.system[key];
    }
  }
}

// ── C: tts/stt subsystem-health diagnostics ───────────
// Face/vision diagnostics are intentionally omitted from the boot
// poll so local face models only load after an explicit settings
// action. A dedicated `diagnostics` slot is used (NOT system.vision/
// voice) so it can't collide with browser-owned state machines.
async function pollDiagnostics() {
  const grab = async (url) => {
    try {
      const r = await fetch(url, { cache: 'no-store' });
      if (!r.ok) return null;
      return await r.json();
    } catch (e) { return null; }
  };
  const [tts, stt] = await Promise.all([
    grab('/api/tts/status'),
    grab('/api/stt/status'),
  ]);
  if (!tts && !stt) return;  // server down
  Telemetry.update({
    system: {
      diagnostics: {
        tts, stt, ts: Date.now(),
      }
    }
  });
}
window.pollTelemetrySnapshot = pollTelemetrySnapshot;
window.pollDiagnostics = pollDiagnostics;

// Poll loop tiers — 2s / 5s / 10s / 15s cadences.
// Handlers: poll-status.js, poll-telemetry.js, poll-v2.js (window.poll*).
// Initial probe + 2s polling. Stops auto when the page is hidden
// (saves resources when the operator switches tabs).
function startPolling() {
  window.pollStatus();
  window.pollBootState();
  window.pollTelemetrySnapshot();
  window.pollV2Fast();
  // Pipelines ride the 2s tier: with four concurrent workers an episode can open,
  // seat, run and close between slower polls, and a missed transition is
  // exactly the observability gap this poll exists to close.
  window.pollPipelines();
  return setInterval(() => {
    if (document.hidden) return;
    window.pollStatus();
    window.pollBootState();
    window.pollTelemetrySnapshot();
    window.pollV2Fast();
    window.pollPipelines();
  }, 2000);
}
startPolling();
// P3-S06: v2 medium-cadence (broker + response state) at 5s.
window.pollV2Medium();
setInterval(() => { if (!document.hidden) window.pollV2Medium(); }, 5000);
// P3-S06: v2 slow-cadence (runtime health) at 10s.
window.pollV2Slow();
setInterval(() => { if (!document.hidden) window.pollV2Slow(); }, 10000);
// Subsystem-health diagnostics on a slower cadence (rarely
// changes; keeps the hot 2s loop untouched — lowest risk).
window.pollDiagnostics();
setInterval(() => { if (!document.hidden) window.pollDiagnostics(); }, 15000);

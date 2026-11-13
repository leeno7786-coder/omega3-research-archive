// Settings: permanent read-only proactive interaction history.
function _proactiveHistoryTypeLabel(type) {
  const labels = { question: 'Question', result: 'Result', notice: 'Notice' };
  return labels[String(type || '').toLowerCase()] || 'Interaction';
}

function _proactiveHistoryStatusLabel(status) {
  const labels = {
    pending: 'Pending',
    responded: 'Responded',
    delivered: 'Pinned',
    cleared: 'Cleared',
  };
  return labels[String(status || '').toLowerCase()] || String(status || 'Recorded');
}

function _proactiveHistoryTimestamp(raw) {
  if (!raw) return 'time unavailable';
  try {
    const value = new Date(raw);
    if (!Number.isFinite(value.getTime())) return 'time unavailable';
    return value.toLocaleString([], {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch (_e) {
    return 'time unavailable';
  }
}

function _proactiveHistoryCard(entry) {
  const E = _escapeHtml;
  const type = String(entry.type || '').toLowerCase();
  const typeLabel = _proactiveHistoryTypeLabel(type);
  const statusLabel = _proactiveHistoryStatusLabel(entry.status);
  const responseText = String(
    (entry.response && entry.response.text) || ''
  ).trim();
  const responseLabel = type === 'result' ? 'your grade' : 'your response';
  const response = responseText
    ? `<div class="proactive-history-response"><span>${responseLabel}</span>${E(responseText)}</div>`
    : '';
  const episode = String(entry.episode_id || '').trim();
  const thread = String(entry.thread_id || '').trim();
  const context = [
    episode ? `episode ${E(episode)}` : '',
    thread ? `thread ${E(thread)}` : '',
  ].filter(Boolean).join(' · ');
  return `<article class="proactive-history-card" data-history-type="${E(type)}" data-history-status="${E(entry.status || '')}">
    <div class="proactive-history-head">
      <span class="proactive-history-type">${typeLabel}</span>
      <span class="proactive-history-status">${E(statusLabel)}</span>
    </div>
    <div class="proactive-history-time">${E(_proactiveHistoryTimestamp(entry.ts))}</div>
    <div class="proactive-history-text">${E(String(entry.text || ''))}</div>
    ${response}
    ${context ? `<div class="proactive-history-context">${context}</div>` : ''}
  </article>`;
}

async function _renderSettingsProactiveHistory(panel) {
  if (!panel) return;
  panel.innerHTML = `<div class="panel-label">proactive history</div>
    <p class="line bullet">Permanent read-only record of Omega's questions, presented results, informational notices, and your replies or grades.</p>
    <div class="proactive-history-loading">loading proactive interactions…</div>`;
  const response = await fetch('/api/proactive/history', { cache: 'no-store' });
  if (!response.ok) throw new Error('history unavailable · HTTP ' + response.status);
  const body = await response.json();
  const entries = Array.isArray(body.entries) ? body.entries : [];
  const content = entries.length
    ? entries.map(_proactiveHistoryCard).join('')
    : '<div class="proactive-history-empty">No proactive interactions recorded yet.</div>';
  panel.innerHTML = `<div class="panel-label">proactive history</div>
    <p class="line bullet">Permanent read-only record of Omega's questions, presented results, informational notices, and your replies or grades.</p>
    <div class="proactive-history-summary">${entries.length} recorded interaction${entries.length === 1 ? '' : 's'}</div>
    <div class="proactive-history-list">${content}</div>`;
}

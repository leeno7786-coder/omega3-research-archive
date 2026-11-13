// Panel registry + openPanel glue (hud/ui-src/panels/register.js)
// Bundled before panel body modules — PANELS[id].body getters resolve at openPanel() time.

const panel = document.getElementById('panel');
const panelTitle = document.getElementById('panel-title');
const panelBody = document.getElementById('panel-body');
const toast = document.getElementById('toast');
const toastCmd = document.getElementById('toast-cmd');
const REMOVED_CHAT_PANEL_IDS = new Set([
  'brain',
  'memory',
  'identity',
  'omega',
  'autoloop',
  'deepsleep',
  'dev',
]);

// Panel body getters defer resolution until openPanel() — builder
// functions load in later script tags (07+). Eager `body: panelX`
// references throw ReferenceError during this file's execution.
const PANELS = {
  help: {
    title: 'help<em>.</em>',
    get body() { return panelHelp; },
    cmd: 'omega, help',
  },
  settings: {
    title: 'settings<em>.</em>',
    get body() { return panelSettings; },
    cmd: 'omega, open settings',
  },
  pipelines: {
    title: 'pipelines<em>.</em>',
    get body() { return panelPipelines; },
    cmd: 'omega, open pipelines',
  },
};

document.querySelectorAll('.pill').forEach(btn => {
  btn.addEventListener('click', () => {
    const id = btn.dataset.panel;
    if (!id) return;
    const p = PANELS[id];
    if (!p) return;
    document.querySelectorAll('.pill').forEach(b => b.classList.toggle('active', b === btn));
    showToast(p.cmd);
    openPanel(id);
  });
});

let _currentPanelId = null;

function openPanel(id) {
  if (REMOVED_CHAT_PANEL_IDS.has(id)) return;
  const previousPanelId = _currentPanelId;
  if ((previousPanelId === 'settings' || id === 'settings') && window._memoryGraphDestroy) {
    window._memoryGraphDestroy();
  }
  _currentPanelId = id;
  const p = PANELS[id];
  if (!p) {
    panelTitle.innerHTML = id + '<em>?</em>';
    panelBody.innerHTML = '<div class="panel-row"><span class="k">!</span>' +
      '<span class="v">unknown panel id: ' + id + '</span><span class="t"></span></div>';
    panel.classList.add('open');
    return;
  }
  panelTitle.innerHTML = p.title;
  panelBody.innerHTML = '';
  try {
    if (typeof p.body === 'function') p.body(panelBody);
  } catch (err) {
    console.error('panel render failed for ' + id + ':', err);
    const errCard = document.createElement('div');
    errCard.className = 'panel-row';
    errCard.innerHTML =
      '<span class="k">!</span>' +
      '<span class="v">panel render failed — ' +
      _escapeHtml(String(err && err.message || err)) +
      '</span><span class="t">js</span>';
    panelBody.appendChild(errCard);
  }
  panel.classList.add('open');

  setTimeout(() => {
    try {
      const live = _PANEL_LIVE_REFRESH[id];
      if (live) live();
      if (id === 'identity' && typeof _refreshPresenceOrbState === 'function') {
        _refreshPresenceOrbState();
      } else if (id === 'autoloop' && typeof _refreshRespiratoryPauseState === 'function') {
        _refreshRespiratoryPauseState();
      } else if (id === 'brain' && typeof _refreshCycleWheelState === 'function') {
        _refreshCycleWheelState();
      }
    } catch (_e) { /* non-fatal */ }
  }, 0);
}

// OAuth callbacks run in a short-lived same-origin popup.  Return the
// operator to the plugin settings view and let its no-store directory request
// show the newly connected status without reflecting any OAuth query values.
function _openPluginSettingsFromOAuth() {
  openPanel('settings');
  const pluginsTab = document.querySelector('[data-settings-view="plugins"]');
  if (pluginsTab) pluginsTab.click();
  if (typeof refreshPendingPluginPermissions === 'function') {
    refreshPendingPluginPermissions();
  }
}

window.addEventListener('message', event => {
  if (event.origin !== window.location.origin) return;
  const data = event.data;
  if (!data || data.type !== 'omega:plugin-connected' ||
      typeof data.pluginId !== 'string' ||
      !/^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$/.test(data.pluginId)) return;
  try {
    _openPluginSettingsFromOAuth();
  } catch (_) {
    // The callback has a same-origin fallback link if the HUD is navigating.
  }
});

// If popup creation was blocked, the callback redirects the current tab here
// instead. Consume the one-shot hash after all panel bodies have loaded.
function _consumePluginSettingsHash() {
  if (window.location.hash !== '#settings/plugins') return;
  try { window.history.replaceState(null, '', window.location.pathname); } catch (_) {}
  try { _openPluginSettingsFromOAuth(); } catch (_) {}
}
if (document.readyState === 'loading') {
  window.addEventListener('DOMContentLoaded', _consumePluginSettingsHash, {once: true});
} else {
  window.setTimeout(_consumePluginSettingsHash, 0);
}
window.addEventListener('hashchange', _consumePluginSettingsHash);

function closePanel() {
  const closingPanelId = _currentPanelId;
  if (closingPanelId === 'settings' && window._memoryGraphDestroy) {
    window._memoryGraphDestroy();
  }
  if (typeof _stopDeepsleepPoll === 'function') _stopDeepsleepPoll();
  _currentPanelId = null;
  panel.classList.remove('open');
  document.querySelectorAll('.pill').forEach(b => b.classList.remove('active'));
}

document.getElementById('panel-close').addEventListener('click', closePanel);
window.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    if (panel.classList.contains('open')) {
      e.preventDefault();
      closePanel();
      return;
    }
    closePanel();
  }
});

function showToast(cmd) {
  toastCmd.textContent = cmd;
  toast.classList.add('show');
  clearTimeout(showToast.t);
  showToast.t = setTimeout(() => toast.classList.remove('show'), 2400);
}

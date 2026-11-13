// Panel: settings — operator profile, themes, voice, memory graph.
// Registry: PANELS.settings.body -> panelSettings
function panelSettings(b) {
  if (window._memoryGraphDestroy) window._memoryGraphDestroy();
  const E = _escapeHtml;
  const wrap = document.createElement('div');
  wrap.className = 'settings-layout';
  wrap.innerHTML = _settingsPanelMarkup(E);
  b.appendChild(wrap);
  _wireSettingsPanel(wrap, E);
}

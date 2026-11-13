// Installed-plugin handoff for the marketplace. Plugins are selected from the
// slash-command menu; there is no plugin picker in the chat composer.
(function () {
  'use strict';

  function acceptTryInChat(handoff) {
    if (!handoff || typeof handoff.plugin_id !== 'string' ||
        typeof handoff.intro_text !== 'string' || !handoff.intro_text.trim()) {
      throw new Error('plugin chat handoff invalid');
    }
    const input = document.getElementById('input');
    if (!input) throw new Error('chat input unavailable');
    input.value = '/plugin:' + handoff.plugin_id + ' ';
    input.dataset.pluginId = handoff.plugin_id;
    if (typeof closePanel === 'function') closePanel();
    if (typeof appendLine === 'function') appendLine('assistant', handoff.intro_text, true);
    input.focus();
  }

  window.OmegaPluginComposer = {acceptTryInChat};
})();

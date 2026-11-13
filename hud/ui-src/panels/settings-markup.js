// Settings: panel HTML template (_settingsPanelMarkup).
// Registry: panelSettings uses this for settings-layout innerHTML.
// Browser Agent decommissioned — removed from HUD.
function _settingsPanelMarkup(E) {
  return `    <div class="settings-functions" aria-label="settings functions">
  <button class="settings-function active" type="button" data-settings-view="operator_profile">operator profile<span>personas · operator</span></button>
  <button class="settings-function" type="button" data-settings-view="themes">themes<span>hud experience</span></button>
  <button class="settings-function" type="button" data-settings-view="voice">voice<span>TTS/STT configuration</span></button>
  <button class="settings-function" type="button" data-settings-view="cloud_model">cloud model<span>provider · model seat</span></button>
  <button class="settings-function" type="button" data-settings-view="proactive_history">proactive history<span>questions · results · notices</span></button>
  <button class="settings-function" type="button" data-settings-view="created_documents">created documents<span>generated document library</span></button>
  <button class="settings-function" type="button" data-settings-view="memory_graph">memory<span>visual node graph</span></button>
  <button class="settings-function" type="button" data-settings-view="workspace">workspace access<span>trusted folders</span></button>
  <button class="settings-function" type="button" data-settings-view="plugins">plugins<span>signed MCP apps</span></button>
</div>
<div class="settings-view active" data-settings-view-panel="operator_profile">
  <form class="settings-form" id="operator-profile-form">
    <div class="panel-label">operator profile</div>
    <p class="line bullet">Complete your profile, occupation, and work topics so Omega can store your persona and build the right learning agenda. Saving queues an explicit first learning episode whose foldback expands each topic into concrete mastery gaps; autonomous idle learning remains paused until you enable it.</p>
    <div class="settings-grid">
      <div class="settings-field"><label for="op-name">name</label><input id="op-name" name="name" type="text" autocomplete="name"></div>
      <div class="settings-field"><label for="op-preferred">preferred</label><input id="op-preferred" name="preferred_name" type="text"></div>
      <div class="settings-field"><label for="op-birthday">birthday</label><input id="op-birthday" name="birthday" type="text" autocomplete="bday"></div>
      <div class="settings-field"><label for="op-pronouns">pronouns</label><input id="op-pronouns" name="pronouns" type="text"></div>
      <div class="settings-field"><label for="op-location">location</label><input id="op-location" name="location" type="text" autocomplete="address-level2"></div>
      <div class="settings-field"><label for="op-timezone">timezone</label><input id="op-timezone" name="timezone" type="text"></div>
      <div class="settings-field wide"><label for="op-occupation">occupation</label><select id="op-occupation" name="occupation"><option value="">select occupation…</option></select></div>
      <div class="settings-field wide">
        <label for="op-subcategories-input">work topics to learn</label>
        <div class="settings-chip-input" id="op-subcategories-wrap">
          <div class="settings-chip-list" id="op-subcategories-chips"></div>
          <input id="op-subcategories-input" type="text" placeholder="type a topic and press Enter or comma…" autocomplete="off">
        </div>
      </div>
      <div class="settings-field wide"><label for="op-relationship">relationship</label><input id="op-relationship" name="relationship" type="text" placeholder="creator, owner, collaborator…"></div>
      <div class="settings-field wide"><label for="op-notes">notes</label><textarea id="op-notes" name="notes"></textarea></div>
    </div>
    <div class="settings-actions">
      <span class="settings-status" id="operator-profile-status">idle</span>
      <button class="verb approve" type="submit">save profile</button>
    </div>
  </form>
  <div class="settings-face-card" id="operator-face-memory-card">
    <div class="panel-label section">face memory</div>
    <p class="line bullet">Save a person to the vision lobe with a name and notes. The facial map runs on the photo before storage so Omega can recall names from faces and faces from names.</p>
    <div class="settings-field">
      <label for="face-upload-name">name</label>
      <input id="face-upload-name" type="text" placeholder="who is in the photo?">
    </div>
    <div class="settings-field wide">
      <label for="face-upload-context">info</label>
      <input id="face-upload-context" type="text" placeholder="relationship, role, notes Omega should remember">
    </div>
    <div class="settings-field">
      <label for="face-upload-file">photo</label>
      <input id="face-upload-file" type="file" accept="image/jpeg,image/png,image/webp">
    </div>
    <div class="settings-actions settings-face-actions">
      <span class="settings-status" id="operator-face-memory-status">idle</span>
      <button class="verb approve" type="button" id="operator-face-memory-upload">save face memory</button>
      <button class="verb" type="button" id="operator-face-memory-refresh">known names</button>
    </div>
  </div>
</div>
<div class="settings-view" data-settings-view-panel="themes">
  <form class="settings-form" id="hud-theme-form">
    <div class="panel-label">themes</div>
    <p class="line bullet">Choose a visual theme that suits your environment. Density affects spacing, motion controls animation smoothness, glow adjusts the ambient lighting effect, and panel depth changes the visual hierarchy.</p>
    <div class="theme-choice-grid" role="radiogroup" aria-label="HUD theme">
      <label class="theme-card"><input type="radio" name="theme" value="industrial"><span class="theme-swatch swatch-industrial"><span></span><span></span><span></span><span></span></span><span><span class="theme-card-title">industrial</span><span class="theme-card-detail">ink · bone · copper</span></span></label>
      <label class="theme-card"><input type="radio" name="theme" value="paper"><span class="theme-swatch swatch-paper"><span></span><span></span><span></span><span></span></span><span><span class="theme-card-title">paper</span><span class="theme-card-detail">light desk surface</span></span></label>
      <label class="theme-card"><input type="radio" name="theme" value="night_garden"><span class="theme-swatch swatch-night-garden"><span></span><span></span><span></span><span></span></span><span><span class="theme-card-title">night garden</span><span class="theme-card-detail">deep green · teal signal</span></span></label>
      <label class="theme-card"><input type="radio" name="theme" value="high_contrast"><span class="theme-swatch swatch-high-contrast"><span></span><span></span><span></span><span></span></span><span><span class="theme-card-title">high contrast</span><span class="theme-card-detail">black · white · signal gold</span></span></label>
        <label class="theme-card"><input type="radio" name="theme" value="ocean_deep"><span class="theme-swatch swatch-ocean-deep"><span></span><span></span><span></span><span></span></span><span><span class="theme-card-title">ocean deep</span><span class="theme-card-detail">navy depths · bioluminescent cyan</span></span></label>
        <label class="theme-card"><input type="radio" name="theme" value="sunset_amber"><span class="theme-swatch swatch-sunset-amber"><span></span><span></span><span></span><span></span></span><span><span class="theme-card-title">sunset amber</span><span class="theme-card-detail">warm amber · dusky rose</span></span></label>
        <label class="theme-card"><input type="radio" name="theme" value="arctic_mist"><span class="theme-swatch swatch-arctic-mist"><span></span><span></span><span></span><span></span></span><span><span class="theme-card-title">arctic mist</span><span class="theme-card-detail">cool blue-white · silver frost</span></span></label>
        <label class="theme-card"><input type="radio" name="theme" value="midnight_violet"><span class="theme-swatch swatch-midnight-violet"><span></span><span></span><span></span><span></span></span><span><span class="theme-card-title">midnight violet</span><span class="theme-card-detail">deep purple · electric accents</span></span></label>
    </div>
    <div class="settings-grid">
      <div class="settings-field"><label for="hud-density">density</label><select id="hud-density" name="density"><option value="comfortable">comfortable</option><option value="compact">compact</option><option value="spacious">spacious</option></select></div>
      <div class="settings-field"><label for="hud-motion">motion</label><select id="hud-motion" name="motion"><option value="full">full</option><option value="reduced">reduced</option></select></div>
      <div class="settings-field"><label for="hud-glow">glow</label><select id="hud-glow" name="glow"><option value="balanced">balanced</option><option value="quiet">quiet</option><option value="vivid">vivid</option></select></div>
      <div class="settings-field"><label for="hud-depth">panel depth</label><select id="hud-depth" name="panel_depth"><option value="standard">standard</option><option value="flat">flat</option><option value="floating">floating</option></select></div>
    </div>
    <div class="settings-actions">
      <span class="settings-status" id="hud-theme-status">loading</span>
      <button class="verb approve" type="submit">save theme</button>
    </div>
  </form>
</div>
<div class="settings-view" data-settings-view-panel="voice">
  <form class="settings-form" id="hud-voice-form">
    <div class="panel-label">voice & speech</div>
    <p class="line bullet">Configure text-to-speech for Omega's voice responses and speech-to-text for your voice commands. Adjust voice type, speed, and pitch to your preference. Wake word and audio processing settings control how Omega listens for commands.</p>
    <div class="panel-label section">text-to-speech</div>
    <div class="settings-grid">
      <div class="settings-field">
        <label for="hud-tts-enabled">TTS enabled</label>
        <select id="hud-tts-enabled" name="tts_enabled">
          <option value="true">on</option>
          <option value="false">off</option>
        </select>
      </div>
      <div class="settings-field">
        <label for="hud-tts-voice">Voice</label>
        <select id="hud-tts-voice" name="tts_voice">
          <option value="Aiden">Aiden · bright American English (default)</option>
          <option value="Ryan">Ryan · dynamic English</option>
          <option value="Vivian">Vivian · bright Chinese</option>
          <option value="Serena">Serena · gentle Chinese</option>
          <option value="Uncle_Fu">Uncle Fu · mellow Chinese</option>
          <option value="Dylan">Dylan · Beijing Chinese</option>
          <option value="Eric">Eric · Sichuan Chinese</option>
          <option value="Ono_Anna">Ono Anna · playful Japanese</option>
          <option value="Sohee">Sohee · warm Korean</option>
        </select>
      </div>
      <div class="settings-field">
        <label for="hud-tts-speed">Speed</label>
        <select id="hud-tts-speed" name="tts_speed">
          <option value="0.75">0.75x · slow</option>
          <option value="0.9">0.9x · relaxed</option>
          <option value="1.0">1.0x · normal</option>
          <option value="1.15">1.15x · brisk</option>
          <option value="1.3">1.3x · fast</option>
        </select>
      </div>
      <div class="settings-field">
        <label for="hud-tts-pitch">Pitch</label>
        <select id="hud-tts-pitch" name="tts_pitch">
          <option value="0.85">0.85x · lower</option>
          <option value="0.95">0.95x · slightly lower</option>
          <option value="1.0">1.0x · natural</option>
          <option value="1.05">1.05x · slightly higher</option>
          <option value="1.15">1.15x · higher</option>
        </select>
      </div>
      <div class="settings-field">
        <label for="hud-auto-speak">Auto-speak</label>
        <select id="hud-auto-speak" name="auto_speak">
          <option value="true">on</option>
          <option value="false">off</option>
        </select>
      </div>
    </div>
    <div class="panel-label section">speech-to-text</div>
    <div class="settings-grid">
      <div class="settings-field">
        <label for="hud-stt-enabled">STT enabled</label>
        <select id="hud-stt-enabled" name="stt_enabled">
          <option value="true">on</option>
          <option value="false">off</option>
        </select>
      </div>
      <div class="settings-field">
        <label for="hud-stt-language">Language</label>
        <select id="hud-stt-language" name="stt_language">
          <option value="auto">auto-detect</option>
          <option value="en">english</option>
          <option value="es">español</option>
          <option value="fr">français</option>
          <option value="de">deutsch</option>
          <option value="ja">日本語</option>
          <option value="zh">中文</option>
          <option value="ko">한국어</option>
          <option value="pt">português</option>
          <option value="it">italiano</option>
          <option value="hi">hindi</option>
        </select>
      </div>
      <div class="settings-field">
        <label for="hud-stt-task">Task</label>
        <select id="hud-stt-task" name="stt_task">
          <option value="transcribe">transcribe (original language)</option>
          <option value="translate">translate (to english)</option>
        </select>
      </div>
      <div class="settings-field">
        <label for="hud-stt-model">Model</label>
        <select id="hud-stt-model" name="stt_model">
          <option value="parakeet_tdt_06b_v3">parakeet-tdt-0.6b-v3 · CrispASR · portable</option>
        </select>
      </div>
    </div>
    <div class="panel-label section">wake word</div>
    <div class="settings-grid">
      <div class="settings-field">
        <label for="hud-wake-word-enabled">Wake word enabled</label>
        <select id="hud-wake-word-enabled" name="wake_word_enabled">
          <option value="true">on</option>
          <option value="false">off</option>
        </select>
      </div>
      <div class="settings-field wide">
        <label for="hud-wake-word">Wake word phrase</label>
        <input id="hud-wake-word" name="wake_word" type="text" placeholder="hey omega">
      </div>
    </div>
    <div class="panel-label section">audio processing</div>
    <div class="settings-grid">
      <div class="settings-field">
        <label for="hud-vad">Voice activity detection</label>
        <select id="hud-vad" name="voice_activity_detection">
          <option value="true">on</option>
          <option value="false">off</option>
        </select>
      </div>
      <div class="settings-field">
        <label for="hud-noise-cancellation">Noise cancellation</label>
        <select id="hud-noise-cancellation" name="noise_cancellation">
          <option value="true">on</option>
          <option value="false">off</option>
        </select>
      </div>
      <div class="settings-field">
        <label for="hud-live-mode">Live conversation</label>
        <select id="hud-live-mode" name="live_mode">
          <option value="true">on</option>
          <option value="false">off</option>
        </select>
      </div>
    </div>
    <div class="settings-actions">
      <span class="settings-status" id="hud-voice-status">loading</span>
      <button class="verb" id="hud-tts-preview" type="button">test voice</button>
      <button class="verb approve" type="submit">save voice</button>
    </div>
  </form>
</div>
<div class="settings-view" data-settings-view-panel="cloud_model">
  <form class="settings-form" id="mind-backend-form">
    <div class="panel-label">cloud model</div>
    <p class="line bullet">Choose a cloud provider, paste its API key, and select the model seat Omega should use. The key is encrypted locally and never returned to the HUD. LM Studio remains the separate local option.</p>
    <div class="settings-grid">
      <div class="settings-field wide">
        <label for="mind-backend-provider">provider</label>
        <select id="mind-backend-provider" name="provider"><option value="">loading providers…</option></select>
      </div>
      <div class="settings-field wide">
        <label for="mind-backend-base-url">endpoint</label>
        <input id="mind-backend-base-url" name="base_url" type="url" placeholder="provider endpoint">
      </div>
      <div class="settings-field wide">
        <label for="mind-backend-model">model seat</label>
        <input id="mind-backend-model" name="model" type="text" list="mind-backend-model-options" placeholder="choose or enter a model id" autocomplete="off">
        <datalist id="mind-backend-model-options"></datalist>
      </div>
      <div class="settings-field wide">
        <label for="mind-backend-api-key">API key</label>
        <input id="mind-backend-api-key" name="api_key" type="password" placeholder="paste once · encrypted on save" autocomplete="new-password">
        <button class="verb" id="mind-backend-save-key" type="button">save key securely</button>
        <small class="settings-hint">Optional. Saves this provider key in Omega’s encrypted portable vault for future boots.</small>
      </div>
    </div>
    <div class="settings-actions">
      <span class="settings-status" id="mind-backend-status">loading</span>
      <button class="verb" id="mind-backend-test" type="button">test connection</button>
      <button class="verb approve" type="submit">use cloud model</button>
    </div>
  </form>
</div>
<div class="settings-view" data-settings-view-panel="proactive_history">
  <!-- Lazy-rendered by _renderSettingsProactiveHistory when selected. -->
</div>
<div class="settings-view" data-settings-view-panel="created_documents">
  <!-- Lazy-rendered by _renderSettingsCreatedDocuments when tab is selected -->
</div>
<div class="settings-view" data-settings-view-panel="memory_graph">
  <div class="panel-label">memory</div>
  <p class="line bullet">Visual representation of your memory graph. Nodes represent memories and connections show how they relate. Click any node to see its details. Use this to understand how Omega organizes and connects information over time.</p>
  <div class="memory-graph-container" style="width:100%;height:480px;position:relative;overflow:hidden;">
    <div class="memory-graph-loading" id="mg-loading">
      <div class="loading-ring"></div>
      <div class="loading-text">INDEXING MEMORY</div>
      <div class="loading-shimmer"></div>
    </div>
    <!-- MemoryGraph JS will mount SVG and controls here -->
  </div>
  <div class="memory-graph-detail-box" data-memory-graph-detail style="margin-top:12px;min-height:160px;max-height:320px;overflow:auto;border:1px solid rgba(196,133,94,0.18);border-radius:12px;background:rgba(12,12,14,0.72);box-shadow:inset 0 1px 0 rgba(255,255,255,0.04);">
    <div style="padding:16px;color:rgba(212,207,199,0.58);font-family:var(--mono, monospace);font-size:12px;">Select a terminal memory node to read the saved memory file.</div>
  </div>
</div>
<div class="settings-view" data-settings-view-panel="workspace">
  <div class="panel-label">workspace access</div>
  <p class="line bullet">Omega only works with folders you explicitly trust. Add a directory, choose the active root for relative paths, and remove grants any time. Omega's own product tree and its parent drive are never grantable.</p>
  <div class="settings-field wide">
    <label for="trusted-folder-path">directory path</label>
    <input id="trusted-folder-path" type="text" placeholder="Projects\\Client or a full path">
  </div>
  <div class="settings-actions">
    <span class="settings-status" id="trusted-folder-status">loading</span>
    <button class="verb approve" type="button" id="trusted-folder-add">trust folder</button>
    <button class="verb" type="button" id="trusted-folder-refresh">refresh</button>
  </div>
  <div id="trusted-folder-list" class="settings-list" aria-live="polite"></div>
</div>
<div class="settings-view" data-settings-view-panel="plugins">
  <!-- Lazy-rendered by _renderSettingsPlugins when selected. -->
</div>
`;
}

// Settings: tab switching, operator profile, face memory, theme/voice form wiring.
function wireCloudProviderSettingsForm(wrap) {
  const form = wrap.querySelector('#mind-backend-form');
  if (!form) return;
  const providerSelect = form.querySelector('#mind-backend-provider');
  const endpointInput = form.querySelector('#mind-backend-base-url');
  const modelInput = form.querySelector('#mind-backend-model');
  const modelOptions = form.querySelector('#mind-backend-model-options');
  const keyInput = form.querySelector('#mind-backend-api-key');
  const saveKeyButton = form.querySelector('#mind-backend-save-key');
  const testButton = form.querySelector('#mind-backend-test');
  const status = form.querySelector('#mind-backend-status');
  let providers = [];
  let currentHasKey = false;
  let savedKeyProviders = new Set();

  const setStatus = (text, kind) => {
    status.className = 'settings-status' + (kind ? ' ' + kind : '');
    status.textContent = text;
  };
  const providerFor = id => providers.find(item => item.id === id);
  const renderModels = provider => {
    modelOptions.replaceChildren();
    const models = provider && Array.isArray(provider.models) ? provider.models : [];
    models.forEach(item => {
      const option = document.createElement('option');
      option.value = item;
      modelOptions.appendChild(option);
    });
  };
  const renderProviders = selectedId => {
    providerSelect.replaceChildren();
    providers.forEach(provider => {
      const option = document.createElement('option');
      option.value = provider.id;
      option.textContent = (provider.icon ? provider.icon + ' ' : '') + provider.name;
      providerSelect.appendChild(option);
    });
    if (providerFor(selectedId)) providerSelect.value = selectedId;
    if (!providerSelect.value && providers[0]) providerSelect.value = providers[0].id;
    const provider = providerFor(providerSelect.value);
    renderModels(provider);
    return provider;
  };

  providerSelect.addEventListener('change', () => {
    const provider = providerFor(providerSelect.value);
    if (!provider) return;
    endpointInput.value = provider.base_url || '';
    renderModels(provider);
    keyInput.value = '';
    currentHasKey = savedKeyProviders.has(provider.id);
    setStatus(provider.description || 'provider selected');
  });

  saveKeyButton.addEventListener('click', async () => {
    const provider = providerFor(providerSelect.value);
    const key = keyInput.value.trim();
    if (!provider) {
      setStatus('choose a provider', 'fail');
      return;
    }
    if (!key) {
      setStatus('enter an API key to save', 'fail');
      return;
    }
    saveKeyButton.disabled = true;
    setStatus('encrypting key securely');
    try {
      const response = await fetch('/api/settings/mind-backend/key', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ provider: provider.id, api_key: key }),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'key save failed');
      savedKeyProviders = new Set(
        Array.isArray(result.saved_key_providers) ? result.saved_key_providers : [],
      );
      currentHasKey = savedKeyProviders.has(provider.id);
      keyInput.value = '';
      setStatus('key saved securely · ' + provider.name, 'ok');
    } catch (err) {
      setStatus('failed · ' + (err && err.message ? err.message.slice(0, 80) : 'key save failed'), 'fail');
    } finally {
      saveKeyButton.disabled = false;
    }
  });

  testButton.addEventListener('click', async () => {
    const provider = providerFor(providerSelect.value);
    if (!provider) {
      setStatus('choose a provider', 'fail');
      return;
    }
    if (!endpointInput.value.trim()) {
      setStatus('endpoint required', 'fail');
      return;
    }
    if (provider.api_key_envs && provider.api_key_envs.length && !keyInput.value.trim() && !currentHasKey) {
      setStatus('API key required', 'fail');
      return;
    }
    testButton.disabled = true;
    setStatus('testing connection');
    try {
      const response = await fetch('/api/settings/mind-backend/test', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({
          provider: provider.id,
          base_url: endpointInput.value.trim(),
          model: modelInput.value.trim(),
          api_key: keyInput.value.trim(),
        }),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'connection failed');
      const models = Array.isArray(result.models) ? result.models : [];
      modelOptions.replaceChildren();
      models.forEach(item => {
        const option = document.createElement('option');
        option.value = item.id;
        modelOptions.appendChild(option);
      });
      setStatus('connected · ' + models.length + ' models found', 'ok');
    } catch (err) {
      setStatus('failed · ' + (err && err.message ? err.message.slice(0, 80) : 'connection failed'), 'fail');
    } finally {
      testButton.disabled = false;
    }
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    const provider = providerFor(providerSelect.value);
    const endpoint = endpointInput.value.trim();
    const model = modelInput.value.trim();
    const key = keyInput.value.trim();
    if (!provider || !endpoint || !model) {
      setStatus('provider, endpoint, and model are required', 'fail');
      return;
    }
    if (key) {
      setStatus('save the key securely first, or clear it to use an existing saved key', 'fail');
      return;
    }
    if (provider.api_key_envs && provider.api_key_envs.length && !key && !currentHasKey) {
      setStatus('API key required', 'fail');
      return;
    }
    const payload = { backend: 'cloud', provider: provider.id, base_url: endpoint, model };
    if (key) payload.api_key = key;
    setStatus('saving');
    try {
      const response = await fetch('/api/settings/mind-backend', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'save failed');
      currentHasKey = Boolean(result.connector && result.connector.has_key);
      if (result.connector && Array.isArray(result.connector.saved_key_providers)) {
        savedKeyProviders = new Set(result.connector.saved_key_providers);
      }
      keyInput.value = '';
      setStatus('saved · ' + provider.name + ' · ' + model, 'ok');
    } catch (err) {
      setStatus('failed · ' + (err && err.message ? err.message.slice(0, 80) : 'save failed'), 'fail');
    }
  });

  if (!_liveHttp()) {
    setStatus('HTTP origin required', 'fail');
    return;
  }
  Promise.all([
    fetch('/api/settings/cloud-providers', { cache: 'no-store' }).then(response => response.json()),
    fetch('/api/settings/mind-backend', { cache: 'no-store' }).then(response => response.json()),
  ]).then(([catalog, current]) => {
    if (!catalog || !catalog.ok || !Array.isArray(catalog.providers) || !catalog.providers.length) {
      throw new Error('cloud provider catalog unavailable');
    }
    providers = catalog.providers;
    const connector = current && current.ok ? (current.connector || {}) : {};
    savedKeyProviders = new Set(
      Array.isArray(connector.saved_key_providers) ? connector.saved_key_providers : [],
    );
    currentHasKey = connector.has_key === true || savedKeyProviders.has(connector.provider || '');
    const provider = renderProviders(connector.provider || '');
    endpointInput.value = connector.base_url || (provider && provider.base_url) || '';
    modelInput.value = connector.model || '';
    setStatus(currentHasKey ? 'ready · key saved' : 'choose a provider and connect');
  }).catch(err => {
    setStatus('failed · ' + (err && err.message ? err.message.slice(0, 80) : 'catalog unavailable'), 'fail');
  });
}

function wireVoiceSettingsForm(voiceForm, voiceStatus, voicePreview) {
  const voiceSubmit = voiceForm.querySelector('[type="submit"]');
  let voiceDirty = false;
  let voiceRevision = 0;
  setVoiceForm(voiceForm, currentVoiceSettings());
  voiceForm.addEventListener('input', () => {
    voiceDirty = true;
    voiceRevision += 1;
    advanceVoiceSettingsRevision();
    const settings = collectVoiceForm(voiceForm);
    applyVoiceSettings(settings);
    saveLocalVoiceSettings(settings);
    voiceStatus.className = 'settings-status';
    voiceStatus.textContent = 'previewing';
  });
  voicePreview.addEventListener('click', () => {
    advanceVoiceSettingsRevision();
    const settings = collectVoiceForm(voiceForm);
    applyVoiceSettings(settings);
    saveLocalVoiceSettings(settings);
    const started = previewVoiceSettings();
    voiceStatus.className = started ? 'settings-status ok' : 'settings-status fail';
    voiceStatus.textContent = started ? 'playing preview' : 'turn TTS on to preview';
  });
  voiceForm.addEventListener('submit', async ev => {
    ev.preventDefault();
    if (voiceForm.dataset.saving === 'true') return;
    voiceForm.dataset.saving = 'true';
    voiceSubmit.disabled = true;
    const submittedSettingsRevision = advanceVoiceSettingsRevision();
    const settings = collectVoiceForm(voiceForm);
    const submittedVoiceRevision = voiceRevision;
    applyVoiceSettings(settings);
    saveLocalVoiceSettings(settings);
    voiceStatus.className = 'settings-status';
    try {
      if (!_liveHttp()) {
        applyVoiceSettings(settings);
        saveLocalVoiceSettings(settings);
        // Sync with Voice object - disable if STT was turned off
        const voice = document.getElementById('voice');
        if (voice && voice.classList.contains('active') && !settings.stt_enabled) {
          voice.classList.remove('active');
          if (typeof Voice !== 'undefined') Voice.disable();
          if (typeof appendLine === 'function') {
            appendLine('system', 'voice input disabled · voice pill deactivated');
          }
        }
        voiceDirty = false;
        voiceStatus.textContent = 'saved locally';
        voiceStatus.classList.add('ok');
        return;
      }
      voiceStatus.textContent = 'saving';
      const r = await fetch('/api/settings/voice', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify(settings),
      });
      const result = await r.json();
      if (!r.ok || !result.ok) {
        throw new Error(result.detail || result.error || 'voice save failed');
      }
      if (
        voiceRevision !== submittedVoiceRevision
        || voiceSettingsRevision() !== submittedSettingsRevision
      ) {
        voiceStatus.className = 'settings-status';
        voiceStatus.textContent = 'saved prior edit · save current changes';
        return;
      }
      const saved = result.settings || settings;
      advanceVoiceSettingsRevision();
      applyVoiceSettings(saved);
      setVoiceForm(voiceForm, saved);
      saveLocalVoiceSettings(saved);
      voiceDirty = false;
      // Sync with Voice object - disable if STT was turned off
      const voice = document.getElementById('voice');
      if (voice && voice.classList.contains('active') && !saved.stt_enabled) {
        voice.classList.remove('active');
        if (typeof Voice !== 'undefined') Voice.disable();
        if (typeof appendLine === 'function') {
          appendLine('system', 'voice input disabled · voice pill deactivated');
        }
      }
      voiceStatus.textContent = 'saved · ' + (saved.tts_voice || 'voice');
      voiceStatus.classList.add('ok');
    } catch (err) {
      voiceStatus.textContent = 'failed · ' + (err && err.message ? err.message.slice(0, 64) : 'unknown');
      voiceStatus.classList.add('fail');
    } finally {
      voiceForm.dataset.saving = 'false';
      voiceSubmit.disabled = false;
    }
  });
  hydrateVoiceForm(voiceForm, voiceStatus, () => voiceDirty);
}

function _wireTrustedFoldersPanel(wrap, E) {
  const panel = wrap.querySelector('[data-settings-view-panel="workspace"]');
  if (!panel || panel.dataset.wired === 'true') return;
  panel.dataset.wired = 'true';
  const list = panel.querySelector('#trusted-folder-list');
  const pathInput = panel.querySelector('#trusted-folder-path');
  const status = panel.querySelector('#trusted-folder-status');
  const setStatus = (text, kind) => {
    if (!status) return;
    status.textContent = text;
    status.className = 'settings-status' + (kind ? ' ' + kind : '');
  };
  const render = result => {
    if (!list) return;
    list.replaceChildren();
    const folders = Array.isArray(result && result.folders) ? result.folders : [];
    if (!folders.length) {
      const empty = document.createElement('div');
      empty.className = 'line bullet';
      empty.textContent = 'No trusted folders configured.';
      list.appendChild(empty);
      return;
    }
    folders.forEach(folder => {
      const path = String(folder && folder.path || '');
      const row = document.createElement('div');
      row.className = 'settings-list-row';
      const label = document.createElement('span');
      label.className = 'settings-list-path';
      label.textContent = path;
      row.appendChild(label);
      if (String(result && result.active || '') === path) {
        const active = document.createElement('span');
        active.className = 'settings-status ok';
        active.textContent = 'active';
        row.appendChild(active);
      } else {
        const select = document.createElement('button');
        select.className = 'verb';
        select.type = 'button';
        select.textContent = 'use';
        select.addEventListener('click', () => mutate('select', path));
        row.appendChild(select);
      }
      const remove = document.createElement('button');
      remove.className = 'verb';
      remove.type = 'button';
      remove.textContent = 'remove';
      remove.addEventListener('click', () => mutate('revoke', path));
      row.appendChild(remove);
      list.appendChild(row);
    });
  };
  const refresh = async () => {
    setStatus('loading');
    try {
      const response = await fetch('/api/settings/trusted-folders');
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.error || 'load failed');
      render(result);
      const folders = Array.isArray(result.folders) ? result.folders : [];
      if (result.active) {
        setStatus('ready');
      } else if (folders.length) {
        setStatus('select a folder to enable tools', 'fail');
      } else {
        setStatus('grant a folder to enable tools', result.required ? 'fail' : '');
      }
    } catch (err) {
      setStatus('failed · ' + String(err && err.message || err).slice(0, 64), 'fail');
    }
  };
  const mutate = async (action, path) => {
    setStatus(action === 'grant' ? 'trusting…' : 'saving…');
    try {
      const response = await fetch('/api/settings/trusted-folders', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ action, path }),
      });
      const result = await response.json();
      if (!response.ok || !result.ok) throw new Error(result.detail || result.error || 'save failed');
      if (action === 'grant' && pathInput) pathInput.value = '';
      await refresh();
    } catch (err) {
      setStatus('failed · ' + String(err && err.message || err).slice(0, 64), 'fail');
    }
  };
  const add = panel.querySelector('#trusted-folder-add');
  if (add) add.addEventListener('click', () => {
    const path = String(pathInput && pathInput.value || '').trim();
    if (!path) { setStatus('enter a directory path', 'fail'); return; }
    mutate('grant', path);
  });
  const refreshButton = panel.querySelector('#trusted-folder-refresh');
  if (refreshButton) refreshButton.addEventListener('click', refresh);
  // A /trust, /cd or /untrust typed in chat changes the same store this panel shows.
  if (typeof window !== 'undefined') window.OmegaTrustedFolders = { refresh };
  refresh();
}

function _wireSettingsPanel(wrap, E) {
  _wireTrustedFoldersPanel(wrap, E);
  wrap.querySelectorAll('[data-settings-view]').forEach(btn => {
    btn.addEventListener('click', () => {
      const id = btn.dataset.settingsView;
      wrap.querySelectorAll('[data-settings-view]').forEach(x => x.classList.toggle('active', x === btn));
      wrap.querySelectorAll('[data-settings-view-panel]').forEach(x => x.classList.toggle('active', x.dataset.settingsViewPanel === id));
      // Memory graph lifecycle management
      if (id === 'memory_graph') {
        requestAnimationFrame(() => { if (window._memoryGraphInit) window._memoryGraphInit(); });
      } else if (window._memoryGraphDestroy) {
        window._memoryGraphDestroy();
      }
      if (id === 'created_documents') {
        const panel = wrap.querySelector('[data-settings-view-panel="created_documents"]');
        if (panel) {
          panel.innerHTML = '';
          _renderSettingsCreatedDocuments(panel).catch(err => {
            panel.innerHTML =
              '<div class="gov-trail-row"><span class="result fail">created documents render failed: ' +
              _escapeHtml(String(err && err.message || err)) + '</span></div>';
          });
        }
      }
      if (id === 'proactive_history') {
        const panel = wrap.querySelector('[data-settings-view-panel="proactive_history"]');
        if (panel) {
          _renderSettingsProactiveHistory(panel).catch(err => {
            panel.innerHTML =
              '<div class="gov-trail-row"><span class="result fail">proactive history render failed: ' +
              _escapeHtml(String(err && err.message || err)) + '</span></div>';
          });
        }
      }
      if (id === 'plugins') {
        const panel = wrap.querySelector('[data-settings-view-panel="plugins"]');
        if (panel) _renderSettingsPlugins(panel);
      }
    });
  });

  const form = wrap.querySelector('#operator-profile-form');
  const status = wrap.querySelector('#operator-profile-status');
  const occupationSelect = wrap.querySelector('#op-occupation');
  const subcategoriesChips = wrap.querySelector('#op-subcategories-chips');
  const subcategoriesInput = wrap.querySelector('#op-subcategories-input');
  const occupationById = new Map();
  let subcategoriesTouched = false;
  let subcategoryValues = [];

  const renderSubcategoryChips = () => {
    if (!subcategoriesChips) return;
    subcategoriesChips.innerHTML = subcategoryValues.map((value, index) =>
      `<span class="settings-chip" data-chip-index="${index}">` +
      `${E(value)}<button type="button" aria-label="remove ${E(value)}">×</button></span>`
    ).join('');
  };

  const addSubcategory = raw => {
    const cleaned = String(raw || '').trim().replace(/,+$/g, '');
    if (!cleaned) return;
    const key = cleaned.toLowerCase();
    if (subcategoryValues.some(v => v.toLowerCase() === key)) return;
    subcategoriesTouched = true;
    subcategoryValues.push(cleaned);
    renderSubcategoryChips();
  };

  const removeSubcategory = index => {
    if (index < 0 || index >= subcategoryValues.length) return;
    subcategoriesTouched = true;
    subcategoryValues.splice(index, 1);
    renderSubcategoryChips();
  };

  const applyOccupationHints = occupationId => {
    if (subcategoriesTouched) return;
    const occ = occupationById.get(occupationId);
    const hints = occ && Array.isArray(occ.subcategory_hints) ? occ.subcategory_hints : [];
    subcategoryValues = hints.map(h => String(h || '').trim()).filter(Boolean);
    renderSubcategoryChips();
  };

  if (subcategoriesChips) {
    subcategoriesChips.addEventListener('click', ev => {
      const btn = ev.target.closest('button');
      if (!btn) return;
      const chip = btn.closest('.settings-chip');
      if (!chip) return;
      const index = Number(chip.dataset.chipIndex);
      if (!Number.isNaN(index)) removeSubcategory(index);
    });
  }

  if (subcategoriesInput) {
    subcategoriesInput.addEventListener('keydown', ev => {
      if (ev.key === 'Enter' || ev.key === ',') {
        ev.preventDefault();
        addSubcategory(subcategoriesInput.value);
        subcategoriesInput.value = '';
      } else if (ev.key === 'Backspace' && !subcategoriesInput.value && subcategoryValues.length) {
        removeSubcategory(subcategoryValues.length - 1);
      }
    });
    subcategoriesInput.addEventListener('blur', () => {
      if (subcategoriesInput.value.trim()) {
        addSubcategory(subcategoriesInput.value);
        subcategoriesInput.value = '';
      }
    });
  }

  if (occupationSelect) {
    occupationSelect.addEventListener('change', () => {
      applyOccupationHints(occupationSelect.value);
    });
  }

  const hydrateOccupationOptions = async () => {
    if (!occupationSelect || !_liveHttp()) return;
    try {
      const r = await fetch('/api/settings/occupation-options', { cache: 'no-store' });
      if (!r.ok) throw new Error('occupation options unavailable');
      const result = await r.json();
      const occupations = Array.isArray(result.occupations) ? result.occupations : [];
      occupationById.clear();
      const options = ['<option value="">select occupation…</option>'];
      for (const occ of occupations) {
        if (!occ || !occ.id) continue;
        occupationById.set(occ.id, occ);
        options.push(`<option value="${E(occ.id)}">${E(occ.label || occ.id)}</option>`);
      }
      occupationSelect.innerHTML = options.join('');
    } catch (err) {
      if (status) {
        status.className = 'settings-status fail';
        status.textContent = 'occupations failed · ' + (err && err.message ? err.message.slice(0, 48) : 'unknown');
      }
    }
  };
  hydrateOccupationOptions();

  // Onboarding is one-shot: once a profile is saved the form LOCKS — you can't create or
  // save another (that's what caused the double-decompose). The server enforces this too;
  // this is the matching UI state. Releasing the lock = a day-zero reset.
  const setFormLocked = locked => {
    form.querySelectorAll('input, select, textarea, button').forEach(el => {
      el.disabled = !!locked;
    });
    if (subcategoriesInput) subcategoriesInput.disabled = !!locked;
    form.classList.toggle('is-locked', !!locked);
  };
  const applyLockIfOnboarded = async () => {
    try {
      if (typeof OperatorIdentity === 'undefined') return;
      if (OperatorIdentity.fetchIdentity) await OperatorIdentity.fetchIdentity().catch(() => { });
      if (OperatorIdentity.isOnboarded && OperatorIdentity.isOnboarded()) {
        setFormLocked(true);
        if (status) {
          status.className = 'settings-status ok';
          status.textContent = 'profile locked · onboarding complete (day-zero to reset)';
        }
      }
    } catch (_) { /* non-fatal — leave the form editable */ }
  };
  applyLockIfOnboarded();

  form.addEventListener('submit', async ev => {
    ev.preventDefault();
    if (form.classList.contains('is-locked')) return;  // already onboarded -> locked
    const payload = {};
    const data = new FormData(form);
    for (const [key, value] of data.entries()) {
      const cleaned = String(value || '').trim();
      if (cleaned) payload[key] = cleaned;
    }
    if (subcategoriesInput && subcategoriesInput.value.trim()) {
      addSubcategory(subcategoriesInput.value);
      subcategoriesInput.value = '';
    }
    if (subcategoryValues.length) payload.subcategories = subcategoryValues.slice();
    status.className = 'settings-status';
    if (!Object.keys(payload).length) {
      status.textContent = 'nothing to save';
      status.classList.add('fail');
      return;
    }
    if (!_liveHttp()) {
      status.textContent = 'http origin required';
      status.classList.add('fail');
      return;
    }
    status.textContent = 'saving';
    try {
      const r = await fetch('/api/settings/operator-profile', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const result = await r.json();
      if (result && result.locked) {  // server rejected a re-save — already onboarded
        setFormLocked(true);
        status.className = 'settings-status ok';
        status.textContent = 'profile locked · already onboarded (day-zero to reset)';
        return;
      }
      if (!r.ok || !result.ok) {
        throw new Error(result.detail || result.error || (result.memory && result.memory.message) || 'write rejected');
      }
      const fields = (result.accepted_fields || []).map(E).join(' · ');
      const seed = result.onboarding_seed || {};
      const seeded = Array.isArray(seed.seeded) ? seed.seeded : [];
      const seedBits = seeded.map(item => item.subcategory || item.dirname).filter(Boolean);
      let statusText = 'saved · ' + (fields || 'profile');
      if (seedBits.length) statusText += ' · seeded ' + seedBits.join(' · ');
      if (Array.isArray(seed.failed) && seed.failed.length) {
        statusText += ' · ' + seed.failed.length + ' seed failed';
      }
      status.textContent = statusText;
      status.classList.add('ok');
      if (typeof OmegaUI !== 'undefined') {
        if (result.acknowledgement) {
          OmegaUI.appendLine('assistant', result.acknowledgement);
        } else {
          let line = 'operator profile saved to personas/operator · ' + (fields || 'profile');
          if (seedBits.length) line += ' · seeded ' + seedBits.join(' · ');
          OmegaUI.appendLine('system', line);
        }
      }
      if (seedBits.length && typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('omega:memory', {
          detail: {
            op: 'subdomain.created',
            status: 'ok',
            seeded: seeded,
          },
        }));
        if (typeof window._memoryGraphRefresh === 'function') {
          window._memoryGraphRefresh();
        }
      }
      if (typeof OperatorIdentity !== 'undefined' && OperatorIdentity.fetchIdentity) {
        OperatorIdentity.fetchIdentity().catch(() => { });
      }
      if (typeof clearFirstBootOperatorProfileChat === 'function') {
        clearFirstBootOperatorProfileChat();
      }
      // One-shot: lock the form immediately this session so a second save can't fire.
      setFormLocked(true);
      status.textContent = statusText + ' · locked';
    } catch (err) {
      status.textContent = 'failed · ' + (err && err.message ? err.message.slice(0, 64) : 'unknown');
      status.classList.add('fail');
    }
  });

  const faceStatus = wrap.querySelector('#operator-face-memory-status');
  const faceUpload = wrap.querySelector('#operator-face-memory-upload');
  const faceRefresh = wrap.querySelector('#operator-face-memory-refresh');
  const faceNameInput = wrap.querySelector('#face-upload-name');
  const faceContextInput = wrap.querySelector('#face-upload-context');
  const faceFileInput = wrap.querySelector('#face-upload-file');
  const setFaceStatus = (text, kind) => {
    if (!faceStatus) return;
    faceStatus.className = 'settings-status' + (kind ? ' ' + kind : '');
    faceStatus.textContent = text;
  };
  if (faceUpload) {
    faceUpload.addEventListener('click', async () => {
      const opNameEl = wrap.querySelector('#op-name');
      const profileName = opNameEl && opNameEl.value ? opNameEl.value.trim() : '';
      const name = (faceNameInput && faceNameInput.value || profileName || '').trim();
      const context = (faceContextInput && faceContextInput.value || '').trim();
      const file = faceFileInput && faceFileInput.files && faceFileInput.files[0];

      if (!name) {
        setFaceStatus('name required', 'fail');
        return;
      }
      if (!file) {
        setFaceStatus('select a photo', 'fail');
        return;
      }

      try {
        const st = await fetch('/api/vision/status', { cache: 'no-store' });
        const stBody = st.ok ? await st.json() : null;
        if (!stBody || !stBody.face_ready) {
          const why = (stBody && (stBody.reason || stBody.error)) || 'face runtime not ready';
          const rt = (stBody && stBody.runtime) || 'http://127.0.0.1:11434';
          setFaceStatus('face runtime down · start ' + rt + ' · ' + why, 'fail');
          return;
        }
      } catch (_) {
        setFaceStatus('face runtime status unavailable', 'fail');
        return;
      }

      setFaceStatus('mapping face · saving…', '');
      faceUpload.disabled = true;

      try {
        const formData = new FormData();
        formData.append('file', file);
        formData.append('name', name);
        if (context) formData.append('context', context);

        const r = await fetch('/api/vision/upload-face', {
          method: 'POST',
          body: formData
        });
        const result = await r.json();

        if (result && result.ok) {
          const savedName = result.name || name;
          setFaceStatus('queued · ' + savedName + ' · result will appear in chat', '');
          if (typeof OmegaUI !== 'undefined') {
            OmegaUI.appendLine('system', 'face memory queued for ' + savedName);
          }
          renderChatResult(result, null);
          if (faceNameInput) faceNameInput.value = '';
          if (faceContextInput) faceContextInput.value = '';
          if (faceFileInput) faceFileInput.value = '';
        } else {
          setFaceStatus('failed · ' + ((result && result.error) || 'unknown error'), 'fail');
        }
      } catch (err) {
        setFaceStatus('upload failed · ' + (err && err.message || 'network error'), 'fail');
      } finally {
        faceUpload.disabled = false;
      }
    });
  }
  if (faceRefresh) {
    faceRefresh.addEventListener('click', async () => {
      if (!_liveHttp()) {
        setFaceStatus('http origin required', 'fail');
        return;
      }
      setFaceStatus('loading names', '');
      try {
        const r = await fetch('/api/vision/anchors', { cache: 'no-store' });
        const result = await r.json();
        if (!r.ok || !result.ok) throw new Error(result.detail || result.error || 'anchors unavailable');
        const names = Array.isArray(result.anchors) ? result.anchors.map(a => a.name) : [];
        setFaceStatus(names.length ? 'known · ' + names.join(' · ') : 'no face memories yet', names.length ? 'ok' : '');
      } catch (err) {
        setFaceStatus('failed · ' + (err && err.message ? err.message.slice(0, 64) : 'unknown'), 'fail');
      }
    });
  }

  const themeForm = wrap.querySelector('#hud-theme-form');
  const themeStatus = wrap.querySelector('#hud-theme-status');
  setHudThemeForm(themeForm, currentHudThemeSettings());
  themeForm.addEventListener('input', () => {
    const settings = collectHudThemeForm(themeForm);
    applyHudThemeSettings(settings);
    saveLocalHudThemeSettings(settings);
    themeStatus.className = 'settings-status';
    themeStatus.textContent = 'previewing';
  });
  themeForm.addEventListener('submit', async ev => {
    ev.preventDefault();
    const settings = collectHudThemeForm(themeForm);
    applyHudThemeSettings(settings);
    saveLocalHudThemeSettings(settings);
    themeStatus.className = 'settings-status';
    if (!_liveHttp()) {
      themeStatus.textContent = 'saved locally';
      themeStatus.classList.add('ok');
      return;
    }
    themeStatus.textContent = 'saving';
    try {
      const r = await fetch('/api/settings/hud-theme', {
        method: 'POST', headers: { 'content-type': 'application/json' },
        body: JSON.stringify(settings),
      });
      const result = await r.json();
      if (!r.ok || !result.ok) {
        throw new Error(result.detail || result.error || 'theme save failed');
      }
      const saved = result.settings || settings;
      applyHudThemeSettings(saved);
      setHudThemeForm(themeForm, saved);
      saveLocalHudThemeSettings(saved);
      themeStatus.textContent = 'saved · ' + (saved.theme || 'theme');
      themeStatus.classList.add('ok');
    } catch (err) {
      themeStatus.textContent = 'failed · ' + (err && err.message ? err.message.slice(0, 64) : 'unknown');
      themeStatus.classList.add('fail');
    }
  });
  hydrateHudThemeForm(themeForm, themeStatus);

  const voiceForm = wrap.querySelector('#hud-voice-form');
  const voiceStatus = wrap.querySelector('#hud-voice-status');
  const voicePreview = wrap.querySelector('#hud-tts-preview');
  wireVoiceSettingsForm(voiceForm, voiceStatus, voicePreview);
  wireCloudProviderSettingsForm(wrap);
}

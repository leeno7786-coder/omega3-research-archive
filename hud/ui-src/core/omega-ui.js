// Composer actions, skill picker, OmegaUI helpers (hud/ui-src/core/omega-ui.js)
// Depends on: appendLine, _escapeHtml, appendDocumentMessage, window.Build, Voice, openPanel

(function () {
  'use strict';
      // ── UI-1: Composer action row — Plugins, document, upload ─────
      // Inject the action buttons next to the .voice pill. Click
      // open small inline forms (no modal — keeps the surface minimal).
      function injectComposerActions() {
        const composer = document.querySelector('.composer');
        if (!composer) return;
        let wrap = composer.querySelector('.composer-actions');
        if (wrap && wrap.dataset.omegaUiBound === '1') return;

        // Style block — matches the design language (uppercase, mono,
        // hairline border, hover fill). Scoped class name avoids collision.
        if (!document.getElementById('omega-composer-actions-style')) {
          const style = document.createElement('style');
          style.id = 'omega-composer-actions-style';
          style.textContent = `
      .composer-actions{display:flex;gap:6px;flex-shrink:0;margin-left:2px}
       .composer-action{cursor:pointer;font:10px/1 var(--mono);
         display:inline-flex;align-items:center;justify-content:center;gap:6px;
         padding:8px 10px;min-height:40px;text-transform:uppercase;letter-spacing:.14em;
         color:var(--whisper);border:1px solid var(--hairline);
         border-radius:8px;background:transparent;-webkit-user-select:none;user-select:none;
         transition:color .2s,border-color .2s,background .2s}
      .composer-action:hover{color:var(--bone-mute);border-color:var(--hairline-2);
        background:rgba(232,228,216,0.03)}
      .composer-action:focus-visible{outline:2px solid rgba(196,133,94,0.45);outline-offset:2px}
       .composer-action.active{color:var(--ember);border-color:var(--ember-dim);
         background:rgba(196,133,94,0.06)}
       .composer-action-icon{display:inline-grid;place-items:center;min-width:14px;
         color:currentColor;font:15px/1 var(--sans)}
       .composer-action-label{white-space:nowrap}
      .composer.has-attachments{flex-wrap:wrap;align-items:center}
      .composer-attachments{order:2;flex:0 0 100%;display:flex;gap:8px;
        align-items:center;min-width:0;overflow-x:auto;overflow-y:hidden;
        padding:2px 2px 1px 20px;scrollbar-width:thin}
      .composer-attachments[hidden]{display:none!important}
      .composer-attachment{display:grid;grid-template-columns:auto minmax(0,1fr) auto;
        align-items:center;gap:8px;min-height:38px;max-width:280px;padding:4px 6px;
        border:1px solid rgba(232,228,216,0.10);border-radius:8px;
        background:linear-gradient(135deg,rgba(232,228,216,0.045),rgba(20,23,28,0.74));
        box-shadow:inset 0 0 18px rgba(196,133,94,0.035)}
      .composer-attachment[data-state="uploading"]{border-color:rgba(196,133,94,0.24)}
      .composer-attachment[data-state="ready"]{border-color:rgba(125,166,137,0.24)}
      .composer-attachment[data-state="error"]{border-color:rgba(176,120,128,0.34)}
      .composer-attachment-thumb{width:30px;height:30px;border-radius:6px;
        display:grid;place-items:center;overflow:hidden;border:1px solid var(--hairline);
        background:rgba(4,6,9,0.46);color:var(--ember);font:9px/1 var(--mono);
        text-transform:uppercase;letter-spacing:.08em}
      .composer-attachment-thumb img{width:100%;height:100%;object-fit:cover;display:block}
      .composer-attachment-main{min-width:0;display:grid;gap:3px}
      .composer-attachment-name{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
        color:var(--bone-mute);font:11px/1.1 var(--mono)}
      .composer-attachment-meta{display:flex;gap:7px;align-items:center;
        color:var(--whisper);font:9px/1 var(--mono);text-transform:uppercase;
        letter-spacing:.08em}
      .composer-attachment-state.ready{color:var(--patina)}
      .composer-attachment-state.error{color:#b07880}
      .composer-attachment-remove{width:24px;height:24px;border:1px solid var(--hairline);
        border-radius:6px;background:transparent;color:var(--whisper);cursor:pointer;
        font:12px/1 var(--mono)}
      .composer-attachment-remove:hover{color:var(--bone);border-color:var(--hairline-2)}
      .composer-attachment-remove:disabled{opacity:.45;cursor:default}
      .composer-flyout{position:fixed;bottom:96px;right:24px;width:420px;
        max-width:calc(100vw - 48px);
        background:var(--ink-lift);border:1px solid var(--hairline);
        padding:18px 18px 16px;z-index:60;
        box-shadow:0 18px 48px rgba(0,0,0,.55),
                   0 0 0 1px rgba(193,124,79,.12);
        display:none}
      .composer-flyout.show{display:block}
      .composer-flyout label{display:block;font:10px/1.2 var(--mono);
        color:var(--ember);text-transform:uppercase;letter-spacing:.10em;
        margin-bottom:10px}
      .composer-flyout textarea,.composer-flyout input[type=text]{
        width:100%;box-sizing:border-box;padding:10px 12px;
        font:13px/1.5 var(--mono);
        border:1px solid var(--hairline);background:var(--ink-edge);
        color:var(--bone);resize:vertical;min-height:84px;outline:none;
        border-radius:2px;-webkit-text-security:none}
      .composer-flyout textarea::placeholder,
      .composer-flyout input[type=text]::placeholder{color:var(--whisper);font-style:italic}
      .composer-flyout textarea:focus,.composer-flyout input[type=text]:focus{
        border-color:var(--ember-dim);background:var(--ink)}
      .composer-flyout-row{display:flex;gap:10px;margin-top:14px;
        justify-content:flex-end;align-items:center}
      .composer-flyout-row .hint{font:10px var(--mono);color:var(--whisper);
        margin-right:auto;letter-spacing:.04em}
       .feed-upload{display:inline-flex;align-items:center;gap:8px;margin:4px 0;
        padding:8px 10px;border:1px solid var(--hairline);border-radius:8px;
        font:12px var(--mono);color:var(--whisper);background:rgba(20,23,28,0.48)}
      .feed-upload .name{color:var(--bone)}
      .feed-upload img{max-height:48px;max-width:48px;object-fit:cover;
        border:1px solid var(--hairline)}
       .feed-upload-actions{display:flex;gap:6px;margin-top:6px;flex-wrap:wrap}
      .feed-action{cursor:pointer;font:10px/1 var(--mono);
        padding:6px 10px;text-transform:uppercase;letter-spacing:.1em;
        color:var(--whisper);border:1px solid var(--hairline);
        border-radius:8px;background:transparent;text-decoration:none;
        transition:color .2s,border-color .2s,background .2s}
      .feed-action:hover{color:var(--bone);border-color:var(--patina);
        background:rgba(95,128,107,0.06)}
    `;
          document.head.appendChild(style);
        }

        if (!wrap) {
          wrap = document.createElement('div');
          wrap.className = 'composer-actions';
          wrap.setAttribute('aria-label', 'composer actions');
          const voicePill = composer.querySelector('.voice');
          if (voicePill) {
            composer.insertBefore(wrap, voicePill);
          } else {
            composer.appendChild(wrap);
          }
        }

        function ensureAction(action, label, title) {
          let btn = wrap.querySelector('[data-composer-action="' + action + '"]');
          if (!btn) {
            btn = document.createElement('button');
            btn.className = 'composer-action';
            btn.dataset.composerAction = action;
            wrap.appendChild(btn);
          }
          btn.type = 'button';
          btn.replaceChildren();
          const icon = document.createElement('span');
          icon.className = 'composer-action-icon';
          icon.setAttribute('aria-hidden', 'true');
          icon.textContent = action === 'plugins' ? '◎' : action === 'create-document' ? '▤' : '↑';
          const text = document.createElement('span');
          text.className = 'composer-action-label';
          text.textContent = label;
          btn.append(icon, text);
          btn.title = title;
          btn.setAttribute('aria-label', title);
          return btn;
        }

        const btnCreateDoc = ensureAction(
          'create-document',
          'document',
          'Create a document from text'
        );
        const btnUpload = ensureAction(
          'upload',
          'attach',
          'Upload docs or pictures'
        );

        // Hidden multi-file picker for the Upload button + voice cmd
        const fileInput = document.createElement('input');
        fileInput.type = 'file';
        fileInput.id = 'omega-file-input';
        fileInput.multiple = true;
        fileInput.accept = '.png,.jpg,.jpeg,.gif,.webp,.bmp,.txt,.md,.pdf,.docx,.rtf,.html,.json,.csv,.log,.yaml,.yml';
        fileInput.style.display = 'none';
        document.body.appendChild(fileInput);

        const docFlyout = document.createElement('div');
        docFlyout.className = 'composer-flyout';
        docFlyout.innerHTML = `
      <label>document · prompt</label>
      <textarea id="cfd-prompt" spellcheck="false" autocomplete="off"
                autocorrect="off" autocapitalize="off"
                placeholder="meeting minutes from the last standup…"></textarea>
      <div class="composer-flyout-row">
        <label for="cfd-format">format</label>
        <select id="cfd-format">
          <option value="md">markdown</option>
          <option value="docx">docx</option>
          <option value="pdf">pdf</option>
        </select>
      </div>
      <div class="composer-flyout-row">
        <label for="cfd-template">template</label>
        <select id="cfd-template">
          <option value="generic">Generic / clean up</option>
          <option value="report">Report</option>
          <option value="letter">Letter</option>
          <option value="meeting_minutes">Meeting Minutes</option>
          <option value="summary">Summary</option>
          <option value="resume">Résumé</option>
          <option value="readme_spec">README / Spec</option>
        </select>
      </div>
      <div class="composer-flyout-row">
        <label>source file</label>
        <input type="file" id="cfd-upload" hidden>
        <button type="button" class="verb" id="cfd-upload-btn">choose file…</button>
        <span class="hint" id="cfd-upload-name">none</span>
      </div>
      <div class="composer-flyout-row">
        <span class="hint">⌘/Ctrl + Enter to generate</span>
        <button class="verb deny" id="cfd-cancel">cancel</button>
        <button class="verb approve" id="cfd-go">generate</button>
      </div>
    `;
        document.body.appendChild(docFlyout);

        const faceFlyout = document.createElement('div');
        faceFlyout.className = 'composer-flyout';
        faceFlyout.id = 'face-memory-flyout';
        faceFlyout.innerHTML = `
      <label>face memory · webcam</label>
      <input id="cff-name" type="text" placeholder="name" style="min-height:auto;margin-bottom:8px">
      <input id="cff-info" type="text" placeholder="info (optional)" style="min-height:auto;margin-bottom:10px">
      <video id="cff-video" autoplay playsinline muted
        style="width:100%;max-height:200px;background:#111;border:1px solid var(--hairline);
        object-fit:cover;transform:scaleX(-1);margin-bottom:10px"></video>
      <div class="composer-flyout-row">
        <span class="hint" id="cff-status"></span>
        <button class="verb deny" type="button" id="cff-cancel">cancel</button>
        <button class="verb" type="button" id="cff-who">who is this?</button>
        <button class="verb approve" type="button" id="cff-go">save</button>
      </div>`;
        document.body.appendChild(faceFlyout);

        let faceStream = null;
        function stopFaceFlyoutCamera() {
          if (faceStream) {
            try { faceStream.getTracks().forEach(t => t.stop()); } catch (_) { }
            faceStream = null;
          }
          const v = faceFlyout.querySelector('#cff-video');
          if (v) v.srcObject = null;
        }
        async function startFaceFlyoutCamera() {
          stopFaceFlyoutCamera();
          const status = faceFlyout.querySelector('#cff-status');
          status.textContent = 'opening camera…';
          faceStream = await navigator.mediaDevices.getUserMedia({
            video: { width: 640, height: 480, facingMode: 'user' },
          });
          const v = faceFlyout.querySelector('#cff-video');
          v.srcObject = faceStream;
          await v.play();
          status.textContent = '';
        }
        function showFaceFlyout(show) {
          faceFlyout.classList.toggle('show', !!show);
          const camBtn = document.getElementById('camera');
          if (camBtn) camBtn.classList.toggle('active', !!show);
          if (show) {
            docFlyout.classList.remove('show');
            btnCreateDoc.classList.remove('active');
            const opName = document.getElementById('op-name');
            const nameEl = faceFlyout.querySelector('#cff-name');
            if (nameEl && !nameEl.value.trim() && opName && opName.value.trim()) {
              nameEl.value = opName.value.trim();
            }
            startFaceFlyoutCamera().catch(() => {
              faceFlyout.querySelector('#cff-status').textContent = 'camera denied';
            });
            setTimeout(() => {
              const n = faceFlyout.querySelector('#cff-name');
              try { if (n) n.focus(); } catch (_) { }
            }, 50);
          } else {
            stopFaceFlyoutCamera();
            faceFlyout.querySelector('#cff-status').textContent = '';
          }
        }
        window._openFaceMemoryFlyoutFn = () => showFaceFlyout(true);

        // Idle-learning toggle (left cockpit, operator 2026-07-18): durable
        // switch for the idle-autonomy timer; applies on the next pressure
        // tick, survives reboots, day-zero resets it to ON.
        const idleBtn = document.getElementById('btn-idle-learning');
        if (idleBtn) {
          const idleNote = document.getElementById('idle-learning-note');
          const paintIdle = (enabled) => {
            idleBtn.setAttribute('aria-pressed', enabled ? 'true' : 'false');
            idleBtn.classList.toggle('system-control-warning', !enabled);
            if (idleNote) idleNote.textContent = enabled ? 'Autonomy on' : 'Paused';
          };
          fetch('/api/idle-learning', { cache: 'no-store' })
            .then((r) => r.json())
            .then((d) => paintIdle(!!d.enabled))
            .catch(() => { if (idleNote) idleNote.textContent = 'unknown'; });
          idleBtn.addEventListener('click', async () => {
            const next = idleBtn.getAttribute('aria-pressed') !== 'true';
            try {
              const r = await fetch('/api/idle-learning', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ enabled: next }),
              });
              const d = await r.json();
              paintIdle(!!d.enabled);
            } catch (_) { /* keep painted state; next load re-syncs */ }
          });
        }

        faceFlyout.querySelector('#cff-cancel').addEventListener('click', () => showFaceFlyout(false));
        // "Who is this?" — photo-as-question (vision spec §3): capture a frame,
        // identify, answer in chat with the persona facts a hit pulls along.
        faceFlyout.querySelector('#cff-who').addEventListener('click', async () => {
          const status = faceFlyout.querySelector('#cff-status');
          const v = faceFlyout.querySelector('#cff-video');
          if (!v || !v.videoWidth) { status.textContent = 'camera not ready'; return; }
          status.textContent = 'looking…';
          try {
            const canvas = document.createElement('canvas');
            canvas.width = v.videoWidth; canvas.height = v.videoHeight;
            canvas.getContext('2d').drawImage(v, 0, 0);
            const image_b64 = canvas.toDataURL('image/jpeg', 0.9);
            const r = await fetch('/api/vision/identify', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ image_b64 }),
            });
            const result = await r.json().catch(() => ({}));
            if (!r.ok || !result || result.face_count === 0) {
              status.textContent = 'no face detected';
              return;
            }
            const name = result.recognized_name;
            if (name) {
              const conf = Math.round(((result.confidence || 0) * 100));
              let line = "that's " + name + ' (' + conf + '% match)';
              const facts = Array.isArray(result.persona_facts) ? result.persona_facts : [];
              if (facts.length) line += ' — ' + facts.slice(0, 3).join(' · ');
              window.OmegaUI.appendLine('system', line);
              status.textContent = '';
              showFaceFlyout(false);
            } else {
              status.textContent = "I don't know this face — add a name and save to teach me";
            }
          } catch (_) {
            status.textContent = 'identify failed';
          }
        });
        faceFlyout.querySelector('#cff-go').addEventListener('click', async () => {
          const name = (faceFlyout.querySelector('#cff-name').value || '').trim();
          const info = (faceFlyout.querySelector('#cff-info').value || '').trim();
          const status = faceFlyout.querySelector('#cff-status');
          if (!name) {
            status.textContent = 'name required';
            return;
          }
          const video = faceFlyout.querySelector('#cff-video');
          if (!video || !video.videoWidth) {
            status.textContent = 'camera not ready';
            return;
          }
          try {
            const st = await fetch('/api/vision/status', { cache: 'no-store' });
            const stBody = st.ok ? await st.json() : null;
            if (!stBody || !stBody.face_ready) {
              const why = (stBody && (stBody.reason || stBody.error)) || 'face runtime not ready';
              const rt = (stBody && stBody.runtime) || 'http://127.0.0.1:11434';
              status.textContent = 'face runtime down · start ' + rt;
              return;
            }
          } catch (_) {
            status.textContent = 'face runtime status unavailable';
            return;
          }
          status.textContent = 'mapping face · saving…';
          const btn = faceFlyout.querySelector('#cff-go');
          btn.disabled = true;
          try {
            const canvas = document.createElement('canvas');
            canvas.width = video.videoWidth;
            canvas.height = video.videoHeight;
            canvas.getContext('2d').drawImage(video, 0, 0);
            const blob = await new Promise((resolve, reject) => {
              canvas.toBlob(b => (b ? resolve(b) : reject(new Error('capture failed'))), 'image/jpeg', 0.85);
            });
            const fd = new FormData();
            fd.append('file', blob, 'webcam.jpg');
            fd.append('name', name);
            if (info) fd.append('context', info);
            const r = await fetch('/api/vision/upload-face', { method: 'POST', body: fd });
            const result = await r.json();
            if (result && result.ok) {
              const saved = result.name || name;
              if (window.OmegaUI) {
                window.OmegaUI.appendLine('system', 'face memory queued for ' + saved);
              }
              renderChatResult(result, null);
              showFaceFlyout(false);
            } else {
              status.textContent = 'failed · ' + ((result && result.error) || 'unknown');
            }
          } catch (err) {
            status.textContent = 'failed · ' + (err && err.message ? err.message : 'error');
          } finally {
            btn.disabled = false;
          }
        });

        function showDocFlyout(show) {
          docFlyout.classList.toggle('show', !!show);
          btnCreateDoc.classList.toggle('active', !!show);
          if (show) {
            faceFlyout.classList.remove('show');
            stopFaceFlyoutCamera();
            setTimeout(() => docFlyout.querySelector('#cfd-prompt').focus(), 50);
          }
        }
        btnCreateDoc.addEventListener('click', () => showDocFlyout(!docFlyout.classList.contains('show')));

        let cfdUploadRef = null;
        const cfdUploadInput = docFlyout.querySelector('#cfd-upload');
        const cfdUploadName = docFlyout.querySelector('#cfd-upload-name');
        docFlyout.querySelector('#cfd-upload-btn').addEventListener('click', () => {
          cfdUploadInput.click();
        });
        cfdUploadInput.addEventListener('change', async () => {
          const file = cfdUploadInput.files && cfdUploadInput.files[0];
          if (!file) return;
          cfdUploadName.textContent = 'uploading…';
          cfdUploadRef = null;
          try {
            const fd = new FormData();
            fd.append('files', file);
            const r = await fetch('/api/upload', { method: 'POST', body: fd });
            if (!r.ok) throw new Error('http ' + r.status);
            const data = await r.json();
            const item = (data.files || []).find(f => f.ok);
            if (!item) throw new Error((data.files && data.files[0] && data.files[0].error) || 'upload failed');
            cfdUploadRef = item.kind + '/' + item.stored_as;
            cfdUploadName.textContent = file.name;
          } catch (e) {
            cfdUploadName.textContent = '✗ ' + e.message;
          }
          cfdUploadInput.value = '';
        });

        docFlyout.querySelector('#cfd-cancel').addEventListener('click', () => {
          cfdUploadRef = null;
          cfdUploadName.textContent = 'none';
          showDocFlyout(false);
        });
        docFlyout.querySelector('#cfd-go').addEventListener('click', async () => {
          const p = docFlyout.querySelector('#cfd-prompt').value.trim();
          if (!p && !cfdUploadRef) return;
          const fmt = docFlyout.querySelector('#cfd-format').value || 'md';
          const template = docFlyout.querySelector('#cfd-template').value || 'generic';
          const uploadRef = cfdUploadRef;
          cfdUploadRef = null;
          cfdUploadName.textContent = 'none';
          showDocFlyout(false);
          const opts = { format: fmt, template: template };
          if (uploadRef) opts.upload_ref = uploadRef;
          window.OmegaUI.createDocument(p || 'structure the uploaded source', opts);
        });
        docFlyout.querySelector('#cfd-prompt').addEventListener('keydown', (e) => {
          if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
            e.preventDefault();
            docFlyout.querySelector('#cfd-go').click();
          }
        });

        btnUpload.addEventListener('click', () => fileInput.click());
        fileInput.addEventListener('change', () => {
          if (fileInput.files && fileInput.files.length) {
            window.OmegaUI.queueComposerUploads(Array.from(fileInput.files));
            fileInput.value = '';   // allow re-picking same file
          }
        });
        wrap.dataset.omegaUiBound = '1';
      }

      // Wait for the design's composer + feed to exist before injecting.
      // Retry on load so a late script order never skips the action row.
      function scheduleInjectComposerActions() {
        function run() {
          try {
            injectComposerActions();
          } catch (err) {
            console.warn('[OmegaUI] composer inject failed:', err);
          }
        }
        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', run);
        } else {
          run();
        }
        window.addEventListener('load', run);
      }
      window._injectComposerActions = injectComposerActions;
      scheduleInjectComposerActions();
})();

(function () {
  'use strict';
      // ── Skill picker (slash-command autocomplete) ─────────────────
      // Triggered when input starts with "/" for the local live-chat skill surface.
      // CommandDropdown UX (graft #4 / skills HUD backend). Operator
      // chose "insert text + show preview" semantic: Enter on a row
      // fills the input with the command + closes the picker; second
      // Enter (existing submit handler) sends it.
      window.OmegaSkillPicker = (function () {
         // Built-ins are the commands implemented by the live chat surface. Keep
         // the same small fallback while the HUD request is loading.
         const FALLBACK_BUILT_IN_COMMANDS = [
           { name: '/help', description: 'Show the live chat surface and its controls', fullDescription: '/help - show the live chat surface and its current controls' },
           { name: '/skills', description: 'List installed local skills', fullDescription: '/skills - refresh the installed SKILL.md command list' },
           { name: '/mode', description: 'Show the current chat permission mode', fullDescription: '/mode - show read-only, allow-edits, or always-allow status' },
           { name: '/read-only', description: 'Block edits, commands, and plugins', fullDescription: '/read-only - set the chat agent to observation-only mode' },
           { name: '/allow-edits', description: 'Allow bounded workspace edits', fullDescription: '/allow-edits - allow bounded file edits in the workspace' },
           { name: '/always-allow', description: 'Allow declared chat tools without this mode gate', fullDescription: '/always-allow - explicitly enable the highest chat tool mode' },
           { name: '/trust', description: "Grant a directory to Omega's workspace tools", fullDescription: '/trust <directory> - grant and activate a workspace directory' },
           { name: '/cd', description: 'Select a previously trusted directory', fullDescription: '/cd <directory> - select the active trusted directory' },
           { name: '/trusted', description: 'List trusted directories and the active root', fullDescription: '/trusted - list workspace grants and the active directory' },
           { name: '/untrust', description: 'Remove a trusted directory', fullDescription: '/untrust <directory> - remove a workspace grant' },
         ];

        const CACHE_TTL_MS = 5000;
        let pickerEl = null;
        let allCommands = [];
        let items = [];
        let selected = 0;
        let cachedSkills = null;
        let cacheTime = 0;
        // The operator's trusted workspaces, offered as rows under /cd and /untrust so the
        // picker shows where Omega may work (operator 2026-09-15). Refreshed on open.
        let trustedFolders = { folders: [], active: '' };
        let trustedTime = 0;

        async function fetchTrustedFolders(forceRefresh) {
          if (!forceRefresh && Date.now() - trustedTime < CACHE_TTL_MS) return trustedFolders;
          try {
            const r = await fetch('/api/settings/trusted-folders', { cache: 'no-store' });
            const body = r.ok ? await r.json() : null;
            if (body && body.ok !== false) {
              trustedFolders = {
                folders: Array.isArray(body.folders) ? body.folders.map(f => String(f && f.path || '')).filter(Boolean) : [],
                active: String(body.active || ''),
              };
              trustedTime = Date.now();
            }
          } catch (_) { /* the store is unreachable; the plain commands stay */ }
          return trustedFolders;
        }

        function workspaceRows(command) {
          return trustedFolders.folders.map(path => ({
            name: command + ' ' + path,
            description: path === trustedFolders.active ? 'active working folder' : 'trusted workspace',
            fullDescription: command + ' ' + path + (path === trustedFolders.active ? ' - already the active working folder' : ' - select this trusted workspace'),
            isSkill: false,
            isWorkspace: true,
          }));
        }

        function ensureStyles() {
          if (document.getElementById('skill-picker-styles')) return;
          const style = document.createElement('style');
          style.id = 'skill-picker-styles';
          style.textContent = `
      .skill-picker{position:fixed;bottom:96px;left:24px;width:520px;
        max-height:340px;background:var(--ink-lift);
        border:1px solid var(--hairline);padding:6px 0;z-index:60;
        box-shadow:0 18px 48px rgba(0,0,0,.55),
                   0 0 0 1px rgba(95,128,107,.10);
        display:none;overflow-y:auto;font:11px var(--mono)}
      .skill-picker.show{display:block}
      .skill-picker-header{padding:6px 14px 4px;color:var(--whisper);
        text-transform:uppercase;letter-spacing:.10em;font-size:10px;
        margin-top:4px}
      .skill-picker-row{padding:5px 14px;cursor:pointer;display:flex;
        gap:14px;color:var(--bone);align-items:baseline}
      .skill-picker-row.selected{background:rgba(95,128,107,0.18)}
       .skill-picker-row .name{color:var(--patina);min-width:160px;
         font-weight:500}
       .skill-picker-row.skill .name{color:var(--ember,var(--patina))}
       .skill-picker-row.plugin .name{color:var(--patina)}
      .skill-picker-row .desc{color:var(--whisper);flex:1;overflow:hidden;
        text-overflow:ellipsis;white-space:nowrap}
      .skill-picker-preview{padding:8px 14px;color:var(--whisper);
        border-top:1px solid var(--hairline);font-style:italic;
        font-size:11px;min-height:14px}
      .skill-picker-hint{padding:6px 14px;color:var(--whisper);
        font-size:10px;border-top:1px solid var(--hairline);
        letter-spacing:.06em}
      .skill-picker-row .badge{color:var(--whisper);font-size:9px;
        text-transform:uppercase;letter-spacing:.08em}
    `;
          document.head.appendChild(style);
        }

        function ensureEl() {
          if (pickerEl) return pickerEl;
          ensureStyles();
          pickerEl = document.createElement('div');
          pickerEl.className = 'skill-picker';
          pickerEl.innerHTML = `
            <div class="skill-picker-list" id="skill-picker-list"></div>
            <div class="skill-picker-preview" id="skill-picker-preview"></div>
          <div class="skill-picker-hint">↑↓ navigate · Enter inserts command · Esc close</div>
          `;
          document.body.appendChild(pickerEl);
          return pickerEl;
        }

        async function fetchSkills(forceRefresh) {
          const now = Date.now();
          if (!forceRefresh && cachedSkills && now - cacheTime < CACHE_TTL_MS) {
            return cachedSkills;
          }
          try {
            const r = await fetch('/api/skills', { cache: 'no-store' });
            const data = await r.json();
            if (data && data.ok !== false) {
              cachedSkills = data;
              cacheTime = now;
              return data;
            }
          } catch (e) { /* offline / non-HTTP — fall through */ }
          return { commands: [], active: [] };
        }

        function invalidate() {
          cachedSkills = null;
          cacheTime = 0;
        }

        function setCommands(data) {
           const skillCommands = (data.commands || []).map(c => ({
            name: c.name,
            description: c.description || '',
            fullDescription: c.full_description || c.description || '',
            skillName: c.skill_name,
             isSkill: true,
           }));
           const pluginCommands = (data.plugins || []).map(c => ({
             name: c.name,
             description: c.description || '',
             fullDescription: c.full_description || c.fullDescription || c.description || '',
             pluginId: c.plugin_id,
             isPlugin: true,
           }));
          const builtinSource = Array.isArray(data.builtins) && data.builtins.length
            ? data.builtins
            : FALLBACK_BUILT_IN_COMMANDS;
          const builtins = builtinSource.map(c => ({
            name: c.name,
            description: c.description || '',
            fullDescription: c.full_description || c.fullDescription || c.description || '',
            isSkill: false,
          }));
           allCommands = builtins.concat(skillCommands, pluginCommands);
          selected = 0;
          update();
        }

        function onSkillList(data) {
          cachedSkills = data || { commands: [], active: [] };
          cacheTime = Date.now();
          if (_listResolve) {
            _listResolve(cachedSkills);
            _listResolve = null;
            _listReject = null;
          }
          if (isOpen()) {
            setCommands(cachedSkills);
          }
        }

        async function open() {
          ensureEl().classList.add('show');
          const [skills] = await Promise.all([fetchSkills(false), fetchTrustedFolders(true)]);
          setCommands(skills);
        }

        function close() {
          if (pickerEl) pickerEl.classList.remove('show');
        }

        function isOpen() {
          return pickerEl && pickerEl.classList.contains('show');
        }

        function update() {
          const input = document.getElementById('input');
          if (!input) return;
          const q = (input.value || '').toLowerCase();
          items = allCommands.filter(c => c.name.toLowerCase().includes(q));
          const workspaceCommand = ['/cd', '/untrust'].find(cmd => q === cmd || q.startsWith(cmd + ' '));
          if (workspaceCommand) {
            const typed = q.slice(workspaceCommand.length).trim();
            items = items.concat(workspaceRows(workspaceCommand).filter(row =>
              !typed || row.name.toLowerCase().includes(typed)));
          }
          if (selected >= items.length) selected = Math.max(0, items.length - 1);
          render();
        }

        function escapeHtml(s) {
          return String(s == null ? '' : s)
            .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        }

        function render() {
          if (!pickerEl) return;
          const list = pickerEl.querySelector('#skill-picker-list');
          const preview = pickerEl.querySelector('#skill-picker-preview');
          if (!list || !preview) return;
          if (items.length === 0) {
            list.innerHTML = '<div class="skill-picker-row"><span class="desc">(no matches)</span></div>';
            preview.textContent = '';
            return;
          }
          let html = '';
          let lastSection = '';
          items.forEach((c, i) => {
             const section = c.isPlugin ? 'Installed Plugins' : c.isSkill ? 'Skills' : 'Built-in Commands';
            if (section !== lastSection) {
              html += '<div class="skill-picker-header">' + section + '</div>';
              lastSection = section;
            }
             const cls = 'skill-picker-row' + (i === selected ? ' selected' : '')
               + (c.isSkill ? ' skill' : '') + (c.isPlugin ? ' plugin' : '');
            html += '<div class="' + cls + '" data-index="' + i + '">'
              + '<span class="name">' + escapeHtml(c.name) + '</span>'
              + '<span class="desc">' + escapeHtml(c.description) + '</span>'
              + '</div>';
          });
          list.innerHTML = html;
          const current = items[selected];
          preview.textContent = current ? (current.fullDescription || current.description || '') : '';
          // Click handlers
          list.querySelectorAll('.skill-picker-row').forEach(row => {
            row.addEventListener('click', () => {
              const i = parseInt(row.dataset.index || '0', 10);
              if (!Number.isNaN(i)) selectIndex(i);
            });
          });
          // Scroll selected into view
          const sel = list.querySelector('.skill-picker-row.selected');
          if (sel && sel.scrollIntoView) sel.scrollIntoView({ block: 'nearest' });
        }

        function move(delta) {
          if (items.length === 0) return;
          selected = (selected + delta + items.length) % items.length;
          render();
        }

        function selectIndex(i) {
          const input = document.getElementById('input');
          if (!input || !items[i]) return;
          const item = items[i];
           if (item.isPlugin && item.pluginId) {
             input.value = item.name + ' ';
             input.dataset.pluginId = item.pluginId;
             close();
             input.focus();
             return;
           }
           if (item.isSkill && item.skillName) {
            // Skills are submitted through the same live-chat composer as every
            // other operator action.
            input.value = item.name;
            close();
            input.focus();
            return;
          }
          // A workspace row is a complete command: the path is already there.
          // Built-in commands: insert text so the operator can add args
          // and submit via the existing Enter handler.
          const trailing = (item.isSkill || item.isWorkspace) ? '' : ' ';
          input.value = item.name + trailing;
          close();
          input.focus();
        }

        function selectCurrent() {
          if (items.length === 0) return false;
          selectIndex(selected);
          return true;
        }

        function maybeOpen() {
          const input = document.getElementById('input');
          if (!input) return;
          const v = input.value || '';
          if (v.startsWith('/')) {
            if (isOpen()) update();
            else open();
          } else if (isOpen()) {
            close();
          }
        }

        return {
          open, close, isOpen, update, move, selectCurrent, maybeOpen,
          invalidate, fetchSkills, setCommands, fetchTrustedFolders, workspaceRows,
        };
      })();

      // Wire input events for the picker. Capture phase on keydown so
      // we intercept Arrow/Tab/Enter BEFORE the existing submit handler.
      (function wireSkillPicker() {
        function bind() {
          const input = document.getElementById('input');
          if (!input) return;
          input.addEventListener('input', () => {
            window.OmegaSkillPicker.maybeOpen();
          });
          input.addEventListener('keydown', (e) => {
            const picker = window.OmegaSkillPicker;
            if (!picker.isOpen()) return;
            if (e.key === 'ArrowDown') {
              e.preventDefault();
              picker.move(1);
            } else if (e.key === 'ArrowUp') {
              e.preventDefault();
              picker.move(-1);
            } else if (e.key === 'Escape') {
              e.preventDefault();
              picker.close();
            } else if (e.key === 'Tab') {
              e.preventDefault();
              picker.selectCurrent();
            } else if (e.key === 'Enter') {
              // Two-step pick — first Enter fills + closes picker,
              // second Enter (existing submit handler) sends. We only
              // prevent the FIRST Enter (picker open + selection made).
              if (picker.selectCurrent()) {
                e.preventDefault();
                e.stopPropagation();
              }
            }
          }, true);  // capture phase
          // Close picker if operator clicks anywhere outside it.
          document.addEventListener('click', (e) => {
            if (!window.OmegaSkillPicker.isOpen()) return;
            const target = e.target;
            if (target.closest && target.closest('.skill-picker')) return;
            if (target === input) return;
            window.OmegaSkillPicker.close();
          });
        }
        if (document.readyState === 'loading') {
          document.addEventListener('DOMContentLoaded', bind);
        } else {
          bind();
        }
      })();
})();

(function () {
  'use strict';
      const composerUploads = [];
      let composerUploadSeq = 0;

      function _composerUploadKind(file) {
        return file && /^image\//i.test(file.type || '') ? 'image' : 'file';
      }

      function _composerUploadSize(size) {
        const n = Number(size || 0);
        if (n >= 1024 * 1024) return (n / (1024 * 1024)).toFixed(1) + ' MB';
        if (n >= 1024) return (n / 1024).toFixed(1) + ' KB';
        return n + ' B';
      }

      function _composerUploadRef(item) {
        if (!item) return '';
        if (item.upload_ref) return String(item.upload_ref);
        if (item.kind && item.stored_as) return item.kind + '/' + item.stored_as;
        if (item.url) return String(item.url);
        return String(item.filename || item.name || '');
      }

      function _normalizeComposerUploadResult(result, file) {
        const src = result || {};
        const normalized = Object.assign({}, src);
        normalized.filename = normalized.filename || normalized.name || (file && file.name) || 'upload';
        normalized.name = normalized.name || normalized.filename;
        normalized.kind = normalized.kind || _composerUploadKind(file);
        normalized.size = Number(normalized.size || (file && file.size) || 0);
        normalized.upload_ref = _composerUploadRef(normalized);
        return normalized;
      }

      function _ensureComposerAttachmentTray() {
        const composer = document.querySelector('.composer');
        if (!composer) return null;
        let tray = composer.querySelector('.composer-attachments');
        if (!tray) {
          tray = document.createElement('div');
          tray.className = 'composer-attachments';
          tray.hidden = true;
          tray.setAttribute('aria-label', 'pending uploads');
          const actions = composer.querySelector('.composer-actions');
          const voice = composer.querySelector('.voice');
          composer.insertBefore(tray, actions || voice || null);
        }
        return tray;
      }

      function _removeComposerUpload(id) {
        const idx = composerUploads.findIndex(item => item.id === id);
        if (idx < 0) return;
        const item = composerUploads[idx];
        if (item && item.previewUrl) {
          try { URL.revokeObjectURL(item.previewUrl); } catch (_) { }
        }
        composerUploads.splice(idx, 1);
        _syncComposerAttachmentTray();
      }

      function _syncComposerAttachmentTray() {
        const composer = document.querySelector('.composer');
        const tray = _ensureComposerAttachmentTray();
        if (!composer || !tray) return;
        composer.classList.toggle('has-attachments', composerUploads.length > 0);
        tray.hidden = composerUploads.length === 0;
        tray.innerHTML = '';
        if (!composerUploads.length) return;

        for (const item of composerUploads) {
          const row = document.createElement('div');
          row.className = 'composer-attachment';
          row.dataset.uploadId = String(item.id);
          row.dataset.state = item.status || 'queued';

          const thumb = document.createElement('span');
          thumb.className = 'composer-attachment-thumb';
          if (item.previewUrl) {
            const img = document.createElement('img');
            img.src = item.previewUrl;
            img.alt = item.name;
            thumb.appendChild(img);
          } else {
            thumb.textContent = item.kind === 'image' ? 'img' : 'file';
          }
          row.appendChild(thumb);

          const main = document.createElement('span');
          main.className = 'composer-attachment-main';
          const name = document.createElement('span');
          name.className = 'composer-attachment-name';
          name.textContent = item.name;
          name.title = item.name;
          main.appendChild(name);
          const meta = document.createElement('span');
          meta.className = 'composer-attachment-meta';
          const kind = document.createElement('span');
          kind.textContent = item.kind;
          const size = document.createElement('span');
          size.textContent = _composerUploadSize(item.size);
          const state = document.createElement('span');
          state.className = 'composer-attachment-state ' + (item.status || 'queued');
          state.textContent = item.status === 'uploading'
            ? 'uploading'
            : item.status === 'ready'
              ? 'ready'
              : item.status === 'error'
                ? 'error'
                : 'queued';
          if (item.error) state.title = item.error;
          meta.appendChild(kind);
          meta.appendChild(size);
          meta.appendChild(state);
          main.appendChild(meta);
          row.appendChild(main);

          const remove = document.createElement('button');
          remove.type = 'button';
          remove.className = 'composer-attachment-remove';
          remove.textContent = 'x';
          remove.title = 'remove upload';
          remove.setAttribute('aria-label', 'remove ' + item.name);
          remove.disabled = item.status === 'uploading';
          remove.addEventListener('click', () => _removeComposerUpload(item.id));
          row.appendChild(remove);
          tray.appendChild(row);
        }
      }

      async function _uploadComposerItem(item) {
        if (item.uploaded) return item.uploaded;
        item.status = 'uploading';
        item.error = '';
        _syncComposerAttachmentTray();
        try {
          const fd = new FormData();
          fd.append('files', item.file, item.name);
          const r = await fetch('/api/upload', { method: 'POST', body: fd });
          const data = await r.json();
          if (!r.ok) {
            throw new Error((data && (data.detail || data.error)) || ('http ' + r.status));
          }
          const raw = (data.files || []).find(f => f && f.ok)
            || (data.files || [])[0];
          if (!raw || raw.ok === false) {
            throw new Error((raw && raw.error) || 'upload failed');
          }
          const uploaded = _normalizeComposerUploadResult(raw, item.file);
          item.uploaded = uploaded;
          item.status = 'ready';
          item.upload_ref = uploaded.upload_ref;
          _syncComposerAttachmentTray();
          return uploaded;
        } catch (e) {
          item.status = 'error';
          item.error = e && e.message ? e.message : String(e || 'upload failed');
          _syncComposerAttachmentTray();
          throw e;
        }
      }

      // ── OmegaUI: shared action helpers (used by buttons + voice) ──
      // Keep the imperative actions in one place so voice + click + future
      // chat-side-effects all share a single rendering path.
      window.OmegaUI = {
        appendLine(cls, text) {
          // Reuse the design's appendLine when available; otherwise fall back
          // to a minimal node so we never lose a message.
          if (typeof appendLine === 'function') return appendLine(cls, text);
          const f = document.getElementById('feed');
          if (!f) return null;
          const el = document.createElement('div');
          el.className = cls; el.textContent = text;
          appendToFeed(el, f); pinFeedToBottom(f);
          return el;
        },
        appendUpload(item) {
          const f = document.getElementById('feed');
          if (!f) return;
          const row = document.createElement('div');
          row.className = 'feed-upload';
          const isImg = item.kind === 'image';
          if (isImg) {
            const img = document.createElement('img');
            img.src = item.url; img.alt = item.filename;
            row.appendChild(img);
          }
          const span = document.createElement('span');
          span.innerHTML = `<span class="name">${_escapeHtml(item.filename)}</span> · ${_escapeHtml(item.kind)} · ${(item.size / 1024).toFixed(1)} KB`;
          row.appendChild(span);
          // Download + open links so docs and pics are reachable from the feed.
          const actions = document.createElement('div');
          actions.className = 'feed-upload-actions';
          const dl = document.createElement('a');
          dl.className = 'feed-action';
          dl.textContent = '↓ download';
          dl.href = item.url;
          dl.download = item.filename || item.stored_as;
          actions.appendChild(dl);
          const open = document.createElement('a');
          open.className = 'feed-action';
          open.textContent = '↗ open';
          open.href = item.url;
          open.target = '_blank';
          open.rel = 'noopener';
          actions.appendChild(open);
          row.appendChild(actions);
          appendToFeed(row, f);
          pinFeedToBottom(f);
        },
        async createDocument(prompt, opts) {
          opts = opts || {};
          this.appendLine('system', '↻ creating document: "' + prompt + '"');
          try {
            const result = await window.Build.generateDocument(prompt,
              Object.assign({}, opts, { render: false }));
            if (result && result.queued === true) {
              if (typeof renderChatResult === 'function') {
                renderChatResult(result, null);
              }
            } else if (result && result.ok && result.document && result.document.url) {
              if (typeof appendDocumentMessage === 'function') {
                appendDocumentMessage(result);
              } else {
                this.appendLine('assistant', '↳ document ready: ' + result.document.url);
              }
            } else {
              this.appendLine('system',
                '✗ document creation: ' + (result && (result.error || JSON.stringify(result)) || 'no output'));
            }
          } catch (e) {
            this.appendLine('system', '✗ document creation failed: ' + e.message);
          }
        },
        openFaceMemoryFlyout() {
          if (window._openFaceMemoryFlyoutFn) {
            window._openFaceMemoryFlyoutFn();
            return;
          }
          this.appendLine('system', 'Open Settings → operator profile to save face memory.');
          if (typeof openPanel === 'function') openPanel('settings');
        },
        queueComposerUploads(files) {
          const list = Array.from(files || []).filter(Boolean);
          for (const file of list) {
            const kind = _composerUploadKind(file);
            const item = {
              id: ++composerUploadSeq,
              file: file,
              name: file.name || 'upload',
              kind: kind,
              size: file.size || 0,
              status: 'queued',
              error: '',
              uploaded: null,
              previewUrl: '',
            };
            if (kind === 'image' && typeof URL !== 'undefined' && URL.createObjectURL) {
              try { item.previewUrl = URL.createObjectURL(file); } catch (_) { }
            }
            composerUploads.push(item);
          }
          _syncComposerAttachmentTray();
          const input = document.getElementById('input');
          if (input) input.focus();
          return composerUploads.slice();
        },
        hasComposerUploads() {
          return composerUploads.length > 0;
        },
        async prepareComposerUploads() {
          if (!composerUploads.length) return [];
          const uploaded = [];
          for (const item of composerUploads.slice()) {
            uploaded.push(await _uploadComposerItem(item));
          }
          return uploaded;
        },
        clearComposerUploads() {
          for (const item of composerUploads) {
            if (item && item.previewUrl) {
              try { URL.revokeObjectURL(item.previewUrl); } catch (_) { }
            }
          }
          composerUploads.splice(0, composerUploads.length);
          _syncComposerAttachmentTray();
        },
        uploadRefFor(item) {
          return _composerUploadRef(item);
        },
        composeMessageWithUploads(text, uploads) {
          const clean = (text || '').trim();
          const refs = (uploads || [])
            .map(item => {
              const name = item && (item.filename || item.name) || 'upload';
              const ref = _composerUploadRef(item);
              return ref ? '- ' + name + ' [' + ref + ']' : '- ' + name;
            })
            .filter(Boolean);
          if (!refs.length) return clean;
          const suffix = 'Attached files:\n' + refs.join('\n');
          return clean ? clean + '\n\n' + suffix : suffix;
        },
        composerUploadDisplayText(text, uploads) {
          const clean = (text || '').trim();
          const names = (uploads || [])
            .map(item => item && (item.filename || item.name))
            .filter(Boolean);
          if (!names.length) return clean;
          const suffix = 'attached: ' + names.join(', ');
          return clean ? clean + '\n' + suffix : suffix;
        },
        async uploadFiles(files) {
          return this.queueComposerUploads(files);
        },
        triggerUploadPicker() {
          const inp = document.querySelector('input[type=file][multiple]');
          if (inp) inp.click();
        },
        async requestTool(description) {
          // WIRE-6: operator → curiosity entry → autonomous engine routes
          // through synthesize_from_curiosity → 6-stage tool synthesis.
          this.appendLine('system', '⚙ requesting new tool: "' + description + '"');
          try {
            const r = await fetch('/api/omega/request_tool', {
              method: 'POST',
              headers: { 'content-type': 'application/json' },
              body: JSON.stringify({ description }),
            });
            const data = await r.json();
            if (data && data.ok) {
              this.appendLine('system',
                '✓ tool need queued (' + data.curiosity_id + ') — autonomous '
                + 'engine will route on next cadence sweep (~90s). Watch the '
                + 'omega panel for synthesis activity.');
              if (typeof Voice !== 'undefined' && Voice.speak)
                Voice.speak('Tool request queued. The autonomous engine will '
                  + 'evaluate and synthesise.');
            } else {
              this.appendLine('system',
                '✗ tool request failed: ' + ((data && data.error) || 'unknown'));
            }
          } catch (e) {
            this.appendLine('system', '✗ tool request error: ' + e.message);
          }
        },
      };
})();

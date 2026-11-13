// Panel: settings-plugins — signed plugin directory, review signature, and user-authorized connection handoff.
// The directory is intentionally provider-neutral: public entries come from the
// signed catalog, while this surface owns discovery, review, installation, and
// connection state only.
function _pluginConnectionAction(status) {
  if (status === 'connected' || status === 'authorized') return 'connected';
  if (
    status === 'installed_disconnected' ||
    status === 'connection_required' ||
    status === 'authorization_required'
  ) return 'start';
  return 'error';
}

// _pluginExactAppId/_pluginExactAppIds moved to core/plugin-views.js
// (follow-up refactor wave W1) -- renderCard's own next-required-app data
// attribute is one of their callers, alongside the business-logic call
// sites below. That module loads before this panel in every real build and
// every .mjs harness (exactly like core/plugin-permissions.js), so these
// remain plain global calls here -- unchanged call sites, no local copy.

function _pluginRevision(value) {
  if (typeof value !== 'string' || !/^[0-9a-f]{64}$/.test(value)) {
    throw new Error('plugin_permission_changed');
  }
  return value;
}

function _pluginConnectionBody(appId, revision) {
  const exactRevision = _pluginRevision(revision);
  return appId == null
    ? {revision: exactRevision}
    : {app_id: _pluginExactAppId(appId), revision: exactRevision};
}

function _pluginPermissionsUrl(pluginId, appId) {
  const base = '/api/plugins/' + encodeURIComponent(pluginId) + '/permissions';
  return appId == null
    ? base
    : base + '?app_id=' + encodeURIComponent(_pluginExactAppId(appId));
}

function _pluginCatalogPageEntries(catalogPlugins, installedPlugins) {
  if (!Array.isArray(catalogPlugins) || !Array.isArray(installedPlugins)) {
    throw new Error('plugin_directory_invalid');
  }
  const catalogIds = catalogPlugins.map(plugin => {
    if (!plugin || typeof plugin !== 'object') {
      throw new Error('plugin_directory_invalid');
    }
    return _pluginExactAppId(plugin.plugin_id);
  });
  if (new Set(catalogIds).size !== catalogIds.length) {
    throw new Error('plugin_directory_invalid');
  }
  const installedById = new Map();
  installedPlugins.forEach(snapshot => {
    if (!snapshot || typeof snapshot !== 'object') {
      throw new Error('plugin_directory_invalid');
    }
    const pluginId = _pluginExactAppId(snapshot.plugin_id);
    if (installedById.has(pluginId)) throw new Error('plugin_directory_invalid');
    installedById.set(pluginId, snapshot);
  });
  return catalogPlugins.map(plugin => {
    const snapshot = installedById.get(plugin.plugin_id);
    return {
      ...plugin,
      ...(snapshot || {}),
      installed: Boolean(snapshot),
      status: snapshot ? snapshot.state : 'available',
    };
  });
}

// The 14 canonical lifecycle states, their labels, tones, primary actions,
// and disabled-action sets all come from core/plugin-permissions.js's
// window.lifecycleView -- the one shared state->action table (final fix
// wave W1). This panel used to hand-encode its own second copy of that
// same table here, plus a pair of matching label/tone lookup literals;
// all of that is deleted now -- see tests/hud/test_plugin_marketplace_ui.py's
// no-private-lifecycle-table source contract and
// tests/hud/test_plugin_marketplace_panel.mjs's 14-state runtime proof.

// _pluginTrustedIconPath/_pluginIconMarkup (the verified same-origin icon
// path check and the card/tile icon markup it gates) and
// _pluginGroupByCategory (the category grouping that preserves the
// server's own item order) moved to core/plugin-views.js (follow-up
// refactor wave W1), alongside the rest of the catalog card/installed
// tile/drawer-body/permissions-editor view builders -- see that module for
// their implementations and tests/hud/test_plugin_marketplace_panel.mjs /
// test_plugin_marketplace_review.mjs for their runtime coverage.

const _PLUGIN_CATALOG_PAGE_SIZE = 24;

// Sends every search/filter change to OmegaPluginApi.catalog(...) -- the
// server owns matching, never a local re-filter of an already-loaded page
// (controller ruling 1). trust=personal scopes the Personal tab; today
// that source is not yet composed server-side, so it always renders the
// server's own empty/unavailable page rather than a fabricated list
// (controller ruling 3). Module scope so it is directly unit-testable.
function _pluginBuildCatalogParams(snap, cursor) {
  const params = {q: snap.query, page_size: _PLUGIN_CATALOG_PAGE_SIZE};
  if (cursor) params.cursor = cursor;
  const filters = snap.filters || {};
  if (filters.category) params.category = filters.category;
  if (snap.tab === 'personal') {
    params.trust = 'personal';
  } else if (filters.trust) {
    params.trust = filters.trust;
  }
  if (filters.state) params.state = filters.state;
  return params;
}

// The trust-tier label map and the details/install-review/update-review/
// uninstall-review drawer body builders moved to core/plugin-views.js
// (follow-up refactor wave W1) -- see that module for their
// implementations. openPluginDetails/openPluginReview/openDrawer/
// uninstallPlugin below still call _pluginRenderDetailsPanel/
// _pluginDrawerShellMarkup/_pluginUninstallReviewMarkup exactly as before;
// they now resolve to core/plugin-views.js's globals, loaded before this
// panel in every real build and every .mjs harness.

// Renders one real, accessible <form> of a secret_form step's declared
// fields (label/required/secret -- task-5 controller ruling 3) inline in
// the card's own review panel, and resolves with {field_id: value} on
// submit. Built with real DOM APIs (never string-concatenated innerHTML) so
// field labels need no separate escaping and so submitted <input> values
// are never echoed back into markup. A pure function of (card, fields) --
// module scope so it is directly unit-testable outside a full render pass.
function _pluginCollectSecretFieldsUi(card, fields) {
  return new Promise((resolve, reject) => {
    const detail = card.querySelector('[data-plugin-review-panel]');
    if (!detail) {
      reject(new Error('plugin_connection_state_invalid'));
      return;
    }
    const form = document.createElement('form');
    form.className = 'plugins-secret-form';
    form.setAttribute('data-plugin-secret-form', '');
    fields.forEach(field => {
      const label = document.createElement('label');
      label.className = 'plugins-secret-field';
      const text = document.createElement('span');
      text.textContent = field.label + (field.required ? ' (required)' : '');
      const input = document.createElement('input');
      input.type = field.secret ? 'password' : 'text';
      input.autocomplete = 'off';
      input.required = Boolean(field.required);
      input.setAttribute('data-plugin-secret-field', field.field_id);
      label.appendChild(text);
      label.appendChild(input);
      form.appendChild(label);
    });
    const submit = document.createElement('button');
    submit.type = 'submit';
    submit.className = 'plugins-install-button';
    submit.textContent = 'Submit';
    form.appendChild(submit);
    form.addEventListener('submit', event => {
      event.preventDefault();
      const values = {};
      fields.forEach(field => {
        const input = form.querySelector('[data-plugin-secret-field="' + field.field_id + '"]');
        values[field.field_id] = input ? input.value : '';
      });
      resolve(values);
    });
    detail.innerHTML = '';
    detail.appendChild(form);
    detail.hidden = false;
  });
}

// Secret values never persist past submission (task-5 controller ruling 3):
// every rendered <input> is cleared, then the whole form is removed from
// the DOM. A pure function of (card) -- module scope for the same reason
// as _pluginCollectSecretFieldsUi above.
function _pluginClearSecretFieldsUi(card) {
  const detail = card.querySelector('[data-plugin-review-panel]');
  const form = detail && detail.querySelector('[data-plugin-secret-form]');
  if (!form) return;
  form.querySelectorAll('[data-plugin-secret-field]').forEach(input => { input.value = ''; });
  form.remove();
}

function _renderSettingsPlugins(panel) {
  const esc = typeof _escapeHtml === 'function' ? _escapeHtml : value => String(value ?? '');
  // The one shared state->action/label/tone/disabled-actions table (final
  // fix wave W1) -- core/plugin-permissions.js's window.lifecycleView,
  // loaded before this panel in every real build (the chat_feed core
  // inject slot) and in every .mjs test harness that exercises this
  // panel's runtime.
  const lifecycleView = window.lifecycleView;
  const STATE_FILTER_OPTIONS = Array.from(window.CANONICAL_PLUGIN_STATES);
  panel.innerHTML = `
    <div class="plugins-directory" data-plugin-browser>
      <div class="plugins-search-shell">
        <span class="plugins-search-icon" aria-hidden="true">⌕</span>
        <label class="plugins-visually-hidden" for="plugins-search-input">Search plugins</label>
        <input id="plugins-search-input" type="search" data-plugin-search placeholder="Search plugins" aria-label="Search plugins" autocomplete="off">
      </div>
      <section class="plugins-installed-section" aria-labelledby="plugins-installed-title">
        <div class="plugins-section-heading">
          <h2 id="plugins-installed-title">Installed</h2>
          <span class="plugins-update-status" data-plugin-update-status aria-live="polite"></span>
          <button class="plugins-quiet-button" type="button" data-plugin-update-check aria-label="Check for plugin updates">↻</button>
          <button class="plugins-quiet-button" type="button" data-plugin-settings aria-label="Plugin settings">⚙</button>
        </div>
        <div class="plugins-installed-list" data-plugin-installed-list aria-live="polite">
          <p class="plugins-empty">Loading installed plugins…</p>
        </div>
      </section>
      <div class="plugins-directory-toolbar">
        <div class="plugins-scope-tabs" role="tablist" aria-label="Plugin visibility">
          <button type="button" id="plugins-tab-public" class="plugins-scope-tab active" data-plugin-tab="public" role="tab" aria-selected="true" aria-controls="plugins-directory-panel">Public</button>
          <button type="button" id="plugins-tab-personal" class="plugins-scope-tab" data-plugin-tab="personal" role="tab" aria-selected="false" aria-controls="plugins-directory-panel">Personal</button>
        </div>
        <button type="button" class="plugins-filter-button" data-plugin-filter aria-expanded="false" aria-controls="plugins-filter-panel" aria-label="Filter plugins">☷</button>
      </div>
      <fieldset class="plugins-filter-panel" id="plugins-filter-panel" data-plugin-filter-panel hidden>
        <legend class="plugins-visually-hidden">Filter plugins</legend>
        <label>Category
          <input type="text" data-plugin-filter-category placeholder="Any category" autocomplete="off">
        </label>
        <label>State
          <select data-plugin-filter-state>
            <option value="">Any state</option>
            ${STATE_FILTER_OPTIONS.map(state =>
              `<option value="${esc(state)}">${esc(lifecycleView({state}).label)}</option>`
            ).join('')}
          </select>
        </label>
      </fieldset>
      <section class="plugins-directory-section" id="plugins-directory-panel" role="tabpanel" aria-label="Plugin results" aria-labelledby="plugins-tab-public" data-plugin-tabpanel>
        <div class="plugins-section-heading plugins-section-heading-public">
          <h2 data-plugin-tab-title>Public</h2>
          <span class="plugins-directory-count" data-plugin-count aria-live="polite"></span>
        </div>
        <p class="plugins-directory-banner" data-plugin-source-banner hidden></p>
        <div class="plugins-public-list" data-plugin-public-list>
          <p class="plugins-empty">Loading signed plugins…</p>
        </div>
        <button type="button" class="plugins-load-more-button" data-plugin-load-more hidden>Load more</button>
      </section>
      <p class="plugins-directory-error" data-plugin-directory-error aria-live="assertive" hidden></p>
      <div class="plugins-drawer-root" data-plugin-drawer-root></div>
    </div>`;

  const search = panel.querySelector('[data-plugin-search]');
  const installedList = panel.querySelector('[data-plugin-installed-list]');
  const publicList = panel.querySelector('[data-plugin-public-list]');
  const errorBox = panel.querySelector('[data-plugin-directory-error]');
  const count = panel.querySelector('[data-plugin-count]');
  const sectionTitle = panel.querySelector('[data-plugin-tab-title]');
  const tabpanel = panel.querySelector('[data-plugin-tabpanel]');
  const sourceBanner = panel.querySelector('[data-plugin-source-banner]');
  const loadMoreButton = panel.querySelector('[data-plugin-load-more]');
  const filterButton = panel.querySelector('[data-plugin-filter]');
  const filterPanel = panel.querySelector('[data-plugin-filter-panel]');
  const filterCategory = panel.querySelector('[data-plugin-filter-category]');
  const filterState = panel.querySelector('[data-plugin-filter-state]');
  const drawerRoot = panel.querySelector('[data-plugin-drawer-root]');
  const updateStatus = panel.querySelector('[data-plugin-update-status]');
  const updateCheck = panel.querySelector('[data-plugin-update-check]');
  const store = window.OmegaPluginStore.create();
  // The merged view of "the currently loaded catalog page, cross-referenced
  // with authoritative installed status" -- rebuilt by refreshPlugins()
  // whenever the store's active-tab catalog or installed map changes. The
  // connection/OAuth/permission flow below reads and (optimistically)
  // mutates entries in this array exactly as it always has.
  let plugins = [];
  let installedStripError = null;
  let requestSeq = 0;
  // A separate monotonic counter for pluginChanged's own ordering guard
  // (task-6 controller ruling 1, fix round 1) -- unrelated to the catalog's
  // own requestSeq above (a different id space for a different reducer
  // field). Assigned once per mutation attempt, before any await, so a
  // slower-resolving earlier attempt for the same plugin can never
  // overwrite a faster later one.
  let pluginRequestSeq = 0;
  const catalogMeta = {public: {status: null, total: null}, personal: {status: null, total: null}};
  // One cancel() controller per plugin with an in-flight
  // window.OmegaPluginSetup.start() call -- disconnectPlugin() cancels the
  // active setup (if any) before it tells the server to disconnect. Setup
  // is driven only from the per-app consent form's submit handler
  // (loadAuthority), which Connect/Continue-setup and a confirmed update's
  // sign-in both open (final fix wave W2: one shared setup machine, one
  // shared in-flight tracker; per-app operator consent).
  const activeSetupControllers = new Map();
  // Exact per-app consent from the visible editor. The setup machine may
  // make one follow-up start call after the permissions write has already
  // created its authorization request; reuse only that exact selection.
  const connectionSelections = new Map();
  // The most recently observed server lifecycle/catalog revision (the same
  // `revision` field every /api/plugins/* response already carries verbatim
  // -- see hud/server.py). This is the "current revision" review/install/
  // update now send and check (controller ruling 2); it is refreshed from
  // every successful catalog page and installed-list load.
  let knownRevision = null;
  // The one loaded install/update review currently open in the drawer, or
  // null when no review is loaded -- the confirm control only ever exists in
  // the DOM while this is set (controller ruling 4).
  let loadedReview = null;
  let drawerFocusReturn = null;

  const api = window.OmegaPluginApi;

  // One shared, closed, provider-neutral error vocabulary (task-6
  // controller ruling 3, fix round 1 finding 2): the panel calls
  // core/plugin-permissions.js's own readablePluginError directly --
  // there is no private cause table here anymore. That module loads
  // before this panel in every real build (the `chat_feed` core inject
  // slot, ahead of `panels`) and now also in every .mjs test harness that
  // exercises this panel's runtime (see test_plugin_marketplace_panel.mjs/
  // test_plugin_marketplace_review.mjs/test_plugin_marketplace_setup.mjs,
  // each updated to load plugin-permissions.js first).
  const readableError = window.readablePluginError;

  const reserveAuthorizationPopup = () => {
    try {
      return window.open('', 'omega-plugin-oauth', 'popup,width=520,height=720');
    } catch (_) {
      return null;
    }
  };

  const returnAuthorizationPopupToOmega = oauthPopup => {
    const omegaReturnUrl = new URL(
      '/#settings/plugins',
      window.location.origin,
    ).href;
    if (oauthPopup && !oauthPopup.closed) {
      // Device-flow providers finish on their own origin. A WindowProxy still
      // permits its opener to navigate it back to Omega even when close() is
      // ignored by an embedded browser.
      try { oauthPopup.location = omegaReturnUrl; } catch (_) {}
      try { oauthPopup.close(); } catch (_) {}
    }
    return omegaReturnUrl;
  };

  const returnToOmegaAfterConnection = oauthPopup => {
    const omegaReturnUrl = returnAuthorizationPopupToOmega(oauthPopup);
    window.location.replace(omegaReturnUrl);
  };

  const renderExactAppChoices = (card, appIds, choose) => {
    const detail = card && card.querySelector('[data-plugin-review-panel]');
    if (!detail) throw new Error('plugin_connection_state_invalid');
    const prior = detail.querySelector('[data-plugin-app-choices]');
    if (prior) prior.remove();
    const choices = document.createElement('fieldset');
    choices.setAttribute('data-plugin-app-choices', '');
    const legend = document.createElement('legend');
    legend.textContent = 'Choose the app to set up';
    choices.appendChild(legend);
    appIds.forEach(appId => {
      const choice = document.createElement('button');
      choice.type = 'button';
      choice.className = 'plugins-secondary-button';
      choice.setAttribute('data-plugin-app-choice', appId);
      choice.textContent = appId;
      choice.addEventListener('click', () => choose(appId));
      choices.appendChild(choice);
    });
    detail.appendChild(choices);
    detail.hidden = false;
  };

  // Icons render only from verified metadata: a same-origin /api/plugins/...
  // asset path or the closed provider identity carried by the directory. An
  // unrecognized plugin never becomes a provider icon through its ID text.
  // See core/plugin-views.js's _pluginIconMarkup (follow-up refactor wave W1).

  // Rebuilds the merged "current catalog page x authoritative installed
  // status" view the card renderer and the connection flow both read.
  const refreshPlugins = () => {
    const snap = store.snapshot();
    try {
      plugins = _pluginCatalogPageEntries(snap.catalog.items, Object.values(snap.installed));
    } catch (_error) {
      plugins = [];
    }
  };

  // The installed payload carries its own verified title (see
  // mcp_platform/plugin_directory.py's installed_plugin_summary) -- this
  // never depends on the plugin also being present on the currently loaded
  // catalog page, so an installed plugin outside the page (or any Personal
  // plugin) still renders its real title, not a raw plugin_id.
  const pluginDisplayTitle = plugin => plugin.title || plugin.plugin_id;

  // The per-tile markup (icon/title/state pill) is core/plugin-views.js's
  // installedTileMarkup (follow-up refactor wave W1) -- this panel keeps
  // the store read, the visible error row, and the empty-state fallback,
  // none of which is pure (they read installedStripError/store state).
  const renderInstalled = () => {
    const snap = store.snapshot();
    const installed = Object.values(snap.installed);
    const rows = installed.map(plugin => installedTileMarkup(plugin, esc, pluginDisplayTitle(plugin)));
    if (installedStripError) {
      // A server response the store rejected (plugin_state_invalid) or a
      // fetch failure is always shown, never silently hidden -- see
      // controller ruling 4 in task-3-brief.md.
      rows.push(
        '<div class="plugins-installed-error" role="alert">' +
          '<span aria-hidden="true">⚠</span>' +
          '<span>' + esc(readableError(installedStripError)) + '</span>' +
        '</div>'
      );
    }
    if (!rows.length) {
      installedList.innerHTML = '<p class="plugins-empty">No installed plugins yet.</p>';
      return;
    }
    installedList.innerHTML = rows.join('');
  };

  // The catalog card markup (including the primary action/management row
  // driven by the plugin's own canonical state) is core/plugin-views.js's
  // renderCard(plugin, esc) (follow-up refactor wave W1) -- called directly
  // below (renderCatalogSection's category grid) wherever the panel used
  // to build its own copy.

  // --- Accessible details/review drawer ------------------------------
  // One drawer element serves the read-only "Details" view and both review
  // modes (install and update): the accessible dialog shell
  // core/plugin-views.js's _pluginDrawerShellMarkup builds, focus moved
  // onto the dialog on open, and focus restored to whatever triggered it
  // on close. Never a browser confirm() for a permission-bearing review
  // (controller ruling: "Do not use a generic browser confirm()...").
  const drawerLoadingMarkup = () => '<p class="plugins-empty" role="status">Loading review…</p>';

  const closeDrawer = () => {
    drawerRoot.innerHTML = '';
    loadedReview = null;
    if (drawerFocusReturn && typeof drawerFocusReturn.focus === 'function') {
      drawerFocusReturn.focus();
    }
    drawerFocusReturn = null;
  };

  const openDrawer = (bodyHtml, title, triggerEl) => {
    drawerFocusReturn = triggerEl || null;
    drawerRoot.innerHTML = _pluginDrawerShellMarkup(bodyHtml, {esc, title});
    const closeButton = drawerRoot.querySelector('[data-plugin-drawer-close]');
    if (closeButton) closeButton.addEventListener('click', closeDrawer);
    const backdrop = drawerRoot.querySelector('[data-plugin-drawer-backdrop]');
    if (backdrop) {
      backdrop.addEventListener('click', event => {
        if (event.target === backdrop) closeDrawer();
      });
    }
    const dialog = drawerRoot.querySelector('[data-plugin-drawer]');
    if (dialog) {
      // Scoped to the dialog itself (not document) -- Escape bubbles here
      // from any focused descendant (the close button, a confirm/cancel
      // control) since focus always lives inside this dialog while it is
      // open. closeDrawer() already discards any loaded review and never
      // submits anything (task-4 fix round 1, finding 3).
      //
      // stopPropagation() is required here (smoke-found regression, fix
      // round 2): without it, this bubble-phase listener does not stop the
      // same Escape keydown from continuing on up to register.js's global
      // `window.addEventListener('keydown', ...)` handler (also
      // bubble-phase, on `window` -- a strict ancestor of this dialog), so
      // Escape closed the drawer AND the whole Settings overlay in one
      // press. Stopping propagation here is sufficient (both listeners are
      // bubble-phase, on different targets) so a same-target
      // stopImmediatePropagation() is not needed.
      dialog.addEventListener('keydown', event => {
        if (event.key === 'Escape') {
          if (typeof event.preventDefault === 'function') event.preventDefault();
          if (typeof event.stopPropagation === 'function') event.stopPropagation();
          closeDrawer();
        }
      });
      if (typeof dialog.focus === 'function') dialog.focus();
    }
    return drawerRoot;
  };

  const setDrawerBody = bodyHtml => {
    const body = drawerRoot.querySelector('[data-plugin-drawer-body]');
    if (body) body.innerHTML = bodyHtml;
  };

  // A stale revision is never retried blindly: the cause renders inside the
  // drawer (controller ruling: "a review revision conflict is rendered from
  // the server cause") while the panel re-fetches and re-renders both the
  // installed strip and the active catalog page, then the drawer closes on
  // its own once fresh data has loaded (controller ruling 2).
  const handleRevisionConflict = async () => {
    setDrawerBody('<p class="plugins-error-inline" role="alert">' + esc(readableError({code: 'plugin_revision_conflict'})) + '</p>');
    await refreshInstalled();
    await loadCatalog();
    closeDrawer();
  };

  const openPluginDetails = async (card, triggerButton) => {
    const pluginId = card.dataset.pluginId;
    const plugin = plugins.find(item => item.plugin_id === pluginId);
    openDrawer(drawerLoadingMarkup(), (plugin && plugin.title) || pluginId, triggerButton);
    try {
      const detailsResponse = await api.details(pluginId);
      setDrawerBody(_pluginRenderDetailsPanel(detailsResponse.plugin, {mode: 'view', esc}));
    } catch (error) {
      setDrawerBody('<p class="plugins-error-inline" role="alert">' + esc(readableError(error)) + '</p>');
    }
  };

  // The exact review-then-confirm surface for both install ('install-review')
  // and update ('update-review') -- the same details surface, with an
  // update's side-by-side changed fields added on top (controller ruling:
  // "Update uses the same details surface with side-by-side changed
  // fields."). The confirm control is added to the DOM only once the review
  // has actually loaded; cancelling discards it and submits nothing.
  const openPluginReview = async (card, triggerButton, mode) => {
    const pluginId = card.dataset.pluginId;
    const plugin = plugins.find(item => item.plugin_id === pluginId);
    const installedSnapshot = store.snapshot().installed[pluginId] || null;
    loadedReview = null;
    openDrawer(drawerLoadingMarkup(), (plugin && plugin.title) || pluginId, triggerButton);
    try {
      let detailsPayload = null;
      try {
        detailsPayload = (await api.details(pluginId)).plugin;
      } catch (_detailsError) {
        detailsPayload = null;
      }
      let reviewRevision;
      let reviewData;
      if (mode === 'update-review') {
        // The candidate/target version to diff against: the details route's
        // own catalog field when available, else the currently loaded
        // catalog card's own plugin_version (both come from the same
        // verified catalog entry) -- never guessed.
        const targetVersion = (detailsPayload && detailsPayload.plugin_version) ||
          (plugin && plugin.plugin_version) || null;
        if (typeof targetVersion !== 'string' || !targetVersion) {
          const notAvailable = new Error('plugin_catalog_entry_unavailable');
          notAvailable.code = 'plugin_catalog_entry_unavailable';
          throw notAvailable;
        }
        const updateReviewResponse = await api.updateReview(pluginId, targetVersion, knownRevision);
        reviewRevision = updateReviewResponse.revision;
        reviewData = updateReviewResponse.review;
      } else {
        const reviewResponse = await api.review(pluginId, knownRevision);
        reviewRevision = reviewResponse.revision;
        reviewData = reviewResponse.review;
      }
      if (typeof reviewRevision === 'string') knownRevision = reviewRevision;
      loadedReview = {
        mode,
        pluginId,
        revision: reviewRevision,
        review: reviewData,
      };
      setDrawerBody(_pluginRenderDetailsPanel(
        detailsPayload || {plugin_id: pluginId, title: (plugin && plugin.title) || pluginId},
        {mode, review: reviewData, installedSnapshot, esc},
      ));
      const confirmButton = drawerRoot.querySelector('[data-plugin-review-confirm]');
      const cancelButton = drawerRoot.querySelector('[data-plugin-review-cancel]');
      if (cancelButton) {
        cancelButton.addEventListener('click', () => {
          loadedReview = null;
          closeDrawer();
        });
      }
      if (confirmButton) {
        confirmButton.addEventListener('click', () => confirmPluginReview());
      }
    } catch (error) {
      if (error && error.code === 'plugin_revision_conflict') {
        await handleRevisionConflict();
        return;
      }
      setDrawerBody('<p class="plugins-error-inline" role="alert">' + esc(readableError(error)) + '</p>');
    }
  };

  // Carries a confirmed update straight into the sign-in it made necessary
  // (promoting a re-signed package retires the old connection). It opens the
  // same exact per-app consent form Connect/Continue-setup opens
  // (startConnection), for the app the update named: the operator sees that
  // app's scope/capability selection immediately, and only submitting it
  // saves the selection and drives the shared setup machine (loadAuthority's
  // submit handler, which registers that in-flight setup with
  // activeSetupControllers and reserves the sign-in window in its own
  // gesture). Nothing connects here without that explicit selection. A card
  // the current catalog view does not render has nowhere to show the form;
  // its installed tile still reports the setup-required state, and Continue
  // setup opens the same form.
  const continueSetupAfterUpdate = async (pluginId, appId) => {
    const card = panel.querySelector('[data-plugin-id="' + pluginId + '"]');
    if (!card) return;
    await loadAuthority(card, card.querySelector('[data-plugin-review-panel]'), appId);
  };

  // Submits exactly the loaded review's own digest and revision -- never a
  // value recomputed or guessed client-side (controller ruling: "Install and
  // update submit exactly the server's review digest (review.sha256) and the
  // current revision").
  const confirmPluginReview = async () => {
    if (!loadedReview) return;
    const {mode, pluginId, revision, review} = loadedReview;
    const confirmButton = drawerRoot.querySelector('[data-plugin-review-confirm]');
    const cancelButton = drawerRoot.querySelector('[data-plugin-review-cancel]');
    if (confirmButton) confirmButton.disabled = true;
    if (cancelButton) cancelButton.disabled = true;
    try {
      let updateResult = null;
      if (mode === 'update-review') {
        // review is GET .../update-review's own shape
        // ({from_version,to_version,permission_changes,sha256}) -- to_version
        // is the exact confirmed target, never plugin_version (that field
        // only exists on an install-review's PluginInstallReview shape).
        updateResult = await api.update(pluginId, revision, review.to_version, review.sha256);
      } else {
        await api.install(pluginId, revision, review.sha256);
      }
      loadedReview = null;
      closeDrawer();
      await refreshInstalled();
      renderCatalogSection();
      // A pending update answers 202 with the app still to authorize. Carry
      // straight into that app's consent form rather than leaving the
      // operator to notice the card now says "Continue setup".
      if (updateResult && updateResult.next_required_app_id) {
        await continueSetupAfterUpdate(pluginId, updateResult.next_required_app_id);
      }
    } catch (error) {
      if (error && error.code === 'plugin_revision_conflict') {
        await handleRevisionConflict();
        return;
      }
      const body = drawerRoot.querySelector('[data-plugin-drawer-body]');
      if (body) {
        let errorNode = body.querySelector('[data-plugin-review-error]');
        if (!errorNode) {
          errorNode = document.createElement('p');
          errorNode.setAttribute('data-plugin-review-error', '');
          errorNode.setAttribute('role', 'alert');
          errorNode.className = 'plugins-error-inline';
          body.appendChild(errorNode);
        }
        errorNode.textContent = readableError(error);
      }
      if (confirmButton) confirmButton.disabled = false;
      if (cancelButton) cancelButton.disabled = false;
    }
  };

  // Renders the active tab's catalog: loading/error/empty states from the
  // store, category sections in server order, the results count, the
  // degraded-source banner, and the load-more control. Never applies search
  // or filters locally -- they were already sent to OmegaPluginApi.catalog().
  const renderCatalogSection = () => {
    const snap = store.snapshot();
    const tab = snap.tab;
    const catalog = snap.catalog;
    const meta = catalogMeta[tab] || {status: null, total: null};

    panel.querySelectorAll('[data-plugin-tab]').forEach(tabButton => {
      const active = tabButton.dataset.pluginTab === tab;
      tabButton.classList.toggle('active', active);
      tabButton.setAttribute('aria-selected', active ? 'true' : 'false');
      // The one shared tabpanel is always labelled by whichever tab is
      // currently selected (controller-required tab semantics: tablist/tab/
      // tabpanel linked through aria-controls/aria-labelledby, not only
      // aria-selected).
      if (active && tabpanel) tabpanel.setAttribute('aria-labelledby', tabButton.id);
    });
    sectionTitle.textContent = tab === 'personal' ? 'Personal' : 'Public';

    sourceBanner.hidden = true;
    if (meta.status === 'degraded') {
      sourceBanner.hidden = false;
      sourceBanner.textContent = 'Some plugin sources are unavailable right now. Showing the last verified results.';
    }

    loadMoreButton.hidden = !catalog.cursor || catalog.loading;
    loadMoreButton.disabled = catalog.loading;

    if (catalog.error) {
      count.textContent = '';
      publicList.innerHTML = '';
      errorBox.hidden = false;
      errorBox.innerHTML = '<span>' + esc(readableError(catalog.error)) + '</span>' +
        '<button type="button" class="plugins-retry-button" data-plugin-retry>Retry</button>';
      return;
    }
    errorBox.hidden = true;

    if (catalog.loading && !catalog.items.length) {
      count.textContent = '';
      publicList.innerHTML = '<p class="plugins-empty">Loading signed plugins…</p>';
      return;
    }

    refreshPlugins();
    count.textContent = meta.total != null ? String(meta.total) : (plugins.length ? String(plugins.length) : '');
    if (!plugins.length) {
      publicList.innerHTML = '<p class="plugins-empty">No plugins match this search</p>';
      return;
    }
    publicList.innerHTML = _pluginGroupByCategory(plugins).map(group =>
      '<section class="plugins-category-section">' +
        '<h3 class="plugins-category-title">' + esc(group.category) + '</h3>' +
        '<div class="plugins-category-grid">' + group.items.map(item => renderCard(item, esc)).join('') + '</div>' +
      '</section>'
    ).join('');
  };

  const loadCatalog = (options = {}) => {
    const snap = store.snapshot();
    const tab = snap.tab;
    const cursor = options.append ? snap.catalog.cursor : null;
    const requestId = ++requestSeq;
    store.dispatch({type: 'catalogLoading', request: requestId, tab});
    renderCatalogSection();
    return api.catalog(_pluginBuildCatalogParams(snap, cursor)).then(page => {
      try {
        // Defense in depth: this panel only ever requests
        // _PLUGIN_CATALOG_PAGE_SIZE items per page. A single server page
        // that carries more than that (a malformed or tampered response)
        // is truncated to the panel's own ceiling before it ever reaches
        // the store/render -- a page over the ceiling never renders more
        // than the ceiling, regardless of how many items the response body
        // actually contains.
        const items = Array.isArray(page.plugins)
          ? page.plugins.slice(0, _PLUGIN_CATALOG_PAGE_SIZE)
          : page.plugins;
        store.dispatch({
          type: 'catalogLoaded',
          request: requestId,
          tab,
          items,
          revision: page.revision,
          cursor: page.next_cursor,
          append: Boolean(options.append),
        });
        if (store.snapshot().catalogs[tab].request === requestId) {
          catalogMeta[tab] = {status: page.status, total: page.total_plugins};
        }
        if (typeof page.revision === 'string') knownRevision = page.revision;
      } catch (dispatchError) {
        // A response the store's own contract check rejects (for example a
        // malformed item) still fails visibly rather than leaving the
        // section stuck on "Loading" -- see controller ruling 4's "unknown
        // states fail visibly" applied here to the catalog page itself.
        const code = (dispatchError && dispatchError.code) || 'plugin_store_event_invalid';
        store.dispatch({type: 'catalogFailed', request: requestId, tab, error: code});
        catalogMeta[tab] = {status: 'failed', total: null};
      }
    }, error => {
      const code = (error && error.code) || 'plugin_catalog_unavailable';
      store.dispatch({type: 'catalogFailed', request: requestId, tab, error: code});
      if (store.snapshot().catalogs[tab].request === requestId) {
        catalogMeta[tab] = {status: 'failed', total: null};
      }
    }).then(() => renderCatalogSection());
  };

  // The installed strip is always sourced from an awaited /installed
  // response (controller ruling 5); an unrecognized lifecycle state throws
  // plugin_state_invalid at the store, which renders as one visible failure
  // row rather than being hidden (controller ruling 4).
  const refreshInstalled = async () => {
    try {
      const response = await api.installed();
      store.dispatch({type: 'installedLoaded', items: response.plugins});
      installedStripError = null;
      if (typeof response.revision === 'string') knownRevision = response.revision;
    } catch (error) {
      installedStripError = error;
    }
    refreshPlugins();
    renderInstalled();
    // loadCatalog() and refreshInstalled() fire as two independent,
    // uncoordinated async chains at panel init (and after most mutations);
    // whichever settles first would otherwise leave the other section
    // showing a merge against stale data. renderCatalogSection() reads the
    // just-updated `plugins` merge (refreshPlugins() above), so a connected
    // card always paints its management row once installed data is known --
    // never only on whichever chain happens to finish last (controller
    // ruling 2).
    renderCatalogSection();
    // core/plugin-composer.js keeps its own connected-plugin picker in sync
    // through this event; it is unpaginated (unlike the catalog browsing
    // state above) since /api/plugins/installed already returns every
    // installed plugin.
    const composerPlugins = Object.values(store.snapshot().installed).map(item => ({
      ...item,
      installed: true,
      status: item.state,
      title: pluginDisplayTitle(item),
    }));
    window.dispatchEvent(new CustomEvent('omega:plugin-directory', {detail: {plugins: composerPlugins}}));
  };

  const loadReview = async card => {
    const pluginId = card.dataset.pluginId;
    const plugin = plugins.find(item => item.plugin_id === pluginId);
    const detail = card.querySelector('[data-plugin-review-panel]');
    const more = card.querySelector('[data-plugin-more]');
    try {
      // The same server-supplied lifecycle/catalog revision review/install/
      // update all use (controller ruling 2) -- plugin.revision is never
      // populated by the catalog/installed merge (see startConnection).
      const review = (await api.review(pluginId, knownRevision)).review;
      const source = plugin && plugin.authoritative_source_url
        ? '<div class="plugins-review-row"><span>provider source</span><a href="' + esc(plugin.authoritative_source_url) + '" target="_blank" rel="noopener noreferrer">' + esc(plugin.publisher_display_name || plugin.publisher_id) + '</a></div>'
        : '';
      detail.innerHTML = '<div class="plugins-review-row"><span>Omega safety envelope</span><strong>' + esc(review.signing_key_id) + '</strong></div>' +
        '<div class="plugins-review-row"><span>version</span><strong>' + esc(review.plugin_version) + '</strong></div>' +
        '<div class="plugins-review-row"><span>capabilities</span><strong>' + esc((review.required_app_ids || []).join(', ') || 'none') + '</strong></div>' +
        source +
        '<div class="plugins-review-digest">review digest ' + esc(review.sha256) + '</div>';
      const opening = detail.hidden;
      detail.hidden = !opening;
      if (more) more.setAttribute('aria-expanded', opening ? 'true' : 'false');
      card.dataset.reviewSha256 = review.sha256;
      if (opening) {
        await loadAuthority(card, detail);
        window.requestAnimationFrame(() => detail.scrollIntoView({block: 'nearest', behavior: 'smooth'}));
      }
    } catch (error) {
      if (error && error.code === 'plugin_revision_conflict') {
        // Never retried blindly: hide the stale inline panel and re-fetch,
        // exactly like openPluginReview's own handleRevisionConflict.
        detail.hidden = true;
        detail.innerHTML = '';
        if (more) more.setAttribute('aria-expanded', 'false');
        await refreshInstalled();
        await loadCatalog();
        return;
      }
      detail.hidden = false;
      detail.innerHTML = '<span class="plugins-error-inline">' + esc(readableError(error)) + '</span>';
    }
  };

  const loadAuthority = async (card, host, appId = null) => {
    const pluginId = card.dataset.pluginId;
    const detail = host || card.querySelector('[data-plugin-review-panel]');
    try {
      const body = await api.permissions(pluginId, appId);
      if (body && body.error_code === 'plugin_app_selection_required') {
        renderExactAppChoices(
          card,
          _pluginExactAppIds(body),
          selectedAppId => loadAuthority(card, detail, selectedAppId),
        );
        return;
      }
      const exactAppId = _pluginExactAppId(body.app_id);
      if (appId != null && exactAppId !== appId) {
        throw new Error('plugin_connection_state_invalid');
      }
      // selected.source_constraints/destination_constraints are read again
      // below, in the submit handler's savePermissions body -- kept here
      // (not only inside the view builder) for that later use.
      const selected = body.selected || {};
      // Network and filesystem scope (task-6 Interfaces: the permission
      // editor covers them alongside selected capabilities and direct-chat
      // authority) -- read-only here, sourced from the same
      // GET .../catalog/{plugin_id} installed_authority projection the
      // Details drawer already renders (mcp_platform/plugin_directory.py's
      // installed_plugin_authority; task-4). Best-effort: a hiccup fetching
      // details omits only this info block, never the editable scope form
      // above it.
      let installedAuthority = null;
      try {
        const detailsResponse = await api.details(pluginId);
        installedAuthority = (detailsResponse && detailsResponse.plugin && detailsResponse.plugin.installed_authority) || null;
      } catch (_detailsError) {
        installedAuthority = null;
      }
      const review = document.createElement('form');
      review.className = 'plugin-connection-review';
      // The provider-scope/capability checkboxes, the "When Omega may act"
      // execution-policy radios, the autonomous-authority toggle, and the
      // read-only network/filesystem info block are core/plugin-views.js's
      // permissionsEditorMarkup (follow-up refactor wave W1) -- a pure
      // function of the same GET .../permissions response and
      // installed_authority this panel already fetched. The submit handler
      // below reads the checked/selected values back out of this same form.
      review.innerHTML = permissionsEditorMarkup(pluginId, body, installedAuthority, esc);
      review.addEventListener('submit', async event => {
        event.preventDefault();
        const submit = review.querySelector('button[type="submit"]');
        const status = card.querySelector('[data-plugin-status]');
        // Assigned synchronously, before any await -- the ordering-guard
        // invariant (task-6 controller ruling 1, fix round 1: a
        // slower-resolving earlier attempt can never overwrite this
        // attempt's own later pluginChanged dispatch).
        const pluginRequestId = ++pluginRequestSeq;
        // Reserved synchronously inside this submit gesture, before any
        // await -- the authorization this consented save starts may need to
        // navigate it, and opening it only after the save's awaits would let
        // a popup blocker sever the opener relationship.
        const oauthPopup = reserveAuthorizationPopup();
        let cancelled = false;
        activeSetupControllers.set(pluginId, {cancel: () => { cancelled = true; }});
        if (submit) submit.disabled = true;
        if (status) {
          status.textContent = 'saving connection settings…';
          status.className = 'plugins-card-status';
        }
        const provider_scopes = Array.from(review.querySelectorAll('[data-plugin-provider-scope]:checked'))
          .map(input => input.getAttribute('data-plugin-provider-scope'))
          .filter(Boolean);
        const capability_ids = Array.from(review.querySelectorAll('[data-plugin-capability]:checked'))
          .map(input => input.getAttribute('data-plugin-capability'))
          .filter(Boolean);
        const policyInput = review.querySelector('[data-plugin-execution-policy]:checked');
        const connectionSettings = {
          provider_scopes,
          capability_ids,
          execution_policy: (policyInput && policyInput.getAttribute('data-plugin-execution-policy')) || 'confirm_each',
          source_constraints: selected.source_constraints || {},
          destination_constraints: selected.destination_constraints || {},
        };
        try {
          const perApp = connectionSelections.get(pluginId) || new Map();
          connectionSelections.set(pluginId, perApp);
          perApp.set(exactAppId, {settings: connectionSettings, start: null});
          const started = await api.savePermissions(
            pluginId,
            body.revision,
            connectionSettings,
            exactAppId,
          );
          perApp.get(exactAppId).start = started;
          if (!status) throw new Error('plugin_connection_state_invalid');
          // Saving settings can itself require finishing an authorization
          // (new scopes needing re-consent, a still-incomplete required
          // app, ...). GET .../connection (task-5 controller ruling 1) is
          // the only source of the closed setup_action, and the panel
          // drives whatever it reports through the one shared
          // window.OmegaPluginSetup machine -- this submit is the only
          // place it is driven, whether the form was opened by
          // Connect/Continue-setup, a confirmed update's sign-in, or
          // Permissions (final fix wave W2 -- never a second, ad hoc
          // popup/device/postMessage implementation). When
          // setup_action is already 'connected', the machine's own
          // no-op pass-through handles that case too.
          const fresh = await api.status(pluginId);
          await driveConnectionSetup(card, status, oauthPopup, fresh, pluginRequestId, () => cancelled);
        } catch (error) {
          if (oauthPopup && !oauthPopup.closed) oauthPopup.close();
          if (error && (error.code === 'plugin_permission_changed' || error.code === 'plugin_revision_conflict')) {
            // Never retried blindly: re-fetch and re-render so the card
            // reflects whatever actually changed, rather than persisting a
            // status message computed against state the client no longer
            // holds -- the same pattern handleRevisionConflict already uses
            // for the review drawer (task-6 controller ruling 3, fix round 1
            // finding 2).
            await refreshInstalled();
            renderCatalogSection();
            return;
          }
          if (status) {
            status.textContent = readableError(error);
            status.className = 'plugins-card-status is-error';
          }
        } finally {
          activeSetupControllers.delete(pluginId);
          if (submit) submit.disabled = false;
        }
      });
      const previous = detail.querySelector('.plugin-connection-review');
      if (previous) previous.remove();
      detail.appendChild(review);
      detail.hidden = false;
    } catch (error) {
      detail.hidden = false;
      const note = document.createElement('span');
      note.className = 'plugins-error-inline';
      note.textContent = readableError(error);
      detail.appendChild(note);
    }
  };

  // The one shared setup machine (final fix wave W2): builds
  // window.OmegaPluginSetup's controls and drives it against `initial` (a
  // fresh GET .../connection snapshot), dispatches only the returned
  // lifecycle snapshot to the store, and refreshes the panel. Its one caller
  // is the per-app consent form's submit handler (loadAuthority), which
  // Connect/Continue-setup (startConnection below), a confirmed update's
  // sign-in (continueSetupAfterUpdate), and Permissions all open -- no path
  // drives its own popup/device/postMessage implementation, and the machine
  // starts an app only under the selection that submit recorded for it.
  const driveConnectionSetup = async (card, status, oauthPopup, initial, pluginRequestId, isCancelled) => {
    const pluginId = card.dataset.pluginId;
    if (typeof initial.revision === 'string') knownRevision = initial.revision;
    const finalSnapshot = await window.OmegaPluginSetup.start(
      initial,
      initial.next_required_app_id,
      {
        pluginId,
        revision: knownRevision,
        api: {
          startConnection: (pid, revision, targetAppId) => {
            const perApp = connectionSelections.get(pid);
            const selection = perApp && perApp.get(targetAppId);
            if (!selection) throw new Error('plugin_scopes_invalid');
            if (selection.start) {
              const started = selection.start;
              selection.start = null;
              return Promise.resolve(started);
            }
            return api.startConnection(pid, revision, targetAppId, selection.settings);
          },
          pollConnection: (pid, revision, requestId) => api.pollConnection(pid, revision, requestId),
          status: pid => api.status(pid),
          // No credential intake exists in the connection service yet
          // (task-5 controller ruling 2) -- submitSecretFields is
          // deliberately left unwired so a secret_form step fails closed
          // with the distinct plugin_setup_credentials_unavailable code
          // rather than a route this task never verified exists.
        },
        popup: oauthPopup,
        origin: window.location.origin,
        addMessageListener: handler => {
          window.addEventListener('message', handler);
          return () => window.removeEventListener('message', handler);
        },
        navigateSameWindow: url => window.location.assign(url),
        finish: (popup, connected) => {
          if (connected) {
            returnToOmegaAfterConnection(popup);
          } else if (popup && !popup.closed) {
            popup.close();
          }
        },
        onDeviceCode: info => {
          status.textContent = info.userCode
            ? 'Enter code ' + info.userCode + ' to finish connecting'
            : 'Waiting for authorization…';
          status.className = 'plugins-card-status is-attention';
        },
        onStatus: text => {
          status.textContent = text;
          status.className = 'plugins-card-status is-attention';
        },
        isCancelled,
        collectSecretFields: fields => _pluginCollectSecretFieldsUi(card, fields),
        clearSecretFields: () => _pluginClearSecretFieldsUi(card),
        onSnapshot: (snapshot, connected) => {
          // The server's lifecycle revision changes after every completed
          // app (PluginLifecycleService.start_connection re-checks it
          // before each subsequent app and rejects a stale one with
          // plugin_permission_changed) -- the panel's own tracker must
          // stay current for a composite plugin's next required app, whose
          // own consent round starts from it.
          if (typeof snapshot.revision === 'string' && snapshot.revision) {
            knownRevision = snapshot.revision;
          }
          if (connected) return;
          if (snapshot.next_required_app_id) {
            card.dataset.nextPluginAppId = snapshot.next_required_app_id;
            status.textContent = 'Additional setup required: ' + snapshot.next_required_app_id;
            status.className = 'plugins-card-status is-attention';
          }
        },
      },
    );
    // The store receives only the returned lifecycle snapshot (controller
    // ruling 5) -- never a value fabricated from the popup/poll traffic.
    // `request: pluginRequestId` lets the store's ordering guard drop this
    // dispatch if a later attempt for the same plugin already resolved
    // first (task-6 controller ruling 1, fix round 1).
    store.dispatch({type: 'pluginChanged', plugin: finalSnapshot, request: pluginRequestId});
    delete card.dataset.nextPluginAppId;
    await refreshInstalled();
    renderCatalogSection();
    return finalSnapshot;
  };

  // Connect/Continue-setup: opens the exact per-app consent form for card's
  // next required app. Submitting it drives the shared setup machine above,
  // whose closed SETUP_ACTIONS map executes exactly the server-declared
  // action for that one app -- never a provider-branched flow chosen here. A
  // composite plugin's further required apps are not started under this
  // app's consent: setup stops after each app, and Continue setup opens the
  // next app's own form.
  const startConnection = async (card, button) => {
    const pluginId = card.dataset.pluginId;
    const status = card.querySelector('[data-plugin-status]');
    const plugin = plugins.find(item => item.plugin_id === pluginId);
    if (!plugin || !plugin.installed) {
      if (status) {
        status.textContent = readableError(Object.assign(new Error('plugin_not_installed'), {code: 'plugin_not_installed'}));
        status.className = 'plugins-card-status is-error';
      }
      await loadCatalog();
      return;
    }
    // A connection starts only after the operator sees and submits this
    // app's exact scope and capability selection. Composite setup returns
    // here after each app so the next app gets a separate consent form.
    const exactAppId = card.dataset.nextPluginAppId || plugin.next_required_app_id || null;
    if (button) button.disabled = true;
    try {
      await loadAuthority(card, card.querySelector('[data-plugin-review-panel]'), exactAppId);
    } finally {
      if (button) button.disabled = false;
    }
  };

  // Disconnect removes connection authority only -- the installed card is
  // kept (controller ruling 4). It sends the panel's currently held
  // lifecycle revision (controller ruling 2); a stale revision is never
  // retried blindly -- the panel re-fetches and re-renders instead, the
  // same "never retry blindly" pattern handleRevisionConflict and the
  // consent form's submit handler already use elsewhere.
  const disconnectPlugin = async (card, button) => {
    const pluginId = card.dataset.pluginId;
    const status = card.querySelector('[data-plugin-status]');
    const activeSetup = activeSetupControllers.get(pluginId);
    if (activeSetup) activeSetup.cancel();
    button.disabled = true;
    status.textContent = 'disconnecting…';
    status.className = 'plugins-card-status';
    try {
      await api.disconnect(pluginId, knownRevision);
      // The disconnected state is read back from the next authoritative
      // /installed response (controller ruling 5), not fabricated locally.
      // The plugin's own record stays in that response (still installed,
      // just disconnected) -- refreshInstalled()/renderCatalogSection()
      // re-render the same card with its new state; nothing removes it.
      await refreshInstalled();
      renderCatalogSection();
    } catch (error) {
      if (error && error.code === 'plugin_revision_conflict') {
        await refreshInstalled();
        renderCatalogSection();
        return;
      }
      status.textContent = readableError(error);
      status.className = 'plugins-card-status is-error';
    } finally {
      button.disabled = false;
    }
  };

  // Uninstall requires an in-panel destructive review before it ever calls
  // the server -- never a browser confirm() (global HUD-plan constraint).
  // The card is removed only once the server's next authoritative
  // /installed response confirms this plugin is no longer installed
  // (controller ruling 4) -- there is no optimistic local removal.
  const uninstallPlugin = (card, button) => {
    const pluginId = card.dataset.pluginId;
    const plugin = plugins.find(item => item.plugin_id === pluginId);
    if (!plugin) return;
    openDrawer(
      _pluginUninstallReviewMarkup(plugin, esc),
      'Uninstall ' + (plugin.title || pluginId),
      button,
    );
    const cancelButton = drawerRoot.querySelector('[data-plugin-uninstall-cancel]');
    const confirmButton = drawerRoot.querySelector('[data-plugin-uninstall-confirm]');
    if (cancelButton) cancelButton.addEventListener('click', () => closeDrawer());
    if (confirmButton) {
      confirmButton.addEventListener('click', async () => {
        confirmButton.disabled = true;
        if (cancelButton) cancelButton.disabled = true;
        try {
          await api.uninstall(pluginId, knownRevision);
          closeDrawer();
          await refreshInstalled();
          renderCatalogSection();
        } catch (error) {
          if (error && error.code === 'plugin_revision_conflict') {
            await handleRevisionConflict();
            return;
          }
          const body = drawerRoot.querySelector('[data-plugin-drawer-body]');
          if (body) {
            let errorNode = body.querySelector('[data-plugin-review-error]');
            if (!errorNode) {
              errorNode = document.createElement('p');
              errorNode.setAttribute('data-plugin-review-error', '');
              errorNode.setAttribute('role', 'alert');
              errorNode.className = 'plugins-error-inline';
              body.appendChild(errorNode);
            }
            errorNode.textContent = readableError(error);
          }
          if (confirmButton) confirmButton.disabled = false;
          if (cancelButton) cancelButton.disabled = false;
        }
      });
    }
  };

  const tryPluginInChat = async (card, button) => {
    const pluginId = card.dataset.pluginId;
    button.disabled = true;
    try {
      const handoff = await api.tryInChat(pluginId);
      if (!window.OmegaPluginComposer || typeof window.OmegaPluginComposer.acceptTryInChat !== 'function') {
        throw new Error('plugin composer unavailable');
      }
      window.OmegaPluginComposer.acceptTryInChat(handoff);
    } catch (error) {
      // The plugin may have disconnected, drifted, or otherwise changed
      // state on the server since this card was last rendered (task-7
      // controller ruling 1: "the exact new state renders", never a stale
      // introduction and never a stale card). Re-fetch and re-render so the
      // card reflects the server's exact new state -- the same "never retry
      // blindly" pattern disconnectPlugin/savePermissions/handleRevisionConflict
      // already use elsewhere in this file -- then render the error on the
      // freshly rendered card (the old `card`/`button` references are
      // detached once renderCatalogSection() replaces publicList's markup).
      await refreshInstalled();
      renderCatalogSection();
      const freshCard = publicList.querySelector('[data-plugin-id="' + pluginId + '"]');
      const freshStatus = freshCard && freshCard.querySelector('[data-plugin-status]');
      if (freshStatus) {
        freshStatus.textContent = readableError(error);
        freshStatus.className = 'plugins-card-status is-error';
      }
    }
  };

  publicList.addEventListener('click', event => {
    const card = event.target.closest('[data-plugin-id]');
    if (!card) return;
    const more = event.target.closest('[data-plugin-more]');
    const details = event.target.closest('[data-plugin-details]');
    const action = event.target.closest('[data-plugin-action]');
    const updateReview = event.target.closest('[data-plugin-update-review]');
    const disconnect = event.target.closest('[data-plugin-disconnect]');
    const uninstall = event.target.closest('[data-plugin-uninstall]');
    const authority = event.target.closest('[data-plugin-authority]');
    const tryButton = event.target.closest('[data-plugin-try]');
    if (more) loadReview(card);
    if (details) openPluginDetails(card, details);
    if (updateReview) {
      const currentPlugin = plugins.find(item => item.plugin_id === card.dataset.pluginId);
      const secondary = lifecycleView(currentPlugin || {}).secondaryAction;
      if (secondary && secondary.type === 'update_review') {
        openPluginReview(card, updateReview, 'update-review');
      }
    }
    if (action) {
      const currentPlugin = plugins.find(item => item.plugin_id === card.dataset.pluginId);
      const primary = lifecycleView(currentPlugin || {}).primaryAction;
      const actionType = primary && primary.type;
      // The action button's routing is entirely driven by the plugin's own
      // canonical state (task-6 controller ruling 4), never a bare Connect
      // by default: Available/Review-required -> Install review;
      // Update-available/Update-required -> Update review (never a bare
      // Connect); Permission-changed -> the permissions editor (the diff of
      // what's newly offered vs. currently selected), not Connect;
      // Setup-required/Offline/Blocked/Revocation-pending/Installed ->
      // startConnection (Continue setup / Retry / Connect all open the same
      // per-app consent form, whose submit drives the one
      // window.OmegaPluginSetup machine -- there is no separate
      // provider-branched "retry" flow).
      if (actionType === 'install') {
        openPluginReview(card, action, 'install-review');
      } else if (actionType === 'update_review') {
        openPluginReview(card, action, 'update-review');
      } else if (actionType === 'permissions') {
        loadAuthority(card);
      } else if (actionType === 'connect' || actionType === 'continue_setup' || actionType === 'retry') {
        // The consent form is loaded for the card's next required app, and
        // GET .../permissions must answer for exactly that app; the setup its
        // submit drives then reads the current setup action from a fresh
        // GET .../connection, never from card state.
        startConnection(card, action);
      }
      // 'connecting'/'installing' (disabled) and null (connected/
      // incompatible/unrecognized) dispatch nothing -- a disabled button
      // never reaches this handler in a real browser.
    }
    if (disconnect) disconnectPlugin(card, disconnect);
    if (uninstall) uninstallPlugin(card, uninstall);
    if (authority) loadAuthority(card);
    if (tryButton) tryPluginInChat(card, tryButton);
  });
  // Search is debounced 180ms and resets the cursor by dispatching
  // queryChanged, whose reducer clears the active tab's loaded page/cursor
  // (controller ruling 1) -- the query is sent to the API, never used to
  // re-filter an already-loaded page.
  let searchDebounce = null;
  search.addEventListener('input', () => {
    if (searchDebounce) window.clearTimeout(searchDebounce);
    const value = search.value;
    searchDebounce = window.setTimeout(() => {
      store.dispatch({type: 'queryChanged', query: value});
      loadCatalog();
    }, 180);
  });

  panel.querySelectorAll('[data-plugin-tab]').forEach(tabButton => {
    tabButton.addEventListener('click', () => {
      const tab = tabButton.dataset.pluginTab;
      if (tab === store.snapshot().tab) return;
      store.dispatch({type: 'tabChanged', tab});
      loadCatalog();
    });
  });

  filterButton.addEventListener('click', () => {
    const expanded = filterButton.getAttribute('aria-expanded') === 'true';
    filterButton.setAttribute('aria-expanded', expanded ? 'false' : 'true');
    filterPanel.hidden = expanded;
  });

  const applyFilters = () => {
    const filters = {};
    const category = filterCategory.value.trim();
    const stateFilter = filterState.value;
    if (category) filters.category = category;
    if (stateFilter) filters.state = stateFilter;
    store.dispatch({type: 'filtersChanged', filters});
    loadCatalog();
  };
  filterCategory.addEventListener('change', applyFilters);
  filterState.addEventListener('change', applyFilters);

  loadMoreButton.addEventListener('click', () => loadCatalog({append: true}));

  panel.querySelector('[data-plugin-settings]').addEventListener('click', () => {
    const installedIds = Object.keys(store.snapshot().installed);
    if (!installedIds.length) return;
    const card = publicList.querySelector('[data-plugin-id="' + installedIds[0] + '"]');
    if (card) loadAuthority(card);
  });

  const renderUpdateStatus = payload => {
    const status = payload && payload.status;
    const policy = payload && payload.policy;
    if (!status || !updateStatus) return;
    const label = status.status === 'checked'
      ? (status.candidate_versions && status.candidate_versions.length ? 'Updates available' : 'Up to date')
      : (status.status === 'not_due' ? 'Updates checked recently' : status.status);
    const checked = Number.isFinite(status.checked_at)
      ? new Date(status.checked_at * 1000).toISOString()
      : 'not checked';
    const candidates = (status.candidate_versions || [])
      .map(item => Array.isArray(item) ? item.join(' ') : '')
      .filter(Boolean).join(', ');
    const staged = (status.staged || [])
      .map(item => Array.isArray(item) ? item.join(' ') : '')
      .filter(Boolean).join(', ');
    const approvals = (status.approval_required || [])
      .map(item => Array.isArray(item) ? item.slice(0, 2).join(' ') : '')
      .filter(Boolean).join(', ');
    const detail = [
      label,
      'Last check: ' + checked,
      candidates ? 'Candidates: ' + candidates : '',
      staged ? 'Staged: ' + staged : '',
      approvals ? 'Approval required: ' + approvals : '',
      status.error ? 'Error: ' + status.error : '',
    ].filter(Boolean).join(' | ');
    updateStatus.textContent = detail;
    if (policy && policy.next_check_at) updateStatus.title = 'Next automatic check scheduled';
  };
  const refreshUpdateStatus = () => {
    if (typeof window.OmegaPluginApi.updateStatus !== 'function') return Promise.resolve();
    return window.OmegaPluginApi.updateStatus().then(renderUpdateStatus).catch(() => {});
  };
  if (updateCheck && typeof window.OmegaPluginApi.checkUpdates === 'function') updateCheck.addEventListener('click', () => {
    updateCheck.disabled = true;
    window.OmegaPluginApi.checkUpdates()
      .then(response => renderUpdateStatus({status: response.status}))
      .finally(() => { updateCheck.disabled = false; });
  });

  errorBox.addEventListener('click', event => {
    if (event.target.closest('[data-plugin-retry]')) loadCatalog();
  });
  // A panel-lifetime `message` listener used to live here for the separate
  // "Save and (re)connect" path, which never itself awaited an OAuth
  // callback (final fix wave W2: both that path and Connect/Continue-setup
  // now drive the same window.OmegaPluginSetup machine, whose own
  // per-call, per-app addMessageListener already awaits and validates
  // exactly the callback it is waiting on -- see plugin-setup.js's
  // _pluginSetupAwaitCallback -- so this second, standing listener is gone).
  refreshInstalled();
  loadCatalog();
  refreshUpdateStatus();
}

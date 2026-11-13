// Pure view builders for panels/settings-plugins.js's plugin marketplace
// surface (follow-up refactor wave W1): catalog card markup, installed tile
// markup, drawer bodies (details/install-review/update-review/
// uninstall-review), the permissions editor markup, section/category
// grouping, and the pure formatting/validation helpers they use. Every
// export here takes plain data in and returns markup/strings out (or, for
// the two exact-app-id validators, throws the same stable
// plugin_connection_state_invalid code the panel's business logic already
// relied on) -- no DOM mutation, no fetch, no OmegaPluginStore/
// OmegaPluginSetup call, no event wiring. The panel
// (panels/settings-plugins.js) keeps state, wiring, event handlers, store/
// setup calls, focus handling, and the drawer keydown handler; it calls
// these exactly as it called its own former private copies (bare
// identifiers -- this module is loaded before the panel in every real build
// and every .mjs harness, exactly like core/plugin-permissions.js), and
// these call window.lifecycleView / window.readablePluginError from
// core/plugin-permissions.js exactly as the panel itself does.

// Shared by several business-logic call sites the panel keeps
// (_pluginConnectionBody/_pluginPermissionsUrl/_pluginCatalogPageEntries/
// loadAuthority) as well as by renderCard's own next-required-app data
// attribute below -- kept together with the rest of the view helpers since
// renderCard is one of its callers (task-6/task-3; see plugin_id format
// task-2-report.md documents server-side).
function _pluginExactAppId(value) {
  if (
    typeof value !== 'string' ||
    !/^[A-Za-z0-9][A-Za-z0-9._+-]*$/.test(value)
  ) throw new Error('plugin_connection_state_invalid');
  return value;
}

function _pluginExactAppIds(started) {
  if (
    !started || typeof started !== 'object' ||
    started.error_code !== 'plugin_app_selection_required' ||
    !Array.isArray(started.app_ids) || started.app_ids.length < 2
  ) throw new Error('plugin_connection_state_invalid');
  const appIds = started.app_ids.map(_pluginExactAppId);
  if (new Set(appIds).size !== appIds.length) {
    throw new Error('plugin_connection_state_invalid');
  }
  return appIds;
}

// A catalog entry's icon renders from a same-origin path the verified catalog
// itself supplied or from the catalog's authoritative host-provider identity.
// Never infer a mark from arbitrary plugin-id text, use a hardcoded icon host,
// or reach for a retired "featured" route.
// The charset alone does not block traversal (a run of only "." characters
// is a valid charset match), so every path segment is checked explicitly:
// no segment may be empty, "." or "..". Anchoring the pattern at "^/" and
// requiring the whole string to match (no "?"/"#" in the charset) already
// rejects a scheme, a protocol-relative "//host/...", a query, and a
// fragment; percent-encoded traversal ("%2e%2e") is rejected too, since
// "%" is outside the allowed charset.
function _pluginTrustedIconPath(path) {
  if (typeof path !== 'string') return false;
  if (!/^\/api\/plugins\/[A-Za-z0-9._~-]+(?:\/[A-Za-z0-9._~-]+)*$/.test(path)) return false;
  const segments = path.slice('/api/plugins/'.length).split('/');
  return segments.every(segment => segment.length > 0 && segment !== '.' && segment !== '..');
}

// Provider identity comes from the signed directory projection, not from a
// plugin-id substring. The map is intentionally closed so an arbitrary
// catalog value cannot become a CSS class or an unverified external image.
var _PLUGIN_PROVIDER_ICON_SPECS = Object.freeze({
  google: {label: 'Google', mark: 'G', tone: 'google'},
  github: {label: 'GitHub', mark: 'GH', tone: 'github'},
  slack: {label: 'Slack', mark: 'S', tone: 'slack'},
  notion: {label: 'Notion', mark: 'N', tone: 'notion'},
  linear: {label: 'Linear', mark: 'L', tone: 'linear'},
  cloudflare: {label: 'Cloudflare', mark: 'C', tone: 'cloudflare'},
  stripe: {label: 'Stripe', mark: 'S', tone: 'stripe'},
  supabase: {label: 'Supabase', mark: 'S', tone: 'supabase'},
  sentry: {label: 'Sentry', mark: 'S', tone: 'sentry'},
  netlify: {label: 'Netlify', mark: 'N', tone: 'netlify'},
  neon: {label: 'Neon', mark: 'N', tone: 'neon'},
  miro: {label: 'Miro', mark: 'M', tone: 'miro'},
  zapier: {label: 'Zapier', mark: 'Z', tone: 'zapier'},
  wix: {label: 'Wix', mark: 'W', tone: 'wix'},
  huggingface: {label: 'Hugging Face', mark: 'HF', tone: 'huggingface'},
});

var _PLUGIN_PROVIDER_ICON_ALIASES = Object.freeze({
  google: 'google',
  github: 'github',
  slack: 'slack',
  notion: 'notion',
  'com.notion': 'notion',
  linear: 'linear',
  cloudflare: 'cloudflare',
  'com.cloudflare': 'cloudflare',
  stripe: 'stripe',
  supabase: 'supabase',
  sentry: 'sentry',
  netlify: 'netlify',
  neon: 'neon',
  miro: 'miro',
  zapier: 'zapier',
  wix: 'wix',
  huggingface: 'huggingface',
  'hugging-face': 'huggingface',
});

function _pluginProviderIconSpec(plugin) {
  const values = [
    plugin && plugin.publisher_id,
    plugin && plugin.provider_publisher_id,
    plugin && plugin.provider_id,
  ];
  for (const value of values) {
    const normalized = typeof value === 'string' ? value.trim().toLowerCase() : '';
    if (!normalized) continue;
    const exact = _PLUGIN_PROVIDER_ICON_ALIASES[normalized];
    if (exact) return _PLUGIN_PROVIDER_ICON_SPECS[exact];
    const prefix = Object.keys(_PLUGIN_PROVIDER_ICON_ALIASES).find(alias =>
      normalized.startsWith(alias + '.')
    );
    if (prefix) return _PLUGIN_PROVIDER_ICON_SPECS[_PLUGIN_PROVIDER_ICON_ALIASES[prefix]];
  }
  return null;
}

function _pluginProviderIconMarkup(plugin, esc, variant) {
  const spec = _pluginProviderIconSpec(plugin);
  if (!spec) return '';
  const root = variant === 'composer' ? 'composer-plugin-icon' : 'plugins-icon';
  return '<span class="' + root + ' ' + root + '-provider provider-icon-' + spec.tone +
    '" role="img" aria-label="' + esc(spec.label) + ' provider">' +
    '<span class="' + root + '-mark" aria-hidden="true">' + esc(spec.mark) + '</span></span>';
}

function _pluginIconMarkup(plugin, esc, variant) {
  var path = plugin && plugin.icon_asset_path;
  const root = variant === 'composer' ? 'composer-plugin-icon' : 'plugins-icon';
  if (_pluginTrustedIconPath(path)) {
    return '<span class="' + root + ' ' + root + '-asset" aria-hidden="true">' +
      '<img src="' + esc(path) + '" alt="" loading="lazy"></span>';
  }
  const providerMarkup = _pluginProviderIconMarkup(plugin, esc, variant);
  if (providerMarkup) return providerMarkup;
  // Neutral puzzle-piece glyph: no verified asset or recognized host provider
  // exists for this plugin.
  return '<span class="' + root + ' ' + root + '-default" role="img" aria-label="Plugin icon">🧩</span>';
}

// Groups a page's items by their first verified category tag, preserving
// the server's own item order inside each group (see controller ruling 1
// in task-3-brief.md: never re-sort what the server already ordered).
function _pluginGroupByCategory(items) {
  const order = [];
  const buckets = new Map();
  items.forEach(plugin => {
    const categories = Array.isArray(plugin.categories) ? plugin.categories : [];
    const key = categories.length && categories[0] ? String(categories[0]) : 'Uncategorized';
    if (!buckets.has(key)) {
      buckets.set(key, []);
      order.push(key);
    }
    buckets.get(key).push(plugin);
  });
  return order.map(key => ({category: key, items: buckets.get(key)}));
}

// Closed trust-tier label map (task-4 controller ruling 3): the server's own
// `trust_tier` string is the only key that can ever select a label -- never
// a client-invented default. `publisher_verified` renders "Publisher
// verified" and must never render "Official"; `personal` renders "Personal".
// A tier string outside this closed map is a visible failure (an alert-role
// paragraph naming the raw value), never a silently blank or default badge.
var _PLUGIN_TRUST_LABELS = {
  omega_verified: 'Omega Verified',
  publisher_verified: 'Publisher verified',
  personal: 'Personal',
};

// omega_verified is the only tier the ruling requires an evidence link for --
// linked to the entry's own verified source_url (the canonical HTTPS URL the
// signed catalog entry supplies), never a provider-name guess or hardcoded
// host. External links always carry rel="noopener noreferrer".
function _pluginTrustBadgeMarkup(details, esc) {
  const tier = details && details.trust_tier;
  const label = _PLUGIN_TRUST_LABELS[tier];
  if (!label) {
    return '<p class="plugins-trust-badge is-unknown" role="alert">Unrecognized trust tier: ' +
      esc(String(tier)) + '</p>';
  }
  let evidence = '';
  if (tier === 'omega_verified' && typeof details.source_url === 'string' && details.source_url) {
    evidence = '<p class="plugins-trust-evidence"><a href="' + esc(details.source_url) +
      '" target="_blank" rel="noopener noreferrer">verification evidence</a></p>';
  }
  return '<p class="plugins-trust-badge plugins-trust-' + esc(tier) + '">' + esc(label) + '</p>' + evidence;
}

// Every public detail field this task's controller ruling 1 lists, rendered
// only from server-verified fields -- never provider HTML, never a field the
// details response did not itself supply. `installed_authority` (capabilities,
// per-app requested scopes/authentication kind, network origins, filesystem
// access) is present only when this plugin is currently installed -- see
// mcp_platform/plugin_directory.py's installed_plugin_authority.
function _pluginDetailsSectionsMarkup(details, esc) {
  const d = details || {};
  const title = d.title || d.plugin_id || '';
  const summary = d.summary || d.description || '';
  const publisher = d.publisher_display_name || d.publisher_id || '';
  const categories = Array.isArray(d.categories) ? d.categories : [];
  const distribution = d.distribution || {};
  const offline = d.offline_capable === true ? 'Works offline'
    : (d.offline_capable === false ? 'Requires network' : '');
  const releaseHistory = Array.isArray(d.release_history) ? d.release_history : [];
  const authority = d.installed_authority;

  let links = '';
  if (typeof d.support_url === 'string' && d.support_url) {
    links += '<a href="' + esc(d.support_url) + '" target="_blank" rel="noopener noreferrer">Support</a>';
  }
  if (typeof d.source_url === 'string' && d.source_url) {
    links += (links ? ' · ' : '') +
      '<a href="' + esc(d.source_url) + '" target="_blank" rel="noopener noreferrer">Source</a>';
  }

  const rows = [];
  if (publisher) rows.push(['Publisher', esc(publisher)]);
  if (d.source_kind) rows.push(['Source', esc(d.source_kind)]);
  if (d.plugin_version) rows.push(['Version', esc(d.plugin_version)]);
  if (categories.length) rows.push(['Categories', esc(categories.join(', '))]);
  if (distribution.kind) {
    const origin = distribution.remote_origin || distribution.artifact_sha256 || '';
    rows.push(['Distribution', esc(distribution.kind) + (origin ? ' — ' + esc(origin) : '')]);
  }
  if (offline) rows.push(['Offline behavior', esc(offline)]);
  const rowsHtml = rows.map(row =>
    '<div class="plugins-detail-row"><span>' + row[0] + '</span><strong>' + row[1] + '</strong></div>'
  ).join('');

  const releaseHtml = releaseHistory.length
    ? '<section class="plugins-detail-section"><h3>Release history</h3><ul>' +
        releaseHistory.map(item => '<li>' + esc(item && item.plugin_version) + '</li>').join('') +
      '</ul></section>'
    : '';

  const authorityHtml = authority ? (
    '<section class="plugins-detail-section"><h3>Currently installed authority</h3>' +
      '<div class="plugins-detail-row"><span>Capabilities</span><strong>' +
        esc((authority.capabilities || []).join(', ') || 'none') + '</strong></div>' +
      (authority.apps || []).map(app =>
        '<div class="plugins-detail-row"><span>' + esc(app.app_id) + '</span><strong>' +
          esc(app.authentication_kind) +
          (app.requested_scopes && app.requested_scopes.length ? ' — ' + esc(app.requested_scopes.join(', ')) : '') +
        '</strong></div>'
      ).join('') +
      '<div class="plugins-detail-row"><span>Network origins</span><strong>' +
        esc((authority.network_origins || []).join(', ') || 'none') + '</strong></div>' +
      '<div class="plugins-detail-row"><span>Filesystem access</span><strong>' +
        (authority.filesystem_access ? 'Yes (runs locally)' : 'No') + '</strong></div>' +
    '</section>'
  ) : '';

  return '<h3>' + esc(title) + '</h3>' +
    (summary ? '<p class="plugins-detail-summary">' + esc(summary) + '</p>' : '') +
    _pluginTrustBadgeMarkup(d, esc) +
    '<div class="plugins-detail-rows">' + rowsHtml + '</div>' +
    (links ? '<p class="plugins-detail-links">' + links + '</p>' : '') +
    authorityHtml +
    releaseHtml;
}

// The loaded install/update review's own fields (never fabricated -- exactly
// what POST .../review returned). The exact confirmation digest is shown so
// it is legible, not just submitted invisibly.
function _pluginReviewSummaryMarkup(review, esc) {
  const r = review || {};
  const requiredApps = Array.isArray(r.required_app_ids) ? r.required_app_ids : [];
  const optionalApps = Array.isArray(r.optional_app_ids) ? r.optional_app_ids : [];
  return '<section class="plugins-detail-section"><h3>Review</h3>' +
    '<div class="plugins-detail-row"><span>Target version</span><strong>' + esc(r.plugin_version || '') + '</strong></div>' +
    '<div class="plugins-detail-row"><span>Required apps</span><strong>' + esc(requiredApps.join(', ') || 'none') + '</strong></div>' +
    (optionalApps.length
      ? '<div class="plugins-detail-row"><span>Optional apps</span><strong>' + esc(optionalApps.join(', ')) + '</strong></div>'
      : '') +
    '<div class="plugins-review-digest">review digest ' + esc(r.sha256 || '') + '</div>' +
  '</section>';
}

// Every category GET .../update-review's permission_changes can name
// (mcp_platform.plugin_lifecycle._update_permission_changes: package,
// artifact, provenance, endpoint, authentication, scopes, capability
// schemas/effects, network and filesystem boundaries), matched against the
// server's own stable change codes -- never a client-side guess at whether
// something changed. Codes tagged "kind:app_id" (endpoint_changed,
// authentication_changed, scopes_changed, capabilities_changed,
// app_added, app_removed) match by their "kind" prefix across every app;
// untagged codes (manifest_changed, package_changed, provenance_changed,
// network_boundary_changed, filesystem_boundary_changed) match exactly.
function _pluginChangeCodeMatches(changes, code) {
  return changes.some(item => item === code || item.startsWith(code + ':'));
}

function _pluginUpdateDiffRow(label, changed, detail) {
  return '<div class="plugins-diff-row' + (changed ? ' is-changed' : ' is-unchanged') + '">' +
    '<span class="plugins-diff-label">' + label + (changed ? ' <em>(changed)</em>' : '') + '</span>' +
    '<span class="plugins-diff-detail">' + detail + '</span>' +
  '</div>';
}

// Side-by-side changed fields for an update review, rendered entirely from
// GET .../update-review's own response (controller ruling, fix round 1
// finding 2) -- the complete server-computed diff, never the client's own
// 4-field guess. Version always shows a concrete before/after (an update
// review only exists because the versions differ); Endpoint additionally
// shows concrete before/after values from the details route's
// installed_authority (currently active) and catalog distribution
// (candidate) when an endpoint actually changed for the first declared
// app. Every other category -- authentication, scopes, capability
// schema/effects, app requirements, network/filesystem boundary, package
// artifact/manifest, publisher provenance -- is driven purely by whether
// its change code is present; an unchanged category renders "No change"
// with a muted, collapsed row instead of a fabricated value.
function _pluginUpdateDiffMarkup(details, installedSnapshot, review, esc) {
  const d = details || {};
  const r = review || {};
  const changes = Array.isArray(r.permission_changes) ? r.permission_changes : [];
  const has = code => _pluginChangeCodeMatches(changes, code);

  const authority = d.installed_authority || {};
  const installedApp = (authority.apps || [])[0] || {};
  const candidateEndpoint = (d.distribution && d.distribution.remote_origin) || '';
  const installedEndpoint = installedApp.remote_origin || '';
  const endpointChanged = has('endpoint_changed');
  const fromVersion = r.from_version || (installedSnapshot && installedSnapshot.package_version) || '';
  const toVersion = r.to_version || d.plugin_version || '';

  const rows = [
    _pluginUpdateDiffRow('Version', true, esc(fromVersion || '—') + ' → ' + esc(toVersion || '—')),
    _pluginUpdateDiffRow(
      'Endpoint', endpointChanged,
      endpointChanged ? (esc(installedEndpoint || '—') + ' → ' + esc(candidateEndpoint || '—')) : 'No change',
    ),
    _pluginUpdateDiffRow('Authentication kind', has('authentication_changed'), has('authentication_changed') ? 'Changed' : 'No change'),
    _pluginUpdateDiffRow('Provider scopes', has('scopes_changed'), has('scopes_changed') ? 'Changed' : 'No change'),
    _pluginUpdateDiffRow('Capability schema/effects', has('capabilities_changed'), has('capabilities_changed') ? 'Changed' : 'No change'),
    _pluginUpdateDiffRow(
      'App requirements', has('app_added') || has('app_removed'),
      (has('app_added') || has('app_removed')) ? 'Changed' : 'No change',
    ),
    _pluginUpdateDiffRow('Network access', has('network_boundary_changed'), has('network_boundary_changed') ? 'Changed' : 'No change'),
    _pluginUpdateDiffRow('Filesystem access', has('filesystem_boundary_changed'), has('filesystem_boundary_changed') ? 'Changed' : 'No change'),
    _pluginUpdateDiffRow('Package artifact', has('package_changed'), has('package_changed') ? 'Changed' : 'No change'),
    _pluginUpdateDiffRow('Package manifest', has('manifest_changed'), has('manifest_changed') ? 'Changed' : 'No change'),
    _pluginUpdateDiffRow('Publisher provenance', has('provenance_changed'), has('provenance_changed') ? 'Changed' : 'No change'),
  ];

  return '<section class="plugins-detail-section plugins-update-diff"><h3>Changed fields</h3>' +
    rows.join('') +
    '<div class="plugins-review-digest">review digest ' + esc(r.sha256 || '') + '</div>' +
  '</section>';
}

// Combines the sections above into one drawer body. `mode` is 'view' (the
// read-only Details drawer -- no review, no confirm control),
// 'install-review', or 'update-review'. The confirm control exists in the
// markup only once a review has actually loaded -- it is never rendered
// disabled-but-present while a fetch is still in flight (controller
// ruling 4: "the confirm control is enabled only inside the loaded review").
function _pluginRenderDetailsPanel(details, options) {
  const esc = options.esc;
  const mode = options.mode;
  const review = options.review;
  let body = _pluginDetailsSectionsMarkup(details, esc);
  if (mode === 'update-review') {
    // The update-review response (from_version/to_version/permission_changes/
    // sha256) carries the digest itself -- _pluginUpdateDiffMarkup renders
    // it, so the install-review-shaped summary below (which expects
    // plugin_version/required_app_ids) does not apply here.
    body += _pluginUpdateDiffMarkup(details, options.installedSnapshot, review, esc);
  } else if (review) {
    body += _pluginReviewSummaryMarkup(review, esc);
  }
  if (mode === 'install-review' || mode === 'update-review') {
    const confirmLabel = mode === 'update-review' ? 'Update' : 'Install';
    body += '<div class="plugins-review-footer">' +
      '<button type="button" class="plugins-secondary-button" data-plugin-review-cancel>Cancel</button>' +
      '<button type="button" class="plugins-install-button" data-plugin-review-confirm>' + esc(confirmLabel) + '</button>' +
    '</div>';
  }
  return body;
}

// The accessible drawer shell: role="dialog" + aria-modal, a labelled
// heading, and an explicit close control. tabindex="-1" lets the dialog
// itself receive focus on open even though it holds no naturally focusable
// element in the loading state.
function _pluginDrawerShellMarkup(bodyHtml, options) {
  const esc = options.esc;
  return '<div class="plugins-drawer-backdrop" data-plugin-drawer-backdrop>' +
    '<div class="plugins-drawer" data-plugin-drawer role="dialog" aria-modal="true" aria-labelledby="plugins-drawer-title" tabindex="-1">' +
      '<div class="plugins-drawer-head">' +
        '<h2 id="plugins-drawer-title">' + esc(options.title || 'Plugin details') + '</h2>' +
        '<button type="button" class="plugins-quiet-button" data-plugin-drawer-close aria-label="Close details">✕</button>' +
      '</div>' +
      '<div class="plugins-drawer-body" data-plugin-drawer-body>' + bodyHtml + '</div>' +
    '</div>' +
  '</div>';
}

// The in-panel destructive uninstall review (global HUD-plan constraint:
// uninstall requires an in-panel destructive review, never a browser
// confirm()). Rendered inside the same accessible drawer the details/review
// surfaces already use -- role="dialog", explicit Cancel, and a destructive
// Confirm that only ever fires the server call once clicked.
function _pluginUninstallReviewMarkup(plugin, esc) {
  const name = esc((plugin && (plugin.title || plugin.plugin_id)) || 'this plugin');
  return '<div class="plugins-detail-section plugins-uninstall-review">' +
    '<p role="alert" class="plugins-uninstall-warning">Uninstalling ' + name +
      ' disconnects it and permanently removes its local package. This cannot be undone.</p>' +
    '<div class="plugins-review-footer">' +
      '<button type="button" class="plugins-secondary-button" data-plugin-uninstall-cancel>Cancel</button>' +
      '<button type="button" class="plugins-install-button is-destructive" data-plugin-uninstall-confirm>Uninstall</button>' +
    '</div>' +
  '</div>';
}

// One catalog/installed card. Reads window.lifecycleView (core/
// plugin-permissions.js's one shared state->action/label/tone/
// disabled-actions table -- loaded before this module in every real build
// and every .mjs harness) for the label/tone/primary-action/disabled-set of
// the plugin's own canonical state; never a private copy of that table.
function renderCard(plugin, esc) {
  const view = lifecycleView(plugin);
  const connected = plugin.status === 'connected';
  // revocation_pending disables the actions that would otherwise conflict
  // with the disconnect already in flight server-side (task-6 controller
  // ruling 4) -- sourced from lifecycleView's own disabledActions, not a
  // panel-local flag.
  const disabledActions = new Set(view.disabledActions || []);
  const action = view.primaryAction ? view.primaryAction.label : view.label;
  const actionDisabled = (!view.primaryAction || view.primaryAction.disabled) ? ' disabled' : '';
  const disconnectDisabled = view.canDisconnect && !disabledActions.has('disconnect') ? '' : ' disabled aria-disabled="true"';
  const uninstallDisabled = disabledActions.has('uninstall') ? ' disabled aria-disabled="true"' : '';
  const authorityDisabled = disabledActions.has('permissions') ? ' disabled aria-disabled="true"' : '';
  const owner = plugin.publisher_display_name || plugin.publisher_id || 'signed publisher';
  const provenance = plugin.provenance === 'authoritative_remote_mcp'
    ? '<span class="plugins-official-badge">Official remote MCP</span>'
    : '';
  const management = plugin.installed ?
    '<div class="plugins-card-management" role="group" aria-label="Manage ' + esc(plugin.title) + '">' +
      (connected ? '<button type="button" class="plugins-secondary-button plugins-try-button" data-plugin-try>Try in chat</button>' : '') +
      (view.secondaryAction && view.secondaryAction.type === 'update_review'
        ? '<button type="button" class="plugins-secondary-button" data-plugin-update-review>' + esc(view.secondaryAction.label) + '</button>' : '') +
      '<button type="button" class="plugins-secondary-button" data-plugin-authority' + authorityDisabled + '>Permissions</button>' +
      '<button type="button" class="plugins-secondary-button" data-plugin-disconnect' + disconnectDisabled + '>Disconnect</button>' +
      '<button type="button" class="plugins-secondary-button is-destructive" data-plugin-uninstall' + uninstallDisabled + '>Uninstall</button>' +
    '</div>' : '';
  const nextApp = plugin.status === 'setup_required' && plugin.next_required_app_id
    ? ' data-next-plugin-app-id="' + esc(_pluginExactAppId(plugin.next_required_app_id)) + '"'
    : '';
  const statusText = view.label.toLowerCase();
  return '<article class="plugins-directory-card" data-plugin-id="' + esc(plugin.plugin_id) + '" data-plugin-title="' + esc(plugin.title) + '"' + nextApp + '>' +
    '<div class="plugins-card-icon">' + _pluginIconMarkup(plugin, esc) + '</div>' +
    '<div class="plugins-card-copy"><h3>' + esc(plugin.title) + '</h3>' +
      '<p>' + esc(plugin.description) + '</p>' +
      '<span class="plugins-card-meta">' + provenance + '<span>' + esc(owner) + '</span></span></div>' +
    '<div class="plugins-card-actions">' +
      '<button type="button" class="plugins-secondary-button" data-plugin-details aria-label="Details for ' + esc(plugin.title) + '">Details</button>' +
      '<button type="button" class="plugins-more-button" data-plugin-more aria-expanded="false" aria-label="More about ' + esc(plugin.title) + '">•••</button>' +
      '<button type="button" class="plugins-install-button' + (connected ? ' is-connected' : '') + '" data-plugin-action' + actionDisabled + '>' + esc(action) + '</button>' +
    '</div>' +
    management +
    '<div class="plugins-card-detail" data-plugin-review-panel hidden></div>' +
    '<div class="plugins-card-status" data-plugin-status>' + esc(statusText) + '</div>' +
  '</article>';
}

// One installed-strip tile. `displayTitle` is the panel's already-resolved
// pluginDisplayTitle(plugin) (the installed payload's own verified title,
// falling back to plugin_id) -- passed in rather than recomputed here since
// the panel also needs that same title for the composer-sync event.
function installedTileMarkup(plugin, esc, displayTitle) {
  const view = lifecycleView(plugin);
  return '<div class="plugins-installed-tile" title="' + esc(displayTitle) + '">' +
    _pluginIconMarkup(plugin, esc) + '<span>' + esc(displayTitle) + '</span>' +
    '<span class="plugins-installed-state ' + view.tone + '">' + esc(view.label) + '</span>' +
  '</div>';
}

// The permissions editor's own inline <form> body (provider scope,
// capability scope, "when Omega may act", autonomous authority, and the
// read-only network/filesystem info block) -- everything loadAuthority
// used to hand-build inline. `body` is GET .../permissions's own response;
// `installedAuthority` is the details route's own installed_authority
// projection (or null on a best-effort fetch miss -- the caller still shows
// the editable form above it). The Save/Cancel wiring and the submit
// handler that reads these same fields back out of the live DOM stay in the
// panel (loadAuthority) -- this only ever returns markup.
function permissionsEditorMarkup(pluginId, body, installedAuthority, esc) {
  const offered = body.offered || {};
  const selected = body.selected || {};
  const scopes = Array.isArray(offered.provider_scopes) ? offered.provider_scopes : [];
  const capabilities = Array.isArray(offered.capability_ids) ? offered.capability_ids : [];
  const selectedScopes = new Set(selected.provider_scopes || []);
  const selectedCaps = new Set(selected.capability_ids || []);
  const policy = selected.execution_policy || 'confirm_each';
  const saveLabel = body.status === 'connected' || body.status === 'authorized'
    ? 'Save and reconnect'
    : 'Save and connect';
  const scopeBoxes = scopes.map(scope =>
    '<label><input type="checkbox" data-plugin-provider-scope="' + esc(scope) + '"' +
      (selectedScopes.has(scope) ? ' checked' : '') + '> ' + esc(scope.split('/').pop() || scope) + '</label>'
  ).join('');
  const capBoxes = capabilities.map(capability =>
    '<label><input type="checkbox" data-plugin-capability="' + esc(capability) + '"' +
      (selectedCaps.has(capability) ? ' checked' : '') + '> ' + esc(String(capability).replace(/^tool:/, '')) + '</label>'
  ).join('');
  // Network and filesystem scope (task-6 Interfaces: the permission
  // editor covers them alongside selected capabilities and direct-chat
  // authority) -- read-only, sourced from the same
  // GET .../catalog/{plugin_id} installed_authority projection the
  // Details drawer already renders.
  const scopeInfoHtml = installedAuthority ? (
    '<div class="plugins-detail-row"><span>Network access</span><strong>' +
      esc((installedAuthority.network_origins || []).join(', ') || 'none') + '</strong></div>' +
    '<div class="plugins-detail-row"><span>Filesystem access</span><strong>' +
      (installedAuthority.filesystem_access ? 'Yes (runs locally)' : 'No') + '</strong></div>'
  ) : '';
  return '<fieldset><legend>Provider scope</legend>' + scopeBoxes + '</fieldset>' +
    '<fieldset><legend>Capability scope</legend>' + capBoxes + '</fieldset>' +
    '<fieldset><legend>When Omega may act</legend>' +
      '<label><input type="radio" name="plugin-execution-' + esc(pluginId) + '" data-plugin-execution-policy="direct_turn"' +
        (policy === 'direct_turn' ? ' checked' : '') + '> run selected scopes directly in chat</label>' +
      '<label><input type="radio" name="plugin-execution-' + esc(pluginId) + '" data-plugin-execution-policy="confirm_each"' +
        (policy === 'confirm_each' ? ' checked' : '') + '> ask before changes (reads run directly)</label>' +
    '</fieldset>' +
    '<p class="plugins-autonomous-unavailable">Unattended actions are unavailable because standing job grants are not supported.</p>' +
    (scopeInfoHtml
      ? '<fieldset class="plugins-scope-readonly" data-plugin-scope-readonly><legend>Network &amp; filesystem</legend>' +
          scopeInfoHtml + '</fieldset>'
      : '') +
    '<button type="submit" class="plugins-install-button">' + esc(saveLabel) + '</button>';
}

const _pluginViewsRoot = typeof window !== 'undefined' ? window : globalThis;
if (_pluginViewsRoot) {
  _pluginViewsRoot.OmegaPluginViews = Object.freeze({
    _pluginExactAppId: _pluginExactAppId,
    _pluginExactAppIds: _pluginExactAppIds,
    _pluginTrustedIconPath: _pluginTrustedIconPath,
    _PLUGIN_PROVIDER_ICON_SPECS: _PLUGIN_PROVIDER_ICON_SPECS,
    _pluginProviderIconSpec: _pluginProviderIconSpec,
    _pluginProviderIconMarkup: _pluginProviderIconMarkup,
    _pluginIconMarkup: _pluginIconMarkup,
    _pluginGroupByCategory: _pluginGroupByCategory,
    _PLUGIN_TRUST_LABELS: _PLUGIN_TRUST_LABELS,
    _pluginTrustBadgeMarkup: _pluginTrustBadgeMarkup,
    _pluginDetailsSectionsMarkup: _pluginDetailsSectionsMarkup,
    _pluginReviewSummaryMarkup: _pluginReviewSummaryMarkup,
    _pluginChangeCodeMatches: _pluginChangeCodeMatches,
    _pluginUpdateDiffRow: _pluginUpdateDiffRow,
    _pluginUpdateDiffMarkup: _pluginUpdateDiffMarkup,
    _pluginRenderDetailsPanel: _pluginRenderDetailsPanel,
    _pluginDrawerShellMarkup: _pluginDrawerShellMarkup,
    _pluginUninstallReviewMarkup: _pluginUninstallReviewMarkup,
    renderCard: renderCard,
    installedTileMarkup: installedTileMarkup,
    permissionsEditorMarkup: permissionsEditorMarkup,
  });
}

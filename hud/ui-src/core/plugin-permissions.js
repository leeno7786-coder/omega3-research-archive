// Plugin permission cards — HUD projection of PluginPermissionRequest.to_hud_dict()
// Consumes only request_id, revision, and display. Buttons carry only
// data-request-id and data-revision. Secret-shaped argument values are redacted
// server-side before the optional requested-values list reaches this module.

// ===========================================================================
// Task 6: closed, provider-neutral lifecycle view + error vocabulary for the
// plugin marketplace/settings surface. Kept here (not only inside
// panels/settings-plugins.js) so both are independently loadable and
// testable without a full panel render -- see test_plugin_permissions.mjs.
// ===========================================================================

// The 14 canonical lifecycle states mirrored verbatim from
// mcp_platform/plugin_state.py's PluginLifecycleState (StrEnum) -- the same
// closed set core/plugin-store.js's LIFECYCLE_STATES already mirrors (see
// task-2-report.md). A card's state always comes from this exact set; an
// unrecognized value fails visibly (see lifecycleView below), never a
// silent default.
var CANONICAL_PLUGIN_STATES = Object.freeze([
  'available',
  'review_required',
  'installing',
  'installed',
  'setup_required',
  'connecting',
  'connected',
  'offline',
  'update_available',
  'update_required',
  'permission_changed',
  'revocation_pending',
  'blocked',
  'incompatible',
]);

var _PLUGIN_LIFECYCLE_VIEW_LABELS = Object.freeze({
  available: 'Available',
  review_required: 'Review required',
  installing: 'Installing…',
  installed: 'Installed',
  setup_required: 'Setup required',
  connecting: 'Connecting…',
  connected: 'Connected',
  offline: 'Offline',
  update_available: 'Update available',
  update_required: 'Update required',
  permission_changed: 'Permission changed',
  revocation_pending: 'Revocation pending',
  blocked: 'Blocked',
  incompatible: 'Incompatible',
});

// One status-pill/tone class per canonical state (final fix wave W1: this
// used to be settings-plugins.js's own private _PLUGIN_LIFECYCLE_STATE_TONE
// literal -- folded in here so label, tone, primary action, and
// disabled-actions all come from this one lifecycleView call, never a
// second table). An unrecognized state renders 'is-neutral', matching the
// panel's own pre-existing fallback exactly.
var _PLUGIN_LIFECYCLE_VIEW_TONE = Object.freeze({
  connected: 'is-connected',
  available: 'is-neutral',
  installed: 'is-neutral',
  review_required: 'is-attention',
  installing: 'is-attention',
  setup_required: 'is-attention',
  connecting: 'is-attention',
  update_available: 'is-attention',
  offline: 'is-error',
  update_required: 'is-error',
  permission_changed: 'is-error',
  revocation_pending: 'is-error',
  blocked: 'is-error',
  incompatible: 'is-error',
});

// One primary action per canonical state (task-6 controller ruling 4): the
// closed card-action vocabulary is Install/Review, Connect, Continue setup,
// Retry, Update review, and Review permissions -- never a generic "Connect"
// for a plugin whose real next step is something else. `connected` and
// `incompatible` are the only two states with no primary action: a
// connected plugin's next steps live in its management row (Try in chat /
// Permissions / Disconnect / Uninstall), and an incompatible plugin cannot
// be acted on from this surface at all.
//
// This is the ONE state->action/label/tone/disabled-actions table for the
// plugin marketplace/settings surface (final fix wave W1): panels/
// settings-plugins.js calls this directly for every card/tile/filter-option
// render instead of hand-encoding its own copy -- see
// tests/hud/test_plugin_marketplace_ui.py's source-contract check and
// tests/hud/test_plugin_marketplace_panel.mjs's 14-state runtime proof.
function lifecycleView(plugin) {
  var p = plugin && typeof plugin === 'object' ? plugin : {};
  var state = p.state != null ? p.state : p.status;
  var label = _PLUGIN_LIFECYCLE_VIEW_LABELS[state];
  if (!label) {
    // Unknown state: fails visibly instead of silently guessing
    // "available"/"connected" -- see the global HUD-plan constraint "every
    // state ... and unknown states fail visibly".
    return {
      state: state,
      label: 'Unrecognized plugin state: ' + String(state),
      tone: 'is-neutral',
      primaryAction: null,
      unknown: true,
    };
  }
  var action = null;
  switch (state) {
    case 'available':
    case 'review_required':
      action = {type: 'install', label: state === 'review_required' ? 'Review' : 'Install'};
      break;
    case 'installed':
      action = {type: 'connect', label: 'Connect'};
      break;
    case 'setup_required':
      action = {type: 'continue_setup', label: 'Continue setup'};
      break;
    case 'installing':
      action = {type: 'installing', label: 'Installing…', disabled: true};
      break;
    case 'connecting':
      action = {type: 'connecting', label: 'Connecting…', disabled: true};
      break;
    case 'offline':
    case 'blocked':
    case 'revocation_pending':
      action = {type: 'retry', label: 'Retry'};
      break;
    case 'update_available':
    case 'update_required':
      action = {type: 'update_review', label: 'Update'};
      break;
    case 'permission_changed':
      action = {type: 'permissions', label: 'Review permissions'};
      break;
    default:
      action = null;
  }
  return {
    state: state,
    label: label,
    tone: _PLUGIN_LIFECYCLE_VIEW_TONE[state] || 'is-neutral',
    primaryAction: action,
    // An unfinished sign-in is cancellable even before authority is activated.
    canDisconnect: state === 'connected' || state === 'connecting',
    // A failed/unfinished connection must not hide the signed package update
    // that can repair it. Reviewing is read-only; confirmation keeps the
    // existing revision and permission gates. Preserve the real primary state.
    secondaryAction: p.installed && p.update_state === 'available' &&
      ['installed', 'setup_required', 'connecting', 'offline', 'connected'].includes(state)
      ? {type: 'update_review', label: 'Review update'} : null,
    // revocation_pending disables the actions that would otherwise conflict
    // with the disconnect already in flight server-side (task-6 controller
    // ruling 4) -- Disconnect and Uninstall, and by extension the
    // permissions editor (a fresh authority mutation would itself race the
    // pending revocation).
    disabledActions: state === 'revocation_pending'
      ? Object.freeze(['disconnect', 'uninstall', 'permissions'])
      : Object.freeze([]),
  };
}

// Closed, provider-neutral error vocabulary (task-6 controller ruling 3):
// keyed by every stable cause hud/server.py's plugin lifecycle/directory
// routes can emit (_plugin_lifecycle_error's status_by_reason map,
// _plugin_directory_error's own additions, _plugin_revision_conflict, and
// the generic _McpMutationRejected validation cause every plugin mutation
// route shares), plus this HUD's own client-local codes (plugin-api.js,
// plugin-store.js, plugin-setup.js). An unknown cause is never swallowed
// into a generic "service unavailable" message -- see the fallback below.
var _PLUGIN_ERROR_MESSAGES = Object.freeze({
  // -- lifecycle state causes (mcp_platform/plugin_state.py _ERROR_BY_STATE) --
  plugin_offline: 'This plugin is offline.',
  plugin_update_required: 'This plugin must be updated before it can be used. Review the update to continue.',
  plugin_permission_changed: 'This plugin changed its requested permissions. Review the changes to continue.',
  plugin_revocation_pending: 'Omega is finishing this plugin’s disconnect. Retry in a moment.',
  plugin_blocked: 'This plugin is blocked and cannot be used right now.',
  plugin_runtime_unsupported: 'This plugin is not compatible with this version of Omega.',
  plugin_setup_required: 'This plugin needs setup before it can be used. Continue setup to proceed.',
  // -- catalog/install/review/update causes --
  plugin_review_required: 'This plugin requires review before it can be used. Review it to continue.',
  plugin_catalog_entry_unavailable: 'This plugin is no longer listed in the signed catalog.',
  plugin_not_installed: 'Install this plugin before connecting.',
  plugin_app_not_installed: 'That app is not part of this plugin. Retry from the plugin page.',
  plugin_catalog_unavailable: 'The signed plugin catalog is unavailable. Check the local package and retry.',
  plugin_catalog_signature_invalid: 'The signed plugin catalog could not be verified.',
  plugin_catalog_identity_ambiguous: 'Two plugin sources returned the same plugin ID. Contact your administrator.',
  plugin_catalog_cursor_stale: 'The plugin catalog changed while loading. Retry to see the latest results.',
  plugin_catalog_cursor_invalid: 'This results page expired. Retry to reload from the first page.',
  plugin_catalog_authority_missing: 'The signed plugin catalog is missing required authority data. Restart the HUD and retry.',
  plugin_package_unavailable: 'The plugin package could not be read. Restart the HUD and retry.',
  plugin_package_integrity_invalid: 'The plugin package failed its integrity check and cannot be installed.',
  mcp_app_registry_unreadable: 'Omega cannot read the saved plugin app records. Plugin recovery is required.',
  plugin_registry_unreadable: 'Omega cannot read the saved plugin installation records. Plugin recovery is required.',
  plugin_registry_reconciliation_failed: 'Omega could not verify an installed plugin against its saved records. Plugin recovery is required before it can be used.',
  plugin_legacy_recovery_required: 'Plugin recovery was interrupted. Complete recovery before connecting a plugin.',
  plugin_source_untrusted: 'This plugin source is not trusted and cannot be installed.',
  plugin_install_review_changed: 'This plugin changed since you reviewed it. Review it again to continue.',
  plugin_update_not_available: 'No update is available for this plugin.',
  plugin_install_confirmation_mismatch: 'This review no longer matches the plugin. Review it again to continue.',
  plugin_setup_not_required: 'This plugin has no further setup required.',
  plugin_app_selection_required: 'Choose which app to set up before continuing.',
  // No explicit per-app selection (or one outside the app's current offer):
  // raised by the server's connection start/permissions routes and by the
  // panel's setup controls when no consent was recorded for the app.
  plugin_scopes_invalid: 'Choose which permissions this app may use, then save to connect.',
  // The chosen tools are not exactly tools the app declares (the app changed
  // since its permissions were shown, or the selection is incomplete).
  plugin_capability_scope_invalid: 'The tools you chose no longer match what this app provides. Review its permissions and save again.',
  plugin_execution_policy_invalid: 'Choose whether this app runs directly in chat or asks for each action, then save again.',
  plugin_capability_invalid: 'The plugin returned capabilities that Omega cannot safely activate.',
  plugin_account_unconfigured: 'The Omega operator account is not configured for plugins.',
  plugin_secret_required: 'This plugin requires credentials Omega cannot yet collect from Settings.',
  plugin_uninstall_incomplete: 'This plugin could not be fully removed. Retry the uninstall.',
  plugin_uninstall_failed: 'The plugin could not be removed safely. Retry from the plugin page.',
  plugin_lifecycle_operation_in_progress: 'Another change to this plugin is already in progress. Retry in a moment.',
  plugin_transport_unavailable: 'The plugin endpoint could not be reached. Check your connection and retry.',
  plugin_connection_state_invalid: 'The plugin returned an invalid connection state. Retry or restart Omega.',
  // -- panel-only causes folded in here on fix round 1 (one shared vocabulary) --
  protocol_mismatch: 'The plugin endpoint does not support Omega’s negotiated MCP protocol.',
  identity_mismatch: 'The plugin endpoint identity does not match its signed Omega configuration.',
  capability_drift: 'The plugin capabilities no longer match the signed Omega configuration.',
  authorization_required: 'The provider authorization is no longer valid. Connect the plugin again.',
  authorization_invalid: 'The provider returned an invalid authorization response. Connect the plugin again.',
  oauth_device_flow_disabled: 'Device authorization is disabled for this app. Enable it in the provider’s app settings, then retry.',
  plugin_connection_cancelled: 'Plugin connection cancelled.',
  plugin_connection_required: 'Connect this plugin before using it in chat.',
  plugin_interaction_unavailable: 'This plugin is installed but is not currently available to chat.',
  plugin_directory_entry_unavailable: 'This plugin is no longer listed in the signed directory.',
  plugin_directory_unavailable: 'The signed plugin directory is unavailable. Restart the HUD and retry.',
  plugin_directory_invalid: 'The plugin directory response was invalid. Retry.',
  plugin_lifecycle_unavailable: 'The plugin service is unavailable. Restart the HUD and retry.',
  plugin_operation_failed: "The provider refused this request. Check this account's access with the provider, then retry.",
  oauth_provider_rejected: "The provider refused the sign-in. Check this account's access with the provider, then retry.",
  plugin_revision_conflict: 'This plugin changed while you were reviewing it. Showing the latest information — review again to continue.',
  invalid_plugin_lifecycle_request: 'That plugin request was invalid. Adjust your filters and retry.',
  // -- authorization causes (provider-neutral: kind + recovery only) --
  plugin_auth_profile_unavailable: 'The signed authorization profile for this plugin is unavailable. Check the official catalog and retry.',
  plugin_auth_denied: 'Authorization for this plugin was denied.',
  plugin_auth_expired: 'The authorization for this plugin expired. Start the connection again.',
  plugin_authorization_failed: 'Sign-in could not be completed. Check the connection and try again.',
  plugin_auth_provider_unreachable: 'This plugin provider is offline. Reconnect and retry.',
  plugin_authorization_required: 'This plugin needs authorization before it can be used. Connect it to continue.',
  oauth_profile_catalog_unavailable: 'The signed OAuth profile catalog is unavailable. Restart the HUD and retry.',
  oauth_profile_unavailable: 'The signed OAuth profile is unavailable. Check the official catalog and retry.',
  oauth_vault_locked: 'The OAuth credential vault is locked. Unlock Omega and retry.',
  oauth_issuer_mismatch: 'The provider sign-in identity does not match the signed plugin configuration. Omega blocked the connection. Check for an official plugin update.',
  oauth_device_authorization_expired: 'The sign-in code expired. Start the connection again.',
  oauth_authorization_denied: 'Plugin authorization was denied.',
  oauth_token_partition_conflict: 'A previous plugin disconnect is still being completed. Retry Connect.',
  oauth_revocation_pending: 'Omega is finishing the previous plugin disconnect. Retry in a moment.',
  oauth_revocation_unsupported: 'The provider could not finish disconnecting this plugin. Retry while online.',
  oauth_provider_unreachable: 'This plugin provider is offline. Omega remains available; reconnect and retry.',
  oauth_provider_timeout: 'The plugin provider did not respond in time. Retry when you are online.',
  oauth_registration_unsupported: 'This plugin’s provider does not support automatic sign-in setup yet. It cannot be connected.',
  oauth_registration_invalid: 'The plugin provider returned an invalid sign-in setup response. Retry, or try again later.',
  oauth_client_registration_rejected: 'The plugin provider rejected automatic sign-in setup. Retry later, or contact the provider if this continues.',
  // -- client-local causes (plugin-api.js / plugin-store.js / plugin-setup.js) --
  plugin_api_network_unavailable: 'Omega could not reach the plugin service. Check your connection and retry.',
  plugin_api_response_invalid: 'The plugin service returned an unexpected response. Retry.',
  plugin_api_mutation_body_invalid: 'Omega could not send that request. Retry.',
  plugin_state_invalid: 'The plugin service returned an unrecognized state. Retry or restart Omega.',
  plugin_store_event_invalid: 'Omega could not process that plugin update. Retry.',
  plugin_setup_secret_field_required: 'Fill in every required field before continuing.',
  plugin_setup_credentials_unavailable: 'Omega does not yet support submitting these credentials from Settings.',
});

// Closed lookup only -- an unrecognized cause renders the cause string
// itself, visibly, rather than collapsing to a generic message (task-6
// controller ruling 3: "an unknown cause renders the cause string
// visibly, never 'The plugin service is unavailable.'").
function readablePluginError(error) {
  var code = error && error.code ? error.code : String((error && error.message) || error || '');
  if (Object.prototype.hasOwnProperty.call(_PLUGIN_ERROR_MESSAGES, code)) {
    return _PLUGIN_ERROR_MESSAGES[code];
  }
  return code || 'Plugin connection failed.';
}

const _pluginPermissionCards = new Map();
const _pluginPermissionBindings = new Map();
const _PLUGIN_PERMISSION_RISK_LABELS = Object.freeze({
  read_only: 'Reads information',
  additive_write: 'Creates information',
  mutating_write: 'Changes existing information',
  destructive: 'Destructive action',
  send_publish: 'Sends or publishes information',
  cross_provider_transfer: 'Transfers information between providers',
  external_unreviewed: 'Unreviewed external action',
});

function _pluginPermText(value) {
  return String(value == null ? '' : value);
}

function _pluginPermAttr(value) {
  return typeof _escapeHtml === 'function' ? _escapeHtml(value) : _pluginPermText(value);
}

function _pluginPermEl(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null && text !== '') node.textContent = _pluginPermText(text);
  return node;
}

function _pluginPermissionFailureMessage(error) {
  const reason = _pluginPermText(error).split(':', 1)[0].trim();
  if (reason === 'plugin_permission_replayed') {
    return 'This permission request was already used.';
  }
  if (reason === 'plugin_permission_invalidated' || reason === 'plugin_permission_lineage_conflict') {
    return 'This permission request drifted and is no longer valid.';
  }
  if (reason === 'plugin_connection_disconnected') {
    return 'The plugin connection was disconnected.';
  }
  if (reason === 'plugin_permission_cancelled') {
    return 'This permission request was cancelled.';
  }
  return 'This permission request is no longer valid.';
}

function _pluginPermissionFeed() {
  return typeof document !== 'undefined' && document.getElementById
    ? document.getElementById('feed')
    : null;
}

function _watchPluginPermissionCycle(cycleId, thinking) {
  if (!cycleId) return;
  const root = typeof window !== 'undefined' ? window : globalThis;
  const responses = (typeof _pendingChatResponses !== 'undefined' && _pendingChatResponses)
    || (root && root._pendingChatResponses);
  const since = (typeof _pendingChatSince !== 'undefined' && _pendingChatSince)
    || (root && root._pendingChatSince);
  try {
    if (responses && typeof responses.set === 'function') {
      responses.set(cycleId, thinking || null);
    }
    if (since && typeof since.has === 'function' && typeof since.set === 'function' && !since.has(cycleId)) {
      since.set(cycleId, Date.now());
    }
  } catch (_err) {
    /* response store is optional in isolated tests */
  }
}

function pluginPermissionBoundCycle(requestId) {
  const binding = _pluginPermissionBindings.get(requestId);
  return binding ? binding.cycleId : undefined;
}

function bindPluginPermissionTask(requestId, cycleId, thinking) {
  const current = _pluginPermissionBindings.get(requestId) || {};
  current.cycleId = cycleId || current.cycleId || '';
  current.thinking = thinking || current.thinking || null;
  _pluginPermissionBindings.set(requestId, current);
  const cardState = _pluginPermissionCards.get(requestId);
  if (cardState) cardState.thinking = current.thinking;
}

function settlePluginPermissionCard(requestId, result) {
  const binding = _pluginPermissionBindings.get(requestId) || {};
  if (result && result.cycle_id) binding.cycleId = result.cycle_id;
  _pluginPermissionBindings.set(requestId, binding);
  if (binding.thinking && typeof updateThinkingBlock === 'function') {
    updateThinkingBlock(binding.thinking, result, result && result.ok === false ? 'degraded' : 'resolved');
  }
  const state = _pluginPermissionCards.get(requestId);
  if (state && state.card && typeof state.card.remove === 'function') state.card.remove();
  _pluginPermissionCards.delete(requestId);
}

function settlePluginPermissionByCycle(cycleId, result) {
  if (!cycleId) return;
  if (result && result.queued) return;
  if (result && /pending_input/i.test(_pluginPermText(result.error))) return;
  for (const [requestId, binding] of _pluginPermissionBindings) {
    if (binding && binding.awaitingContinuation && binding.cycleId === cycleId) {
      settlePluginPermissionCard(requestId, result);
      return;
    }
  }
}

function renderPluginPermissionCard(request) {
  const payload = request && typeof request === 'object' ? request : {};
  const display = payload.display && typeof payload.display === 'object' ? payload.display : {};
  const requestId = _pluginPermText(payload.request_id);
  const revision = _pluginPermText(payload.revision);
  const existing = _pluginPermissionCards.get(requestId);
  if (existing && existing.card) return existing.card;

  const card = _pluginPermEl('div', 'plugin-permission-card');
  const title = _pluginPermEl('div', 'plugin-permission-title', 'Permission required');
  card.appendChild(title);

  const apps = Array.isArray(display.plugin_apps) ? display.plugin_apps : [];
  apps.forEach(app => {
    if (!app || typeof app !== 'object') return;
    const row = _pluginPermEl('div', 'plugin-permission-app');
    const pluginName = _pluginPermEl('strong', '', app.plugin_name);
    const appName = _pluginPermEl('span', 'plugin-permission-app-name', app.app_name);
    row.appendChild(pluginName);
    row.appendChild(appName);
    card.appendChild(row);
  });

  if (display.effect_summary) {
    card.appendChild(_pluginPermEl('p', 'plugin-permission-effect', display.effect_summary));
  }

  const operations = Array.isArray(display.ordered_operations) ? display.ordered_operations : [];
  if (operations.length) {
    const list = _pluginPermEl('ol', 'plugin-permission-operations');
    operations.forEach(item => list.appendChild(_pluginPermEl('li', '', item)));
    card.appendChild(list);
  }

  const destination = _pluginPermText(display.destination).trim();
  if (destination && destination !== '{}') {
    const row = _pluginPermEl('div', 'plugin-permission-destination');
    row.appendChild(_pluginPermEl('span', 'plugin-permission-label', 'Destination'));
    row.appendChild(_pluginPermEl('span', '', destination));
    card.appendChild(row);
  }

  const audience = _pluginPermText(display.audience).trim();
  if (audience && audience !== '{}') {
    const row = _pluginPermEl('div', 'plugin-permission-audience');
    row.appendChild(_pluginPermEl('span', 'plugin-permission-label', 'Audience'));
    row.appendChild(_pluginPermEl('span', '', audience));
    card.appendChild(row);
  }

  if (display.crosses_providers) {
    card.appendChild(_pluginPermEl(
      'p',
      'plugin-permission-cross',
      'This action crosses provider boundaries.',
    ));
  }

  const risks = Array.isArray(display.risks) ? display.risks.filter(Boolean) : [];
  if (risks.length) {
    const row = _pluginPermEl('div', 'plugin-permission-risks');
    row.appendChild(_pluginPermEl('span', 'plugin-permission-label', 'Risk'));
    const list = _pluginPermEl('ul', 'plugin-permission-risk-list');
    risks.forEach(risk => {
      const label = _PLUGIN_PERMISSION_RISK_LABELS[_pluginPermText(risk)] || 'Unclassified risk';
      list.appendChild(_pluginPermEl('li', '', label));
    });
    row.appendChild(list);
    card.appendChild(row);
  }

  const fields = Array.isArray(display.field_names) ? display.field_names.filter(Boolean) : [];
  if (fields.length) {
    const row = _pluginPermEl('div', 'plugin-permission-fields');
    row.appendChild(_pluginPermEl('span', 'plugin-permission-label', 'Fields'));
    row.appendChild(_pluginPermEl('span', '', fields.join(', ')));
    card.appendChild(row);
  }

  const argumentPreviews = Array.isArray(display.argument_previews)
    ? display.argument_previews.filter(Boolean)
    : [];
  if (argumentPreviews.length) {
    const row = _pluginPermEl('div', 'plugin-permission-arguments');
    row.appendChild(_pluginPermEl('span', 'plugin-permission-label', 'Requested values'));
    const list = _pluginPermEl('ul', 'plugin-permission-argument-list');
    argumentPreviews.forEach(item => {
      list.appendChild(_pluginPermEl('li', '', _pluginPermText(item)));
    });
    row.appendChild(list);
    card.appendChild(row);
  }

  const status = _pluginPermEl('p', 'plugin-permission-status');
  status.hidden = true;
  card.appendChild(status);

  const actions = _pluginPermEl('div', 'plugin-permission-actions');
  const allow = _pluginPermEl('button', 'plugin-permission-allow', 'Allow');
  const cancel = _pluginPermEl('button', 'plugin-permission-cancel', 'Cancel');
  allow.type = 'button';
  cancel.type = 'button';
  allow.setAttribute('data-request-id', _pluginPermAttr(requestId));
  allow.setAttribute('data-revision', _pluginPermAttr(revision));
  cancel.setAttribute('data-request-id', _pluginPermAttr(requestId));
  cancel.setAttribute('data-revision', _pluginPermAttr(revision));
  actions.appendChild(allow);
  actions.appendChild(cancel);
  card.appendChild(actions);

  const state = {
    card,
    allow,
    cancel,
    status,
    posting: false,
    thinking: (_pluginPermissionBindings.get(requestId) || {}).thinking || null,
  };
  _pluginPermissionCards.set(requestId, state);

  const decide = async action => {
    if (state.posting || allow.disabled || cancel.disabled) return;
    state.posting = true;
    allow.disabled = true;
    cancel.disabled = true;
    try {
      const response = await fetch(
        '/api/plugins/permissions/' + encodeURIComponent(requestId) + '/' + action,
        {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ expected_revision: revision }),
        },
      );
      let body = {};
      try { body = await response.json(); } catch (_err) { body = {}; }
      if (!response.ok || !body.ok) {
        status.hidden = false;
        status.textContent = _pluginPermissionFailureMessage(body.error || '');
        if (response.status >= 500) {
          state.posting = false;
          allow.disabled = false;
          cancel.disabled = false;
        }
        return;
      }
      const binding = _pluginPermissionBindings.get(requestId) || {};
      if (body.task_id) binding.cycleId = body.task_id;
      binding.awaitingContinuation = true;
      _pluginPermissionBindings.set(requestId, binding);
      _watchPluginPermissionCycle(binding.cycleId, binding.thinking || state.thinking);
    } catch (_err) {
      status.hidden = false;
      status.textContent = _pluginPermissionFailureMessage('');
      state.posting = false;
      allow.disabled = false;
      cancel.disabled = false;
    }
  };

  allow.addEventListener('click', event => {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    decide('approve');
  });
  cancel.addEventListener('click', event => {
    if (event && typeof event.preventDefault === 'function') event.preventDefault();
    decide('cancel');
  });
  return card;
}

async function refreshPendingPluginPermissions() {
  const feed = _pluginPermissionFeed();
  try {
    const response = await fetch('/api/plugins/permissions/pending', { cache: 'no-store' });
    let body = {};
    try { body = await response.json(); } catch (_err) { body = {}; }
    const requests = body && body.ok && Array.isArray(body.requests) ? body.requests : [];
    const liveIds = new Set(
      requests.map(item => _pluginPermText(item && item.request_id)).filter(Boolean),
    );
    for (const [requestId, state] of Array.from(_pluginPermissionCards.entries())) {
      const binding = _pluginPermissionBindings.get(requestId) || {};
      if (liveIds.has(requestId) || binding.awaitingContinuation) continue;
      if (state && state.card && typeof state.card.remove === 'function') state.card.remove();
      _pluginPermissionCards.delete(requestId);
    }
    requests.forEach(item => {
      const card = renderPluginPermissionCard(item);
      if (feed && card && !card.parentNode) appendToFeed(card, feed);
    });
    if (typeof _scrollFeedToBottom === 'function') _scrollFeedToBottom();
    return requests;
  } catch (_err) {
    return [];
  }
}

if (typeof document !== 'undefined' && document.addEventListener) {
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshPendingPluginPermissions();
  });
}

const _pluginPermissionRoot = typeof window !== 'undefined' ? window : globalThis;
if (_pluginPermissionRoot) {
  _pluginPermissionRoot.renderPluginPermissionCard = renderPluginPermissionCard;
  _pluginPermissionRoot.refreshPendingPluginPermissions = refreshPendingPluginPermissions;
  _pluginPermissionRoot.bindPluginPermissionTask = bindPluginPermissionTask;
  _pluginPermissionRoot.settlePluginPermissionCard = settlePluginPermissionCard;
  _pluginPermissionRoot.settlePluginPermissionByCycle = settlePluginPermissionByCycle;
  _pluginPermissionRoot.pluginPermissionBoundCycle = pluginPermissionBoundCycle;
  _pluginPermissionRoot.CANONICAL_PLUGIN_STATES = CANONICAL_PLUGIN_STATES;
  _pluginPermissionRoot.lifecycleView = lifecycleView;
  _pluginPermissionRoot.readablePluginError = readablePluginError;
}

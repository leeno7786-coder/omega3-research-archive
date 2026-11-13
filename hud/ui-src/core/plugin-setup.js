// Generic multi-app authentication and setup driver (task-5).
//
// The server is the only authority on which action connects one app:
// GET /api/plugins/{plugin_id}/connection (hud/server.py) returns a
// PluginLifecycleSnapshot plus one closed `setup_action` --
// 'connected' | 'browser' | 'device' | 'secret_form' | 'none' -- derived
// from that app's declared PluginAuthenticationKind
// (plugin_platform/contracts.py). This module executes exactly the
// declared action; it never brands, guesses, or branches on a
// provider/publisher name. One start() call sets up one app, under the exact
// selection the operator consented to for that app: when a composite plugin
// still names a further `next_required_app_id`, start() stops and returns
// that snapshot (with its fresh revision) instead of starting the next app
// under the previous app's consent -- the caller shows the next app its own
// consent form and calls start() again for it.
//
// `controls` (every function below is supplied by the caller -- see
// panels/settings-plugins.js for the production wiring and
// tests/hud/test_plugin_marketplace_setup.mjs for the fixture wiring):
//   pluginId                    -- the plugin being set up
//   revision                    -- the client's currently known lifecycle revision
//   api.startConnection(pluginId, revision, appId) -> ConnectionStart-shaped;
//                                   caller includes the exact settings the
//                                   operator selected for this app
//   api.pollConnection(pluginId, revision, requestId) -> poll result
//   api.status(pluginId)        -- the extended GET .../connection snapshot
//                                   (must include `setup_action` for the
//                                   snapshot's own `next_required_app_id`)
//   api.submitSecretFields(pluginId, revision, appId, values) -> snapshot
//                                   (optional -- no server route exists yet;
//                                   see task-5-report.md)
//   popup                       -- an already-opened popup window, or
//                                   null/undefined if none was reserved or
//                                   the browser blocked it. Must be opened
//                                   synchronously inside the user gesture by
//                                   the caller (see settings-plugins.js) --
//                                   this module only ever navigates it, and
//                                   treats it as spent after one browser or
//                                   device step (a provider's OAuth callback
//                                   page closes the popup itself once it
//                                   posts back, so it cannot be reused for a
//                                   second required app's authorization
//                                   without a fresh user gesture).
//   origin                      -- the expected window.location.origin for
//                                   a trusted callback postMessage
//   addMessageListener(handler) -- registers a window 'message' handler,
//                                   returns an unsubscribe function
//   navigateSameWindow(url)     -- popup-blocked (or already-spent-popup)
//                                   fallback: same-window navigation
//   finish(popup, connected)    -- called once when the app's step ends; the
//                                   caller decides how to close/return the
//                                   popup (or same-window) to Omega
//   onDeviceCode({userCode, verificationUri, expiresAt})
//   onStatus(text)
//   onSnapshot(snapshot)        -- called once the app's step completes, with
//                                   that step's fresh lifecycle snapshot
//   isCancelled()                -- checked before/after every device wait
//   wait(ms) -> Promise           -- injectable sleep (tests use fake timers)
//   now() -> milliseconds since epoch -- injectable clock
//   collectSecretFields(fields, {pluginId, appId}) -> Promise<{field_id: value}>
//   clearSecretFields()          -- called once submitted values are no
//                                   longer needed, success or failure alike
(function () {
  'use strict';

  function _pluginSetupError(code) {
    var error = new Error(code);
    error.code = code;
    return error;
  }

  function _pluginSetupIdentity(value) {
    return typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._+-]*$/.test(value) ? value : null;
  }

  function _pluginSetupSnapshot(snapshot) {
    if (
      !snapshot || typeof snapshot !== 'object' ||
      typeof snapshot.state !== 'string' || !snapshot.state
    ) throw _pluginSetupError('plugin_connection_state_invalid');
    return snapshot;
  }

  function _pluginSetupRequireString(value, code) {
    if (typeof value !== 'string' || !value) throw _pluginSetupError(code);
    return value;
  }

  // ---- none: the app declares PluginAuthenticationKind.NONE --------------
  // The lifecycle service still needs exactly one connection/start call per
  // app (even a credentialless one) to compile its authority -- there is no
  // separate "no-op" server call.
  async function completeDirectConnection(context) {
    var controls = context.controls;
    await controls.api.startConnection(controls.pluginId, controls.revision, context.appId);
    return await controls.api.status(controls.pluginId);
  }

  // ---- browser: authorization-code/PKCE or a client-metadata document ----
  async function startBrowserAuthorization(context) {
    var controls = context.controls;
    var started = await controls.api.startConnection(controls.pluginId, controls.revision, context.appId);
    if (started && started.status === 'connected') {
      return await controls.api.status(controls.pluginId);
    }
    var url = _pluginSetupRequireString(
      started && started.authorization_url, 'plugin_connection_state_invalid',
    );
    var popup = controls.popup;
    var navigated = false;
    if (popup && !popup.closed) {
      try {
        popup.location.replace(url);
        popup.focus();
        navigated = true;
      } catch (_navigateError) { /* fall through to the same-window fallback below */ }
    }
    // A popup is spent the moment it is used for one authorization round --
    // the provider's callback page closes it itself once it posts back
    // (hud/server.py's /oauth/{profile_id}/callback), so a later required
    // app in the same composite chain can never reuse it without a fresh
    // user gesture. Clearing it here makes that explicit and deterministic
    // rather than racing the popup's own close timer.
    controls.popup = null;
    if (!navigated) {
      if (typeof controls.navigateSameWindow !== 'function') {
        throw _pluginSetupError('plugin_connection_state_invalid');
      }
      controls.navigateSameWindow(url);
    }
    if (typeof controls.onStatus === 'function') controls.onStatus('authorization required');
    await _pluginSetupAwaitCallback(controls, {
      pluginId: controls.pluginId, appId: context.appId, requestId: started.request_id,
    });
    return await controls.api.status(controls.pluginId);
  }

  // The popup (or same-window return trip) posts one message back to this
  // origin -- see hud/server.py's /oauth/{profile_id}/callback. The message
  // is trusted only as a "check now" signal: the resolved app/plugin state
  // always comes from the next awaited api.status() call, never from the
  // message body itself. A message that does not match this exact origin,
  // this exact plugin, and this in-flight app is ignored outright (this
  // handler simply keeps waiting) rather than waking a setup flow it was
  // never meant for.
  function _pluginSetupAwaitCallback(controls, expected) {
    if (typeof controls.addMessageListener !== 'function') {
      throw _pluginSetupError('plugin_connection_state_invalid');
    }
    return new Promise(function (resolve, reject) {
      var settled = false;
      var unsubscribe = controls.addMessageListener(function (event) {
        if (settled) return;
        if (!event || event.origin !== controls.origin) return;
        var data = event.data;
        if (!data || typeof data !== 'object') return;
        if (data.type === 'omega:plugin-authorization-failed') {
          if (typeof expected.requestId !== 'string' || !expected.requestId || data.requestId !== expected.requestId) return;
          settled = true;
          if (typeof unsubscribe === 'function') unsubscribe();
          reject(_pluginSetupError(
            data.errorCode === 'plugin_auth_expired' ? 'plugin_auth_expired' : 'plugin_authorization_failed',
          ));
          return;
        }
        if (data.type !== 'omega:plugin-connected' && data.type !== 'omega:plugin-setup-required') return;
        if (data.pluginId !== expected.pluginId) return;
        if (data.type === 'omega:plugin-setup-required') {
          // The app_id this message type carries is the *next* app -- it
          // must be a real app identity, and it can never legitimately be
          // the app this flow just finished authorizing (that would mean
          // the server is asking us to set up the very app we just set up).
          var nextAppId = _pluginSetupIdentity(data.nextAppId);
          if (nextAppId === null || nextAppId === expected.appId) return;
        } else if (data.nextAppId != null) {
          return;
        }
        settled = true;
        if (typeof unsubscribe === 'function') unsubscribe();
        resolve(data);
      });
    });
  }

  // ---- device: PluginAuthenticationKind.OAUTH_DEVICE_CODE ----------------
  function _pluginSetupClampInterval(value, fallback) {
    var seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds <= 0) seconds = fallback;
    return Math.max(1, Math.min(300, seconds));
  }

  async function startDeviceAuthorization(context) {
    var controls = context.controls;
    var started = await controls.api.startConnection(controls.pluginId, controls.revision, context.appId);
    if (started && started.status === 'connected') {
      return await controls.api.status(controls.pluginId);
    }
    var requestId = _pluginSetupRequireString(
      started && started.request_id, 'plugin_connection_state_invalid',
    );
    var expiresAt = Number(started.expires_at);
    if (!Number.isFinite(expiresAt) || expiresAt <= 0) {
      throw _pluginSetupError('plugin_connection_state_invalid');
    }
    var userCode = started.user_code;
    var popup = controls.popup;
    // The verification page is opened opportunistically -- device flow
    // never depends on the popup (the user can open verification_uri
    // themselves), so a spent/blocked popup is never a hard failure here.
    if (popup && !popup.closed && typeof started.authorization_url === 'string' && started.authorization_url) {
      try {
        popup.location.replace(started.authorization_url);
        popup.focus();
      } catch (_navigateError) { /* polling continues regardless */ }
    }
    controls.popup = null;
    if (typeof controls.onDeviceCode === 'function') {
      controls.onDeviceCode({
        userCode: userCode,
        verificationUri: started.verification_uri,
        expiresAt: expiresAt,
      });
    }
    var now = typeof controls.now === 'function' ? controls.now : Date.now;
    var wait = typeof controls.wait === 'function' ? controls.wait : function (ms) {
      return new Promise(function (resolve) { setTimeout(resolve, ms); });
    };
    var isCancelled = typeof controls.isCancelled === 'function' ? controls.isCancelled : function () { return false; };
    var intervalSeconds = _pluginSetupClampInterval(started.poll_interval_seconds, 5);
    while (true) {
      if (isCancelled()) throw _pluginSetupError('plugin_connection_cancelled');
      if (now() / 1000 >= expiresAt) throw _pluginSetupError('oauth_device_authorization_expired');
      await wait(intervalSeconds * 1000);
      if (isCancelled()) throw _pluginSetupError('plugin_connection_cancelled');
      var result = await controls.api.pollConnection(controls.pluginId, controls.revision, requestId);
      // Completion returns the aggregate lifecycle snapshot (`state`),
      // including setup_required when a composite has another app left.
      // `status` belongs only to the pending/finalizing poll response.
      if (result && typeof result.state === 'string') {
        return await controls.api.status(controls.pluginId);
      }
      var retryAfter = result && result.retry_after_seconds;
      if (retryAfter != null) {
        intervalSeconds = Math.max(intervalSeconds, _pluginSetupClampInterval(retryAfter, intervalSeconds));
      }
      if (typeof controls.onStatus === 'function') {
        controls.onStatus(
          userCode ? 'Enter code ' + userCode + ' to finish connecting' : 'Waiting for authorization…',
        );
      }
    }
  }

  // ---- secret_form: api_key / bearer_token / basic / declared_secret_fields
  function _pluginSetupSecretFields(fields) {
    if (!Array.isArray(fields) || !fields.length) {
      throw _pluginSetupError('plugin_connection_state_invalid');
    }
    return fields.map(function (field) {
      if (
        !field || typeof field !== 'object' ||
        typeof field.field_id !== 'string' || !field.field_id ||
        typeof field.label !== 'string' || !field.label ||
        typeof field.secret !== 'boolean' ||
        typeof field.required !== 'boolean'
      ) throw _pluginSetupError('plugin_connection_state_invalid');
      return field;
    });
  }

  function _pluginSetupValidateSecretValues(fields, values) {
    if (!values || typeof values !== 'object') {
      throw _pluginSetupError('plugin_connection_state_invalid');
    }
    fields.forEach(function (field) {
      var value = values[field.field_id];
      if (field.required && (typeof value !== 'string' || !value)) {
        throw _pluginSetupError('plugin_setup_secret_field_required');
      }
      if (value != null && typeof value !== 'string') {
        throw _pluginSetupError('plugin_connection_state_invalid');
      }
    });
  }

  function _pluginSetupClearValues(values) {
    if (!values || typeof values !== 'object') return;
    Object.keys(values).forEach(function (key) {
      values[key] = '';
      delete values[key];
    });
  }

  async function collectDeclaredCredentials(context) {
    var controls = context.controls;
    var fields = _pluginSetupSecretFields(context.snapshot && context.snapshot.secret_fields);
    if (typeof controls.collectSecretFields !== 'function') {
      throw _pluginSetupError('plugin_connection_state_invalid');
    }
    var values = null;
    try {
      values = await controls.collectSecretFields(fields, {pluginId: controls.pluginId, appId: context.appId});
      _pluginSetupValidateSecretValues(fields, values);
      if (!controls.api || typeof controls.api.submitSecretFields !== 'function') {
        // No credential intake exists in the connection service today (task-5
        // controller ruling 2) -- collected values are still cleared in the
        // `finally` below; nothing is ever retained past this point.
        throw _pluginSetupError('plugin_setup_credentials_unavailable');
      }
      var snapshot = await controls.api.submitSecretFields(
        controls.pluginId, controls.revision, context.appId, values,
      );
      return _pluginSetupSnapshot(snapshot);
    } finally {
      // Secret values never persist past submission -- in memory or in the
      // DOM (task-5 controller ruling 3) -- success or failure alike.
      _pluginSetupClearValues(values);
      if (typeof controls.clearSecretFields === 'function') controls.clearSecretFields();
    }
  }

  var SETUP_ACTIONS = Object.freeze({
    browser: startBrowserAuthorization,
    device: startDeviceAuthorization,
    secret_form: collectDeclaredCredentials,
    none: completeDirectConnection,
    connected: async function (context) { return context.snapshot; },
  });

  // Defensive bound on loop iterations, well beyond any real plugin's
  // declared app count -- guards a malformed server response from looping
  // forever rather than a real product limit.
  var _PLUGIN_SETUP_MAX_APPS = 64;

  async function start(pluginSnapshot, appId, controls) {
    if (
      !controls || typeof controls !== 'object' ||
      typeof controls.pluginId !== 'string' || !controls.pluginId ||
      !controls.api || typeof controls.api.startConnection !== 'function' ||
      typeof controls.api.status !== 'function'
    ) throw _pluginSetupError('plugin_connection_state_invalid');
    var snapshot = _pluginSetupSnapshot(pluginSnapshot);
    var currentAppId = appId == null ? null : appId;
    // A spent popup must not be reused for another app, but its handle
    // still belongs to the completion UI so device success can close it.
    var completionPopup = controls.popup || null;
    var guard = 0;
    var connected = false;
    while (!connected) {
      guard += 1;
      if (guard > _PLUGIN_SETUP_MAX_APPS) throw _pluginSetupError('plugin_connection_state_invalid');
      var handler = SETUP_ACTIONS[snapshot.setup_action];
      if (typeof handler !== 'function') throw _pluginSetupError('plugin_connection_state_invalid');
      snapshot = _pluginSetupSnapshot(await handler({
        pluginId: controls.pluginId,
        appId: currentAppId,
        snapshot: snapshot,
        controls: controls,
      }));
      // The server's lifecycle revision changes after every completed app
      // (PluginLifecycleService.start_connection checks the caller's held
      // revision against the current one before each app and rejects a
      // stale one with plugin_permission_changed -- mcp_platform/
      // plugin_lifecycle.py's `_lifecycle_components()` folds in
      // grants/authority/vault/permissions, all of which a completed
      // connection just changed). A composite plugin's second and later
      // required app must be started with the *fresh* revision this step's
      // own snapshot just reported, never the revision this call began
      // with -- that app is started by the caller's next consent round,
      // which takes the revision from the snapshot returned (and reported
      // through onSnapshot) here; controls.revision is refreshed to match.
      if (typeof snapshot.revision === 'string' && snapshot.revision) {
        controls.revision = snapshot.revision;
      }
      connected = snapshot.state === 'connected';
      if (typeof controls.onSnapshot === 'function') controls.onSnapshot(snapshot, connected);
      if (!connected) {
        if (!snapshot.next_required_app_id) break;
        // Each app owns its own provider scopes and capabilities. Return to
        // the panel for a fresh, exact selection instead of starting the
        // next app under the previous app's consent.
        break;
      }
    }
    if (typeof controls.finish === 'function') {
      controls.finish(completionPopup, connected);
    }
    return snapshot;
  }

  window.OmegaPluginSetup = Object.freeze({start: start, SETUP_ACTIONS: SETUP_ACTIONS});
})();

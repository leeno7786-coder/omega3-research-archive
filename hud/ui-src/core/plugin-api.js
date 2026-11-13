// Provider-neutral plugin API client. Every method calls exactly the
// generic /api/plugins/* route family the HUD server exposes for the
// plugin lifecycle: no /featured route, no provider ID table, no provider
// substring, and no hardcoded OAuth flow. Callers receive the parsed
// server JSON body on success, or a thrown Error carrying the server's
// stable code as `error.code` on failure -- a malformed or missing
// response is never silently converted to `{}`.
(function () {
  'use strict';

  function encodeQuery(params) {
    if (params == null || typeof params !== 'object') return '';
    return Object.keys(params)
      .filter(key => params[key] !== undefined && params[key] !== null)
      .map(key => encodeURIComponent(key) + '=' + encodeURIComponent(String(params[key])))
      .join('&');
  }

  function pluginPath(pluginId, suffix) {
    return '/api/plugins/' + encodeURIComponent(pluginId) + suffix;
  }

  function mutation(body, method) {
    if (body === null || typeof body !== 'object' || Array.isArray(body)) {
      throw new Error('plugin_api_mutation_body_invalid');
    }
    return {
      method: method || 'POST',
      headers: {'content-type': 'application/json'},
      body: JSON.stringify(body),
    };
  }

  function failure(code) {
    const error = new Error(code);
    error.code = code;
    return error;
  }

  async function requestJson(url, options) {
    let response;
    try {
      response = await fetch(url, Object.assign({cache: 'no-store'}, options || {}));
    } catch (_networkError) {
      throw failure('plugin_api_network_unavailable');
    }
    let body;
    try {
      body = await response.json();
    } catch (_parseError) {
      throw failure('plugin_api_response_invalid');
    }
    if (body === null || typeof body !== 'object' || Array.isArray(body)) {
      throw failure('plugin_api_response_invalid');
    }
    if (typeof body.ok !== 'boolean') {
      throw failure('plugin_api_response_invalid');
    }
    if (!body.ok) {
      const code = typeof body.error === 'string' && body.error
        ? body.error
        : 'plugin_api_response_invalid';
      throw failure(code);
    }
    return body;
  }

  function permissionsGetPath(pluginId, appId) {
    const base = pluginPath(pluginId, '/permissions');
    return appId == null ? base : base + '?app_id=' + encodeURIComponent(appId);
  }

  window.OmegaPluginApi = Object.freeze({
    // The shared fetch primitive itself (follow-up refactor wave W3):
    // network/JSON/`{ok:boolean}` handling in exactly one place. Every
    // method below is a thin wrapper around this; core/plugin-composer.js's
    // own two catalog/installed reads route through it directly too,
    // rather than carrying a second, private fetch-and-validate copy.
    requestJson: requestJson,
    catalog: params => requestJson('/api/plugins/catalog?' + encodeQuery(params)),
    details: pluginId => requestJson('/api/plugins/catalog/' + encodeURIComponent(pluginId)),
    // GET .../update-review?version=&revision= (task-4 fix round 1 finding 2):
    // the complete server-computed identity/capability/permission diff
    // between the installed version and one exact candidate version.
    // Validation-only; `revision` is required and checked the same way
    // review/install/update already are.
    updateReview: (pluginId, version, revision) => requestJson(
      pluginPath(pluginId, '/update-review') + '?' + encodeQuery({version, revision})
    ),
    installed: () => requestJson('/api/plugins/installed'),
    sync: revision => requestJson('/api/plugins/catalog/sync', mutation({revision})),
    updateStatus: () => requestJson('/api/plugins/updates'),
    checkUpdates: () => requestJson('/api/plugins/updates/check', mutation({})),
    // hud/server.py's /review route now requires and checks the client's
    // held lifecycle revision (task-4 controller ruling 2): a stale
    // revision returns the stable plugin_revision_conflict cause rather
    // than proceeding. See task-1-report.md for why every mutating method
    // keeps `revision` in its own signature even on the six routes that
    // still never place it on the wire.
    review: (pluginId, revision) => requestJson(pluginPath(pluginId, '/review'), mutation({revision})),
    // /install accepts exactly {"confirmation", "revision"}
    // (_strict_plugin_install_json) -- both required (task-4 controller
    // ruling 2).
    install: (pluginId, revision, confirmation) => requestJson(
      pluginPath(pluginId, '/install'), mutation({confirmation, revision})
    ),
    // GET /api/plugins/{plugin_id}/connection returns the redacted connection
    // standing and setup action from the live server route.
    status: pluginId => requestJson(pluginPath(pluginId, '/connection')),
    startConnection: (pluginId, revision, appId, settings) => requestJson(
      pluginPath(pluginId, '/connection/start'), mutation({revision, app_id: appId, settings})
    ),
    // /connection/poll accepts exactly {"request_id"} (_strict_device_poll_json);
    // `revision` is unused on the wire.
    pollConnection: (pluginId, revision, requestId) => requestJson(
      pluginPath(pluginId, '/connection/poll'), mutation({request_id: requestId})
    ),
    // app_id is an optional exact-match extension beyond the brief's one-arg
    // sketch: multi-app plugins need it to read one app's authority, exactly
    // as the pre-refactor call site already required -- see task-1-report.md.
    permissions: (pluginId, appId) => requestJson(permissionsGetPath(pluginId, appId)),
    savePermissions: (pluginId, revision, settings, appId) => requestJson(
      pluginPath(pluginId, '/permissions'),
      mutation(
        appId == null ? {revision, settings} : {revision, settings, app_id: appId},
        'PUT',
      )
    ),
    tryInChat: pluginId => requestJson(pluginPath(pluginId, '/try-in-chat'), mutation({})),
    // /disconnect now requires and checks the client's held lifecycle
    // revision (task-6 controller ruling 2, mirroring /review's own
    // {"revision"}-only body): a stale revision returns the stable
    // plugin_revision_conflict cause rather than proceeding.
    disconnect: (pluginId, revision) => requestJson(pluginPath(pluginId, '/disconnect'), mutation({revision})),
    // /update accepts exactly {"confirmation","version","revision"}
    // (_strict_plugin_update_json) -- all three required (task-4 controller
    // ruling 2).
    update: (pluginId, revision, version, confirmation) => requestJson(
      pluginPath(pluginId, '/update'), mutation({version, confirmation, revision})
    ),
    // /uninstall now requires and checks the client's held lifecycle
    // revision (task-6 controller ruling 2), same shape as /disconnect above.
    uninstall: (pluginId, revision) => requestJson(pluginPath(pluginId, '/uninstall'), mutation({revision})),
  });
})();

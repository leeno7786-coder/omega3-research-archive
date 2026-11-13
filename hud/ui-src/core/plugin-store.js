// Canonical, provider-neutral plugin marketplace state reducer. This is the
// ONLY place marketplace UI state lives: every panel that renders the Public
// or Personal catalog, an installed plugin card, or a mutation in flight
// reads it from `window.OmegaPluginStore.create(initial)` -- never from ad
// hoc panel-local variables. The reducer owns no server business logic: it
// only stores, isolates, and de-duplicates exactly what the server contract
// already sent through `window.OmegaPluginApi` (see core/plugin-api.js).
//
// Lifecycle state names are mirrored VERBATIM from the server's own closed
// enum, `PluginLifecycleState` in mcp_platform/plugin_state.py. That module
// is the single source of truth; this list must never add, remove, or
// rename a member independently of it.
(function () {
  'use strict';

  var LIFECYCLE_STATES = Object.freeze([
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
  var LIFECYCLE_STATE_SET = new Set(LIFECYCLE_STATES);

  // The closed event set this reducer accepts. Every panel-facing action
  // (task 3 onward) must be expressed as one of these -- dispatching
  // anything else throws `plugin_store_event_unknown`.
  var EVENT_TYPES = new Set([
    'catalogLoading',
    'catalogLoaded',
    'catalogFailed',
    'installedLoaded',
    'tabChanged',
    'queryChanged',
    'filtersChanged',
    'detailsOpened',
    'detailsClosed',
    'mutationStarted',
    'pluginChanged',
    'mutationFailed',
  ]);

  // The Public/Personal split is the marketplace's only tab concept (see
  // "Public and Personal tabs" in the marketplace design doc). It is a HUD
  // presentation split, not a server lifecycle state, so an invalid tab
  // throws the generic client-side event error, never `plugin_state_invalid`.
  var TABS = new Set(['public', 'personal']);

  function fail(code) {
    var error = new Error(code);
    error.code = code;
    throw error;
  }

  function isPlainObject(value) {
    return value !== null && typeof value === 'object' && !Array.isArray(value);
  }

  function isNonEmptyString(value) {
    return typeof value === 'string' && value.length > 0;
  }

  function deepClone(value) {
    return JSON.parse(JSON.stringify(value));
  }

  function deepFreeze(value) {
    if (Array.isArray(value)) {
      value.forEach(deepFreeze);
      return Object.freeze(value);
    }
    if (isPlainObject(value)) {
      Object.keys(value).forEach(function (key) {
        deepFreeze(value[key]);
      });
      return Object.freeze(value);
    }
    return value;
  }

  // A plugin object always comes straight from a parsed server response
  // (PluginLifecycleSnapshot.to_dict() -- plugin_id, state, registry_revision,
  // package_version, apps, next_required_app_id, enabled, error_code). The
  // reducer's own contract check is narrow on purpose: it verifies exactly
  // the two fields it depends on (`plugin_id` for map identity, `state`
  // against the closed lifecycle set) and otherwise trusts the server, which
  // already validated the full shape before ever sending it.
  function clonePlugin(plugin) {
    if (!isPlainObject(plugin) || !isNonEmptyString(plugin.plugin_id)) {
      fail('plugin_store_event_invalid');
    }
    if (!LIFECYCLE_STATE_SET.has(plugin.state)) {
      fail('plugin_state_invalid');
    }
    return deepClone(plugin);
  }

  function emptyCatalog() {
    return {items: [], cursor: null, revision: '', request: -1, loading: false, error: null};
  }

  function cloneCatalog(catalog) {
    return {
      items: catalog.items.slice(),
      cursor: catalog.cursor,
      revision: catalog.revision,
      request: catalog.request,
      loading: catalog.loading,
      error: catalog.error,
    };
  }

  function cloneCatalogs(catalogs) {
    return {public: cloneCatalog(catalogs.public), personal: cloneCatalog(catalogs.personal)};
  }

  function initialState(initial) {
    var state = {
      tab: 'public',
      query: '',
      filters: {},
      detailsId: null,
      installed: {},
      pendingMutations: {},
      mutationErrors: {},
      catalogs: {public: emptyCatalog(), personal: emptyCatalog()},
      // Per-plugin ordering guard bookkeeping (task-6 controller ruling 1,
      // fix round 1): the highest `request` id accepted for each plugin so
      // far, keyed `{[plugin_id]: number}`. Ordering is never derived from
      // `registry_revision` -- that field is a content digest of the whole
      // registry document (plugin_platform/registry.py's `_revision_for`)
      // and legitimately repeats whenever the registry returns to
      // byte-identical content (e.g. connect -> disconnect -> reconnect
      // toggling only `enabled`), so a content-keyed seen-set would reject
      // a correct in-order recurrence. `request` is instead a caller-
      // assigned monotonic id, the same pattern `catalog.request` already
      // uses above. Internal only -- never projected into snapshot() (see
      // projectSnapshot below), since it is pure reducer bookkeeping, not
      // consumer-facing data.
      installedRequest: {},
    };
    if (initial === undefined || initial === null) return state;
    if (!isPlainObject(initial)) fail('plugin_store_event_invalid');
    if (initial.tab !== undefined) {
      if (!TABS.has(initial.tab)) fail('plugin_store_event_invalid');
      state.tab = initial.tab;
    }
    if (initial.query !== undefined) {
      if (typeof initial.query !== 'string') fail('plugin_store_event_invalid');
      state.query = initial.query;
    }
    if (initial.filters !== undefined) {
      if (!isPlainObject(initial.filters)) fail('plugin_store_event_invalid');
      state.filters = deepClone(initial.filters);
    }
    return state;
  }

  // Every catalog event may name its tab explicitly; when it doesn't, it
  // applies to whichever tab is currently active. Public and Personal each
  // carry their own `items`/`cursor`/`revision`/`request` bucket, so loading
  // one can never touch, reset, or dedupe against the other.
  function resolveTab(event, state) {
    if (event.tab === undefined) return state.tab;
    if (!TABS.has(event.tab)) fail('plugin_store_event_invalid');
    return event.tab;
  }

  // Shared guard for the three catalog request events: `request` must be a
  // monotonically increasing id (per tab) supplied by the caller (task 3's
  // fetch glue). A response/notification whose id is not newer than the tab's
  // already-accepted id is a stale straggler and is dropped whole -- this is
  // what makes a late response unable to overwrite newer state, independent
  // of network arrival order.
  function reduceCatalogRequest(state, event, apply) {
    var tab = resolveTab(event, state);
    if (typeof event.request !== 'number' || !Number.isFinite(event.request)) {
      fail('plugin_store_event_invalid');
    }
    var catalog = state.catalogs[tab];
    if (event.request < catalog.request) {
      return state;
    }
    var nextCatalog = apply(cloneCatalog(catalog), event);
    nextCatalog.request = event.request;
    var catalogs = cloneCatalogs(state.catalogs);
    catalogs[tab] = nextCatalog;
    return Object.assign({}, state, {catalogs: catalogs});
  }

  function resetActiveCatalog(state) {
    var catalogs = cloneCatalogs(state.catalogs);
    catalogs[state.tab] = emptyCatalog();
    return catalogs;
  }

  var REDUCERS = {
    catalogLoading: function (state, event) {
      return reduceCatalogRequest(state, event, function (catalog) {
        catalog.loading = true;
        catalog.error = null;
        return catalog;
      });
    },
    catalogFailed: function (state, event) {
      if (!isNonEmptyString(event.error)) fail('plugin_store_event_invalid');
      return reduceCatalogRequest(state, event, function (catalog, ev) {
        catalog.loading = false;
        catalog.error = ev.error;
        return catalog;
      });
    },
    // `cursor` mirrors the server's `next_cursor` field verbatim (the caller
    // maps that name across); `append: true` means "this page extends the
    // current list" (cursor pagination) rather than replacing it (a fresh
    // query/tab/filter load).
    catalogLoaded: function (state, event) {
      if (!Array.isArray(event.items)) fail('plugin_store_event_invalid');
      if (typeof event.revision !== 'string') fail('plugin_store_event_invalid');
      event.items.forEach(function (item) {
        if (!isPlainObject(item) || !isNonEmptyString(item.plugin_id)) {
          fail('plugin_store_event_invalid');
        }
      });
      return reduceCatalogRequest(state, event, function (catalog, ev) {
        catalog.loading = false;
        catalog.error = null;
        catalog.revision = ev.revision;
        catalog.cursor = ev.cursor === undefined ? null : ev.cursor;
        var incoming = deepClone(ev.items);
        if (ev.append) {
          var seen = {};
          catalog.items.forEach(function (item) {
            seen[item.plugin_id] = true;
          });
          incoming.forEach(function (item) {
            if (!seen[item.plugin_id]) {
              seen[item.plugin_id] = true;
              catalog.items.push(item);
            }
          });
        } else {
          catalog.items = incoming;
        }
        return catalog;
      });
    },
    installedLoaded: function (state, event) {
      if (!Array.isArray(event.items)) fail('plugin_store_event_invalid');
      var installed = {};
      event.items.forEach(function (item) {
        var cloned = clonePlugin(item);
        installed[cloned.plugin_id] = cloned;
      });
      // Prune the ordering watermark for any plugin this authoritative
      // /installed response no longer names (final fix wave W3):
      // installedRequest otherwise grows without bound and a stale entry
      // would wrongly reject that plugin's next legitimate, independently-
      // numbered request sequence (e.g. after an uninstall + reinstall) as
      // an out-of-order straggler.
      var installedRequest = state.installedRequest;
      var pruned = null;
      Object.keys(installedRequest).forEach(function (pluginId) {
        if (!Object.prototype.hasOwnProperty.call(installed, pluginId)) {
          if (pruned === null) pruned = Object.assign({}, installedRequest);
          delete pruned[pluginId];
        }
      });
      return Object.assign({}, state, {
        installed: installed,
        installedRequest: pruned === null ? installedRequest : pruned,
      });
    },
    tabChanged: function (state, event) {
      if (!TABS.has(event.tab)) fail('plugin_store_event_invalid');
      if (event.tab === state.tab) return state;
      return Object.assign({}, state, {tab: event.tab});
    },
    // A new search query invalidates the active tab's loaded pages: any
    // items/cursor already held belonged to the previous query and cannot be
    // appended to safely, so this always resets (never merges).
    queryChanged: function (state, event) {
      if (typeof event.query !== 'string') fail('plugin_store_event_invalid');
      return Object.assign({}, state, {query: event.query, catalogs: resetActiveCatalog(state)});
    },
    filtersChanged: function (state, event) {
      if (!isPlainObject(event.filters)) fail('plugin_store_event_invalid');
      return Object.assign({}, state, {
        filters: deepClone(event.filters),
        catalogs: resetActiveCatalog(state),
      });
    },
    detailsOpened: function (state, event) {
      if (!isNonEmptyString(event.pluginId)) fail('plugin_store_event_invalid');
      return Object.assign({}, state, {detailsId: event.pluginId});
    },
    detailsClosed: function (state) {
      if (state.detailsId === null) return state;
      return Object.assign({}, state, {detailsId: null});
    },
    // Per-plugin mutation locking: only one mutation may be in flight for a
    // given plugin at a time. A second `mutationStarted` for an already
    // locked plugin is a no-op -- the original mutation keeps the lock --
    // while a different plugin locks independently.
    mutationStarted: function (state, event) {
      if (!isNonEmptyString(event.pluginId) || !isNonEmptyString(event.mutationId)) {
        fail('plugin_store_event_invalid');
      }
      if (state.pendingMutations[event.pluginId]) {
        return state;
      }
      var pending = Object.assign({}, state.pendingMutations);
      pending[event.pluginId] = event.mutationId;
      var errors = Object.assign({}, state.mutationErrors);
      delete errors[event.pluginId];
      return Object.assign({}, state, {pendingMutations: pending, mutationErrors: errors});
    },
    // The authoritative outcome of any mutation (install, connect, review,
    // permission save, disconnect, ...) always arrives as a fresh, fully
    // resolved snapshot for that plugin. Applying it here is a full replace
    // -- there is no "sticky" state: a plugin that was `connected` moves to
    // whatever the incoming snapshot says, including `permission_changed`,
    // and it clears that plugin's mutation lock and any prior error.
    //
    // Ordering guard (task-6 controller ruling 1, fix round 1): a pushed or
    // polled event can arrive out of network order. Ordering is never
    // derived from a content digest -- `registry_revision` is a SHA-256 of
    // the whole installed-plugin registry document
    // (plugin_platform/registry.py's `_revision_for`) and legitimately
    // repeats whenever the registry returns to byte-identical content (a
    // connect -> disconnect -> reconnect cycle toggling only `enabled` can
    // reproduce an earlier digest exactly), so a content-keyed guard would
    // silently drop a correct in-order recurrence. Instead, exactly like
    // the catalog's own `request` id above: the caller assigns a per-plugin
    // monotonic `request` at dispatch time (settings-plugins.js's
    // `startConnection`, the one real dispatch site), and an incoming
    // event whose `request` is older than the latest one already accepted
    // for that plugin is dropped as a whole no-op -- an identical revision
    // arriving with an in-order (equal-or-newer) `request` is accepted.
    // `request` is optional: an event that omits it (every pre-existing
    // caller and test) is never ordering-guarded, preserving last-write-
    // wins for callers that don't yet track a request id. `event.force:
    // true` bypasses the guard's *rejection* for that one event, but never
    // regresses the tracked watermark (see `Math.max` below), so a later
    // normal event still can't be fooled into accepting an even-older one.
    pluginChanged: function (state, event) {
      var cloned = clonePlugin(event.plugin);
      var pluginId = cloned.plugin_id;
      var hasRequest = event.request !== undefined;
      if (hasRequest && (typeof event.request !== 'number' || !Number.isFinite(event.request))) {
        fail('plugin_store_event_invalid');
      }
      var lastRequest = Object.prototype.hasOwnProperty.call(state.installedRequest, pluginId)
        ? state.installedRequest[pluginId]
        : -1;
      if (event.force !== true && hasRequest && event.request < lastRequest) {
        return state;
      }
      var installed = Object.assign({}, state.installed);
      installed[pluginId] = cloned;
      var pending = Object.assign({}, state.pendingMutations);
      delete pending[pluginId];
      var errors = Object.assign({}, state.mutationErrors);
      delete errors[pluginId];
      var nextState = Object.assign({}, state, {
        installed: installed,
        pendingMutations: pending,
        mutationErrors: errors,
      });
      // 'available' means "not installed" (see the client's own installed/
      // catalog merge, which forces this exact value for any plugin the
      // /installed response no longer names) -- a pluginChanged reporting
      // it directly (e.g. an uninstall's own final snapshot) drops that
      // plugin's watermark too (final fix wave W3), for the same reason
      // installedLoaded does above: a stale entry would otherwise reject a
      // future reinstall's own fresh, independently-numbered request
      // sequence as an out-of-order straggler.
      if (cloned.state === 'available') {
        if (Object.prototype.hasOwnProperty.call(state.installedRequest, pluginId)) {
          var pruned = Object.assign({}, state.installedRequest);
          delete pruned[pluginId];
          nextState.installedRequest = pruned;
        }
      } else if (hasRequest) {
        var installedRequest = Object.assign({}, state.installedRequest);
        installedRequest[pluginId] = Math.max(lastRequest, event.request);
        nextState.installedRequest = installedRequest;
      }
      return nextState;
    },
    mutationFailed: function (state, event) {
      if (!isNonEmptyString(event.pluginId) || !isNonEmptyString(event.error)) {
        fail('plugin_store_event_invalid');
      }
      var pending = Object.assign({}, state.pendingMutations);
      delete pending[event.pluginId];
      var errors = Object.assign({}, state.mutationErrors);
      errors[event.pluginId] = event.error;
      return Object.assign({}, state, {pendingMutations: pending, mutationErrors: errors});
    },
  };

  function projectSnapshot(state) {
    var view = {
      tab: state.tab,
      query: state.query,
      filters: state.filters,
      detailsId: state.detailsId,
      installed: state.installed,
      pendingMutations: state.pendingMutations,
      mutationErrors: state.mutationErrors,
      catalogs: state.catalogs,
      catalog: state.catalogs[state.tab],
    };
    return deepFreeze(deepClone(view));
  }

  function create(initial) {
    var state = initialState(initial);
    var listeners = [];
    var currentSnapshot = projectSnapshot(state);

    function dispatch(event) {
      if (!isPlainObject(event) || typeof event.type !== 'string') {
        fail('plugin_store_event_invalid');
      }
      if (!EVENT_TYPES.has(event.type)) {
        fail('plugin_store_event_unknown');
      }
      var next = REDUCERS[event.type](state, event);
      if (next === state) {
        return currentSnapshot;
      }
      state = next;
      currentSnapshot = projectSnapshot(state);
      listeners.forEach(function (listener) {
        listener(currentSnapshot);
      });
      return currentSnapshot;
    }

    function snapshot() {
      return currentSnapshot;
    }

    function subscribe(listener) {
      if (typeof listener !== 'function') fail('plugin_store_event_invalid');
      listeners.push(listener);
      return function unsubscribe() {
        var index = listeners.indexOf(listener);
        if (index !== -1) listeners.splice(index, 1);
      };
    }

    return Object.freeze({dispatch: dispatch, snapshot: snapshot, subscribe: subscribe});
  }

  window.OmegaPluginStore = Object.freeze({
    create: create,
    EVENT_TYPES: EVENT_TYPES,
    LIFECYCLE_STATES: LIFECYCLE_STATES,
  });
})();

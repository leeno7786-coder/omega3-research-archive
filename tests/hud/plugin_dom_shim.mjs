// Shared fake-DOM primitives for the plugin marketplace .mjs harnesses
// (follow-up refactor wave W2). No jsdom dependency exists in this repo, so
// several harnesses each carried their own copy of this same small but real
// DOM (HTML parser, querySelector[All]/closest/classList/dataset, event
// delegation, and a focus/activeElement tracker) to run the panel's actual
// source inside a `vm` context. This file is the one shared copy; it must
// NOT match the `test_plugin_marketplace_*` glob the pytest wrapper
// (tests/hud/test_plugin_marketplace_ui.py) uses to enumerate harnesses.
//
// The copies had drifted (see the follow-up refactor report for the exact
// diffs found): this module keeps the superset of every behavior any
// caller relied on --
//   - Element#fireKeydown (bubble-phase keydown dispatch with
//     stopPropagation/stopImmediatePropagation support) --
//     test_plugin_marketplace_review.mjs's own addition, needed for the
//     drawer's Escape-handling tests.
//   - Element#className getter/setter, #append, #replaceChildren, and
//     #insertBefore -- test_plugin_marketplace_handoff.mjs's own additions.
//   - Element#dispatchSubmit (async, awaits every registered submit
//     listener) -- test_plugin_permissions.mjs's own addition (there named
//     DomElement), needed for its permissions-editor submit-handler tests.
//   - matchesSelector's compound class-selector support (`.a.b`, not just a
//     single class) -- test_plugin_marketplace_review.mjs/
//     test_plugin_marketplace_handoff.mjs/test_plugin_permissions.mjs's own
//     version; test_plugin_marketplace_panel.mjs's copy only matched a
//     single class, a strict subset of this behavior.
//   - okCatalogPage/okInstalled accept an optional `revision` override
//     (defaulting to the same 'a'.repeat(64) every copy used) --
//     test_plugin_marketplace_review.mjs/test_plugin_marketplace_handoff.mjs's
//     own addition; test_plugin_marketplace_panel.mjs's copy hardcoded the
//     revision inline, which is behaviorally identical for every call site
//     that never passed one.
// None of this changes any existing assertion: every addition is purely
// additive (a method/parameter no prior caller used), and the merged
// matchesSelector/okCatalogPage/okInstalled behavior is unchanged for every
// call shape the drifted copies already agreed on.
//
// test_plugin_marketplace_setup.mjs's own `FakeElement`/`fakeDom` are a
// deliberately smaller, different-shaped shim (sufficient only for the two
// pure secret-field helpers it exercises -- single-listener addEventListener,
// selector support limited to `[attr]`/`[attr="value"]`, no classList/
// dataset) -- kept here as its own separate export rather than folded into
// the bigger `Element`, since it is not a drifted copy of the same thing.

export const VOID = new Set(['img', 'input', 'br', 'hr']);

// Real focus()/activeElement tracking (task-8: keyboard reachability and
// focus-restoration assertions need a genuine focus target, not just a
// markup attribute). One instance per importing process -- each harness is
// invoked as its own `node tests/hud/test_x.mjs` process, so this module's
// single top-level instance is never shared across harness files.
export const focusTracker = { current: null };

export function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[ch]));
}

export class ClassList {
  constructor(el) { this.el = el; }
  _set() { return new Set((this.el.getAttribute('class') || '').split(/\s+/).filter(Boolean)); }
  _write(set) { this.el.setAttribute('class', Array.from(set).join(' ')); }
  add(c) { const s = this._set(); s.add(c); this._write(s); }
  remove(c) { const s = this._set(); s.delete(c); this._write(s); }
  toggle(c, force) {
    const s = this._set();
    const on = force === undefined ? !s.has(c) : force;
    if (on) s.add(c); else s.delete(c);
    this._write(s);
    return on;
  }
  contains(c) { return this._set().has(c); }
}

export function datasetProxy(el) {
  return new Proxy({}, {
    get(_t, prop) {
      const attr = 'data-' + String(prop).replace(/[A-Z]/g, m => '-' + m.toLowerCase());
      return el.getAttribute(attr);
    },
    set(_t, prop, value) {
      const attr = 'data-' + String(prop).replace(/[A-Z]/g, m => '-' + m.toLowerCase());
      el.setAttribute(attr, value);
      return true;
    },
  });
}

export class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.attrs = new Map();
    this.children = [];
    this.parent = null;
    this._text = null;
    this.listeners = new Map();
    this.classList = new ClassList(this);
    this.dataset = datasetProxy(this);
    this._hidden = false;
    this._disabled = false;
    this._value = '';
  }
  get hidden() { return this.attrs.has('hidden') || this._hidden; }
  set hidden(v) { this._hidden = Boolean(v); if (v) this.attrs.set('hidden', ''); else this.attrs.delete('hidden'); }
  get disabled() { return this.attrs.has('disabled') || this._disabled; }
  set disabled(v) { this._disabled = Boolean(v); if (v) this.attrs.set('disabled', ''); else this.attrs.delete('disabled'); }
  get value() { return this._value; }
  set value(v) { this._value = v; }
  get className() { return this.getAttribute('class') || ''; }
  set className(v) { this.setAttribute('class', v); }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  removeAttribute(name) { this.attrs.delete(name); }
  hasAttribute(name) { return this.attrs.has(name); }
  get textContent() {
    if (this._text !== null) return this._text;
    return this.children.map(c => (c.nodeType === 'text' ? c.data : c.textContent)).join('');
  }
  set textContent(v) { this._text = String(v); this.children = []; }
  set innerHTML(html) { this._text = null; this.children = parseHtml(html, this); }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  // Needed by the chat feed's FIFO cap (2026-09-20), which evicts the oldest entry.
  removeChild(child) {
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    if (child && child.parent === this) child.parent = null;
    return child;
  }
  append(...nodes) { nodes.forEach(node => this.appendChild(node)); }
  replaceChildren(...nodes) {
    this._text = null;
    this.children = [];
    nodes.forEach(node => this.appendChild(node));
  }
  insertBefore(newNode, referenceNode) {
    newNode.parent = this;
    const idx = this.children.indexOf(referenceNode);
    if (idx === -1) this.children.push(newNode);
    else this.children.splice(idx, 0, newNode);
    return newNode;
  }
  addEventListener(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  fire(type) {
    (this.listeners.get(type) || []).forEach(fn => fn({ target: this }));
  }
  // Real bubble-phase dispatch (target outward through .parent), so a
  // scoped listener's stopPropagation()/stopImmediatePropagation() can be
  // exercised against a listener registered on an ancestor (e.g. the
  // document-level Escape handler standing in for register.js's global
  // `window.addEventListener('keydown', ...)` -- both are bubble-phase, so
  // stopping propagation at the dialog is sufficient in real DOM terms too).
  fireKeydown(key) {
    let prevented = false;
    let propagationStopped = false;
    let node = this;
    while (node && !propagationStopped) {
      let immediateStopped = false;
      const listeners = (node.listeners.get('keydown') || []).slice();
      for (const fn of listeners) {
        if (immediateStopped) break;
        fn({
          key,
          target: this,
          currentTarget: node,
          preventDefault() { prevented = true; },
          stopPropagation() { propagationStopped = true; },
          stopImmediatePropagation() { propagationStopped = true; immediateStopped = true; },
        });
      }
      node = node.parent;
    }
    return prevented;
  }
  dispatchClick() {
    let node = this;
    const fakeEvent = { target: this, closest: (sel) => this.closest(sel), preventDefault() {}, stopPropagation() {} };
    while (node) {
      (node.listeners.get('click') || []).forEach(fn => fn(fakeEvent));
      node = node.parent;
    }
  }
  // Async: awaits every registered submit listener (test_plugin_permissions.mjs's
  // own addition, needed since the permissions-editor's submit handler is
  // itself async).
  dispatchSubmit() {
    const fakeEvent = { target: this, closest: (sel) => this.closest(sel), preventDefault() {}, stopPropagation() {} };
    return Promise.all((this.listeners.get('submit') || []).map(fn => fn(fakeEvent)));
  }
  matches(selector) { return matchesSelector(this, selector); }
  closest(selector) {
    let node = this;
    while (node) {
      if (node.matches && node.matches(selector)) return node;
      node = node.parent;
    }
    return null;
  }
  querySelectorAll(selector) {
    const out = [];
    const walk = (node) => {
      node.children.forEach(child => {
        if (child.nodeType !== 'text') {
          if (matchesSelector(child, selector)) out.push(child);
          walk(child);
        }
      });
    };
    walk(this);
    return out;
  }
  querySelector(selector) {
    const all = this.querySelectorAll(selector);
    return all.length ? all[0] : null;
  }
  focus() { focusTracker.current = this; }
  blur() { if (focusTracker.current === this) focusTracker.current = null; }
}

export function matchesSelector(el, selector) {
  selector = selector.trim();
  const attrMatch = selector.match(/^\[([a-zA-Z0-9_-]+)(?:="([^"]*)")?\]$/);
  if (attrMatch) {
    const [, name, value] = attrMatch;
    if (!el.hasAttribute(name)) return false;
    if (value === undefined) return true;
    return el.getAttribute(name) === value;
  }
  if (selector.startsWith('.')) {
    return selector.slice(1).split('.').every(cls => el.classList.contains(cls));
  }
  if (selector.startsWith('#')) return el.getAttribute('id') === selector.slice(1);
  return el.tagName === selector;
}

export function decodeEntities(s) {
  return s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"').replace(/&#39;/g, "'");
}

export function parseHtml(html, parent) {
  const tagRe = /<(\/?)([a-zA-Z][a-zA-Z0-9-]*)((?:\s+[a-zA-Z0-9_-]+(?:="[^"]*"|='[^']*')?)*)\s*(\/?)>/g;
  const nodes = [];
  const stack = [{ el: parent, children: nodes }];
  let lastIndex = 0;
  let match;
  while ((match = tagRe.exec(html)) !== null) {
    const [full, closing, tag, attrString, selfClose] = match;
    const textBefore = html.slice(lastIndex, match.index);
    if (textBefore.length) stack[stack.length - 1].children.push({ nodeType: 'text', data: decodeEntities(textBefore) });
    lastIndex = match.index + full.length;
    if (closing) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const el = new Element(tag);
    const attrRe = /([a-zA-Z0-9_-]+)(?:=(?:"([^"]*)"|'([^']*)'))?/g;
    let am;
    while ((am = attrRe.exec(attrString)) !== null) {
      const name = am[1];
      const value = am[2] !== undefined ? am[2] : (am[3] !== undefined ? am[3] : '');
      el.setAttribute(name, decodeEntities(value));
    }
    el.parent = stack[stack.length - 1].el;
    stack[stack.length - 1].children.push(el);
    if (!VOID.has(tag) && !selfClose) stack.push({ el, children: el.children });
  }
  const tail = html.slice(lastIndex);
  if (tail.length) stack[stack.length - 1].children.push({ nodeType: 'text', data: decodeEntities(tail) });
  return nodes;
}

// ---------------------------------------------------------------------------
// Fixture builders -- the server response shapes every harness fakes.
// ---------------------------------------------------------------------------
export function okCatalogPage({ plugins = [], next_cursor = null, total_plugins, status = 'ok', revision = 'a'.repeat(64) } = {}) {
  return { ok: true, revision, status, plugins, next_cursor, total_plugins: total_plugins ?? plugins.length };
}

export function okInstalled(plugins = [], revision = 'a'.repeat(64)) {
  return { ok: true, revision, plugins };
}

// ---------------------------------------------------------------------------
// Fake timers -- makes the 180ms search debounce deterministic and instant.
// ---------------------------------------------------------------------------
export function makeFakeTimers() {
  let nextId = 0;
  const pending = new Map();
  return {
    setTimeout(fn, ms) {
      const id = ++nextId;
      pending.set(id, { fn, ms });
      return id;
    },
    clearTimeout(id) { pending.delete(id); },
    pendingCount() { return pending.size; },
    flush() {
      const entries = Array.from(pending.values());
      pending.clear();
      entries.forEach(entry => entry.fn());
    },
  };
}

export function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

// ---------------------------------------------------------------------------
// test_plugin_marketplace_setup.mjs's own smaller shim -- sufficient only
// for the two pure, module-scope secret-field helpers
// (_pluginCollectSecretFieldsUi/_pluginClearSecretFieldsUi in
// settings-plugins.js), not the full render harness the bigger `Element`
// above serves. Kept as its own separate, deliberately minimal shape (see
// the file-header note above) rather than folded into `Element`.
// ---------------------------------------------------------------------------
export class FakeElement {
  constructor(tagName) {
    this.tagName = tagName;
    this.attrs = new Map();
    this.children = [];
    this.parent = null;
    this.listeners = new Map();
    this._hidden = false;
    this._value = '';
    this._text = null;
  }
  get hidden() { return this._hidden; }
  set hidden(v) { this._hidden = Boolean(v); }
  get value() { return this._value; }
  set value(v) { this._value = v; }
  get textContent() {
    if (this._text !== null) return this._text;
    return this.children.map(c => c.textContent || '').join('');
  }
  set textContent(v) { this._text = v; this.children = []; }
  set className(v) { this.setAttribute('class', v); }
  get className() { return this.attrs.get('class') || ''; }
  set innerHTML(v) {
    if (v !== '') throw new Error('FakeElement only supports innerHTML = \'\' (clear)');
    this.children.forEach(child => { child.parent = null; });
    this.children = [];
  }
  setAttribute(name, value) { this.attrs.set(name, String(value)); }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  remove() {
    if (!this.parent) return;
    const i = this.parent.children.indexOf(this);
    if (i >= 0) this.parent.children.splice(i, 1);
    this.parent = null;
  }
  addEventListener(type, handler) { this.listeners.set(type, handler); }
  dispatchSubmit() {
    const handler = this.listeners.get('submit');
    if (!handler) throw new Error('no submit listener registered');
    handler({preventDefault() {}});
  }
  _matches(selector) {
    const match = /^\[([a-zA-Z0-9-]+)(?:="([^"]*)")?\]$/.exec(selector);
    if (!match) throw new Error('unsupported selector in FakeElement: ' + selector);
    const [, name, value] = match;
    if (!this.attrs.has(name)) return false;
    return value === undefined || this.attrs.get(name) === value;
  }
  _all(selector) {
    const results = [];
    const walk = node => {
      node.children.forEach(child => {
        if (child._matches(selector)) results.push(child);
        walk(child);
      });
    };
    walk(this);
    return results;
  }
  querySelector(selector) { return this._all(selector)[0] || null; }
  querySelectorAll(selector) { return this._all(selector); }
}

export function fakeDom() {
  return {createElement: tagName => new FakeElement(tagName)};
}

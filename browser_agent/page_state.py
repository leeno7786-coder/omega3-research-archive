"""What Omega's task window shows, read through page JavaScript and bounded on the way in.

The page's answer is untrusted data: bounded text, a short list of visible controls with
their viewport centres, never markup, never a credential-bearing field value.
"""
from __future__ import annotations

from typing import Any

MAX_TEXT = 8000
MAX_ELEMENTS = 80

# Visible, interactive controls with their centre in CSS viewport pixels. An element hidden
# under another (a modal, a cookie banner) is skipped so the model sees what a click would hit.
PAGE_STATE_SCRIPT = r"""
const vw = window.innerWidth, vh = window.innerHeight;
const selector = 'a[href], button, input, select, textarea, summary, [role="button"], [role="link"],'
  + ' [role="tab"], [role="menuitem"], [role="checkbox"], [role="radio"], [role="option"],'
  + ' [role="combobox"], [role="switch"], [contenteditable="true"], [onclick]';
const elements = [];
const seen = new Set();
for (const el of document.querySelectorAll(selector)) {
  if (elements.length >= 80) break;
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.right < 0 || r.top > vh || r.left > vw) continue;
  const style = window.getComputedStyle(el);
  if (style.visibility === 'hidden' || style.display === 'none' || style.opacity === '0') continue;
  const cx = Math.round(Math.min(Math.max(r.left, 0) + Math.min(r.width, vw - Math.max(r.left, 0)) / 2, vw - 1));
  const cy = Math.round(Math.min(Math.max(r.top, 0) + Math.min(r.height, vh - Math.max(r.top, 0)) / 2, vh - 1));
  const top = document.elementFromPoint(cx, cy);
  if (top && top !== el && !el.contains(top) && !top.contains(el)) continue;
  const tag = el.tagName.toLowerCase();
  const label = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder')
    || el.getAttribute('title') || el.getAttribute('alt') || el.getAttribute('name') || '')
    .replace(/\s+/g, ' ').trim().slice(0, 60);
  const key = tag + '|' + label + '|' + cx + '|' + cy;
  if (seen.has(key)) continue;
  seen.add(key);
  const entry = { tag: tag, text: label, x: cx, y: cy };
  if (tag === 'input') entry.type = (el.type || 'text').toLowerCase();
  if (tag === 'input' && entry.type === 'password') entry.text = label ? label : 'password';
  if (tag === 'a' && el.href) entry.href = String(el.href).slice(0, 200);
  if (el === document.activeElement) entry.focused = true;
  if (el.disabled) entry.disabled = true;
  elements.push(entry);
}
const active = document.activeElement;
const activeInfo = active && active !== document.body ? {
  tag: active.tagName.toLowerCase(),
  type: active.tagName === 'INPUT' ? (active.type || 'text').toLowerCase() : '',
  editable: !!(active.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(active.tagName))
} : null;
return {
  url: location.href,
  title: document.title,
  ready: document.readyState,
  viewport: { width: vw, height: vh, dpr: window.devicePixelRatio || 1 },
  scroll: { x: Math.round(window.scrollX), y: Math.round(window.scrollY),
            height: Math.round(document.documentElement.scrollHeight) },
  text: (document.body && document.body.innerText ? document.body.innerText : '').slice(0, 8000),
  elements: elements,
  active: activeInfo
};
"""

READY_STATE_SCRIPT = "return document.readyState;"

# A teal pointer drawn into Omega's tab so the operator can see where the agent is about to act.
# pointer-events:none keeps the gesture on the page underneath.
CURSOR_SCRIPT = r"""
const x = Number(arguments[0]), y = Number(arguments[1]), state = String(arguments[2] || 'idle');
const root = document.documentElement || document.body;
let host = document.getElementById('omega-agent-cursor-host');
if (!host) {
  host = document.createElement('div');
  host.id = 'omega-agent-cursor-host';
  host.setAttribute('aria-hidden', 'true');
  host.style.cssText = 'position:fixed;inset:0;z-index:2147483646;pointer-events:none;';
  const cursor = document.createElement('div');
  cursor.id = 'omega-agent-cursor';
  cursor.style.cssText = 'position:fixed;left:0;top:0;width:18px;height:18px;z-index:2147483647;pointer-events:none;'
    + 'filter:drop-shadow(0 1px 2px rgba(0,0,0,.7));';
  const shape = document.createElement('div');
  shape.style.cssText = 'width:12px;height:17px;background:#00d4aa;border:1px solid #061d19;'
    + 'clip-path:polygon(0 0, 0 100%, 28% 72%, 46% 100%, 62% 91%, 44% 63%, 78% 63%);';
  cursor.appendChild(shape);
  host.appendChild(cursor);
  root.appendChild(host);
}
const cursor = document.getElementById('omega-agent-cursor');
cursor.dataset.state = state;
cursor.style.transform = 'translate3d(' + x + 'px,' + y + 'px,0)';
return true;
"""

# A visible, non-blocking mark on Omega's window. pointer-events:none keeps every click and
# key reaching the page; ownership is enforced by the controller, not by the overlay.
TASK_BANNER_SCRIPT = r"""
try {
  if (document.getElementById('omega-task-banner')) return true;
  const banner = document.createElement('div');
  banner.id = 'omega-task-banner';
  banner.textContent = 'OMEGA is working in this window';
  banner.style.cssText = 'position:fixed;top:0;left:0;right:0;z-index:2147483647;pointer-events:none;'
    + 'font:600 11px/18px system-ui,sans-serif;letter-spacing:.08em;text-align:center;'
    + 'color:#f5e9d6;background:rgba(120,60,20,.82);box-shadow:0 0 0 2px rgba(210,120,50,.9) inset;';
  (document.body || document.documentElement).appendChild(banner);
  return true;
} catch (e) { return false; }
"""


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit] if isinstance(value, str) else ""


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value or value in (float("inf"), float("-inf")):
        return default
    return float(value)


def bounded_page_state(raw: Any) -> dict[str, Any]:
    """Bound the page's answer: sizes, counts and lengths, credentials never echoed."""
    from schema.browser_perception import public_url

    if not isinstance(raw, dict):
        raw = {}
    viewport = raw.get("viewport") if isinstance(raw.get("viewport"), dict) else {}
    scroll = raw.get("scroll") if isinstance(raw.get("scroll"), dict) else {}
    elements: list[dict[str, Any]] = []
    for item in (raw.get("elements") or [])[:MAX_ELEMENTS] if isinstance(raw.get("elements"), list) else []:
        if not isinstance(item, dict):
            continue
        entry = {"tag": _text(item.get("tag"), 32), "text": _text(item.get("text"), 60),
                 "x": int(_number(item.get("x"))), "y": int(_number(item.get("y")))}
        if item.get("type"):
            entry["type"] = _text(item.get("type"), 24)
        if item.get("href"):
            entry["href"] = public_url(_text(item.get("href"), 200))
        for flag in ("focused", "disabled"):
            if item.get(flag) is True:
                entry[flag] = True
        elements.append(entry)
    active = raw.get("active") if isinstance(raw.get("active"), dict) else None
    text = str(raw.get("text") or "")[:MAX_TEXT] if isinstance(raw.get("text"), str) else ""
    return {
        "url": public_url(_text(raw.get("url"), 2000)),
        "title": _text(raw.get("title"), 500),
        "ready": _text(raw.get("ready"), 20) or "unknown",
        "viewport": {"width": int(_number(viewport.get("width"))), "height": int(_number(viewport.get("height"))),
                     "dpr": round(_number(viewport.get("dpr"), 1.0) or 1.0, 3)},
        "scroll": {"x": int(_number(scroll.get("x"))), "y": int(_number(scroll.get("y"))),
                   "height": int(_number(scroll.get("height")))},
        "text": text,
        "elements": elements,
        "active": ({"tag": _text(active.get("tag"), 32), "type": _text(active.get("type"), 24),
                    "editable": active.get("editable") is True} if active else None),
    }


__all__ = ["CURSOR_SCRIPT", "PAGE_STATE_SCRIPT", "READY_STATE_SCRIPT", "TASK_BANNER_SCRIPT", "bounded_page_state"]

"""Bounded structured reads of the page the browser agent is looking at.

The page script collects search results, article text, links, tables, metadata,
and JSON-LD. ``bound_extract`` is the trust boundary: sizes, counts, and public
URLs only. A result that is not an extract object fails instead of looking empty.
"""
from __future__ import annotations

from typing import Any

from schema.browser_perception import public_url

EXTRACT_MODES = frozenset({"article", "search_results", "links", "table", "meta", "jsonld", "selector"})
_MAX_TEXT = 6000
_MAX_SNIPPET = 300
_MAX_ITEMS = 12
_MAX_LINKS = 25
_MAX_ROWS = 6
_MAX_CELLS = 8

# arguments: [mode, selector, attribute]. Returns {success, mode, data|error}.
EXTRACT_SCRIPT = r"""
/* omega-extract */
const mode = String(arguments[0] || '');
const selector = String(arguments[1] || '');
const attribute = String(arguments[2] || 'text');
const clip = (value, max) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, max);
const absUrl = (href) => { try { return new URL(href, location.href).href; } catch (e) { return ''; } };
const visible = (el) => {
  if (!el || !el.getBoundingClientRect) return false;
  const rect = el.getBoundingClientRect();
  if (rect.width < 2 || rect.height < 2) return false;
  const style = getComputedStyle(el);
  return style.visibility !== 'hidden' && style.display !== 'none' && style.opacity !== '0';
};
const textOf = (node, max) => {
  if (!node) return '';
  const clone = node.cloneNode(true);
  clone.querySelectorAll('script,style,noscript,svg,nav,footer,[aria-hidden="true"]').forEach((child) => child.remove());
  return clip(clone.innerText || clone.textContent || '', max);
};
if (mode === 'article') {
  const candidates = document.querySelectorAll('article, main, [role="main"]');
  let best = null;
  let bestWords = -1;
  candidates.forEach((node) => {
    const words = textOf(node, 12000).split(/\s+/).filter(Boolean).length;
    if (words > bestWords) { bestWords = words; best = node; }
  });
  const source = best || document.body;
  const text = textOf(source, 6000);
  return {success: true, mode: mode, data: {
    text: text, excerpt: clip(text, 500),
    word_count: text ? text.split(/\s+/).filter(Boolean).length : 0,
    selector_hint: best ? best.tagName.toLowerCase() : 'body'}};
}
if (mode === 'search_results') {
  const results = [];
  const seen = new Set();
  document.querySelectorAll('h2 a[href], h3 a[href], [data-testid="result"] a[href], .result a[href]').forEach((link) => {
    if (results.length >= 12 || !visible(link)) return;
    const url = absUrl(link.getAttribute('href'));
    if (!url || url.indexOf('javascript:') === 0 || seen.has(url) || url.indexOf('/search?') !== -1) return;
    const title = clip(link.innerText || link.getAttribute('aria-label') || '', 200);
    if (title.length < 2) return;
    seen.add(url);
    const card = link.closest('li, article, div') || link;
    results.push({title: title, url: url, snippet: clip(card.innerText || '', 300)});
  });
  return {success: true, mode: mode, data: results};
}
if (mode === 'links') {
  const links = [];
  const seen = new Set();
  document.querySelectorAll('a[href]').forEach((link) => {
    if (links.length >= 25 || !visible(link)) return;
    const url = absUrl(link.getAttribute('href'));
    const text = clip(link.innerText || link.getAttribute('aria-label') || '', 160);
    if (!url || url.indexOf('javascript:') === 0 || text.length < 2 || seen.has(url)) return;
    seen.add(url);
    links.push({text: text, url: url});
  });
  return {success: true, mode: mode, data: links};
}
if (mode === 'table') {
  const tables = [];
  document.querySelectorAll('table').forEach((table) => {
    if (tables.length >= 3 || !visible(table)) return;
    const headers = Array.from(table.querySelectorAll('thead th, tr th')).slice(0, 8).map((cell) => clip(cell.textContent, 80));
    const rows = [];
    table.querySelectorAll('tbody tr, tr').forEach((row) => {
      if (rows.length >= 6) return;
      const cells = Array.from(row.querySelectorAll('td')).slice(0, 8).map((cell) => clip(cell.textContent, 80));
      if (cells.some(Boolean)) rows.push(cells);
    });
    if (headers.length || rows.length) {
      const caption = table.querySelector('caption');
      tables.push({caption: clip(caption ? caption.textContent : '', 120), headers: headers, rows: rows});
    }
  });
  return {success: true, mode: mode, data: tables};
}
if (mode === 'meta') {
  const pick = (sel) => { const el = document.querySelector(sel); return clip(el ? (el.getAttribute('content') || '') : '', 300); };
  const canonical = document.querySelector('link[rel="canonical"]');
  return {success: true, mode: mode, data: {
    title: clip(document.title, 200),
    description: pick('meta[name="description"]') || pick('meta[property="og:description"]'),
    canonical: canonical ? absUrl(canonical.getAttribute('href')) : ''}};
}
if (mode === 'jsonld') {
  const blocks = [];
  document.querySelectorAll('script[type="application/ld+json"]').forEach((script) => {
    if (blocks.length >= 5) return;
    try {
      const parsed = JSON.parse(script.textContent || '');
      const entries = Array.isArray(parsed) ? parsed : [parsed];
      entries.forEach((entry) => {
        if (!entry || typeof entry !== 'object' || blocks.length >= 5) return;
        blocks.push({type: clip(entry['@type'] || entry.type || 'Thing', 80),
                     name: clip(entry.name || entry.headline || '', 200),
                     description: clip(entry.description || '', 300)});
      });
    } catch (e) { /* one bad block is skipped; the rest still return */ }
  });
  return {success: true, mode: mode, data: blocks};
}
if (mode === 'selector') {
  if (!selector) return {success: false, mode: mode, error: 'selector is required'};
  let nodes;
  try { nodes = document.querySelectorAll(selector); }
  catch (e) { return {success: false, mode: mode, error: 'invalid selector'}; }
  if (!nodes.length) return {success: false, mode: mode, error: 'No elements: ' + selector.slice(0, 120)};
  const data = [];
  nodes.forEach((el) => {
    if (data.length >= 20) return;
    if (attribute === 'href') data.push(absUrl(el.getAttribute('href')));
    else if (attribute === 'src') data.push(absUrl(el.getAttribute('src')));
    else data.push(clip(el.innerText || el.textContent || '', 500));
  });
  return {success: true, mode: mode, selector: selector.slice(0, 200), attribute: attribute, data: data.filter(Boolean)};
}
return {success: false, mode: mode || 'unknown', error: 'unknown extract mode'};
"""


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _http_url(value: Any) -> str:
    return public_url(_clip(value, 2000))


def _rows(value: Any) -> list[dict[str, str]]:
    rows = []
    if not isinstance(value, list):
        return rows
    for item in value[:_MAX_ITEMS]:
        if not isinstance(item, dict):
            continue
        url = _http_url(item.get("url"))
        title = _clip(item.get("title") or item.get("text"), 200)
        if not url or not title:
            continue
        row = {"title": title, "url": url}
        if item.get("snippet"):
            row["snippet"] = _clip(item.get("snippet"), _MAX_SNIPPET)
        if item.get("text") and "title" not in item:
            row = {"text": title, "url": url}
        rows.append(row)
    return rows


def _bound_data(mode: str, data: Any) -> Any:
    if mode == "article":
        body = data if isinstance(data, dict) else {}
        text = _clip(body.get("text"), _MAX_TEXT)
        return {"text": text, "excerpt": _clip(body.get("excerpt") or text, 500),
                "word_count": len(text.split()) if text else 0,
                "selector_hint": _clip(body.get("selector_hint"), 32) or "body"}
    if mode == "search_results":
        return _rows(data)
    if mode == "links":
        links = []
        if isinstance(data, list):
            for item in data[:_MAX_LINKS]:
                if not isinstance(item, dict):
                    continue
                url = _http_url(item.get("url"))
                text = _clip(item.get("text"), 160)
                if url and text:
                    links.append({"text": text, "url": url})
        return links
    if mode == "table":
        tables = []
        if isinstance(data, list):
            for item in data[:3]:
                if not isinstance(item, dict):
                    continue
                headers = [_clip(cell, 80) for cell in (item.get("headers") or [])[:_MAX_CELLS] if _clip(cell, 80)]
                rows = []
                for row in (item.get("rows") or [])[:_MAX_ROWS]:
                    if isinstance(row, list):
                        cells = [_clip(cell, 80) for cell in row[:_MAX_CELLS]]
                        if any(cells):
                            rows.append(cells)
                if headers or rows:
                    tables.append({"caption": _clip(item.get("caption"), 120), "headers": headers, "rows": rows})
        return tables
    if mode == "meta":
        body = data if isinstance(data, dict) else {}
        return {"title": _clip(body.get("title"), 200), "description": _clip(body.get("description"), 300),
                "canonical": _http_url(body.get("canonical"))}
    if mode == "jsonld":
        blocks = []
        if isinstance(data, list):
            for item in data[:5]:
                if isinstance(item, dict):
                    blocks.append({"type": _clip(item.get("type"), 80) or "Thing",
                                   "name": _clip(item.get("name"), 200),
                                   "description": _clip(item.get("description"), 300)})
        return blocks
    values = []
    if isinstance(data, list):
        for item in data[:20]:
            text = _clip(item, 500)
            if text.startswith(("http://", "https://")):
                text = _http_url(text)
            if text:
                values.append(text)
    return values


def bound_extract(raw: Any) -> dict[str, Any]:
    """Bound one page extract. A non-object is a failure, not an empty page."""
    if not isinstance(raw, dict):
        raise ValueError("page extract was not an object")
    mode = str(raw.get("mode") or "")
    if mode not in EXTRACT_MODES:
        raise ValueError("page extract mode was not recognized")
    if raw.get("success") is not True:
        return {"success": False, "mode": mode, "error": _clip(raw.get("error") or "extract failed", 300)}
    bound: dict[str, Any] = {"success": True, "mode": mode, "data": _bound_data(mode, raw.get("data"))}
    if mode == "selector":
        bound["selector"] = _clip(raw.get("selector"), 200)
        bound["attribute"] = _clip(raw.get("attribute"), 16) or "text"
    return bound


__all__ = ["EXTRACT_MODES", "EXTRACT_SCRIPT", "bound_extract"]

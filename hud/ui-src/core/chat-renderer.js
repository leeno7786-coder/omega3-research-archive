// Safe assistant-output renderer for Markdown, fenced code, and Mermaid blocks.
// This file is loaded after boot.js and replaces its legacy renderer helpers.
(function () {
  'use strict';

  function _escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function _safeUrl(url) {
    const raw = String(url == null ? '' : url).replace(/&amp;/g, '&');
    const probe = raw.replace(/\s+/g, '').toLowerCase();
    const match = probe.match(/^([a-z][a-z0-9+.\-]*):/);
    if (match && !['http', 'https', 'mailto'].includes(match[1])) return null;
    return raw;
  }

  // Highlighting runs over ESCAPED html, so every pass must step OVER an html entity
  // rather than through it. The number pass learned this in 2026-09-15; the comment
  // pass did not, and `#.*$` matched from the `#` inside an escaped apostrophe
  // (`&#39;`) to the end of the line. That split the entity from its ampersand, so the
  // browser painted a literal `&#39;` and coloured the rest of the line as a comment —
  // in every tool output that contained an apostrophe (operator transcript 2026-09-20).
  // Painting the gaps BETWEEN entities makes that structurally impossible.
  const _HTML_ENTITY = /&#?\w+;/g;

  function _paintCodeSegment(text, keywords) {
    return text
      .replace(new RegExp(keywords, 'g'), '<span class="syn-kw">$1</span>')
      .replace(/(#.*$|\/\/.*$)/gm, '<span class="syn-cm">$1</span>')
      .replace(/\b(\d+\.?\d*)\b/g, '<span class="syn-num">$1</span>');
  }

  function _highlightCode(code, lang) {
    const escaped = _escapeHtml(code);
    const language = String(lang || '').toLowerCase();
    const keywords = language === 'python'
      ? '\\b(def|class|return|if|elif|else|for|while|import|from|as|try|except|raise|with|pass|yield|lambda|None|True|False|self|in|not|and|or|async|await|print|len|range|type|str|int|float|list|dict|set|tuple|open|assert)\\b'
      : '\\b(function|const|let|var|if|else|for|while|do|switch|case|break|continue|return|import|export|from|class|extends|new|this|super|try|catch|finally|throw|async|await|yield|null|undefined|true|false|typeof|instanceof|in|of|require|module|console|Promise|Map|Set|Array|Object)\\b';
    let html = '';
    let cursor = 0;
    let entity;
    _HTML_ENTITY.lastIndex = 0;
    while ((entity = _HTML_ENTITY.exec(escaped)) !== null) {
      html += _paintCodeSegment(escaped.slice(cursor, entity.index), keywords) + entity[0];
      cursor = entity.index + entity[0].length;
    }
    return html + _paintCodeSegment(escaped.slice(cursor), keywords);
  }

  function _encodeChatCode(value) {
    return encodeURIComponent(String(value == null ? '' : value));
  }

  function _decodeChatCode(value) {
    try { return decodeURIComponent(String(value || '')); } catch (_) { return ''; }
  }

  // A unified diff, one classed line at a time: adds, removes, hunk headers and
  // the file header each get their own colour. Used for ```diff fences and for
  // the diff a write_file / edit_file tool call reports into the thinking trace.
  function _renderDiff(text) {
    const lines = String(text == null ? '' : text).replace(/\r\n?/g, '\n').split('\n');
    return lines.map(line => {
      let cls = 'diff-ctx';
      if (/^(\+\+\+|---)\s/.test(line)) cls = 'diff-meta';
      else if (line.startsWith('@@')) cls = 'diff-hunk';
      else if (line.startsWith('+')) cls = 'diff-add';
      else if (line.startsWith('-')) cls = 'diff-del';
      return '<span class="diff-line ' + cls + '">' + _escapeHtml(line) + '</span>';
    }).join('\n');
  }

  function _renderCodeBlock(code, lang) {
    const language = String(lang || 'text').replace(/[^a-z0-9_+\-]/gi, '').slice(0, 24) || 'text';
    const isDiff = language === 'diff' || language === 'patch';
    return '<div class="chat-code-block' + (isDiff ? ' is-diff' : '') + '">'
      + '<div class="chat-code-head"><span class="chat-code-lang">' + _escapeHtml(language)
      + '</span><button type="button" class="chat-code-copy" data-code="' + _encodeChatCode(code)
      + '">copy</button></div><pre><code class="lang-' + _escapeHtml(language) + '">'
      + (isDiff ? _renderDiff(code) : _highlightCode(code, language)) + '</code></pre></div>';
  }

  function _renderMermaidBlock(source) {
    const diagram = window.OmegaMermaid && typeof window.OmegaMermaid.render === 'function'
      ? window.OmegaMermaid.render(source) : null;
    const fallbackClass = diagram ? 'chat-mermaid-fallback' : 'chat-mermaid-fallback';
    const fallbackOpen = diagram ? '' : ' open';
    return '<section class="chat-mermaid">'
      + (diagram ? '<div class="chat-mermaid-diagram" role="img" aria-label="Mermaid diagram">'
        + diagram + '</div>' : '<div class="chat-mermaid-error">Mermaid preview unavailable; source preserved.</div>')
      + '<details class="' + fallbackClass + '"' + fallbackOpen + '><summary>source</summary>'
      + _renderCodeBlock(source, 'mermaid') + '</details></section>';
  }

  function _tokenizeChatBlocks(text) {
    const tokens = [];
    const source = String(text == null ? '' : text).replace(/\r\n?/g, '\n');
    const fence = /^```([a-z0-9_+\-]*)[ \t]*\n([\s\S]*?)^```[ \t]*$/gim;
    let cursor = 0;
    let result = '';
    let match;
    while ((match = fence.exec(source))) {
      result += source.slice(cursor, match.index);
      const value = match[2].replace(/\n$/, '');
      const type = String(match[1] || '').toLowerCase() === 'mermaid' ? 'mermaid' : 'code';
      tokens.push({ type, lang: match[1] || 'text', value });
      result += '\n@@OMEGA_BLOCK_' + (tokens.length - 1) + '@@\n';
      cursor = fence.lastIndex;
    }
    result += source.slice(cursor);
    return { text: result, tokens };
  }

  function _renderInline(value) {
    let html = _escapeHtml(value);
    const inlineCode = [];
    html = html.replace(/`([^`\n]+)`/g, (_m, code) => {
      inlineCode.push('<code>' + code + '</code>');
      // Keep the placeholder free of Markdown punctuation. The old underscored
      // token was consumed by the emphasis pass below and leaked into the HUD
      // as ``@@OMEGA<em>INLINE</em>0@@`` instead of rendering the code span.
      return '@@OMEGAINLINE' + (inlineCode.length - 1) + 'TOKEN@@';
    });
    html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_m, label, url) => {
      const safe = _safeUrl(url);
      return safe ? '<a href="' + _escapeHtml(safe) + '" target="_blank" rel="noopener">' + label + '</a>' : label;
    });
    html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__(.+?)__/g, '<strong>$1</strong>');
    html = html.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    html = html.replace(/_([^_\n]+)_/g, '<em>$1</em>');
    return html.replace(/@@OMEGAINLINE(\d+)TOKEN@@/g, (_m, index) => inlineCode[Number(index)] || '');
  }

  function _tableRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim());
  }

  function _renderMarkdownText(text) {
    const lines = String(text || '').split('\n');
    const output = [];
    let paragraph = [];
    const flushParagraph = () => {
      if (paragraph.length) output.push('<p>' + paragraph.map(_renderInline).join('<br>') + '</p>');
      paragraph = [];
    };
    for (let i = 0; i < lines.length; i += 1) {
      const line = lines[i];
      const block = line.match(/^@@OMEGA_BLOCK_(\d+)@@$/);
      if (block) { flushParagraph(); output.push(block[0]); continue; }
      const heading = line.match(/^\s{0,3}(#{1,3})\s+(.+?)\s*#*\s*$/);
      if (heading) { flushParagraph(); const level = heading[1].length; output.push('<h' + level + '>' + _renderInline(heading[2]) + '</h' + level + '>'); continue; }
      if (/^\s*>/.test(line)) { flushParagraph(); output.push('<blockquote>' + _renderInline(line.replace(/^\s*>\s?/, '')) + '</blockquote>'); continue; }
      const tableSeparator = i + 1 < lines.length && /^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)+\|?\s*$/.test(lines[i + 1]);
      if (line.includes('|') && tableSeparator) {
        flushParagraph();
        const headers = _tableRow(line);
        const rows = [];
        i += 2;
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) { rows.push(_tableRow(lines[i])); i += 1; }
        i -= 1;
        output.push('<table><thead><tr>' + headers.map(cell => '<th>' + _renderInline(cell) + '</th>').join('') + '</tr></thead><tbody>'
          + rows.map(row => '<tr>' + headers.map((_cell, index) => '<td>' + _renderInline(row[index] || '') + '</td>').join('') + '</tr>').join('')
          + '</tbody></table>');
        continue;
      }
      const unordered = line.match(/^\s*[-*+]\s+(.+)$/);
      if (unordered) { flushParagraph(); const items = [unordered[1]]; while (i + 1 < lines.length && (lines[i + 1].match(/^\s*[-*+]\s+(.+)$/))) items.push(lines[++i].match(/^\s*[-*+]\s+(.+)$/)[1]); output.push('<ul>' + items.map(item => '<li>' + _renderInline(item) + '</li>').join('') + '</ul>'); continue; }
      const ordered = line.match(/^\s*\d+[.)]\s+(.+)$/);
      if (ordered) { flushParagraph(); const items = [ordered[1]]; while (i + 1 < lines.length && lines[i + 1].match(/^\s*\d+[.)]\s+(.+)$/)) items.push(lines[++i].match(/^\s*\d+[.)]\s+(.+)$/)[1]); output.push('<ol>' + items.map(item => '<li>' + _renderInline(item) + '</li>').join('') + '</ol>'); continue; }
      if (!line.trim()) { flushParagraph(); continue; }
      paragraph.push(line);
    }
    flushParagraph();
    return output.join('');
  }

  function _renderMarkdown(text) {
    if (!text) return '';
    const tokenized = _tokenizeChatBlocks(text);
    let html = _renderMarkdownText(tokenized.text);
    tokenized.tokens.forEach((token, index) => {
      const marker = '@@OMEGA_BLOCK_' + index + '@@';
      const rendered = token.type === 'mermaid' ? _renderMermaidBlock(token.value) : _renderCodeBlock(token.value, token.lang);
      html = html.split(marker).join(rendered);
    });
    return html;
  }

  document.addEventListener('click', event => {
    const button = event.target && event.target.closest ? event.target.closest('.chat-code-copy') : null;
    if (!button) return;
    const source = _decodeChatCode(button.getAttribute('data-code') || '');
    const original = button.textContent;
    const restore = () => { button.textContent = original || 'copy'; };
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') { button.textContent = 'copy unavailable'; setTimeout(restore, 1200); return; }
    navigator.clipboard.writeText(source).then(() => { button.textContent = 'copied'; setTimeout(restore, 1200); }, () => { button.textContent = 'copy failed'; setTimeout(restore, 1200); });
  });

  window._escapeHtml = _escapeHtml;
  window._safeUrl = _safeUrl;
  window._highlightCode = _highlightCode;
  window._renderMarkdown = _renderMarkdown;
  window._renderDiff = _renderDiff;
  window._renderCodeBlock = _renderCodeBlock;
}());

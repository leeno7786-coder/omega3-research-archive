// Deterministic, local Mermaid subset for the HUD chat renderer.
// Supports the diagram forms Omega is instructed to emit; unsupported syntax
// returns null so the caller can show the source instead of a broken diagram.
(function () {
  'use strict';

  const MAX_SOURCE_LENGTH = 12000;

  function escapeSvg(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function cleanId(value) {
    return String(value == null ? '' : value).trim().replace(/^\[\*\]$/, '__start__');
  }

  function cleanLabel(value) {
    return String(value == null ? '' : value).trim().replace(/^['"]|['"]$/g, '');
  }

  function linesFor(source) {
    return String(source == null ? '' : source)
      .replace(/\r\n?/g, '\n')
      .split('\n')
      .map(line => line.trim())
      .filter(line => line && !line.startsWith('%%'));
  }

  function svgShell(width, height, body, label) {
    return '<svg class="omega-mermaid-svg" xmlns="http://www.w3.org/2000/svg" '
      + 'viewBox="0 0 ' + width + ' ' + height + '" role="img" aria-label="'
      + escapeSvg(label || 'Mermaid diagram') + '">'
      + '<defs><marker id="omega-mermaid-arrow" markerWidth="8" markerHeight="8" '
      + 'refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 z" '
      + 'class="omega-mermaid-arrow"/></marker></defs>' + body + '</svg>';
  }

  function textNode(x, y, text, className, anchor) {
    return '<text x="' + x + '" y="' + y + '" class="' + className + '"'
      + (anchor ? ' text-anchor="' + anchor + '"' : '') + '>'
      + escapeSvg(text) + '</text>';
  }

  function nodePart(id, open, label) {
    if (!open) return { id: cleanId(id), label: cleanLabel(id), shape: 'rect' };
    const shape = open === '{' ? 'diamond' : open === '(' ? 'round' : 'rect';
    return { id: cleanId(id), label: cleanLabel(label || id), shape };
  }

  function addFlowNode(nodes, id, open, label) {
    const node = nodePart(id, open, label);
    if (!node.id || nodes[node.id]) return;
    nodes[node.id] = node;
  }

  function renderFlow(lines) {
    const header = lines[0].match(/^(?:flowchart|graph)\s+(TD|TB|LR|RL|BT)$/i);
    if (!header) return null;
    const vertical = /^(TD|TB)$/i.test(header[1]);
    const nodes = Object.create(null);
    const edges = [];
    const nodePattern = '([A-Za-z0-9_]+)(?:\\s*([\\[\\{\\(])([^\\]\\}\\)]*)[\\]\\}\\)])?';
    const edgeRe = new RegExp('^' + nodePattern + '\\s+(-->|---|==>|-\\.->)\\s*'
      + '(?:\\|([^|]+)\\|\\s*)?' + nodePattern + '$');
    const standaloneRe = new RegExp('^' + nodePattern + '$');
    for (let i = 1; i < lines.length; i += 1) {
      const line = lines[i];
      const edge = line.match(edgeRe);
      if (edge) {
        addFlowNode(nodes, edge[1], edge[2], edge[3]);
        addFlowNode(nodes, edge[6], edge[7], edge[8]);
        edges.push({ from: cleanId(edge[1]), to: cleanId(edge[6]), label: cleanLabel(edge[5]) });
        continue;
      }
      const standalone = line.match(standaloneRe);
      if (standalone) {
        addFlowNode(nodes, standalone[1], standalone[2], standalone[3]);
        continue;
      }
      return null;
    }
    const ids = Object.keys(nodes);
    if (!ids.length) return null;
    const cellW = 190;
    const cellH = 90;
    const width = vertical ? 260 : Math.max(280, ids.length * cellW + 50);
    const height = vertical ? Math.max(180, ids.length * cellH + 50) : 170;
    const positions = Object.create(null);
    ids.forEach((id, index) => {
      positions[id] = vertical
        ? { x: width / 2, y: 45 + index * cellH }
        : { x: 100 + index * cellW, y: 80 };
    });
    let body = '';
    edges.forEach(edge => {
      const a = positions[edge.from];
      const b = positions[edge.to];
      if (!a || !b) return;
      body += '<line x1="' + a.x + '" y1="' + a.y + '" x2="' + b.x + '" y2="' + b.y
        + '" class="omega-mermaid-edge" marker-end="url(#omega-mermaid-arrow)"/>';
      if (edge.label) body += textNode((a.x + b.x) / 2, (a.y + b.y) / 2 - 8, edge.label,
        'omega-mermaid-edge-label', 'middle');
    });
    ids.forEach(id => {
      const node = nodes[id];
      const p = positions[id];
      const label = node.label.length > 24 ? node.label.slice(0, 23) + '…' : node.label;
      if (node.shape === 'diamond') {
        body += '<polygon points="' + p.x + ',' + (p.y - 28) + ' ' + (p.x + 70) + ','
          + p.y + ' ' + p.x + ',' + (p.y + 28) + ' ' + (p.x - 70) + ',' + p.y
          + '" class="omega-mermaid-node omega-mermaid-diamond"/>';
      } else {
        body += '<rect x="' + (p.x - 70) + '" y="' + (p.y - 26) + '" width="140" height="52"'
          + ' rx="' + (node.shape === 'round' ? '26' : '6') + '" class="omega-mermaid-node"/>';
      }
      body += textNode(p.x, p.y + 5, label, 'omega-mermaid-node-label', 'middle');
    });
    return svgShell(width, height, body, 'Mermaid flowchart');
  }

  function renderSequence(lines) {
    if (!/^sequenceDiagram$/i.test(lines[0])) return null;
    const people = [];
    const messages = [];
    const addPerson = name => {
      const value = cleanLabel(name);
      if (value && !people.includes(value)) people.push(value);
    };
    for (let i = 1; i < lines.length; i += 1) {
      const participant = lines[i].match(/^(?:participant|actor)\s+(.+?)(?:\s+as\s+(.+))?$/i);
      if (participant) {
        addPerson(participant[2] || participant[1]);
        continue;
      }
      const message = lines[i].match(/^(.+?)\s*(-{1,2}>>?|-->>?)\s*(.+?)\s*:\s*(.+)$/);
      if (!message) return null;
      addPerson(message[1]);
      addPerson(message[3]);
      messages.push({ from: cleanLabel(message[1]), to: cleanLabel(message[3]), text: cleanLabel(message[4]) });
    }
    if (people.length < 2 || !messages.length) return null;
    const width = Math.max(340, people.length * 170 + 40);
    const height = messages.length * 58 + 100;
    const x = name => 80 + people.indexOf(name) * 170;
    let body = '';
    people.forEach(person => {
      const px = x(person);
      body += '<rect x="' + (px - 52) + '" y="18" width="104" height="30" rx="4" class="omega-mermaid-node"/>';
      body += textNode(px, 38, person, 'omega-mermaid-node-label', 'middle');
      body += '<line x1="' + px + '" y1="48" x2="' + px + '" y2="' + (height - 18)
        + '" class="omega-mermaid-lifeline"/>';
    });
    messages.forEach((message, index) => {
      const y = 82 + index * 58;
      const from = x(message.from);
      const to = x(message.to);
      body += '<line x1="' + from + '" y1="' + y + '" x2="' + to + '" y2="' + y
        + '" class="omega-mermaid-edge" marker-end="url(#omega-mermaid-arrow)"/>';
      body += textNode((from + to) / 2, y - 8, message.text, 'omega-mermaid-edge-label', 'middle');
    });
    return svgShell(width, height, body, 'Mermaid sequence diagram');
  }

  function renderState(lines) {
    if (!/^stateDiagram(?:-v2)?$/i.test(lines[0])) return null;
    const states = Object.create(null);
    const edges = [];
    for (let i = 1; i < lines.length; i += 1) {
      const match = lines[i].match(/^(.+?)\s*-->\s*(.+?)(?:\s*:\s*(.*))?$/);
      if (!match) return null;
      const from = cleanId(match[1]);
      const to = cleanId(match[2]);
      if (!from || !to) return null;
      states[from] = cleanLabel(match[1]);
      states[to] = cleanLabel(match[2]);
      edges.push({ from, to, label: cleanLabel(match[3]) });
    }
    const ids = Object.keys(states);
    if (!ids.length) return null;
    const width = Math.max(300, ids.length * 170 + 50);
    const height = 180;
    const positions = Object.create(null);
    ids.forEach((id, index) => { positions[id] = { x: 90 + index * 170, y: 90 }; });
    let body = '';
    edges.forEach(edge => {
      const a = positions[edge.from];
      const b = positions[edge.to];
      body += '<line x1="' + a.x + '" y1="' + a.y + '" x2="' + b.x + '" y2="' + b.y
        + '" class="omega-mermaid-edge" marker-end="url(#omega-mermaid-arrow)"/>';
      if (edge.label) body += textNode((a.x + b.x) / 2, 76, edge.label, 'omega-mermaid-edge-label', 'middle');
    });
    ids.forEach(id => {
      const p = positions[id];
      body += '<rect x="' + (p.x - 58) + '" y="' + (p.y - 24) + '" width="116" height="48"'
        + ' rx="24" class="omega-mermaid-node omega-mermaid-state"/>';
      body += textNode(p.x, p.y + 5, states[id].slice(0, 20), 'omega-mermaid-node-label', 'middle');
    });
    return svgShell(width, height, body, 'Mermaid state diagram');
  }

  function render(source) {
    const raw = String(source == null ? '' : source);
    if (!raw.trim() || raw.length > MAX_SOURCE_LENGTH) return null;
    try {
      const lines = linesFor(raw);
      return renderFlow(lines) || renderSequence(lines) || renderState(lines) || null;
    } catch (_) {
      return null;
    }
  }

  window.OmegaMermaid = { render };
}());

// Memory graph visualization + settings integration (hud/ui-src/core/memory-graph.js)
// Depends on: Telemetry (settings live feeds), fetch /api/memory/lobes
// Exposes: global.MemoryGraph, window._memoryGraphInit, window._memoryGraphDestroy

/**
 *      y Graph Visualization - Omega HUD
 * =     =================================
 * Interactive, blooming node gr     or the Omega cognitive substrate.
 * Pur     il     vaScript. No dep     cies.
 *
 * Architecture:
 *   - ForceLayout:   Physics engine (r     ion, springs, gravity, collision)
 *   - BloomAnimator: Sp     physics bloom/collapse animations
 *   - SVGRenderer:       DOM management, zoom, pan, events
 *   - LinkParticles: An     d particles traveling along links
 *   - T     pManager: Floating hover tooltips
 *   - DetailPanel:        -i     el for memory leaf content
 *
 * Exposes: MemoryGraph.init(container), .destroy     loadData(data), .setSearch(query)
 */
(function (global) {
  'use strict';
// ==========================================================================
// CONFIGURATION
// ==========================================================================

/** Color palette for the 8 lobes (Dewey hundreds) */
const LOBE_COLORS = [
  { fill: '#c9a465', glow: 'rgba(201,164,101,0.5)', label: '#e0c080' }, // 000 Identity - amber
  { fill: '#d4a834', glow: 'rgba(212,168,52,0.5)', label: '#e8c860' }, // 100 Personas - gold
  { fill: '#6b8ab4', glow: 'rgba(107,138,180,0.5)', label: '#90b0d0' }, // 200 Knowledge - steel blue
  { fill: '#8a7aad', glow: 'rgba(138,122,173,0.5)', label: '#a89ad0' }, // 300 Reasoning - purple
  { fill: '#6b8a6f', glow: 'rgba(107,138,111,0.5)', label: '#90b090' }, // 400 Situational - sage
  { fill: '#c4855e', glow: 'rgba(196,133,94,0.5)', label: '#e0a87a' }, // 500 Tools - copper
  { fill: '#b07880', glow: 'rgba(176,120,128,0.5)', label: '#d0a0a8' }, // 600 Journal - rose
  { fill: '#5a9a8a', glow: 'rgba(90,154,138,0.5)', label: '#80c0b0' }, // 700 Vision - teal
  { fill: '#b85b4a', glow: 'rgba(184,91,74,0.5)', label: '#d08070' }, // 800 Coding - crimson
  { fill: '#8a9aaa', glow: 'rgba(138,154,170,0.5)', label: '#a8b8c8' }, // 900 Temporal - silver
  { fill: '#c9a44a', glow: 'rgba(201,164,74,0.5)', label: '#e0c068' }, // 1000 Workflows - orange
  { fill: '#6a6a7a', glow: 'rgba(106,106,122,0.5)', label: '#8888a0' }, // 1100 Dev - slate
]

/** Physics constants */
const PHYSICS = {
  REPULSION_STRENGTH: 1200,
  REPULSION_DISTANCE: 200,
  SPRING_BASE_STIFFNESS: 0.08,
  SPRING_LEVEL_MULTIPLIER: 0.02,
  CENTER_GRAVITY: 0.002,
  PARENT_GRAVITY: 0.04,
  DAMPING: 0.55,
  MAX_VELOCITY: 10,
  COLLISION_PADDING: 8,
  BOUNDARY_MARGIN: 60,
  BOUNDARY_STRENGTH: 0.3,
  VELOCITY_THRESHOLD: 0.02,
  STABLE_FRAMES: 45,
};

/** Animation constants */
const ANIMATION = {
  BLOOM_DURATION: 600,
  COLLAPSE_DURATION: 300,
  STAGGER_DELAY: 40,
  EASE_OUT_BACK: function (t) {
    // Cubic-bezier(0.34, 1.56, 0.64, 1) approximation
    const c1 = 1.70158;
    const c3 = c1 + 1;
    return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
  },
  EASE_IN: function (t) { return t * t; },
  EASE_OUT_QUAD: function (t) { return 1 - (1 - t) * (1 - t); },
};

/** Node sizing */
const NODE_RADIUS = {
  root: 28,
  lobe: 20,
  sublobe: 14,
  subdomain: 14,
  directory: 11,
  memory: 9,
};

/** Zoom constraints */
const ZOOM = {
  MIN: 0.15,
  MAX: 4.0,
  WHEEL_SENSITIVITY: 0.001,
  BUTTON_STEP: 0.4,
};

// ==========================================================================
// MOCK DATA GENERATOR
// =====================================      ====       ====================

/**
 * Generate a realis       ory tree for demo/fallback use.
 * 8 lobes, 3-5 s       gories each, 4-8 memories each.
 */
function generateMockTree() {
  // ── Real 12-Lobe Dewey 3D Memory Tree ──
  const lobes = [
    {
      coord: '000', label: 'Identity', color: 0, type: 'scripted', subs: [
        { coord: '0,0', label: 'constitution' },
        { coord: '0,1', label: 'personality' },
        { coord: '0,2', label: 'emergent_traits' },
        { coord: '0,3', label: 'evolution' },
        { coord: '0,4', label: 'guardian' },
        { coord: '0,5', label: 'self_inquiry' },
        { coord: '0,6', label: 'omega' },
      ]
    },
    {
      coord: '100', label: 'Personas', color: 1, type: 'entity', subs: [
        { coord: '1,0', label: 'operator' },
        { coord: '1,1', label: 'person_stub' },
      ]
    },
    {
      coord: '200', label: 'Knowledge', color: 2, type: 'setfit', subs: [
        { coord: '2,0', label: 'astronomy' }, { coord: '2,1', label: 'physics' },
        { coord: '2,2', label: 'quantum_physics' }, { coord: '2,3', label: 'chemistry' },
        { coord: '2,4', label: 'biology' }, { coord: '2,5', label: 'genetics' },
        { coord: '2,6', label: 'neuroscience' }, { coord: '2,7', label: 'ecology' },
        { coord: '2,8', label: 'geology' }, { coord: '2,9', label: 'meteorology' },
        { coord: '2,10', label: 'oceanography' }, { coord: '2,11', label: 'environmental' },
        { coord: '2,12', label: 'mathematics' }, { coord: '2,13', label: 'statistics' },
        { coord: '2,14', label: 'probability' }, { coord: '2,15', label: 'logic' },
        { coord: '2,16', label: 'game_theory' }, { coord: '2,17', label: 'mech_engineering' },
        { coord: '2,18', label: 'elec_engineering' }, { coord: '2,19', label: 'civil_engineering' },
        { coord: '2,20', label: 'materials' }, { coord: '2,21', label: 'aerospace' },
        { coord: '2,22', label: 'software_eng' }, { coord: '2,23', label: 'robotics' },
        { coord: '2,24', label: 'ai' }, { coord: '2,25', label: 'cybersecurity' },
        { coord: '2,26', label: 'cryptography' }, { coord: '2,27', label: 'medicine' },
        { coord: '2,28', label: 'pharmacology' }, { coord: '2,29', label: 'epidemiology' },
        { coord: '2,30', label: 'anatomy' }, { coord: '2,31', label: 'psychology' },
        { coord: '2,32', label: 'sociology' }, { coord: '2,33', label: 'anthropology' },
        { coord: '2,34', label: 'archaeology' }, { coord: '2,35', label: 'economics' },
        { coord: '2,36', label: 'political_science' }, { coord: '2,37', label: 'philosophy' },
        { coord: '2,38', label: 'ethics' }, { coord: '2,39', label: 'religion' },
        { coord: '2,40', label: 'history' }, { coord: '2,41', label: 'geography' },
        { coord: '2,42', label: 'linguistics' }, { coord: '2,43', label: 'literature' },
        { coord: '2,44', label: 'music' }, { coord: '2,45', label: 'visual_arts' },
        { coord: '2,46', label: 'performing_arts' }, { coord: '2,47', label: 'architecture' },
        { coord: '2,48', label: 'film' }, { coord: '2,49', label: 'culinary' },
        { coord: '2,50', label: 'agriculture' }, { coord: '2,51', label: 'sports' },
        { coord: '2,52', label: 'education' }, { coord: '2,53', label: 'law' },
        { coord: '2,54', label: 'business' }, { coord: '2,55', label: 'finance' },
      ]
    },
    {
      coord: '300', label: 'Reasoning', color: 3, type: 'scripted', subs: [
        { coord: '3,0', label: 'goals' }, { coord: '3,1', label: 'decisions' },
        { coord: '3,2', label: 'constraints' }, { coord: '3,3', label: 'heuristics' },
        { coord: '3,4', label: 'alignment' },
      ]
    },
    {
      coord: '400', label: 'Situational', color: 4, type: 'entity', subs: [
        { coord: '4,0', label: 'who' }, { coord: '4,1', label: 'what' },
        { coord: '4,2', label: 'when' }, { coord: '4,3', label: 'where' },
        { coord: '4,4', label: 'why' }, { coord: '4,5', label: 'how' },
        { coord: '4,6', label: 'outcome' }, { coord: '4,7', label: 'sentiment' },
      ]
    },
    {
      coord: '500', label: 'Tools', color: 5, type: 'scripted', subs: [
        { coord: '5,0', label: 'cognitive' }, { coord: '5,1', label: 'dev' },
      ]
    },
    {
      coord: '600', label: 'Journal', color: 6, type: 'scripted', subs: [
        { coord: '6,0', label: 'deepsleep' },
      ]
    },
    {
      coord: '700', label: 'Vision', color: 7, type: 'scripted', subs: [
        { coord: '7,0', label: 'images' }, { coord: '7,1', label: 'face_embeddings' },
        { coord: '7,2', label: 'scene_descriptions' }, { coord: '7,3', label: 'visual_patterns' },
      ]
    },
    {
      coord: '800', label: 'Coding', color: 8, type: 'setfit', subs: [
        { coord: '8,0', label: 'python_general' }, { coord: '8,1', label: 'python_async' },
        { coord: '8,2', label: 'python_packaging' }, { coord: '8,3', label: 'python_typing' },
        { coord: '8,4', label: 'python_testing' }, { coord: '8,5', label: 'python_web' },
        { coord: '8,6', label: 'python_ml' }, { coord: '8,7', label: 'python_data' },
        { coord: '8,8', label: 'typescript_general' }, { coord: '8,9', label: 'typescript_react' },
        { coord: '8,10', label: 'typescript_node' }, { coord: '8,11', label: 'typescript_types' },
        { coord: '8,12', label: 'rust_general' }, { coord: '8,13', label: 'rust_systems' },
        { coord: '8,14', label: 'rust_async' }, { coord: '8,15', label: 'go_general' },
        { coord: '8,16', label: 'go_concurrency' }, { coord: '8,17', label: 'c_general' },
        { coord: '8,18', label: 'c_embedded' }, { coord: '8,19', label: 'cpp_general' },
        { coord: '8,20', label: 'cpp_templates' }, { coord: '8,21', label: 'cpp_performance' },
        { coord: '8,22', label: 'sql_queries' }, { coord: '8,23', label: 'sql_performance' },
        { coord: '8,24', label: 'sql_modeling' }, { coord: '8,25', label: 'shell_bash' },
        { coord: '8,26', label: 'shell_powershell' }, { coord: '8,27', label: 'html_css' },
        { coord: '8,28', label: 'algorithms' }, { coord: '8,29', label: 'data_structures' },
        { coord: '8,30', label: 'design_patterns' }, { coord: '8,31', label: 'architecture' },
        { coord: '8,32', label: 'concurrency' }, { coord: '8,33', label: 'networking' },
        { coord: '8,34', label: 'protocols_http' }, { coord: '8,35', label: 'protocols_grpc' },
        { coord: '8,36', label: 'databases' }, { coord: '8,37', label: 'caching' },
        { coord: '8,38', label: 'testing' }, { coord: '8,39', label: 'debugging' },
        { coord: '8,40', label: 'devops_docker' }, { coord: '8,41', label: 'devops_k8s' },
        { coord: '8,42', label: 'devops_cicd' }, { coord: '8,43', label: 'security_code' },
        { coord: '8,44', label: 'cryptography_code' }, { coord: '8,45', label: 'performance' },
        { coord: '8,46', label: 'memory_mgmt' }, { coord: '8,47', label: 'file_io' },
        { coord: '8,48', label: 'build_systems' }, { coord: '8,49', label: 'version_control' },
      ]
    },
    {
      coord: '900', label: 'Temporal', color: 9, type: 'scripted', subs: [
        { coord: '9,0', label: 'sessions' }, { coord: '9,1', label: 'events' },
        { coord: '9,2', label: 'milestones' }, { coord: '9,3', label: 'history' },
      ]
    },
    {
      coord: '1000', label: 'Workflows', color: 10, type: 'setfit', subs: [
        { coord: '10,0', label: 'code_authoring' }, { coord: '10,1', label: 'code_debugging' },
        { coord: '10,2', label: 'code_review' }, { coord: '10,3', label: 'code_testing' },
        { coord: '10,4', label: 'code_refactoring' }, { coord: '10,5', label: 'learn_concept' },
        { coord: '10,6', label: 'operator_recorded' }, { coord: '10,7', label: 'tool_chain' },
      ]
    },
    {
      coord: '1100', label: 'Dev', color: 11, type: 'scripted', subs: [
        { coord: '11,0', label: 'artifacts' }, { coord: '11,1', label: 'progress' },
        { coord: '11,2', label: 'context' }, { coord: '11,3', label: 'vision' },
      ]
    },
  ];

  // Build tree nodes
  const tree = {
    id: 'root', label: 'Memory Core', coord: '', color: -1,
    type: 'root', node_kind: 'root', children: [],
  };
  lobes.forEach((l) => {
    const lobeNode = {
      id: 'lobe_' + l.coord, label: l.label, coord: l.coord,
      color: l.color, type: l.type, node_kind: 'lobe', children: [],
    };
    (l.subs || []).forEach((s) => {
      const parts = s.coord.split(',');
      lobeNode.children.push({
        id: 'sub_' + s.coord.replace(',', '_'),
        label: s.label, coord: s.coord, color: l.color, type: l.type,
        node_kind: 'subdomain',
        lobe: Number(parts[0]),
        subdomain: Number(parts[1]),
        children: [], count: 0, entry_count: 0,
      });
    });
    tree.children.push(lobeNode);
  });

  return tree;
}

// ==========================================================================
// UTILITY FUNCTIONS
// ==========================================================================

/** Clamp a value between min and max */
function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

/** Linear interpolation */
function lerp(a, b, t) { return a + (b - a) * t; }

/** Distance between two points */
function dist(x1, y1, x2, y2) {
  const dx = x2 - x1, dy = y2 - y1;
  return Math.sqrt(dx * dx + dy * dy);
}

/** Unique ID generator */
let _uidCounter = 0;
function uid() { return 'mg-' + (++_uidCounter) + '-' + Math.random().toString(36).slice(2, 7); }

/** Check if user prefers reduced motion */
function prefersReducedMotion() {
  return window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

/** Escape HTML for safe insertion */
function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** Format ISO date to readable string */
function formatDate(iso) {
  try {
    const d = new Date(iso);
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch (e) {
    return iso || '';
  }
}
  global.__MG = {
LOBE_COLORS,
PHYSICS,
ANIMATION,
NODE_RADIUS,
ZOOM,
generateMockTree,
clamp,
lerp,
dist,
uid,
prefersReducedMotion,
escapeHtml,
formatDate,
  };
})(typeof window !== 'undefined' ? window : typeof global !== 'undefined' ? global : this);
(function (global) {
  'use strict';
  var MG = global.__MG;
  var LOBE_COLORS = MG.LOBE_COLORS;
  var escapeHtml = MG.escapeHtml;
  var formatDate = MG.formatDate;
  var PHYSICS = MG.PHYSICS;
  var ANIMATION = MG.ANIMATION;
  var NODE_RADIUS = MG.NODE_RADIUS;
  var clamp = MG.clamp;
  var lerp = MG.lerp;
  var dist = MG.dist;
  var uid = MG.uid;
  var prefersReducedMotion = MG.prefersReducedMotion;
// ==========================================================================
// FORCE LAYOUT ENGINE
// =====================================      ====       ====================

/**
 *        -based node positioning engine.
 * Simulates repulsion,        forces, gravity, and collision.
 */
function ForceLayout(width, height) {
  this.width = width || 800;
  this.height = height || 600;
  this.centerX = this.width / 2;
  this.centerY = this.height / 2;
  this.nodes = [];
  this.links = [];
  this.nodeMap = Object.create(null);
  this.stableFrameCount = 0;
  this.isStable = false;
}

/** Update viewport dimensions */
ForceLayout.prototype.setSize = function (w, h) {
  const oldCenterX = this.centerX;
  const oldCenterY = this.centerY;
  this.width = w;
  this.height = h;
  this.centerX = w / 2;
  this.centerY = h / 2;
  const dx = this.centerX - oldCenterX;
  const dy = this.centerY - oldCenterY;
  if (dx === 0 && dy === 0) return;
  this.nodes.forEach(function (node) {
    node.x += dx;
    node.y += dy;
    if (node._fx !== null && node._fx !== undefined) {
      node._fx += dx;
      node._fy += dy;
    }
  });
};

/** Add a node to the simulation */
ForceLayout.prototype.addNode = function (node) {
  if (this.nodeMap[node.id]) return this.nodeMap[node.id];
  node.vx = node.vx || 0;
  node.vy = node.vy || 0;
  node.mass = node.mass || (node.radius || 10);
  node._fx = null;
  node._fy = null;
  this.nodes.push(node);
  this.nodeMap[node.id] = node;
  this.isStable = false;
  this.stableFrameCount = 0;
  return node;
};

/** Add a link (spring) between two nodes */
ForceLayout.prototype.addLink = function (link) {
  const s = typeof link.source === 'string' ? this.nodeMap[link.source] : link.source;
  const t = typeof link.target === 'string' ? this.nodeMap[link.target] : link.target;
  if (!s || !t) return null;
  const existing = this.links.find(function (l) {
    const ls = typeof l.source === 'object' ? l.source.id : l.source;
    const lt = typeof l.target === 'object' ? l.target.id : l.target;
    return ls === s.id && lt === t.id;
  });
  if (existing) {
    existing.length = link.length || existing.length || 100;
    if (link.strength !== undefined) existing.strength = link.strength;
    return existing;
  }
  link.source = s;
  link.target = t;
  link.length = link.length || 100;
  if (link.strength === undefined) {
    link.strength = PHYSICS.SPRING_BASE_STIFFNESS;
  }
  this.links.push(link);
  this.isStable = false;
  this.stableFrameCount = 0;
  return link;
};

/** Remove a node and its connected links */
ForceLayout.prototype.removeNode = function (id) {
  const node = this.nodeMap[id];
  if (!node) return;
  delete this.nodeMap[id];
  this.nodes = this.nodes.filter(function (n) { return n.id !== id; });
  this.links = this.links.filter(function (l) {
    return (typeof l.source === 'object' ? l.source.id : l.source) !== id &&
      (typeof l.target === 'object' ? l.target.id : l.target) !== id;
  });
  this.isStable = false;
  this.stableFrameCount = 0;
};

/** Get a node by ID */
ForceLayout.prototype.getNode = function (id) { return this.nodeMap[id] || null; };

/** Pin a node to a fixed position (null to unpin) */
ForceLayout.prototype.pin = function (id, x, y) {
  const node = this.nodeMap[id];
  if (!node) return;
  if (x === null || x === undefined) {
    node._fx = null;
    node._fy = null;
    node._frozen = false;
    node._freezeCount = 0;
    this.isStable = false;
    this.stableFrameCount = 0;
  } else {
    node._fx = x;
    node._fy = y;
    node.x = x;
    node.y = y;
    node.vx = 0;
    node.vy = 0;
    node._frozen = true;
  }
};

/** Single physics step */
ForceLayout.prototype.step = function () {
  try {
    if (this.isStable) return;

    const nodes = this.nodes;
    const links = this.links;
    const n = nodes.length;
    if (n === 0) return;

    let movingNodes = 0;
    const DAMPING = PHYSICS.DAMPING;
    const PIN_SPEED = 0.01; // speed below which a node freezes

    // 1. Repulsion (Coulomb-like, inverse-square, capped)
    for (let i = 0; i < n; i++) {
      const a = nodes[i];
      if (a._hidden || a._frozen) continue;
      for (let j = i + 1; j < n; j++) {
        const b = nodes[j];
        if (b._hidden || b._frozen) continue;
        const dx = a.x - b.x;
        const dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) d2 = 1;
        const d = Math.sqrt(d2);

        const minDist = (a.radius + b.radius + PHYSICS.COLLISION_PADDING);
        // Only repel when close — avoids jitter from distant tiny forces
        if (d > minDist * 4) continue;

        const repelStrength = PHYSICS.REPULSION_STRENGTH * (a.mass * b.mass) / d2;
        const force = Math.min(repelStrength, 8); // hard cap

        const fx = (dx / d) * force;
        const fy = (dy / d) * force;

        a.vx += fx / a.mass;
        a.vy += fy / a.mass;
        b.vx -= fx / b.mass;
        b.vy -= fy / b.mass;
      }
    }

    // 2. Spring forces (Hooke's law) on links
    for (let i = 0; i < links.length; i++) {
      const link = links[i];
      const a = link.source;
      const b = link.target;
      if (!a || !b || a._hidden || b._hidden || a._frozen || b._frozen) continue;

      const dx = b.x - a.x;
      const dy = b.y - a.y;
      let d = Math.sqrt(dx * dx + dy * dy);
      if (d < 0.1) d = 0.1;

      const displacement = d - link.length;
      // Use critical damping: sqrt(k) * 2 for no oscillation
      const force = displacement * link.strength * 0.6;

      const fx = (dx / d) * force;
      const fy = (dy / d) * force;

      a.vx += fx / a.mass;
      a.vy += fy / a.mass;
      b.vx -= fx / b.mass;
      b.vy -= fy / b.mass;
    }

    // 3. Parent gravity (gentle — only when far from parent)
    for (let i = 0; i < n; i++) {
      const node = nodes[i];
      if (node._hidden || node._fx !== null || node._frozen) continue;
      if (node.parent && this.nodeMap[node.parent]) {
        const p = this.nodeMap[node.parent];
        const dx = p.x - node.x;
        const dy = p.y - node.y;
        const d = Math.sqrt(dx * dx + dy * dy) + 0.1;
        // Weaker pull when close — avoids oscillation
        const pullRange = Math.min(d / 100, 1);
        const force = PHYSICS.PARENT_GRAVITY * d * pullRange;
        node.vx += (dx / d) * force;
        node.vy += (dy / d) * force;
      }
    }

    // 4. Center gravity (very weak — just prevents drift offscreen)
    for (let i = 0; i < n; i++) {
      const node = nodes[i];
      if (node._hidden || node._fx !== null || node._frozen) continue;
      const dx = this.centerX - node.x;
      const dy = this.centerY - node.y;
      // Only apply if far from center
      const dist = Math.sqrt(dx * dx + dy * dy);
      if (dist > 200) {
        const strength = Math.min((dist - 200) / 500, 1) * PHYSICS.CENTER_GRAVITY;
        node.vx += dx * strength;
        node.vy += dy * strength;
      }
    }

    // 5. Collision resolution (positional, not velocity-based)
    for (let i = 0; i < n; i++) {
      const a = nodes[i];
      if (a._hidden) continue;
      for (let j = i + 1; j < n; j++) {
        const b = nodes[j];
        if (b._hidden) continue;
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const d = Math.sqrt(dx * dx + dy * dy);
        const minDist = a.radius + b.radius + PHYSICS.COLLISION_PADDING;
        if (d < minDist && d > 0.1) {
          const overlap = (minDist - d) * 0.5;
          const sx = (dx / d) * overlap;
          const sy = (dy / d) * overlap;
          if (!a._frozen) { a.x -= sx; a.y -= sy; }
          if (!b._frozen) { b.x += sx; b.y += sy; }
        }
      }
    }

    // 6. Soft boundary force
    const margin = PHYSICS.BOUNDARY_MARGIN;
    for (let i = 0; i < n; i++) {
      const node = nodes[i];
      if (node._hidden || node._fx !== null || node._frozen) continue;
      if (node.x < margin) node.vx += (margin - node.x) * PHYSICS.BOUNDARY_STRENGTH;
      if (node.x > this.width - margin) node.vx -= (node.x - (this.width - margin)) * PHYSICS.BOUNDARY_STRENGTH;
      if (node.y < margin) node.vy += (margin - node.y) * PHYSICS.BOUNDARY_STRENGTH;
      if (node.y > this.height - margin) node.vy -= (node.y - (this.height - margin)) * PHYSICS.BOUNDARY_STRENGTH;
    }

    // 7. Apply velocity with strong damping + per-node freezing
    for (let i = 0; i < n; i++) {
      const node = nodes[i];
      if (node._hidden) continue;

      if (node._fx !== null) {
        node.x = node._fx;
        node.y = node._fy;
        node.vx = 0;
        node.vy = 0;
        continue;
      }

      // Clamp velocity
      const speed = Math.sqrt(node.vx * node.vx + node.vy * node.vy);
      if (speed > PHYSICS.MAX_VELOCITY) {
        node.vx = (node.vx / speed) * PHYSICS.MAX_VELOCITY;
        node.vy = (node.vy / speed) * PHYSICS.MAX_VELOCITY;
      }

      node.vx *= DAMPING;
      node.vy *= DAMPING;

      // Freeze nodes that are barely moving (prevents jitter)
      if (speed < PIN_SPEED && !node._frozen) {
        node._freezeCount = (node._freezeCount || 0) + 1;
        if (node._freezeCount > 10) {
          node._frozen = true;
          node.vx = 0;
          node.vy = 0;
        }
      } else if (speed >= PIN_SPEED) {
        node._freezeCount = 0;
        node._frozen = false;
      }

      if (!node._frozen) {
        node.x += node.vx;
        node.y += node.vy;
        movingNodes++;
      }
    }

    // Stability detection: all nodes frozen or very low energy
    if (movingNodes === 0 || this.stableFrameCount >= PHYSICS.STABLE_FRAMES) {
      this.stableFrameCount++;
      if (this.stableFrameCount >= PHYSICS.STABLE_FRAMES) {
        this.isStable = true;
        // Freeze everything
        nodes.forEach(function (n) { n._frozen = true; n.vx = 0; n.vy = 0; });
      }
    } else {
      this.stableFrameCount = 0;
    }
  } catch (err) {
    console.error('[MemoryGraph] Physics step error:', err);
  }
}; ForceLayout.prototype.wake = function () {
  this.isStable = false;
  this.stableFrameCount = 0;
  for (let i = 0; i < this.nodes.length; i++) {
    this.nodes[i]._frozen = false;
    this.nodes[i]._freezeCount = 0;
  }
};

// ==========================================================================
// BLOOM ANIMATOR
// =====================================      ====       ====================

/**
 * Manages bloom (expa        collapse animations for nodes.
 * Uses spring       s with ease-out-back overshoot.
 */
function BloomAnimator() {
  this.animations = [];
  this.reducedMotion = prefersReducedMotion();
}

/** Update reduced motion preference */
BloomAnimator.prototype.checkReducedMotion = function () {
  this.reducedMotion = prefersReducedMotion();
};

/** Bloom a node: animate its children outward from parent */
BloomAnimator.prototype.bloomNode = function (node, layout, onComplete) {
  const children = node.children;
  const parent = layout.getNode(node.id);
  if (!parent) return;
  const dataChildren = parent.data && parent.data.children;
  const childDataList = parent.children || children || dataChildren || [];
  if (!childDataList || childDataList.length === 0) return;

  parent.expanded = true;

  // Calculate radial positions for children
  const count = childDataList.length;
  const parentLevel = parent.level || node.level || 0;
  const baseRadius = (80 + count * 8) * (1 + parentLevel * 0.3);

  childDataList.forEach(function (childData, idx) {
    const angle = (Math.PI * 2 * idx) / count - Math.PI / 2;
    const targetX = parent.x + Math.cos(angle) * baseRadius;
    const targetY = parent.y + Math.sin(angle) * baseRadius;

    const childKind = childData.node_kind || childData.type;
    const radius = NODE_RADIUS[childKind] || NODE_RADIUS[childData.type] || 10;

    let childNode = layout.getNode(childData.id);
    const wasVisible = childNode && !childNode._hidden;
    if (childNode) {
      childNode.radius = radius;
      childNode.mass = radius * 0.5;
      childNode.parent = parent.id;
      childNode.children = childData.children || [];
      childNode.level = parentLevel + 1;
      childNode.data = childData;
      childNode._hidden = false;
      if (childNode._scale === undefined || childNode._scale <= 0) {
        childNode._scale = 1;
      }
      if (childNode._opacity === undefined || childNode._opacity <= 0) {
        childNode._opacity = 1;
      }
    } else {
      childNode = {
        id: childData.id,
        x: parent.x,
        y: parent.y,
        vx: 0,
        vy: 0,
        radius: radius,
        mass: radius * 0.5,
        parent: parent.id,
        children: childData.children || [],
        expanded: false,
        level: parentLevel + 1,
        data: childData,
        _scale: 0,
        _opacity: 0,
        _targetX: targetX,
        _targetY: targetY,
        _fromX: parent.x,
        _fromY: parent.y,
      };
      layout.addNode(childNode);
    }
    layout.addLink({
      source: parent.id,
      target: childData.id,
      length: baseRadius,
      strength: PHYSICS.SPRING_BASE_STIFFNESS + PHYSICS.SPRING_LEVEL_MULTIPLIER * (1 + parentLevel),
    });

    if (wasVisible) {
      childNode._targetX = childNode.x;
      childNode._targetY = childNode.y;
      return;
    }

    // Staggered animation
    const delay = idx * ANIMATION.STAGGER_DELAY;

    if (this.reducedMotion) {
      childNode._scale = 1;
      childNode._opacity = 1;
      childNode.x = targetX;
      childNode.y = targetY;
    } else {
      this.animations.push({
        type: 'bloom',
        node: childNode,
        startTime: performance.now() + delay,
        duration: ANIMATION.BLOOM_DURATION,
        fromX: parent.x,
        fromY: parent.y,
        toX: targetX,
        toY: targetY,
        onComplete: idx === count - 1 ? onComplete : null,
      });
    }
  }, this);

  layout.wake();
};

/** Collapse a node: shrink children toward parent, then remove */
BloomAnimator.prototype.collapseNode = function (node, layout, onComplete) {
  const parent = layout.getNode(node.id);
  if (!parent) return;
  parent.expanded = false;

  // Recursively collapse any expanded children first
  const childNodes = layout.nodes.filter(function (n) { return n.parent === node.id && !n._hidden; });
  if (childNodes.length === 0) {
    if (onComplete) onComplete();
    return;
  }

  let pending = childNodes.length;

  childNodes.forEach(function (child, idx) {
    // Recursively collapse if this child is expanded
    if (child.expanded) {
      this.collapseNode(child.data || { id: child.id }, layout, function () { });
    }

    if (this.reducedMotion) {
      layout.removeNode(child.id);
      pending--;
      if (pending === 0 && onComplete) onComplete();
    } else {
      this.animations.push({
        type: 'collapse',
        node: child,
        startTime: performance.now() + idx * 15,
        duration: ANIMATION.COLLAPSE_DURATION,
        toX: parent.x,
        toY: parent.y,
        onComplete: function () {
          layout.removeNode(child.id);
          pending--;
          if (pending === 0 && onComplete) onComplete();
        },
      });
    }
  }, this);

  layout.wake();
};

/** Update all active animations */
BloomAnimator.prototype.tick = function (now) {
  let hadWork = this.animations.length > 0;
  for (let i = this.animations.length - 1; i >= 0; i--) {
    const anim = this.animations[i];

    if (now < anim.startTime) continue;

    const elapsed = now - anim.startTime;
    let progress = Math.min(elapsed / anim.duration, 1);

    if (anim.type === 'bloom') {
      const eased = ANIMATION.EASE_OUT_BACK(progress);
      anim.node.x = lerp(anim.fromX, anim.toX, Math.min(eased, 1));
      anim.node.y = lerp(anim.fromY, anim.toY, Math.min(eased, 1));
      anim.node._scale = Math.min(eased, 1);
      anim.node._opacity = Math.min(progress * 2, 1);
    } else if (anim.type === 'collapse') {
      const eased = ANIMATION.EASE_IN(progress);
      anim.node.x = lerp(anim.node.x, anim.toX, eased);
      anim.node.y = lerp(anim.node.y, anim.toY, eased);
      anim.node._scale = 1 - progress;
      anim.node._opacity = 1 - progress;
    }

    if (progress >= 1) {
      if (anim.type === 'bloom') {
        anim.node._scale = 1;
        anim.node._opacity = 1;
      }
      if (anim.onComplete) anim.onComplete();
      this.animations.splice(i, 1);
    }
  }
  return hadWork;
};

/** Clear all animations */
BloomAnimator.prototype.clear = function () {
  this.animations = [];
};
function LinkParticles(svgElement, linksLayer) {
  this.svg = svgElement;
  this.linksLayer = linksLayer;
  this.particles = [];
  this.particleGroup = null;
  this.maxParticles = 80;
  this.enabled = true;
}

/** Initialize the particle layer */
LinkParticles.prototype.init = function () {
  this.particleGroup = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  this.particleGroup.setAttribute('class', 'mg-particles');
  var vp = this.linksLayer && this.linksLayer.parentNode;
  if (vp) vp.insertBefore(this.particleGroup, this.linksLayer.nextSibling);
  else this.svg.appendChild(this.particleGroup);
};

/** Set whether particles are enabled */
LinkParticles.prototype.setEnabled = function (v) { this.enabled = v; };

/** Spawn particles along visible links */
LinkParticles.prototype.spawn = function (links, nodeMap) {
  if (!this.enabled || !this.particleGroup) return;

  // Clean up dead particles
  this.particles = this.particles.filter(function (p) { return p.alive; });

  if (this.particles.length >= this.maxParticles) return;

  // Pick random visible links
  const visibleLinks = links.filter(function (l) {
    const s = typeof l.source === 'object' ? l.source : nodeMap[l.source];
    const t = typeof l.target === 'object' ? l.target : nodeMap[l.target];
    return s && t && !s._hidden && !t._hidden && s._opacity > 0.3 && t._opacity > 0.3;
  });

  if (visibleLinks.length === 0) return;

  // Spawn 1-2 particles per tick
  const spawnCount = Math.min(2, this.maxParticles - this.particles.length);
  for (let i = 0; i < spawnCount; i++) {
    const link = visibleLinks[Math.floor(Math.random() * visibleLinks.length)];
    const s = typeof link.source === 'object' ? link.source : nodeMap[link.source];
    const t = typeof link.target === 'object' ? link.target : nodeMap[link.target];
    if (!s || !t) continue;

    const colorIdx = (s.data && s.data.color !== undefined) ? s.data.color : 0;
    const color = LOBE_COLORS[colorIdx] || LOBE_COLORS[0];

    const particle = {
      x: s.x,
      y: s.y,
      sourceX: s.x,
      sourceY: s.y,
      targetX: t.x,
      targetY: t.y,
      progress: 0,
      speed: 0.008 + Math.random() * 0.012,
      radius: 1.5 + Math.random() * 1.5,
      color: color.fill,
      alive: true,
      el: null,
    };

    // Create SVG element
    const el = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    el.setAttribute('r', particle.radius);
    el.setAttribute('fill', particle.color);
    el.setAttribute('opacity', '0');
    this.particleGroup.appendChild(el);
    particle.el = el;

    this.particles.push(particle);
  }
};
/** Update particle positions */
LinkParticles.prototype.tick = function () {
  if (!this.enabled || !this.particleGroup) return false;
  let changed = false;

  for (let i = this.particles.length - 1; i >= 0; i--) {
    const p = this.particles[i];
    p.progress += p.speed;

    if (p.progress >= 1) {
      p.alive = false;
      if (p.el && p.el.parentNode) p.el.parentNode.removeChild(p.el);
      changed = true;
      continue;
    }

    p.x = lerp(p.sourceX, p.targetX, p.progress);
    p.y = lerp(p.sourceY, p.targetY, p.progress);

    let opacity;
    if (p.progress < 0.2) {
      opacity = p.progress / 0.2;
    } else if (p.progress > 0.7) {
      opacity = (1 - p.progress) / 0.3;
    } else {
      opacity = 1;
    }

    if (p.el) {
      p.el.setAttribute('cx', p.x.toFixed(1));
      p.el.setAttribute('cy', p.y.toFixed(1));
      p.el.setAttribute('opacity', (opacity * 0.7).toFixed(2));
    }
    changed = true;
  }
  return changed;
};

/** Clear all particles */
LinkParticles.prototype.clear = function () {
  this.particles.forEach(function (p) {
    if (p.el && p.el.parentNode) p.el.parentNode.removeChild(p.el);
  });
  this.particles = [];
};

/** Destroy the particle system */
LinkParticles.prototype.destroy = function () {
  this.clear();
  if (this.particleGroup && this.particleGroup.parentNode) {
    this.particleGroup.parentNode.removeChild(this.particleGroup);
  }
  this.particleGroup = null;
};

// ==========================================================================
// TOOLTIP MANAGER
// =====================================      ====       ====================

/**
   Floating tooltip on node hover.
 * Shows        type, child count, and preview.
 */
function TooltipManager(container) {
  this.container = container;
  this.el = null;
  this.visible = false;
  this.fadeTimer = null;
  this._onMouseMove = this._onMouseMove.bind(this);
  this._onMouseLeave = this._onMouseLeave.bind(this);
}

/** Initialize tooltip element */
TooltipManager.prototype.init = function () {
  this.el = document.createElement('div');
  this.el.className = 'mg-tooltip';
  this.el.style.cssText = [
    'position:absolute',
    'pointer-events:none',
    'background:rgba(18,18,20,0.92)',
    'border:1px solid rgba(196,133,94,0.22)',
    'border-radius:8px',
    'padding:12px 16px',
    'font-family:var(--mono, JetBrains Mono, ui-monospace, monospace)',
    'font-size:12px',
    'color:#d4cfc7',
    'max-width:280px',
    'box-shadow:0 8px 32px rgba(0,0,0,0.5)',
    'backdrop-filter:blur(16px) saturate(1.15)',
    'z-index:1000',
    'opacity:0',
    'transition:opacity 150ms ease-out',
    'line-height:1.5',
  ].join(';');
  this.container.appendChild(this.el);
};

/** Show tooltip for a node */
TooltipManager.prototype.show = function (node, clientX, clientY) {
  if (!this.el) return;

  const data = node.data || {};
  const children = node.children || (data.children || []);
  const colorIdx = data.color !== undefined ? data.color : (node.color || 0);
  const color = LOBE_COLORS[colorIdx] || LOBE_COLORS[0];

  let html = '<div style="font-weight:600;color:' + color.label + ';margin-bottom:4px;font-size:14px;">' + escapeHtml(data.label || node.label || 'Unknown') + '</div>';
  html += '<div style="color:#8090a0;font-size:11px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px;">' + (data.type || node.type || 'node') + '</div>';

  if (data.coord) {
    html += '<div style="color:#607080;font-size:11px;margin-bottom:4px;font-family:monospace;">' + data.coord + '</div>';
  }

  if (children.length > 0) {
    html += '<div style="color:#8090a0;font-size:11px;">' + children.length + ' child' + (children.length > 1 ? 'ren' : '') + '</div>';
  }

  if (data.strength !== undefined) {
    html += '<div style="margin-top:6px;"><div style="background:rgba(255,255,255,0.08);height:4px;border-radius:2px;overflow:hidden;">' +
      '<div style="background:' + color.fill + ';height:100%;width:' + Math.round(data.strength * 100) + '%;transition:width 300ms;"></div></div>' +
      '<div style="color:#607080;font-size:10px;margin-top:2px;">strength ' + Math.round(data.strength * 100) + '%</div></div>';
  }

  if (data.content) {
    const preview = data.content.substring(0, 120);
    html += '<div style="color:#8090a0;font-size:11px;margin-top:6px;border-top:1px solid rgba(255,255,255,0.08);padding-top:6px;">' + escapeHtml(preview) + (data.content.length > 120 ? '...' : '') + '</div>';
  }

  this.el.innerHTML = html;
  this.el.style.opacity = '1';
  this.visible = true;

  this._position(clientX, clientY);
};

/** Position tooltip near cursor, keeping within viewport */
TooltipManager.prototype._position = function (cx, cy) {
  if (!this.el || !this.container) return;
  const rect = this.container.getBoundingClientRect();
  const x = cx - rect.left + 16;
  const y = cy - rect.top + 16;

  // Keep within container
  const maxX = rect.width - 300;
  const maxY = rect.height - 150;

  this.el.style.left = clamp(x, 8, maxX) + 'px';
  this.el.style.top = clamp(y, 8, maxY) + 'px';
};

/** Hide tooltip */
TooltipManager.prototype.hide = function () {
  if (!this.el) return;
  this.el.style.opacity = '0';
  this.visible = false;
};

/** Mouse move handler */
TooltipManager.prototype._onMouseMove = function (e) {
  if (this.visible) this._position(e.clientX, e.clientY);
};

/** Mouse leave handler */
TooltipManager.prototype._onMouseLeave = function () {
  this.hide();
};

/** Clean up */
TooltipManager.prototype.destroy = function () {
  if (this.el && this.el.parentNode) {
    this.el.parentNode.removeChild(this.el);
  }
  this.el = null;
};

// ==========================================================================
// DETAIL PANEL
// =====================================      ====       ====================

/**
 * Slide-in pan       displaying memory leaf content.
 */
function DetailPanel(container) {
  this.container = container;
  this.el = null;
  this.visible = false;
  this.external = false;
}

/** Initialize the panel */
DetailPanel.prototype.init = function () {
  const external = this.container._memoryDetailTarget || null;
  if (external) {
    this.el = external;
    this.external = true;
    return;
  }
  this.el = document.createElement('div');
  this.el.className = 'mg-detail-panel';
  this.el.style.cssText = [
    'position:absolute',
    'top:0',
    'right:0',
    'width:380px',
    'height:100%',
    'background:rgba(12,16,24,0.97)',
    'border-left:1px solid rgba(100,140,180,0.2)',
    'transform:translateX(100%)',
    'transition:transform 350ms cubic-bezier(0.22,1,0.36,1)',
    'overflow-y:auto',
    'z-index:900',
    'box-shadow:-8px 0 32px rgba(0,0,0,0.4)',
    'font-family:system-ui,-apple-system,sans-serif',
  ].join(';');
  this.container.appendChild(this.el);
};

/** Show detail panel with memory data */
DetailPanel.prototype.show = function (memoryData) {
  if (!this.el) return;
  this.selectedMemoryData = memoryData;
  const colorIdx = memoryData.color !== undefined ? memoryData.color : 0;
  const color = LOBE_COLORS[colorIdx] || LOBE_COLORS[0];

  let html = '<div style="padding:24px;">';

  // Close button
  html += '<button class="mg-panel-close" style="float:right;background:none;border:none;color:#8090a0;font-size:20px;cursor:pointer;padding:4px;line-height:1;">&times;</button>';

  // Header
  html += '<div style="color:' + color.label + ';font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-bottom:8px;">' + (memoryData.type || 'memory') + '</div>';
  html += '<h2 style="color:#e0e8f0;font-size:20px;font-weight:600;margin:0 0 12px 0;line-height:1.3;">' + escapeHtml(memoryData.label || 'Untitled') + '</h2>';

  // Meta
  html += '<div style="display:flex;gap:16px;margin-bottom:16px;flex-wrap:wrap;">';
  if (memoryData.coord) {
    html += '<div style="background:rgba(255,255,255,0.05);padding:4px 10px;border-radius:4px;font-family:monospace;font-size:11px;color:#8090a0;">' + escapeHtml(memoryData.coord) + '</div>';
  }
  if (memoryData.timestamp) {
    html += '<div style="background:rgba(255,255,255,0.05);padding:4px 10px;border-radius:4px;font-size:11px;color:#607080;">' + formatDate(memoryData.timestamp) + '</div>';
  }
  if (memoryData.size !== undefined) {
    html += '<div style="background:rgba(255,255,255,0.05);padding:4px 10px;border-radius:4px;font-size:11px;color:#607080;">' + memoryData.size + ' bytes</div>';
  }
  html += '</div>';

  // Strength bar
  if (memoryData.strength !== undefined) {
    html += '<div style="margin-bottom:20px;">';
    html += '<div style="display:flex;justify-content:space-between;margin-bottom:4px;">';
    html += '<span style="color:#8090a0;font-size:11px;">Memory Strength</span>';
    html += '<span style="color:' + color.label + ';font-size:11px;font-weight:600;">' + Math.round(memoryData.strength * 100) + '%</span>';
    html += '</div>';
    html += '<div style="background:rgba(255,255,255,0.06);height:6px;border-radius:3px;overflow:hidden;">';
    html += '<div style="background:' + color.fill + ';height:100%;width:' + Math.round(memoryData.strength * 100) + '%;border-radius:3px;transition:width 500ms;"></div>';
    html += '</div></div>';
  }

  // Content
  if (memoryData.content) {
    html += '<pre style="margin:0;color:#b0c0d0;font-size:12px;line-height:1.55;white-space:pre-wrap;font-family:var(--mono, monospace);">' + escapeHtml(memoryData.content) + '</pre>';
  } else {
    html += '<div style="color:#607080;font-size:13px;line-height:1.6;">No saved content loaded for this node.</div>';
  }

  // Footer
  html += '<div style="margin-top:24px;padding-top:14px;border-top:1px solid rgba(255,255,255,0.06);color:#506070;font-size:11px;font-family:var(--mono, monospace);">Memory ID: ' + escapeHtml(memoryData.memory_id || memoryData.id || '') + '</div>';

  html += '</div>';

  this.el.innerHTML = html;
  if (!this.external) {
    this.el.style.transform = 'translateX(0)';
  }
  this.visible = true;

  // Close handler
  const closeBtn = this.el.querySelector('.mg-panel-close');
  if (closeBtn) {
    closeBtn.addEventListener('click', this.hide.bind(this));
  }
};

/** Hide the panel */
DetailPanel.prototype.hide = function () {
  if (!this.el) return;
  if (this.external) {
    this.el.innerHTML = '<div style="padding:16px;color:rgba(212,207,199,0.58);font-family:var(--mono, monospace);font-size:12px;">Select a terminal memory node to read the saved memory file.</div>';
  } else {
    this.el.style.transform = 'translateX(100%)';
  }
  this.visible = false;
  this.selectedMemoryData = null;
};

/** Clean up */
DetailPanel.prototype.destroy = function () {
  if (this.external) {
    this.hide();
  } else if (this.el && this.el.parentNode) {
    this.el.parentNode.removeChild(this.el);
  }
  this.el = null;
};
  Object.assign(global.__MG, {
ForceLayout,
BloomAnimator,
LinkParticles,
TooltipManager,
DetailPanel,
  });
})(typeof window !== 'undefined' ? window : typeof global !== 'undefined' ? global : this);
(function (global) {
  'use strict';
  var MG = global.__MG;
  var LOBE_COLORS = MG.LOBE_COLORS;
  var PHYSICS = MG.PHYSICS;
  var ANIMATION = MG.ANIMATION;
  var NODE_RADIUS = MG.NODE_RADIUS;
  var ZOOM = MG.ZOOM;
  var generateMockTree = MG.generateMockTree;
  var clamp = MG.clamp;
  var lerp = MG.lerp;
  var dist = MG.dist;
  var uid = MG.uid;
  var prefersReducedMotion = MG.prefersReducedMotion;
  var ForceLayout = MG.ForceLayout;
  var BloomAnimator = MG.BloomAnimator;
  var LinkParticles = MG.LinkParticles;
  var TooltipManager = MG.TooltipManager;
  var DetailPanel = MG.DetailPanel;
// ==========================================================================
// SVG RENDERER
// =====================================      ====       ====================

/**
        es and manages the SVG element.
 * Handles rend       zoom, pan, hover, click events.
 */
function SVGRenderer(container, layout, animator) {
  this.container = container;
  this.layout = layout;
  this.animator = animator;

  this.svg = null;
  this.viewport = null;
  this.linksLayer = null;
  this.nodesLayer = null;
  this.labelsLayer = null;

  this.scale = 1;
  this.panX = 0;
  this.panY = 0;

  this.isDragging = false;
  this.dragNode = null;
  this.dragStart = null;
  this.lastMouse = null;
  this._nodeClickHandled = false;

  this.particles = null;
  this.tooltip = null;
  this.detailPanel = null;

  this.searchQuery = '';
  this.searchMatches = new Set();

  this._renderFrame = null;
  this._animationLoop = null;

  // Bind handlers
  this._onWheel = this._onWheel.bind(this);
  this._onMouseDown = this._onMouseDown.bind(this);
  this._onMouseMove = this._onMouseMove.bind(this);
  this._onMouseUp = this._onMouseUp.bind(this);
  this._onDblClick = this._onDblClick.bind(this);
  this._onClick = this._onClick.bind(this);
  this._onTouchStart = this._onTouchStart.bind(this);
  this._onTouchMove = this._onTouchMove.bind(this);
  this._onTouchEnd = this._onTouchEnd.bind(this);
}

/** Create SVG structure and set up event listeners */
SVGRenderer.prototype.init = function () {
  // Create SVG element
  this.svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  this.svg.setAttribute('class', 'memory-graph-svg');
  this.svg.style.cssText = [
    'width:100%',
    'height:100%',
    'display:block',
    'cursor:grab',
    'background:transparent',
  ].join(';');

  // ViewBox
  this.svg.setAttribute('viewBox', '0 0 ' + this.layout.width + ' ' + this.layout.height);
  this.svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

  // Defs (filters and gradients)
  this._createDefs();

  // Viewport group (zoom/pan transform)
  this.viewport = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  this.viewport.setAttribute('class', 'mg-viewport');
  this.svg.appendChild(this.viewport);

  // Links layer
  this.linksLayer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  this.linksLayer.setAttribute('class', 'mg-links-layer');
  this.viewport.appendChild(this.linksLayer);

  // Particles layer placeholder (particles init creates its own)
  // Nodes layer
  this.nodesLayer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  this.nodesLayer.setAttribute('class', 'mg-nodes-layer');
  this.viewport.appendChild(this.nodesLayer);

  // Labels layer
  this.labelsLayer = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  this.labelsLayer.setAttribute('class', 'mg-labels-layer');
  this.viewport.appendChild(this.labelsLayer);

  // Append to container
  this.container.appendChild(this.svg);

  // Initialize subsystems
  this.particles = new LinkParticles(this.svg, this.linksLayer);
  this.particles.init();

  this.tooltip = new TooltipManager(this.container);
  this.tooltip.init();

  this.detailPanel = new DetailPanel(this.container);
  this.detailPanel.init();

  // Event listeners
  this.svg.addEventListener('wheel', this._onWheel, { passive: false });
  this.svg.addEventListener('mousedown', this._onMouseDown);
  document.addEventListener('mousemove', this._onMouseMove);
  document.addEventListener('mouseup', this._onMouseUp);
  this.svg.addEventListener('dblclick', this._onDblClick);
  this.svg.addEventListener('click', this._onClick);
  this.svg.addEventListener('touchstart', this._onTouchStart, { passive: false });
  this.svg.addEventListener('touchmove', this._onTouchMove, { passive: false });
  this.svg.addEventListener('touchend', this._onTouchEnd);

  // Start animation loops
  this._startAnimationLoop();

  // Initial fit
  this.fitView();
};

/** Create SVG defs (filters, gradients) */
SVGRenderer.prototype._createDefs = function () {
  const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');

  // Glow filter for each lobe color
  LOBE_COLORS.forEach(function (col, idx) {
    const filter = document.createElementNS('http://www.w3.org/2000/svg', 'filter');
    filter.setAttribute('id', 'mg-glow-' + idx);
    filter.setAttribute('x', '-50%');
    filter.setAttribute('y', '-50%');
    filter.setAttribute('width', '200%');
    filter.setAttribute('height', '200%');

    const blur = document.createElementNS('http://www.w3.org/2000/svg', 'feGaussianBlur');
    blur.setAttribute('stdDeviation', '6');
    blur.setAttribute('result', 'blur');
    filter.appendChild(blur);

    const merge = document.createElementNS('http://www.w3.org/2000/svg', 'feMerge');
    const mn1 = document.createElementNS('http://www.w3.org/2000/svg', 'feMergeNode');
    mn1.setAttribute('in', 'blur');
    const mn2 = document.createElementNS('http://www.w3.org/2000/svg', 'feMergeNode');
    mn2.setAttribute('in', 'SourceGraphic');
    merge.appendChild(mn1);
    merge.appendChild(mn2);
    filter.appendChild(merge);

    defs.appendChild(filter);
  });

  // Gradient for root node
  const rootGrad = document.createElementNS('http://www.w3.org/2000/svg', 'radialGradient');
  rootGrad.setAttribute('id', 'mg-root-grad');
  const rStop1 = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
  rStop1.setAttribute('offset', '0%');
  rStop1.setAttribute('stop-color', '#f1e9d6');
  rootGrad.appendChild(rStop1);
  const rStop2 = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
  rStop2.setAttribute('offset', '100%');
  rStop2.setAttribute('stop-color', '#c4855e');
  rootGrad.appendChild(rStop2);
  defs.appendChild(rootGrad);

  // Link gradient
  const linkGrad = document.createElementNS('http://www.w3.org/2000/svg', 'linearGradient');
  linkGrad.setAttribute('id', 'mg-link-grad');
  linkGrad.setAttribute('x1', '0%');
  linkGrad.setAttribute('y1', '0%');
  linkGrad.setAttribute('x2', '100%');
  linkGrad.setAttribute('y2', '0%');
  const ls1 = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
  ls1.setAttribute('offset', '0%');
  ls1.setAttribute('stop-color', 'rgba(196,133,94,0.28)');
  linkGrad.appendChild(ls1);
  const ls2 = document.createElementNS('http://www.w3.org/2000/svg', 'stop');
  ls2.setAttribute('offset', '100%');
  ls2.setAttribute('stop-color', 'rgba(232,228,216,0.05)');
  linkGrad.appendChild(ls2);
  defs.appendChild(linkGrad);

  this.svg.appendChild(defs);
};

// ---- Zoom & Pan ----

/** Zoom by a delta amount, centered on given SVG coordinates */
SVGRenderer.prototype.zoom = function (delta, cx, cy) {
  const oldScale = this.scale;
  this.scale = clamp(this.scale + delta, ZOOM.MIN, ZOOM.MAX);

  if (cx !== undefined && cy !== undefined) {
    // Zoom toward cursor
    this.panX = cx - (cx - this.panX) * (this.scale / oldScale);
    this.panY = cy - (cy - this.panY) * (this.scale / oldScale);
  }

  this._updateTransform();
};

/** Pan by a delta */
SVGRenderer.prototype.pan = function (dx, dy) {
  this.panX += dx;
  this.panY += dy;
  this._updateTransform();
};

/** Set zoom and pan to fit all nodes in view */
SVGRenderer.prototype.fitView = function () {
  const nodes = this.layout.nodes;
  if (nodes.length === 0) return;

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  nodes.forEach(function (n) {
    if (n._hidden) return;
    const r = n.radius + 20;
    minX = Math.min(minX, n.x - r);
    minY = Math.min(minY, n.y - r);
    maxX = Math.max(maxX, n.x + r);
    maxY = Math.max(maxY, n.y + r);
  });

  if (minX === Infinity) return;

  const contentW = maxX - minX;
  const contentH = maxY - minY;
  const viewW = this.layout.width;
  const viewH = this.layout.height;

  const scaleX = viewW / contentW;
  const scaleY = viewH / contentH;
  this.scale = Math.min(scaleX, scaleY, ZOOM.MAX) * 0.85;

  this.panX = (viewW - contentW * this.scale) / 2 - minX * this.scale;
  this.panY = (viewH - contentH * this.scale) / 2 - minY * this.scale;

  this._updateTransform();
};

/** Update the viewport transform */
SVGRenderer.prototype._updateTransform = function () {
  if (this.viewport) {
    this.viewport.setAttribute('transform', 'translate(' + this.panX + ',' + this.panY + ') scale(' + this.scale + ')');
  }
};

/** Convert client coordinates to SVG coordinates */
SVGRenderer.prototype._clientToSvg = function (cx, cy) {
  const rect = this.svg.getBoundingClientRect();
  return {
    x: (cx - rect.left - this.panX) / this.scale,
    y: (cy - rect.top - this.panY) / this.scale,
  };
};

// ---- Event Handlers ----

SVGRenderer.prototype._onWheel = function (e) {
  e.preventDefault();
  const pt = this._clientToSvg(e.clientX, e.clientY);
  const delta = -e.deltaY * ZOOM.WHEEL_SENSITIVITY * this.scale;
  this.zoom(delta, pt.x, pt.y);
};

SVGRenderer.prototype._onMouseDown = function (e) {
  if (e.button !== 0) return; // Only left click
  const target = e.target.closest('.mg-node, .mg-label');

  if (target) {
    const nodeId = (
      target.getAttribute('data-node-id')
      || target.getAttribute('data-label-id')
    );
    this.dragNode = this.layout.getNode(nodeId);
    this.isDragging = false;
    this.lastMouse = { x: e.clientX, y: e.clientY };
  } else {
    // Background pan
    this.isDragging = true;
    this.dragStart = { x: e.clientX, y: e.clientY };
    this.svg.style.cursor = 'grabbing';
  }
};

SVGRenderer.prototype._onMouseMove = function (e) {
  // Update tooltip position
  if (this.tooltip && this.tooltip.visible) {
    this.tooltip._position(e.clientX, e.clientY);
  }

  if (this.dragNode) {
    const dx = e.clientX - this.lastMouse.x;
    const dy = e.clientY - this.lastMouse.y;
    if (Math.abs(dx) > 3 || Math.abs(dy) > 3) {
      this.isDragging = true;
      this.layout.pin(this.dragNode.id, null, null); // unpin for drag
      this.dragNode.x += dx / this.scale;
      this.dragNode.y += dy / this.scale;
      this.layout.wake();
      this.lastMouse = { x: e.clientX, y: e.clientY };
    }
  } else if (this.isDragging) {
    const dx = e.clientX - this.dragStart.x;
    const dy = e.clientY - this.dragStart.y;
    this.panX += dx;
    this.panY += dy;
    this.dragStart = { x: e.clientX, y: e.clientY };
    this._updateTransform();
  }
};

SVGRenderer.prototype._onMouseUp = function (e) {
  const wasDragging = this.isDragging;
  const clickedNode = this.dragNode;
  this.svg.style.cursor = 'grab';
  this.dragNode = null;
  this.isDragging = false;
  this.dragStart = null;
  if (clickedNode && !wasDragging) {
    this._nodeClickHandled = true;
    this._activateNode(clickedNode);
  }
};

SVGRenderer.prototype._onDblClick = function (e) {
  this.fitView();
};

SVGRenderer.prototype._activateNode = function (node) {
  const data = node.data || {};

  if (data.node_kind === 'file' || data.type === 'memory') {
    this._showMemoryFile(Object.assign({}, data, { id: node.id }));
    return;
  }

  if (node.expanded) {
    this.animator.collapseNode(node, this.layout);
  } else {
    this.animator.bloomNode(node, this.layout);
  }
};

SVGRenderer.prototype._onClick = function (e) {
  if (this._nodeClickHandled) {
    this._nodeClickHandled = false;
    return;
  }

  // Label clicks (and fallback when mouseup did not run first)
  const nodeEl = e.target.closest('.mg-node, .mg-label');
  if (!nodeEl) return;

  const nodeId = (
    nodeEl.getAttribute('data-node-id')
    || nodeEl.getAttribute('data-label-id')
  );
  const node = this.layout.getNode(nodeId);
  if (!node) return;

  this._activateNode(node);
};

SVGRenderer.prototype._showMemoryFile = async function (data) {
  if (!this.detailPanel) return;
  const loading = Object.assign({}, data, { content: 'Loading saved memory…' });
  this.detailPanel.show(loading);
  if (data.content) return;
  const readPath = data.read_path || data.path || '';
  const subdomainDir = data.subdomain_dir || data.subdomainDir || '';
  let lobe = data.lobe;
  let subdomain = data.subdomain;
  if ((lobe === undefined || subdomain === undefined) && data.coord) {
    const m = String(data.coord).match(/^(\d+),(\d+)/);
    if (m) {
      lobe = m[1];
      subdomain = m[2];
    }
  }
  if (!readPath || lobe === undefined || subdomain === undefined) {
    this.detailPanel.show(Object.assign({}, data, {
      content: 'Unable to read saved memory: missing file path metadata.',
    }));
    return;
  }
  try {
    const params = new URLSearchParams({
      lobe: String(lobe),
      subdomain: String(subdomain),
      path: String(readPath),
    });
    if (subdomainDir) params.set('subdomain_dir', String(subdomainDir));
    const r = await fetch('/api/memory/read?' + params.toString(), { cache: 'no-store' });
    if (!r.ok) throw new Error('read failed: ' + r.status);
    const content = await r.text();
    this.detailPanel.show(Object.assign({}, data, { content: content }));
  } catch (err) {
    this.detailPanel.show(Object.assign({}, data, {
      content: 'Unable to read saved memory: ' + (err && err.message ? err.message : err),
    }));
  }
};

// Touch support
SVGRenderer.prototype._onTouchStart = function (e) {
  if (e.touches.length === 1) {
    const t = e.touches[0];
    const target = document.elementFromPoint(t.clientX, t.clientY);
    const nodeEl = target && target.closest('.mg-node');

    if (nodeEl) {
      const nodeId = nodeEl.getAttribute('data-node-id');
      this.dragNode = this.layout.getNode(nodeId);
      this.isDragging = false;
    } else {
      this.isDragging = true;
    }
    this.lastMouse = { x: t.clientX, y: t.clientY };
  } else if (e.touches.length === 2) {
    this._lastPinchDist = this._pinchDist(e);
  }
};

SVGRenderer.prototype._onTouchMove = function (e) {
  e.preventDefault();
  if (e.touches.length === 1 && this.lastMouse) {
    const t = e.touches[0];
    const dx = t.clientX - this.lastMouse.x;
    const dy = t.clientY - this.lastMouse.y;

    if (this.dragNode) {
      this.dragNode.x += dx / this.scale;
      this.dragNode.y += dy / this.scale;
      this.layout.wake();
    } else if (this.isDragging) {
      this.panX += dx;
      this.panY += dy;
      this._updateTransform();
    }
    this.lastMouse = { x: t.clientX, y: t.clientY };
  } else if (e.touches.length === 2 && this._lastPinchDist) {
    const d = this._pinchDist(e);
    const delta = (d - this._lastPinchDist) * 0.005 * this.scale;
    const cx = (e.touches[0].clientX + e.touches[1].clientX) / 2;
    const cy = (e.touches[0].clientY + e.touches[1].clientY) / 2;
    const pt = this._clientToSvg(cx, cy);
    this.zoom(delta, pt.x, pt.y);
    this._lastPinchDist = d;
  }
};

SVGRenderer.prototype._onTouchEnd = function (e) {
  if (e.touches.length === 0) {
    this.dragNode = null;
    this.isDragging = false;
    this.lastMouse = null;
    this._lastPinchDist = null;
  }
};

SVGRenderer.prototype._pinchDist = function (e) {
  const dx = e.touches[0].clientX - e.touches[1].clientX;
  const dy = e.touches[0].clientY - e.touches[1].clientY;
  return Math.sqrt(dx * dx + dy * dy);
};

// ---- Search ----

/** Set search query and update highlighting */
SVGRenderer.prototype.setSearch = function (query) {
  this.searchQuery = (query || '').toLowerCase().trim();
  this.searchMatches.clear();

  if (!this.searchQuery) {
    this._render();
    return 0;
  }

  // Find matching nodes
  this.layout.nodes.forEach(function (node) {
    const data = node.data || {};
    const label = (data.label || node.label || '').toLowerCase();
    const content = (data.content || '').toLowerCase();
    const coord = (data.coord || '').toLowerCase();

    if (label.indexOf(this.searchQuery) !== -1 ||
      content.indexOf(this.searchQuery) !== -1 ||
      coord.indexOf(this.searchQuery) !== -1) {
      this.searchMatches.add(node.id);
    }
  }, this);

  this._render();
  return this.searchMatches.size;
};

MG.SVGRenderer = SVGRenderer;
})(typeof window !== 'undefined' ? window : typeof global !== 'undefined' ? global : this);
(function (global) {
  'use strict';
  var MG = global.__MG;
  var LOBE_COLORS = MG.LOBE_COLORS;
  var PHYSICS = MG.PHYSICS;
  var ANIMATION = MG.ANIMATION;
  var NODE_RADIUS = MG.NODE_RADIUS;
  var ZOOM = MG.ZOOM;
  var generateMockTree = MG.generateMockTree;
  var clamp = MG.clamp;
  var lerp = MG.lerp;
  var dist = MG.dist;
  var uid = MG.uid;
  var prefersReducedMotion = MG.prefersReducedMotion;
  var ForceLayout = MG.ForceLayout;
  var BloomAnimator = MG.BloomAnimator;
  var LinkParticles = MG.LinkParticles;
  var TooltipManager = MG.TooltipManager;
  var DetailPanel = MG.DetailPanel;
  var SVGRenderer = MG.SVGRenderer;
// ---- Rendering ----

/** Main render: sync SVG elements to layout state */
SVGRenderer.prototype._render = function () {
  try {
    this._renderLinks();
    this._renderNodes();
    this._renderLabels();
  } catch (err) {
    console.error('[MemoryGraph] Render error:', err);
  }
};

/** Render/update link elements */
SVGRenderer.prototype._renderLinks = function () {
  const links = this.layout.links;
  const existing = this.linksLayer.querySelectorAll('.mg-link');
  const elMap = Object.create(null);
  existing.forEach(function (el) { elMap[el.getAttribute('data-link-id')] = el; });

  links.forEach(function (link, idx) {
    const s = typeof link.source === 'object' ? link.source : this.layout.nodeMap[link.source];
    const t = typeof link.target === 'object' ? link.target : this.layout.nodeMap[link.target];
    if (!s || !t || s._hidden || t._hidden) return;

    const linkId = (s.id + '-' + t.id);
    let el = elMap[linkId];

    if (!el) {
      el = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      el.setAttribute('class', 'mg-link');
      el.setAttribute('data-link-id', linkId);
      el.setAttribute('stroke', 'rgba(232,228,216,0.08)');
      el.setAttribute('stroke-width', '1');
      this.linksLayer.appendChild(el);
    }

    // Opacity based on search
    let opacity = Math.min((s._opacity || 1), (t._opacity || 1));
    if (this.searchQuery && this.searchMatches.size > 0) {
      const sMatch = this.searchMatches.has(s.id);
      const tMatch = this.searchMatches.has(t.id);
      if (!sMatch && !tMatch) opacity *= 0.15;
    }

    el.setAttribute('x1', s.x.toFixed(1));
    el.setAttribute('y1', s.y.toFixed(1));
    el.setAttribute('x2', t.x.toFixed(1));
    el.setAttribute('y2', t.y.toFixed(1));
    el.setAttribute('opacity', opacity.toFixed(2));
    elMap[linkId] = null; // Mark as used
  }, this);

  // Remove unused
  Object.keys(elMap).forEach(function (id) {
    if (elMap[id] && elMap[id].parentNode) {
      elMap[id].parentNode.removeChild(elMap[id]);
    }
  });
};

/** Render/update node elements */
SVGRenderer.prototype._renderNodes = function () {
  const nodes = this.layout.nodes;
  const existing = this.nodesLayer.querySelectorAll('.mg-node');
  const elMap = Object.create(null);
  existing.forEach(function (el) { elMap[el.getAttribute('data-node-id')] = el; });

  nodes.forEach(function (node) {
    if (node._hidden) {
      if (elMap[node.id] && elMap[node.id].parentNode) {
        elMap[node.id].parentNode.removeChild(elMap[node.id]);
      }
      return;
    }

    const data = node.data || {};
    const colorIdx = data.color !== undefined ? data.color : (node.color || 0);
    const color = LOBE_COLORS[colorIdx] || LOBE_COLORS[0];
    const isMatch = this.searchMatches.has(node.id);
    const hasSearch = this.searchQuery && this.searchMatches.size > 0;

    let el = elMap[node.id];
    if (!el) {
      el = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      el.setAttribute('data-node-id', node.id);
      el.style.cursor = 'pointer';
      el.style.transition = 'filter 200ms ease';

      // Hover events
      el.addEventListener('mouseenter', function (e) {
        const nid = el.getAttribute('data-node-id');
        const n = this.layout.getNode(nid);
        if (n) {
          el.setAttribute('filter', 'url(#mg-glow-' + colorIdx + ')');
          this.tooltip.show(n, e.clientX, e.clientY);
        }
      }.bind(this));

      el.addEventListener('mouseleave', function () {
        el.removeAttribute('filter');
        this.tooltip.hide();
      }.bind(this));

      this.nodesLayer.appendChild(el);
    }

    const scale = node._scale !== undefined ? node._scale : 1;
    const baseOpacity = node._opacity !== undefined ? node._opacity : 1;

    // Calculate visual radius
    const r = (node.radius || 10) * scale;
    const nodeKind = data.node_kind || data.type || node.type || '';
    const hasChildren = (node.children || (data.children || [])).length > 0;
    const kindClass = nodeKind === 'root'
      ? 'mg-node-root'
      : (nodeKind === 'lobe' ? 'mg-node-lobe'
        : (nodeKind === 'file' || data.type === 'memory' ? 'mg-node-memory'
          : (nodeKind === 'subdomain' || nodeKind === 'directory' ? 'mg-node-subdomain'
            : 'mg-node-lobe')));
    el.setAttribute('class', 'mg-node ' + kindClass + (hasChildren && !node.expanded ? ' mg-node-collapsed' : ''));
    el.setAttribute('data-lobe', String(colorIdx));

    // Search highlight
    let opacity = baseOpacity;
    let fill = (data.type === 'root' || nodeKind === 'root') ? 'url(#mg-root-grad)' : color.fill;
    let stroke = color.label;

    if (hasSearch) {
      if (isMatch) {
        opacity = Math.min(baseOpacity * 1.2, 1);
        el.setAttribute('filter', 'url(#mg-glow-' + colorIdx + ')');
      } else {
        opacity = baseOpacity * 0.12;
      }
    }

    el.setAttribute('cx', node.x.toFixed(1));
    el.setAttribute('cy', node.y.toFixed(1));
    el.setAttribute('r', Math.max(r, 0).toFixed(1));
    el.setAttribute('fill', fill);
    el.setAttribute('stroke', stroke);
    el.setAttribute('stroke-width', (isMatch && hasSearch ? '2.5' : (hasChildren && !node.expanded ? '2' : '1.5')));
    if (hasChildren && !node.expanded) {
      el.setAttribute('stroke-dasharray', '3 3');
    } else {
      el.removeAttribute('stroke-dasharray');
    }
    el.setAttribute('opacity', opacity.toFixed(2));

    elMap[node.id] = null;
  }, this);

  // Remove unused
  Object.keys(elMap).forEach(function (id) {
    if (elMap[id] && elMap[id].parentNode) {
      elMap[id].parentNode.removeChild(elMap[id]);
    }
  });
};

/** Render/update label elements */
SVGRenderer.prototype._renderLabels = function () {
  const nodes = this.layout.nodes;
  const existing = this.labelsLayer.querySelectorAll('.mg-label');
  const elMap = Object.create(null);
  existing.forEach(function (el) { elMap[el.getAttribute('data-label-id')] = el; });

  const showLabels = this.scale > 0.5;

  nodes.forEach(function (node) {
    if (node._hidden || node._scale < 0.5) {
      if (elMap[node.id] && elMap[node.id].parentNode) {
        elMap[node.id].parentNode.removeChild(elMap[node.id]);
      }
      return;
    }

    const data = node.data || {};
    const colorIdx = data.color !== undefined ? data.color : (node.color || 0);
    const color = LOBE_COLORS[colorIdx] || LOBE_COLORS[0];
    const nodeKind = data.node_kind || data.type || node.type || '';
    const isRoot = data.type === 'root' || nodeKind === 'root';
    const isLobe = data.type === 'lobe' || nodeKind === 'lobe';
    const isMemory = data.type === 'memory' || nodeKind === 'file';

    // Only show labels for expanded nodes or root/lobe types when zoomed
    const shouldShow = isRoot || isLobe ||
      (node.expanded && showLabels) ||
      this.searchMatches.has(node.id);

    if (!shouldShow) {
      if (elMap[node.id] && elMap[node.id].parentNode) {
        elMap[node.id].parentNode.removeChild(elMap[node.id]);
      }
      return;
    }

    let el = elMap[node.id];
    if (!el) {
      el = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      el.setAttribute('class', 'mg-label');
      el.setAttribute('data-label-id', node.id);
      el.setAttribute('data-node-id', node.id);
      el.setAttribute('text-anchor', 'middle');
      el.setAttribute('dominant-baseline', 'middle');
      el.style.pointerEvents = 'auto';
      el.style.cursor = 'pointer';
      el.style.userSelect = 'none';
      this.labelsLayer.appendChild(el);
    }

    const fontSize = isRoot ? 13 : (isLobe ? 11 : 9);
    const yOffset = (node.radius || 10) + fontSize + 4;

    el.setAttribute('x', node.x.toFixed(1));
    el.setAttribute('y', (node.y + yOffset).toFixed(1));
    el.setAttribute('fill', color.label);
    el.setAttribute('font-size', fontSize);
    el.setAttribute('font-weight', isMemory ? '400' : '600');
    el.setAttribute('opacity', (node._opacity || 1).toFixed(2));

    // Truncate label
    let label = data.label || node.label || '';
    if (label.length > 28 && data.type === 'memory') {
      label = label.substring(0, 25) + '...';
    }
    el.textContent = label;

    elMap[node.id] = null;
  }, this);

  // Remove unused
  Object.keys(elMap).forEach(function (id) {
    if (elMap[id] && elMap[id].parentNode) {
      elMap[id].parentNode.removeChild(elMap[id]);
    }
  });
};

// ---- Animation Loop ----

/** Start the render animation loop */
SVGRenderer.prototype._startAnimationLoop = function () {
  const loop = function (now) {
    try {
      let needsRender = false;

      // Run physics only if not stable
      if (!this.layout.isStable) {
        this.layout.step();
        needsRender = true;
      }

      // Run bloom animations
      const animating = this.animator.tick(now);
      if (animating) needsRender = true;

      // Spawn and update particles (visual only, always run)
      this.particles.spawn(this.layout.links, this.layout.nodeMap);
      const particlesChanged = this.particles.tick();
      if (particlesChanged) needsRender = true;

      // Render SVG only when something changed
      // When stable, render every 3rd frame (20fps) for particle motion
      if (needsRender || !this._skipCounter || this._skipCounter-- <= 0) {
        this._render();
        this._skipCounter = this.layout.isStable ? 2 : 0;
      }
    } catch (err) {
      console.error('[MemoryGraph] Animation loop error:', err);
    }
    this._animationLoop = requestAnimationFrame(loop.bind(this));
  }.bind(this);

  this._animationLoop = requestAnimationFrame(loop);
};

/** Stop the animation loop */
SVGRenderer.prototype._stopAnimationLoop = function () {
  if (this._animationLoop) {
    cancelAnimationFrame(this._animationLoop);
    this._animationLoop = null;
  }
};

// ---- Resize ----

SVGRenderer.prototype.resize = function (wakePhysics) {
  const rect = this.container.getBoundingClientRect();
  const w = rect.width || this.layout.width;
  const h = rect.height || this.layout.height;
  const sizeChanged = w !== this.layout.width || h !== this.layout.height;
  this.layout.setSize(w, h);
  this.svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
  if (wakePhysics !== false && sizeChanged && !this.layout.isStable) {
    this.layout.wake();
  }
};

// ---- Destroy ----

SVGRenderer.prototype.destroy = function () {
  this._stopAnimationLoop();

  // Remove event listeners
  this.svg.removeEventListener('wheel', this._onWheel);
  this.svg.removeEventListener('mousedown', this._onMouseDown);
  document.removeEventListener('mousemove', this._onMouseMove);
  document.removeEventListener('mouseup', this._onMouseUp);
  this.svg.removeEventListener('dblclick', this._onDblClick);
  this.svg.removeEventListener('click', this._onClick);
  this.svg.removeEventListener('touchstart', this._onTouchStart);
  this.svg.removeEventListener('touchmove', this._onTouchMove);
  this.svg.removeEventListener('touchend', this._onTouchEnd);

  // Destroy subsystems
  if (this.particles) { this.particles.destroy(); this.particles = null; }
  if (this.tooltip) { this.tooltip.destroy(); this.tooltip = null; }
  if (this.detailPanel) { this.detailPanel.destroy(); this.detailPanel = null; }

  // Remove SVG
  if (this.svg && this.svg.parentNode) {
    this.svg.parentNode.removeChild(this.svg);
  }
  this.svg = null;
  this.viewport = null;
  this.linksLayer = null;
  this.nodesLayer = null;
  this.labelsLayer = null;
};
  Object.assign(global.__MG, {
SVGRenderer,
  });
})(typeof window !== 'undefined' ? window : typeof global !== 'undefined' ? global : this);
(function (global) {
  'use strict';
  var MG = global.__MG;
  var ForceLayout = MG.ForceLayout;
  var BloomAnimator = MG.BloomAnimator;
  var SVGRenderer = MG.SVGRenderer;
  var generateMockTree = MG.generateMockTree;
  var escapeHtml = MG.escapeHtml;
  var formatDate = MG.formatDate;
// ==========================================================================
// PUBLIC API - MemoryGraph
// ==========================================================================

let _instance = null;

/** Fetch the live memory tree from the backend. */
async function fetchMemoryTree() {
  try {
    const r = await fetch('/api/memory/lobes?expand=all', { cache: 'no-store' });
    if (!r.ok) throw new Error('fetch failed');
    return await r.json();
  } catch (err) {
    console.warn('[MemoryGraph] live memory tree unavailable:', err);
    return null;
  }
}

/**
 * Build layout nodes from tree data.
 * Initial view creates root + lobes only; subdomains and live memory
 * files expand on demand via bloom.
 */
function buildLayoutFromTree(layout, tree) {
  var LOBE_R = 150;
  // Root at center
  layout.addNode({
    id: tree.id, label: tree.label,
    x: layout.centerX, y: layout.centerY,
    vx: 0, vy: 0, radius: 28, mass: 5,
    parent: null, children: tree.children || [],
    expanded: true, level: 0, data: tree,
    _scale: 1, _opacity: 1,
  });
  layout.pin(tree.id, layout.centerX, layout.centerY);
  // Lobes in a circle
  (tree.children || []).forEach(function (lobe, idx) {
    var angle = (Math.PI * 2 * idx) / Math.max(tree.children.length, 1) - Math.PI / 2;
    var lx = layout.centerX + Math.cos(angle) * LOBE_R;
    var ly = layout.centerY + Math.sin(angle) * LOBE_R;
    layout.addNode({
      id: lobe.id, label: lobe.label,
      x: lx, y: ly, vx: 0, vy: 0,
      radius: 20, mass: 3, parent: tree.id,
      children: lobe.children || [], expanded: false,
      level: 1, data: lobe,
      _scale: 1, _opacity: 1,
    });
    layout.addLink({ source: tree.id, target: lobe.id, length: LOBE_R, strength: 0 });
  });
  // Let physics settle the initial layout naturally — don't freeze.
  layout.wake();
}

function captureGraphState(instance) {
  const state = {
    expandedIds: new Set(),
    positions: Object.create(null),
    viewport: null,
    selectedMemoryData: null,
  };
  if (!instance || !instance.layout) return state;
  instance.layout.nodes.forEach(function (node) {
    state.positions[node.id] = {
      x: node.x,
      y: node.y,
      fx: node._fx,
      fy: node._fy,
      frozen: node._frozen,
    };
    if (node.expanded) state.expandedIds.add(node.id);
  });
  if (instance.renderer) {
    state.viewport = {
      scale: instance.renderer.scale,
      panX: instance.renderer.panX,
      panY: instance.renderer.panY,
    };
    if (instance.renderer.detailPanel) {
      state.selectedMemoryData =
        instance.renderer.detailPanel.selectedMemoryData || null;
    }
  }
  return state;
}

function restoreNodePositions(layout, state) {
  if (!layout || !state || !state.positions) return;
  layout.nodes.forEach(function (node) {
    const pos = state.positions[node.id];
    if (!pos) return;
    node.x = pos.x;
    node.y = pos.y;
    node.vx = 0;
    node.vy = 0;
    if (pos.fx !== null && pos.fx !== undefined) {
      node._fx = pos.fx;
      node._fy = pos.fy;
    } else {
      node._fx = null;
      node._fy = null;
    }
    node._frozen = !!pos.frozen;
  });
}

function _pathFromRoot(tree, targetId, trail) {
  trail = trail || [];
  if (!tree) return null;
  if (tree.id === targetId) return trail.concat([tree]);
  const kids = tree.children || [];
  for (let i = 0; i < kids.length; i++) {
    const hit = _pathFromRoot(kids[i], targetId, trail.concat([tree]));
    if (hit) return hit;
  }
  return null;
}

function restoreExpandedNodes(instance, state, rootId) {
  if (!instance || !state || !state.expandedIds || !instance.tree) return;
  const expanded = state.expandedIds;
  if (!expanded.size) return;
  const bloomOrder = [];
  const seen = new Set();
  expanded.forEach(function (id) {
    if (!id || id === rootId) return;
    const path = _pathFromRoot(instance.tree, id);
    if (!path) return;
    path.forEach(function (node) {
      if (!node.id || node.id === rootId || seen.has(node.id)) return;
      seen.add(node.id);
      bloomOrder.push(node.id);
    });
  });
  if (!bloomOrder.length) return;
  const animator = instance.animator;
  const oldReduced = animator.reducedMotion;
  animator.reducedMotion = true;
  try {
    bloomOrder.forEach(function (id) {
      const node = instance.layout.getNode(id);
      if (!node) return;
      if ((node.children || []).length > 0 && !node.expanded) {
        animator.bloomNode(node, instance.layout);
        node.expanded = true;
      }
    });
  } finally {
    animator.reducedMotion = oldReduced;
  }
}

function restoreViewport(renderer, state) {
  if (!renderer || !state || !state.viewport) return;
  renderer.scale = state.viewport.scale;
  renderer.panX = state.viewport.panX;
  renderer.panY = state.viewport.panY;
  renderer._updateTransform();
}

function stabilizeLayout(layout) {
  if (!layout) return;
  layout.nodes.forEach(function (node) {
    node._frozen = true;
    node.vx = 0;
    node.vy = 0;
  });
  layout.isStable = true;
  layout.stableFrameCount = 999;
}

function refreshSelectedMemoryDetail(instance, state) {
  if (!instance || !state || !state.selectedMemoryData) return;
  const selected = state.selectedMemoryData;
  const renderer = instance.renderer;
  if (!renderer || !renderer.detailPanel || !renderer.detailPanel.visible) {
    return;
  }
  const node = instance.layout.getNode(selected.id);
  if (!node) return;
  const nextData = Object.assign({}, node.data || {}, { id: node.id });
  if (selected.content) nextData.content = selected.content;
  renderer.detailPanel.show(nextData);
}


function createToolbar(container, renderer) {
  const toolbar = document.createElement('div');
  toolbar.className = 'mg-toolbar';
  toolbar.style.cssText = [
    'position:absolute',
    'top:12px',
    'left:12px',
    'right:12px',
    'display:flex',
    'gap:6px',
    'align-items:center',
    'z-index:800',
    'background:rgba(18,18,20,0.72)',
    'border:1px solid rgba(255,255,255,0.07)',
    'border-radius:10px',
    'padding:8px 14px',
    'backdrop-filter:blur(20px) saturate(1.2)',
    'box-shadow:0 4px 16px rgba(0,0,0,0.3), inset 0 1px 0 rgba(255,255,255,0.03)',
    'font-family:var(--mono, JetBrains Mono, ui-monospace, monospace)',
  ].join(';');

  // Search input
  const searchBox = document.createElement('div');
  searchBox.style.cssText = 'position:relative;display:flex;align-items:center;';

  const searchInput = document.createElement('input');
  searchInput.type = 'text';
  searchInput.placeholder = 'SEARCH MEMORY...';
  searchInput.style.cssText = [
    'background:rgba(0,0,0,0.35)',
    'border:1px solid rgba(255,255,255,0.08)',
    'border-radius:6px',
    'padding:6px 10px 6px 28px',
    'color:#e8e4d8',
    'font-size:11px',
    'letter-spacing:0.12em',
    'width:220px',
    'outline:none',
    'transition:all 200ms',
    'font-family:var(--mono, JetBrains Mono, ui-monospace, monospace)',
  ].join(';');

  searchInput.addEventListener('input', function () {
    const count = renderer.setSearch(searchInput.value);
    resultCount.textContent = searchInput.value.trim() ? count + ' found' : '';
  });

  searchInput.addEventListener('focus', function () {
    searchInput.style.borderColor = 'rgba(196,133,94,0.35)';
    searchInput.style.boxShadow = '0 0 12px rgba(196,133,94,0.18)';
  });
  searchInput.addEventListener('blur', function () {
    searchInput.style.borderColor = 'rgba(255,255,255,0.08)';
    searchInput.style.boxShadow = 'none';
  });

  // Search icon
  const searchIcon = document.createElement('span');
  searchIcon.innerHTML = '&#9906;'; // magnifying glass
  searchIcon.style.cssText = 'position:absolute;left:8px;color:rgba(196,133,94,0.65);font-size:14px;transform:rotate(-45deg);';

  searchBox.appendChild(searchIcon);
  searchBox.appendChild(searchInput);
  toolbar.appendChild(searchBox);

  // Result count
  const resultCount = document.createElement('span');
  resultCount.style.cssText = 'color:#8a847a;font-size:10px;margin-left:4px;min-width:60px;text-transform:uppercase;letter-spacing:0.12em;';
  toolbar.appendChild(resultCount);

  // Separator
  const sep = document.createElement('div');
  sep.style.cssText = 'width:1px;height:20px;background:rgba(232,228,216,0.07);margin:0 4px;';
  toolbar.appendChild(sep);

  // Zoom buttons
  function makeBtn(label, title) {
    const btn = document.createElement('button');
    btn.textContent = label;
    btn.title = title;
    btn.style.cssText = [
      'background:rgba(255,255,255,0.04)',
      'border:1px solid rgba(232,228,216,0.07)',
      'border-radius:6px',
      'color:#8a847a',
      'font-size:14px',
      'width:30px',
      'height:28px',
      'cursor:pointer',
      'display:flex',
      'align-items:center',
      'justify-content:center',
      'transition:all 150ms',
    ].join(';');
    btn.addEventListener('mouseenter', function () {
      btn.style.background = 'rgba(196,133,94,0.08)';
      btn.style.borderColor = 'rgba(196,133,94,0.35)';
      btn.style.color = '#c4855e';
    });
    btn.addEventListener('mouseleave', function () {
      btn.style.background = 'rgba(255,255,255,0.04)';
      btn.style.borderColor = 'rgba(232,228,216,0.07)';
      btn.style.color = '#8a847a';
    });
    return btn;
  }

  const zoomInBtn = makeBtn('+', 'Zoom in');
  zoomInBtn.addEventListener('click', function () { renderer.zoom(ZOOM.BUTTON_STEP * renderer.scale); });
  toolbar.appendChild(zoomInBtn);

  const zoomOutBtn = makeBtn('\u2212', 'Zoom out');
  zoomOutBtn.addEventListener('click', function () { renderer.zoom(-ZOOM.BUTTON_STEP * renderer.scale); });
  toolbar.appendChild(zoomOutBtn);

  const fitBtn = makeBtn('\u26F6', 'Fit view');
  fitBtn.style.fontSize = '16px';
  fitBtn.addEventListener('click', function () { renderer.fitView(); });
  toolbar.appendChild(fitBtn);

  container.appendChild(toolbar);
  return toolbar;
}

/** Create info overlay */
function createInfoOverlay(container) {
  const info = document.createElement('div');
  info.className = 'mg-info';
  info.style.cssText = [
    'position:absolute',
    'bottom:12px',
    'left:12px',
    'color:rgba(212,207,199,0.5)',
    'font-size:10px',
    'font-family:var(--mono, JetBrains Mono, ui-monospace, monospace)',
    'letter-spacing:0.12em',
    'text-transform:uppercase',
    'z-index:800',
    'line-height:1.5',
    'background:rgba(18,18,20,0.62)',
    'border:1px solid rgba(255,255,255,0.06)',
    'border-radius:8px',
    'padding:6px 10px',
    'backdrop-filter:blur(14px)',
  ].join(';');
  info.innerHTML = 'Click nodes to expand &bull; terminal nodes open live memory';
  container.appendChild(info);
  return info;
}

// ==========================================================================
// EXPOSED API
// ==================================================================

var MemoryGraph = {

  /**
   * Initialize the graph in a container element.
   * @param {HTMLElement} containe         - The container to mount into
   */
  init: function (containerElement, options) {
    if (!containerElement) {
      console.error('[MemoryGraph] init requires a container element');
      return;
    }
    const opts = options || {};

    // Clean up any existing instance
    if (_instance) {
      MemoryGraph.destroy();
    }

    // Ensure container has positioning
    const cs = getComputedStyle(containerElement);
    if (cs.position === 'static') {
      containerElement.style.position = 'relative';
    }
    containerElement.style.overflow = 'hidden';

    const rect = containerElement.getBoundingClientRect();
    const layout = new MG.ForceLayout(rect.width || 800, rect.height || 600);
    const animator = new MG.BloomAnimator();
    const renderer = new MG.SVGRenderer(containerElement, layout, animator);

    renderer.init();

    // Build toolbar
    const toolbar = createToolbar(containerElement, renderer);
    const info = createInfoOverlay(containerElement);

    // Resize handler
    const onResize = debounce(function () {
      renderer.resize(false);
    }, 150);
    window.addEventListener('resize', onResize);

    // Store instance
    _instance = {
      container: containerElement,
      layout: layout,
      animator: animator,
      renderer: renderer,
      toolbar: toolbar,
      info: info,
      onResize: onResize,
      tree: null,
    };

    // Demo/fallback only — settings panel defers to live /api/memory fetch.
    if (!opts.deferLoad) {
      MemoryGraph.loadData(generateMockTree());
    }

    // Dismiss loading screen
    var mgLoad = containerElement.querySelector('.memory-graph-loading');
    if (mgLoad) { mgLoad.style.display = 'none'; }

    console.log('[MemoryGraph] Initialized');
  },

  /**
   * Load memory tree data into the graph.
   * @param {Object} data - The tree data structure
   * @param {Object} [options] - { preserveState: boolean }
   */
  loadData: function (data, options) {
    if (!_instance) {
      console.error('[MemoryGraph] Call init() before loadData()');
      return;
    }
    if (!data) return;

    const opts = options || {};
    const shouldPreserve = !!opts.preserveState && !!_instance.tree;
    const previousState = shouldPreserve
      ? captureGraphState(_instance)
      : null;

    _instance.tree = data;

    // Clear existing
    _instance.layout.nodes = [];
    _instance.layout.links = [];
    _instance.layout.nodeMap = Object.create(null);
    if (!shouldPreserve) {
      _instance.layout.wake();
    }
    _instance.animator.clear();
    _instance.particles && _instance.particles.clear();

    // Build layout
    buildLayoutFromTree(_instance.layout, data);

    const rootNode = _instance.layout.getNode(data.id);
    if (rootNode) {
      rootNode.expanded = true;
    }

    if (shouldPreserve) {
      restoreExpandedNodes(_instance, previousState, data.id);
      restoreNodePositions(_instance.layout, previousState);
      restoreViewport(_instance.renderer, previousState);
      refreshSelectedMemoryDetail(_instance, previousState);
      stabilizeLayout(_instance.layout);
    } else {
      _instance.renderer.fitView();
    }

    var mgLoad = _instance.container && _instance.container.querySelector('.memory-graph-loading');
    if (mgLoad) { mgLoad.style.display = 'none'; }
  },

  /**
   * Set s         ry to filter/highlight nodes.
   *         string} query - Search string
           ns {number} Number of matches
   */
  setSearch: function (query) {
    if (!_instance) {
      console.error('[MemoryGraph] Call init() before setSearch()');
      return 0;
    }
    return _instance.renderer.setSearch(query);
  },

  /**
   * Destroy the grap         e and clean up all resources.
   */
  destroy: function () {
    if (!_instance) return;

    window.removeEventListener('resize', _instance.onResize);

    if (_instance.renderer) {
      _instance.renderer.destroy();
    }

    if (_instance.toolbar && _instance.toolbar.parentNode) {
      _instance.toolbar.parentNode.removeChild(_instance.toolbar);
    }

    if (_instance.info && _instance.info.parentNode) {
      _instance.info.parentNode.removeChild(_instance.info);
    }

    _instance = null;
    console.log('[MemoryGraph] Destroyed');
  },

  /** Access the internal layout (for debugging) */
  _getLayout: function () { return _instance ? _instance.layout : null; },
  /** Access the internal renderer (for debugging) */
  _getRenderer: function () { return _instance ? _instance.renderer : null; },
  /** Generate a structural mock tree for fallback / demo */
  generateMockTree: generateMockTree,
};

/** Utility: debounce function */
function debounce(fn, wait) {
  let timer;
  return function () {
    const args = arguments;
    clearTimeout(timer);
    timer = setTimeout(function () { fn.apply(null, args); }, wait);
  };
}
  global.MemoryGraph = MemoryGraph;
})(typeof window !== 'undefined' ? window : typeof global !== 'undefined' ? global : this);
// ── Settings Panel Integration ──────────────────────────────
// Auto-init MemoryGraph when the memory settings tab is activated.
//
// Live backend wiring: fetches the on-disk Dewey tree from
// `/api/memory/lobes?expand=all`, polls every 10s while the view
// is mounted, listens to SSE (`omega:signal`, `omega:memory`,
// `omega:data`) for immediate refresh on writes, and watches
// `Telemetry.memory` for snapshot-driven updates. All listeners
// tear down in destroyMemoryGraph when the tab or settings panel
// closes (live-feed doctrine).
const _MG_CONTAINER_SKELETON =
'<div class="memory-graph-loading" id="mg-loading">' +
'<div class="loading-ring"></div>' +
'<div class="loading-text">INDEXING MEMORY</div>' +
'<div class="loading-shimmer"></div>' +
'</div>';

let _mgContainer = null;
let _mgActive = false;
let _mgPollTimer = null;
let _mgRefreshTimer = null;
let _mgSignalHandler = null;
let _mgMemoryHandler = null;
let _mgDataHandler = null;
let _mgTelemetryUnsub = null;
let _mgLastMemoryHash = '';
let _mgLastTreeHash = '';

function _mgShouldRefreshFromEvent(kind, detail) {
if (!detail || typeof detail !== 'object') return false;
if (kind === 'signal') {
  const name = String(detail.name || '').toLowerCase();
  return name.startsWith('memory.')
    || name.indexOf('subdomain') !== -1
    || name.indexOf('taxonomy') !== -1;
}
if (kind === 'memory') {
  const op = String(detail.op || detail.phase || detail.name || '').toLowerCase();
  const status = String(detail.status || '').toLowerCase();
  return op.indexOf('write') !== -1
    || op.indexOf('remember') !== -1
    || op.indexOf('commit') !== -1
    || op.indexOf('subdomain') !== -1
    || op.indexOf('taxonomy') !== -1
    || status === 'ok'
    || status === 'committed'
    || status === 'staged';
}
if (kind === 'data') {
  const loop = String(detail.loop || detail.system || '');
  return loop === 'memory';
}
return false;
}

function _scheduleMemoryGraphRefresh() {
if (!_mgActive) return;
if (_mgRefreshTimer) clearTimeout(_mgRefreshTimer);
_mgRefreshTimer = setTimeout(() => {
  _mgRefreshTimer = null;
  _refreshMemoryGraph();
}, 400);
}

if (typeof window !== 'undefined') {
window._memoryGraphRefresh = _scheduleMemoryGraphRefresh;
}

async function _fetchRealMemoryTree() {
try {
  const r = await fetch('/api/memory/lobes?expand=all', { cache: 'no-store' });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const tree = await r.json();
  if (!tree || tree.id !== 'root' || !Array.isArray(tree.children)) {
    throw new Error('invalid memory tree shape');
  }
  return tree;
} catch (e) {
  console.warn('[MemoryGraph] _fetchRealMemoryTree failed:', e);
  throw e;
}
}

async function _refreshMemoryGraph() {
if (!_mgActive) return;
try {
  const tree = await _fetchRealMemoryTree();
  let treeHash = '';
  try {
    treeHash = JSON.stringify(tree);
  } catch (_) {
    treeHash = String(Date.now());
  }
  if (treeHash === _mgLastTreeHash) return;
  _mgLastTreeHash = treeHash;
  MemoryGraph.loadData(tree, { preserveState: true });
} catch (e) {
  console.warn('[MemoryGraph] refresh failed:', e);
}
}

function _installMemoryGraphLiveFeeds() {
if (_mgSignalHandler) {
  try { window.removeEventListener('omega:signal', _mgSignalHandler); } catch (_) { }
}
if (_mgMemoryHandler) {
  try { window.removeEventListener('omega:memory', _mgMemoryHandler); } catch (_) { }
}
if (_mgDataHandler) {
  try { window.removeEventListener('omega:data', _mgDataHandler); } catch (_) { }
}
_mgSignalHandler = function (ev) {
  if (_mgShouldRefreshFromEvent('signal', ev && ev.detail)) {
    _scheduleMemoryGraphRefresh();
  }
};
_mgMemoryHandler = function (ev) {
  if (_mgShouldRefreshFromEvent('memory', ev && ev.detail)) {
    _scheduleMemoryGraphRefresh();
  }
};
_mgDataHandler = function (ev) {
  if (_mgShouldRefreshFromEvent('data', ev && ev.detail)) {
    _scheduleMemoryGraphRefresh();
  }
};
window.addEventListener('omega:signal', _mgSignalHandler);
window.addEventListener('omega:memory', _mgMemoryHandler);
window.addEventListener('omega:data', _mgDataHandler);

if (_mgTelemetryUnsub) {
  try { _mgTelemetryUnsub(); } catch (_) { }
  _mgTelemetryUnsub = null;
}
if (typeof Telemetry !== 'undefined' && typeof Telemetry.subscribe === 'function') {
  _mgLastMemoryHash = '';
  _mgTelemetryUnsub = Telemetry.subscribe((snap) => {
    if (!_mgActive) return;
    const mem = (snap && snap.memory) || {};
    let hash = '';
    try {
      hash = JSON.stringify({
        w: mem.recent_writes || [],
        l: mem.lobe_activity || {},
        t: mem.lobe_activity_total || 0,
      });
    } catch (_) { return; }
    if (hash === _mgLastMemoryHash) return;
    _mgLastMemoryHash = hash;
    _scheduleMemoryGraphRefresh();
  });
}
}

function _mgNeedsReinit(container) {
  if (!_mgActive) return true;
  if (!_mgContainer || !_mgContainer.isConnected || _mgContainer !== container) return true;
  if (typeof MemoryGraph === 'undefined' || !MemoryGraph._getRenderer()) return true;
  const loading = container.querySelector('.memory-graph-loading');
  if (loading && loading.style.display !== 'none') return true;
  if (!container.querySelector('.memory-graph-svg')) return true;
  return false;
}

function _mgHideLoading(container) {
  if (!container) return;
  const loading = container.querySelector('.memory-graph-loading');
  if (loading) loading.style.display = 'none';
}

function _mgResizeGraph(fitView) {
const renderer = MemoryGraph._getRenderer();
if (!renderer) return;
renderer.resize(false);
if (fitView) renderer.fitView();
}

function initMemoryGraphInSettings() {
const view = document.querySelector('[data-settings-view-panel="memory_graph"]');
if (!view) return;
let container = view.querySelector('.memory-graph-container');
if (!container) return;

const runInit = () => {
  if (!container.isConnected || !view.classList.contains('active')) return;

  if (!_mgNeedsReinit(container)) {
    requestAnimationFrame(() => _mgResizeGraph(false));
    return;
  }

  if (_mgActive) {
    try { MemoryGraph.destroy(); } catch (_) { }
    _mgActive = false;
    _mgContainer = null;
  }

  if (!container.querySelector('.memory-graph-loading') && !container.querySelector('.memory-graph-svg')) {
    container.innerHTML = _MG_CONTAINER_SKELETON;
  }
  _mgContainer = container;
  try {
    container._memoryDetailTarget = view.querySelector('[data-memory-graph-detail]');
    MemoryGraph.init(container, { deferLoad: true });
    _mgHideLoading(container);
    _mgActive = true;

    _refreshMemoryGraph().then(() => {
      _mgHideLoading(container);
      requestAnimationFrame(() => {
        requestAnimationFrame(() => _mgResizeGraph(true));
      });
    });

    if (_mgPollTimer) clearInterval(_mgPollTimer);
    _mgPollTimer = setInterval(() => {
      if (_mgActive && !document.hidden) _refreshMemoryGraph();
    }, 10000);

    _installMemoryGraphLiveFeeds();
  } catch (e) {
    console.error('[MemoryGraph] init failed:', e);
    _mgActive = false;
    _mgContainer = null;
    container.innerHTML =
      '<div class="memory-graph-empty"><div class="empty-icon"></div>' +
      '<p class="empty-title">Graph Engine Error</p>' +
      '<p class="empty-subtitle">' + (e.message || 'unknown') + '</p></div>';
  }
};

// Wait until the settings sub-view is laid out (not display:none).
requestAnimationFrame(() => {
  requestAnimationFrame(runInit);
});
}

function destroyMemoryGraph() {
if (_mgActive) {
  if (_mgPollTimer) {
    clearInterval(_mgPollTimer);
    _mgPollTimer = null;
  }
  if (_mgRefreshTimer) {
    clearTimeout(_mgRefreshTimer);
    _mgRefreshTimer = null;
  }
  if (_mgSignalHandler) {
    try { window.removeEventListener('omega:signal', _mgSignalHandler); } catch (_) { }
    _mgSignalHandler = null;
  }
  if (_mgMemoryHandler) {
    try { window.removeEventListener('omega:memory', _mgMemoryHandler); } catch (_) { }
    _mgMemoryHandler = null;
  }
  if (_mgDataHandler) {
    try { window.removeEventListener('omega:data', _mgDataHandler); } catch (_) { }
    _mgDataHandler = null;
  }
  if (_mgTelemetryUnsub) {
    try { _mgTelemetryUnsub(); } catch (_) { }
    _mgTelemetryUnsub = null;
  }
  _mgLastMemoryHash = '';
  _mgLastTreeHash = '';
  try { MemoryGraph.destroy(); } catch (_) { }
  if (_mgContainer) {
    _mgContainer._memoryDetailTarget = null;
    _mgContainer.innerHTML = _MG_CONTAINER_SKELETON;
  }
  _mgActive = false;
  _mgContainer = null;
}
}

// Expose for the settings panel tab switcher
window._memoryGraphInit = initMemoryGraphInSettings;
window._memoryGraphDestroy = destroyMemoryGraph;

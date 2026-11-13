// Zed panel visual effects — particles, sparklines, badges (hud/ui-src/core/panel-fx.js)
// Depends on: window.openPanel, window.closePanel

(function () {
  'use strict';

  // =====================================================================
  // Particle Network Background — renders on canvas behind panel content
  // =====================================================================
  const ParticleSystem = (function () {
    let _canvas = null;
    let _ctx = null;
    let _particles = [];
    let _animationId = null;
    let _running = false;
    const PARTICLE_COUNT = 60;
    const CONNECTION_DIST = 120;
    const MOUSE_DIST = 150;

    function _resize() {
      if (!_canvas || !_canvas.parentElement) return;
      const rect = _canvas.parentElement.getBoundingClientRect();
      _canvas.width = rect.width;
      _canvas.height = rect.height;
    }

    function _createParticles() {
      _particles = [];
      const w = _canvas.width;
      const h = _canvas.height;
      for (let i = 0; i < PARTICLE_COUNT; i++) {
        _particles.push({
          x: Math.random() * w,
          y: Math.random() * h,
          vx: (Math.random() - 0.5) * 0.4,
          vy: (Math.random() - 0.5) * 0.4,
          radius: Math.random() * 1.5 + 0.5,
          opacity: Math.random() * 0.3 + 0.1,
        });
      }
    }

    function _draw() {
      if (!_ctx || !_running) return;
      const w = _canvas.width;
      const h = _canvas.height;
      _ctx.clearRect(0, 0, w, h);

      // Get theme-aware colors
      const isPaper = document.documentElement.getAttribute('data-theme') === 'paper';
      const r = isPaper ? 27 : 196;
      const g_col = isPaper ? 32 : 133;
      const b = isPaper ? 36 : 94;

      // Update and draw particles
      for (let i = 0; i < _particles.length; i++) {
        const p = _particles[i];
        p.x += p.vx;
        p.y += p.vy;
        if (p.x < 0) { p.x = w; }
        if (p.x > w) { p.x = 0; }
        if (p.y < 0) { p.y = h; }
        if (p.y > h) { p.y = 0; }

        _ctx.beginPath();
        _ctx.arc(p.x, p.y, p.radius, 0, Math.PI * 2);
        _ctx.fillStyle = `rgba(${r}, ${g_col}, ${b}, ${p.opacity})`;
        _ctx.fill();

        // Draw connections
        for (let j = i + 1; j < _particles.length; j++) {
          const q = _particles[j];
          const dx = p.x - q.x;
          const dy = p.y - q.y;
          const dist = Math.sqrt(dx * dx + dy * dy);
          if (dist < CONNECTION_DIST) {
            const alpha = (1 - dist / CONNECTION_DIST) * 0.12;
            _ctx.beginPath();
            _ctx.moveTo(p.x, p.y);
            _ctx.lineTo(q.x, q.y);
            _ctx.strokeStyle = `rgba(${r}, ${g_col}, ${b}, ${alpha})`;
            _ctx.lineWidth = 0.5;
            _ctx.stroke();
          }
        }
      }

      _animationId = requestAnimationFrame(_draw);
    }

    function attach(panelFrameEl) {
      if (!panelFrameEl) return;
      // Remove existing canvas
      const existing = panelFrameEl.querySelector('.panel-particle-canvas');
      if (existing) existing.remove();

      _canvas = document.createElement('canvas');
      _canvas.className = 'panel-particle-canvas';
      panelFrameEl.insertBefore(_canvas, panelFrameEl.firstChild);
      _ctx = _canvas.getContext('2d');
      _resize();
      _createParticles();
      _running = true;
      if (_animationId) cancelAnimationFrame(_animationId);
      _draw();

      window.addEventListener('resize', _resize);
    }

    function detach() {
      _running = false;
      if (_animationId) {
        cancelAnimationFrame(_animationId);
        _animationId = null;
      }
      if (_canvas && _canvas.parentElement) {
        _canvas.remove();
      }
      _canvas = null;
      _ctx = null;
      _particles = [];
    }

    return { attach, detach };
  })();

  // =====================================================================
  // Animated Sparkline — SVG-based live data visualization
  // =====================================================================
  const Sparkline = (function () {
    const _instances = new Map();

    function create(container, options) {
      const opts = Object.assign({
        width: 300,
        height: 48,
        maxPoints: 60,
        color: '#c4855e',
        glowColor: 'rgba(196, 133, 94, 0.35)',
        fillOpacity: 0.15,
        lineWidth: 1.5,
        min: 0,
        max: 1,
      }, options);

      const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      svg.setAttribute('width', '100%');
      svg.setAttribute('height', opts.height);
      svg.setAttribute('viewBox', `0 0 ${opts.width} ${opts.height}`);
      svg.style.display = 'block';
      svg.style.overflow = 'visible';

      // Glow filter
      const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
      const filter = document.createElementNS('http://www.w3.org/2000/svg', 'filter');
      filter.setAttribute('id', 'spark-glow-' + Math.random().toString(36).slice(2, 8));
      filter.innerHTML = `
    <feGaussianBlur stdDeviation="2.5" result="coloredBlur"/>
    <feMerge>
      <feMergeNode in="coloredBlur"/>
      <feMergeNode in="SourceGraphic"/>
    </feMerge>`;
      defs.appendChild(filter);
      svg.appendChild(defs);

      // Gradient fill
      const grad = document.createElementNS('http://www.w3.org/2000/svg', 'linearGradient');
      grad.setAttribute('id', 'spark-fill-' + Math.random().toString(36).slice(2, 8));
      grad.setAttribute('x1', '0');
      grad.setAttribute('y1', '0');
      grad.setAttribute('x2', '0');
      grad.setAttribute('y2', '1');
      grad.innerHTML = `
    <stop offset="0%" stop-color="${opts.color}" stop-opacity="${opts.fillOpacity}"/>
    <stop offset="100%" stop-color="${opts.color}" stop-opacity="0"/>`;
      defs.appendChild(grad);

      // Fill area path
      const fillPath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      fillPath.setAttribute('fill', `url(#${grad.getAttribute('id')})`);
      svg.appendChild(fillPath);

      // Stroke path
      const strokePath = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      strokePath.setAttribute('fill', 'none');
      strokePath.setAttribute('stroke', opts.color);
      strokePath.setAttribute('stroke-width', opts.lineWidth);
      strokePath.setAttribute('filter', `url(#${filter.getAttribute('id')})`);
      strokePath.style.filter = `drop-shadow(0 0 4px ${opts.glowColor})`;
      svg.appendChild(strokePath);

      // Current value dot
      const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      dot.setAttribute('r', '3');
      dot.setAttribute('fill', opts.color);
      dot.style.filter = `drop-shadow(0 0 6px ${opts.glowColor})`;
      svg.appendChild(dot);

      container.appendChild(svg);

      const instance = {
        svg, fillPath, strokePath, dot, opts,
        data: [],
        push(value) {
          this.data.push(value);
          if (this.data.length > opts.maxPoints) this.data.shift();
          this._render();
        },
        _render() {
          if (this.data.length < 2) return;
          const { width, height, min, max } = this.opts;
          const range = max - min || 1;
          const stepX = width / (this.opts.maxPoints - 1);

          let d = '';
          let strokeD = '';
          const points = this.data.map((v, i) => {
            const x = (this.data.length <= this.opts.maxPoints)
              ? i * stepX
              : (i - (this.data.length - this.opts.maxPoints)) * stepX;
            const y = height - ((v - min) / range) * height;
            return { x, y: Math.max(2, Math.min(height - 2, y)), value: v };
          });

          points.forEach((p, i) => {
            const cmd = i === 0 ? 'M' : 'L';
            d += `${cmd}${p.x.toFixed(1)},${p.y.toFixed(1)} `;
            if (i < points.length - 1) strokeD += `${cmd}${p.x.toFixed(1)},${p.y.toFixed(1)} `;
          });

          // Close fill path
          const fillD = d + `L${points[points.length - 1].x.toFixed(1)},${height} L0,${height} Z`;
          this.fillPath.setAttribute('d', fillD);
          this.strokePath.setAttribute('d', strokeD || d);

          // Update dot position
          const last = points[points.length - 1];
          this.dot.setAttribute('cx', last.x.toFixed(1));
          this.dot.setAttribute('cy', last.y.toFixed(1));
        },
        destroy() {
          if (this.svg && this.svg.parentElement) this.svg.remove();
        }
      };

      const key = 'spark-' + Math.random().toString(36).slice(2, 8);
      _instances.set(key, instance);
      return { key, push: (v) => instance.push(v), destroy: () => { instance.destroy(); _instances.delete(key); } };
    }

    return { create };
  })();

  // =====================================================================
  // State Badge Component — glowing, animated status indicator
  // =====================================================================
  const StateBadge = (function () {
    const COLOR_MAP = {
      green: { color: '#6b8a6f', glow: 'rgba(107, 138, 111, 0.45)', label: 'HEALTHY' },
      amber: { color: '#c9a465', glow: 'rgba(201, 164, 101, 0.45)', label: 'DEGRADED' },
      red: { color: '#b85b4a', glow: 'rgba(184, 91, 74, 0.55)', label: 'CRITICAL' },
      blue: { color: '#6b8ab4', glow: 'rgba(107, 138, 180, 0.45)', label: 'PAUSED' },
      gray: { color: '#8a847a', glow: 'rgba(138, 132, 122, 0.25)', label: 'IDLE' },
      purple: { color: '#a078c8', glow: 'rgba(160, 120, 200, 0.45)', label: 'LOCKED' },
    };

    function create(container, state, value, meta) {
      const cfg = COLOR_MAP[state] || COLOR_MAP.gray;
      const el = document.createElement('div');
      el.className = 'v2-state-badge';
      el.style.cssText = `
    display: inline-flex;
    align-items: center;
    gap: 10px;
    font-family: var(--mono);
    font-size: 10px;
    letter-spacing: 0.28em;
    text-transform: uppercase;
    padding: 6px 14px;
    border-radius: 6px;
    border: 1px solid ${cfg.color}88;
    background: linear-gradient(165deg, ${cfg.color}15 0%, ${cfg.color}08 100%);
    box-shadow: 0 0 16px ${cfg.glow}30, inset 0 1px 0 ${cfg.color}10;
    color: ${cfg.color};
    text-shadow: 0 0 8px ${cfg.glow};
    transition: all 0.3s var(--easing-calm);
    cursor: default;
    position: relative;
    overflow: hidden;
  `;

      const dot = document.createElement('span');
      dot.style.cssText = `
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: ${cfg.color};
    box-shadow: 0 0 8px ${cfg.glow};
    animation: ${state === 'green' || state === 'live' ? 'statusDotPulse' : 'none'} 2.5s ease-in-out infinite;
    flex-shrink: 0;
  `;

      const text = document.createElement('span');
      text.textContent = value || cfg.label;

      el.appendChild(dot);
      el.appendChild(text);

      if (meta) {
        const metaEl = document.createElement('span');
        metaEl.style.cssText = `
      opacity: 0.6;
      font-size: 9px;
      letter-spacing: 0.16em;
      margin-left: 4px;
    `;
        metaEl.textContent = meta;
        el.appendChild(metaEl);
      }

      // Hover lift
      el.addEventListener('mouseenter', () => {
        el.style.boxShadow = `0 0 24px ${cfg.glow}50, inset 0 1px 0 ${cfg.color}18`;
        el.style.transform = 'translateY(-1px)';
      });
      el.addEventListener('mouseleave', () => {
        el.style.boxShadow = `0 0 16px ${cfg.glow}30, inset 0 1px 0 ${cfg.color}10`;
        el.style.transform = 'translateY(0)';
      });

      container.appendChild(el);
      return el;
    }

    return { create };
  })();

  // =====================================================================
  // Panel Section Card — glassmorphism container
  // =====================================================================
  const PanelCard = (function () {
    function create(container, title, options) {
      const opts = Object.assign({
        accentColor: 'var(--ember)',
        glowIntensity: 0.06,
      }, options);

      const card = document.createElement('div');
      card.className = 'panel-section';
      card.style.cssText = `
    margin-bottom: 28px;
    padding: 18px 20px;
    background: linear-gradient(165deg, rgba(26, 29, 35, 0.65) 0%, rgba(14, 16, 20, 0.5) 100%);
    border: 1px solid rgba(232, 228, 216, 0.06);
    border-radius: 10px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.35), 0 1px 3px rgba(0, 0, 0, 0.25), inset 0 1px 0 rgba(255, 255, 255, 0.04);
    position: relative;
    overflow: hidden;
    transition: box-shadow 0.35s var(--easing-calm), transform 0.35s var(--easing-calm);
  `;

      // Top accent line
      const accent = document.createElement('div');
      accent.style.cssText = `
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 1px;
    background: linear-gradient(90deg, transparent 0%, ${opts.accentColor}40 15%, ${opts.accentColor}15 50%, ${opts.accentColor}40 85%, transparent 100%);
  `;
      card.appendChild(accent);

      // Title
      if (title) {
        const label = document.createElement('div');
        label.className = 'panel-label';
        label.textContent = title;
        card.appendChild(label);
      }

      container.appendChild(card);
      return card;
    }

    return { create };
  })();

  // =====================================================================
  // Wire into existing panel system
  // =====================================================================

  // Attach particle system when panel opens
  const _origOpenPanel = window.openPanel;
  if (_origOpenPanel) {
    window.openPanel = function (id) {
      const result = _origOpenPanel(id);
      // Small delay to let panel render
      requestAnimationFrame(() => {
        const panelFrame = document.querySelector('.panel-frame');
        if (panelFrame && document.querySelector('.panel.open')) {
          ParticleSystem.attach(panelFrame);
        }
      });
      return result;
    };
  }

  // Detach particles when panel closes
  const _origClosePanel = window.closePanel;
  if (_origClosePanel) {
    window.closePanel = function () {
      ParticleSystem.detach();
      return _origClosePanel();
    };
  }

  // Also hook the ESC key / panel-close button
  const _panelCloseBtn = document.getElementById('panel-close');
  if (_panelCloseBtn) {
    _panelCloseBtn.addEventListener('click', () => {
      ParticleSystem.detach();
    });
  }

  // Expose to global for panel renderers
  window.OmegaVisuals = {
    ParticleSystem,
    Sparkline,
    StateBadge,
    PanelCard,
  };

  console.log('[OmegaVisuals] modern panel effect system loaded');
})();

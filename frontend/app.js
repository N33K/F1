// ── State ─────────────────────────────────────────────────────────────
const state = {
  circuits:        [],       // all circuits from /api/circuits
  selected:        null,     // currently selected circuit object
  animFrame:       null,     // requestAnimationFrame handle
  animRunning:     false,
  animProgress:    0,        // 0.0 → 1.0 across the lap
  animSpeed:       1,        // multiplier
  animLastTime:    null,     // timestamp of last frame
  pathLength:      0,        // SVG path total length in px
};

// ── API helpers ───────────────────────────────────────────────────────
async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} from ${url}`);
  return res.json();
}

// ── Boot ──────────────────────────────────────────────────────────────
async function init() {
  try {
    // Load coverage summary
    const status = await fetchJSON('/api/status');
    document.getElementById('coverage-text').textContent =
      `${status.fetched} / ${status.total_circuits} circuits fetched`;

    // Load all circuits
    state.circuits = await fetchJSON('/api/circuits');
    renderCircuitList(state.circuits);

  } catch (err) {
    console.error('Init failed:', err);
    document.getElementById('coverage-text').textContent = 'API offline';
  }
}

// ── Sidebar list ──────────────────────────────────────────────────────
function renderCircuitList(circuits) {
  const ul = document.getElementById('circuit-list');
  ul.innerHTML = '';

  circuits.forEach(c => {
    const li = document.createElement('li');
    li.className = 'circuit-item';
    li.dataset.id = c.id;

    const dotClass = {
      rich:     'dot-rich',
      balanced: 'dot-balanced',
      poor:     'dot-poor',
    }[c.energy_type] || 'dot-balanced';

    li.innerHTML = `
      <span class="circuit-item-round">${String(c.round).padStart(2,'0')}</span>
      <span class="circuit-item-name">${c.name.replace(' Grand Prix','').replace(' Prix','')}</span>
      <span class="circuit-item-dot ${dotClass}"></span>
    `;

    li.addEventListener('click', () => selectCircuit(c.id));
    ul.appendChild(li);
  });
}

// ── Select circuit ────────────────────────────────────────────────────
async function selectCircuit(id) {
  // Highlight in sidebar
  document.querySelectorAll('.circuit-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === id);
  });

  // Stop any running animation
  stopAnimation();

  // Show detail panel, hide empty state
  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('circuit-detail').style.display = 'block';

  // Fetch fresh data for this circuit
  try {
    const c = await fetchJSON(`/api/circuit/${id}`);
    state.selected = c;
    renderCircuitDetail(c);
  } catch (err) {
    console.error('Failed to load circuit:', err);
  }
}

// ── Render detail panel ───────────────────────────────────────────────
function renderCircuitDetail(c) {
  // Header
  document.getElementById('detail-round').textContent    = `ROUND ${String(c.round).padStart(2,'0')}`;
  document.getElementById('detail-name').textContent     = c.name;
  document.getElementById('detail-location').textContent = `${c.circuit} · ${c.city}, ${c.country}`;

  // No data banner
  const noData = document.getElementById('no-data-banner');
  noData.style.display = c.limits ? 'none' : 'flex';

  // Metric cards
  renderMetrics(c);

  // Session table
  renderSessionTable(c);

  // Load SVG
  loadCircuitSVG(c);
}

// ── Metric cards ──────────────────────────────────────────────────────
function renderMetrics(c) {
  const row = document.getElementById('metrics-row');

  if (!c.limits) {
    row.innerHTML = `<p style="color:var(--muted);font-size:12px;grid-column:1/-1">
      No official data yet — fetch from FIA to see energy metrics.
    </p>`;
    return;
  }

  const raceMJ  = c.limits.race.overtake_inactive;
  const qualiMJ = c.limits.qualifying;
  const lt      = c.lap_time_sec;

  const avgKw     = Math.round((raceMJ * 1000) / lt);
  const boostSec  = (raceMJ * 1e6 / 350000).toFixed(1);
  const fills     = (raceMJ / 4).toFixed(2);
  const homes     = Math.round(raceMJ / 3.6 * 1000);

  const cards = [
    { icon:'⚡', label:'Race MJ cap',        value: raceMJ + ' MJ',   color:'c-teal'   },
    { icon:'🏁', label:'Quali MJ cap',        value: qualiMJ + ' MJ', color:'c-blue'   },
    { icon:'📊', label:'Avg harvest rate',    value: avgKw + ' kW',   color:'c-orange' },
    { icon:'💥', label:'Max boost time',      value: boostSec + 's',  color:'c-red'    },
    { icon:'🔋', label:'Battery fills/lap',   value: fills + '×',     color:'c-purple' },
    { icon:'🏠', label:'Homes powered (1h)',  value: homes + '',       color:'c-teal'   },
  ];

  row.innerHTML = cards.map(card => `
    <div class="metric-card ${card.color}">
      <div class="metric-icon">${card.icon}</div>
      <div class="metric-value">${card.value}</div>
      <div class="metric-label">${card.label}</div>
    </div>
  `).join('');
}

// ── Session table ─────────────────────────────────────────────────────
function renderSessionTable(c) {
  const wrap = document.getElementById('session-table-wrap');
  const body = document.getElementById('session-table-body');

  if (!c.limits) {
    wrap.style.display = 'none';
    return;
  }

  wrap.style.display = 'block';
  const lt = c.lap_time_sec;

  const sessions = [
    { name: 'Race (no overtake)', mj: c.limits.race.overtake_inactive },
    { name: 'Race (overtake)',    mj: c.limits.race.overtake_active   },
    { name: 'Qualifying',         mj: c.limits.qualifying             },
    { name: 'Free Practice',      mj: c.limits.free_practice          },
    { name: 'Out laps',           mj: c.limits.out_laps               },
  ];

  body.innerHTML = sessions.map(s => {
    const avgKw    = Math.round((s.mj * 1000) / lt);
    const boostSec = (s.mj * 1e6 / 350000).toFixed(1);
    const fills    = (s.mj / 4).toFixed(2);
    const mjClass  = s.mj >= 8 ? 'mj-high' : s.mj >= 6 ? 'mj-mid' : 'mj-low';

    return `<tr>
      <td class="session-name">${s.name}</td>
      <td><span class="mj-pill ${mjClass}">${s.mj} MJ</span></td>
      <td>${avgKw} kW</td>
      <td>${boostSec}s</td>
      <td>${fills}×</td>
    </tr>`;
  }).join('');
}

// ── SVG loading ───────────────────────────────────────────────────────
async function loadCircuitSVG(c) {
  const wrap    = document.getElementById('svg-wrap');
  const loading = document.getElementById('svg-loading');

  if (loading) loading.style.display = 'block';

  // Remove any previous SVG
  wrap.querySelectorAll('svg').forEach(el => el.remove());

  if (!c.svg_file) {
    if (loading) loading.textContent = 'No SVG available for this circuit';
    return;
  }

  try {
    const res = await fetch(`/svgs/${c.svg_file}`);
    if (!res.ok) throw new Error(`SVG not found: ${c.svg_file}`);

    const svgText = await res.text();

    // Parse SVG text into DOM
    const parser = new DOMParser();
    const doc    = parser.parseFromString(svgText, 'image/svg+xml');
    const svg    = doc.querySelector('svg');

    if (!svg) throw new Error('Invalid SVG');

    // Get the path element — there's only one in these files
    const path = svg.querySelector('path');
    if (!path) throw new Error('No path in SVG');

    // Clone the path to create two layers:
    // 1. base — dim grey underlay showing the full circuit
    // 2. energy — coloured animated overlay

    const basePath   = path.cloneNode();
    const energyPath = path.cloneNode();

    basePath.classList.add('track-base');
    basePath.removeAttribute('style');

    energyPath.classList.add('track-energy');
    energyPath.removeAttribute('style');
    energyPath.style.stroke = energyColour(1.0);

    // Clear existing paths and add both layers
    svg.innerHTML = '';
    svg.appendChild(basePath);
    svg.appendChild(energyPath);

    // Ensure SVG scales responsively
    // Ensure SVG scales responsively
    // Read original dimensions before removing them
    const origW = svg.getAttribute('width')  || '500';
    const origH = svg.getAttribute('height') || '500';

    // Set viewBox so the browser knows the coordinate space
    if (!svg.getAttribute('viewBox')) {
    svg.setAttribute('viewBox', `0 0 ${origW} ${origH}`);
}

    svg.removeAttribute('width');
    svg.removeAttribute('height');
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

    if (loading) loading.style.display = 'none';
    wrap.appendChild(svg);

    // Measure path length after it's in the DOM
    // We need it in the DOM first so the browser can calculate geometry
    requestAnimationFrame(() => {
      state.pathLength = energyPath.getTotalLength();

      // Initialise dasharray — hides the overlay entirely to start
      energyPath.style.strokeDasharray  = state.pathLength;
      energyPath.style.strokeDashoffset = state.pathLength;

      // Reset animation state
      state.animProgress = 0;
      resetAnimationUI();
    });

  } catch (err) {
    console.error('SVG load error:', err);
    if (loading) loading.textContent = `Could not load circuit layout`;
  }
}

// ── Colour by energy state ────────────────────────────────────────────
// Maps battery percentage (0–1) to a colour:
// 1.0 (full)  → teal  #00D2BE
// 0.5 (half)  → orange #FF8000
// 0.0 (empty) → red   #E8002D
function energyColour(pct) {
  if (pct > 0.6) {
    // teal → orange
    const t = (1 - pct) / 0.4;
    return lerpColour([0,210,190], [255,128,0], t);
  } else {
    // orange → red
    const t = (0.6 - pct) / 0.6;
    return lerpColour([255,128,0], [232,0,45], t);
  }
}

function lerpColour(a, b, t) {
  const r = Math.round(a[0] + (b[0]-a[0]) * t);
  const g = Math.round(a[1] + (b[1]-a[1]) * t);
  const bl= Math.round(a[2] + (b[2]-a[2]) * t);
  return `rgb(${r},${g},${bl})`;
}

// ── Animation ─────────────────────────────────────────────────────────
function toggleAnimation() {
  if (state.animRunning) {
    stopAnimation();
  } else {
    startAnimation();
  }
}

function startAnimation() {
  if (!state.pathLength) return;
  if (state.animProgress >= 1) state.animProgress = 0;

  state.animRunning  = true;
  state.animLastTime = null;

  document.getElementById('play-btn').textContent = '⏸ Pause';
  requestAnimationFrame(animStep);
}

function stopAnimation() {
  state.animRunning = false;
  if (state.animFrame) cancelAnimationFrame(state.animFrame);
  document.getElementById('play-btn').textContent = '▶ Play lap';
}

function resetAnimationUI() {
  stopAnimation();
  state.animProgress = 0;

  const energyPath = document.querySelector('.track-energy');
  if (energyPath) {
    energyPath.style.strokeDashoffset = state.pathLength;
    energyPath.style.stroke = energyColour(1.0);
  }

  document.getElementById('energy-bar-fill').style.width      = '100%';
  document.getElementById('energy-bar-fill').style.background = 'var(--teal)';
  document.getElementById('energy-bar-pct').textContent       = '100%';
  document.getElementById('lap-time-display').textContent     = '0.0s';

  const c = state.selected;
  if (c) {
    document.getElementById('lap-total').textContent = `/ ${c.lap_time_sec}s`;
  }
}

function animStep(timestamp) {
  if (!state.animRunning) return;

  if (!state.animLastTime) state.animLastTime = timestamp;
  const delta = (timestamp - state.animLastTime) / 1000; // seconds
  state.animLastTime = timestamp;

  const c  = state.selected;
  const lt = c ? c.lap_time_sec : 90;

  // Advance progress — scaled by speed multiplier
  // Real time: delta seconds / lap_time_sec = fraction of lap per second
  state.animProgress += (delta * state.animSpeed) / lt;

  if (state.animProgress >= 1) {
    state.animProgress = 1;
    state.animRunning  = false;
    document.getElementById('play-btn').textContent = '▶ Play lap';
  }

  updateAnimationFrame(state.animProgress, lt);

  if (state.animRunning) {
    state.animFrame = requestAnimationFrame(animStep);
  }
}

function updateAnimationFrame(progress, lapTime) {
  const energyPath = document.querySelector('.track-energy');
  if (!energyPath || !state.pathLength) return;

  // Reveal the path from start proportional to progress
  const drawn = state.pathLength * progress;
  energyPath.style.strokeDashoffset = state.pathLength - drawn;

  // ── Energy simulation ────────────────────────────────────────────
  // Simple model: battery depletes as power is deployed,
  // partially recovers during estimated braking phases.
  // This is a visual approximation, not a real simulation.

  const c      = state.selected;
  const raceMJ = c && c.limits ? c.limits.race.overtake_inactive : 7.0;
  const batCap = 4.0; // MJ — fixed by regulation

  // Estimate energy state at this point in the lap:
  // - Deployment is roughly proportional to throttle (non-linear)
  // - Recovery happens in the last ~20% of most laps (braking zones)
  const deployRate  = raceMJ / batCap;          // depletes at this relative rate
  const rawDepletion = progress * deployRate;
  // Recovery kicks in during braking — modelled as a sine bump near lap end
  const recovery    = 0.3 * Math.max(0, Math.sin(progress * Math.PI));
  const batteryPct  = Math.max(0, Math.min(1, 1 - rawDepletion + recovery));

  // Update SVG stroke colour
  energyPath.style.stroke = energyColour(batteryPct);

  // Update energy bar
  const pctInt = Math.round(batteryPct * 100);
  const bar    = document.getElementById('energy-bar-fill');
  bar.style.width      = pctInt + '%';
  bar.style.background = energyColour(batteryPct);
  document.getElementById('energy-bar-pct').textContent = pctInt + '%';

  // Update lap timer
  const elapsed = (progress * lapTime).toFixed(1);
  document.getElementById('lap-time-display').textContent = elapsed + 's';
}

function setSpeed(multiplier, btn) {
  state.animSpeed = multiplier;
  document.querySelectorAll('.speed-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
}

// ── Fetch circuit data from FIA ───────────────────────────────────────
async function fetchCircuitData() {
  const c = state.selected;
  if (!c) return;

  const btn   = document.getElementById('fetch-btn');
  const label = document.getElementById('fetch-btn-label');

  btn.classList.add('loading');
  label.textContent = '⟳ Fetching...';
  btn.disabled      = true;

  try {
    const res = await fetch(`/api/fetch/${c.id}`, { method: 'POST' });
    const data = await res.json();

    if (data.status === 'fetching') {
      label.textContent = '⌛ Downloading PDF...';

      // Poll every 2 seconds until limits appear
      const poll = setInterval(async () => {
        try {
          const fresh = await fetchJSON(`/api/circuit/${c.id}`);
          if (fresh.limits) {
            clearInterval(poll);
            state.selected = fresh;
            renderCircuitDetail(fresh);

            btn.classList.remove('loading');
            label.textContent = '✓ Updated';
            btn.disabled      = false;

            setTimeout(() => {
              label.textContent = '↻ Refresh data';
            }, 2000);

            // Refresh sidebar coverage count
            const status = await fetchJSON('/api/status');
            document.getElementById('coverage-text').textContent =
              `${status.fetched} / ${status.total_circuits} circuits fetched`;
          }
        } catch (e) {
          clearInterval(poll);
          btn.classList.remove('loading');
          label.textContent = '↻ Refresh data';
          btn.disabled      = false;
        }
      }, 2000);

      // Stop polling after 30 seconds regardless
      setTimeout(() => {
        clearInterval(poll);
        if (btn.disabled) {
          btn.classList.remove('loading');
          label.textContent = '↻ Refresh data';
          btn.disabled      = false;
        }
      }, 30000);
    }

  } catch (err) {
    console.error('Fetch failed:', err);
    btn.classList.remove('loading');
    label.textContent = '↻ Refresh data';
    btn.disabled      = false;
  }
}

// ── Start ─────────────────────────────────────────────────────────────
init();

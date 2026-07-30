// ── State ─────────────────────────────────────────────────────────────
const state = {
  circuits:        [],
  selected:        null,
  animFrame:       null,
  animRunning:     false,
  animProgress:    0,
  animSpeed:       1,
  animLastTime:    null,
  pathLength:      0,
  menuOpen:        false,
};

// ── API helpers ───────────────────────────────────────────────────────
async function fetchJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status} from ${url}`);
  return res.json();
}

// ── Mobile menu ───────────────────────────────────────────────────────
function toggleMobileMenu() {
  state.menuOpen = !state.menuOpen;
  document.getElementById('burger-btn')?.classList.toggle('open', state.menuOpen);
  document.getElementById('sidebar')?.classList.toggle('open', state.menuOpen);
  document.getElementById('mob-overlay')?.classList.toggle('open', state.menuOpen);
}

function closeMobileMenu() {
  state.menuOpen = false;
  document.getElementById('burger-btn')?.classList.remove('open');
  document.getElementById('sidebar')?.classList.remove('open');
  document.getElementById('mob-overlay')?.classList.remove('open');
}

// ── Boot ──────────────────────────────────────────────────────────────
async function init() {
  try {
    const status = await fetchJSON('/api/status');
    document.getElementById('coverage-text').textContent =
      `${status.fetched} / ${status.total_circuits} circuits fetched`;

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

    li.addEventListener('click', () => {
      selectCircuit(c.id);
      closeMobileMenu();  // auto-close drawer on mobile after selection
    });
    ul.appendChild(li);
  });
}

// ── Select circuit ────────────────────────────────────────────────────
async function selectCircuit(id) {
  document.querySelectorAll('.circuit-item').forEach(el => {
    el.classList.toggle('active', el.dataset.id === id);
  });

  stopAnimation();

  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('circuit-detail').style.display = 'block';

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
  // Desktop header
  document.getElementById('detail-round').textContent    = `ROUND ${String(c.round).padStart(2,'0')}`;
  document.getElementById('detail-name').textContent     = c.name;
  document.getElementById('detail-location').textContent = `${c.circuit} · ${c.city}, ${c.country}`;

  // Mobile player header
  document.getElementById('mp-name').textContent = c.name;
  document.getElementById('mp-sub').textContent  = `ROUND ${String(c.round).padStart(2,'0')} · ${c.lap_time_sec}s lap`;
  document.getElementById('mp-total').textContent = `/ ${c.lap_time_sec}s`;

  const noData = document.getElementById('no-data-banner');
  noData.style.display = c.limits ? 'none' : 'flex';

  renderMetrics(c);
  renderSessionTable(c);
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

  const avgKw    = Math.round((raceMJ * 1000) / lt);
  const boostSec = (raceMJ * 1e6 / 350000).toFixed(1);
  const fills    = (raceMJ / 4).toFixed(2);
  const homes    = Math.round(raceMJ / 3.6 * 1000);

  const cards = [
    { icon:'⚡', label:'Race MJ cap',       value: raceMJ + ' MJ',  color:'c-teal'   },
    { icon:'🏁', label:'Quali MJ cap',       value: qualiMJ + ' MJ', color:'c-blue'   },
    { icon:'📊', label:'Avg harvest rate',   value: avgKw + ' kW',   color:'c-orange' },
    { icon:'💥', label:'Max boost time',     value: boostSec + 's',  color:'c-red'    },
    { icon:'🔋', label:'Battery fills/lap',  value: fills + '×',     color:'c-purple' },
    { icon:'🏠', label:'Homes powered (1h)', value: homes + '',      color:'c-teal'   },
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

  if (!c.limits) { wrap.style.display = 'none'; return; }

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
  wrap.querySelectorAll('svg').forEach(el => el.remove());

  if (!c.svg_file) {
    if (loading) loading.textContent = 'No SVG available for this circuit';
    return;
  }

  try {
    const res = await fetch(`/svgs/${c.svg_file}`);
    if (!res.ok) throw new Error(`SVG not found: ${c.svg_file}`);

    const svgText = await res.text();
    const parser  = new DOMParser();
    const doc     = parser.parseFromString(svgText, 'image/svg+xml');
    const svg     = doc.querySelector('svg');

    if (!svg) throw new Error('Invalid SVG');

    const path = svg.querySelector('path');
    if (!path) throw new Error('No path in SVG');

    const basePath   = path.cloneNode();
    const energyPath = path.cloneNode();

    basePath.classList.add('track-base');
    basePath.removeAttribute('style');

    energyPath.classList.add('track-energy');
    energyPath.removeAttribute('style');
    energyPath.style.stroke = energyColour(1.0);

    svg.innerHTML = '';
    svg.appendChild(basePath);
    svg.appendChild(energyPath);

    const origW = svg.getAttribute('width')  || '500';
    const origH = svg.getAttribute('height') || '500';

    if (!svg.getAttribute('viewBox')) {
      svg.setAttribute('viewBox', `0 0 ${origW} ${origH}`);
    }

    svg.removeAttribute('width');
    svg.removeAttribute('height');
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');

    if (loading) loading.style.display = 'none';
    wrap.appendChild(svg);

    requestAnimationFrame(() => {
      state.pathLength = energyPath.getTotalLength();
      energyPath.style.strokeDasharray  = state.pathLength;
      energyPath.style.strokeDashoffset = state.pathLength;
      state.animProgress = 0;
      resetAnimationUI();
    });

  } catch (err) {
    console.error('SVG load error:', err);
    if (loading) loading.textContent = 'Could not load circuit layout';
  }
}

// ── Colour by energy state ────────────────────────────────────────────
function energyColour(pct) {
  if (pct > 0.6) {
    const t = (1 - pct) / 0.4;
    return lerpColour([0,210,190], [255,128,0], t);
  } else {
    const t = (0.6 - pct) / 0.6;
    return lerpColour([255,128,0], [232,0,45], t);
  }
}

function lerpColour(a, b, t) {
  const r  = Math.round(a[0] + (b[0]-a[0]) * t);
  const g  = Math.round(a[1] + (b[1]-a[1]) * t);
  const bl = Math.round(a[2] + (b[2]-a[2]) * t);
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

  setPlayLabel('⏸ Pause');
  requestAnimationFrame(animStep);
}

function stopAnimation() {
  state.animRunning = false;
  if (state.animFrame) cancelAnimationFrame(state.animFrame);
  setPlayLabel('▶ Play lap');
}

// Updates both desktop and mobile play buttons
function setPlayLabel(label) {
  const desktop = document.getElementById('play-btn');
  const mobile  = document.getElementById('mp-play-btn');
  if (desktop) desktop.textContent = label;
  if (mobile)  mobile.textContent  = label;
}

function resetAnimationUI() {
  stopAnimation();
  state.animProgress = 0;

  const energyPath = document.querySelector('.track-energy');
  if (energyPath) {
    energyPath.style.strokeDashoffset = state.pathLength;
    energyPath.style.stroke = energyColour(1.0);
  }

  const c = state.selected;

  // Desktop elements
  const desktopBar = document.getElementById('energy-bar-fill');
  if (desktopBar) {
    desktopBar.style.width      = '100%';
    desktopBar.style.background = 'var(--teal)';
  }
  const desktopPct = document.getElementById('energy-bar-pct');
  if (desktopPct) desktopPct.textContent = '100%';
  const desktopTimer = document.getElementById('lap-time-display');
  if (desktopTimer) desktopTimer.textContent = '0.0s';
  const desktopTotal = document.getElementById('lap-total');
  if (desktopTotal && c) desktopTotal.textContent = `/ ${c.lap_time_sec}s`;

  // Mobile elements
  const mobProg = document.getElementById('mp-prog');
  if (mobProg) { mobProg.style.width = '0%'; mobProg.style.background = 'var(--teal)'; }
  const mobBatFill = document.getElementById('mp-bat-fill');
  if (mobBatFill) { mobBatFill.style.width = '100%'; mobBatFill.style.background = 'var(--teal)'; }
  const mobBatPct = document.getElementById('mp-bat-pct');
  if (mobBatPct) { mobBatPct.textContent = '100%'; mobBatPct.style.color = 'var(--teal)'; }
  const mobTimer = document.getElementById('mp-lap-time');
  if (mobTimer) mobTimer.textContent = '0.0s';
}

function animStep(timestamp) {
  if (!state.animRunning) return;

  if (!state.animLastTime) state.animLastTime = timestamp;
  const delta = (timestamp - state.animLastTime) / 1000;
  state.animLastTime = timestamp;

  const c  = state.selected;
  const lt = c ? c.lap_time_sec : 90;

  state.animProgress += (delta * state.animSpeed) / lt;

  if (state.animProgress >= 1) {
    state.animProgress = 1;
    state.animRunning  = false;
    setPlayLabel('▶ Play lap');
  }

  updateAnimationFrame(state.animProgress, lt);

  if (state.animRunning) {
    state.animFrame = requestAnimationFrame(animStep);
  }
}

function updateAnimationFrame(progress, lapTime) {
  const energyPath = document.querySelector('.track-energy');
  if (!energyPath || !state.pathLength) return;

  const drawn = state.pathLength * progress;
  energyPath.style.strokeDashoffset = state.pathLength - drawn;

  // Energy simulation
  const c       = state.selected;
  const raceMJ  = c && c.limits ? c.limits.race.overtake_inactive : 7.0;
  const batCap  = 4.0;

  const deployRate   = raceMJ / batCap;
  const rawDepletion = progress * deployRate;
  const recovery     = 0.3 * Math.max(0, Math.sin(progress * Math.PI));
  const batteryPct   = Math.max(0, Math.min(1, 1 - rawDepletion + recovery));
  const colour       = energyColour(batteryPct);
  const pctInt       = Math.round(batteryPct * 100);
  const elapsed      = (progress * lapTime).toFixed(1);

  energyPath.style.stroke = colour;

  // Desktop updates
  const desktopBar = document.getElementById('energy-bar-fill');
  if (desktopBar) { desktopBar.style.width = pctInt + '%'; desktopBar.style.background = colour; }
  const desktopPct = document.getElementById('energy-bar-pct');
  if (desktopPct) desktopPct.textContent = pctInt + '%';
  const desktopTimer = document.getElementById('lap-time-display');
  if (desktopTimer) desktopTimer.textContent = elapsed + 's';

  // Mobile updates
  const mobProg = document.getElementById('mp-prog');
  if (mobProg) { mobProg.style.width = Math.round(progress * 100) + '%'; mobProg.style.background = colour; }
  const mobBatFill = document.getElementById('mp-bat-fill');
  if (mobBatFill) { mobBatFill.style.width = pctInt + '%'; mobBatFill.style.background = colour; }
  const mobBatPct = document.getElementById('mp-bat-pct');
  if (mobBatPct) { mobBatPct.textContent = pctInt + '%'; mobBatPct.style.color = colour; }
  const mobTimer = document.getElementById('mp-lap-time');
  if (mobTimer) mobTimer.textContent = elapsed + 's';
}

// ── Speed control ─────────────────────────────────────────────────────
// Updates both desktop (.speed-btn) and mobile (.mp-sb) button sets
function setSpeed(multiplier, btn) {
  state.animSpeed = multiplier;

  // Remove active from all speed buttons in both sets
  document.querySelectorAll('.speed-btn, .mp-sb').forEach(b => b.classList.remove('active'));

  // Activate the matching button in both sets
  document.querySelectorAll('.speed-btn, .mp-sb').forEach(b => {
    if (b.textContent.trim() === multiplier + '×') b.classList.add('active');
  });
}

// ── Fetch circuit data from FIA ───────────────────────────────────────
async function fetchCircuitData() {
  const c = state.selected;
  if (!c) return;

  // Update both desktop and mobile fetch buttons
  const desktopBtn   = document.getElementById('fetch-btn');
  const desktopLabel = document.getElementById('fetch-btn-label');
  const mobileBtn    = document.getElementById('mob-refresh-btn');

  if (desktopBtn)   desktopBtn.classList.add('loading');
  if (desktopLabel) desktopLabel.textContent = '⟳ Fetching...';
  if (mobileBtn)    mobileBtn.textContent    = '⟳ Fetching...';

  if (desktopBtn) desktopBtn.disabled = true;
  if (mobileBtn)  mobileBtn.disabled  = true;

  const resetBtns = () => {
    if (desktopBtn)   { desktopBtn.classList.remove('loading'); desktopBtn.disabled = false; }
    if (desktopLabel) desktopLabel.textContent = '↻ Refresh data';
    if (mobileBtn)    { mobileBtn.textContent = '↻ Refresh'; mobileBtn.disabled = false; }
  };

  try {
    const res  = await fetch(`/api/fetch/${c.id}`, { method: 'POST' });
    const data = await res.json();

    if (data.status === 'fetching') {
      if (desktopLabel) desktopLabel.textContent = '⌛ Downloading...';
      if (mobileBtn)    mobileBtn.textContent    = '⌛ ...';

      const poll = setInterval(async () => {
        try {
          const fresh = await fetchJSON(`/api/circuit/${c.id}`);
          if (fresh.limits) {
            clearInterval(poll);
            state.selected = fresh;
            renderCircuitDetail(fresh);

            if (desktopLabel) desktopLabel.textContent = '✓ Updated';
            if (mobileBtn)    mobileBtn.textContent    = '✓ Done';

            setTimeout(resetBtns, 2000);

            const status = await fetchJSON('/api/status');
            const covEl  = document.getElementById('coverage-text');
            if (covEl) covEl.textContent =
              `${status.fetched} / ${status.total_circuits} circuits fetched`;
          }
        } catch (e) { clearInterval(poll); resetBtns(); }
      }, 2000);

      setTimeout(() => { clearInterval(poll); resetBtns(); }, 30000);
    }

  } catch (err) {
    console.error('Fetch failed:', err);
    resetBtns();
  }
}

// ── Start ─────────────────────────────────────────────────────────────
init();
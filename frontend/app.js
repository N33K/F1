// ── State ─────────────────────────────────────────────────────────────
const state = {
  circuits:   [],
  selected:   null,
  simulation: null,
  menuOpen:   false,
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

  state.simulation = null;

  document.getElementById('empty-state').style.display = 'none';
  document.getElementById('circuit-detail').style.display = 'block';

  try {
    const c = await fetchJSON(`/api/circuit/${id}`);
    state.selected = c;
    renderCircuitDetail(c);
    loadTrackMap(c);  // independent of simulation data — not awaited
  } catch (err) {
    console.error('Failed to load circuit:', err);
    return;
  }

  state.simulation = await loadSimulation(id);
  renderSpeedPowerGraph();
  renderSocGraph();
}

// ── Lap simulation (real telemetry-driven battery model) ───────────────
// Falls back to the black "no data found" timeline (see renderSocTimeline)
// when a circuit has no cached telemetry yet — most circuits, currently,
// since only 11 of 24 have been exported. Never treated as a hard failure.
// Expands the raw corner/straight segments into one flat list of sections
// carrying everything the graph (and the colour logic below, kept for
// later use) need: a cumulative distance range built by accumulating
// length_m in array order — NOT the segments' own start_distance_m, which
// resets/wraps at the start/finish line and so isn't a safe cumulative
// x-axis on its own — plus the real SOC at both ends and the energy
// "restored" (added to the battery) vs "discharged" (drawn from it) there.
function buildSimSections(data) {
  const sections = [];
  let cursor = 0;

  data.segments.forEach(seg => {
    const length = seg.length_m;
    const soc0Mj = sections.length === 0 ? data.meta.soc_start_mj : sections[sections.length - 1].soc1Mj;
    const soc1Mj = seg.soc_after_j / 1_000_000;

    if (seg.type === 'corner') {
      // "braking" (brake pedal, 350kW) or "liftoff" (throttle lift only, no
      // brake pedal, 200kW lift-and-coast rate) — undefined when no real
      // braking/lift zone was detected at all.
      const label = {
        braking: 'Braking zone',
        liftoff: 'Lift & coast corner',
      }[seg.corner_type] || 'Corner';

      sections.push({
        type: 'corner',
        cornerType: seg.corner_type,
        label,
        d0: cursor, d1: cursor + length,
        soc0Mj, soc1Mj,
        restoredMj: (seg.E_regen_j ?? 0) / 1_000_000,
        dischargedMj: 0,
      });
    } else {
      // coastD1 marks where the telemetry-measured coast portion
      // (_measure_coast_length in energy_model.py) ends and the drive
      // phase begins — superclipping regen runs the whole drive phase, not
      // just up to where deployment itself stops, so it's tracked as one
      // combined "restored" figure for the drive portion rather than
      // trying to split it further.
      const coastLen = Math.min(Math.max(seg.coast_length_m || 0, 0), length);

      sections.push({
        type: 'straight',
        label: 'Straight',
        d0: cursor, d1: cursor + length,
        coastD1: cursor + coastLen,
        soc0Mj, soc1Mj,
        restoredMj: ((seg.E_coast_regen_j ?? 0) + (seg.E_superclip_regen_j ?? 0)) / 1_000_000,
        dischargedMj: (seg.E_deployed_j ?? 0) / 1_000_000,
      });
    }

    cursor += length;
  });

  return sections;
}

// Concatenates each segment's own (distance, speed, deploy power) trace —
// corners get just their entry/apex/exit points (no dt-stepped loop to
// sample there), straights get the finer per-step trace — into one
// continuous, lap-wide array, converting each segment's locally-relative
// distance_m into the same cumulative x-axis buildSimSections uses.
function buildSimTrace(data) {
  const points = [];
  let cursor = 0;

  data.segments.forEach(seg => {
    (seg.trace || []).forEach(pt => {
      points.push({
        d: cursor + pt.distance_m,
        speedKmh: pt.speed_kmh,
        deployKw: (pt.deploy_power_w || 0) / 1000,
      });
    });
    cursor += seg.length_m;
  });

  return points;
}

async function loadSimulation(id) {
  try {
    const res = await fetch(`/api/simulate/${id}?session=race`);
    if (!res.ok) return null;

    const data = await res.json();
    return {
      meta: data.meta,
      lapLengthM: data.meta.lap_length_m,
      sections: buildSimSections(data),
      trace: buildSimTrace(data),
    };

  } catch (err) {
    console.error(`Simulation load failed for ${id}:`, err);
    return null;
  }
}

// ── Render detail panel ───────────────────────────────────────────────
function renderCircuitDetail(c) {
  document.getElementById('detail-round').textContent    = `ROUND ${String(c.round).padStart(2,'0')}`;
  document.getElementById('detail-name').textContent     = c.name;
  document.getElementById('detail-location').textContent = `${c.circuit} · ${c.city}, ${c.country}`;

  const noData = document.getElementById('no-data-banner');
  noData.style.display = c.limits ? 'none' : 'flex';

  renderMetrics(c);
  renderSessionTable(c);
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

// ── SOC timeline (real telemetry-driven battery model) ──────────────────
// One continuous, gapless bar spanning the whole lap distance, coloured by
// which regen/deployment system moved energy at that point — not split
// into separate labeled blocks (see buildTimelineRuns/renderSocTimeline).
// Lift-and-coast (coast phases + liftoff corners) is its own colour
// regardless of magnitude, since it's a distinct physical event; braking
// corners and straight-line deployment are coloured by the sign of their
// actual net SOC change instead.
function signColor(deltaMj) {
  return deltaMj < 0 ? 'discharge' : 'charge';
}

// Expands sim.sections into colour runs, in the order they actually happen
// along the lap:
//   - corner  -> liftoff corners are always "liftcoast"; braking corners
//     (or corners with no real braking/lift detected) are coloured by the
//     sign of their actual SOC delta.
//   - straight -> up to two runs:
//       1. "liftcoast" for the coast portion (d0 to coastD1), regardless of
//          how much it actually harvested.
//       2. Everything from there to the end of the straight, coloured by
//          the sign of restoredMj minus dischargedMj — deployment and
//          superclipping run concurrently and superclipping doesn't stop
//          when deployment does, so there's no clean boundary left between
//          "deploying" and "neutral cruising".
function buildTimelineRuns(sim) {
  const runs = [];

  sim.sections.forEach(sec => {
    if (sec.type === 'corner') {
      const colorClass = sec.cornerType === 'liftoff' ? 'liftcoast' : signColor(sec.soc1Mj - sec.soc0Mj);
      runs.push({ lengthM: sec.d1 - sec.d0, colorClass });
      return;
    }

    const coastLen = (sec.coastD1 ?? sec.d0) - sec.d0;
    if (coastLen > 0.5) {
      runs.push({ lengthM: coastLen, colorClass: 'liftcoast' });
    }
    const driveLen = sec.d1 - (sec.coastD1 ?? sec.d0);
    if (driveLen > 0.5) {
      runs.push({ lengthM: driveLen, colorClass: signColor(sec.restoredMj - sec.dischargedMj) });
    }
  });

  return runs;
}

// Renders the lap as one continuous bar (each run's width is its real
// share of the lap distance, with no gaps or borders between runs, so
// adjacent same- or different-coloured runs read as a single line).
function renderSocTimeline() {
  const timeline = document.getElementById('soc-timeline');
  const legend   = document.getElementById('phase-legend');
  if (!timeline) return;

  timeline.innerHTML = '';

  const sim = state.simulation;
  if (!sim) {
    if (legend) legend.style.display = 'none';
    timeline.classList.add('no-data');
    timeline.textContent = 'No data found';
    return;
  }

  timeline.classList.remove('no-data');
  if (legend) legend.style.display = 'flex';

  buildTimelineRuns(sim).forEach(run => {
    const block = document.createElement('div');
    block.className = `soc-timeline-run ${run.colorClass}`;
    block.style.flex = `${run.lengthM} 0 auto`;
    block.title = `${Math.round(run.lengthM)} m`;
    timeline.appendChild(block);
  });
}

// ── Track map (inline SVG + hover-marker calibration) ───────────────────
// Injected inline (not <img>) so JS can read the SVG's own path geometry —
// an <img> tag has no way to do that.
function pathStyleNumber(el, prop) {
  const m = new RegExp(`${prop}:\\s*([\\d.]+)`).exec(el.getAttribute('style') || '');
  return m ? parseFloat(m[1]) : null;
}

function pathHasFill(el) {
  return /fill:\s*#/.test(el.getAttribute('style') || '');
}

function bboxCenter(el) {
  const b = el.getBBox();
  return { x: b.x + b.width / 2, y: b.y + b.height / 2 };
}

function distSq(a, b) {
  const dx = a.x - b.x, dy = a.y - b.y;
  return dx * dx + dy * dy;
}

// Finds the arc-length (per path.getPointAtLength) closest to `point`: a
// coarse pass around the whole path, then a fine pass in a small window
// around the coarse winner for better precision without scanning densely
// end to end.
function nearestLengthOnPath(path, totalLength, point) {
  let best = 0, bestDist = Infinity;
  const coarseSteps = 200;

  for (let i = 0; i <= coarseSteps; i++) {
    const len = (i / coarseSteps) * totalLength;
    const d = distSq(path.getPointAtLength(len), point);
    if (d < bestDist) { bestDist = d; best = len; }
  }

  const window = totalLength / coarseSteps;
  const lo = Math.max(0, best - window);
  const hi = Math.min(totalLength, best + window);
  const fineSteps = 40;

  for (let i = 0; i <= fineSteps; i++) {
    const len = lo + (i / fineSteps) * (hi - lo);
    const d = distSq(path.getPointAtLength(len), point);
    if (d < bestDist) { bestDist = d; best = len; }
  }

  return best;
}

// Parses a simple relative-moveto polyline path's own vertex coordinates
// straight out of its `d` attribute, e.g. `d="m91.6 243.7 2.9-3.4 2.9-3.4
// 2.9 3.4 2.9 3.4"` — a bare list of numbers where the first pair is an
// absolute start point and every pair after that is a delta added to the
// previous point. This is all the chevron marker paths in these SVGs
// actually contain (no curves, no further command letters), verified
// against the raw files rather than assumed.
function parsePolylinePoints(pathEl) {
  const d = pathEl.getAttribute('d') || '';
  const nums = (d.match(/-?\d*\.?\d+(?:e-?\d+)?/g) || []).map(Number);
  const points = [];
  for (let i = 0; i + 1 < nums.length; i += 2) {
    if (i === 0) {
      points.push({ x: nums[0], y: nums[1] });
    } else {
      const prev = points[points.length - 1];
      points.push({ x: prev.x + nums[i], y: prev.y + nums[i + 1] });
    }
  }
  return points;
}

// Every circuit's SVG carries the same 3-path structure beyond the ribbon
// itself: a filled bar (fill set, stroke-width 0) and a thin open chevron
// (stroke-width 2, 5 points, two straight segments meeting at a vertex).
// Checked directly against the raw path data (not assumed): the filled
// bar's vertices are all equidistant from its own centroid — a plain
// rectangle with no distinguishable "tip" — and it sits right on the
// ribbon (<0.5 units off, across every circuit checked), so it's the
// start/finish line marker itself, not a direction indicator. The thin
// chevron sits ~33 units OFF the ribbon (also consistent across every
// circuit checked) but its own vertex angle gives a real, unambiguous
// pointing direction — that's the actual direction indicator. (An earlier
// version of this function had these two roles backwards, comparing the
// chevron's *position* against the bar's — which happened to produce
// exactly-equal offsets, since the chevron isn't near the ribbon at all;
// that's what caused a wrong direction on at least one circuit.)
//
// The "-outline" SVG variants add a 4th path: a stroke-width-5 duplicate of
// the ribbon's own outline — the width cutoff below (< 4) excludes that
// path deliberately, so this doesn't depend on document order.
//
// Nothing ties the ribbon's own arc-length parameterization (where length 0
// starts, which direction is "forward") to distance_m's real start/finish
// line and direction of travel — these are drawn by an unrelated
// third-party tracing tool. So: use the bar's position to find where the
// real line sits along the ribbon, and the chevron's own pointing
// direction, compared against the ribbon's local tangent there, to tell
// which way along the ribbon's parameterization is "forward". Returns null
// if the SVG doesn't have this structure, so callers can just skip showing
// the marker rather than guessing.
function calibrateTrack(svgEl) {
  const paths = Array.from(svgEl.querySelectorAll('path'));
  const mainPath = paths[0];
  if (!mainPath) return null;

  const sfLinePath = paths.find(p => pathHasFill(p) && pathStyleNumber(p, 'stroke-width') === 0);
  const chevronPath = paths.find(p => {
    if (p === mainPath || pathHasFill(p)) return false;
    const w = pathStyleNumber(p, 'stroke-width');
    return w !== null && w < 4;
  });
  if (!sfLinePath || !chevronPath) return null;

  const chevronPoints = parsePolylinePoints(chevronPath);
  if (chevronPoints.length < 5) return null;

  const totalLength = mainPath.getTotalLength();
  const startOffset = nearestLengthOnPath(mainPath, totalLength, bboxCenter(sfLinePath));

  // Chevron direction: the vertex where its two straight segments meet
  // (the middle point) minus the midpoint of its two open ends — points
  // the way the chevron is "aimed", independent of the ribbon.
  const vertex = chevronPoints[2];
  const openEndsMid = {
    x: (chevronPoints[0].x + chevronPoints[4].x) / 2,
    y: (chevronPoints[0].y + chevronPoints[4].y) / 2,
  };
  const chevronDir = { x: vertex.x - openEndsMid.x, y: vertex.y - openEndsMid.y };

  // Ribbon's own local tangent at the start/finish line's position —
  // whichever of +t/-t the chevron's direction agrees with (positive dot
  // product) is the real direction of travel.
  const eps = Math.max(totalLength * 1e-4, 0.01);
  const before = mainPath.getPointAtLength(Math.max(startOffset - eps, 0));
  const after  = mainPath.getPointAtLength(Math.min(startOffset + eps, totalLength));
  const tangent = { x: after.x - before.x, y: after.y - before.y };

  const alignment = tangent.x * chevronDir.x + tangent.y * chevronDir.y;
  const directionSign = alignment >= 0 ? 1 : -1;

  return { mainPath, totalLength, startOffset, directionSign };
}

// Maps a real distance-from-start/finish (metres) to a point on the track
// SVG. Returns null if calibration failed, so callers just skip the marker.
function pointForDistance(calibration, distanceM, lapLengthM) {
  if (!calibration || !lapLengthM) return null;
  const frac = (((distanceM % lapLengthM) + lapLengthM) % lapLengthM) / lapLengthM;
  const len = (
    calibration.startOffset +
    calibration.directionSign * frac * calibration.totalLength +
    calibration.totalLength * 2
  ) % calibration.totalLength;
  return calibration.mainPath.getPointAtLength(len);
}

async function loadTrackMap(c) {
  const container = document.getElementById('track-map');
  if (!container) return;

  container.innerHTML = '';
  state.trackCalibration = null;
  state.trackMarkerEl = null;

  if (!c.svg_file) return;

  try {
    const res = await fetch(`/svgs/${c.svg_file}`);
    if (!res.ok) return;
    const svgText = await res.text();

    container.innerHTML = svgText;
    const svgEl = container.querySelector('svg');
    if (!svgEl) return;

    // Set a viewBox from the original width/height BEFORE dropping them —
    // stripping width/height without a viewBox already in place is what
    // caused the old SVG clipping bug (see PROJECT_CONTEXT.md). Doing it
    // in this order keeps the coordinate space intact while still letting
    // CSS scale the element responsively.
    if (!svgEl.hasAttribute('viewBox')) {
      const w = svgEl.getAttribute('width') || '500';
      const h = svgEl.getAttribute('height') || '500';
      svgEl.setAttribute('viewBox', `0 0 ${w} ${h}`);
    }
    svgEl.removeAttribute('width');
    svgEl.removeAttribute('height');

    const marker = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    marker.setAttribute('class', 'track-marker');
    marker.setAttribute('r', '9');
    marker.setAttribute('display', 'none');
    svgEl.appendChild(marker);

    state.trackMarkerEl = marker;
    state.trackCalibration = calibrateTrack(svgEl);

  } catch (err) {
    console.error(`Track map load failed for ${c.id}:`, err);
  }
}

function showTrackMarker(distanceM) {
  const marker = state.trackMarkerEl;
  if (!marker) return;
  const point = pointForDistance(state.trackCalibration, distanceM, state.simulation?.lapLengthM);
  if (!point) { marker.setAttribute('display', 'none'); return; }
  marker.setAttribute('cx', point.x);
  marker.setAttribute('cy', point.y);
  marker.setAttribute('display', '');
}

function hideTrackMarker() {
  if (state.trackMarkerEl) state.trackMarkerEl.setAttribute('display', 'none');
}

// ── Shared graph helpers ──────────────────────────────────────────────
function valueToY(value, maxValue, height, pad) {
  const usable  = height - pad * 2;
  const clamped = Math.max(0, Math.min(value, maxValue));
  return pad + usable * (1 - clamped / maxValue);
}

function sectionAt(sim, distanceM) {
  return sim.sections.find(s => distanceM >= s.d0 && distanceM <= s.d1)
      || sim.sections[sim.sections.length - 1];
}

// Nearest trace point at-or-before distanceM, for reading off speed/deploy
// power on the speed/power graph — the trace is already fine-grained
// (~0.2s of sim time apart), so "nearest" reads as smooth interactively.
function tracePointAt(sim, distanceM) {
  const trace = sim.trace;
  if (!trace || !trace.length) return null;
  let lo = 0, hi = trace.length - 1;
  if (distanceM <= trace[0].d) return trace[0];
  if (distanceM >= trace[hi].d) return trace[hi];
  while (lo < hi - 1) {
    const mid = (lo + hi) >> 1;
    if (trace[mid].d <= distanceM) lo = mid; else hi = mid;
  }
  return trace[lo];
}

// ── SOC graph (real telemetry-driven battery model) — secondary display ──
// x axis: distance around the lap, start/finish line to start/finish line.
// y axis: battery charge in MJ. Colour is intentionally not applied here
// yet (see buildTimelineRuns above, kept for later use) — this is a plain
// single-coloured line.
const SOC_GRAPH_W   = 1000;
const SOC_GRAPH_H   = 120;
const SOC_GRAPH_PAD = 10;

function renderSocGraph() {
  const svg    = document.getElementById('soc-graph');
  const nodata = document.getElementById('soc-graph-nodata');
  if (!svg) return;

  svg.innerHTML = '';

  const sim = state.simulation;
  if (!sim || !sim.sections.length) {
    nodata.style.display = 'flex';
    return;
  }
  nodata.style.display = 'none';

  const maxMj      = sim.meta.battery_cap_mj || 4;
  const lapLengthM = sim.lapLengthM || sim.sections[sim.sections.length - 1].d1;
  const toX = d   => (d / lapLengthM) * SOC_GRAPH_W;
  const toY = soc => valueToY(soc, maxMj, SOC_GRAPH_H, SOC_GRAPH_PAD);

  const points = [{ d: 0, soc: sim.sections[0].soc0Mj }];
  sim.sections.forEach(sec => points.push({ d: sec.d1, soc: sec.soc1Mj }));

  const ns = 'http://www.w3.org/2000/svg';

  // Baselines at empty and full battery
  [0, maxMj].forEach(mj => {
    const line = document.createElementNS(ns, 'line');
    line.setAttribute('class', 'soc-graph-baseline');
    line.setAttribute('x1', 0);
    line.setAttribute('x2', SOC_GRAPH_W);
    line.setAttribute('y1', toY(mj));
    line.setAttribute('y2', toY(mj));
    svg.appendChild(line);
  });

  const linePoints = points.map(p => `${toX(p.d)},${toY(p.soc)}`).join(' ');
  const fillPoints = `${toX(points[0].d)},${toY(0)} ${linePoints} ${toX(points[points.length - 1].d)},${toY(0)}`;

  const fill = document.createElementNS(ns, 'polygon');
  fill.setAttribute('class', 'soc-graph-fill');
  fill.setAttribute('points', fillPoints);
  svg.appendChild(fill);

  const line = document.createElementNS(ns, 'polyline');
  line.setAttribute('class', 'soc-graph-line');
  line.setAttribute('points', linePoints);
  svg.appendChild(line);

  const cursor = document.createElementNS(ns, 'line');
  cursor.setAttribute('class', 'soc-graph-cursor');
  cursor.setAttribute('id', 'soc-graph-cursor');
  cursor.setAttribute('y1', 0);
  cursor.setAttribute('y2', SOC_GRAPH_H);
  svg.appendChild(cursor);

  const dot = document.createElementNS(ns, 'circle');
  dot.setAttribute('class', 'soc-graph-dot');
  dot.setAttribute('id', 'soc-graph-dot');
  dot.setAttribute('r', 4);
  svg.appendChild(dot);
}

// One-time listener setup — reads state.simulation fresh on every move
// rather than closing over it, since renderSocGraph() rebuilds the SVG's
// children (including the cursor/dot elements) on every circuit switch.
function initSocGraphHover() {
  const svg     = document.getElementById('soc-graph');
  const tooltip = document.getElementById('chart-tooltip');
  if (!svg || !tooltip) return;

  svg.addEventListener('mousemove', evt => {
    const sim = state.simulation;
    if (!sim || !sim.sections.length) return;

    const maxMj      = sim.meta.battery_cap_mj || 4;
    const lapLengthM = sim.lapLengthM || sim.sections[sim.sections.length - 1].d1;

    const rect      = svg.getBoundingClientRect();
    const xFrac     = Math.min(Math.max((evt.clientX - rect.left) / rect.width, 0), 1);
    const distanceM = xFrac * lapLengthM;

    const sec  = sectionAt(sim, distanceM);
    const span = sec.d1 - sec.d0;
    const frac = span > 0 ? (distanceM - sec.d0) / span : 0;
    const socMj = sec.soc0Mj + frac * (sec.soc1Mj - sec.soc0Mj);

    const svgX = xFrac * SOC_GRAPH_W;
    const svgY = valueToY(socMj, maxMj, SOC_GRAPH_H, SOC_GRAPH_PAD);

    const cursor = document.getElementById('soc-graph-cursor');
    const dot    = document.getElementById('soc-graph-dot');
    if (cursor) { cursor.setAttribute('x1', svgX); cursor.setAttribute('x2', svgX); cursor.style.opacity = 1; }
    if (dot)    { dot.setAttribute('cx', svgX); dot.setAttribute('cy', svgY); dot.style.opacity = 1; }

    tooltip.innerHTML = `
      <div class="chart-tooltip-title">${sec.label}</div>
      <div class="chart-tooltip-row"><span>Battery charge</span><span>${socMj.toFixed(2)} MJ</span></div>
      <div class="chart-tooltip-row"><span>Restored</span><span>+${sec.restoredMj.toFixed(3)} MJ</span></div>
      <div class="chart-tooltip-row"><span>Discharged</span><span>−${sec.dischargedMj.toFixed(3)} MJ</span></div>
    `;
    tooltip.style.display = 'block';
    tooltip.style.left = `${evt.clientX + 16}px`;
    tooltip.style.top  = `${evt.clientY + 16}px`;

    showTrackMarker(distanceM);
  });

  svg.addEventListener('mouseleave', () => {
    tooltip.style.display = 'none';
    const cursor = document.getElementById('soc-graph-cursor');
    const dot    = document.getElementById('soc-graph-dot');
    if (cursor) cursor.style.opacity = 0;
    if (dot)    dot.style.opacity = 0;
    hideTrackMarker();
  });
}

// ── Speed & electrical deployment graph — the headline visualization ────
// x axis: distance around the lap. Two y-scales sharing the same x: speed
// (km/h, the teal line) and electrical deployment power (kW, the orange
// filled area) — built from the fine-grained per-step trace (see
// buildSimTrace), not the coarse per-section boundaries, since the whole
// point is to show the sudden speed penalty at the exact moment deployment
// power decays away, not just the start/end of a straight.
const POWER_GRAPH_W        = 1000;
const POWER_GRAPH_H        = 240;
const POWER_GRAPH_PAD      = 10;
const POWER_GRAPH_MAX_KW   = 350;  // the FIA's own deployment ceiling — fixed, not autoscaled, so the filled area's height means the same thing on every circuit

function renderSpeedPowerGraph() {
  const svg    = document.getElementById('speed-power-graph');
  const nodata = document.getElementById('speed-power-graph-nodata');
  if (!svg) return;

  svg.innerHTML = '';

  const sim = state.simulation;
  if (!sim || !sim.trace || !sim.trace.length) {
    nodata.style.display = 'flex';
    return;
  }
  nodata.style.display = 'none';

  const lapLengthM = sim.lapLengthM || sim.trace[sim.trace.length - 1].d;
  const maxSpeed = Math.max(...sim.trace.map(p => p.speedKmh), 50) * 1.05;

  const toX = d => (d / lapLengthM) * POWER_GRAPH_W;
  const toYSpeed = kmh => valueToY(kmh, maxSpeed, POWER_GRAPH_H, POWER_GRAPH_PAD);
  const toYPower = kw  => valueToY(kw, POWER_GRAPH_MAX_KW, POWER_GRAPH_H, POWER_GRAPH_PAD);

  const ns = 'http://www.w3.org/2000/svg';

  // Baseline at the FIA's 350kW deployment ceiling, for scale reference
  const ceilingLine = document.createElementNS(ns, 'line');
  ceilingLine.setAttribute('class', 'speed-power-graph-baseline');
  ceilingLine.setAttribute('x1', 0);
  ceilingLine.setAttribute('x2', POWER_GRAPH_W);
  ceilingLine.setAttribute('y1', toYPower(POWER_GRAPH_MAX_KW));
  ceilingLine.setAttribute('y2', toYPower(POWER_GRAPH_MAX_KW));
  svg.appendChild(ceilingLine);

  const powerLinePoints = sim.trace.map(p => `${toX(p.d)},${toYPower(p.deployKw)}`).join(' ');
  const powerFillPoints = `${toX(sim.trace[0].d)},${toYPower(0)} ${powerLinePoints} ${toX(sim.trace[sim.trace.length - 1].d)},${toYPower(0)}`;

  const powerFill = document.createElementNS(ns, 'polygon');
  powerFill.setAttribute('class', 'speed-power-graph-fill');
  powerFill.setAttribute('points', powerFillPoints);
  svg.appendChild(powerFill);

  const powerLine = document.createElementNS(ns, 'polyline');
  powerLine.setAttribute('class', 'speed-power-graph-power-line');
  powerLine.setAttribute('points', powerLinePoints);
  svg.appendChild(powerLine);

  const speedLinePoints = sim.trace.map(p => `${toX(p.d)},${toYSpeed(p.speedKmh)}`).join(' ');
  const speedLine = document.createElementNS(ns, 'polyline');
  speedLine.setAttribute('class', 'speed-power-graph-speed-line');
  speedLine.setAttribute('points', speedLinePoints);
  svg.appendChild(speedLine);

  const cursor = document.createElementNS(ns, 'line');
  cursor.setAttribute('class', 'speed-power-graph-cursor');
  cursor.setAttribute('id', 'speed-power-graph-cursor');
  cursor.setAttribute('y1', 0);
  cursor.setAttribute('y2', POWER_GRAPH_H);
  svg.appendChild(cursor);

  const speedDot = document.createElementNS(ns, 'circle');
  speedDot.setAttribute('class', 'speed-power-graph-dot');
  speedDot.setAttribute('id', 'speed-power-graph-speed-dot');
  speedDot.setAttribute('r', 4);
  svg.appendChild(speedDot);
}

// One-time listener setup, same reasoning as initSocGraphHover — reads
// state.simulation fresh on every move.
function initSpeedPowerGraphHover() {
  const svg     = document.getElementById('speed-power-graph');
  const tooltip = document.getElementById('chart-tooltip');
  if (!svg || !tooltip) return;

  svg.addEventListener('mousemove', evt => {
    const sim = state.simulation;
    if (!sim || !sim.trace || !sim.trace.length) return;

    const lapLengthM = sim.lapLengthM || sim.trace[sim.trace.length - 1].d;
    const maxSpeed = Math.max(...sim.trace.map(p => p.speedKmh), 50) * 1.05;

    const rect      = svg.getBoundingClientRect();
    const xFrac     = Math.min(Math.max((evt.clientX - rect.left) / rect.width, 0), 1);
    const distanceM = xFrac * lapLengthM;

    const pt = tracePointAt(sim, distanceM);
    if (!pt) return;

    const svgX = xFrac * POWER_GRAPH_W;
    const svgY = valueToY(pt.speedKmh, maxSpeed, POWER_GRAPH_H, POWER_GRAPH_PAD);

    const cursor   = document.getElementById('speed-power-graph-cursor');
    const speedDot = document.getElementById('speed-power-graph-speed-dot');
    if (cursor)   { cursor.setAttribute('x1', svgX); cursor.setAttribute('x2', svgX); cursor.style.opacity = 1; }
    if (speedDot) { speedDot.setAttribute('cx', svgX); speedDot.setAttribute('cy', svgY); speedDot.style.opacity = 1; }

    const sec = sectionAt(sim, distanceM);
    tooltip.innerHTML = `
      <div class="chart-tooltip-title">${sec.label}</div>
      <div class="chart-tooltip-row"><span>Speed</span><span>${pt.speedKmh.toFixed(0)} km/h</span></div>
      <div class="chart-tooltip-row"><span>Electrical deployment</span><span>${pt.deployKw.toFixed(0)} kW</span></div>
    `;
    tooltip.style.display = 'block';
    tooltip.style.left = `${evt.clientX + 16}px`;
    tooltip.style.top  = `${evt.clientY + 16}px`;

    showTrackMarker(distanceM);
  });

  svg.addEventListener('mouseleave', () => {
    tooltip.style.display = 'none';
    const cursor   = document.getElementById('speed-power-graph-cursor');
    const speedDot = document.getElementById('speed-power-graph-speed-dot');
    if (cursor)   cursor.style.opacity = 0;
    if (speedDot) speedDot.style.opacity = 0;
    hideTrackMarker();
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
initSpeedPowerGraphHover();
initSocGraphHover();
init();
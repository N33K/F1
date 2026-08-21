# F1 Energy Decoder — Session Context (Energy Model + Visualization Rework)

This picks up after `PROJECT_CONTEXT.md` and `Simulation engine clarification and
purpose.md`. Read both of those first — this file covers one long session that
implemented the FIA energy rules from the second doc, found and fixed several
real bugs along the way, and ended with a significant pivot in what the
frontend actually visualizes. Written so the next chat can continue without
re-deriving any of this.

---

## Where things ended up

- `backend/energy_model.py` now implements three distinct, separately-capped
  regen systems, a deployment decay curve, and braking-intensity scaling —
  all per explicit confirmation from the user across many rounds of Q&A (see
  "Rules confirmed" below — don't re-derive these, they were hard-won).
- The frontend's headline visualization is no longer "battery state of
  charge" — it's **speed vs. electrical deployment power**, because the real
  point of the app is to show that energy limits cause a visible on-track
  performance penalty. Battery SOC is now a small secondary panel.
- A circuit map (inline SVG, not `<img>`) with a hover-linked marker that
  finds its position on the track via real geometry calibration (see below).

---

## Regulatory constants (energy_model.py, ~line 90-125)

| System | Cap | Constant | Notes |
|---|---|---|---|
| Braking regen (mechanical) | 350 kW peak | `P_MGUK_MAX = 350_000` | Scaled down by `braking_force_fraction()` — see below. Not flat. |
| Superclipping (crankshaft, concurrent w/ deployment) | 250 kW flat | `P_SUPERCLIP_MAX = 250_000` | No decay of its own. Runs the whole straight, not scoped to deployment's decay window. |
| Lift-and-coast (crankshaft, zero throttle+brake; also used for liftoff corners) | 200 kW flat | `P_LIFT_COAST_MAX = 200_000` | User's deliberate simplification of the FIA's literal 250kW superclipping figure — one flat rate for both coasting and liftoff corners. |
| Deployment decay | 350kW → 0kW | `DEPLOY_DECAY_RATE = 50_000` W/s | ~7s to fully decay. Resets only at the start of a new straight's drive phase — NOT mid-stretch (see bug list). |
| Battery | 4 MJ | `BATTERY_CAP = 4_000_000` | |
| Per-lap recharge (fallback) | 8.5 MJ | `DEFAULT_RECHARGE_LIMIT_J` | Real per-circuit values come from `overrides.json` via the FIA fetch pipeline; this is just the fallback. |

**These limit values are the user's explicit FIA-given numbers — treat them
as immutable.** Don't second-guess or retune 350/250/200/50kW-per-s/4MJ
without being asked. Everything fixed this session was about the
*structure/logic* around these values, never the values themselves.

## Rules confirmed via extensive back-and-forth (do not re-litigate)

These took several rounds of clarifying questions to nail down — treat as
settled unless the user explicitly revisits them:

1. **Deployment and superclipping run concurrently** (both active every
   second on a straight), matching the original FIA table the user provided
   — NOT sequential ("deploy until exhausted, then switch to regen mode",
   which was a tempting-sounding but wrong reading of a verbal description
   the user gave partway through).
2. **Superclipping is gated by battery headroom** (`battery_cap - soc`), not
   by the battery reaching literal empty. This is already how the code
   works (`min(P_SUPERCLIP_MAX, (battery_cap-soc)/dt, ...)`) — confirmed
   correct, not something to change.
3. **Superclipping stays flat at 250kW** — no decay curve of its own. Only
   deployment decays.
4. **Superclipping runs for the whole straight**, not scoped to the same
   ~7-8s window that limits deployment. This is *why* the battery ends up
   pinned at 4.00 MJ for long stretches of many circuits — see next section.
5. **Braking regen scales with how hard the driver actually brakes** — see
   `braking_force_fraction()` below. This uses the FIA doc's own suggested
   proxy (`braking_intensity ≈ speed_delta / duration`, since FastF1 only
   exposes a boolean brake signal, not real pedal force).

## The "battery always full" investigation — conclusion

After multiple fixes (below), the battery still ends up at 4.00 MJ for large
stretches of most circuits checked (e.g. China/Shanghai). This was
extensively traced and **is now understood to be the correct mathematical
consequence of the confirmed rules**, not a remaining bug: superclipping at a
flat 250kW for an entire straight structurally out-earns a deployment curve
that only spends ~1.2 MJ max per continuous run (`∫(350,000−50,000t)dt` from
0 to 7s) before decaying to zero. Once deployment ends, superclip keeps
charging, unopposed, for however long the straight continues.

**Given that, the user pivoted the actual goal** (see "The pivot" below)
rather than continuing to chase this as a bug. Don't re-open "why is the
battery full" without reading that section first.

---

## Bugs found and fixed this session

1. **Deploy-decay reset bug** (`simulate_lap`, drive-phase loop). The decay
   timer (`deploy_seconds_elapsed`) was reset to 0 whenever `P_deploy`
   computed to zero *for any reason* — including simply because the decay
   curve had run its natural course. That let deployment spike back to full
   350kW moments after bottoming out, instead of staying at the floor for
   the rest of the straight. Fixed: the timer now only resets at the start
   of each straight's drive phase.

2. **Flat braking regen regardless of duration.** A single exceptionally
   long braking zone (e.g. China's Turn 14: 293→105 km/h over 307m, ~5.56s,
   only ~0.96g average) was harvesting ~1.85 MJ — 46% of the whole battery —
   from one corner. Added `braking_force_fraction(a_brake_ms2)`: maps
   average deceleration to a fraction of `P_MGUK_MAX`, 30% at ≤1g
   (gentle/trail-braking) up to 100% at ≥5g (hardest real F1 stops), linear
   between (`BRAKING_DECEL_MIN_G`/`MAX_G`, `BRAKING_FORCE_MIN/MAX_FRACTION`,
   ~line 619). **Note:** the exact 1g/5g anchor points are my own reasonable
   defaults grounded in real F1 braking figures, not numbers the user
   explicitly specified — worth flagging if revisited.

3. **Obsolete pre-superclipping budget system.** `estimate_recoverable_energy()`
   and `allocate_straight_budgets()` were removed entirely (confirmed with
   the user first — this was a structural removal, not a value change).
   They predated superclipping/lift-and-coast and rationed a lap-wide
   "recoverable energy" estimate across straights by length, to stop the car
   spending the whole battery on one straight. That's now redundant (deploy
   already self-limits to ~1.2 MJ per run via decay alone) and was actively
   harmful — it cut deployment short *before* decay would have, handing
   extra unopposed time to superclip regen. Deployment is now capped only by
   its own decay curve and available battery charge.

4. **Track-map hover-marker direction detection, wrong on at least China.**
   The SVGs (from `f1-circuits-svg`, `white-outline` variant) have a
   4-path structure: `[0]` thick ribbon (stroke-width 20), `[1]` a filled
   bar (stroke-width 0, fill set), `[2]` a thin 5-point open chevron
   (stroke-width 2), `[3]` an outline duplicate of `[0]` (stroke-width 5,
   only in `-outline` variants). **Verified directly against raw path data
   across every circuit**, not assumed:
   - Path `[1]` (filled bar) sits <0.5 units from the ribbon on every
     circuit checked — it's the start/finish line marker itself, not a
     direction arrow (its 4 vertices are all equidistant from centroid — a
     plain rectangle, no "tip"). Used as the position anchor.
   - Path `[2]` (thin chevron) sits ~33 units *off* the ribbon on every
     circuit checked — its own vertex angle (not its position) gives the
     real direction signal. `parsePolylinePoints()` extracts its 5 vertices
     from the raw `d` attribute; direction = `vertex[2] - midpoint(vertex[0], vertex[4])`,
     compared against the ribbon's local tangent at the bar's position.
   - The original approach (comparing the two markers' *positions*) was
     wrong because the chevron isn't near the ribbon at all — it produced
     exactly-equal offsets and a coin-flip direction, which is what broke on
     China. Fixed in `calibrateTrack()` in `frontend/app.js`.

---

## The pivot: from "battery SOC" to "speed & electrical deployment"

Partway through investigating "why is the battery full," the user realized
the actual symptom they'd noticed wasn't about SOC being wrong — it's that
**deployment decay causes a real, visible performance penalty** (reduced
acceleration, potentially a speed drop) partway down a straight. That's not
a bug to hide — it's the whole educational point of the app: energy limits
have real on-track consequences, and the visualization should show that
directly rather than focusing on the battery percentage.

Explicit instruction: **"Let's not concentrate on the battery charge, and
rather on the electrical energy used throughout the lap... What matters is
to show people that energy limits actually create those problems of low
speed."** SOC display "doesn't really matter" — kept as a small secondary
panel.

### What was built

- **Backend** (additive only, no rule changed): a per-step trace
  (`distance_m`, `speed_kmh`, `deploy_power_w`, `superclip_power_w`) is now
  captured during the existing straight drive-phase loop (sampled every
  `TRACE_SAMPLE_EVERY_N_STEPS = 4` steps, ~0.2s of sim time) and a coarser
  2-3 point trace (entry/apex/exit) for corners, since corners are computed
  via closed-form formulas, not a stepped loop. Exposed as a `"trace"` field
  on each segment in the existing `/api/simulate/<id>` response — no new
  route, no app.py changes needed.
- **Frontend, new headline graph** — "Speed & Electrical Deployment": one
  SVG chart, x-axis = lap distance, teal line = speed (km/h, autoscaled),
  orange filled area = deployment power (kW, fixed 0-350kW scale against the
  real FIA ceiling so it means the same thing on every circuit). Built from
  `buildSimTrace()` concatenating every segment's trace into one continuous,
  cumulative-distance array. Hover shows a tooltip (speed + deployment kW)
  and moves the track-map marker.
- **Battery SOC graph**: same as before, but demoted — smaller viewBox
  (`0 0 1000 120` vs. the new graph's `0 0 1000 240`), plain panel labeled
  "BATTERY STATE OF CHARGE" with no fanfare.
- **Shared tooltip**: both graphs now use one `#chart-tooltip` element
  (`.chart-tooltip*` CSS classes) instead of the old SOC-only `#soc-tooltip`.

### Honest finding from testing

Checked China/Shanghai's segments after building this — **no dramatic
"sudden drop" actually appeared**, just a shallower acceleration slope once
deployment power tapers off (ICE alone still out-paces drag at those speeds
in this dataset). The user described "a sudden drop from 320 to 260" as the
kind of effect to expect/illustrate, but that specific figure wasn't
reproduced from real output — it may show up on other circuits/scenarios
(shorter straights, higher top speeds where ICE-alone can't sustain further
acceleration). Worth checking across more circuits with the new graph before
assuming it's "working as intended" everywhere.

### Deferred / explicitly not yet done

- User flagged **"some small issues with tooltip"** on the earlier
  (pre-pivot) graphs, to fix **"later"** — not detailed, not yet addressed.
  Ask what specifically before guessing.
- The color-coding logic from an earlier iteration this session
  (`buildTimelineRuns()`, `signColor()`, blue=charging/red=discharging/
  green=lift-and-coast, CSS vars `--blue`/`--green` etc.) is **intentionally
  kept in the code, unused** — the user said "don't remove them, maybe we'll
  use them later." Don't delete this as dead code without checking first.

---

## Frontend visual history within this session (for context, not action items)

The phase/track visualization went through several iterations in one
session — if you see old terminology in git history or stale comments,
here's the lineage: block-based colored "phase strip" with labels →
inline-SVG track map + continuous colored SOC timeline bar → SOC line graph
(replacing the bar) → **current state**: speed/deployment-power graph as
primary, SOC graph demoted to secondary. `backend/app.py`'s `/svgs/<file>`
route now serves the `white-outline` SVG variant (not `white`), so the track
outline stays visible against the dark theme.

---

## Environment gotchas hit this session (save yourself the rediscovery)

- **No Node.js on this machine.** To syntax-check `app.js`, install
  `esprima` (`pip install esprima`, pure Python) and normalize `?.` → `.`,
  `??` → `||`, and numeric separators (`1_000_000` → `1000000`) via regex
  before parsing — esprima doesn't support those ES2020+/ES2021 forms and
  will report false positives on unrelated, pre-existing code otherwise.
- **Windows + Git Bash process management is unreliable.** `python app.py &`
  followed by `kill $(cat pidfile)` (using bash's `$!`) does **not**
  reliably kill the real Windows process — it left 2-3 stale Flask servers
  simultaneously bound to port 5000 earlier in this session, serving stale
  code while restarts appeared to succeed. Always verify with
  `netstat -ano | grep "5000.*LISTENING"` and kill the *actual* PID it
  reports via `taskkill //F //PID <pid>`, not the bash job PID.
- Flask dev server: `cd backend && python app.py`, port 5000, `debug=False`
  (no auto-reload — must manually restart after any backend edit).

---

## Quick pointers for the next session

- `backend/energy_model.py` — all physics. Read the module docstring at the
  top; it's kept up to date with what's "included" and "known limitations."
- `frontend/app.js` — `buildSimSections()`/`buildSimTrace()` build the two
  data shapes the graphs consume; `calibrateTrack()` + friends handle the
  track-map geometry; `renderSpeedPowerGraph()` is the new headline chart.
- Server: `cd backend && python app.py`, then `http://localhost:5000`.

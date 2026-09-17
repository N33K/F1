"""
F1 2026 Energy Recovery / Deployment Model — v3 (telemetry-driven speed,
regulatory-max deployment)
=============================================================

Computes MGU-K energy recovery/deployment across a lap. Speed is read
directly from REAL telemetry (interpolated by distance) rather than
independently re-simulated from a force-balance model — v1 forward-simulated
speed via an inferred gear/RPM curve, a tire-traction ceiling, and a
throttle ramp-up curve; it could overshoot the real recorded top speed by
double digits of km/h with nothing structurally preventing it (see git
history / session notes if you need the old approach for reference).

Deployment POWER, however, is modeled on regulatory behavior, not derived
from that real motion: whenever a driver is at full throttle with SOC
available, they deploy the maximum MGU-K power the decay curve and battery
allow — full stop. (v2 briefly computed deploy_power as a residual —
required_power, from real acceleration, minus what the ICE alone could
supply at the real RPM — but near real cruising speed, drag rises so
steeply with v^2 that this residual collapses to near zero regardless of
how much power actually produced that speed; that's just what a
terminal-velocity plateau looks like, not evidence of low deployment. It
underestimated lap-wide deployment by roughly 5-10x versus what real
telemetry/HUDs show, and is why this module no longer does it — see
deploy_power in simulate_lap()'s drive-phase loop.)

USAGE — the one function you need:

    import energy_model as em

    circuits_data = em.load_circuits('circuits.json')
    result = em.run_lap_simulation(
        telemetry,              # FastF1 car data, .add_distance() already applied
        circuits_data,
        track_key='miami',
        scenario_name='race_pace',   # or 'qualifying'
        recharge_limit_mj=8.5,       # real value from the FIA PU Info doc, if available
        elevation_m=2, air_temp_c=29,
    )
    print(result['summary'])        # total MJ recovered/deployed, corner counts, etc.

WHAT'S INCLUDED:
  - 2026 regulatory constants (MGU-K 350kW, the 4MJ ES state-of-charge swing
    limit, mass, fuel)
  - Two scenarios: qualifying (starts at the top of the SOC window, light
    fuel) / race pace (starts mid-window, half fuel) — both starting points
    are assumptions, not regulation; see SCENARIOS
  - Real per-circuit recharge limits pulled at runtime (not hardcoded — see
    run_lap_simulation's recharge_limit_mj argument), falling back to the
    8.5 MJ modal default when a circuit's FIA doc isn't published yet
  - Per-circuit aero profiles (corner-mode vs. straight-mode Cd*A), read
    from circuits.json's downforce_level -> aero_profiles lookup
  - Air density from circuit elevation + session temperature
  - On a straight, real recorded speed (interpolated from telemetry by
    distance — see real_speed_kmh() in simulate_lap()) drives the trace: no
    gear/RPM inference, no traction-limit model, no throttle ramp-up curve,
    nothing that could overshoot what the car actually did. Deployment
    POWER, separately, is the regulatory decay-curve max — see the module
    docstring above and deploy_power in simulate_lap().
  - Corner/straight extraction with TWO detection methods merged:
      1. Brake-signal based (hard braking zones), filtered for minimum
         distance AND genuine speed loss (rejects brake-signal flicker/noise)
      2. Throttle-lift based (corners taken by lifting only, no brake pedal —
         real MGU-K engine-braking regen opportunity a brake-only detector
         would completely miss)
  - Length-proportional energy budget allocation across deployment straights
    (prevents the car from spending the whole battery on the first straight)
  - Two distinct regen systems, each with its own FIA-mandated cap: 350kW
    mechanical-brake regen (braking corners) and 200kW lift-and-coast regen
    (zero throttle AND zero brake — coast phases and liftoff corners). (An
    earlier version also modeled a 250kW "superclipping" regen mode,
    mutually exclusive with deployment, triggered at low SOC — removed as a
    misreading of Article C5.2.8's "Alt 1" curve, which is actually a
    deployment power *ceiling*, not a regen system. See the note near
    P_MGUK_MAX.)
  - Deployment power decay: 350kW ramping down 50kW per second of continuous
    full-throttle deployment, resetting on any interruption, per the FIA's
    published deployment curve
  - Speed-dependent deployment ceiling (Article C5.2.8 — see
    speed_deployment_ceiling_w): a second, independent cap on deployment
    power, combined with the decay curve via min(). Both the "Base -
    Standard" (limb i) and "Base - Overtake" (limb ii) curves are
    implemented; which one applies follows the session type (see SCENARIOS
    and Article B7.2.2).
  - Overtake Override Mode's two distinct effects, per its glossary
    definition: the alternative power curve above, AND the additional
    per-lap Recharge allowance (+0.5MJ, C5.2.10.iii). The second is applied
    by the caller passing the "Overtake active" MJ figure — see app.py's
    get_recharge_limit_mj.
  - Article C5.12's real deployment ramp-down mechanics (see
    deploy_decay_cap_w): a 150kW instant cut at the start of a power-limited
    period, held >=1s, then reduced at that circuit's own FIA-published rate
    (50 or 100 kW/s, from its Power Unit Information doc via overrides.json)
    down to zero
  - Coast-phase regen with closed-aero drag: the telemetry-measured gap
    between brake-release and re-throttle now regens via lift-and-coast
    (200kW) and uses the corner (closed/high-drag) aero profile, rather than
    being energy- and drag-neutral

VALIDATION APPROACH: speed is no longer an approximation being measured
against telemetry — it IS telemetry, by construction, so a speed-matching
error metric no longer means anything (v1's old 4-12% figures measured
exactly that, and are obsolete here). Deployment power is a regulatory
assumption (full decay-curve max at full throttle), not derived from
telemetry either, so there's nothing to validate it against directly — what's
worth sanity-checking is plausibility: lap totals should use a meaningful
fraction of the 4MJ permitted SOC swing on tracks with long straights (not
near-zero), summary["soc_swing_mj"] should never exceed that 4MJ limit, and
totals should be non-negative.

KNOWN LIMITATIONS (accepted, not silently hidden):
  - Deployment power is a regulatory-behavior assumption (drivers use
    everything available), not derived from or reconciled against real
    per-step motion — real telemetry only drives the displayed speed trace
    and real corner entry/apex/exit speeds. This means total deployed vs.
    regen energy for a straight isn't guaranteed to exactly explain that
    straight's real speed delta; it's a plausible accounting of what the
    power unit was doing, not a from-first-principles derivation of speed.
  - Lift-and-coast: real driver strategy choice, not modeled as a general
    rule (tested and reverted — a blanket rule hurt more than it helped)
  - Flat-out high-speed corner sequences with zero brake/throttle signature
    (e.g. Silverstone's Maggotts-Becketts) are invisible to both corner-
    detection methods — a real structural gap, not a bug
  - "Alt 1" (Article C5.2.8.iii) is still not implemented — it applies only
    at FIA-designated circuit sectors during Sprint/Race. Those sector
    windows ARE published in each event's PU Information doc; parser.py just
    doesn't extract them yet.
  - Overtake in a RACE is proximity-gated (Article B7.2.3: within the
    Detection Gap of another car at the Detection Line, plus Safety Car and
    yellow-flag suspensions). None of that is derivable from single-car,
    single-lap telemetry, so race sims default to Overtake off (clean air)
    and can only be forced on wholesale via run_lap_simulation's
    overtake_active — which then treats the WHOLE lap as overtake-enabled,
    not just the real activation window. Qualifying needs none of this: it's
    an LTCS session, enabled end to end by B7.2.2.
  - Because the per-lap deployment cap reuses the recharge limit (see
    lap_deploy_used), Overtake's +0.5MJ recharge allowance also raises the
    deployment ceiling by the same 0.5MJ — a side effect of that existing
    simplification, not a modeled regulation.
  - Article C5.2.9 bounds the SOC swing over the whole time the car is on
    track; this model simulates ONE lap in isolation and reports that lap's
    swing (summary["soc_swing_mj"]). A slow SOC drift across many laps could
    breach the limit while every individual lap looks compliant — invisible
    here by construction, since there's no multi-lap state.
  - Where the permitted 4MJ SOC window physically sits inside the Energy
    Store is unknown (team design choice, deliberately unspecified by the
    regulations) and unmodelled — SOC here is position within that window,
    not absolute charge.
"""

import json
import math
import numpy as np
import pandas as pd
from scipy.interpolate import PchipInterpolator


# ---------------------------------------------------------------------------
# 1. REGULATORY CONSTANTS (2026)
# ---------------------------------------------------------------------------

P_MGUK_MAX = 350_000        # W, bidirectional (deploy + regen)
# Article C5.2.9: "The difference between the maximum and the minimum state
# of charge of the ES may not exceed 4MJ at any time the car is on track."
# NOT a statement of battery capacity — the ES's actual usable capacity is a
# team engineering choice the regulations deliberately don't specify. What's
# capped is how far SOC may *swing*.
#
# This model therefore treats SOC as a position within that permitted 4MJ
# window (0 = bottom of the window, ES_SOC_SWING_LIMIT_J = top), rather than
# as absolute charge in the cells. Clamping SOC to [0, 4MJ] is equivalent to
# enforcing max-min <= 4MJ for a single lap, while being honest that where
# that window sits inside the physical store is unknown and unmodellable.
ES_SOC_SWING_LIMIT_J = 4_000_000     # J (4 MJ)

# NOTE: an earlier version of this module modeled "superclipping" as a
# separate crankshaft-connected regen system, mutually exclusive with
# deployment, triggered once SOC dropped to 20% of battery_cap. That was
# based on a misreading: per the FIA's actual Article C5.2.8, the 250kW
# "Alt 1" figure is a third MGU-K *deployment power ceiling* curve (like
# Standard/Overtake), gated to specific circuit sectors during Race/Sprint
# (subject to Article B7.2) — not an independent regen mechanism. There is
# no crankshaft-only regen system distinct from Recharge (C5.2.10, the same
# MGU-K harvesting braking/coast already model). The mutual-exclusivity
# machinery and its SOC-fraction trigger were removed for this reason — see
# git history for the old implementation. Alt-1 itself (as a deployment
# ceiling) is deferred until per-circuit sector data exists to know where it
# applies; see this module's known-limitations notes.

# Lift-and-coast: crankshaft regen with zero throttle AND zero brake — the
# telemetry-measured coast phase between corners, and liftoff corners (a
# throttle lift with no brake pedal) use this same rate.
P_LIFT_COAST_MAX = 200_000  # W — lift-and-coast regen cap



def speed_deployment_ceiling_w(v_kmh, overtake_active=False, alt_curve=False):
    """
    Article C5.2.8 — the MGU-K DC power ceiling as a function of car speed
    (vCar). A second, independent constraint on deployment, separate from
    the time-decay curve above: at any instant the car may draw no more than
    whichever of the two is lower (see deploy_power_cap in simulate_lap()'s
    drive-phase loop). At low speed these formulas alone would allow more
    than P_MGUK_MAX (e.g. 1800kW at 0km/h) — that's fine, since the
    decay-curve cap never itself exceeds 350kW, so combining both via min()
    automatically respects the absolute C5.2.7 ceiling without a separate
    clamp here.

    overtake_active selects between:
      - limb i, "Base - Standard" (default): tapers from ~290km/h and
        reaches zero at 345km/h.
      - limb ii, "Base - Overtake": holds far more power at the top end
        (only drops below 350kW above ~337km/h) and reaches zero at
        355km/h. Per Article B7.2.2 this is enabled for the whole session
        in Practice/Qualifying (LTCS); in Sprint/Race (TTCS) it is
        proximity-gated and NOT modeled — see run_lap_simulation.

    alt_curve selects limb iii, "Alt 1" — a flat 250kW below 310km/h that
    then merges into limb i's taper. It is markedly more restrictive than
    Standard at low and middling speed (Standard allows 550kW at 250km/h),
    and applies only within the circuit sectors the FIA designates for it,
    in a Race or Sprint. The FIA marks limb iii as safety-related. Which
    sectors those are is published per event and parsed into overrides.json
    as sectors.alt_power_curve; simulate_lap applies it by position.
    """
    if alt_curve:
        if v_kmh < 310:
            p_kw = 250
        elif v_kmh < 340:
            p_kw = 1800 - 5 * v_kmh
        elif v_kmh < 345:
            p_kw = 6900 - 20 * v_kmh
        else:
            p_kw = 0
        return max(p_kw, 0) * 1000

    if overtake_active:
        p_kw = 7100 - 20 * v_kmh if v_kmh < 355 else 0
    elif v_kmh < 340:
        p_kw = 1800 - 5 * v_kmh
    elif v_kmh < 345:
        p_kw = 6900 - 20 * v_kmh
    else:
        p_kw = 0
    return max(p_kw, 0) * 1000


# Deployment ramp-down (Article C5.12) — the FIA's rate-limited power
# reduction mechanics, in force for every round of the season (verified
# directly: every PU Information document from R01 onward publishes its own
# Article C5.12.8 "Maximum PU Power Reduction Rate" table).
INITIAL_POWER_CUT_MAX = 150_000  # W — C5.12.4: max instantaneous cut allowed at the start of a power-limited-pending period (350kW -> as low as 200kW)
INITIAL_HOLD_SECONDS = 1.0       # C5.12.4: the reduced level must then hold fixed for at least this long before any further reduction

# C5.12.6: while ERS-K power is above 100kW, further reduction beyond the
# initial cut+hold is rate-limited to one of two tiers, chosen by that
# circuit's "Power Limited Distance" vs this threshold. The FIA publishes
# BOTH values per event (parser.py extracts them into overrides.json's
# power_reduction block), so the real published rate is used directly when
# available and this threshold only derives it as a fallback — the two agree
# on all 12 circuits checked, including Japan at 3472m (just under) and
# Australia at 3518m (just over).
POWER_LIMITED_DISTANCE_THRESHOLD_M = 3500
RAMP_DOWN_RATE_LONG_PLD_W_PER_S = 50_000    # circuits where PLD > 3500m
RAMP_DOWN_RATE_SHORT_PLD_W_PER_S = 100_000  # circuits where PLD <= 3500m — also the fallback when a circuit's FIA doc hasn't been parsed yet, since ">3500m" is the regulation's stated exception rather than its default case

# C5.12.6 also caps *total* cumulative power reduction at 700kW — not
# enforced separately here since P_MGUK_MAX (350kW) is already well under
# that; the cap can never bind given this project's own absolute ceiling.


def resolve_ramp_down_rate_w_per_s(rate_limit_kw_per_s=None, power_limited_distance_m=None):
    """
    The per-circuit ramp-down rate (W/s) for Article C5.12.6, in order of
    preference:
      1. the rate the FIA published for this event (overrides.json's
         power_reduction.rate_limit_kw_per_s, via parser.py),
      2. derived from that event's published Power Limited Distance vs the
         3500m threshold,
      3. the 100kW/s default, for a circuit whose PU Information doc hasn't
         been fetched/parsed yet.
    """
    if rate_limit_kw_per_s is not None:
        return rate_limit_kw_per_s * 1000
    if power_limited_distance_m is not None and power_limited_distance_m > POWER_LIMITED_DISTANCE_THRESHOLD_M:
        return RAMP_DOWN_RATE_LONG_PLD_W_PER_S
    return RAMP_DOWN_RATE_SHORT_PLD_W_PER_S


def in_sector(distance_m, sectors, lap_length_m, race_only=True):
    """
    True if an absolute lap distance falls inside any of the given published
    sector windows. Windows can legitimately wrap past start/finish (Spain's
    Exit T14 reset sector is 4300-4700m on a 4657m lap), so both the position
    and the window are normalised onto the lap before comparing.

    race_only skips sectors the FIA brackets as Sprint-Qualifying/Qualifying
    only — those never apply to a Sprint or a Race.
    """
    if not sectors:
        return False
    position = distance_m % lap_length_m
    for sector in sectors:
        if race_only and sector.get("quali_only"):
            continue
        start = sector["start_m"] % lap_length_m
        end = sector["end_m"] % lap_length_m
        inside = start <= position <= end if start <= end else (position >= start or position <= end)
        if inside:
            return True
    return False


# How much of a permitted Article C5.12.5 reset a race driver actually takes,
# as a fraction of the headroom between the lap's sustainable floor and the
# full 350kW demand.
#
# The regulation permits a return to the full 350kW, but taking that at every
# reset is not affordable: at 1.0 the lap's energy budget cannot balance at
# all on Spa or Barcelona — the floor solver bottoms out at 0kW and the lap
# still ends ~1MJ down on where it started. Anything at or below ~0.6
# balances on all 14 cached circuits.
#
# This value CANNOT be calibrated against the FIA's published Power Limited
# Distance, which is the model's only external check: PLD counts distance
# where the driver asks for full power and gets less, so any partial restore
# still counts as limited and the figure doesn't move. Measured directly —
# mean |modelled - published| is identical (230m) for every fraction from 0.0
# to 0.6. It is therefore a modelling judgement, not a fitted value.
#
# 0.25 is chosen to make the published reset sectors visibly matter without
# degenerating the rest of the lap: it lifts in-zone power to roughly 2-3x the
# sustained floor (Australia 64->136kW, Britain 43->120kW, Spain 60->133kW,
# Spa 36->115kW) while keeping every circuit's baseline above 35kW. At 0.5 the
# bursts reach ~190kW but Spa's baseline collapses to 13kW.
RESET_RESTORE_FRACTION = 0.25


class PowerReductionState:
    """
    Tracks Article C5.12's power reduction across a lap.

    The regulations do NOT mandate a decay curve — every clause is a limit on
    how power may be taken away (C5.12.4: an initial cut of no more than
    150kW, held at least 1s; C5.12.6: no faster than 50 or 100 kW/s
    thereafter; C5.12.5: it may not be increased again until a permitted
    reset). What actually triggers a reduction lives in FIA-F1-DOC-058, which
    isn't public, so this model supplies the missing piece: the car reduces
    only as far as it must to make the lap's energy budget last, settling at
    a floor rather than decaying to zero.

    That floor is solved per lap (see solve_deploy_floor_w) — it is the
    driver-side question "what power level makes my energy last the lap."

    Crucially the reduction PERSISTS across corners and later straights: it
    is cleared only by passing through a published C5.12.5 reset sector. Ten
    of the fourteen circuits checked publish no race reset sectors at all, so
    on those the reduction, once applied, stands for the rest of the lap.

    Inside a reset sector power is restored part of the way back toward the
    full 350kW demand (see RESET_RESTORE_FRACTION) and the ratchet is held
    open until the car leaves; on the way out the usual C5.12.4 cut and
    C5.12.6 ramp bring it back down to the floor. Because the floor is solved
    for the lap as a whole, spending more inside the reset sectors buys a
    lower floor everywhere else rather than more energy overall — the lap's
    budget is unchanged either way.
    """

    def __init__(self, floor_w, rate_w_per_s, reset_sectors=None,
                 greater_reduction_sectors=None, lap_length_m=1.0):
        self.floor_w = floor_w
        self.rate_w_per_s = rate_w_per_s
        self.reset_sectors = reset_sectors or []
        self.greater_reduction_sectors = greater_reduction_sectors or []
        self.lap_length_m = lap_length_m

        self.reduction_w = 0.0      # how much has been taken off the 350kW demand
        self.hold_remaining_s = 0.0  # C5.12.4's mandatory >=1s hold after a cut
        self.reset_count = 0
        self.in_reset_zone = False
        self.limited_distance_m = 0.0  # how far the car ran reduced — compare against the FIA's published PLD

    @property
    def target_reduction_w(self):
        return max(P_MGUK_MAX - self.floor_w, 0.0)

    def available_w(self):
        """The driver's currently permitted maximum power demand."""
        return max(P_MGUK_MAX - self.reduction_w, 0.0)

    def observe(self, absolute_distance_m):
        """Call once per step, wherever the car is — Article C5.12.5's ratchet
        holds through corners too. Clears the reduction while the car is
        inside a permitted reset sector, and holds it cleared until the car
        leaves (otherwise the reduction would re-apply and re-clear on
        alternating steps for the whole width of the sector)."""
        inside = in_sector(absolute_distance_m, self.reset_sectors, self.lap_length_m)
        if inside:
            if not self.in_reset_zone:
                self.reset_count += 1
            # How much of the permitted reset the driver actually takes —
            # see RESET_RESTORE_FRACTION.
            restore_to = self.floor_w + RESET_RESTORE_FRACTION * max(
                P_MGUK_MAX - self.floor_w, 0.0)
            self.reduction_w = max(P_MGUK_MAX - restore_to, 0.0)
            self.hold_remaining_s = 0.0
        self.in_reset_zone = inside

    def tally_limited(self, step_distance_m):
        """Call once per DEPLOYING step. Accumulates distance run with the
        driver asking for full power and receiving less — which is what the
        FIA's published Power Limited Distance measures. Distance spent
        braking or coasting doesn't count, even though the reduction itself
        persists through it."""
        if self.reduction_w > 0:
            self.limited_distance_m += step_distance_m

    def advance(self, dt, absolute_distance_m):
        """Call once per deploying step. Grows the reduction toward its
        target, respecting the initial-cut ceiling, the 1s hold and the
        per-second rate limit — never exceeding what's needed for the floor,
        and never decreasing (C5.12.5's ratchet)."""
        if self.in_reset_zone:
            return  # a reset is in force here; power may climb back to max
        target = self.target_reduction_w
        if self.reduction_w >= target:
            return

        if self.reduction_w <= 0.0:
            # C5.12.4's initial step. Specified circuit sectors permit a
            # larger one than the usual 150kW (the FIA publishes the maximum
            # per sector, typically 350kW — i.e. the full cut at once).
            cut_max = INITIAL_POWER_CUT_MAX
            for sector in self.greater_reduction_sectors:
                if sector.get("quali_only"):
                    continue
                if in_sector(absolute_distance_m, [sector], self.lap_length_m):
                    cut_max = max(cut_max, (sector.get("max_reduction_kw") or 0) * 1000)
            self.reduction_w = min(cut_max, target)
            self.hold_remaining_s = INITIAL_HOLD_SECONDS
            return

        if self.hold_remaining_s > 0:
            self.hold_remaining_s -= dt
            return

        self.reduction_w = min(self.reduction_w + self.rate_w_per_s * dt, target)


# Real per-lap recharge limits come from the FIA's "Power Unit Information" doc,
# published shortly before each race weekend, and pulled live by the existing
# FIA-fetch feature in this project — NOT stored in circuits.json. Pass the
# real value straight into simulate_lap() as lap_recharge_limit_j once your
# fetch feature retrieves it for a given circuit/session.
#
# Used only as a fallback when no live value is available yet (e.g. testing,
# or a circuit whose FIA doc hasn't been published this season).
DEFAULT_RECHARGE_LIMIT_J = 8_500_000  # 8.5 MJ — modal value across circuits confirmed so far


def mj_to_j(value_mj):
    """Small helper: convert a recharge limit in MJ (as it appears in the FIA
    PDFs, e.g. 8.5) to Joules, for passing into simulate_lap()."""
    return value_mj * 1_000_000

MIN_CAR_MASS = 768          # kg, no fuel
MAX_RACE_FUEL = 70          # kg

CRR = 0.015                 # rolling resistance coefficient, generic estimate — not F1-specific/verified
G = 9.80665

# FastF1's Speed channel is whole-km/h and only updates every few samples —
# holding one rounded value for several consecutive rows, then jumping.
# Interpolating real_speed_kmh() directly off those raw samples would give
# the displayed speed trace a stair-step character it never really had.
# Upsampling onto this dense, uniform distance grid via a shape-preserving
# cubic (PCHIP) fit first — rather than plain linear interpolation — fills
# the gaps between real samples with a smooth curve instead. See
# _resample_telemetry() in simulate_lap().
TELEMETRY_RESAMPLE_STEP_M = 2.0  # meters


def _resample_telemetry(telemetry, step_m=TELEMETRY_RESAMPLE_STEP_M):
    """Upsamples raw telemetry (Distance/Speed) onto a dense, uniform-distance
    grid via PCHIP interpolation — see TELEMETRY_RESAMPLE_STEP_M for why.
    PCHIP is shape-preserving (no overshoot beyond neighboring sample
    values), so it fills the gaps between real samples without inventing
    speeds the telemetry never recorded. Returns (distance_m, speed_kmh)
    arrays on the fine grid.
    """
    raw_distance = telemetry["Distance"].to_numpy()
    _, unique_idx = np.unique(raw_distance, return_index=True)
    unique_idx.sort()
    raw_distance = raw_distance[unique_idx]
    raw_speed = telemetry["Speed"].to_numpy()[unique_idx]

    fine_distance = np.arange(raw_distance[0], raw_distance[-1], step_m)
    speed_kmh = PchipInterpolator(raw_distance, raw_speed)(fine_distance)
    return fine_distance, speed_kmh


# Scenario definitions (locked in earlier).
#
# NOTE on soc_start_j: these are ASSUMPTIONS about where in the permitted SOC
# window (see ES_SOC_SWING_LIMIT_J) a lap begins — not regulatory values.
# Nothing in the FIA regulations fixes a starting state of charge; a real
# car's SOC at any given lap is a team strategy outcome. "Top of the window"
# for a qualifying lap and "mid-window" for steady-state race pace are
# plausible stand-ins, chosen for lack of real per-lap SOC telemetry (FastF1
# doesn't expose it), and they materially affect how early deployment runs
# out — worth revisiting if real SOC data ever becomes available.
SCENARIOS = {
    "qualifying": {
        "mass_kg": MIN_CAR_MASS + 5,                # near-empty tank, small reserve
        "soc_start_j": ES_SOC_SWING_LIMIT_J,        # assumed to start at the top of the window
        # One flat-out lap with nothing held back: spend the window down to
        # empty. See solve_deploy_floor_w.
        "target_end_soc_j": 0.0,
        "lift_and_coast": False,                    # quali is one flat-out lap — no fuel-saving technique needed
        # Qualifying is a "Lap Time Classified Session" (LTCS), where
        # Article B7.2.2 enables Overtake Override Mode for the ENTIRE
        # session with no proximity gating — so the Overtake power curve
        # applies for the whole lap. This is the regulation, not an
        # assumption.
        "overtake_active": True,
    },
    "race_pace": {
        "mass_kg": MIN_CAR_MASS + 35,               # half of 70kg race fuel burned
        "soc_start_j": ES_SOC_SWING_LIMIT_J * 0.5,  # assumed to start mid-window
        # Steady state: a race lap must end on the charge it began with, or
        # the driver runs out within a handful of laps. This is what forces
        # the Article C5.12 power reduction. See solve_deploy_floor_w.
        "target_end_soc_j": ES_SOC_SWING_LIMIT_J * 0.5,
        "lift_and_coast": False,                    # see note below — disabled pending a per-corner model
        # A race is a "Total Time Classified Session" (TTCS), where Overtake
        # is proximity-gated (B7.2.3): it activates at the Activation Line
        # only if the car was within the Detection Gap of another car at the
        # Detection Line, and is disabled entirely under Safety Car. None of
        # that is knowable from single-car, single-lap telemetry, so the
        # default models a car in clean air. Override per-call via
        # run_lap_simulation(overtake_active=True) to see the attacking case.
        "overtake_active": False,
    },
}

# Lift-and-coast infrastructure (the `lift_and_coast` flag and
# LIFT_AND_COAST_FRACTION below) is kept in the code but OFF by default.
# Testing showed a blanket "coast for the last X% of every straight" rule
# hurts more than it helps — real drivers use LiCo selectively on specific
# corners chosen by the team's fuel-saving plan, not uniformly on every
# straight in the lap. Applying it everywhere badly undershot the long
# straights, where real telemetry shows cars pushing hard almost to the end.
# Revisit this as a per-corner flag (driven by real strategy data, if
# available) rather than a lap-wide constant.
LIFT_AND_COAST_FRACTION = 0.3


# ---------------------------------------------------------------------------
# 2. AERO PROFILES (from circuits.json)
# ---------------------------------------------------------------------------

def load_circuits(path="circuits.json"):
    with open(path) as f:
        return json.load(f)


def get_aero_coeffs(circuits_data, track_key, mode="corner"):
    """mode: 'corner' or 'straight'"""
    circuit = circuits_data["circuits"][track_key]
    level = circuit["downforce_level"]
    profile = circuits_data["downforce_level"][level]

    cda = profile["CdA_corner"] if mode == "corner" else profile["CdA_straight"]
    return {"CdA": cda, "ClA": profile["ClA"]}


# ---------------------------------------------------------------------------
# 3. AIR DENSITY (elevation + session temperature)
# ---------------------------------------------------------------------------

R_SPECIFIC = 287.05  # J/(kg*K), dry air
P0 = 101_325         # Pa, sea level standard
M_AIR = 0.0289644    # kg/mol
R_GAS = 8.314        # J/(mol*K)
T0 = 288.15          # K, standard temp


def pressure_at_elevation(elevation_m):
    return P0 * math.exp(-M_AIR * G * elevation_m / (R_GAS * T0))


def air_density(elevation_m, air_temp_c):
    p = pressure_at_elevation(elevation_m)
    t_kelvin = air_temp_c + 273.15
    return p / (R_SPECIFIC * t_kelvin)


# ---------------------------------------------------------------------------
# 4. CORNER / STRAIGHT EXTRACTION (from FastF1 telemetry)
# ---------------------------------------------------------------------------

MIN_BRAKING_DISTANCE_M = 4.0    # brake-signal blips shorter than this are noise/trail-braking flicker, not real corners
MIN_LIFTOFF_DISTANCE_M = 30.0   # minimum distance for a throttle-lift zone to count as a real deceleration event
LIFTOFF_THROTTLE_THRESHOLD = 20  # below this, driver isn't meaningfully accelerating — real partial lifts rarely go fully to 0%
MIN_LIFTOFF_SPEED_LOSS_KMH = 3.0  # filters out full-throttle measurement noise (e.g. 313->312 km/h) from being misread as a lift-off event
COAST_THROTTLE_THRESHOLD = 2.0  # % — near-zero, not the LIFTOFF one above: this marks genuine "no input at all" coasting, not "not yet at full throttle"


def _find_liftoff_zones(telemetry, brake_corners, min_liftoff_distance_m):
    """
    Finds throttle-lift-only deceleration zones (Throttle == 0, no brake
    pedal) hiding inside the gaps between brake-based corners. These are
    real corners in terms of energy recovery — releasing the throttle
    engages MGU-K engine-braking/derate, charging the battery, even with
    zero mechanical brake input — but a Brake-signal-only detector is
    structurally blind to them. Real-world example: drivers can complete
    close to half a lap at some circuits without ever touching the brake
    pedal, using lift-and-coast-style throttle release through medium-speed
    corners instead.
    """
    liftoff_corners = []
    lap_length = float(telemetry["Distance"].max())
    n = len(brake_corners)

    if n == 0:
        gaps = [(0.0, lap_length)]
    else:
        gaps = []
        for i in range(n):
            start = brake_corners[i]["exit_distance_m"]
            end = brake_corners[(i + 1) % n]["entry_distance_m"]
            if i == n - 1:
                end = lap_length + end  # wraps past start/finish
            gaps.append((start, end))

    for start, end in gaps:
        if end > lap_length:
            # Wrapped zone: concatenation order (end-of-lap chunk, then
            # start-of-lap chunk) IS the correct chronological order already —
            # re-sorting by raw Distance would incorrectly reverse it, since
            # the start-of-lap chunk has lower Distance values despite coming
            # later in time. Preserve order, don't sort.
            part1 = telemetry[telemetry["Distance"] >= start].sort_values("Distance").copy()
            part2 = telemetry[telemetry["Distance"] <= (end - lap_length)].sort_values("Distance").copy()
            part1["DistanceUnwrapped"] = part1["Distance"]
            part2["DistanceUnwrapped"] = part2["Distance"] + lap_length  # continues past the wrap point
            zone = pd.concat([part1, part2]).reset_index(drop=True)
        else:
            zone = telemetry[(telemetry["Distance"] >= start) & (telemetry["Distance"] <= end)].copy()
            zone = zone.sort_values("Distance").reset_index(drop=True)
            zone["DistanceUnwrapped"] = zone["Distance"]
        if len(zone) < 2:
            continue

        is_lift = zone["Throttle"] <= LIFTOFF_THROTTLE_THRESHOLD
        lift_starts = zone.index[is_lift & ~is_lift.shift(1, fill_value=False)]
        lift_ends = zone.index[~is_lift & is_lift.shift(1, fill_value=False)]

        for s_idx in lift_starts:
            matching_ends = lift_ends[lift_ends > s_idx]
            e_idx = matching_ends[0] if len(matching_ends) else zone.index[-1]

            entry_idx = max(s_idx - 1, 0)
            entry_row = zone.iloc[entry_idx]
            exit_row = zone.iloc[e_idx]
            run = zone.iloc[entry_idx:e_idx + 1]
            apex_row = run.loc[run["Speed"].idxmin()]

            distance = apex_row["DistanceUnwrapped"] - entry_row["DistanceUnwrapped"]
            if distance < min_liftoff_distance_m:
                continue
            speed_loss = entry_row["Speed"] - apex_row["Speed"]
            if speed_loss < MIN_LIFTOFF_SPEED_LOSS_KMH:
                # Either no real deceleration, or a tiny fluctuation/noise
                # (e.g. 313->312 km/h at full throttle) rather than a genuine
                # lift-off event.
                continue

            liftoff_corners.append({
                "entry_distance_m": entry_row["Distance"],
                "entry_speed_kmh": entry_row["Speed"],
                "apex_distance_m": apex_row["Distance"],
                "apex_speed_kmh": apex_row["Speed"],
                "exit_distance_m": exit_row["Distance"],
                "exit_speed_kmh": exit_row["Speed"],
                "braking_distance_m": distance,
                "corner_type": "liftoff",
            })

    return liftoff_corners


def extract_lap_segments(telemetry, min_straight_length_m=150,
                          min_braking_distance_m=MIN_BRAKING_DISTANCE_M,
                          min_liftoff_distance_m=MIN_LIFTOFF_DISTANCE_M):
    """
    telemetry: DataFrame with columns Distance, Speed, Brake (from FastF1,
    already run through .add_distance()).

    Returns (corners_df, straights_df).
    """
    telemetry = telemetry.reset_index(drop=True)

    brake_starts = telemetry.index[
        (telemetry["Brake"] == True) & (telemetry["Brake"].shift(1, fill_value=False) == False)
    ]
    brake_ends = telemetry.index[
        (telemetry["Brake"] == False) & (telemetry["Brake"].shift(1, fill_value=False) == True)
    ]

    corners_data = []
    for start_idx in brake_starts:
        matching_ends = brake_ends[brake_ends > start_idx]
        if len(matching_ends) == 0:
            continue
        end_idx = matching_ends[0]

        zone = telemetry.iloc[start_idx:end_idx + 1]
        entry_row = telemetry.iloc[max(start_idx - 1, 0)]
        apex_row = telemetry.loc[zone["Speed"].idxmin()]
        exit_row = telemetry.iloc[end_idx]

        braking_distance = apex_row["Distance"] - entry_row["Distance"]
        if braking_distance < min_braking_distance_m:
            # Trail-braking flicker / brake-signal noise, not a real corner —
            # skip entirely so the straight before and after merges naturally
            # into one continuous segment, rather than splitting on a phantom
            # braking zone that never actually happened.
            continue
        if entry_row["Speed"] - apex_row["Speed"] < MIN_LIFTOFF_SPEED_LOSS_KMH:
            # Brake signal flickered during acceleration/steady speed, or
            # only a tiny real change — no genuine deceleration event.
            continue

        corners_data.append({
            "entry_distance_m": entry_row["Distance"],
            "entry_speed_kmh": entry_row["Speed"],
            "apex_distance_m": apex_row["Distance"],
            "apex_speed_kmh": apex_row["Speed"],
            "exit_distance_m": exit_row["Distance"],
            "exit_speed_kmh": exit_row["Speed"],
            "braking_distance_m": braking_distance,
            "corner_type": "braking",
        })

    # Find throttle-lift-only corners hiding inside the gaps between the
    # brake-based corners above, then merge everything into one distance-
    # ordered sequence — this is what lets a corner taken purely by lifting
    # (no brake pedal at all) still get picked up as a real energy-recovery
    # opportunity, instead of disappearing into what would otherwise be
    # treated as one long straight.
    liftoff_corners = _find_liftoff_zones(telemetry, corners_data, min_liftoff_distance_m)
    corners_data = sorted(corners_data + liftoff_corners, key=lambda c: c["entry_distance_m"])

    corners_df = pd.DataFrame(corners_data)

    straights_data = []
    lap_length = telemetry["Distance"].max()

    for i in range(len(corners_df)):
        current = corners_df.iloc[i]
        nxt = corners_df.iloc[(i + 1) % len(corners_df)]

        wraps = (i == len(corners_df) - 1)
        end_distance = (lap_length + nxt["entry_distance_m"]) if wraps else nxt["entry_distance_m"]

        current_exit = current["exit_distance_m"]
        if current_exit < current["entry_distance_m"]:
            # This corner's own span wrapped past the start/finish line
            # (possible with longer liftoff zones) — its exit_distance is a
            # small post-wrap value despite being chronologically later, so
            # unwrap it before computing straight length.
            current_exit += lap_length
        length = end_distance - current_exit

        coast_length_m = _measure_coast_length(
            telemetry, current["exit_distance_m"], length, lap_length
        )

        straights_data.append({
            "straight_after_corner": i,
            "start_distance_m": current["exit_distance_m"],
            "entry_speed_kmh": current["exit_speed_kmh"],
            "end_distance_m": end_distance,
            "exit_speed_kmh": nxt["entry_speed_kmh"],
            "length_m": length,
            "coast_length_m": coast_length_m,
            "segment_type": "deployment_straight" if length >= min_straight_length_m else "connector",
        })

    straights_df = pd.DataFrame(straights_data)
    return corners_df, straights_df


def _measure_coast_length(telemetry, start_distance_m, length_m, lap_length_m,
                           throttle_threshold=COAST_THROTTLE_THRESHOLD):
    """
    Real distance from the start of a straight to the first point the driver
    applies genuine throttle, per telemetry — not simulated, since we have
    the actual signal for exactly this span. Brake releases well before a
    driver is back on the throttle (a real coast through/past the apex), and
    without this the energy model was treating that whole gap as active
    MGU-K deployment starting the instant the brake came off.

    Distinct from LIFTOFF_THROTTLE_THRESHOLD (used to detect a lift-off
    corner in the first place) — this threshold marks "no input at all",
    not "not yet at full throttle".
    """
    start = start_distance_m % lap_length_m
    end = start + length_m

    if end <= lap_length_m:
        zone = telemetry[(telemetry["Distance"] >= start) & (telemetry["Distance"] < end)]
        zone = zone.sort_values("Distance")
        rel_distance = zone["Distance"] - start
    else:
        part1 = telemetry[telemetry["Distance"] >= start].sort_values("Distance")
        part2 = telemetry[telemetry["Distance"] < (end - lap_length_m)].sort_values("Distance")
        rel_distance = pd.concat([
            part1["Distance"] - start,
            part2["Distance"] + (lap_length_m - start),
        ])
        zone = pd.concat([part1, part2])

    if len(zone) == 0:
        return 0.0

    above = (zone["Throttle"] > throttle_threshold).to_numpy()
    if not above.any():
        return float(length_m)  # never sees real throttle — coasts the whole straight (short ones, rarely)

    first_idx = above.argmax()
    return float(min(max(rel_distance.to_numpy()[first_idx], 0.0), length_m))


# ---------------------------------------------------------------------------
# 5. THE SIMULATION
# ---------------------------------------------------------------------------

# The MGU-K sits on the crankshaft and so recovers only through the REAR
# axle — whatever the front brakes dissipate is lost as heat no matter how
# gently or hard the driver brakes. Brake bias in F1 is always front-biased
# and drivers adjust it continuously through a lap (and across a session), so
# no exact per-corner figure is knowable; 58% front is used as a
# representative median, leaving this much at the rear.
#
# This replaced an earlier "braking force fraction" curve that scaled regen
# between 30% and 100% of P_MGUK_MAX according to how hard the driver braked.
# That curve double-counted the MGU-K's own power ceiling and ran backwards:
# measured against real telemetry, 62% of braking corners already demand more
# than 350kW (median 505kW), so the ceiling alone determines the outcome in
# most corners — while in the GENTLE corners it wrongly cut recovery to ~30%
# of 350kW, when a light brake application is exactly the case the MGU-K can
# absorb in full.
REAR_BRAKE_SHARE = 0.42


def corner_regen_cap_w(corner_type, braking_power_w=None):
    """
    The MGU-K recovery ceiling for one corner, in Watts.

    Braking corners: whichever is lower of the MGU-K's own 350kW limit and
    the rear axle's share of the braking power actually being dissipated.
    Hard stops overrun the MGU-K and hand the surplus to the friction brakes;
    light ones sit entirely within it and are recovered in full.

    Liftoff corners: the flat 200kW lift-and-coast rate. There is no wheel
    braking to divide there — the recovery comes off the crankshaft as the
    driver lifts — so the rear-axle share doesn't apply.
    """
    if corner_type != "braking":
        return P_LIFT_COAST_MAX
    if braking_power_w is None:
        return P_MGUK_MAX
    return min(P_MGUK_MAX, max(braking_power_w, 0.0) * REAR_BRAKE_SHARE)


# Corners are computed closed-form rather than stepped, so their span is
# walked in these increments purely to check for published reset sectors
# (mostly corner-exit windows) and to tally power-limited distance. Not a
# regulatory value.
RESET_SCAN_STEP_M = 10.0

# Pedal position at or above which the driver counts as asking for everything
# the power unit can give. Used for reporting how much of the lap runs
# power-limited, so it can be compared against the FIA's published figure.
FULL_THROTTLE_FRACTION = 0.95

# Not a regulatory value — purely how finely the per-step trace captured
# below (for the speed/deployment-power visualization) is sampled. Doesn't
# touch any of the physics above; every rule still runs at the real dt.
TRACE_SAMPLE_EVERY_N_STEPS = 4  # ~0.2s at dt=0.05s


def simulate_lap(corners_df, straights_df, mass_kg, soc_start_j,
                  aero_corner, aero_straight, rho,
                  lap_recharge_limit, lap_length_m, telemetry,
                  soc_swing_limit=ES_SOC_SWING_LIMIT_J,
                  lift_and_coast=False,
                  overtake_active=False,
                  ramp_down_rate_w_per_s=RAMP_DOWN_RATE_SHORT_PLD_W_PER_S,
                  deploy_floor_w=P_MGUK_MAX,
                  reset_sectors=None,
                  greater_reduction_sectors=None,
                  alt_curve_sectors=None,
                  reduction=None,
                  dt=0.05):
    """
    Runs one full lap through the corner/straight sequence, tracking SOC
    continuously. Returns a list of per-segment results.

    Deployment on straights is capped only by its own decay curve
    (P_MGUK_MAX decaying at DEPLOY_DECAY_RATE) and by available battery
    charge — not by a separate pre-allocated per-straight energy budget.
    An earlier version rationed a lap-wide "recoverable energy" estimate
    across straights by length, to stop the car blowing the whole battery
    on the first straight it reached; that's no longer needed (or correct)
    now that deployment already self-limits to ~1.2 MJ per continuous run
    via the decay curve alone, nowhere near the 4MJ battery cap — and it
    was actively causing straights to cut deployment short well before the
    decay curve would, handing the remaining time to unopposed superclip
    regen and inflating how often the battery sat pinned at full.

    aero_corner / aero_straight: dicts with 'CdA' (m^2) and 'ClA' (m^2).

    telemetry: the same DataFrame passed into run_lap_simulation() — real
    ground truth for what the car actually did. On a straight, speed is read
    directly from telemetry (interpolated by absolute lap distance; see
    real_speed_kmh() below) instead of being independently re-simulated from
    a force-balance model. This is what makes speed impossible to overshoot:
    it's never computed, only looked up. Deployment POWER, separately, is a
    regulatory-max assumption, not derived from this speed (see deploy_power
    in the drive-phase loop below). Corners don't need any of this —
    extract_lap_segments() already builds their entry/apex/exit speeds
    directly from telemetry.

    ramp_down_rate_w_per_s: this circuit's Article C5.12.6 power-reduction
    rate (see deploy_decay_cap_w) — run_lap_simulation() resolves it from
    the FIA-published figures before calling this function.
    """

    # Ground truth for a straight's speed trace — see the telemetry param
    # note above. Raw telemetry is upsampled first (see _resample_telemetry /
    # TELEMETRY_RESAMPLE_STEP_M) so the coarse, quantized Speed channel
    # doesn't give the trace a stair-step character it never really had.
    _tel_distance_m, _tel_speed_kmh = _resample_telemetry(telemetry)

    def real_speed_kmh(absolute_distance_m):
        return float(np.interp(absolute_distance_m % lap_length_m, _tel_distance_m, _tel_speed_kmh))

    # Real throttle position, by distance. A straight's "drive phase" begins
    # the moment the driver touches the throttle at all (that's what
    # _measure_coast_length detects), but touching the throttle is not the
    # same as demanding full power — a driver feeding it in through a corner
    # exit is asking for a fraction of it. Article C5.12.1 requires torque
    # demand to rise monotonically with pedal position, so pedal position is
    # the demand signal, and deployment is scaled by it below.
    _raw_distance = telemetry["Distance"].to_numpy()
    _raw_throttle = telemetry["Throttle"].to_numpy()
    _throttle_order = np.argsort(_raw_distance)

    def real_throttle_fraction(absolute_distance_m):
        pct = float(np.interp(
            absolute_distance_m % lap_length_m,
            _raw_distance[_throttle_order], _raw_throttle[_throttle_order],
        ))
        return min(max(pct, 0.0), 100.0) / 100.0

    # Typical achievable braking deceleration, computed from this lap's own
    # corners (not an arbitrary assumed G-force) — used below to cap how fast
    # each straight is allowed to accelerate to, based on whether the corner
    # it leads into can actually absorb that speed in its real braking zone.
    # Without this, a short straight before a tight corner will happily
    # accelerate the car to a speed no real driver would carry there, since
    # that speed just gets thrown away as wasted braking effort anyway.
    braking_rates = []
    for _, c in corners_df.iterrows():
        d = c["braking_distance_m"]
        v_e = c["entry_speed_kmh"] / 3.6
        v_a = c["apex_speed_kmh"] / 3.6
        if d > 0 and v_e > v_a:
            braking_rates.append((v_e**2 - v_a**2) / (2 * d))
    avg_a_brake = sum(braking_rates) / len(braking_rates) if braking_rates else 10.0

    soc = soc_start_j
    lap_recharge_used = 0.0
    results = []

    # Total electrical energy deployed this lap, capped against the same
    # regulatory per-lap MJ figure that already bounds recharge (the FIA's
    # "Maximum Recharge per lap" table, C5.2.10) — real per-lap electrical
    # deployment is bounded by the same energy budget: a driver can't keep
    # cycling the battery through more deploy/regen swings than the
    # regulations allow it to be topped up by, lap after lap. Once hit, no
    # more deployment happens on this or any later straight this lap — the
    # car coasts on ICE alone for the rest of every remaining straight (see
    # lap_deploy_capped below).
    lap_deploy_used = 0.0
    lap_deploy_capped = False

    # Article C5.12's power reduction is lap-wide state, not per-straight —
    # it ratchets through corners and later straights until a published reset
    # sector clears it. See PowerReductionState. The caller may pass one in
    # (run_lap_simulation does, so it can read the resulting power-limited
    # distance back out and compare it against the FIA's published figure).
    if reduction is None:
        reduction = PowerReductionState(
            floor_w=deploy_floor_w,
            rate_w_per_s=ramp_down_rate_w_per_s,
            reset_sectors=reset_sectors,
            greater_reduction_sectors=greater_reduction_sectors,
            lap_length_m=lap_length_m,
        )

    n_segments = len(corners_df)  # straights_df[i] follows corners_df[i], by construction

    for i in range(n_segments):
        # --- Corner i ---
        corner = corners_df.iloc[i]
        v_entry = corner["entry_speed_kmh"] / 3.6
        v_apex = corner["apex_speed_kmh"] / 3.6
        d_brake = corner["braking_distance_m"]

        # Full entry-to-exit span of this corner, not just the braking zone —
        # this is what the frontend uses to place this segment's colour at
        # its true position/width along the track, instead of assuming all
        # segments are equal-length (they're wildly not: a 40m hairpin and a
        # 800m straight are both "one segment").
        corner_length_m = corner["exit_distance_m"] - corner["entry_distance_m"]
        if corner_length_m < 0:
            corner_length_m += lap_length_m  # this corner's span wrapped past start/finish

        if d_brake <= 0 or v_entry <= v_apex:
            results.append({
                "type": "corner",
                "E_regen_j": 0.0,
                "soc_after_j": soc,
                "start_distance_m": corner["entry_distance_m"],
                "length_m": corner_length_m,
                "note": "no braking detected",
                # No dedicated dt-stepped loop for corners (regen is a
                # closed-form estimate, not a simulated trajectory), so this
                # is just the entry/exit points rather than a fine trace —
                # still enough for a continuous lap-wide speed/SOC line.
                # soc_j is flat here since no regen happens at all.
                "trace": [
                    {"distance_m": 0.0, "speed_kmh": corner["entry_speed_kmh"], "soc_j": soc},
                    {"distance_m": corner_length_m, "speed_kmh": corner["exit_speed_kmh"], "soc_j": soc},
                ],
            })
        else:
            dKE = 0.5 * mass_kg * (v_entry**2 - v_apex**2)
            a_brake = (v_entry**2 - v_apex**2) / (2 * d_brake)
            t_brake = (v_entry - v_apex) / a_brake if a_brake > 0 else 0

            avg_v = (v_entry + v_apex) / 2
            E_aero = 0.5 * rho * aero_corner["CdA"] * avg_v**2 * d_brake
            E_roll = CRR * mass_kg * G * d_brake

            E_regen_potential = max(dKE - E_aero - E_roll, 0)
            # What the braking system itself has to dissipate: the kinetic
            # energy shed, less what drag and rolling resistance took care
            # of, spread over the braking zone's duration. The MGU-K can take
            # the rear axle's share of that, up to its own ceiling — see
            # corner_regen_cap_w.
            braking_power_w = E_regen_potential / t_brake if t_brake > 0 else 0.0
            regen_cap_w = corner_regen_cap_w(corner.get("corner_type", "braking"), braking_power_w)
            E_regen = min(
                regen_cap_w * t_brake,
                E_regen_potential,
                soc_swing_limit - soc,
                lap_recharge_limit - lap_recharge_used,
            )
            E_regen = max(E_regen, 0)

            soc_before_corner = soc
            soc += E_regen
            lap_recharge_used += E_regen

            apex_offset_m = min(max(corner["apex_distance_m"] - corner["entry_distance_m"], 0.0), corner_length_m)
            results.append({
                "type": "corner",
                "corner_type": corner.get("corner_type", "braking"),
                "dKE_j": dKE,
                "E_aero_j": E_aero,
                "E_roll_j": E_roll,
                "E_regen_j": E_regen,
                "soc_after_j": soc,
                "start_distance_m": corner["entry_distance_m"],
                "length_m": corner_length_m,
                # Regen happens over the braking zone (entry -> apex, where
                # speed bottoms out) in one closed-form lump, not a stepped
                # loop, so soc_j jumps from its pre-regen to post-regen value
                # at apex and stays there through the exit point.
                "trace": [
                    {"distance_m": 0.0, "speed_kmh": corner["entry_speed_kmh"], "soc_j": soc_before_corner},
                    {"distance_m": apex_offset_m, "speed_kmh": corner["apex_speed_kmh"], "soc_j": soc},
                    {"distance_m": corner_length_m, "speed_kmh": corner["exit_speed_kmh"], "soc_j": soc},
                ],
            })

        # Article C5.12.5's ratchet holds through the corner as well, and the
        # published reset sectors are mostly corner-exit windows ("Exit T3"),
        # so walk the corner's span in coarse steps rather than sampling only
        # its entry point — otherwise a reset sitting mid-corner is missed.
        if corner_length_m > 0:
            walked = 0.0
            while walked < corner_length_m:
                step = min(RESET_SCAN_STEP_M, corner_length_m - walked)
                reduction.observe(corner["entry_distance_m"] + walked)
                walked += step

        # --- Straight i (immediately follows corner i, by construction) ---
        straight = straights_df.iloc[i]
        start_distance_m = straight["start_distance_m"]
        length = straight["length_m"]
        coast_length = min(straight.get("coast_length_m", 0.0), length)
        aero = aero_straight if straight["segment_type"] == "deployment_straight" else aero_corner

        distance_covered = 0.0
        v = real_speed_kmh(start_distance_m) / 3.6

        # Coast phase (telemetry-measured, see _measure_coast_length): the
        # real gap between the brake coming off and the driver actually
        # getting back on the throttle — a real lift-and-coast, zero
        # throttle AND zero brake. No mechanical braking or deployment here,
        # but the crankshaft is still spinning down, so lift-and-coast regen
        # (capped at P_LIFT_COAST_MAX) still charges the battery. Regen here
        # is a flat rate regardless of speed, so — unlike the drive phase
        # below — there's no need to derive power from real motion at all;
        # real speed only feeds the trace/display.
        E_coast_regen = 0.0
        # Per-step (distance, speed, power) samples for the speed/deployment
        # visualization — purely observational, doesn't feed back into any
        # of the physics above.
        trace = []
        trace_step = 0
        while distance_covered < coast_length and v > 0.1:
            distance_covered += v * dt
            v = real_speed_kmh(start_distance_m + distance_covered) / 3.6

            regen_power = max(min(
                P_LIFT_COAST_MAX,
                (soc_swing_limit - soc) / dt,
                (lap_recharge_limit - lap_recharge_used) / dt,
            ), 0.0)
            regen_j = regen_power * dt
            soc += regen_j
            lap_recharge_used += regen_j
            E_coast_regen += regen_j

            trace_step += 1
            if trace_step % TRACE_SAMPLE_EVERY_N_STEPS == 0:
                trace.append({
                    "distance_m": distance_covered,
                    "speed_kmh": v * 3.6,
                    "deploy_power_w": 0.0,
                    "soc_j": soc,
                })

        E_deployed = 0.0
        coast_trigger_distance = length * (1 - LIFT_AND_COAST_FRACTION)

        # Distance into this straight (from its start, same origin as
        # coast_length above) where deployment actually stops — the battery
        # running dry, or this lap's deployment budget running out (see
        # lap_deploy_capped). Past that point the car coasts on ICE alone for
        # the rest of the straight — no regen substitutes for it, since
        # Recharge (C5.2.10) only happens via braking or coast, never
        # cruise-at-full-throttle (see this module's note on "superclipping"
        # near P_MGUK_MAX). Defaults to the full length when it never
        # happens, so the frontend can show the coasting tail as neutral
        # instead of painting the whole straight as one discharge zone.
        #
        # NOTE: reduced power is NOT the same as deployment ending — the car
        # keeps deploying at the reduced level (see PowerReductionState), so
        # this stays at `length` on a straight that merely runs power-limited.
        active_deploy_m = length
        deploy_ended = False

        while distance_covered < length and v > 0.1:
            past_coast_trigger = lift_and_coast and distance_covered >= coast_trigger_distance

            # Reset sectors and the reduced-distance tally are evaluated
            # everywhere the car travels, deploying or not — Article C5.12.5's
            # ratchet holds through corners too, and only a published reset
            # sector clears it.
            reduction.observe(start_distance_m + distance_covered)

            if past_coast_trigger or lap_deploy_capped:
                # Throttle AND deployment both off (real lift-and-coast), or
                # this lap's total deployment budget is already spent — the
                # car stays ICE-only for the rest of this straight, and every
                # remaining straight this lap, once lap_deploy_capped trips.
                energy_this_step = 0.0
                can_deploy = False
            else:
                can_deploy = soc > 0

                # A real driver at full throttle deploys everything the regs
                # and the battery allow; the real speed this produces (via
                # real_speed_kmh() above) is a consequence of that combined
                # ICE+MGU-K power, not a target to reverse-engineer
                # deployment from. (An earlier version derived deploy_power as
                # required_power minus ice_power — but near real cruising
                # speed, drag rises so steeply that this residual collapses to
                # near zero regardless of how much power actually produced
                # that speed, so lap-wide deployment came out a small fraction
                # of what real telemetry/HUDs show.)
                #
                # Three independent limits, whichever binds first:
                #   - the permitted power demand after Article C5.12's
                #     reduction (see PowerReductionState) — the reduction
                #     persists across segments and clears only at a published
                #     reset sector,
                #   - the speed-dependent ceiling of Article C5.2.8,
                #   - available charge and what's left of the lap's budget.
                # What the driver is actually asking for. Deploying the full
                # regulatory maximum while the pedal is half way down would
                # overstate both the energy used and how much of the lap runs
                # power-limited — the drive phase starts at the first touch of
                # throttle, not at full throttle.
                throttle = real_throttle_fraction(start_distance_m + distance_covered)

                # Article C5.2.8.iii's "Alt 1" curve applies only inside the
                # sectors the FIA designates for it, and only in a Race or
                # Sprint — never in Practice or Qualifying, where Overtake is
                # enabled session-wide instead (Article B7.2.2).
                alt_here = (not overtake_active) and in_sector(
                    start_distance_m + distance_covered, alt_curve_sectors, lap_length_m)

                deploy_power = max(min(
                    reduction.available_w() * throttle,
                    speed_deployment_ceiling_w(v * 3.6, overtake_active, alt_here),
                    soc / dt if can_deploy else 0.0,
                    (lap_recharge_limit - lap_deploy_used) / dt,
                ), 0.0)
                # Only count distance where the driver is genuinely asking for
                # everything and getting less — that is what the FIA's
                # published Power Limited Distance measures.
                if throttle >= FULL_THROTTLE_FRACTION:
                    reduction.tally_limited(v * dt)
                reduction.advance(dt, start_distance_m + distance_covered)

                energy_this_step = deploy_power * dt
                lap_deploy_used += energy_this_step
                if lap_deploy_used >= lap_recharge_limit:
                    lap_deploy_capped = True

            if not can_deploy and not deploy_ended:
                active_deploy_m = distance_covered
                deploy_ended = True

            distance_covered += v * dt
            v = real_speed_kmh(start_distance_m + distance_covered) / 3.6

            soc -= energy_this_step
            E_deployed += energy_this_step

            trace_step += 1
            if trace_step % TRACE_SAMPLE_EVERY_N_STEPS == 0:
                trace.append({
                    "distance_m": distance_covered,
                    "speed_kmh": v * 3.6,
                    "deploy_power_w": energy_this_step / dt,
                    "soc_j": soc,
                })

        results.append({
            "type": "straight",
            "segment_type": straight["segment_type"],
            "exit_speed_kmh": v * 3.6,
            "E_deployed_j": E_deployed,
            "E_coast_regen_j": E_coast_regen,
            "soc_after_j": soc,
            "start_distance_m": start_distance_m,
            "length_m": length,
            "coast_length_m": coast_length,
            "active_deploy_m": active_deploy_m,
            "trace": trace,
        })

    return results


# ---------------------------------------------------------------------------
# 6. TOP-LEVEL API — the single entry point for everything above
# ---------------------------------------------------------------------------

DEPLOY_FLOOR_SOLVE_ITERATIONS = 22  # bisection steps; ~80W resolution over the 0-350kW range


def solve_deploy_floor_w(final_soc_for_floor, target_end_soc_j):
    """
    Finds the sustained power floor that makes a lap's energy last.

    This is the one genuinely unknown quantity in the Article C5.12 model:
    the regulations cap how fast power may be taken away but never say what
    triggers a reduction (that lives in FIA-F1-DOC-058, which isn't public).
    What IS knowable is that the car must not spend more energy than it can
    sustain, so the reduction is exactly as deep as required and no deeper —
    the driver's own question, "what power level makes my energy last?"

    The target differs by scenario, and this is what sets it:
      - RACE PACE is steady state. A driver cannot drain the battery every
        lap, so the lap must end on the state of charge it started with —
        deployment equals what braking and coasting put back.
      - QUALIFYING is a single flat-out lap with nothing held back, so the
        target is an empty battery.

    Note the per-lap Recharge limit (C5.2.10) is NOT the binding budget
    within one lap: on an 8.5MJ circuit the car starts race pace on 2MJ and
    recovers ~2.5MJ, so charge availability binds first by a wide margin.
    That limit still applies as a hard cap inside simulate_lap.

    final_soc_for_floor(floor_w) -> the lap's final SOC in joules. Raising
    the floor deploys more and therefore ends lower, so the relationship is
    monotonic and plain bisection converges. If the car can run completely
    unreduced without dipping below target, no limiting is needed and
    P_MGUK_MAX is returned.
    """
    if final_soc_for_floor(P_MGUK_MAX) >= target_end_soc_j:
        return P_MGUK_MAX

    low, high = 0.0, float(P_MGUK_MAX)
    for _ in range(DEPLOY_FLOOR_SOLVE_ITERATIONS):
        mid = (low + high) / 2
        if final_soc_for_floor(mid) < target_end_soc_j:
            high = mid
        else:
            low = mid
    return low


def run_lap_simulation(telemetry, circuits_data, track_key, scenario_name,
                        recharge_limit_mj=None, elevation_m: float = 0, air_temp_c: float = 25,
                        rate_limit_kw_per_s=None, power_limited_distance_m=None,
                        overtake_active=None, sectors=None):
    """
    The one function to call for a full lap simulation. Wires together every
    piece built in this module: corner/straight extraction (with lift-off
    detection), aero lookup from circuits.json, air density from elevation +
    temperature, scenario mass/SOC/lift-and-coast settings, and simulate_lap()
    itself.

    Args:
        telemetry: DataFrame with columns Distance, Speed, Throttle, Brake,
            RPM, ElapsedSec (from FastF1 car data, already run through
            .add_distance() — see telemetry.py's REQUIRED_COLUMNS). RPM and
            ElapsedSec feed simulate_lap()'s real-motion-driven straight
            processing (real RPM for ICE power, real elapsed time for real
            acceleration) — not optional add-ons.
        circuits_data: parsed circuits.json (see load_circuits()).
        track_key: circuit key matching circuits.json, e.g. 'miami'.
        scenario_name: 'qualifying' or 'race_pace' (see SCENARIOS).
        recharge_limit_mj: real per-lap recharge limit in MJ, from the FIA's
            Power Unit Information doc for this circuit/session, pulled via
            the project's existing FIA-fetch feature. If not yet available
            (doc not published for this round yet), omit this and the
            DEFAULT_RECHARGE_LIMIT_J fallback (8.5 MJ) is used instead.
        elevation_m: circuit elevation, for air density.
        air_temp_c: session air temperature, for air density.
        rate_limit_kw_per_s / power_limited_distance_m: this event's
            Article C5.12.8 figures, as published in its FIA Power Unit
            Information doc and parsed into overrides.json's
            power_reduction block. Omit both and the deployment ramp-down
            falls back to the 100kW/s default — see
            resolve_ramp_down_rate_w_per_s.

    Returns:
        dict with 'corners_df', 'straights_df', 'segment_results' (raw
        per-segment output from simulate_lap), 'summary' (aggregated
        totals — see summarize_results()), and 'meta' (the resolved
        scenario/elevation/temp/recharge-limit inputs the sim actually ran
        with, since recharge_limit_mj may have fallen back to the default).
    """
    corners_df, straights_df = extract_lap_segments(telemetry)
    lap_length_m = telemetry["Distance"].max()

    aero_corner = get_aero_coeffs(circuits_data, track_key, mode="corner")
    aero_straight = get_aero_coeffs(circuits_data, track_key, mode="straight")
    rho = air_density(elevation_m, air_temp_c)

    recharge_limit_j = (
        mj_to_j(recharge_limit_mj) if recharge_limit_mj is not None
        else DEFAULT_RECHARGE_LIMIT_J
    )

    ramp_down_rate = resolve_ramp_down_rate_w_per_s(
        rate_limit_kw_per_s, power_limited_distance_m
    )

    scenario = SCENARIOS[scenario_name]
    # Each scenario carries the Overtake state its session type implies
    # (always-on in qualifying/LTCS per B7.2.2, off for a race car in clean
    # air) — an explicit argument overrides it, e.g. to show the attacking
    # case in a race. NOTE: the caller is responsible for passing the
    # matching recharge_limit_mj (the "Overtake active" column, +0.5MJ per
    # C5.2.10.iii) when forcing this on for a race; this function doesn't
    # re-derive the MJ figure.
    if overtake_active is None:
        overtake_active = scenario.get("overtake_active", False)

    sectors = sectors or {}
    reset_sectors = sectors.get("power_reduction_reset", [])
    greater_reduction_sectors = sectors.get("greater_reduction", [])
    alt_curve_sectors = sectors.get("alt_power_curve", [])

    def run(floor_w, state=None):
        return simulate_lap(
            corners_df, straights_df,
            mass_kg=scenario["mass_kg"],
            soc_start_j=scenario["soc_start_j"],
            aero_corner=aero_corner,
            aero_straight=aero_straight,
            rho=rho,
            lap_recharge_limit=recharge_limit_j,
            lap_length_m=lap_length_m,
            lift_and_coast=scenario["lift_and_coast"],
            overtake_active=overtake_active,
            telemetry=telemetry,
            ramp_down_rate_w_per_s=ramp_down_rate,
            deploy_floor_w=floor_w,
            reset_sectors=reset_sectors,
            greater_reduction_sectors=greater_reduction_sectors,
            alt_curve_sectors=alt_curve_sectors,
            reduction=state,
        )

    # Solve the one unknown — how far power must be reduced for this lap's
    # energy to last — then run once more with that floor, keeping the
    # reduction state so its power-limited distance can be reported and
    # compared against the FIA's published figure.
    target_end_soc_j = scenario.get("target_end_soc_j", 0.0)
    if callable(target_end_soc_j):
        target_end_soc_j = target_end_soc_j(scenario)

    def final_soc(floor_w):
        results = run(floor_w)
        return results[-1]["soc_after_j"] if results else 0.0

    deploy_floor_w = solve_deploy_floor_w(final_soc, target_end_soc_j)
    reduction = PowerReductionState(
        floor_w=deploy_floor_w,
        rate_w_per_s=ramp_down_rate,
        reset_sectors=reset_sectors,
        greater_reduction_sectors=greater_reduction_sectors,
        lap_length_m=lap_length_m,
    )
    segment_results = run(deploy_floor_w, state=reduction)

    modelled_pld = reduction.limited_distance_m
    return {
        "corners_df": corners_df,
        "straights_df": straights_df,
        "segment_results": segment_results,
        "summary": summarize_results(segment_results),
        "meta": {
            "scenario": scenario_name,
            "elevation_m": elevation_m,
            "air_temp_c": air_temp_c,
            "air_density_kg_m3": rho,
            "recharge_limit_mj": recharge_limit_j / 1_000_000,
            "soc_start_mj": scenario["soc_start_j"] / 1_000_000,
            "soc_swing_limit_mj": ES_SOC_SWING_LIMIT_J / 1_000_000,
            "lap_length_m": float(lap_length_m),
            "overtake_active": overtake_active,
            "ramp_down_rate_w_per_s": ramp_down_rate,
            # The FIA's published Power Limited Distance, and what this
            # simulation actually produced — the model's one external check.
            "power_limited_distance_m": power_limited_distance_m,
            "modelled_power_limited_distance_m": modelled_pld,
            "deploy_floor_w": deploy_floor_w,
            "power_reduction_resets": reduction.reset_count,
        },
    }


def soc_swing_j(segment_results):
    """
    The actual quantity Article C5.2.9 constrains: max SOC minus min SOC
    across the lap, in Joules. Read from the fine per-step traces (every
    segment's trace carries soc_j), not just segment boundaries, so a dip
    partway along a straight isn't missed.

    Returns 0.0 if no trace points exist at all (a degenerate lap). Note the
    regulation bounds this over the whole time the car is on track, not per
    lap — see this module's known limitations.
    """
    soc_values = [
        pt["soc_j"]
        for seg in segment_results
        for pt in seg.get("trace", [])
        if "soc_j" in pt
    ]
    if not soc_values:
        return 0.0
    return max(soc_values) - min(soc_values)


def summarize_results(segment_results):
    """Aggregates simulate_lap()'s per-segment output into headline totals."""
    corners = [r for r in segment_results if r["type"] == "corner"]
    straights = [r for r in segment_results if r["type"] == "straight"]

    braking_regen_j = sum(r["E_regen_j"] for r in corners if r.get("corner_type", "braking") == "braking")
    liftoff_regen_j = sum(r["E_regen_j"] for r in corners if r.get("corner_type") == "liftoff")
    coast_regen_j = sum(r["E_coast_regen_j"] for r in straights)

    total_regen_j = braking_regen_j + liftoff_regen_j + coast_regen_j
    total_deployed_j = sum(r["E_deployed_j"] for r in straights)

    return {
        "total_regen_mj": total_regen_j / 1e6,
        "total_deployed_mj": total_deployed_j / 1e6,
        "braking_regen_mj": braking_regen_j / 1e6,
        "liftoff_regen_mj": liftoff_regen_j / 1e6,
        "coast_regen_mj": coast_regen_j / 1e6,
        "final_soc_mj": segment_results[-1]["soc_after_j"] / 1e6 if segment_results else None,
        # Article C5.2.9's actual constraint (max - min SOC <= 4MJ). Reported
        # so it's visible rather than merely assumed — see soc_swing_j.
        "soc_swing_mj": soc_swing_j(segment_results) / 1e6,
        "num_corners": len(corners),
        "num_straights": len(straights),
        "num_liftoff_corners": sum(1 for r in corners if r.get("corner_type") == "liftoff"),
    }

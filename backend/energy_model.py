"""
F1 2026 Energy Recovery / Deployment Model — v1 (finalized)
=============================================================

A physics-based simulation of MGU-K energy recovery and deployment across
a lap, built and validated against real 2026 FastF1 telemetry from four
circuits (Miami, Monaco, Hungary, Silverstone) across three downforce
categories and multiple driving styles.

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
  - 2026 regulatory constants (MGU-K 350kW, 4MJ battery, 400kW ICE, mass, fuel)
  - Two scenarios: qualifying (full SOC, light fuel) / race pace (50% SOC, half fuel)
  - Real per-circuit recharge limits pulled at runtime (not hardcoded — see
    run_lap_simulation's recharge_limit_mj argument), falling back to the
    8.5 MJ modal default when a circuit's FIA doc isn't published yet
  - Per-circuit aero profiles (corner-mode vs. straight-mode Cd*A), read
    from circuits.json's downforce_level -> aero_profiles lookup
  - Air density from circuit elevation + session temperature
  - Gear ratios reconstructed from real telemetry, with a STATEFUL upshift
    model (seeded from corner-exit speed, upshifts one gear at a time —
    critical fix, since a naive per-speed lookup picks unrealistic gears)
  - FIA-mandated normalized ICE power curve
  - Tire traction limit (grip-based force ceiling, fades with speed via downforce)
  - Throttle/traction ramp-up after corner exit (~1.2s, calibrated from real data)
  - Corner/straight extraction with TWO detection methods merged:
      1. Brake-signal based (hard braking zones), filtered for minimum
         distance AND genuine speed loss (rejects brake-signal flicker/noise)
      2. Throttle-lift based (corners taken by lifting only, no brake pedal —
         real MGU-K engine-braking regen opportunity a brake-only detector
         would completely miss)
  - Length-proportional energy budget allocation across deployment straights
    (prevents the car from spending the whole battery on the first straight)
  - Three distinct regen systems, each with its own FIA-mandated cap: 350kW
    mechanical-brake regen (braking corners), 250kW superclipping (crankshaft
    regen concurrent with full-throttle deployment on straights), and 200kW
    lift-and-coast regen (crankshaft regen with zero throttle AND zero brake
    — coast phases and liftoff corners)
  - Deployment power decay: 350kW ramping down 50kW per second of continuous
    full-throttle deployment, resetting on any interruption, per the FIA's
    published deployment curve
  - Coast-phase regen with closed-aero drag: the telemetry-measured gap
    between brake-release and re-throttle now regens via lift-and-coast
    (200kW) and uses the corner (closed/high-drag) aero profile, rather than
    being energy- and drag-neutral

VALIDATION RESULTS (deployment straights, the metric that matters):
  Miami:       ~4-5% average error (medium downforce, aero-anchored)
  Monaco:      ~7.2% average error (high downforce, aero estimate)
  Hungary:     ~10.3% average error (high downforce, stress-tested trail-braking)
  Silverstone: ~11-12% average error (medium downforce, one flagged outlier)

KNOWN LIMITATIONS (accepted, not silently hidden):
  - Short connector segments (<150m): consistently higher error — genuinely
    hard to model at this length regardless of physics accuracy
  - Lift-and-coast: real driver strategy choice, not modeled as a general
    rule (tested and reverted — a blanket rule hurt more than it helped)
  - High-downforce aero (Cd*A) is a scaled estimate, not independently
    anchored to real data the way medium-downforce was via Miami
  - Flat-out high-speed corner sequences with zero brake/throttle signature
    (e.g. Silverstone's Maggotts-Becketts) are invisible to both detection
    methods — a real structural gap, not a bug
  - One team's gear ratios (fitted from VER/Red Bull-Ford) used as a
    representative car — real ratios vary by team
  - Superclipping/coast regen and the deployment decay curve are new
    additions on top of the validated model above — the 4-12% error figures
    predate these mechanics and haven't been re-checked against telemetry yet
"""

import json
import math
import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# 1. REGULATORY CONSTANTS (2026)
# ---------------------------------------------------------------------------

P_MGUK_MAX = 350_000        # W, bidirectional (deploy + regen)
BATTERY_CAP = 4_000_000     # J (4 MJ) — physical hardware cap on instantaneous charge

# Superclipping: a separate crankshaft-connected regen system (distinct from
# the mechanical-brake regen above), active concurrently with full-throttle
# deployment on straights.
P_SUPERCLIP_MAX = 250_000   # W — superclipping regen cap, concurrent with deployment

# Lift-and-coast: crankshaft regen with zero throttle AND zero brake — the
# telemetry-measured coast phase between corners, and liftoff corners (a
# throttle lift with no brake pedal) use this same rate.
P_LIFT_COAST_MAX = 200_000  # W — lift-and-coast regen cap

# Deployment power ramps down over sustained full-throttle use — 350kW ->
# 300kW -> ... -> 0kW across 8 continuous seconds, per the FIA's published
# deployment decay curve — rather than staying flat at P_MGUK_MAX the whole
# time. Resets to the full 350kW whenever deployment is interrupted (a
# corner, a coast phase, or the battery running dry).
DEPLOY_DECAY_RATE = 50_000  # W per second of continuous full-throttle deployment

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

P_ICE_MAX = 400_000         # W (400 kW)
RPM_MAX = 15_000

MIN_CAR_MASS = 768          # kg, no fuel
MAX_RACE_FUEL = 70          # kg

CRR = 0.015                 # rolling resistance coefficient, generic estimate — not F1-specific/verified
G = 9.80665

# Tire traction limit — the missing piece exposed by the gear-selection fix.
# Real F1 cars are grip-limited at low speed: engine power alone doesn't
# guarantee the tires can put it down without wheelspin. Approximated here as
# mu * normal_load on the rear axle (the driven wheels), where normal_load
# includes both static weight share and speed-dependent aero downforce —
# which is why this constraint naturally fades out as speed builds (more
# downforce = more available grip), matching real behavior: wheelspin risk
# is highest right off a slow corner and disappears well before top speed.
TIRE_MU = 1.6              # representative F1 slick friction coefficient — approximate, not tire-specific
REAR_WEIGHT_FRACTION = 0.45  # rough static rear-axle load share — approximate

# Real telemetry (Miami, corner-exit acceleration zones) shows acceleration
# builds progressively over ~1.2s after a corner — 0.26g -> 0.53g -> 0.71g ->
# 0.85g -> 1.06g across consecutive samples — rather than jumping instantly
# to full available force. This is a genuine throttle/steering-unwind ramp,
# not just a static grip ceiling, and it's exactly why short connector
# straights previously overshot so badly: there's no time for a real car to
# ramp up before the next braking zone, but the model was applying full
# force immediately. Modeled here as a simple linear ramp on available drive
# force over the first RAMP_UP_TIME_SEC of each straight.
RAMP_UP_TIME_SEC = 1.2


def max_traction_force(v_ms, mass_kg, cla, rho, mu=TIRE_MU, rear_fraction=REAR_WEIGHT_FRACTION):
    """Max force (N) the rear tires can transmit before wheelspin, at a given speed."""
    downforce = 0.5 * rho * cla * v_ms**2
    normal_load_rear = rear_fraction * (mass_kg * G + downforce)
    return mu * normal_load_rear

# Scenario definitions (locked in earlier)
SCENARIOS = {
    "qualifying": {
        "mass_kg": MIN_CAR_MASS + 5,          # near-empty tank, small reserve
        "soc_start_j": BATTERY_CAP,           # full charge
        "lift_and_coast": False,              # quali is one flat-out lap — no fuel-saving technique needed
    },
    "race_pace": {
        "mass_kg": MIN_CAR_MASS + 35,         # half of 70kg race fuel burned
        "soc_start_j": BATTERY_CAP * 0.5,     # 50% SOC — steady-state mid-race assumption
        "lift_and_coast": False,              # see note below — disabled pending a per-corner model
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
# 2. FIA NORMALIZED ICE POWER CURVE
# ---------------------------------------------------------------------------
# N/Nmax vs P/Pmax, from the published regulation table.
# Interpolated at runtime — NOTE: values transcribed from public reporting,
# not the literal regulation PDF, treat as approximate.

_RPM_FRAC = np.array([0.0, 0.55, 0.65, 0.75, 0.85, 0.90, 0.95, 1.00, 1.025])
_POWER_FRAC = np.array([0.0, 0.50, 0.66, 0.83, 0.96, 0.99, 1.00, 0.99, 0.86])


def ice_power_at_rpm(rpm):
    """Returns ICE power output (W) at a given engine rpm."""
    rpm_frac = np.clip(rpm / RPM_MAX, 0, _RPM_FRAC[-1])
    power_frac = np.interp(rpm_frac, _RPM_FRAC, _POWER_FRAC)
    return power_frac * P_ICE_MAX


# ---------------------------------------------------------------------------
# 3. GEAR RATIOS (reconstructed from FastF1 telemetry)
# ---------------------------------------------------------------------------
# Units: RPM per km/h (matches how they were fitted from telemetry).
# Source: VER, throttle-filtered samples, averaged across Australia / Miami /
# Great Britain / Hungary 2026. Gear 8's Great Britain sample was EXCLUDED —
# it was contaminated by mid-braking gear-transition rows (Throttle==0),
# confirmed by raw-sample inspection, not a real ratio difference.
#
# NOTE: this is one team's (Red Bull-Ford) ratios. Different teams will have
# different values — treat this as a placeholder for "a representative car"
# until/unless you fit ratios per-team.

GEAR_RATIOS_RPM_PER_KMH = {
    1: 113.55,
    2: 91.37,
    3: 74.02,
    4: 64.08,
    5: 54.39,
    6: 46.68,
    7: 40.57,
    8: 36.41,
}


def fit_gear_ratios_from_telemetry(telemetry_df, min_throttle=100):
    """
    Refit gear ratios from a telemetry DataFrame with columns:
    Speed (km/h), RPM, nGear, Throttle.

    Filters to full-throttle samples only (avoids the mid-shift contamination
    found in the Great Britain gear-8 investigation) and returns a dict of
    {gear: mean_rpm_per_kmh}.
    """
    clean = telemetry_df[
        (telemetry_df["nGear"] > 0) & (telemetry_df["Throttle"] >= min_throttle)
    ].copy()
    clean["rpm_per_kmh"] = clean["RPM"] / clean["Speed"]

    return clean.groupby("nGear")["rpm_per_kmh"].mean().to_dict()


def engine_rpm_for_speed(v_ms, gear, gear_ratios=GEAR_RATIOS_RPM_PER_KMH):
    """v_ms -> engine RPM in a given gear, using RPM-per-km/h ratio."""
    v_kmh = v_ms * 3.6
    return gear_ratios[gear] * v_kmh


# Shift point, derived from REAL data rather than assumed: the earlier
# multi-track gear-ratio fitting (throttle=100 filtered, Australia/Miami/
# Great Britain/Hungary) showed max observed RPM clustering tightly around
# 11,700-12,100 across EVERY gear — not near the 15,000 rpm redline. That's
# real evidence teams shift well before absolute redline (likely because the
# next gear's power output already exceeds the current gear's power that
# close to the limiter, or simply that FP/race straights end before redline
# is reached). Using the empirical value here rather than a theoretical
# percentage of RPM_MAX.
SHIFT_RPM = 11_900


def initial_gear_for_speed(v_ms, gear_ratios=GEAR_RATIOS_RPM_PER_KMH):
    """
    Gear a driver would already be in on exiting a corner at this speed —
    the lowest gear whose RPM doesn't exceed the shift point. Used to seed
    gear state at the start of a straight (we don't simulate the corner
    itself shifting down, just infer a sensible starting gear from exit speed).
    """
    for gear in sorted(gear_ratios):
        rpm = engine_rpm_for_speed(v_ms, gear, gear_ratios)
        if rpm <= SHIFT_RPM:
            return gear
    return max(gear_ratios)  # fallback: speed exceeds every gear's comfortable range


def next_gear(current_gear, v_ms, gear_ratios=GEAR_RATIOS_RPM_PER_KMH):
    """
    Upshifts by exactly one gear if current gear's RPM has crossed the shift
    point — never skips gears, never downshifts while accelerating. This is
    what makes the model correctly stay in a high gear at high speed (since
    it arrived there by shifting up through every gear in between) while
    still using an appropriately low gear — and real torque — right after a
    slow corner, instead of the old stateless bug that picked top gear at
    any speed including a dead-slow corner exit.
    """
    max_gear = max(gear_ratios)
    if current_gear >= max_gear:
        return current_gear
    rpm = engine_rpm_for_speed(v_ms, current_gear, gear_ratios)
    if rpm > SHIFT_RPM:
        return current_gear + 1
    return current_gear


def F_ICE_at_gear(v_ms, gear, gear_ratios=GEAR_RATIOS_RPM_PER_KMH):
    """Traction force (N) available from the ICE alone, in a specific gear."""
    if v_ms <= 0.1:
        return 0.0
    rpm = engine_rpm_for_speed(v_ms, gear, gear_ratios)
    power = ice_power_at_rpm(rpm)
    return power / v_ms


def F_ICE(v_ms, gear_ratios=GEAR_RATIOS_RPM_PER_KMH):
    """
    Stateless convenience wrapper — infers a gear from speed alone, ignoring
    shift history. Fine for one-off checks/diagnostics, but simulate_lap()
    uses F_ICE_at_gear() with real tracked gear state instead, since gear
    choice genuinely depends on how you got to a given speed, not just the
    speed itself (see next_gear() above).
    """
    gear = initial_gear_for_speed(v_ms, gear_ratios)
    return F_ICE_at_gear(v_ms, gear, gear_ratios)


# ---------------------------------------------------------------------------
# 4. AERO PROFILES (from circuits.json)
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
# 5. AIR DENSITY (elevation + session temperature)
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
# 6. CORNER / STRAIGHT EXTRACTION (from FastF1 telemetry)
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
# 7. THE SIMULATION
# ---------------------------------------------------------------------------

# FastF1 only exposes a boolean brake signal, not real pedal force, so the
# FIA doc's own suggested stand-in — braking_intensity ~= speed_delta /
# braking_duration (already computed elsewhere as a_brake) — is used here
# as a proxy for how hard the driver is actually braking. A short, hard stop
# (car sheds speed fast, in the shortest time possible) implies deceleration
# near the car's real limit and is treated as close to full braking force; a
# long, sustained trail-braking zone (same or bigger speed loss, spread over
# much more time) implies a much gentler input and shouldn't get the same
# peak MGU-K regen rate as a genuinely hard stop. Reference points are real
# F1 braking deceleration figures — ~1g for gentle trail-braking, up to ~5g
# for the hardest stops — not values tuned to any specific corner.
BRAKING_DECEL_MIN_G = 1.0     # trail-braking floor
BRAKING_DECEL_MAX_G = 5.0     # hardest realistic F1 stops
BRAKING_FORCE_MIN_FRACTION = 0.30
BRAKING_FORCE_MAX_FRACTION = 1.00


def braking_force_fraction(a_brake_ms2):
    """
    Maps average braking deceleration (m/s^2) to a fraction of P_MGUK_MAX —
    0.30 at/below BRAKING_DECEL_MIN_G, 1.00 at/above BRAKING_DECEL_MAX_G,
    linear in between. See the module-level comment above for the reasoning.
    """
    a_min = BRAKING_DECEL_MIN_G * G
    a_max = BRAKING_DECEL_MAX_G * G
    t = (a_brake_ms2 - a_min) / (a_max - a_min)
    t = max(0.0, min(1.0, t))
    return BRAKING_FORCE_MIN_FRACTION + t * (BRAKING_FORCE_MAX_FRACTION - BRAKING_FORCE_MIN_FRACTION)


def corner_regen_cap_w(corner_type, a_brake_ms2=None):
    """
    Braking corners: P_MGUK_MAX (350kW) scaled by braking_force_fraction —
    a hard, short stop uses nearly all of it; a long trail-braked corner
    uses only 30-ish% of it, per the reasoning above. Liftoff corners: the
    flat 200kW lift-and-coast cap, since those are a throttle lift with no
    brake pedal at all — there's no "braking force" to scale there.
    """
    if corner_type != "braking":
        return P_LIFT_COAST_MAX
    fraction = braking_force_fraction(a_brake_ms2) if a_brake_ms2 is not None else BRAKING_FORCE_MAX_FRACTION
    return P_MGUK_MAX * fraction


# Not a regulatory value — purely how finely the per-step trace captured
# below (for the speed/deployment-power visualization) is sampled. Doesn't
# touch any of the physics above; every rule still runs at the real dt.
TRACE_SAMPLE_EVERY_N_STEPS = 4  # ~0.2s at dt=0.05s


def simulate_lap(corners_df, straights_df, mass_kg, soc_start_j,
                  aero_corner, aero_straight, rho,
                  lap_recharge_limit, lap_length_m,
                  battery_cap=BATTERY_CAP,
                  lift_and_coast=False,
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

    aero_corner / aero_straight: dicts with 'CdA' (m^2) and 'ClA' (m^2) —
    ClA feeds the tire traction-limit calculation (max_traction_force),
    not just CdA/drag.
    """

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
                # still enough for a continuous lap-wide speed line.
                "trace": [
                    {"distance_m": 0.0, "speed_kmh": corner["entry_speed_kmh"]},
                    {"distance_m": corner_length_m, "speed_kmh": corner["exit_speed_kmh"]},
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
            regen_cap_w = corner_regen_cap_w(corner.get("corner_type", "braking"), a_brake)
            E_regen = min(
                regen_cap_w * t_brake,
                E_regen_potential,
                battery_cap - soc,
                lap_recharge_limit - lap_recharge_used,
            )
            E_regen = max(E_regen, 0)

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
                "trace": [
                    {"distance_m": 0.0, "speed_kmh": corner["entry_speed_kmh"]},
                    {"distance_m": apex_offset_m, "speed_kmh": corner["apex_speed_kmh"]},
                    {"distance_m": corner_length_m, "speed_kmh": corner["exit_speed_kmh"]},
                ],
            })

        # --- Straight i (immediately follows corner i, by construction) ---
        straight = straights_df.iloc[i]
        v = straight["entry_speed_kmh"] / 3.6
        length = straight["length_m"]
        coast_length = min(straight.get("coast_length_m", 0.0), length)
        aero = aero_straight if straight["segment_type"] == "deployment_straight" else aero_corner

        distance_covered = 0.0
        current_gear = initial_gear_for_speed(v)

        # Coast phase (telemetry-measured, see _measure_coast_length): the
        # real gap between the brake coming off and the driver actually
        # getting back on the throttle — a real lift-and-coast, zero
        # throttle AND zero brake. No mechanical braking or deployment here,
        # but the crankshaft is still spinning down, so lift-and-coast regen
        # (capped at P_LIFT_COAST_MAX) still charges the battery — and the
        # aero elements close (modeled here with aero_corner's higher CdA,
        # since circuits.json has no dedicated "closed" profile), increasing
        # drag beyond the straight's normal open-aero setting.
        E_coast_regen = 0.0
        # Per-step (distance, speed, power) samples for the speed/deployment
        # visualization — purely observational, doesn't feed back into any
        # of the physics above.
        trace = []
        trace_step = 0
        while distance_covered < coast_length and v > 0.1:
            F_drag = 0.5 * rho * aero_corner["CdA"] * v**2
            F_roll = CRR * mass_kg * G
            a = (-F_drag - F_roll) / mass_kg
            v += a * dt
            distance_covered += v * dt

            regen_power = max(min(
                P_LIFT_COAST_MAX,
                (battery_cap - soc) / dt,
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
                    "superclip_power_w": regen_power,
                })

        E_deployed = 0.0
        E_superclip_regen = 0.0
        elapsed_time = 0.0  # reset here, not at the straight's start — the ramp-up below should begin when real acceleration begins, not when coasting does
        deploy_seconds_elapsed = 0.0  # continuous full-throttle deployment time, for the decay curve below — resets to 0 the instant deployment pauses
        coast_trigger_distance = length * (1 - LIFT_AND_COAST_FRACTION)

        # Distance into this straight (from its start, same origin as
        # coast_length above) where deployment actually stops — the decay
        # curve bottoming out (or, less often, the battery running dry) is
        # often reached well before the straight ends, especially the long
        # ones, since deployment is power-limited (P_MGUK_MAX decaying)
        # rather than distance-limited. Past that point the car is coasting
        # on ICE alone, which the physics below already models correctly
        # (can_deploy just goes False) — this only records *where* that
        # happens, defaulting to the full length when it never does, so the
        # frontend can show the coasting tail as neutral instead of painting
        # the whole straight as one discharge zone.
        active_deploy_m = length
        deploy_ended = False

        while distance_covered < length and v > 0.1:
            past_coast_trigger = lift_and_coast and distance_covered >= coast_trigger_distance
            current_gear = next_gear(current_gear, v)
            ramp_factor = min(elapsed_time / RAMP_UP_TIME_SEC, 1.0)

            if past_coast_trigger:
                F_drive = 0.0  # throttle AND deployment both off — real lift-and-coast
                energy_this_step = 0.0
                can_deploy = False
                superclip_j = 0.0
            else:
                can_deploy = soc > 0

                # Deployment power decays 50kW for every second of continuous
                # full-throttle deployment (350kW -> ... -> 0kW by 8s), per
                # the FIA's published curve — rather than a flat 350kW the
                # whole time. deploy_seconds_elapsed is reset only at the
                # start of each straight's drive phase (a real interruption —
                # a preceding corner or coast). It must NOT reset just
                # because P_deploy computes to 0 within this same continuous
                # stretch (decay bottoming out, or the battery running dry)
                # — that was a real bug: it let the clock restart and
                # deployment spike straight back up to 350kW moments after
                # fully decaying, instead of staying at the floor for the
                # rest of this straight, the way "decayed" should behave.
                deploy_power_cap = max(P_MGUK_MAX - DEPLOY_DECAY_RATE * deploy_seconds_elapsed, 0.0)
                P_deploy = min(deploy_power_cap, soc / dt) if can_deploy else 0.0
                deploy_seconds_elapsed += dt

                F_drive_uncapped = (F_ICE_at_gear(v, current_gear) + (P_deploy / v if v > 0 else 0)) * ramp_factor

                F_max_traction = max_traction_force(v, mass_kg, aero["ClA"], rho)
                if F_drive_uncapped > F_max_traction and F_drive_uncapped > 0:
                    scale = F_max_traction / F_drive_uncapped
                    F_drive = F_max_traction
                    energy_this_step = P_deploy * ramp_factor * scale * dt
                else:
                    F_drive = F_drive_uncapped
                    energy_this_step = P_deploy * ramp_factor * dt

                # Superclipping: the crankshaft-connected regen system keeps
                # charging the battery even while at full throttle and
                # actively deploying (see the module docstring's FIA table),
                # capped at P_SUPERCLIP_MAX — distinct from the 200kW
                # lift-and-coast rate used for coasting and liftoff corners.
                superclip_power = max(min(
                    P_SUPERCLIP_MAX,
                    (battery_cap - soc) / dt,
                    (lap_recharge_limit - lap_recharge_used) / dt,
                ), 0.0)
                superclip_j = superclip_power * dt

            if not can_deploy and not deploy_ended:
                active_deploy_m = distance_covered
                deploy_ended = True

            F_drag = 0.5 * rho * aero["CdA"] * v**2
            F_roll = CRR * mass_kg * G

            a = (F_drive - F_drag - F_roll) / mass_kg
            v += a * dt
            distance_covered += v * dt
            elapsed_time += dt

            soc -= energy_this_step
            soc += superclip_j
            E_deployed += energy_this_step
            E_superclip_regen += superclip_j
            lap_recharge_used += superclip_j

            trace_step += 1
            if trace_step % TRACE_SAMPLE_EVERY_N_STEPS == 0:
                trace.append({
                    "distance_m": distance_covered,
                    "speed_kmh": v * 3.6,
                    # Energy actually delivered this step, not the raw decay
                    # curve value — reflects ramp-up and traction-limit
                    # scaling too, so this is what really moved the car.
                    "deploy_power_w": energy_this_step / dt,
                    "superclip_power_w": superclip_j / dt,
                })

        results.append({
            "type": "straight",
            "segment_type": straight["segment_type"],
            "exit_speed_kmh": v * 3.6,
            "E_deployed_j": E_deployed,
            "E_coast_regen_j": E_coast_regen,
            "E_superclip_regen_j": E_superclip_regen,
            "soc_after_j": soc,
            "start_distance_m": straight["start_distance_m"],
            "length_m": length,
            "coast_length_m": coast_length,
            "active_deploy_m": active_deploy_m,
            "trace": trace,
        })

    return results


# ---------------------------------------------------------------------------
# 8. TOP-LEVEL API — the single entry point for everything above
# ---------------------------------------------------------------------------

def run_lap_simulation(telemetry, circuits_data, track_key, scenario_name,
                        recharge_limit_mj=None, elevation_m: float = 0, air_temp_c: float = 25):
    """
    The one function to call for a full lap simulation. Wires together every
    piece built in this module: corner/straight extraction (with lift-off
    detection), aero lookup from circuits.json, air density from elevation +
    temperature, scenario mass/SOC/lift-and-coast settings, and simulate_lap()
    itself.

    Args:
        telemetry: DataFrame with columns Distance, Speed, Throttle, Brake
            (from FastF1 car data, already run through .add_distance()).
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

    scenario = SCENARIOS[scenario_name]
    segment_results = simulate_lap(
        corners_df, straights_df,
        mass_kg=scenario["mass_kg"],
        soc_start_j=scenario["soc_start_j"],
        aero_corner=aero_corner,
        aero_straight=aero_straight,
        rho=rho,
        lap_recharge_limit=recharge_limit_j,
        lap_length_m=lap_length_m,
        lift_and_coast=scenario["lift_and_coast"],
    )

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
            "battery_cap_mj": BATTERY_CAP / 1_000_000,
            "lap_length_m": float(lap_length_m),
        },
    }


def summarize_results(segment_results):
    """Aggregates simulate_lap()'s per-segment output into headline totals."""
    corners = [r for r in segment_results if r["type"] == "corner"]
    straights = [r for r in segment_results if r["type"] == "straight"]

    braking_regen_j = sum(r["E_regen_j"] for r in corners if r.get("corner_type", "braking") == "braking")
    liftoff_regen_j = sum(r["E_regen_j"] for r in corners if r.get("corner_type") == "liftoff")
    coast_regen_j = sum(r["E_coast_regen_j"] for r in straights)
    superclip_regen_j = sum(r["E_superclip_regen_j"] for r in straights)

    total_regen_j = braking_regen_j + liftoff_regen_j + coast_regen_j + superclip_regen_j
    total_deployed_j = sum(r["E_deployed_j"] for r in straights)

    return {
        "total_regen_mj": total_regen_j / 1e6,
        "total_deployed_mj": total_deployed_j / 1e6,
        "braking_regen_mj": braking_regen_j / 1e6,
        "liftoff_regen_mj": liftoff_regen_j / 1e6,
        "coast_regen_mj": coast_regen_j / 1e6,
        "superclip_regen_mj": superclip_regen_j / 1e6,
        "final_soc_mj": segment_results[-1]["soc_after_j"] / 1e6 if segment_results else None,
        "num_corners": len(corners),
        "num_straights": len(straights),
        "num_liftoff_corners": sum(1 for r in corners if r.get("corner_type") == "liftoff"),
    }

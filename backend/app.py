import os
import json
import logging
import subprocess
import sys
from flask import Flask, jsonify, send_from_directory, abort, request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# ── Paths ───────────────────────────────────────────────────────────────────
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
DATA_DIR       = os.path.join(BASE_DIR, "../data")
FRONTEND_DIR   = os.path.join(BASE_DIR, "../frontend")
CIRCUITS_PATH  = os.path.join(DATA_DIR, "circuits.json")
OVERRIDES_PATH = os.path.join(DATA_DIR, "overrides.json")

# ── Flask app ────────────────────────────────────────────────────────────────
app = Flask(__name__, static_folder=FRONTEND_DIR)


# ── Data helpers ─────────────────────────────────────────────────────────────
def load_circuits() -> dict:
    """Loads circuits.json — the baseline data for all 24 circuits."""
    with open(CIRCUITS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_overrides() -> dict:
    """
    Loads overrides.json — official MJ limits parsed from FIA PDFs.
    Returns empty structure if file doesn't exist yet.
    """
    try:
        with open(OVERRIDES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"events": []}



def get_recharge_limit_mj(circuit_id: str, session: str,
                          overtake_active: bool = False) -> float | None:
    """
    This event's per-lap recharge limit (Article C5.2.10), in MJ.

    Qualifying has a single published figure — the +0.5MJ Overtake allowance
    (C5.2.10.iii) is a TTCS-only provision, so the FIA's table only splits
    overtake-active/inactive in the Race column.

    For a race, overtake_active selects between those two columns. It
    defaults to the inactive figure: that's the baseline a car runs to in
    clean air, and Overtake in a race is proximity-gated (Article B7.2.3) on
    multi-car data this project doesn't have.
    """
    overrides = load_overrides()

    for event in overrides.get("events", []):
        if event.get("event") != circuit_id:
            continue

        limits = event.get("limits", {})

        if session == "qualifying":
            return limits.get("qualifying")

        race = limits.get("race", {})
        if overtake_active:
            # Falls back to the inactive figure if the doc somehow lacks the
            # overtake column, rather than returning None and silently
            # dropping to the model's generic default.
            return race.get("overtake_active", race.get("overtake_inactive"))
        return race.get("overtake_inactive")

    return None


def get_sectors(circuit_id: str) -> dict:
    """
    This event's published sector tables (Alt-1 zones, >150kW-reduction
    sectors, power-reduction reset sectors, higher-speed-threshold sectors),
    parsed from its FIA Power Unit Information doc by parser.py. Empty for a
    circuit whose doc hasn't been fetched yet — the model then simply has no
    reset points, which is also how several real circuits are published.
    """
    for event in load_overrides().get("events", []):
        if event.get("event") == circuit_id:
            return event.get("sectors", {})
    return {}


def get_power_reduction(circuit_id: str) -> dict:
    """
    This event's Article C5.12.8 figures (Power Limited Distance and the
    published power-reduction rate), parsed from its FIA Power Unit
    Information doc by parser.py. Returns an empty dict for a circuit whose
    doc hasn't been fetched/parsed yet — energy_model then falls back to its
    own default ramp-down rate rather than failing.
    """
    for event in load_overrides().get("events", []):
        if event.get("event") == circuit_id:
            return event.get("power_reduction", {})
    return {}


def merge_circuit_data() -> list:
    """
    Merges circuits.json with overrides.json.

    For each circuit:
    - Start with baseline data from circuits.json
    - If overrides.json has a parsed PDF entry for this circuit,
      add the official MJ limits
    - If not, mark limits as None so the frontend knows
      data hasn't been fetched yet

    This means the app always works — circuits without fetched
    data show as "pending" rather than crashing.
    """
    circuits  = load_circuits()
    overrides = load_overrides()

    # Build a lookup dict from overrides: {circuit_id: event_data}
    override_map = {
        event["event"]: event
        for event in overrides.get("events", [])
    }

    result = []

    for circuit_id, circuit in circuits["circuits"].items():
        entry = {
            "id":           circuit_id,
            "name":         circuit["name"],
            "circuit":      circuit["circuit"],
            "city":         circuit["city"],
            "country":      circuit["country"],
            "round":        circuit["round"],
            "lap_time_sec": circuit["lap_time_sec"],
            "race_laps":    circuit["race_laps"],
            "energy_type":  circuit["energy_type"],
            "svg_file":     circuit.get("svg_file"),
            "svg_direction_reversed": circuit.get("svg_direction_reversed", False),
            "limits":       None,
            "source_url":   None,
            "extracted_at": None,
}

        if circuit_id in override_map:
            override = override_map[circuit_id]
            entry["limits"]       = override["limits"]
            entry["source_url"]   = override.get("source_url")
            entry["extracted_at"] = override.get("extracted_at")

        result.append(entry)

    # Sort by round number
    result.sort(key=lambda x: x["round"])
    return result


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serves the frontend index.html."""
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.route("/api/circuits", methods=["GET"])
def get_circuits():
    """
    Returns all 24 circuits with their current MJ limits.
    Circuits without fetched data have limits: null.

    Example response:
    [
      {
        "id": "canada",
        "name": "Canadian Grand Prix",
        "round": 7,
        "energy_type": "balanced",
        "limits": {
          "race": {"overtake_inactive": 8.0, "overtake_active": 8.5},
          "qualifying": 6.0,
          "free_practice": 8.5,
          "out_laps": 8.5
        }
      },
      ...
    ]
    """
    try:
        data = merge_circuit_data()
        log.info(f"Serving /api/circuits — {len(data)} circuits")
        return jsonify(data)
    except Exception as e:
        log.error(f"Error in /api/circuits: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/circuit/<circuit_id>", methods=["GET"])
def get_circuit(circuit_id: str):
    """
    Returns data for a single circuit by ID.
    Example: GET /api/circuit/monaco
    Returns 404 if circuit ID is not recognised.
    """
    try:
        all_circuits = merge_circuit_data()
        match = next(
            (c for c in all_circuits if c["id"] == circuit_id),
            None
        )
        if not match:
            log.warning(f"Circuit not found: {circuit_id}")
            abort(404)

        log.info(f"Serving /api/circuit/{circuit_id}")
        return jsonify(match)

    except Exception as e:
        log.error(f"Error in /api/circuit/{circuit_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/fetch/<circuit_id>", methods=["POST"])
def trigger_fetch(circuit_id: str):
    """
    Triggers fetcher.py for the given circuit ID.
    Runs as a subprocess so Flask doesn't block while
    the PDF is being downloaded and parsed.

    Example: POST /api/fetch/hungary
    Returns immediately with a status message.
    The frontend polls /api/circuit/<id> to detect
    when new data appears.
    """
    # Validate the circuit ID exists before launching subprocess
    try:
        circuits = load_circuits()
        if circuit_id not in circuits["circuits"]:
            log.warning(f"Fetch requested for unknown circuit: {circuit_id}")
            abort(404)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    fetcher_path = os.path.join(BASE_DIR, "fetcher.py")

    try:
        # sys.executable ensures we use the same Python running Flask,
        # not some other version that might be on the system
        process = subprocess.Popen(
            [sys.executable, fetcher_path, circuit_id],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        log.info(
            f"Launched fetcher for {circuit_id} "
            f"(PID {process.pid})"
        )
        return jsonify({
            "status":     "fetching",
            "circuit_id": circuit_id,
            "message":    f"Fetching PU data for {circuit_id} in background"
        })

    except Exception as e:
        log.error(f"Failed to launch fetcher for {circuit_id}: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/status", methods=["GET"])
def status():
    """
    Health check endpoint.
    Returns a summary of how many circuits have data fetched.
    Useful for the frontend to show a data coverage indicator.
    """
    try:
        data    = merge_circuit_data()
        total   = len(data)
        fetched = sum(1 for c in data if c["limits"] is not None)
        pending = total - fetched

        return jsonify({
            "status":         "ok",
            "total_circuits": total,
            "fetched":        fetched,
            "pending":        pending,
            "coverage":       f"{fetched}/{total}"
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/svgs/<filename>")
def serve_svg(filename: str):
    """
    Serves circuit SVG files from the cloned repo. Uses the "white-outline"
    variant — the base white track ribbon plus a thin contrasting outline
    stroked on top of the same path — so the track shape stays legible
    against the app's dark theme rather than just a plain white ribbon.
    """
    svg_dir = os.path.join(
        BASE_DIR,
        "../f1-circuits-svg/circuits/detailed/white-outline"
    )
    return send_from_directory(svg_dir, filename)


@app.route("/<path:filename>")
def serve_frontend(filename: str):
    """Serves any static file from the frontend folder."""
    return send_from_directory(FRONTEND_DIR, filename)



@app.route("/api/simulate/<circuit_id>", methods=["GET"])
def simulate_circuit(circuit_id: str):
    """
    Runs the physics energy model for one circuit.

    Query params:
        session      race | qualifying           (default: race)
        elevation    metres                      (default: 0)
        temp         air temperature in Celsius  (default: 25)
        overtake     1 | true — force Overtake Override Mode on for a race
                     (default: off). Qualifying ignores this: Article B7.2.2
                     enables Overtake for that whole session regardless.

    Example: GET /api/simulate/miami?session=race&elevation=2&temp=29

    Returns 503 rather than 500 when telemetry isn't available for a circuit,
    since that's an expected missing-data state, not a server fault.
    """
    # Imported here, not at module top: pandas and numpy take a noticeable
    # moment to import, and every other route works fine without them. This
    # keeps app startup fast and keeps the rest of the API unaffected if the
    # scientific stack ever fails to install.
    from energy_model import run_lap_simulation
    from telemetry import load_telemetry

    session = request.args.get("session", "race")

    scenario = {
        "race":       "race_pace",
        "qualifying": "qualifying",
    }.get(session)

    if scenario is None:
        return jsonify({
            "error": f"Unknown session '{session}'. Use 'race' or 'qualifying'."
        }), 400

    try:
        elevation_m = float(request.args.get("elevation", 0))
        air_temp_c  = float(request.args.get("temp", 25))
    except ValueError:
        return jsonify({"error": "elevation and temp must be numbers"}), 400

    circuits = load_circuits()
    circuit  = circuits["circuits"].get(circuit_id)

    if circuit is None:
        log.warning(f"Simulation requested for unknown circuit: {circuit_id}")
        abort(404)

    telemetry = load_telemetry(
        circuit_id,
        session      = session,
        circuit_name = circuit.get("circuit"),
        allow_fastf1 = False,   # never hit the network from the server
    )
    telemetry_source = session

    # No cached race telemetry yet (e.g. the round hasn't happened this
    # season) — fall back to real telemetry from earlier in the same race
    # weekend, rather than a previous season's data (car/PU characteristics
    # change too much year to year). Only applies to race simulations; a
    # qualifying request with no cached qualifying telemetry still just 503s.
    if telemetry is None and session == "race":
        from telemetry import load_race_proxy_telemetry
        telemetry, telemetry_source = load_race_proxy_telemetry(
            circuit_id,
            circuit_name = circuit.get("circuit"),
            allow_fastf1 = False,
        )

    if telemetry is None:
        log.info(f"No telemetry cached for {circuit_id}/{session}")
        return jsonify({
            "error":      "no_telemetry",
            "circuit_id": circuit_id,
            "session":    session,
            "message":    (
                f"No telemetry cached for {circuit_id}/{session}"
                + (" or any earlier session this weekend (FP1/sprint)" if session == "race" else "")
                + f". Generate it locally with: python backend/telemetry.py export {circuit_id}"
            ),
        }), 503

    # Qualifying is always Overtake-enabled (B7.2.2), so the query param only
    # means anything for a race — where it forces the attacking case on for
    # the whole lap, since real activation windows need multi-car data.
    overtake_active   = request.args.get("overtake", "").lower() in ("1", "true", "yes")
    if scenario == "qualifying":
        overtake_active = True

    recharge_limit_mj = get_recharge_limit_mj(circuit_id, session, overtake_active)
    power_reduction   = get_power_reduction(circuit_id)

    try:
        result = run_lap_simulation(
            telemetry, circuits, circuit_id, scenario,
            recharge_limit_mj = recharge_limit_mj,
            elevation_m       = elevation_m,
            air_temp_c        = air_temp_c,
            rate_limit_kw_per_s      = power_reduction.get("rate_limit_kw_per_s"),
            power_limited_distance_m = power_reduction.get("power_limited_distance_m"),
            overtake_active          = overtake_active,
            sectors                  = get_sectors(circuit_id),
        )
    except ValueError as e:
        # Bad data rather than a crash — surface the model's own message.
        log.error(f"Simulation failed for {circuit_id}: {e}")
        return jsonify({"error": "simulation_failed", "message": str(e)}), 422
    except Exception as e:
        log.error(f"Unexpected simulation error for {circuit_id}: {e}")
        return jsonify({"error": "internal_error", "message": str(e)}), 500

    # Which session's telemetry actually powered this simulation — "race"
    # unless the race-proxy fallback above kicked in (fp1/sprint_race/
    # sprint_qualifying), so the frontend can flag it as an estimate rather
    # than presenting it as real race data.
    result["meta"]["telemetry_source"] = telemetry_source

    # The DataFrames are dropped here deliberately: they're a debugging
    # convenience for local work, not something the frontend needs, and
    # serialising ~20 corner rows per request is wasted bandwidth.
    return jsonify({
        "circuit_id": circuit_id,
        "session":    session,
        "summary":    result["summary"],
        "meta":       result["meta"],
        "segments":   result["segment_results"],
    })


# ── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=== F1 Energy Decoder API starting ===")
    log.info(f"Data dir:     {DATA_DIR}")
    log.info(f"Frontend dir: {FRONTEND_DIR}")

    # debug=True means Flask auto-restarts when you save .py files
    # Never use debug=True in production
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)

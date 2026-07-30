import os
import json
import logging
import subprocess
import sys
from flask import Flask, jsonify, send_from_directory, abort

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
    """Serves circuit SVG files from the cloned repo."""
    svg_dir = os.path.join(
        BASE_DIR,
        "../f1-circuits-svg/circuits/detailed/black-outline"
    )
    return send_from_directory(svg_dir, filename)


@app.route("/<path:filename>")
def serve_frontend(filename: str):
    """Serves any static file from the frontend folder."""
    return send_from_directory(FRONTEND_DIR, filename)


# ── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=== F1 Energy Decoder API starting ===")
    log.info(f"Data dir:     {DATA_DIR}")
    log.info(f"Frontend dir: {FRONTEND_DIR}")

    # debug=True means Flask auto-restarts when you save .py files
    # Never use debug=True in production
    port = int(os.environ.get('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)

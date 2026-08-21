"""
Telemetry loading for the energy model.

energy_model.py needs a DataFrame with Distance, Speed, Throttle and Brake
columns for one lap. This module is the only place that knows where such a
DataFrame comes from.

Two sources, in priority order — same "cached first, live second, always
works" pattern as fetcher.py:

    1. Local CSV in data/telemetry/{circuit_id}_{session}.csv
       Committed to the repo. Fast, offline, works on any host.

    2. FastF1 (optional dependency)
       Downloads real session telemetry from the F1 timing API. Only used if
       fastf1 is installed AND no cached CSV exists. Slow on first call
       (tens of seconds), and its cache is lost on ephemeral hosts.

RECOMMENDED WORKFLOW: run export_telemetry_csv() locally once per circuit,
commit the CSVs, and let the deployed server read only from cache. Render's
free tier has an ephemeral filesystem and 512MB RAM — re-downloading FastF1
sessions on every cold start is not viable there.

    python backend/telemetry.py export miami 2026 race VER

Session can be typed as "race" or "R" (or "qualifying"/"Q", "practice"/"FP2")
— normalize_session() below maps both spellings to the same cache filename,
so export and check can never disagree on where a file lives.
"""

import json
import logging
import os

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DATA_DIR      = os.path.normpath(os.path.join(BASE_DIR, "../data"))
TELEMETRY_DIR = os.path.join(DATA_DIR, "telemetry")

# Columns energy_model.py requires. Anything else in the CSV is ignored.
REQUIRED_COLUMNS = ["Distance", "Speed", "Throttle", "Brake"]

# Session codes accepted by both the cache filename and FastF1.
SESSIONS = {
    "race":       "R",
    "qualifying": "Q",
    "practice":   "FP2",
}

# Reverse lookup so a FastF1-style code typed at the CLI (e.g. "R") resolves
# to the same canonical name as "race" — this is what export and check were
# disagreeing on before: one accepted "R", the other only recognised "race",
# and cache_path() used whatever string it was given with no normalization.
_SESSION_ALIASES = {code: name for name, code in SESSIONS.items()}


def normalize_session(session: str) -> str:
    """
    Maps any accepted spelling of a session to its canonical name.
    "R", "r", "race" all become "race". Unknown values pass through
    unchanged so a genuinely new session type doesn't silently disappear —
    it'll just produce a distinctly-named cache file instead of a mismatch.
    """
    key = session.strip()
    if key in SESSIONS:
        return key
    return _SESSION_ALIASES.get(key.upper(), key.lower())


# ── Cache ────────────────────────────────────────────────────────────────────

def _load_circuits_json() -> dict:
    path = os.path.join(DATA_DIR, "circuits.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def resolve_circuit_id(circuit_id: str) -> str:
    """
    Resolves any alias circuits.json knows about (e.g. "barcelona",
    "silverstone") to its canonical id ("spain", "great_britain") — the same
    alias map parser.py/fetcher.py already use to match FIA PDF names
    against circuits.json. Prevents a cache file being written under a name
    the rest of the app will never ask for (load_telemetry() is always
    called with the canonical id, e.g. by app.py's /api/simulate route).
    Unknown values pass through unchanged, matching normalize_session()'s
    "don't silently disappear" behaviour for a genuinely new circuit.
    """
    data = _load_circuits_json()
    if circuit_id in data.get("circuits", {}):
        return circuit_id
    return data.get("aliases", {}).get(circuit_id.strip().lower(), circuit_id)


def _alias_candidates(canonical_id: str) -> list[str]:
    """Every alias circuits.json maps onto this canonical id — e.g. "spain"
    -> ["barcelona", "spanish", "catalunya", "barcelona-catalunya", ...]."""
    aliases = _load_circuits_json().get("aliases", {})
    return [alias for alias, cid in aliases.items() if cid == canonical_id]


def _raw_cache_path(circuit_id: str, session: str) -> str:
    """Path for exactly this circuit_id string — no alias resolution."""
    return os.path.join(TELEMETRY_DIR, f"{circuit_id}_{normalize_session(session)}.csv")


def cache_path(circuit_id: str, session: str) -> str:
    """
    Path where a NEW circuit/session telemetry CSV should be written.
    circuit_id is resolved to its canonical circuits.json id first, so an
    export run with a common/FastF1-friendly name (e.g. "barcelona") still
    lands under the canonical filename ("spain_race.csv") the rest of the
    app actually looks for. Session is normalized first so "R" and "race"
    always resolve to the same file — the mismatch that used to make export
    and check disagree.
    """
    return _raw_cache_path(resolve_circuit_id(circuit_id), session)


def load_from_cache(circuit_id: str, session: str) -> pd.DataFrame | None:
    """
    Reads a committed telemetry CSV. Tries the canonical id first, then
    falls back through every alias circuits.json knows for it — some
    existing cache files were exported under a common name (e.g.
    barcelona_race.csv, for what circuits.json calls "spain") before this
    resolution existed, and are found here without needing to rename them.
    Returns None if nothing matches, so the caller can decide whether to
    try FastF1 or give up gracefully.
    """
    canonical = resolve_circuit_id(circuit_id)
    candidates = [canonical] + _alias_candidates(canonical)

    path = None
    for candidate in candidates:
        candidate_path = _raw_cache_path(candidate, session)
        if os.path.exists(candidate_path):
            path = candidate_path
            break

    if path is None:
        log.info(f"No cached telemetry for {circuit_id}/{session}")
        return None

    try:
        df = pd.read_csv(path)
    except Exception as e:
        log.error(f"Failed to read {path}: {e}")
        return None

    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        log.error(
            f"{path} is missing column(s): {', '.join(missing)} — ignoring file"
        )
        return None

    # Brake round-trips through CSV as the strings "True"/"False". The model
    # compares it with == True, so coerce it back to real booleans here rather
    # than letting a silent type mismatch make every corner disappear.
    if df["Brake"].dtype == object:
        df["Brake"] = df["Brake"].astype(str).str.strip().str.lower().isin(
            ("true", "1", "yes")
        )
    else:
        df["Brake"] = df["Brake"].astype(bool)

    log.info(
        f"Loaded {len(df)} telemetry rows from cache for {circuit_id}/{session} "
        f"({os.path.basename(path)})"
    )
    return df


# ── FastF1 (optional) ────────────────────────────────────────────────────────

def fastf1_available() -> bool:
    """True if the optional fastf1 dependency is installed."""
    try:
        import fastf1  # noqa: F401
        return True
    except ImportError:
        return False


def load_from_fastf1(circuit_id: str, session: str, year: int = 2026,
                     driver: str = "VER") -> pd.DataFrame | None:
    """
    Downloads the fastest lap's telemetry for a driver from FastF1.

    Returns None on any failure — missing dependency, unknown event, no data
    published yet — so the API degrades to "no simulation available" rather
    than returning a 500.

    Note: circuit_id here must be resolvable by FastF1's event lookup. It
    accepts circuit and country names, which is why the caller passes the
    circuit's proper name rather than our internal snake_case id.
    """
    if not fastf1_available():
        log.warning("fastf1 is not installed — cannot fetch live telemetry")
        return None

    import fastf1

    session_code = SESSIONS.get(session, session)

    try:
        os.makedirs(os.path.join(DATA_DIR, "fastf1_cache"), exist_ok=True)
        fastf1.Cache.enable_cache(os.path.join(DATA_DIR, "fastf1_cache"))

        ses = fastf1.get_session(year, circuit_id, session_code)
        ses.load(telemetry=True, laps=True, weather=False)

        lap = ses.laps.pick_drivers(driver).pick_fastest()
        if lap is None:
            log.error(f"No fastest lap found for {driver} at {circuit_id}")
            return None

        tel = lap.get_car_data().add_distance()
        df  = tel[["Distance", "Speed", "Throttle", "Brake", "RPM", "nGear"]].copy()

        log.info(f"Fetched {len(df)} telemetry rows from FastF1 for {circuit_id}")
        return df

    except Exception as e:
        log.error(f"FastF1 fetch failed for {circuit_id}/{session}: {e}")
        return None


# ── Public loader ────────────────────────────────────────────────────────────

def load_telemetry(circuit_id: str, session: str = "race",
                   circuit_name: str | None = None,
                   allow_fastf1: bool = False) -> pd.DataFrame | None:
    """
    Gets telemetry for a circuit/session, cache first.

    Args:
        circuit_id:   our internal id, e.g. "miami".
        session:      "race", "qualifying" or "practice".
        circuit_name: proper circuit name for FastF1 lookup, e.g.
                      "Miami International Autodrome". Only used if falling
                      back to FastF1.
        allow_fastf1: if False (the default) this never touches the network.
                      Keep it False on the deployed server; pass True only
                      when generating cache files locally.

    Returns None if no telemetry can be found, which callers should treat as
    "simulation unavailable for this circuit yet".
    """
    df = load_from_cache(circuit_id, session)
    if df is not None:
        return df

    if not allow_fastf1:
        return None

    return load_from_fastf1(circuit_name or circuit_id, session)


# ── Cache generation (run locally) ───────────────────────────────────────────

def export_telemetry_csv(circuit_id: str, session: str = "race",
                         year: int = 2026, driver: str = "VER",
                         circuit_name: str | None = None) -> bool:
    """
    Downloads telemetry via FastF1 and writes it to the cache directory so it
    can be committed. Run this locally, never on the server.
    """
    df = load_from_fastf1(circuit_name or circuit_id, session, year, driver)
    if df is None:
        return False

    canonical = resolve_circuit_id(circuit_id)
    if canonical != circuit_id:
        log.info(f"'{circuit_id}' resolved to circuits.json's canonical id '{canonical}'")

    os.makedirs(TELEMETRY_DIR, exist_ok=True)
    path = cache_path(circuit_id, session)
    df.to_csv(path, index=False)

    log.info(f"Wrote {len(df)} rows to {path}")
    return True


if __name__ == "__main__":
    import sys

    # Usage:
    #   python backend/telemetry.py export miami 2026 R VER
    #   python backend/telemetry.py check  miami race
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    command    = sys.argv[1]
    circuit_id = sys.argv[2]

    if command == "export":
        year    = int(sys.argv[3]) if len(sys.argv) > 3 else 2026
        session = normalize_session(sys.argv[4]) if len(sys.argv) > 4 else "race"
        driver  = sys.argv[5] if len(sys.argv) > 5 else "VER"
        ok = export_telemetry_csv(circuit_id, session, year, driver)
        sys.exit(0 if ok else 1)

    elif command == "check":
        session = normalize_session(sys.argv[3]) if len(sys.argv) > 3 else "race"
        df = load_telemetry(circuit_id, session)
        if df is None:
            log.error(f"No telemetry available for {circuit_id}/{session}")
            sys.exit(1)
        log.info(f"OK — {len(df)} rows, columns: {list(df.columns)}")

    else:
        log.error(f"Unknown command: {command}")
        sys.exit(1)
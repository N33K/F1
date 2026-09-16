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
# ElapsedSec (real seconds since the lap started) lets the energy model
# derive real acceleration/duration from actual sample timing instead of
# assuming a fixed step — telemetry's raw samples are NOT evenly spaced.
REQUIRED_COLUMNS = ["Distance", "Speed", "Throttle", "Brake", "ElapsedSec"]

# Session codes accepted by both the cache filename and FastF1. Codes
# confirmed against the installed fastf1 package's own
# fastf1.events._SESSION_TYPE_ABBREVIATIONS.
SESSIONS = {
    "race":               "R",
    "qualifying":         "Q",
    "practice":           "FP2",
    "fp1":                "FP1",
    "sprint_race":        "S",
    "sprint_qualifying":  "SQ",
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


def _normalize(text: str) -> str:
    """Lowercase, strip accents and punctuation — so 'Montréal' matches
    'Montreal' and 'Spa-Francorchamps' matches 'Spa'."""
    import unicodedata
    text = unicodedata.normalize("NFKD", str(text))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return "".join(ch if ch.isalnum() else " " for ch in text.lower()).split().__str__()


def resolve_fastf1_event(circuit_id: str, year: int):
    """
    Finds the FastF1 event for one of our circuit ids by matching against the
    season schedule EXPLICITLY, instead of handing our internal snake_case id
    to FastF1's fuzzy event lookup.

    That fuzzy lookup silently picks a wrong event when our id doesn't
    resemble any real event name, and it does so with only a warning — which
    is how 'great_britain' once produced a whole season's worth of Austrian
    Grand Prix telemetry saved under great_britain_race.csv. Other ids with
    the same failure mode: 'usa' -> Qatar, 'saudi_arabia' -> Abu Dhabi, and
    'spain' -> the Spanish GP at MADRID rather than Barcelona (the 2026
    calendar reassigned that name — see data/Context/PROJECT_CONTEXT.md).

    Matching order, most specific first: the circuit's city (FastF1's
    Location column), then its circuits.json name against EventName.

    Returns the matched schedule row. Raises ValueError if nothing matches
    confidently, so a bad export fails loudly instead of writing a
    plausible-looking CSV full of another circuit's data.
    """
    import fastf1

    circuits = _load_circuits_json().get("circuits", {})
    circuit = circuits.get(resolve_circuit_id(circuit_id))
    if circuit is None:
        raise ValueError(f"'{circuit_id}' is not a circuit in circuits.json")

    schedule = fastf1.get_event_schedule(year)
    schedule = schedule[schedule["RoundNumber"] > 0]  # drop pre-season testing

    city_tokens = set(_normalize(circuit.get("city", "")).strip("[]").split(", "))
    name_tokens = set(_normalize(circuit.get("name", "")).strip("[]").split(", "))

    for _, row in schedule.iterrows():
        loc_tokens = set(_normalize(row["Location"]).strip("[]").split(", "))
        if city_tokens & loc_tokens:
            return row

    for _, row in schedule.iterrows():
        event_tokens = set(_normalize(row["EventName"]).strip("[]").split(", "))
        # Ignore the words every event shares, or everything matches everything.
        distinctive = (name_tokens & event_tokens) - {"'grand'", "'prix'", "grand", "prix"}
        if distinctive:
            return row

    raise ValueError(
        f"No {year} event matches circuit '{circuit_id}' "
        f"(city={circuit.get('city')!r}, name={circuit.get('name')!r}). "
        f"Refusing to guess — check circuits.json against the real calendar."
    )


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
                     driver: str = "VER", lap_number: int | None = None) -> pd.DataFrame | None:
    """
    Downloads one lap's telemetry for a driver from FastF1.

    lap_number: a specific lap to use instead of the fastest one. Matters
    for the race_pace scenario in energy_model.py, which assumes ~50% fuel
    load (SCENARIOS["race_pace"]) — the fastest lap of a race is typically
    an early, low-fuel lap, not representative of that assumption the way a
    lap from the middle of the race is. Defaults to None (the fastest lap),
    which is still the right choice for a quick/one-off qualifying-style
    lookup.

    Returns None on any failure — missing dependency, unknown event, no data
    published yet, or no matching lap for this driver — so the API degrades
    to "no simulation available" rather than returning a 500.

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

        # Resolve via the season schedule and fetch BY ROUND NUMBER, rather
        # than handing a name to FastF1's fuzzy lookup — see
        # resolve_fastf1_event for why that matters (it silently returned
        # Austrian GP data for 'great_britain' once).
        event = resolve_fastf1_event(circuit_id, year)
        log.info(
            f"'{circuit_id}' -> {year} round {int(event['RoundNumber'])}: "
            f"{event['EventName']} ({event['Location']})"
        )
        ses = fastf1.get_session(year, int(event["RoundNumber"]), session_code)
        ses.load(telemetry=True, laps=True, weather=False)

        driver_laps = ses.laps.pick_drivers(driver)
        if lap_number is not None:
            matching = driver_laps.pick_laps(lap_number)
            lap = matching.iloc[0] if len(matching) else None
            if lap is None:
                log.error(f"No lap {lap_number} found for {driver} at {circuit_id}")
                return None
        else:
            lap = driver_laps.pick_fastest()
            if lap is None:
                log.error(f"No fastest lap found for {driver} at {circuit_id}")
                return None

        tel = lap.get_car_data().add_distance()
        df  = tel[["Distance", "Speed", "Throttle", "Brake", "RPM", "nGear"]].copy()
        # Real seconds since the lap started — telemetry's own Time column
        # (a timedelta), converted to a plain float so it round-trips
        # through CSV. Real samples aren't evenly spaced; the energy model
        # needs the actual per-sample dt, not an assumed fixed one.
        df["ElapsedSec"] = (tel["Time"] - tel["Time"].iloc[0]).dt.total_seconds()

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
        circuit_name: deprecated/ignored — FastF1 lookup now resolves the
                      event from circuits.json via the season schedule (see
                      resolve_fastf1_event). Kept so existing callers don't
                      break.
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

    # circuit_id (not circuit_name) — load_from_fastf1 resolves the event
    # from circuits.json via the season schedule now, so it needs our id.
    return load_from_fastf1(circuit_id, session)


# ── Race-proxy fallback (future/unraced events) ─────────────────────────────
#
# For a round that hasn't been run yet this season, there's no race telemetry
# to cache. Rather than falling back to a previous season (car/PU
# characteristics change too much year to year), we use telemetry from
# earlier in the SAME weekend instead: on a sprint weekend, the actual Sprint
# Race (or Sprint Qualifying) is genuine competitive racing data and a much
# better stand-in for race pace than a practice run; FP1 is the universal
# last resort since it's the one session every weekend runs before anything
# else. This only ever applies to `session="race"` simulation requests — see
# app.py's /api/simulate route.

def race_proxy_session_order(circuit_id: str) -> list[str]:
    """
    Ordered list of session keys to try, in preference order, as a stand-in
    for real race telemetry that doesn't exist yet for this circuit. Sprint
    weekends try real sprint session data first; every weekend falls back to
    FP1 last.
    """
    circuits = _load_circuits_json()
    circuit = circuits.get("circuits", {}).get(resolve_circuit_id(circuit_id), {})
    has_sprint = circuit.get("has_sprint", False)

    order = ["sprint_race", "sprint_qualifying"] if has_sprint else []
    order.append("fp1")
    return order


def load_race_proxy_telemetry(circuit_id: str, circuit_name: str | None = None,
                              allow_fastf1: bool = False) -> tuple[pd.DataFrame | None, str | None]:
    """
    Tries each session in race_proxy_session_order() in turn via the normal
    load_telemetry() path, returning the first one found. Returns
    (dataframe, session_used), or (None, None) if nothing is available
    anywhere yet (e.g. the weekend hasn't started).
    """
    for session in race_proxy_session_order(circuit_id):
        df = load_telemetry(circuit_id, session, circuit_name, allow_fastf1)
        if df is not None:
            return df, session
    return None, None


# ── Cache generation (run locally) ───────────────────────────────────────────

def export_telemetry_csv(circuit_id: str, session: str = "race",
                         year: int = 2026, driver: str = "VER",
                         circuit_name: str | None = None,
                         lap_number: int | None = None) -> bool:
    """
    Downloads telemetry via FastF1 and writes it to the cache directory so it
    can be committed. Run this locally, never on the server.
    """
    df = load_from_fastf1(circuit_id, session, year, driver, lap_number)
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
    #   python backend/telemetry.py export miami 2026 R VER 35
    #   python backend/telemetry.py check  miami race
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    command    = sys.argv[1]
    circuit_id = sys.argv[2]

    if command == "export":
        year       = int(sys.argv[3]) if len(sys.argv) > 3 else 2026
        session    = normalize_session(sys.argv[4]) if len(sys.argv) > 4 else "race"
        driver     = sys.argv[5] if len(sys.argv) > 5 else "VER"
        lap_number = int(sys.argv[6]) if len(sys.argv) > 6 else None
        ok = export_telemetry_csv(circuit_id, session, year, driver, lap_number=lap_number)
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
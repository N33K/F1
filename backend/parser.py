import re
import json
import logging
import urllib.request
import os
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)

# Matches a sequence of MJ values on a single line
# e.g. "8.0 MJ 8.5 MJ 6.0 MJ 8.5 MJ 8.5 MJ"
# Captures each numeric value separately
MJ_LINE = re.compile(
    r"(\d+(?:\.\d+)?)\s*MJ\s+"   # value 1 — race, no overtake
    r"(\d+(?:\.\d+)?)\s*MJ\s+"   # value 2 — race, with overtake
    r"(\d+(?:\.\d+)?)\s*MJ\s+"   # value 3 — qualifying
    r"(\d+(?:\.\d+)?)\s*MJ\s+"   # value 4 — free practice
    r"(\d+(?:\.\d+)?)\s*MJ",     # value 5 — out laps
    re.IGNORECASE
)

# Article C5.12.8's "Maximum PU Power Reduction Rate" pair, which the FIA
# prints on the same line as the MJ values above:
#   "8.0 MJ 8.5 MJ 7.0 MJ 8.5 MJ 8.5 MJ 3518 m 50 kW/s"
#                                      ^^^^^^ ^^^^^^^^
#                                      PLD    rate limit
# Matched separately rather than by extending MJ_LINE, so a document that
# omits this section (or changes its layout) still yields the MJ table
# instead of failing both — same "always works" pattern used elsewhere.
# The "<number> m <number> kW/s" adjacency is specific enough not to collide
# with the sector tables' own distances ("4100-4550", "350kW") or the
# Detection/Activation lines ("2805 m 2950 m").
POWER_REDUCTION_LINE = re.compile(
    r"(\d+(?:\.\d+)?)\s*m\s+"        # Power Limited Distance, meters
    r"(\d+(?:\.\d+)?)\s*kW/s",       # Maximum PU Power Reduction Rate
    re.IGNORECASE
)


# Per-circuit sector tables the FIA publishes under "IDENTIFICATION OF
# SECTORS WHERE EXCEPTIONS APPLY". Keyed by the article number that appears
# in each table's own header cell, mapped to the name we store it under.
#
# These can't be pulled with a line regex the way the MJ figures can: three
# articles sit side by side in ONE table, and each cell holds a newline-
# separated list (sectors in one column, their lap-distance windows in the
# next). pdfplumber's table extraction keeps that structure intact, so the
# columns are matched by locating each article's header cell and taking the
# columns up to the next article's.
SECTOR_ARTICLES = {
    "C5.2.8iii": "alt_power_curve",        # where the Alt-1 power curve applies (Sprint & Race)
    "C5.12.4":   "greater_reduction",      # where the initial power cut may exceed 150kW
    "C5.12.5":   "power_reduction_reset",  # where an applied power reduction may be reset
    "C5.12.7":   "higher_speed_threshold", # where the 210kph rate-limit carve-out uses a higher speed
}


def _cell_items(cell) -> list[str]:
    """A table cell holds a newline-separated list. Returns its entries with
    blanks and the FIA's '-' placeholder ("no such sectors") removed."""
    if not cell:
        return []
    return [line.strip() for line in str(cell).split("\n")
            if line.strip() and line.strip() != "-"]


def _parse_number(text: str) -> float | None:
    """First number in a string — '350kW' -> 350, '4650 m (TBC)' -> 4650."""
    match = re.search(r"(\d+(?:\.\d+)?)", str(text).replace(",", ""))
    return float(match.group(1)) if match else None


def _parse_distance_m(text: str) -> float | None:
    """A distance explicitly given in metres — '4047 m' -> 4047.0. Returns
    None for a cell with no metre value (e.g. the FIA's 'TBC' placeholder),
    so an unpublished figure isn't confused with a lap marker like 'L24'."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*m\b", str(text))
    return float(match.group(1)) if match else None


def _parse_window(text: str) -> tuple[float, float] | None:
    """'820-2100' -> (820.0, 2100.0). Square brackets mark SQ/Q-only sectors
    and are stripped here — the caller reads them via _is_quali_only."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", str(text))
    return (float(match.group(1)), float(match.group(2))) if match else None


def _is_quali_only(text: str) -> bool:
    """The FIA brackets sectors that apply only to Sprint Qualifying and
    Qualifying: '[Exit T14]', '[3750-4500]'."""
    return "[" in str(text)


def extract_text_from_pdf(pdf_path: str) -> str:
    """
    Reads a PDF file and returns all text as a single string.
    Works on both local file paths and downloaded temp files.
    
    pdfplumber is better than PyPDF2 for tables because it
    preserves the spatial layout of text on each page.
    """
    try:
        import pdfplumber
    except ImportError:
        log.error("pdfplumber not installed. Run: pip install pdfplumber")
        return ""

    text_parts = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            page_text = page.extract_text()
            if page_text:
                text_parts.append(page_text)

    full_text = "\n".join(text_parts)
    log.info(f"Extracted {len(full_text)} characters from {len(text_parts)} pages")
    return full_text


def parse_energy_table(text: str, event_name: str, pdf_url: str) -> dict | None:
    """
    Searches extracted PDF text for the Article C5.2.10 energy table
    and returns a structured dictionary of MJ values per session.

    Returns None if the table cannot be found — meaning this PDF
    is not a Power Unit Information document, or its format changed.
    """

    # First confirm this is actually a Power Unit Information document
    if "Maximum Recharge per lap" not in text:
        log.warning(f"No energy table found in PDF for {event_name}")
        return None

    # Find the MJ value line
    match = MJ_LINE.search(text)

    if not match:
        log.warning(
            f"Energy table header found but MJ values not extracted for "
            f"{event_name} — table format may have changed"
        )
        return None

    # Extract all five values
    race_no_overtake = float(match.group(1))
    race_overtake    = float(match.group(2))
    qualifying       = float(match.group(3))
    free_practice    = float(match.group(4))
    out_laps         = float(match.group(5))

    log.info(
        f"{event_name} → Race: {race_no_overtake}/{race_overtake} MJ | "
        f"Quali: {qualifying} MJ | FP: {free_practice} MJ"
    )

    result = {
        "event":           event_name,
        "source_url":      pdf_url,
        "extracted_at":    datetime.utcnow().isoformat(),
        "limits": {
            "race": {
                "overtake_inactive": race_no_overtake,
                "overtake_active":   race_overtake,
            },
            "qualifying":    qualifying,
            "free_practice": free_practice,
            "out_laps":      out_laps,
        }
    }

    # Article C5.12.8 — how fast the PU may ramp deployment power down, and
    # the Power Limited Distance the FIA derived that rate from. Optional:
    # a document without this section still yields the MJ limits above.
    power_reduction = POWER_REDUCTION_LINE.search(text)
    if power_reduction:
        result["power_reduction"] = {
            "power_limited_distance_m": float(power_reduction.group(1)),
            "rate_limit_kw_per_s":      float(power_reduction.group(2)),
        }
        log.info(
            f"{event_name} → Power Limited Distance: "
            f"{result['power_reduction']['power_limited_distance_m']} m | "
            f"Rate limit: {result['power_reduction']['rate_limit_kw_per_s']} kW/s"
        )
    else:
        log.warning(
            f"No Article C5.12.8 power-reduction values found for {event_name} "
            f"— energy_model will fall back to its default ramp-down rate"
        )

    return result


def _article_columns(header_row: list) -> list[tuple[str, int, int]]:
    """
    Locates each article within a table's header row, returning
    (article, first_column, last_column) per article. One table can hold
    several articles side by side (C5.2.8iii | C5.12.4 | C5.12.5), each
    spanning however many columns sit before the next article's header.
    """
    found = []
    for col, cell in enumerate(header_row):
        if not cell:
            continue
        text = str(cell)
        for article in SECTOR_ARTICLES:
            # "C5.12.4" would also match inside "C5.2.8iii"-free text, so
            # anchor on the article label the FIA actually prints.
            if f"Article {article}" in text:
                found.append((article, col))
                break

    spans = []
    for i, (article, start) in enumerate(found):
        end = found[i + 1][1] - 1 if i + 1 < len(found) else len(header_row) - 1
        spans.append((article, start, end))
    return spans


def parse_sector_tables(pdf_path: str) -> dict:
    """
    Extracts the per-circuit sector tables and the Overtake detection/
    activation lines from a Power Unit Information PDF.

    Returns {"sectors": {...}, "overtake": {...}}, omitting anything the
    document doesn't define. Early-season documents (rounds 1-3) carry no
    C5.2.8iii or C5.12.7 table at all — Alt-1 was introduced mid-season —
    and a circuit with no reset sectors prints "-", which yields an empty
    list. Both are normal, not parse failures.
    """
    try:
        import pdfplumber
    except ImportError:
        log.error("pdfplumber not installed. Run: pip install pdfplumber")
        return {}

    sectors: dict[str, list] = {}
    overtake: dict[str, float] = {}

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables():
                if not table or not table[0]:
                    continue

                flat = " ".join(str(c) for row in table for c in row if c)

                # --- Overtake (Article B7.2): detection gap + two lines ---
                if "Detection Gap" in flat and "Activation Line" in flat:
                    data = table[-1]
                    if len(data) >= 3:
                        gap = _parse_number(data[0])
                        # These cells pair a distance with a lap marker
                        # ("4047 m\nL19"), so require the metre unit — some
                        # events publish "TBC\nL24" instead, where grabbing
                        # the first number would silently yield a lap number.
                        detection = _parse_distance_m(data[1])
                        activation = _parse_distance_m(data[2])
                        if gap is not None:
                            overtake["detection_gap_s"] = gap
                        if detection is not None:
                            overtake["detection_line_m"] = detection
                        if activation is not None:
                            overtake["activation_line_m"] = activation
                    continue

                # --- Sector tables, possibly several per table ---
                spans = _article_columns(table[0])
                if not spans:
                    continue
                # Row 0 holds the article headers and the final row the data
                # (one newline-separated list per cell). The column-label row
                # is usually row 1, but some tables (C5.12.7) put a subtitle
                # there and push the labels to row 2 — so find it by content.
                labels = next(
                    (row for row in table[1:-1]
                     if any(cell and "Identification" in str(cell) for cell in row)),
                    table[1],
                )
                data = table[-1]

                for article, start, end in spans:
                    key = SECTOR_ARTICLES[article]
                    cols = list(range(start, min(end, len(data) - 1) + 1))

                    def column_for(fragment, cols=cols):
                        for c in cols:
                            if c < len(labels) and labels[c] and fragment in str(labels[c]):
                                return c
                        return None

                    ident_col = column_for("Identification")
                    window_col = column_for("Lap distance window")
                    if ident_col is None or window_col is None:
                        continue

                    idents = _cell_items(data[ident_col])
                    windows = _cell_items(data[window_col])

                    # Optional third column, present only on some articles.
                    extra_col = column_for("Maximum PU power") or column_for("Speed threshold")
                    extras = _cell_items(data[extra_col]) if extra_col is not None else []

                    entries = []
                    for i, (ident, window) in enumerate(zip(idents, windows)):
                        bounds = _parse_window(window)
                        if bounds is None:
                            continue
                        entry = {
                            "label": ident.strip("[]").strip(),
                            "start_m": bounds[0],
                            "end_m": bounds[1],
                            # Bracketed sectors apply to Sprint Qualifying and
                            # Qualifying only, never to a Sprint or Race.
                            "quali_only": _is_quali_only(ident) or _is_quali_only(window),
                        }
                        if i < len(extras):
                            value = _parse_number(extras[i])
                            if value is not None:
                                if article == "C5.12.4":
                                    entry["max_reduction_kw"] = value
                                elif article == "C5.12.7":
                                    entry["speed_threshold_kmh"] = value
                        entries.append(entry)

                    sectors.setdefault(key, []).extend(entries)

    result = {}
    if sectors:
        result["sectors"] = sectors
    if overtake:
        result["overtake"] = overtake
    return result


def save_result(result: dict, overrides_path: str) -> None:
    """
    Adds a parsed result to overrides.json.
    If an entry for the same event already exists, it is replaced
    rather than duplicated — so re-running the parser stays safe.
    """
    try:
        with open(overrides_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {"events": []}

    # Remove any existing entry for this event (deduplication)
    data["events"] = [
        e for e in data["events"]
        if e.get("event") != result["event"]
    ]

    # Add the new result
    data["events"].append(result)

    # Sort by event name for readability
    data["events"].sort(key=lambda x: x["event"])

    with open(overrides_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    log.info(f"Saved result for {result['event']} to {overrides_path}")


if __name__ == "__main__":

    base_dir       = os.path.dirname(os.path.abspath(__file__))
    overrides_path = os.path.join(base_dir, "../data/overrides.json")

    # Test files — replace these paths with wherever you saved the PDFs
    test_pdfs = [
        {
            "path":  os.path.join(base_dir, "C:\\Users\\N33K\\Documents\\Projects\\f1-energy-decoder\\data\\2026 Monaco Grand Prix - Power Unit Information.pdf"),
            "event": "monaco",
            "url":   "https://www.fia.com/system/files/decision-document/2026_monaco_grand_prix_-_power_unit_information.pdf"
        },
        {
            "path":  os.path.join(base_dir, "C:\\Users\\N33K\\Documents\\Projects\\f1-energy-decoder\\data\\2026 Canadian Grand Prix - Power Unit Information.pdf"),
            "event": "canada",
            "url":   "https://www.fia.com/system/files/decision-document/2026_canadian_grand_prix_-_power_unit_information.pdf"
        },
    ]

    for item in test_pdfs:
        log.info(f"--- Processing {item['event']} ---")

        if not os.path.exists(item["path"]):
            log.error(f"File not found: {item['path']}")
            continue

        text   = extract_text_from_pdf(item["path"])
        result = parse_energy_table(text, item["event"], item["url"])

        if result:
            save_result(result, overrides_path)
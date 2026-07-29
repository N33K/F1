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

    return {
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
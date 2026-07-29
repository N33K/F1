import urllib.request
import urllib.parse
import urllib.error
import re
import json
import os
import logging
from datetime import datetime


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
log = logging.getLogger(__name__)


def load_fia_event_names(circuits_path: str) -> dict:
    """
    Loads the FIA event name mapping from circuits.json.
    Returns a dict of {circuit_id: official_grand_prix_name}
    """
    with open(circuits_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    return {
        circuit_id: circuit["name"]
        for circuit_id, circuit in data["circuits"].items()
    }


# Base URL for FIA documents pages
FIA_DOCS_BASE = (
    "https://www.fia.com/documents/championships/"
    "fia-formula-one-world-championship-14/"
    "season/season-2026-2072/event/"
)

# Pattern to identify Power Unit Information PDF links in the page HTML
PU_INFO_PATTERN = re.compile(
    r'href="([^"]*power_unit_information[^"]*\.pdf)"',
    re.IGNORECASE
)

def fetch_documents_page(circuit_id: str, fia_event_names: dict) -> str | None:
    """
    Fetches the FIA documents page for a given circuit
    and returns the raw HTML as a string.
    Returns None if the page cannot be reached.
    """
    fia_event_name = fia_event_names.get(circuit_id)

    if not fia_event_name:
        log.error(f"No FIA event name found for circuit ID '{circuit_id}'")
        return None

    # URL-encode the event name: spaces become %20
    encoded_name = urllib.parse.quote(fia_event_name)
    url = FIA_DOCS_BASE + encoded_name

    log.info(f"Fetching documents page: {url}")

    try:
        # Some servers reject requests without a User-Agent header
        # We identify ourselves honestly as a research tool
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "F1EnergyDecoder/1.0 (research tool)"}
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            html = response.read().decode("utf-8", errors="replace")
            log.info(f"Page fetched successfully ({len(html)} characters)")
            return html

    except urllib.error.HTTPError as e:
        log.error(f"HTTP {e.code} fetching documents page for {circuit_id}")
        return None
    except urllib.error.URLError as e:
        log.error(f"Network error fetching {circuit_id}: {e.reason}")
        return None
    except Exception as e:
        log.error(f"Unexpected error fetching {circuit_id}: {e}")
        return None


def find_pu_pdf_url(html: str, circuit_id: str) -> str | None:
    """
    Scans the FIA documents page HTML for a Power Unit Information
    PDF link and returns the full URL.
    Returns None if no such link is found.
    """
    matches = PU_INFO_PATTERN.findall(html)

    if not matches:
        log.warning(f"No Power Unit Information PDF found for {circuit_id}")
        return None

    if len(matches) > 1:
        # Multiple matches can happen if the FIA published a revised version
        # Always take the last one — it will be the most recent revision
        log.info(
            f"Found {len(matches)} PU PDF links for {circuit_id} "
            f"— using most recent"
        )
        pdf_url = matches[-1]
    else:
        pdf_url = matches[0]

    # Some links on FIA pages are relative paths — make them absolute
    if pdf_url.startswith("/"):
        pdf_url = "https://www.fia.com" + pdf_url

    log.info(f"Found PDF URL: {pdf_url}")
    return pdf_url


def download_pdf(pdf_url: str, destination_path: str) -> bool:
    """
    Downloads a PDF from the given URL and saves it to destination_path.
    Returns True on success, False on failure.
    """
    log.info(f"Downloading PDF to {destination_path}")

    try:
        request = urllib.request.Request(
            pdf_url,
            headers={"User-Agent": "F1EnergyDecoder/1.0 (research tool)"}
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            pdf_bytes = response.read()

        with open(destination_path, "wb") as f:
            f.write(pdf_bytes)

        log.info(f"Downloaded {len(pdf_bytes):,} bytes")
        return True

    except Exception as e:
        log.error(f"Failed to download PDF: {e}")
        return False


def fetch_and_parse(circuit_id: str, data_dir: str, overrides_path: str) -> bool:
    """
    Full pipeline for one circuit:
    1. Fetch the FIA documents page
    2. Find the Power Unit Information PDF link
    3. Download the PDF
    4. Parse it (calls parser.py)
    5. Save results to overrides.json

    Returns True if a result was saved, False otherwise.
    """
    # Import parser here to avoid circular imports
    # (parser.py also imports from this module in some setups)
    from parser import extract_text_from_pdf, parse_energy_table, save_result

    circuits_path   = os.path.join(data_dir, "circuits.json")
    fia_event_names = load_fia_event_names(circuits_path)

    # Step 1 — fetch documents page
    html = fetch_documents_page(circuit_id, fia_event_names)
    if not html:
        return False

    # Step 2 — find PDF URL
    pdf_url = find_pu_pdf_url(html, circuit_id)
    if not pdf_url:
        return False

    # Step 3 — download PDF to a temp path in data/
    pdf_filename = f"pu_info_{circuit_id}.pdf"
    pdf_path     = os.path.join(data_dir, pdf_filename)

    success = download_pdf(pdf_url, pdf_path)
    if not success:
        return False

    # Step 4 — parse
    text   = extract_text_from_pdf(pdf_path)
    result = parse_energy_table(text, circuit_id, pdf_url)

    if not result:
        return False

    # Step 5 — save
    save_result(result, overrides_path)
    return True


if __name__ == "__main__":
    import sys

    base_dir       = os.path.dirname(os.path.abspath(__file__))
    data_dir       = os.path.join(base_dir, "../data")
    overrides_path = os.path.join(data_dir, "overrides.json")

    # Accept a circuit ID as command line argument
    # Usage: python backend/fetcher.py canada
    # Default to "hungary" (next upcoming race) if none provided
    circuit_id = sys.argv[1] if len(sys.argv) > 1 else "hungary"

    log.info(f"=== Fetching PU information for: {circuit_id} ===")
    success = fetch_and_parse(circuit_id, data_dir, overrides_path)

    if success:
        log.info(f"=== Done — {circuit_id} saved to overrides.json ===")
    else:
        log.error(f"=== Failed to fetch data for {circuit_id} ===")
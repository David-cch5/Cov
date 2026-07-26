"""Fidlar "AVA" vendor adapter (`ava.fidlar.com/<StateCounty>/AvaWeb`).
Confirmed in use by Kerr County (ava.fidlar.com/TXKerr/AvaWeb) -- fully open
to guests, no login.

Angular Material form with no stable element IDs (`mat-input-0`, `-1`, ...
renumber depending on what else is on the page), so fields are targeted by
their visible placeholder text instead, which is stable.

For a search that returns exactly one result, AVA renders that document's
full detail (Page Count, Parties, Legals, Notes) inline in the same response
-- no extra click needed, which covers this project's main use case (checking
a specific book/page or a fairly distinctive declarant name). Confirmed
multiple-result behavior (e.g. searching a bare common surname like "SMITH")
returns nothing at all here -- Kerr's own search requires the name "spelled
out" per its help text (e.g. "DOE JOHN A.", not just "DOE") -- so a bare
last name is not a reliable multi-result probe for this vendor; how a
genuine multi-row result set exposes each row's own detail panel was not
determined and is a real gap, not assumed to work.
"""
import re

from playwright.sync_api import BrowserContext

BASE_PATH = "/AvaWeb"

_FIELD_PLACEHOLDERS = {
    "last_name": "Last Name / Business Name",
    "first_name": "First Name",
    "document_number": "Document Number",
    "book": "Book",
    "page": "Page",
    "reference_number": "Reference Number",
    "subdivision_name": "Subdivision Name",
    "lot": "Lot",
    "block": "Block",
    "house_no": "House No",
    "street": "Street",
    "city": "City",
    "zipcode": "Zipcode",
}


def _fill_with_retry(page, selector: str, value: str, attempts: int = 4) -> None:
    """page.fill() on this Angular form intermittently times out on a
    locator Playwright's own is_visible() already reports true for --
    confirmed repeatedly, not a one-off fluke, and a fixed settle delay
    after page load didn't reliably fix it either. Angular Material tends
    to destroy/recreate form controls during its own hydration passes, so
    retrying against a freshly-queried locator each time is more robust than
    a longer single wait."""
    last_error = None
    for attempt in range(attempts):
        try:
            page.wait_for_timeout(750 * (attempt + 1))
            page.fill(selector, value, timeout=8000)
            return
        except Exception as e:
            last_error = e
    raise last_error


def search(context: BrowserContext, base_url: str, **fields) -> dict:
    """Fill any subset of _FIELD_PLACEHOLDERS' keys (e.g. book="1765",
    page="243", or last_name="TORRES ELAINE") and submit. Returns
    {"result_count": int, "documents": [...]} -- each document dict has
    whatever of document_no/doc_type/recorded_date/ref_no/book_page/
    page_count/parties/legal_notes could be parsed from the response text.
    When exactly one result comes back, page_count/parties/legal_notes are
    populated directly (see module docstring); with more than one, only the
    summary grid fields are populated."""
    unknown = set(fields) - set(_FIELD_PLACEHOLDERS)
    if unknown:
        raise ValueError(f"unknown AVA search field(s): {unknown}")

    page = context.new_page()
    try:
        page.goto(f"{base_url}{BASE_PATH}", wait_until="networkidle")
        for key, value in fields.items():
            placeholder = _FIELD_PLACEHOLDERS[key]
            _fill_with_retry(page, f'input[placeholder="{placeholder}"]', value)
        page.click('button:has-text("SEARCH")')
        page.wait_for_timeout(2500)
        return _parse_results(page.inner_text("body"))
    finally:
        page.close()


def _parse_results(text: str) -> dict:
    count_match = re.search(r"Results:\s*(\d+)", text)
    result_count = int(count_match.group(1)) if count_match else 0

    doc = {}
    ref_match = re.search(r"Ref No:(.+)", text)
    if ref_match:
        doc["ref_no"] = ref_match.group(1).strip()
    bp_match = re.search(r"B:(\d+)\s*P:(\d+)", text)
    if bp_match:
        doc["book"], doc["page"] = bp_match.group(1), bp_match.group(2)
    pc_match = re.search(r"Page Count:(\d+)", text)
    if pc_match:
        doc["page_count"] = int(pc_match.group(1))
    notes_match = re.search(r"Notes:\s*\n?(.+)", text)
    if notes_match:
        doc["legal_notes"] = notes_match.group(1).strip()

    parties_match = re.search(r"Party 1:\s*\n(.*?)Party 2:\s*\n(.*?)(?:Legals|Additional)", text, re.DOTALL)
    if parties_match:
        doc["party1"] = [p.strip() for p in parties_match.group(1).strip().split("\n") if p.strip()]
        doc["party2"] = [p.strip() for p in parties_match.group(2).strip().split("\n") if p.strip()]

    return {"result_count": result_count, "documents": [doc] if doc else []}

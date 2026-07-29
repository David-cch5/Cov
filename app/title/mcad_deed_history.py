"""Montgomery Central Appraisal District (MCAD, mcad-tx.org) per-parcel deed
history -- a structured, APN-indexed grantor/grantee/date/instrument table
(the "Deed History" section of each property's detail page). Confirmed live
(2026-07-29): gave a complete, 14-hop chain of title back to 1996 for a real
parcel (APN 41116) that this project's own recorder-portal name-search
couldn't reconstruct at all -- since it's indexed by the PARCEL itself, it
sidesteps the exact problem that broke the name-search there: the covenant's
own extracted declarant name ("ANANTA LLC") differed from the actual grantor
on the real conveyances ("ANANTA PARTNERS LLC", a related-but-distinct
entity). MCAD's own data even self-corrects: one instrument had been
mistakenly associated with the wrong parcel and was marked "DELETED" in
their own records rather than left silently wrong.

No public REST API found after directly testing for one (intercepting both
window.fetch and XMLHttpRequest.prototype.open around the actual search/
detail-page load, the same techique that ruled one out for GovOS
PublicSearch -- see app/recorder/session.py's own docstring) -- a
Playwright-rendered page, same as the recorder-portal adapters, not a REST
client.

Texas is a non-disclosure state: no consideration amount here, matching
every other Texas source this project already uses (cad_deed_history.py,
the recorder-portal adapters) -- grantor/grantee/date/deed-type/instrument
only, never a price.
"""
import re

from playwright.sync_api import BrowserContext

DEFAULT_TIMEOUT = 30000
SEARCH_BOX_SELECTOR = 'input[placeholder="Search by Account Number, Address or Owner Name"]'

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def fetch_deed_history(context: BrowserContext, base_url: str, apn: str) -> list[dict]:
    """Every deed-history row MCAD lists for the given APN/account number,
    newest-first as MCAD itself orders them, each: {"deed_date", "deed_type",
    "description", "grantor", "grantee", "book", "volume", "page",
    "instrument"}. Returns [] if the APN isn't found (never raises just
    because a specific parcel has no MCAD record)."""
    page = context.new_page()
    try:
        page.goto(f"{base_url}/property-search", wait_until="networkidle", timeout=DEFAULT_TIMEOUT)
        page.fill(SEARCH_BOX_SELECTOR, apn)
        page.click('button[aria-label="Search properties"], button:has-text("Search properties")')
        row_button = page.locator(f'button:has-text("{apn}")').first
        try:
            row_button.wait_for(timeout=DEFAULT_TIMEOUT)
        except Exception:
            return []  # no result row for this APN -- not found, not an error
        row_button.click()
        page.wait_for_selector("text=Deed History", timeout=DEFAULT_TIMEOUT)
        # Confirmed live: the Deed History table is an AG-Grid virtualized grid -- only
        # rows currently scrolled into view exist in the DOM. Without this,
        # inner_text("body") captures just the header row before jumping straight to
        # unrelated footer content (Address/Mailing/Phone), silently dropping every
        # actual deed row.
        page.locator("text=Deed History").scroll_into_view_if_needed()
        page.wait_for_timeout(1500)
        body_text = page.inner_text("body")
        return _parse_deed_history(body_text)
    finally:
        page.close()


def _parse_deed_history(body_text: str) -> list[dict]:
    """MCAD renders each deed-history row as 9 tab-separated fields (confirmed
    live against APN 41116's real 11-row history): deed_date, deed_type,
    description, grantor, grantee, book, volume, page, instrument -- the
    trailing 4 (book/volume/page/instrument) are often blank depending on
    whether the deed was indexed by book/volume/page (older deeds) or by
    instrument number (modern ones), never both. Splitting on tabs directly
    is more robust than a single regex with optional groups, since a blank
    field still produces an empty string between two tabs rather than
    collapsing away."""
    if "Deed History" not in body_text:
        return []
    section = body_text.split("Deed History", 1)[1]
    rows = []
    for line in section.splitlines():
        parts = line.split("\t")
        if not parts or not _DATE_RE.match(parts[0]):
            continue  # not a data row (header row, or footer content after the table)
        parts += [""] * (9 - len(parts))  # tolerate a short trailing row missing blank tail fields
        deed_date, deed_type, description, grantor, grantee, book, volume, page_no, instrument = parts[:9]
        rows.append({
            "deed_date": deed_date, "deed_type": deed_type, "description": description,
            "grantor": grantor.strip(), "grantee": grantee.strip(),
            "book": book or None, "volume": volume or None, "page": page_no or None,
            "instrument": instrument or None,
        })
    return rows

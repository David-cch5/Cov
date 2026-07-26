"""Harris Recording Solutions "Acclaim" vendor adapter
(`<host>/AcclaimWeb/...`). Confirmed in use by Ellis County
(ellisccktxpublicsearch.us).

Unlike PublicSearch, this vendor's search results ARE a clean JSON response
(POST .../Search/GetSearchResults -> {"Data": [...]}), captured directly via
a Playwright response listener rather than DOM-scraped -- more robust, and
returns every row in one call instead of a paginated grid.

Document detail (page count, full grantor/grantee list) and the raw page
images live behind a document-detail view that opens in a NEW TAB/POPUP when
a search-result row is clicked -- confirmed the hard way: neither a
Playwright locator `.click()`, a raw coordinate `page.mouse.click()`, nor a
dispatched `MouseEvent` navigates the *current* page. `context.expect_page()`
is required to catch the popup; this is the actual mechanism, not a bug in
any of those click methods.

This adapter is also the path to diagnosing a missing-Exhibit-A recording
(see app/recorder/diagnose.py): `get_document_detail`'s `number_of_pages`
compares directly against the local PDF's page count, and
`get_page_image_bytes` fetches whichever pages our copy is missing so they
can go through vision OCR (app/ocr/vision_ocr.py) instead of a human paging
through the portal by hand.
"""
import re
from urllib.parse import urlencode, urlparse, parse_qs, urlunparse

from playwright.sync_api import BrowserContext

SEARCH_PATH = "/AcclaimWeb/Search/SearchTypeName"


def search_by_name(context: BrowserContext, base_url: str, name: str) -> list[dict]:
    """Name search -- returns every row from the portal's own JSON response
    (party, doc type, instrument number, recorded date, and
    DocLegalDescription -- often just "SEE INSTRUMENT" but sometimes carries
    the real legal description directly, e.g. book/page reconciliation)."""
    page = context.new_page()
    try:
        page.goto(f"{base_url}{SEARCH_PATH}", wait_until="networkidle")
        page.fill("#Name", name)
        with page.expect_response(lambda r: "GetSearchResults" in r.url, timeout=20000) as resp_info:
            page.click("#SearchBtn")
        return resp_info.value.json().get("Data", [])
    finally:
        page.close()


def get_document_detail(context: BrowserContext, base_url: str, name_for_search: str, instrument_number: str) -> dict | None:
    """Re-runs the name search (Acclaim's guest access has no direct
    search-by-instrument-number form -- see county_recorder_registry quirks)
    and clicks through to the matched row's Document Details popup. Returns
    number_of_pages, the raw detail text (parties/legal description/book-page),
    and an image_handler_url usable with get_page_image_bytes."""
    page = context.new_page()
    try:
        page.goto(f"{base_url}{SEARCH_PATH}", wait_until="networkidle")
        page.fill("#Name", name_for_search)
        with page.expect_response(lambda r: "GetSearchResults" in r.url, timeout=20000):
            page.click("#SearchBtn")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(500)

        loc = page.locator("tr", has_text=instrument_number).first
        if loc.count() == 0:
            return None
        loc.scroll_into_view_if_needed()
        with context.expect_page(timeout=10000) as popup_info:
            loc.click()
        detail_page = popup_info.value
        detail_page.wait_for_load_state("networkidle")
        try:
            return _parse_document_detail(detail_page)
        finally:
            detail_page.close()
    finally:
        page.close()


def _parse_document_detail(detail_page) -> dict:
    text = detail_page.inner_text("body")
    pages_match = re.search(r"Number Of Pages\s*\n\s*(\d+)", text)
    img = detail_page.query_selector("img[src*='atala_docurl']")
    origin = f"{urlparse(detail_page.url).scheme}://{urlparse(detail_page.url).netloc}"
    return {
        "number_of_pages": int(pages_match.group(1)) if pages_match else None,
        "raw_text": text,
        "image_handler_url": img.get_attribute("src") if img else None,
        "origin": origin,
    }


def get_page_image_bytes(context: BrowserContext, detail: dict, page_index: int, zoom: float = 1.0) -> bytes:
    """Fetch one page's raw image bytes from the document viewer, given the
    `detail` dict returned by get_document_detail (which carries the
    session-scoped atala_docurl cache-file reference -- NOT stable across
    sessions, so always get a fresh `detail` right before calling this
    rather than caching image_handler_url long-term). page_index is
    0-based; note the viewer duplicates most pages across two consecutive
    indices (confirmed manually for Ellis covid 8386 -- every physical page
    appeared at two adjacent ataladocpage values), so don't assume
    index == physical page number without checking the page-number stamp
    visible in the returned image."""
    handler_url = detail["image_handler_url"]
    if not handler_url:
        raise RuntimeError("detail dict has no image_handler_url -- call get_document_detail first")
    full_url = handler_url if handler_url.startswith("http") else f"{detail['origin']}{handler_url}"
    parsed = urlparse(full_url)
    params = parse_qs(parsed.query)
    params["ataladocpage"] = [str(page_index)]
    params["atala_doczoom"] = [str(zoom)]
    final_url = urlunparse(parsed._replace(query=urlencode(params, doseq=True)))

    resp = context.request.get(final_url)
    if not resp.ok:
        raise RuntimeError(f"page image fetch failed: {resp.status} {final_url}")
    return resp.body()

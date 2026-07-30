"""GovOS "PublicSearch" vendor adapter (`<county>.tx.publicsearch.us`).
Confirmed in use by Denton, Nueces, Collin, and Bexar (the last via
`bexar.tx.ds.search.govos.com`, a differently-hosted but identically-behaving
instance of the same product) -- likely many more Texas counties given how
common this vendor is.

The results table's columns vary per county (Bexar adds NCB/COUNTY BLOCK/
PROPERTY ADDRESS that Denton/Nueces/Collin don't have), so rows are parsed by
mapping each `<thead>` header text to its column index dynamically rather than
assuming a fixed schema -- confirmed empty leading columns (checkbox/expand
icons) exist in every county tested, so empty header cells are just dropped.

No stable JSON API was found for this vendor after real reverse-engineering
attempts (monkey-patching window.fetch and XMLHttpRequest.prototype.open both
came up empty even though the DOM populates with real data) -- something in
its bundle serves results by a mechanism neither intercepts. Hence: a real
Playwright-rendered page, not a REST client.
"""
import re

from playwright.sync_api import BrowserContext

SEARCH_SCOPE_FULL_TEXT_ID = "withOcr"
SEARCH_BOX_ID = "basicSearchInputBox"


def search_by_name(context: BrowserContext, base_url: str, query: str, full_text_ocr: bool = True) -> list[dict]:
    """Quick-search by grantor/grantee/subdivision/doc-type/doc# text. With
    full_text_ocr=True this also searches the OCR'd body of every document
    (as a phrase, per the portal's own help text), not just indexed fields --
    the only way to find a covenant whose relevant text isn't in the index
    (e.g. searching for an abstract code or a specific dollar figure)."""
    page = context.new_page()
    try:
        page.goto(base_url, wait_until="networkidle")
        if full_text_ocr:
            page.check(f"#{SEARCH_SCOPE_FULL_TEXT_ID}", force=True)
        page.fill(f"#{SEARCH_BOX_ID}", query)
        # not button:has-text("Search") -- confirmed live (Montgomery, Nueces) that it
        # ambiguously matches 2 buttons ("Reset Search" contains "Search" too); Playwright
        # picks the first by DOM order, which happens to be correct today but is fragile.
        # This test-id is confirmed stable across every PublicSearch county checked.
        page.click('[data-testid="searchSubmitButton"]')
        try:
            page.wait_for_selector("table", timeout=20000)
        except Exception:
            return []  # "No Results Found" -- no table renders at all
        page.wait_for_timeout(500)  # table exists before all rows finish populating
        return _parse_results_table(page)
    finally:
        page.close()


def search_plats_by_subdivision(context: BrowserContext, base_url: str, subdivision_name: str) -> list[dict]:
    """Every Plats-department record matching a subdivision name -- confirmed
    live (Montgomery) to return FILE NUMBER/VOL-BK-PG/DOC TYPE/GRANTOR/
    RECORDED DATE/SECTION/ABSTRACT/ABSTRACT NAME columns, one row per
    section/phase (e.g. searching "THE CANOPIES" returns Sections 1, 2, 3,
    4, 18 at once, two different recording dates among them) -- so ONE
    search per base subdivision name resolves every section's own real
    plat date, not one search per section.

    This vendor's quick-search box always defaults to the "Public Records"
    department on a fresh page load; the Department control is a react-
    select combobox (no native <select>, so Playwright's own select_option
    can't drive it) -- confirmed by inspecting the live DOM: click the
    current department label to open it, then click the "Plats" option by
    its text (not by its dynamically-numbered react-select-N-option-M id,
    which is not stable across reloads)."""
    page = context.new_page()
    try:
        page.goto(base_url, wait_until="networkidle")
        page.click("text=Public Records")
        page.wait_for_timeout(300)
        page.click("text=Plats", timeout=5000)
        page.wait_for_timeout(300)
        page.fill(f"#{SEARCH_BOX_ID}", subdivision_name)
        page.click('[data-testid="searchSubmitButton"]')
        try:
            page.wait_for_selector("table", timeout=20000)
        except Exception:
            return []  # "No Results Found" -- no plat found under this name, not an error
        page.wait_for_timeout(500)
        return _parse_results_table(page)
    finally:
        page.close()


def _digits_only(s: str) -> str:
    return re.sub(r"\D", "", s or "")


def search_by_document_number(context: BrowserContext, base_url: str, doc_number: str) -> dict | None:
    """Exact instrument-number lookup. Uses the quick-search box (doc# is one
    of the fields it matches per the portal's own placeholder text) rather
    than the advanced-search field IDs, which were observed to vary/break
    across a page reload in a way the quick box did not.

    Both the QUERY sent to the search box and the DOC NUMBER comparison are
    digits-only, not the raw string: a covenant's own extracted
    recording_instrument sometimes carries a dash as printed on the actual
    document (e.g. "2009-089679", confirmed on covid 4780/Montgomery) even
    though this vendor's own DOC NUMBER field never has one ("2009089679")
    -- every DOC NUMBER seen from this vendor across Montgomery/Denton/
    Nueces/Collin/Bexar has been purely numeric, so stripping to digits is
    safe, not just a Montgomery-specific patch. Stripping the comparison
    alone was confirmed NOT enough on covid 4780: the portal's own quick
    search returns zero rows for a dashed query string, so there's nothing
    left to compare against unless the query itself is normalized first."""
    wanted = _digits_only(doc_number)
    results = search_by_name(context, base_url, wanted, full_text_ocr=False)
    for row in results:
        if _digits_only(row.get("DOC NUMBER", "")) == wanted:
            return row
    return results[0] if len(results) == 1 else None


def _parse_results_table(page) -> list[dict]:
    table = page.query_selector("table")
    if table is None:
        return []
    headers = table.eval_on_selector_all(
        "thead th, tr:first-child th", "els => els.map(e => e.innerText.trim())"
    )
    if not headers:
        return []

    rows = []
    for tr in table.query_selector_all("tbody tr"):
        cells = tr.eval_on_selector_all("td", "els => els.map(e => e.innerText.trim())")
        if not cells:
            continue
        row = {
            headers[i]: cells[i]
            for i in range(min(len(headers), len(cells)))
            if headers[i]  # drop the empty-header checkbox/icon columns
        }
        if row:
            rows.append(row)
    return rows

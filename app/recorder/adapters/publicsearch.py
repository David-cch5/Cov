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
        page.click('button:has-text("Search")')
        try:
            page.wait_for_selector("table", timeout=20000)
        except Exception:
            return []  # "No Results Found" -- no table renders at all
        page.wait_for_timeout(500)  # table exists before all rows finish populating
        return _parse_results_table(page)
    finally:
        page.close()


def search_by_document_number(context: BrowserContext, base_url: str, doc_number: str) -> dict | None:
    """Exact instrument-number lookup. Uses the quick-search box (doc# is one
    of the fields it matches per the portal's own placeholder text) rather
    than the advanced-search field IDs, which were observed to vary/break
    across a page reload in a way the quick box did not."""
    results = search_by_name(context, base_url, doc_number, full_text_ocr=False)
    for row in results:
        if row.get("DOC NUMBER", "").strip() == doc_number.strip():
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

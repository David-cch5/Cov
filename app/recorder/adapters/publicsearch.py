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

RESULTS ARE PAGINATED, AND THIS MODULE READS PAGE ONE. Measured 2026-08-11: a
Nueces search for "PALMILLA BEACH" reports 2,297 results and returns 50 rows. The
page is addressable -- `&offset=50` on the /results URL returns the next 50,
confirmed by a different leading document -- so the "~50-row result cap" described
in app/title/chain.py is really page 1 of N. Every conclusion drawn from a result
set of exactly 50 rows should be treated as truncated until it is paged; that is
how a real unit-4A plat went unfound while an unrelated document was accepted in
its place. Paging is NOT implemented here yet, deliberately: widening what
search_by_name returns changes which documents a chain-of-title walk considers,
and that deserves its own verified change rather than a footnote to this one.

A SHORT PLAT NUMBER IS NOT A UNIQUE DOCUMENT NUMBER. Nueces plat rows carry file
numbers like 46201, 55219, 30286, while its deeds carry 2022003773. Looking up
"46201" with search_by_document_number returns a BACKFILE OIL GAS LEASE at
BFIO/46/201 -- a different series that happens to share the digits. So a document
number taken from a plat row must not be round-tripped through a
document-number search and assumed to be the same instrument.

THE SAME DOCUMENT READS DIFFERENTLY IN TWO DEPARTMENTS. Doc 2019037783 is
"Subdivision- Name: PALMILLA BEACH PUD" in the Plats results and "Subdivision-
Name: PALMILLA BEACH PUD ETAL Lot: 5A Block: 6 Unit: 1E" in Official Public
Records. The unit -- the thing that identifies which filing platted which phase --
is only in the second. Prefer the richer department when the question is "which
phase did this plat create".

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


class RecorderSearchUnanswered(Exception):
    """The portal did not give a usable answer about the documents asked for -- as
    distinct from answering that there are none.

    Its own exception type for the same reason app/gis/ngs.py has NgsUnanswered:
    the two outcomes are identical on the page (no results table renders either
    way) and opposite in meaning. "The portal is down" read as "this parcel has no
    recorded documents" writes an absence of title into the record as though it
    were a finding, and app/title/chain.py's walk would proceed to conclude a
    chain from nothing.

    Raising instead reaches app/queue/job_queue.py's run_with_job_queue, which
    every caller of this module already goes through: retried across a ~76s
    window, then recorded as a durable job_queue row a human can see. A county's
    portal being briefly unavailable is a reason to come back later, never a fact
    about the land."""


class RecorderSearchUnavailable(RecorderSearchUnanswered):
    """The portal itself reported an error running the search. Transient: retry.

    Confirmed live on 2026-08-11 -- montgomery.tx.publicsearch.us answered
    "Error While Running Search: Error with search query" to EVERY query,
    including a bare common surname, while nueces.tx.publicsearch.us answered the
    identical URL shape normally. Not the date range (every range from 1600 to a
    single 2009 year errored alike), not a bot gate, and not this adapter: the
    county's own instance was failing server-side."""


# The portal's own error banner. Matched on the vendor's wording rather than on
# "no table rendered", because a genuine no-results page renders no table either
# -- which is exactly why the two were indistinguishable before.
_PORTAL_ERROR = re.compile(r"Error While Running Search|Error with search query", re.I)
_NO_RESULTS = re.compile(r"No Results Found", re.I)


def _no_table_verdict(page, base_url: str, query: str) -> list[dict]:
    """Decide what a missing results table MEANS, and return [] only for a real
    empty result. Raises RecorderSearchUnavailable when the portal said it failed."""
    try:
        body = page.inner_text("body")
    except Exception:  # page went away; treat as unavailable, not as "no documents"
        raise RecorderSearchUnavailable(
            f"{base_url}: no results table and the page could not be read "
            f"(query {query!r})") from None
    if _PORTAL_ERROR.search(body):
        raise RecorderSearchUnavailable(
            f"{base_url}: the portal reported an error running the search for "
            f"{query!r} -- not an answer that no such document exists")
    if _NO_RESULTS.search(body):
        return []
    # Neither the vendor's "no results" wording nor its error wording. Could be a
    # layout change, could be a slow render. Either way this adapter cannot say
    # the county holds no matching document, so it does not say so.
    raise RecorderSearchUnanswered(
        f"{base_url}: search for {query!r} returned no results table and no "
        f"recognisable message -- the page may have changed")

# Some counties on this vendor (confirmed: Collin) expose no dedicated
# SUBDIVISION/LOT/BLOCK columns in the results table at all -- every row jams
# them into one free-text "LEGAL DESCRIPTION" field instead, e.g.
# "Subdivision- Name: STAR TRAIL #1A PROSPER Lot: 7 Block: F Reference - 2017/721"
# or "Survey- Name: COLLIN COUNTY SCHOOL LAND #12 Survey: 147 Acres: 269.506".
_LEGAL_DESC_NAME_RE = re.compile(
    r"(?:Subdivision|Survey)\s*-\s*Name:\s*(.+?)"
    r"(?=\s*(?:,\s*Reference|\s+Lot:|\s+Block:|\s+Reference\s*-|\s+Survey:|\s+Acres:|$))",
    re.IGNORECASE,
)
_LEGAL_DESC_LOT_RE = re.compile(r"\bLot:\s*(\S+)", re.IGNORECASE)
_LEGAL_DESC_BLOCK_RE = re.compile(r"\bBlock:\s*(\S+)", re.IGNORECASE)


def _enrich_row_from_legal_description(row: dict) -> dict:
    """Derives SUBDIVISION/LOT/BLOCK from a row's own free-text LEGAL
    DESCRIPTION field when the county's own results table has no dedicated
    columns for them at all -- confirmed real and load-bearing, not a
    cosmetic nicety: app/title/chain.py's own _matches_anchor treats a
    MISSING field on either side of a comparison as "can't compare, don't
    reject" (by design, so a genuinely blank field never wrongly rejects a
    real match) -- but for a county with no such columns whatsoever, both
    sides are always missing, so every comparison silently no-ops and
    _matches_anchor accepts EVERY row unfiltered. Confirmed on covid 3028
    (Collin): a chain walk picked up an entirely unrelated "Prosper Town
    Center" deed from the same grantor (American Bank Texas, a bank with
    hundreds of unrelated releases/deeds countywide) as if it were a real
    hop in this covenant's own Star Trail chain, purely because there was no
    SUBDIVISION field on either side to catch the mismatch. A no-op when the
    row already has any of these as native keys (e.g. Montgomery, Bexar) --
    never overrides a column the county's own table actually provides."""
    legal = row.get("LEGAL DESCRIPTION")
    if not legal:
        return row
    enriched = dict(row)
    # PER FIELD, not all-or-nothing. Skipping every derivation when ANY of the three
    # was already present meant Nueces -- whose results table carries LOT and BLOCK
    # columns but NO subdivision column -- never got a SUBDIVISION at all, even
    # though its own LEGAL DESCRIPTION says "Subdivision- Name: PALMILLA BEACH UNIT
    # 1B". 39 real plat rows were then discarded as not matching the subdivision
    # they were rows for. A column the county DOES provide is still never
    # overridden; that is what the per-field check preserves.
    for field, pattern in (("SUBDIVISION", _LEGAL_DESC_NAME_RE),
                           ("LOT", _LEGAL_DESC_LOT_RE),
                           ("BLOCK", _LEGAL_DESC_BLOCK_RE)):
        if row.get(field):
            continue
        m = pattern.search(legal)
        if m:
            enriched[field] = m.group(1).strip().rstrip(",")
    return enriched


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
            # No table. That is EITHER a real empty result or the portal failing,
            # and the difference decides whether a chain walk records "no
            # documents" as a fact -- so it is not guessed here.
            return _no_table_verdict(page, base_url, query)
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

    This vendor's quick-search box always defaults to the Department
    combobox's own per-county default label ("Public Records" for
    Montgomery, "Property Records" for Collin -- confirmed live to differ
    across counties, not a fixed string); the Department control is a
    react-select combobox (no native <select>, so Playwright's own
    select_option can't drive it) -- confirmed by inspecting the live DOM:
    the clickable control is the DIV whose class ends in "-control" (the
    standard react-select-generated suffix), which several other, non-
    clickable DIVs also happen to contain the same label text as (the
    outer container, the value-display span) -- a plain text locator can
    match any of those first and silently no-op, which is what broke a
    Collin search: "text=Public Records" simply doesn't exist on that
    county's page at all. Click the current department label to open it
    (found by its "-control" class suffix, not its label text, so this
    works whatever the current department happens to be), then click the
    "Plats" option by its own text (not by its dynamically-numbered
    react-select-N-option-M id, which is not stable across reloads)."""
    page = context.new_page()
    try:
        page.goto(base_url, wait_until="networkidle")
        control = page.query_selector("div[class$='-control']")
        if control is None:
            raise RuntimeError("Department combobox control not found (page layout may have changed)")
        control.click()
        page.wait_for_timeout(300)
        departments = [o.inner_text().strip()
                       for o in page.query_selector_all("div[id*='option']")]
        if "Plats" not in departments:
            # NOT EVERY COUNTY ON THIS VENDOR HAS A PLATS DEPARTMENT. Confirmed
            # live 2026-08-11: Denton and Collin both offer one; Nueces offers
            # Official Public Records, Marriage, Foreclosures, Miscellaneous,
            # Marks and Brands, Commissioners Court, Public Notice and Campaign
            # Reports -- and no Plats. Its plats are filed in the default
            # department as MAP and PLAT document types instead (the same place
            # covid 5963's 2008 replat was found). Clicking a department that
            # isn't there timed out five times and got recorded as a portal
            # failure, when the real answer was "search somewhere else".
            page.keyboard.press("Escape")
            return _search_plats_in_default_department(page, base_url, subdivision_name)
        page.click("text=Plats", timeout=5000)
        page.wait_for_timeout(300)
        page.fill(f"#{SEARCH_BOX_ID}", subdivision_name)
        page.click('[data-testid="searchSubmitButton"]')
        try:
            page.wait_for_selector("table", timeout=20000)
        except Exception:
            # Same distinction as search_by_name: a missing plat and a broken
            # portal look alike, and only one of them means "no plat exists".
            return _no_table_verdict(page, base_url, subdivision_name)
        page.wait_for_timeout(500)
        return _parse_results_table(page)
    finally:
        page.close()


# Document types that ARE a subdivision plat, for counties with no Plats
# department. Taken from what Nueces' own index actually returns for a
# subdivision name, not from a guess at the vendor's vocabulary.
_PLAT_DOC_TYPES = {"MAP", "PLAT", "REPLAT", "MAP/PLAT", "PLAT/MAP", "AMENDED PLAT"}


def _search_plats_in_default_department(page, base_url: str, subdivision_name: str) -> list[dict]:
    """Plat records from a county with no Plats department: the ordinary search,
    narrowed to plat document types.

    The narrowing is the whole point -- an unfiltered subdivision search returns
    deeds, liens and releases naming the same subdivision, and treating one of
    those as the plat would date the lots from a mortgage. Returned rows keep this
    vendor's own column names, so app/gis/plat_tracking.py consumes them exactly
    as it consumes real Plats-department rows."""
    page.fill(f"#{SEARCH_BOX_ID}", subdivision_name)
    page.click('[data-testid="searchSubmitButton"]')
    try:
        page.wait_for_selector("table", timeout=20000)
    except Exception:
        return _no_table_verdict(page, base_url, subdivision_name)
    page.wait_for_timeout(500)
    return [r for r in _parse_results_table(page)
            if (r.get("DOC TYPE") or "").strip().upper() in _PLAT_DOC_TYPES]


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
            rows.append(_enrich_row_from_legal_description(row))
    return rows

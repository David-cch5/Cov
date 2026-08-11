"""Smoke test: exercise all three county recorder-portal adapters
(app/recorder/adapters/*.py) against known-good live cases and assert they
reproduce what was found by hand this session. These are real third-party
sites' own frontends, not a stable API this project controls -- if a vendor
changes its markup, this is how a future session finds out quickly rather
than rediscovering the break the same slow way these adapters were built.

The shared retry/job_queue-logging mechanism these adapters use (via
app/recorder/diagnose.py) has its own tests in scripts/test_job_queue.py,
since that mechanism is no longer recorder-specific -- app/gis/classifier.py
uses it too.

Usage: python3 scripts/test_recorder_adapters.py
"""
import sys

sys.path.insert(0, ".")

from app.recorder.diagnose import check_corpus_completeness
from app.recorder.session import recorder_context
from app.recorder.adapters import publicsearch
from app.recorder.adapters.publicsearch import (
    RecorderSearchUnanswered,
    RecorderSearchUnavailable,
    _enrich_row_from_legal_description,
    _no_table_verdict,
)


def test_acclaim_ellis() -> None:
    """covid 8386: recorded instrument is 36 images/18 stamped pages; our
    local PDF has 11. This is the exact finding that resolved the covenant.

    Expects 18, not the 36 asserted when this test was written. Ellis's own
    "Number Of Pages" field said 36 then and says 18 now -- it switched from
    counting viewer IMAGES to counting stamped PAGES, and the viewer duplicates
    every physical page across two consecutive image indices (see
    acclaim.get_page_image_bytes). 18 is therefore the number this project had
    already established by hand, verified against the live detail page on
    2026-08-11, and the more meaningful one to assert. The finding it supports is
    unchanged either way: 11 local pages against 18 recorded is a real gap."""
    result = check_corpus_completeness(
        local_pages=11, vendor="acclaim_harris_recording_solutions",
        base_url="https://ellisccktxpublicsearch.us",
        search_name="RED OAK COYOTE RIDGE", instrument_number="1019671",
    )
    assert result["checked"], result
    assert result["recorder_pages"] == 18, result
    assert result["missing_pages"] == 7, result
    assert result["corpus_gap_suspected"] is True, result
    print("PASS: acclaim (Ellis) ->", result)


def test_ava_fidlar_kerr() -> None:
    """covid 7768: recorded instrument is 17 pages; our local PDF has 15."""
    result = check_corpus_completeness(
        local_pages=15, vendor="fidlar_ava", base_url="https://ava.fidlar.com/TXKerr",
        book="1765", page="243",
    )
    assert result["checked"], result
    assert result["recorder_pages"] == 17, result
    assert result["corpus_gap_suspected"] is True, result
    print("PASS: fidlar_ava (Kerr) ->", result)


def test_publicsearch_nueces() -> None:
    """covid 5963: Passco Corpus Christi LLC's chain of title, including the
    2008 replat that turned Lots 1-5 Block 2 into Lot 1A Block 2."""
    with recorder_context() as context:
        rows = publicsearch.search_by_name(context, "https://nueces.tx.publicsearch.us", "PASSCO CORPUS CHRISTI")
    assert len(rows) > 10, f"expected a substantial chain of title, got {len(rows)} rows"
    replat = [r for r in rows if r.get("DOC TYPE") == "MAP" and r.get("DOC NUMBER") == "2008014951"]
    assert replat, "expected to find the 2008 Passco replat (doc 2008014951) in the results"
    print(f"PASS: publicsearch (Nueces) -> {len(rows)} rows, found the 2008 replat")


def test_publicsearch_montgomery() -> None:
    """covid 4780: AVALON HARBOR II, LP's declaration, doc 2009089679 --
    confirms the adapter works unmodified against Montgomery's instance of
    the same GovOS PublicSearch product (the county the cost probe in
    BUILD_SPEC.md Section 7 is built around).

    A county-side outage is reported as a SKIP, not a pass and not a failure. On
    2026-08-11 Montgomery's instance answered "Error While Running Search" to
    every query, including a bare surname, while Nueces answered the identical
    URL shape normally -- so the adapter was fine and the county was not. That
    verdict is only available because the adapter now RAISES on the portal's own
    error banner instead of returning an empty list, which would have arrived here
    as "doc 2009089679 does not exist"."""
    try:
        with recorder_context() as context:
            row = publicsearch.search_by_document_number(
                context, "https://montgomery.tx.publicsearch.us", "2009089679")
    except RecorderSearchUnanswered as e:
        print(f"SKIP: publicsearch (Montgomery) -> the county's own portal is not "
              f"answering searches, so this proves nothing about the adapter: {e}")
        return
    assert row is not None, "expected to find doc 2009089679"
    assert row.get("DOC TYPE") == "DECLARATION", row
    assert row.get("GRANTOR") == "AVALON HARBOR LP", row
    print(f"PASS: publicsearch (Montgomery) -> found doc 2009089679, {row.get('DOC TYPE')}")


def test_results_are_paged_not_capped_at_one_page() -> None:
    """The "~50-row cap" this project reasoned about for months is page 1 of N.
    Nueces reports 2,298 documents for PALMILLA BEACH and serves them 50 at a time
    via &offset=; reading only the first page is how a real plat went unfound while
    this project reported it did not exist.

    Checked without a network call for the URL mechanics, and with one live search
    for the behaviour that matters: more than one page comes back, and every row is
    a distinct document."""
    from app.recorder.adapters.publicsearch import PAGE_SIZE, _with_offset

    assert _with_offset("https://x/results?a=1", 50) == "https://x/results?a=1&offset=50"
    assert _with_offset("https://x/results?a=1&offset=50", 100) == "https://x/results?a=1&offset=100"
    assert _with_offset("https://x/results", 50) == "https://x/results?offset=50"

    with recorder_context() as context:
        rows = publicsearch.search_by_name(
            context, "https://nueces.tx.publicsearch.us", "PALMILLA BEACH",
            full_text_ocr=False, max_rows=120)
    assert len(rows) > PAGE_SIZE, (
        f"a search matching thousands must return more than one page, got {len(rows)}")
    docs = [r.get("DOC NUMBER") for r in rows]
    assert len(set(docs)) == len(docs), "an offset page repeated a document"
    assert len(rows) <= 120, f"max_rows must bound the walk, got {len(rows)}"
    print(f"PASS: paging returns {len(rows)} distinct documents across "
          f"{-(-len(rows) // PAGE_SIZE)} pages, bounded by max_rows")


def test_portal_error_is_not_read_as_an_empty_result() -> None:
    """The distinction itself, without a network call: the same missing results
    table means three different things, and only one of them is "no such
    document". Before this, all three returned [] -- so a portal outage during a
    chain walk would have been recorded as a parcel with no recorded documents."""
    class FakePage:
        def __init__(self, body): self._body = body
        def inner_text(self, _sel): return self._body

    assert _no_table_verdict(FakePage("SEARCH RESULTS\nNo Results Found\nEdit Search"),
                             "https://x", "q") == []

    for body, label in [
        ("No Results Found\nError While Running Search:\nError with search query",
         "the live Montgomery page (it says BOTH -- the error must win)"),
        ("Error with search query", "the bare vendor error"),
    ]:
        try:
            _no_table_verdict(FakePage(body), "https://x", "q")
        except RecorderSearchUnavailable:
            pass
        else:
            raise AssertionError(f"a portal error must not read as empty: {label}")

    # An unrecognised page is also not evidence of absence.
    try:
        _no_table_verdict(FakePage("something else entirely"), "https://x", "q")
    except RecorderSearchUnanswered:
        pass
    else:
        raise AssertionError("an unrecognised page must not read as empty")

    class DeadPage:
        def inner_text(self, _sel): raise RuntimeError("page closed")
    try:
        _no_table_verdict(DeadPage(), "https://x", "q")
    except RecorderSearchUnavailable:
        pass
    else:
        raise AssertionError("an unreadable page must not read as empty")
    print("PASS: only the vendor's own 'No Results Found' reads as an empty result; "
          "an error, an unknown page and a dead page all raise")


def test_enrich_row_from_legal_description() -> None:
    """Confirmed real and load-bearing (covid 3028, Collin): this vendor's
    results table has no dedicated SUBDIVISION/LOT/BLOCK columns for some
    counties at all -- everything is jammed into one free-text LEGAL
    DESCRIPTION field. Without deriving them, app/title/chain.py's own
    _matches_anchor treats a missing field on both sides as "can't compare,
    don't reject" -- so EVERY row silently passed unfiltered, and a chain
    walk accepted an entirely unrelated "Prosper Town Center" deed from the
    same grantor (American Bank Texas, a bank with hundreds of unrelated
    releases countywide) as if it were a real hop in this covenant's own
    Star Trail chain."""
    unrelated = _enrich_row_from_legal_description({
        "GRANTOR": "AMERICAN BANK TEXAS", "GRANTEE": "TEXAS STATE OF",
        "LEGAL DESCRIPTION": "Subdivision- Name: PROSPER TOWN CENTER I L2&3/A Q/504 Lot: 2, Reference - Q / 504",
    })
    assert unrelated["SUBDIVISION"] == "PROSPER TOWN CENTER I L2&3/A Q/504", unrelated
    assert unrelated["LOT"] == "2", unrelated

    declaration = _enrich_row_from_legal_description({
        "GRANTOR": "PROSPER LEGACY LAKES LTD", "GRANTEE": "N/A",
        "LEGAL DESCRIPTION": "Subdivision- Name: COLLIN COUNTY SCHOOL LAND #12, Reference - S / 147",
    })
    assert declaration["SUBDIVISION"] == "COLLIN COUNTY SCHOOL LAND #12", declaration

    plat = _enrich_row_from_legal_description({
        "GRANTOR": "BLUE STAR ALLEN LAND L/P", "GRANTEE": "STAR TRAIL #1A PROSPER",
        "LEGAL DESCRIPTION": "Subdivision - Name: STAR TRAIL #1A PROSPER Lot: 7 Block: F Reference - 2017/721",
    })
    assert plat["SUBDIVISION"] == "STAR TRAIL #1A PROSPER", plat
    assert plat["LOT"] == "7", plat
    assert plat["BLOCK"] == "F", plat

    no_structure = _enrich_row_from_legal_description({"LEGAL DESCRIPTION": "SEE INSTRUMENT"})
    assert "SUBDIVISION" not in no_structure, no_structure

    native_columns = _enrich_row_from_legal_description({"LOT": "5", "SECTION": "01"})
    assert "SUBDIVISION" not in native_columns, native_columns  # never overrides a county that HAS real columns

    print("PASS: _enrich_row_from_legal_description -> SUBDIVISION/LOT/BLOCK derived from free "
          "text only when a county's own table has no such columns at all, confirming the "
          "unrelated-grantor mismatch would now be caught, not silently passed through")


if __name__ == "__main__":
    test_acclaim_ellis()
    test_ava_fidlar_kerr()
    test_publicsearch_nueces()
    test_publicsearch_montgomery()
    test_results_are_paged_not_capped_at_one_page()
    test_portal_error_is_not_read_as_an_empty_result()
    test_enrich_row_from_legal_description()
    print("\nall recorder adapter smoke tests passed")

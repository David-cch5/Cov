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


def test_acclaim_ellis() -> None:
    """covid 8386: recorded instrument is 36 images/18 stamped pages; our
    local PDF has 11. This is the exact finding that resolved the covenant."""
    result = check_corpus_completeness(
        local_pages=11, vendor="acclaim_harris_recording_solutions",
        base_url="https://ellisccktxpublicsearch.us",
        search_name="RED OAK COYOTE RIDGE", instrument_number="1019671",
    )
    assert result["checked"], result
    assert result["recorder_pages"] == 36, result
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
    BUILD_SPEC.md Section 7 is built around)."""
    with recorder_context() as context:
        row = publicsearch.search_by_document_number(context, "https://montgomery.tx.publicsearch.us", "2009089679")
    assert row is not None, "expected to find doc 2009089679"
    assert row.get("DOC TYPE") == "DECLARATION", row
    assert row.get("GRANTOR") == "AVALON HARBOR LP", row
    print(f"PASS: publicsearch (Montgomery) -> found doc 2009089679, {row.get('DOC TYPE')}")


if __name__ == "__main__":
    test_acclaim_ellis()
    test_ava_fidlar_kerr()
    test_publicsearch_nueces()
    test_publicsearch_montgomery()
    print("\nall recorder adapter smoke tests passed")

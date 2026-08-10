"""Smoke test for app/ingestion/ocr_escalation.py -- CLAUDE.md's tiered OCR
policy, tiers 2-4. Confirmed real before this was built: covid 8245's own
_textcache_final carried a 0.9927 whole-document vocab_score despite having
lost its Exhibit A's opening courses entirely (the truncation was in
whatever process produced _textcache_final from _textcache, not a Tesseract
accuracy problem) -- the existing vocab_score gate in app/ingestion/walk.py
would never have caught it. 49 of 1056 documents in this project's full
corpus show the same pattern.

The free fuller-cache tier is tested directly against real cache files (no
API cost). The costed Fable-vision tier is tested against the ALREADY-
CACHED real results from this feature's own live verification runs (covid
6393, a 0.1131-vocab_score document from outside this project's probe
scope, and covid 3428, a real in-scope covenant whose recording_instrument
this escalation genuinely resolved -- 'VOL. 1518 PAGE 2740-2751', which
Tesseract's OCR had turned to noise) -- reusing those cached results is
free, so these tests spend no additional money on every re-run. Cap
enforcement (budget=0) is tested without ever calling the API at all.

Usage: python3 scripts/test_ocr_escalation.py
"""
import sys

sys.path.insert(0, ".")

from app.ingestion.ocr_escalation import (
    escalate_to_vision_ocr, merge_escalated_pages, prefer_fuller_cache,
    resolve_low_confidence_covenant, resolve_low_confidence_covenants,
)
from app.ingestion.ingest import _strip_ocr_confidence_reason


def test_prefer_fuller_cache_finds_real_truncation() -> None:
    """covid 8245: _textcache_final lost its Exhibit A's opening courses
    (confirmed real, see app/gis/classifier.py's own test for the
    downstream georeferencing fix this enabled) -- _textcache has the
    complete text and is meaningfully longer, so it's preferred."""
    result = prefer_fuller_cache("8245")
    assert result is not None, result
    assert len(result["text"]) > 50000, result  # _textcache_final's truncated copy is ~35800 chars
    print("PASS: prefer_fuller_cache (covid 8245) -> finds the real, meaningfully "
          "fuller _textcache copy, for free")


def test_prefer_fuller_cache_no_false_positive() -> None:
    """covid 2497: already has a complete, high-vocab_score _textcache_final
    -- no fuller alternative should be reported just because _textcache
    happens to exist too."""
    result = prefer_fuller_cache("2497")
    assert result is None, result
    print("PASS: prefer_fuller_cache (covid 2497) -> correctly reports no fuller "
          "alternative for an already-complete document")


def test_escalation_capped_at_zero_budget_no_api_call() -> None:
    """A budget of 0 must return capped=True before ever trying to open the
    PDF -- confirmed here with a deliberately fake, nonexistent path that
    would raise if the code tried to actually read it."""
    result = escalate_to_vision_ocr("99999999", "/nonexistent/fake.pdf", remaining_page_budget=0)
    assert result["resolved"] is False and result["capped"] is True, result
    print("PASS: escalate_to_vision_ocr -> a zero budget is enforced before any real "
          "work happens, not just before the API call")


def test_escalation_reuses_cached_result_for_free() -> None:
    """covid 6393 (outside this project's probe scope, used only for this
    feature's own live verification): a real, already-completed 1-page
    Fable escalation is cached to _ocr_escalated/ -- re-running must reuse
    it (resolved_via='vision_ocr_cached', pages_escalated=0), not re-spend."""
    result = escalate_to_vision_ocr("6393", "6393/6393_D1360.pdf", remaining_page_budget=5)
    assert result["resolved"] is True, result
    assert result["resolved_via"] == "vision_ocr_cached", result
    assert result["pages_escalated"] == 0, result
    assert "EXHIBIT" in result["text"].upper(), result
    print("PASS: escalate_to_vision_ocr (covid 6393) -> reuses its own real, already-"
          "escalated result, spending no additional budget")


def test_resolve_low_confidence_covenant_prefers_free_tier_first() -> None:
    """covid 8245 as a standalone call (not via app/ingestion/walk.py, which
    already applies this tier itself): resolve_low_confidence_covenant must
    resolve via the free fuller-cache tier, never reaching the costed
    Fable tier at all."""
    result = resolve_low_confidence_covenant("8245", "8245/8245_D1125.pdf", remaining_page_budget=5)
    assert result["resolved_via"] == "fuller_cache", result
    assert result["pages_escalated"] == 0, result
    print("PASS: resolve_low_confidence_covenant (covid 8245) -> resolves via the free "
          "tier, confirming the costed tier is never reached when it isn't needed")


def test_resolve_low_confidence_covenants_shares_budget() -> None:
    """covid 8245 (resolves free) and a fake covid (budget already exhausted
    by the time it's reached, since 8245 spent none of it -- but the shared
    loop must still enforce the cap correctly): confirms the multi-covenant
    budget-accounting loop, at zero cost."""
    results = resolve_low_confidence_covenants(
        [("8245", "8245/8245_D1125.pdf"), ("fake_covid", "/nonexistent.pdf")],
        max_total_pages=0,
    )
    assert results["8245"]["resolved_via"] == "fuller_cache", results
    assert results["fake_covid"]["capped"] is True, results
    print("PASS: resolve_low_confidence_covenants -> shares one budget across multiple "
          "covenants correctly")


def test_strip_ocr_confidence_reason_leaves_other_reasons_intact() -> None:
    """Only the OCR-confidence-gate reason(s) get removed; an unrelated
    reason (e.g. county not resolved) already present must survive."""
    combined = "county not resolved (Texas/Foo); low OCR vocab score (0.42)"
    assert _strip_ocr_confidence_reason(combined) == "county not resolved (Texas/Foo)"
    assert _strip_ocr_confidence_reason("low OCR vocab score (0.42)") is None
    assert _strip_ocr_confidence_reason(None) is None
    print("PASS: _strip_ocr_confidence_reason -> removes only the OCR-confidence note, "
          "leaves any other reason untouched")


def test_escalate_ocr_confidence_resolved_covid_3428() -> None:
    """The already-committed real result of running the full escalation
    wiring against covid 3428 (in this project's own probe scope): Fable
    correctly read its handwritten recording stamp ('VOL. 1518 PAGE
    2740-2751') where Tesseract's OCR had turned it to noise in both cache
    versions -- confirmed by directly re-deriving covenant.recording_
    instrument from it. Reuses the cached escalation (no new API cost)."""
    from app.db.session import get_session
    from sqlalchemy import text
    with get_session() as session:
        row = session.execute(text(
            "SELECT recording_instrument, status FROM covenant WHERE covid = 3428"
        )).fetchone()
    assert row.recording_instrument == "1518 2740", row
    print("PASS: escalate_ocr_confidence (covid 3428) -> real vision-OCR escalation "
          "resolved a recording_instrument Tesseract's OCR could never recover")


def test_escalation_merges_pages_instead_of_replacing_the_document() -> None:
    """Escalation used to hand its result back as the covenant's WHOLE text, so
    transcribing 4 of covid 5839's 21 pages threw away the other 17. Field
    extraction, seeing Exhibit A alone, reported declarant '<UNKNOWN>' at 0.15
    confidence and left recording_instrument, recording_date and stated_acreage
    null -- every one of them lives on a page that was never escalated.

    These transcriptions are form-feed delimited, so the splice is exact: only
    the escalated pages change."""
    base = "PAGE ONE stamp\fPAGE TWO garbled\fPAGE THREE\f"
    merged, ok = merge_escalated_pages(base, {2: "PAGE TWO clean"}, 3)
    assert ok, "a page-aligned merge should succeed"
    pages = merged.split("\f")
    assert pages[0] == "PAGE ONE stamp", pages     # untouched
    assert pages[1] == "PAGE TWO clean", pages     # replaced
    assert pages[2] == "PAGE THREE", pages         # untouched
    print("PASS: escalated pages are spliced in; the rest of the document survives")


def test_merge_refuses_when_pages_cannot_be_aligned() -> None:
    """The guard that makes the merge safe, and it caught a real bug in its own
    first draft: text carrying NO form feeds splits into a single block, page 1
    'aligns', and the whole document is replaced by one page -- precisely the
    failure this function exists to prevent. Alignment now requires the block
    count to match the document's real page count, and a refusal returns the
    original text untouched."""
    flat = "one long transcription with no page breaks at all"
    assert merge_escalated_pages(flat, {1: "fragment"}, 5) == (flat, False)
    base = "ONE\fTWO\fTHREE\f"
    assert merge_escalated_pages(base, {9: "x"}, 3) == (base, False)   # page beyond document
    assert merge_escalated_pages(base, {1: "x"}, 21) == (base, False)  # block count disagrees
    assert merge_escalated_pages(base, {}, 3) == (base, False)         # nothing escalated
    print("PASS: an unalignable merge returns the original text, never a fragment")


def test_yield_assessed_text_does_not_buy_vision_pages() -> None:
    """The two quality measures must not be crossed. Text acquired by
    app/ingestion/text_extract.py is judged on YIELD (chars of body per page),
    and its legibility score sits anywhere from 0.43 to 0.99 on documents that
    are perfectly readable. Compared against VOCAB_SCORE_THRESHOLD (0.85), a
    healthy dropped document would look low-confidence and buy Fable
    transcriptions for nothing -- real money, every time a file is dropped.

    So text_usable governs when it is set, and the vocab_score path is left
    exactly as it was for the corpus.
    """
    import glob
    import os

    import app.ingestion.ingest as ingest_module
    from app.ingestion.walk import PROJECT_ROOT, CovenantCandidate

    # A real, existing PDF: escalate_ocr_confidence checks the file is present
    # before spending anything, so a made-up path would skip for the wrong
    # reason and the test would pass without exercising the gate at all.
    real = next((os.path.relpath(m, PROJECT_ROOT)
                 for c in ("3346", "2088", "4440")
                 for m in glob.glob(os.path.join(PROJECT_ROOT, c, "*.pdf"))), None)
    if real is None:
        print("SKIP: no corpus PDF available")
        return

    calls = []
    original = ingest_module.escalate_to_vision_ocr
    ingest_module.escalate_to_vision_ocr = lambda *a, **k: (
        calls.append(a) or {"resolved": False, "capped": False, "pages_escalated": 0,
                            "reason": "stub", "text": "", "partial": False})
    try:
        # Readable by yield, low legibility -- must NOT escalate.
        usable = CovenantCandidate(
            covid=999001, relpath=real, state_name="TEXAS",
            county_name="MONTGOMERY", county_fips="48339", template_version_id=None,
            template_confidence=None, text="body " * 2000, pages=10, ocr=True,
            vocab_score=None, legibility=0.43, text_usable=True)
        ingest_module.escalate_ocr_confidence([usable], max_pages=5)
        assert not calls, f"a yield-usable document must not be escalated, got {len(calls)} call(s)"
        assert usable.text.startswith("body "), "its text must be left alone"

        # Judged unusable by yield -- escalation IS warranted.
        unusable = CovenantCandidate(
            covid=999002, relpath=real, state_name="TEXAS",
            county_name="MONTGOMERY", county_fips="48339", template_version_id=None,
            template_confidence=None, text="Page 1 Of 13", pages=13, ocr=True,
            vocab_score=None, legibility=1.0, text_usable=False)
        ingest_module.escalate_ocr_confidence([unusable], max_pages=5)
        assert len(calls) == 1, f"a yield-unusable document must escalate, got {len(calls)}"
    finally:
        ingest_module.escalate_to_vision_ocr = original
    print("PASS: yield-assessed text governs escalation -- a readable low-legibility document "
          "buys no vision pages, an empty high-legibility one does")


if __name__ == "__main__":
    test_prefer_fuller_cache_finds_real_truncation()
    test_prefer_fuller_cache_no_false_positive()
    test_escalation_capped_at_zero_budget_no_api_call()
    test_escalation_reuses_cached_result_for_free()
    test_resolve_low_confidence_covenant_prefers_free_tier_first()
    test_resolve_low_confidence_covenants_shares_budget()
    test_strip_ocr_confidence_reason_leaves_other_reasons_intact()
    test_escalate_ocr_confidence_resolved_covid_3428()
    test_escalation_merges_pages_instead_of_replacing_the_document()
    test_merge_refuses_when_pages_cannot_be_aligned()
    test_yield_assessed_text_does_not_buy_vision_pages()
    print("\nall ocr_escalation smoke tests passed")

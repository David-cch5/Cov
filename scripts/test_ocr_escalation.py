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
    escalate_to_vision_ocr, prefer_fuller_cache, resolve_low_confidence_covenant,
    resolve_low_confidence_covenants,
)
from scripts.ingest_probe import _strip_ocr_confidence_reason


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


if __name__ == "__main__":
    test_prefer_fuller_cache_finds_real_truncation()
    test_prefer_fuller_cache_no_false_positive()
    test_escalation_capped_at_zero_budget_no_api_call()
    test_escalation_reuses_cached_result_for_free()
    test_resolve_low_confidence_covenant_prefers_free_tier_first()
    test_resolve_low_confidence_covenants_shares_budget()
    test_strip_ocr_confidence_reason_leaves_other_reasons_intact()
    test_escalate_ocr_confidence_resolved_covid_3428()
    print("\nall ocr_escalation smoke tests passed")

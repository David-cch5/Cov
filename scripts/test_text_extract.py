"""Tests for app/ingestion/text_extract.py -- the free text-acquisition tier.

Every case here is a real corpus document, named by covid, because the whole
design rests on measurements taken from them: which gate blocks, where the
thresholds sit, and which failures are invisible to a vocabulary score. A
synthetic fixture would let the thresholds drift without anything noticing.

Usage: python3 scripts/test_text_extract.py
"""
import glob
import json
import os
import sys

sys.path.insert(0, ".")

from app.ingestion.text_extract import (
    MAX_RENDER_PIXELS, MIN_CHARS_PER_PAGE, PAGE_DELIMITER, acquire_text, assess,
    legibility, looks_like_vendor_overlay, ocr_with_tesseract, page_sizes_pts,
    pdf_page_count, safe_dpi, vocabulary, worst_pages,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _cached(covid: str) -> dict:
    files = sorted(glob.glob(os.path.join(PROJECT_ROOT, "_textcache_final", f"{covid}_*.json")))
    assert files, f"no cached text for covid {covid}"
    with open(files[0], encoding="utf-8") as f:
        return json.load(f)


def test_vocabulary_loads() -> None:
    words = vocabulary()
    assert len(words) > 200_000, f"expected the system wordlist plus deed terms, got {len(words)}"
    # From the generated corpus supplement, and verified absent from
    # /usr/share/dict/words -- so these prove the supplement is actually
    # loaded, not just that the system wordlist is. ("grantees" and "grantors"
    # are in neither: they appear in under 5% of documents, below the
    # supplement's document-frequency floor.)
    for term in ("acres", "assigns"):
        assert term in words, f"deed vocabulary missing {term!r}"
    print(f"PASS: vocabulary loads ({len(words)} words incl. corpus deed terms)")


def test_stamp_only_document_is_rejected() -> None:
    """covid 4956: the case the yield gate exists for. 13 pages, 1,238 chars,
    every line an imaging-vendor stamp, and a corpus vocab_score of 1.0000
    that sails through the >= 0.85 confidence gate."""
    d = _cached("4956")
    assert d["vocab_score"] >= 0.85, "fixture assumption: 4956 passes the old confidence gate"
    a = assess(d["text"], d["pages"])
    assert not a["usable"], f"4956 must be rejected, got {a}"
    assert a["content_chars"] == 0, f"expected zero document body, got {a['content_chars']}"
    assert looks_like_vendor_overlay(d["text"]), "4956 is pure vendor overlay"
    assert any("stamp" in r or "chars/page" in r for r in a["reasons"]), a["reasons"]
    print(f"PASS: covid 4956 rejected despite vocab_score {d['vocab_score']} "
          f"({a['chars_per_page']} chars/page, {a['content_chars']} body chars)")


def test_viewer_chrome_document_is_rejected() -> None:
    """covid 8299: not the deed at all -- 12 pages of a saved GSCCCA.org image
    viewer page, URLs and timestamps. A different failure from 4956's stamps,
    and equally invisible to a per-page character count alone."""
    d = _cached("8299")
    a = assess(d["text"], d["pages"])
    assert not a["usable"], f"8299 must be rejected, got {a}"
    print(f"PASS: covid 8299 (saved viewer page) rejected "
          f"({a['chars_per_page']} chars/page of body)")


def test_readable_but_messy_document_is_accepted() -> None:
    """The other half of the design, and the reason legibility does not block:
    covid 3925 scores ~0.43 purely because OCR lost word spacing, and covid
    8224 ~0.69 for the same reason. Both are perfectly usable documents. A
    legibility gate set anywhere near "looks clean" would throw them away."""
    for covid in ("3925", "8224"):
        d = _cached(covid)
        a = assess(d["text"], d["pages"])
        assert a["usable"], f"covid {covid} must be accepted, got {a}"
        assert a["legibility"] < 0.75, (
            f"fixture assumption: covid {covid} is meant to be a LOW-legibility "
            f"readable document, got {a['legibility']}")
        print(f"PASS: covid {covid} accepted on yield ({a['chars_per_page']} chars/page) "
              f"despite legibility {a['legibility']}")


def test_clean_documents_are_accepted() -> None:
    for covid in ("2088", "4440", "5838"):
        d = _cached(covid)
        a = assess(d["text"], d["pages"])
        assert a["usable"], f"covid {covid} should be usable, got {a}"
        assert a["legibility"] > 0.9, a
    print("PASS: clean documents (2088, 4440, 5838) accepted")


def test_gate_separates_the_whole_corpus_as_measured() -> None:
    """The calibration itself, asserted: exactly the six documents measured as
    having no usable body text must fail, and nothing else. If a threshold is
    ever nudged, this fails and names what moved."""
    expected_bad = {"4956", "5991", "5993", "6117", "8299", "4497"}
    rejected, accepted = set(), 0
    for path in sorted(glob.glob(os.path.join(PROJECT_ROOT, "_textcache_final", "*.json"))):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        if not d.get("pages"):
            continue
        if assess(d.get("text") or "", d["pages"])["usable"]:
            accepted += 1
        else:
            rejected.add(str(d["covid"]))
    # 4497 has TWO cache files; only the thin one fails, so it may appear
    # either way depending on which files exist -- assert containment both ways
    # on the five unambiguous ones.
    unambiguous = expected_bad - {"4497"}
    assert unambiguous <= rejected, f"expected these to fail: {unambiguous - rejected}"
    assert rejected <= expected_bad, f"unexpected rejections: {rejected - expected_bad}"
    print(f"PASS: gate rejects exactly the measured-bad set ({sorted(rejected)}), "
          f"accepts {accepted} others")


def test_empty_and_degenerate_input() -> None:
    assert legibility("") == 0.0
    assert legibility("12345 !!! ///") == 0.0
    a = assess("", 0)
    assert not a["usable"] and "page count unknown" in " ".join(a["reasons"])
    # Text with no pages known still gets a legibility reading rather than
    # crashing. 0.8, not 1.0, on clean English: /usr/share/dict/words holds no
    # inflected forms, so "jumps" scores as a non-word -- one of the reasons
    # legibility is a tripwire rather than a quality gate.
    assert assess("the quick brown fox jumps", 0)["legibility"] == 0.8
    assert not looks_like_vendor_overlay(""), "empty text is not an overlay claim"
    print("PASS: empty and degenerate input handled without crashing")


def test_worst_pages_targets_the_thinnest() -> None:
    page_texts = {1: "x" * 4000, 2: "", 3: "y" * 4000, 4: "Page 3 Of 12", 5: "z" * 10}
    assert worst_pages(page_texts, limit=3) == [2, 4, 5], worst_pages(page_texts, limit=3)
    print("PASS: worst_pages targets the thinnest pages, counting stamps as empty")


def test_oversized_pages_render_at_a_fitted_dpi() -> None:
    """covid 2088's pages are 2550 x 3224 pts -- a 35 x 45 inch sheet. At the
    requested 300 DPI that is 142.7M pixels, over PIL's own decompression-bomb
    threshold and more than a gigabyte of bitmap. The DPI must be fitted from
    the page box BEFORE rendering, not downscaled after."""
    d = _cached("2088")
    pdf = os.path.join(PROJECT_ROOT, d["relpath"])
    if not os.path.exists(pdf):
        print(f"SKIP: {d['relpath']} not present")
        return
    sizes = page_sizes_pts(pdf)
    assert len(sizes) == d["pages"], f"expected {d['pages']} page sizes, got {len(sizes)}"
    w, h = sizes[1]
    at_300 = (w / 72.0 * 300) * (h / 72.0 * 300)
    assert at_300 > MAX_RENDER_PIXELS, f"fixture assumption: page 1 is oversized, got {at_300:.0f}px"
    fitted = safe_dpi(w, h)
    assert fitted < 300, f"oversized page must be fitted below 300 DPI, got {fitted}"
    assert (w / 72.0 * fitted) * (h / 72.0 * fitted) <= MAX_RENDER_PIXELS
    # A normal letter-size page must NOT be downgraded.
    assert safe_dpi(612, 792) == 300, safe_dpi(612, 792)
    print(f"PASS: oversized {w:.0f}x{h:.0f}pt page fitted to {fitted} DPI "
          f"({at_300/1e6:.0f}M px -> {(w/72*fitted)*(h/72*fitted)/1e6:.0f}M px); letter stays 300")


def test_live_tesseract_reads_a_real_page() -> None:
    """The free tier end to end on a real scan -- the claim that Tesseract can
    read these documents at all. One page only, so this stays fast."""
    d = _cached("2088")
    pdf = os.path.join(PROJECT_ROOT, d["relpath"])
    if not os.path.exists(pdf):
        print(f"SKIP: {d['relpath']} not present")
        return
    assert pdf_page_count(pdf) == d["pages"], "pdfinfo page count must match the cache"
    text, page_texts = ocr_with_tesseract(pdf, max_pages=1)
    assert len(page_texts) == 1, page_texts.keys()
    a = assess(text, 1)
    assert a["content_chars"] > 500, f"Tesseract read too little: {a}"
    assert a["legibility"] > 0.75, f"Tesseract output not legible: {a}"
    print(f"PASS: live Tesseract read page 1 of covid 2088 "
          f"({a['content_chars']} chars, legibility {a['legibility']})")


def test_acquired_text_uses_the_corpus_page_delimiter() -> None:
    """ocr_escalation.merge_escalated_pages locates a page for free by
    splitting on form feeds. Text produced here has to split the same way or
    that page-targeting quietly stops working."""
    d = _cached("2088")
    pdf = os.path.join(PROJECT_ROOT, d["relpath"])
    if not os.path.exists(pdf):
        print(f"SKIP: {d['relpath']} not present")
        return
    text, _ = ocr_with_tesseract(pdf, max_pages=3)
    assert text.count(PAGE_DELIMITER) == 2, (
        f"3 pages must join with 2 form feeds, found {text.count(PAGE_DELIMITER)}")
    print("PASS: acquired text is form-feed delimited, matching the corpus convention")


def test_acquire_text_falls_through_the_layer_to_ocr() -> None:
    """Measured on this corpus: the embedded text layer yields nothing, so
    acquire_text must report that it tried and fell through -- not silently
    return empty text as though the document had been read."""
    d = _cached("2088")
    pdf = os.path.join(PROJECT_ROOT, d["relpath"])
    if not os.path.exists(pdf):
        print(f"SKIP: {d['relpath']} not present")
        return
    r = acquire_text(pdf, max_pages=2)
    methods = [a["method"] for a in r["attempts"]]
    assert methods == ["pdf_text_layer", "tesseract"], methods
    assert not r["attempts"][0]["usable"], "corpus scans have no usable text layer"
    assert r["method"] == "tesseract"
    print(f"PASS: acquire_text tried the text layer, fell through to Tesseract "
          f"(layer gave {r['attempts'][0]['content_chars']} body chars)")


if __name__ == "__main__":
    test_vocabulary_loads()
    test_stamp_only_document_is_rejected()
    test_viewer_chrome_document_is_rejected()
    test_readable_but_messy_document_is_accepted()
    test_clean_documents_are_accepted()
    test_gate_separates_the_whole_corpus_as_measured()
    test_empty_and_degenerate_input()
    test_worst_pages_targets_the_thinnest()
    test_oversized_pages_render_at_a_fitted_dpi()
    test_live_tesseract_reads_a_real_page()
    test_acquired_text_uses_the_corpus_page_delimiter()
    test_acquire_text_falls_through_the_layer_to_ocr()
    print("\nall text-acquisition tests passed")

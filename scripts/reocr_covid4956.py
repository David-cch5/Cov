"""Re-OCR covid 4956, whose corpus text is 100% imaging-vendor page-stamps.

_textcache_final/4956_4956_D1380.pdf.json holds 1,238 characters across 13 pages
and every single line is the vendor's overlay stamp:

    *ACS/TRC* DALLAS  Doc: 000287011   Date: 10/08/2009  Vol: 0000000
    Page: 00000  Page: 1 Of 13

Zero characters of the actual Dallas County declaration. `ocr: false`, so no OCR
was ever attempted -- whatever built that cache took the PDF's text layer, which
on these scans is the vendor overlay rather than the document. And it scores
vocab_score 1.0000, because DALLAS, Doc, Date, Vol and Page are all dictionary
words, so it PASSES the >= 0.85 confidence gate. It is the only one of 1,056
cached covenants waved through with nothing readable in it.

WRITES TO _intake_text/, NOT _textcache_final. CLAUDE.md: read source files in
place, do not modify the covenant data folders. app/ingestion/walk.py's
get_deed_text searches both caches and prefers whichever has the better yield, so
the re-OCR wins on merit without the original being touched or lost -- and the
bad original stays on disk as evidence of what happened.

Usage: python3 scripts/reocr_covid4956.py [--covid 4956]
"""
import argparse
import glob
import json
import os
import sys
import time

sys.path.insert(0, ".")

from app.ingestion.intake import INTAKE_TEXT_DIR
from app.ingestion.text_extract import (
    PAGE_DELIMITER, acquire_text, assess, looks_like_vendor_overlay,
)
from app.ingestion.walk import PROJECT_ROOT, TEXTCACHE


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--covid", default="4956")
    args = parser.parse_args(argv)
    covid = args.covid

    existing_paths = sorted(glob.glob(os.path.join(TEXTCACHE, f"{covid}_*.json")))
    if not existing_paths:
        print(f"no corpus cache for covid {covid}")
        return 1
    with open(existing_paths[0], encoding="utf-8") as f:
        old = json.load(f)

    old_assessment = assess(old.get("text") or "", old.get("pages") or 0)
    print(f"covid {covid}: {old['relpath']}")
    print(f"  BEFORE  {len(old.get('text') or '')} chars over {old.get('pages')} pages "
          f"= {old_assessment['chars_per_page']}/pg, {old_assessment['content_chars']} of it "
          f"document body")
    print(f"          corpus vocab_score {old.get('vocab_score')}, ocr={old.get('ocr')}, "
          f"vendor-overlay-only={looks_like_vendor_overlay(old.get('text') or '')}")

    pdf = os.path.join(PROJECT_ROOT, old["relpath"])
    if not os.path.exists(pdf):
        print(f"  PDF missing: {pdf}")
        return 1

    print(f"  re-OCR ({old.get('pages')} pages) ...")
    started = time.time()
    acquired = acquire_text(pdf, progress=True)
    elapsed = time.time() - started
    new_assessment = acquired["assessment"]

    print(f"  AFTER   {len(acquired['text'])} chars over {acquired['pages']} pages "
          f"= {new_assessment['chars_per_page']}/pg, {new_assessment['content_chars']} of it "
          f"document body")
    print(f"          method={acquired['method']}, legibility={new_assessment['legibility']}, "
          f"usable={new_assessment['usable']} in {elapsed:.0f}s")
    if not new_assessment["usable"]:
        print(f"  STILL UNUSABLE: {'; '.join(new_assessment['reasons'])}")
        print("  -> this is the human-review case, not something to force through")
        return 2

    record = {
        "relpath": old["relpath"],
        "filename": os.path.basename(old["relpath"]),
        "covid": str(covid),
        "pages": acquired["pages"],
        "text": acquired["text"],
        "ocr": acquired["method"] == "tesseract",
        "method": acquired["method"],
        "legibility": new_assessment["legibility"],
        "chars_per_page": new_assessment["chars_per_page"],
        "usable": True,
        "assessment_reasons": [],
        "needs_escalation": False,
        "page_texts": {str(k): v for k, v in (acquired["page_texts"] or {}).items()},
        "reocr_of": os.path.relpath(existing_paths[0], PROJECT_ROOT),
        "reocr_reason": ("corpus cache was 100% imaging-vendor page-stamps: "
                         f"{old_assessment['content_chars']} document-body chars over "
                         f"{old.get('pages')} pages, at corpus vocab_score "
                         f"{old.get('vocab_score')}"),
    }
    os.makedirs(INTAKE_TEXT_DIR, exist_ok=True)
    out = os.path.join(INTAKE_TEXT_DIR, f"{covid}_{os.path.basename(old['relpath'])}.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(record, f)
    print(f"  wrote {os.path.relpath(out, PROJECT_ROOT)} "
          f"({acquired['text'].count(PAGE_DELIMITER) + 1} form-feed-delimited pages)")
    print(f"  _textcache_final left untouched: {os.path.relpath(existing_paths[0], PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

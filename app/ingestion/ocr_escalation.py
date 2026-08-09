"""Vision-OCR escalation orchestration (CLAUDE.md's tiered OCR policy, tiers
2-4): when a covenant's cached OCR text is missing or low-confidence, first
try a free, deterministic fallback (a fuller, less-processed cache already
on disk) and only escalate to Claude vision OCR (app/ocr/vision_ocr.py) as a
last resort -- capped at a small, explicit page budget per invocation so it
can run without needing separate approval each time. Anything beyond the cap
comes back capped=True rather than silently continuing to spend.

Confirmed real, not hypothetical: _textcache_final lost more than 10% of the
original _textcache's OCR text for 49 of 1056 documents in this project's
full corpus (some over half) -- a data-loss bug in whatever process
produced _textcache_final, not a Tesseract-accuracy problem. covid 8245's
own missing Exhibit A (its opening courses, including the POB) was one of
these -- resolved for free just by preferring _textcache's own fuller copy,
no vision OCR needed at all. Only a document that's STILL low-confidence
after that free fallback (e.g. covid 3428, whose recording stamp is
genuinely garbled in both cache versions) needs the real, costed escalation.

Escalated results are cached to _ocr_escalated/ -- a new directory this
project's own code owns, distinct from the read-only _textcache*/_pilot/
Covenant_Matrix/<covid>/ data locations CLAUDE.md lists -- so a later re-run
never pays for the same page twice.
"""
import json
import os
import tempfile

from pdf2image import convert_from_path

# Claude's own hard limit is 8000px per side; confirmed real (covid 3428's own recording-
# stamp page) that pdf2image's default 200 DPI render can exceed this for an oversized
# plat/exhibit page -- rejected outright by the API, not silently downscaled server-side.
MAX_IMAGE_DIMENSION = 8000

from app.config import LLM_MODEL_HARDEST
from app.ocr.vision_ocr import ocr_page_image

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEXTCACHE_FINAL = os.path.join(PROJECT_ROOT, "_textcache_final")
FALLBACK_CACHES = [os.path.join(PROJECT_ROOT, "_textcache"), os.path.join(PROJECT_ROOT, "_textcache_v2")]
ESCALATED_CACHE = os.path.join(PROJECT_ROOT, "_ocr_escalated")

# How many Fable page-transcriptions a single call can run WITHOUT separate approval --
# small and explicit on purpose (real $ per page, Fable being the most expensive tier).
# A caller that wants more must pass a larger budget deliberately, not by raising this.
MAX_PAGES_WITHOUT_APPROVAL = 5
MAX_PAGES_PER_DOCUMENT = 3  # even a generous remaining budget doesn't mean escalate a whole document

VOCAB_SCORE_THRESHOLD = 0.85  # matches app/ingestion/walk.py's own existing gate


def _find_cache_file(cache_dir: str, covid: str) -> dict | None:
    if not os.path.isdir(cache_dir):
        return None
    matches = [f for f in os.listdir(cache_dir) if f.startswith(f"{covid}_")]
    if not matches:
        return None
    with open(os.path.join(cache_dir, matches[0]), encoding="utf-8") as f:
        return json.load(f)


def prefer_fuller_cache(covid: str) -> dict | None:
    """Free, deterministic fallback: if _textcache or _textcache_v2 has
    meaningfully more text than _textcache_final for this covid, use it
    instead -- no OCR re-run, no API call, just picking the more complete
    of two already-existing extractions. Returns None if no fuller
    alternative exists (nothing to fall back to, or _final is already the
    fullest)."""
    final = _find_cache_file(TEXTCACHE_FINAL, covid)
    final_len = len((final or {}).get("text") or "")

    best, best_len = None, final_len
    for cache_dir in FALLBACK_CACHES:
        candidate = _find_cache_file(cache_dir, covid)
        candidate_len = len((candidate or {}).get("text") or "")
        if candidate_len > best_len * 1.1:  # meaningfully more, not just noise-level bigger
            best, best_len = candidate, candidate_len
    return best


def _escalated_cache_path(covid: str) -> str:
    os.makedirs(ESCALATED_CACHE, exist_ok=True)
    return os.path.join(ESCALATED_CACHE, f"{covid}_vision_escalated.json")



def merge_escalated_pages(base_text: str, page_texts: dict,
                          total_pages: int | None = None) -> tuple[str, bool]:
    """Splice vision-OCR'd pages INTO the existing transcription, replacing only
    those pages and leaving every other page of the document intact.

    Confirmed real, and the reason this exists: escalation used to hand its
    result back as the covenant's whole text. Escalating 4 of covid 5839's 21
    pages therefore threw away the other 17, and field extraction -- looking at
    Exhibit A alone -- reported declarant '<UNKNOWN>' at 0.15 confidence, then
    on a retry left recording_instrument, recording_date and stated_acreage null
    and mistyped a metes-and-bounds instrument as a subdivision plat. Every one
    of those fields lives on a page that was never escalated and never should
    have been discarded.

    These transcriptions are form-feed delimited by page, so the splice is
    exact. Returns (text, merged): merged=False means the pages could NOT be
    aligned -- a document whose cached text carries no page breaks, or fewer
    pages than were escalated -- and in that case base_text comes back
    untouched, because replacing a whole document with a fragment is the bug
    this function exists to prevent.
    """
    if not base_text or not page_texts:
        return base_text, False
    blocks = base_text.split("\f")
    trailing = bool(blocks) and not blocks[-1].strip()
    if trailing:
        blocks = blocks[:-1]
    # The block count must MATCH the document's real page count. Without this,
    # text carrying no form feeds at all splits into a single block, page 1
    # "aligns", and the whole document is replaced by one page -- the very bug
    # this function exists to prevent, found by its own test.
    if not blocks or max(page_texts) > len(blocks):
        return base_text, False
    if total_pages is not None and len(blocks) != total_pages:
        return base_text, False
    for page_no, text in page_texts.items():
        blocks[page_no - 1] = text
    return "\f".join(blocks) + ("\f" if trailing else ""), True


def escalate_to_vision_ocr(
    covid: str, pdf_path: str, remaining_page_budget: int, model: str = LLM_MODEL_HARDEST,
    pages: list[int] | None = None,
) -> dict:
    """The costed tier ONLY -- does not retry prefer_fuller_cache (a caller
    that already tried it, e.g. app/ingestion/walk.py's own iter_candidates,
    would otherwise get the same free-tier answer back forever and never
    actually reach Fable). Checks for an already-escalated cached result
    first (spends no budget), then vision-OCRs the LAST few pages of the PDF
    (Exhibit A and the recording certification are conventionally near the
    end of these instruments -- a documented heuristic based on the two real
    cases confirmed so far, not a guarantee of finding the actual bad page)
    up to whatever's left of remaining_page_budget, capped at
    MAX_PAGES_PER_DOCUMENT regardless of how much budget remains. Returns
    resolved=False, capped=True if the budget can't cover even one page --
    explicitly left for a human to approve a larger budget, never silently
    skipped.

    `pages` (1-indexed) overrides the last-N heuristic when the caller already
    KNOWS which pages matter, which is strictly better than guessing: the cached
    text of these documents is form-feed delimited, so the page carrying a given
    tract can be located for free before spending anything. Confirmed useful on
    covid 5839, whose 43.354 acre tract runs pages 14-17 of 21 -- the last-N
    heuristic would have read pages 19-21 and missed it entirely.

    An explicit page list also bypasses MAX_PAGES_PER_DOCUMENT, because that cap
    exists to stop an unbounded guess from walking a whole document; naming the
    pages IS the deliberate choice it asks for. remaining_page_budget still
    binds, so the spend stays capped by the caller either way."""
    cached_path = _escalated_cache_path(covid)
    if os.path.exists(cached_path):
        with open(cached_path, encoding="utf-8") as f:
            cached = json.load(f)
        # Confirmed real, and caught the hard way: a 4-page targeted escalation of
        # covid 5839 was served back to ingestion as that covenant's whole text,
        # so field extraction saw only Exhibit A and returned declarant
        # '<UNKNOWN>' at 0.15 confidence. A partial transcription is only
        # reusable by a caller asking for those same pages; anyone else must not
        # receive it, and gets a fresh full-document escalation instead.
        if cached.get("partial") and set(pages or []) != set(cached.get("page_numbers") or []):
            cached = None
        if cached is not None:
            return {"covid": covid, "resolved": True, "resolved_via": "vision_ocr_cached",
                    "text": cached["text"], "pages_escalated": 0, "capped": False,
                    "min_confidence": cached.get("min_confidence"),
                    "page_numbers": cached.get("page_numbers"),
                    "page_texts": {int(k): v for k, v in (cached.get("page_texts") or {}).items()},
                    "total_pages": cached.get("total_pages"),
                    "partial": bool(cached.get("partial")),
                    # 0 tokens, not the original run's usage -- no API request was made.
                    "usage": {"input_tokens": 0, "output_tokens": 0,
                              "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}}

    if remaining_page_budget <= 0:
        return {"covid": covid, "resolved": False, "resolved_via": None,
                "pages_escalated": 0, "capped": True,
                "reason": "no Fable page budget remains for this run -- needs a larger "
                          "budget approved explicitly, not escalated automatically"}

    images = convert_from_path(pdf_path)
    total_pages = len(images)
    if pages:
        wanted = [n for n in sorted(set(pages)) if 1 <= n <= total_pages]
        if not wanted:
            return {"covid": covid, "resolved": False, "resolved_via": None,
                    "pages_escalated": 0, "capped": False,
                    "reason": f"none of pages {sorted(set(pages))} exist in a "
                              f"{total_pages}-page document"}
        wanted = wanted[:remaining_page_budget]
        pages_to_try = [images[n - 1] for n in wanted]
        n_pages = len(pages_to_try)
    else:
        n_pages = min(remaining_page_budget, total_pages, MAX_PAGES_PER_DOCUMENT)
        wanted = list(range(total_pages - n_pages + 1, total_pages + 1))
        pages_to_try = images[-n_pages:]  # last N pages -- see docstring's heuristic note

    texts, confidences, notes = [], [], []
    page_texts: dict[int, str] = {}
    usage_totals = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }
    with tempfile.TemporaryDirectory() as tmp_dir:
        for i, img in enumerate(pages_to_try):
            longest_side = max(img.size)
            if longest_side > MAX_IMAGE_DIMENSION:
                # scale down, preserving aspect ratio, to just under the API's own hard
                # limit -- never scale UP (that would fabricate detail, not preserve it)
                scale = MAX_IMAGE_DIMENSION / longest_side
                img = img.resize((int(img.width * scale), int(img.height * scale)))
            tmp_path = os.path.join(tmp_dir, f"{covid}_page_{i}.png")
            img.save(tmp_path)
            result = ocr_page_image(tmp_path, model=model)
            texts.append(result["text"])
            page_texts[wanted[i]] = result["text"]
            confidences.append(result.get("confidence", 0))
            if result.get("notes"):
                notes.append(result["notes"])
            for key in usage_totals:
                usage_totals[key] += (result.get("usage") or {}).get(key, 0)

    combined_text = "\n\n".join(texts)
    min_confidence = min(confidences) if confidences else None
    print(f"  [ocr_escalation] covid={covid} pages={wanted} total_usage={usage_totals}")

    with open(cached_path, "w", encoding="utf-8") as f:
        json.dump({"covid": covid, "text": combined_text, "min_confidence": min_confidence,
                   "notes": notes, "pages_escalated": n_pages, "page_numbers": wanted,
                   # Partial means FEWER PAGES THAN THE DOCUMENT HAS -- not merely
                   # "pages were named". A last-N escalation of 3 pages out of 21 is
                   # just as partial as a targeted one, and defining it the other way
                   # let exactly that case go on substituting itself for covid 5839's
                   # whole text. Flagged so such a cache is never handed back as a
                   # whole-document transcription.
                   "partial": n_pages < total_pages, "total_pages": total_pages,
                   "page_texts": {str(k): v for k, v in page_texts.items()},
                   "model": model, "usage": usage_totals}, f)

    return {"covid": covid, "resolved": True, "resolved_via": "vision_ocr", "text": combined_text,
            "pages_escalated": n_pages, "page_numbers": wanted,
            "capped": n_pages < total_pages, "min_confidence": min_confidence,
            "page_texts": page_texts, "total_pages": total_pages, "partial": n_pages < total_pages,
            "notes": notes, "usage": usage_totals}


def resolve_low_confidence_covenant(
    covid: str, pdf_path: str, remaining_page_budget: int, model: str = LLM_MODEL_HARDEST,
) -> dict:
    """Convenience wrapper for a standalone caller that hasn't already tried
    the free tier: prefer_fuller_cache first (spends no budget), then
    escalate_to_vision_ocr if still unresolved. app/ingestion/walk.py's own
    iter_candidates already applies prefer_fuller_cache to every candidate
    it loads -- a caller downstream of THAT (e.g. scripts/ingest_probe.py's
    own budgeted escalation step) should call escalate_to_vision_ocr
    directly instead, not this wrapper, to avoid re-checking a fallback
    that's already been tried."""
    fuller = prefer_fuller_cache(covid)
    if fuller is not None:
        return {"covid": covid, "resolved": True, "resolved_via": "fuller_cache",
                "text": fuller["text"], "pages_escalated": 0, "capped": False,
                "min_confidence": fuller.get("vocab_score")}
    return escalate_to_vision_ocr(covid, pdf_path, remaining_page_budget, model)


def resolve_low_confidence_covenants(
    covids_and_paths: list[tuple[str, str]], max_total_pages: int = MAX_PAGES_WITHOUT_APPROVAL,
) -> dict:
    """Shared budget across every covenant passed in -- once max_total_pages
    Fable pages have been spent, everything after that comes back
    capped=True rather than silently continuing to spend."""
    remaining = max_total_pages
    results = {}
    for covid, pdf_path in covids_and_paths:
        result = resolve_low_confidence_covenant(covid, pdf_path, remaining)
        results[covid] = result
        remaining -= result.get("pages_escalated", 0)
    return results

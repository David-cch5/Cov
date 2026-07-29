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


def escalate_to_vision_ocr(
    covid: str, pdf_path: str, remaining_page_budget: int, model: str = LLM_MODEL_HARDEST,
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
    skipped."""
    cached_path = _escalated_cache_path(covid)
    if os.path.exists(cached_path):
        with open(cached_path, encoding="utf-8") as f:
            cached = json.load(f)
        return {"covid": covid, "resolved": True, "resolved_via": "vision_ocr_cached",
                "text": cached["text"], "pages_escalated": 0, "capped": False,
                "min_confidence": cached.get("min_confidence")}

    if remaining_page_budget <= 0:
        return {"covid": covid, "resolved": False, "resolved_via": None,
                "pages_escalated": 0, "capped": True,
                "reason": "no Fable page budget remains for this run -- needs a larger "
                          "budget approved explicitly, not escalated automatically"}

    images = convert_from_path(pdf_path)
    total_pages = len(images)
    n_pages = min(remaining_page_budget, total_pages, MAX_PAGES_PER_DOCUMENT)
    pages_to_try = images[-n_pages:]  # last N pages -- see docstring's heuristic note

    texts, confidences, notes = [], [], []
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
            confidences.append(result.get("confidence", 0))
            if result.get("notes"):
                notes.append(result["notes"])

    combined_text = "\n\n".join(texts)
    min_confidence = min(confidences) if confidences else None

    with open(cached_path, "w", encoding="utf-8") as f:
        json.dump({"covid": covid, "text": combined_text, "min_confidence": min_confidence,
                   "notes": notes, "pages_escalated": n_pages, "model": model}, f)

    return {"covid": covid, "resolved": True, "resolved_via": "vision_ocr", "text": combined_text,
            "pages_escalated": n_pages, "capped": n_pages < total_pages, "min_confidence": min_confidence,
            "notes": notes}


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

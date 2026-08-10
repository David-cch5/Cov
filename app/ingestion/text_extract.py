"""Free text-acquisition tier (CLAUDE.md's tiered OCR policy, tier 1-2) -- the
step that turns a document nobody has read yet into text, plus the quality
assessment that decides whether the paid vision tier is worth spending on.

This did not exist before. Ingestion was corpus-shaped: app/ingestion/walk.py
reads text that _textcache_final already contains and, when there is none,
reports "no cached OCR text found" and routes to review. Nothing anywhere
turned a PDF into text, so a covenant arriving as a dropped file (the intended
final shape of this app -- drop a covenant in, everything runs) could not be
processed at all. `vocab_score`, which the whole confidence gate keys on, was
likewise only ever READ out of the corpus JSON; no code computed it.

WHY TESSERACT AND NOT THE PDF TEXT LAYER
Measured, not assumed: `pdftotext -layout` returns ZERO characters on 12 of 12
sampled corpus documents. These are photographic scans of recorded
instruments with no text layer at all. The text layer is still tried first --
it is free and exact when present, and documents arriving from a recorder's
API may well be born-digital -- but it cannot be the only path, and a text
layer that IS present has to be sanity-checked before being believed (see
looks_like_vendor_overlay).

THE YIELD GATE IS PRIMARY; LEGIBILITY IS A DIAGNOSTIC
This is the important design conclusion, and it is the opposite of what the
existing vocab_score gate assumes. A vocabulary score measures how accurate
the text that WAS read looks -- it says nothing about whether the document was
read at all, and it is at its most confident when almost nothing was:

    covid 4956   vocab_score 1.0000 (!)   13 pages   95 chars/page
                 Every line is the imaging vendor's overlay stamp,
                 "*ACS/TRC* DALLAS Doc: 000287011 ... Page: 1 Of 13".
                 Zero characters of the actual 13-page Dallas County
                 declaration. It scores a perfect 1.0 because DALLAS, Doc,
                 Date, Vol and Page are all real dictionary words, and so it
                 PASSES the >= 0.85 gate -- the only one of 1,056 cached
                 covenants waved through with no readable content.

Characters-per-page catches exactly that, for free, and separates cleanly on
real corpus data: the six documents with no usable body text sit at 56-183
chars/page, and the next document up is at 1,142. A 6x gap with nothing in
it. MIN_CHARS_PER_PAGE is set at 500 -- comfortably above the worst failure
and comfortably below the weakest real document.

Legibility deliberately does NOT block, because on this corpus it does not
discriminate. Two things depress it on perfectly good text. First,
/usr/share/dict/words contains no inflected forms at all -- "jumps", "runs"
and "walked" are all absent -- so even clean English prose scores about 0.8
rather than 1.0, and deed prose full of plurals and past participles scores
lower still. Second, and larger: genuinely readable documents reach 0.43
through lost word spacing alone ("shallrefertoeachpartylistedinParagraph17ofthisDeclaration" is
one token to a tokenizer and a whole clause to a reader) -- covid 3925 at
0.431 and covid 8224 at 0.607 are both perfectly usable. Meanwhile covid 5991
scores a flawless 1.000 on 56 chars/page of pure stamp. So legibility is
recorded as a diagnostic with a low "this is not language at all" tripwire,
and the yield gate does the actual gating.

NOT COMPARABLE TO THE CORPUS'S OWN vocab_score
Whatever produced _textcache_final's vocab_score is not reproducible from the
text: a dictionary-fraction score correlates with it at pearson r = 0.25 over
a 57-document sample. So this module's number is deliberately named
`legibility` and stored separately. Never compare one to the other, and never
substitute this for a corpus vocab_score -- they measure different things on
different scales, and treating them as interchangeable would silently move
the money-spending threshold.
"""
import os
import re
import subprocess

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEED_VOCABULARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "deed_vocabulary.txt")
SYSTEM_WORDS = "/usr/share/dict/words"

# The corpus's own page delimiter -- app/ingestion/ocr_escalation.py's
# merge_escalated_pages already relies on cached text being form-feed
# delimited to locate a given page for free. Text produced here must follow
# the same convention or that page-targeting silently stops working.
PAGE_DELIMITER = "\f"

# Calibrated against the full corpus, not chosen by feel: the 6 documents with
# no usable body text measure 56, 56, 56, 95, 180 and 183 chars/page; the
# weakest document with real content measures 1,142. Anywhere in 200-1,000
# separates them, and 500 sits near the middle of that empty band.
MIN_CHARS_PER_PAGE = 500

# A "not language at all" tripwire, NOT a quality gate -- see the module
# docstring. Real, readable corpus documents reach 0.43, so anything at or
# above this is treated as legible and left to the yield gate to judge.
MIN_LEGIBILITY = 0.25

# Tesseract's own page-segmentation default assumes a single uniform block;
# recorded instruments are multi-column headers over body text, so 3 (fully
# automatic, no OSD) reads them far better.
TESSERACT_CONFIG = "--psm 3"
TESSERACT_DPI = 300

# Oversized sheets in this corpus render to ~142 MILLION pixels at 300 DPI
# (confirmed on covid 2088's own pages, which trip PIL's decompression-bomb
# warning at 89M) -- plats and exhibit maps recorded on large-format paper.
# Left alone, each one is well over a gigabyte of resident bitmap and minutes
# of Tesseract time. Downscaling to fit is safe here rather than lossy in any
# way that matters: a 142M-pixel page scaled to 80M is still ~225 DPI, far
# above what Tesseract needs for printed text. app/ingestion/ocr_escalation.py
# caps the same pages differently (MAX_IMAGE_DIMENSION = 8000) because there
# the constraint is the vision API's own hard per-side limit, not memory.
MAX_RENDER_PIXELS = 80_000_000

# A page that OCRs below this is a candidate for being scanned sideways or
# upside down, and gets re-tried at other rotations. Only suspect pages pay for
# the extra passes, so a normal document (pages at 0.97-0.98) costs nothing
# extra. Set at 0.70 because a rotated page scores around 0.43 while readable
# pages sit far above it.
ROTATION_TRIAL_THRESHOLD = 0.70

# A rotation is accepted only if it beats the original by this much. Necessary
# because a low score does NOT by itself mean "rotated": lost word spacing takes
# genuinely readable pages down to 0.43 too (covid 3925). Requiring a clear
# margin means the trial can only ever improve a page -- if nothing wins
# decisively, the original stands.
ROTATION_ACCEPT_MARGIN = 0.15

# Deliberately NOT pytesseract.image_to_osd. Measured on covid 4956 page 12, an
# upside-down Exhibit A: OSD reported "Orientation in degrees: 270 / Rotate: 90"
# at confidence 21.47, and rotating by its answer yields legibility 0.4356 with
# zero THENCE -- garbage. Rotating 180 instead yields 0.9289 with 4 THENCE and
# 2 BEGINNING. So orientation is chosen by MEASURING each candidate's output,
# the same way every other repair in this project is accepted only when it
# demonstrably resolves the discrepancy.
ROTATION_CANDIDATES = (180, 270, 90)

_TOKEN = re.compile(r"[A-Za-z]{2,}")

# The imaging vendor's overlay stamp, which IS the entire text layer on some of
# these scans (covid 4956). Matched per line so a document whose real text
# merely CONTAINS stamps is unaffected -- only one whose every line is a stamp
# is rejected.
_VENDOR_OVERLAY_LINE = re.compile(
    r"^\s*(?:\*[A-Z/]{2,}\*|Doc(?:ument)?\s*[:#]|Vol\s*[:#]|Page\s*[:#]?\s*\d+\s*(?:Of|/)\s*\d+"
    r"|Page\s+\d+\s+of\s+\d+|Book\s*[:#]|Instrument\s*[:#])",
    re.I,
)

# Browser print-to-PDF chrome: someone saved the recorder's IMAGE VIEWER page
# instead of the document behind it. Confirmed real on covid 8299 -- 12 pages
# of GSCCCA.org viewer headers, URLs and timestamps, no deed at all.
_VIEWER_CHROME_LINE = re.compile(
    r"^\s*(?:https?://|\d{1,2}/\d{1,2}/\d{2,4},\s*\d{1,2}:\d{2}\s*[AP]M\s*$"
    r"|(?:[\w.-]+\.(?:org|com|gov|net))\s*[-–]\s*\w)",
    re.I,
)

_vocabulary: set[str] | None = None


def vocabulary() -> set[str]:
    """The system wordlist plus this corpus's own deed vocabulary. Loaded once
    and cached -- 236k words off disk on every call would dominate the cost of
    scoring a document."""
    global _vocabulary
    if _vocabulary is None:
        words: set[str] = set()
        if os.path.exists(SYSTEM_WORDS):
            with open(SYSTEM_WORDS, encoding="utf-8", errors="replace") as f:
                words = {w.strip().lower() for w in f if w.strip()}
        if os.path.exists(DEED_VOCABULARY):
            with open(DEED_VOCABULARY, encoding="utf-8") as f:
                words |= {w.strip().lower() for w in f
                          if w.strip() and not w.startswith("#")}
        _vocabulary = words
    return _vocabulary


def legibility(text: str) -> float:
    """Fraction of alphabetic tokens that are real words. A DIAGNOSTIC, not a
    gate -- see the module docstring for why this does not discriminate
    usable documents from unusable ones on this corpus, and why it must never
    be compared against the corpus's own vocab_score."""
    words = vocabulary()
    tokens = [t.lower() for t in _TOKEN.findall(text or "")]
    if not tokens:
        return 0.0
    return sum(1 for t in tokens if t in words) / len(tokens)


def _content_lines(text: str) -> list[str]:
    """Non-blank lines that are neither a vendor overlay stamp nor browser
    viewer chrome -- i.e. lines that could plausibly be document body."""
    return [ln for ln in (text or "").splitlines()
            if ln.strip()
            and not _VENDOR_OVERLAY_LINE.match(ln)
            and not _VIEWER_CHROME_LINE.match(ln)]


def looks_like_vendor_overlay(text: str) -> bool:
    """True when a text layer is nothing but the imaging vendor's stamps or a
    saved viewer page -- text that exists, scores well, and contains none of
    the document. Believing one of these is how covid 4956 came to hold 13
    pages of "*ACS/TRC* DALLAS Doc: 000287011" and a perfect vocab_score."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    return len(_content_lines(text)) == 0


def assess(text: str, pages: int) -> dict:
    """Judge extracted text without reference to how it was produced, so the
    same standard applies to a text layer, Tesseract output and a vision-OCR
    merge. Returns the measurements alongside the verdict rather than just a
    boolean -- a caller routing to review needs to say WHICH gate failed and
    by how much, and the numbers get stored as provenance."""
    text = text or ""
    pages = max(int(pages or 0), 0)
    content = "\n".join(_content_lines(text))
    chars_per_page = (len(content) / pages) if pages else 0.0
    score = legibility(content)

    reasons = []
    if pages and chars_per_page < MIN_CHARS_PER_PAGE:
        reasons.append(
            f"only {chars_per_page:.0f} chars/page of document body "
            f"(floor {MIN_CHARS_PER_PAGE}) -- the document was not read, "
            f"whatever the legibility score says"
        )
    if not pages:
        reasons.append("page count unknown -- cannot judge yield per page")
    if score < MIN_LEGIBILITY:
        reasons.append(f"legibility {score:.3f} below {MIN_LEGIBILITY} -- output is not language")
    if looks_like_vendor_overlay(text):
        reasons.append("every line is an imaging-vendor stamp or viewer chrome, not document text")

    return {
        "chars": len(text),
        "content_chars": len(content),
        "pages": pages,
        "chars_per_page": round(chars_per_page, 1),
        "legibility": round(score, 4),
        "usable": not reasons,
        "reasons": reasons,
    }


def pdf_page_count(pdf_path: str) -> int | None:
    """Page count via poppler's pdfinfo -- already a dependency (pdf2image
    shells out to the same toolchain), so this adds nothing new to install."""
    try:
        out = subprocess.run(["pdfinfo", pdf_path], capture_output=True, timeout=120)
        m = re.search(rb"^Pages:\s+(\d+)", out.stdout, re.M)
        return int(m.group(1)) if m else None
    except (OSError, subprocess.SubprocessError):
        return None


def page_sizes_pts(pdf_path: str) -> dict[int, tuple[float, float]]:
    """Per-page {page_no: (width_pt, height_pt)} from pdfinfo. Needed because
    these scans carry oversized page boxes -- covid 2088's pages are 2550 x
    3224 pts, a 35 x 45 INCH sheet, which at 300 DPI is 142.7M pixels."""
    sizes: dict[int, tuple[float, float]] = {}
    try:
        out = subprocess.run(["pdfinfo", "-f", "1", "-l", "10000", pdf_path],
                             capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return sizes
    for m in re.finditer(rb"^Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)\s+pts",
                          out.stdout, re.M):
        sizes[int(m.group(1))] = (float(m.group(2)), float(m.group(3)))
    return sizes


def safe_dpi(width_pt: float, height_pt: float, requested: int = TESSERACT_DPI) -> int:
    """The highest DPI at or below `requested` that keeps the rendered page
    under MAX_RENDER_PIXELS. Computed BEFORE rendering rather than downscaling
    after: a post-hoc resize still pays for the full-size decode, which on
    these oversized sheets is the expensive part."""
    inches_sq = (width_pt / 72.0) * (height_pt / 72.0)
    if inches_sq <= 0:
        return requested
    fitted = int((MAX_RENDER_PIXELS / inches_sq) ** 0.5)
    return max(72, min(requested, fitted))


def extract_text_layer(pdf_path: str) -> str:
    """The PDF's own embedded text, page delimiters preserved. Free and exact
    when it exists -- and empty on every corpus document sampled, so callers
    must treat "" as the normal case and fall through to OCR."""
    try:
        out = subprocess.run(["pdftotext", "-layout", pdf_path, "-"],
                             capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return ""
    # pdftotext already separates pages with form feeds, which is the
    # convention the rest of this codebase expects.
    return out.stdout.decode("utf-8", errors="replace")


def ocr_with_tesseract(pdf_path: str, *, dpi: int = TESSERACT_DPI,
                        max_pages: int | None = None,
                        progress: bool = False) -> tuple[str, dict[int, str], dict[int, int]]:
    """Free OCR, page by page. Returns (form-feed joined text, {page_no: text},
    {page_no: rotation_applied}) -- the per-page mapping is what lets a later
    vision escalation replace one bad page instead of the whole document, the
    same shape ocr_escalation.merge_escalated_pages already consumes, and the
    rotations are provenance: which pages were not scanned upright.

    Rendered one page at a time rather than converting the whole PDF up front:
    a 26-page instrument at 300 DPI is a lot of resident bitmap, and a failure
    on page 9 should still leave pages 1-8 usable rather than losing the run.

    A PAGE SCANNED SIDEWAYS OR UPSIDE DOWN IS RE-TRIED AT OTHER ROTATIONS, and
    the winner is chosen by measuring the output rather than by asking
    Tesseract's OSD, which was measured getting it wrong. This is not an edge
    case: covid 4956's Exhibit A -- the whole legal description, the one page
    that matters most -- is page 12 of 13 and scanned upside down. Read
    upright it yields "ONINIVLNOO CNV ONINNIDAD JO LNIOd" where the deed says
    "POINT OF BEGINNING AND CONTAINING", and zero courses parse. Rotated, it
    yields the real tract: 0.9907 acres in the Elisha Fike Survey, Abstract 478,
    Lot 14 and part of Lot 13, Block 1, Metropolitan Commercial Park Addition.
    """
    import pytesseract
    from pdf2image import convert_from_path

    total = pdf_page_count(pdf_path) or 0
    last = min(total, max_pages) if max_pages else total
    sizes = page_sizes_pts(pdf_path)
    pages: dict[int, str] = {}
    rotations: dict[int, int] = {}
    for n in range(1, (last or 0) + 1):
        page_dpi = safe_dpi(*sizes[n], requested=dpi) if n in sizes else dpi
        if progress and page_dpi != dpi:
            print(f"    page {n}: oversized sheet, rendering at {page_dpi} DPI instead of {dpi}")
        try:
            images = convert_from_path(pdf_path, dpi=page_dpi, first_page=n, last_page=n)
        except Exception as e:
            pages[n] = ""
            if progress:
                print(f"    page {n}: render failed ({type(e).__name__}: {e})")
            continue
        image = images[0]
        try:
            w, h = image.size
            if w * h > MAX_RENDER_PIXELS:
                scale = (MAX_RENDER_PIXELS / (w * h)) ** 0.5
                image.thumbnail((int(w * scale), int(h * scale)))
                if progress:
                    print(f"    page {n}: {w}x{h} downscaled to {image.size[0]}x{image.size[1]}")
            pages[n] = pytesseract.image_to_string(image, config=TESSERACT_CONFIG)
            best_score = legibility("\n".join(_content_lines(pages[n])))
            if best_score < ROTATION_TRIAL_THRESHOLD:
                # Suspect page: try the other orientations and keep whichever
                # reads best, but only if it wins by a clear margin.
                for degrees in ROTATION_CANDIDATES:
                    turned = image.rotate(degrees, expand=True)
                    try:
                        candidate = pytesseract.image_to_string(turned, config=TESSERACT_CONFIG)
                    finally:
                        turned.close()
                    score = legibility("\n".join(_content_lines(candidate)))
                    if score > best_score + ROTATION_ACCEPT_MARGIN:
                        pages[n], best_score = candidate, score
                        rotations[n] = degrees
                if progress:
                    print(f"    page {n}: rotated {rotations.get(n, 0)} deg "
                          f"(legibility now {best_score:.4f})")
        except Exception as e:
            pages[n] = ""
            if progress:
                print(f"    page {n}: OCR failed ({type(e).__name__}: {e})")
        finally:
            image.close()
        if progress:
            print(f"    page {n}/{last}: {len(pages[n])} chars")
    text = PAGE_DELIMITER.join(pages.get(n, "") for n in range(1, (last or 0) + 1))
    return text, pages, rotations


def acquire_text(pdf_path: str, *, max_pages: int | None = None,
                  progress: bool = False) -> dict:
    """The free tier end to end: try the embedded text layer, fall back to
    Tesseract, and assess whichever produced usable output.

    Returns a dict carrying the text, the method that produced it, the
    assessment, and `needs_escalation` -- true when the free tier could not
    produce usable text and the paid vision tier
    (app/ingestion/ocr_escalation.escalate_to_vision_ocr) is the next step.
    Deliberately does NOT escalate itself: what to spend is the caller's
    decision, and this module stays free and side-effect-free so it can run
    over a whole drop folder without costing anything.
    """
    pages_total = pdf_page_count(pdf_path)
    attempts = []

    layer = extract_text_layer(pdf_path)
    layer_assessment = assess(layer, pages_total or 0)
    attempts.append({"method": "pdf_text_layer", **layer_assessment})
    if layer_assessment["usable"]:
        return {"pdf_path": pdf_path, "text": layer, "page_texts": None,
                "method": "pdf_text_layer", "pages": pages_total, "rotations": {},
                "assessment": layer_assessment, "attempts": attempts,
                "needs_escalation": False}

    if progress:
        why = "; ".join(layer_assessment["reasons"]) or "no embedded text"
        print(f"  text layer unusable ({why}) -- running Tesseract")

    text, page_texts, rotations = ocr_with_tesseract(pdf_path, max_pages=max_pages,
                                                     progress=progress)
    ocr_assessment = assess(text, pages_total or 0)
    attempts.append({"method": "tesseract", **ocr_assessment})

    return {"pdf_path": pdf_path, "text": text, "page_texts": page_texts,
            "method": "tesseract", "pages": pages_total, "rotations": rotations,
            "assessment": ocr_assessment, "attempts": attempts,
            "needs_escalation": not ocr_assessment["usable"]}


def worst_pages(page_texts: dict[int, str], limit: int = 3) -> list[int]:
    """The page numbers a paid escalation should target: fewest content
    characters first. Naming pages beats escalate_to_vision_ocr's last-N
    heuristic, which its own docstring flags as a guess -- and which would
    have read pages 19-21 of covid 5839 while the tract it needed ran 14-17.
    """
    scored = sorted(page_texts.items(),
                    key=lambda kv: len("\n".join(_content_lines(kv[1] or ""))))
    return [n for n, _ in scored[:limit]]

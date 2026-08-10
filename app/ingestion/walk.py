"""Walk the covenant corpus, match each covid to its template and county, and surface
anything that needs review before field extraction runs. Deterministic, no LLM calls.
"""
import csv
import json
import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

from app.ingestion.ocr_escalation import prefer_fuller_cache

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEXTCACHE = os.path.join(PROJECT_ROOT, "_textcache_final")
COVENANT_MATRIX_PATH = os.path.join(PROJECT_ROOT, "Covenant_Matrix", "covenant_matrix.json")
COVID_INDEX_PATH = os.path.join(PROJECT_ROOT, "_pilot", "covid_index.csv")

# Per BUILD_SPEC section 2: these covids have no PDF on file at all.
MISSING_PDF_COVIDS = {"2506", "3504", "3516", "7642"}

# Confirmed real OCR/data-entry artifacts in _pilot/covid_index.csv's own county column
# (covid 4123: "DOUGLAS OQ." instead of "DOUGLAS", the county table's actual name for
# Colorado's Douglas County) -- corrected here rather than in the source index file
# itself (a read-only data location per CLAUDE.md), and kept as an explicit, narrowly-
# scoped map rather than fuzzy-matching county names in general, which risks silently
# mismatching a genuinely different multi-word county name.
_KNOWN_COUNTY_NAME_TYPOS = {
    ("COLORADO", "DOUGLAS OQ."): "DOUGLAS",
}


@dataclass
class CovenantCandidate:
    covid: int
    relpath: Optional[str]
    state_name: Optional[str]
    county_name: Optional[str]
    county_fips: Optional[str]
    template_version_id: Optional[str]
    template_confidence: Optional[float]
    text: Optional[str]
    pages: Optional[int]
    ocr: Optional[bool]
    vocab_score: Optional[float]
    needs_review: bool = False
    review_reason: Optional[str] = None


def _load_covid_index() -> dict:
    idx = {}
    with open(COVID_INDEX_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            idx[row["covid"]] = row
    return idx


def _load_covid_map() -> dict:
    data = json.load(open(COVENANT_MATRIX_PATH, encoding="utf-8"))
    return {row["covid"]: row for row in data["covid_map"]}


def _load_county_fips_lookup(session) -> dict:
    rows = session.execute(text("SELECT state_code, county_name, county_fips FROM county")).fetchall()
    return {(r.state_code, r.county_name): r.county_fips for r in rows}


def _state_code_for_name(session, state_name: str) -> Optional[str]:
    row = session.execute(
        text("SELECT state_code FROM state WHERE state_name = :n"), {"n": state_name}
    ).fetchone()
    return row.state_code if row else None


def iter_candidates(session, covids: list[str]):
    """Yield a CovenantCandidate per requested covid, resolved as far as possible.
    Anything unresolvable (missing PDF, county not yet in the reference table,
    template not identified, poor OCR) is flagged needs_review rather than guessed --
    per CLAUDE.md, low-confidence data goes to the review queue, never a guess.
    """
    covid_index = _load_covid_index()
    covid_map = _load_covid_map()
    fips_lookup = _load_county_fips_lookup(session)

    for covid in covids:
        idx_row = covid_index.get(covid)

        if covid in MISSING_PDF_COVIDS:
            yield CovenantCandidate(
                covid=int(covid), relpath=None,
                state_name=idx_row["state"] if idx_row else None,
                county_name=idx_row["county"] if idx_row else None,
                county_fips=None, template_version_id=None, template_confidence=None,
                text=None, pages=None, ocr=None, vocab_score=None,
                needs_review=True, review_reason="no PDF on file",
            )
            continue

        if idx_row is None:
            yield CovenantCandidate(
                covid=int(covid), relpath=None, state_name=None, county_name=None,
                county_fips=None, template_version_id=None, template_confidence=None,
                text=None, pages=None, ocr=None, vocab_score=None,
                needs_review=True, review_reason="covid not found in covid_index.csv",
            )
            continue

        relpath = idx_row["relpath"]
        text_files = [f for f in os.listdir(TEXTCACHE) if f.startswith(f"{covid}_")]
        doc_text = pages = ocr_flag = vocab_score = None
        if text_files:
            # 19 of the 1,056 cached covenants have MORE THAN ONE cache file,
            # and this used to take text_files[0] -- whichever os.listdir
            # happened to return first, i.e. filesystem order. For covid 4497
            # that is a coin flip between 4497_D2045 (54,005 chars over 14
            # pages, a real document) and 4497_D20638 (4,691 chars over 26
            # pages, 180 chars/page of nothing). Pick by yield instead, the
            # same principle prefer_fuller_cache already applies ACROSS cache
            # directories, now applied WITHIN one.
            cached = _best_cache_file(TEXTCACHE, text_files)
            doc_text = cached.get("text")
            pages = cached.get("pages")
            ocr_flag = cached.get("ocr")
            vocab_score = cached.get("vocab_score")

            # Free, deterministic, zero-cost check -- no LLM call, just comparing two
            # already-existing cached extractions -- run for EVERY candidate, not just
            # low-vocab_score ones. Confirmed real and necessary: covid 8245's own
            # _textcache_final carried a 0.9927 vocab_score (a whole-document average)
            # despite having lost its Exhibit A's opening courses entirely -- the
            # vocab_score gate below would never have caught it. _textcache's own JSON
            # has no vocab_score field at all (only ever computed during whatever
            # produced _textcache_final), so using its fuller text means quality is
            # honestly unknown rather than assumed good -- falls through to the "no OCR
            # vocab score computed" reason below, same as any other un-scored text.
            fuller = prefer_fuller_cache(covid)
            if fuller is not None:
                doc_text = fuller["text"]
                vocab_score = fuller.get("vocab_score")

        state_name = idx_row["state"]
        county_name = _KNOWN_COUNTY_NAME_TYPOS.get((state_name, idx_row["county"]), idx_row["county"])
        state_code = _state_code_for_name(session, state_name)
        county_fips = fips_lookup.get((state_code, county_name)) if state_code else None

        map_row = covid_map.get(covid)
        template_version_id = map_row["version_id"] if map_row else None
        template_confidence = map_row.get("confidence") if map_row else None

        # Hard blockers: nothing downstream can responsibly run without these,
        # regardless of what an LLM extraction attempt might report.
        reasons = []
        if doc_text is None:
            reasons.append("no cached OCR text found")
        if county_fips is None:
            reasons.append(f"county not resolved ({state_name}/{county_name})")
        if vocab_score is None and doc_text is not None:
            reasons.append("no OCR vocab score computed -- quality unknown, not assumed good")
        elif vocab_score is not None and vocab_score < 0.85:
            reasons.append(f"low OCR vocab score ({vocab_score})")

        # A template-clustering miss (no match, or landed in the clustering pass's
        # own "review"/"unreadable" catch-all buckets, e.g. "U27") is deliberately
        # NOT a hard blocker by itself -- confirmed real, not hypothetical (covid
        # 4981, Collin): clustered into U27 ("unreadable" per covenant_template's
        # own status), yet this document's actual cached text is ordinary, legible
        # Declaration-of-Covenant boilerplate with a 0.9203 vocab_score -- the
        # cluster label reflects the ORIGINAL clustering pass's own judgment (made
        # once, on whatever text/heuristic it had then), not this document's
        # current, separately-and-directly-measured OCR quality. extract_fields
        # already has a documented fallback for exactly this ("template not yet
        # identified -- extract from the raw text directly") and reports its own
        # confidence -- refusing to even attempt that real, confidence-gated read
        # is strictly less informative than making it. Recorded as a note either
        # way, never silently dropped, but it no longer forces needs_review by
        # itself the way an actual OCR/county blocker still does.
        template_note = None
        if template_version_id is None:
            template_note = "template not identified -- extracting directly from raw text"
        elif not template_version_id.startswith("V"):
            template_note = (
                f"template {template_version_id} is a clustering-pass review/unreadable bucket, not "
                "a confirmed real template -- extracting directly from raw text rather than trusting "
                "its boilerplate hint"
            )

        all_notes = reasons + ([template_note] if template_note else [])

        yield CovenantCandidate(
            covid=int(covid), relpath=relpath, state_name=state_name, county_name=county_name,
            county_fips=county_fips, template_version_id=template_version_id,
            template_confidence=template_confidence, text=doc_text, pages=pages,
            ocr=ocr_flag, vocab_score=vocab_score,
            needs_review=bool(reasons), review_reason="; ".join(all_notes) if all_notes else None,
        )


def _best_cache_file(cache_dir: str, filenames: list[str]) -> dict:
    """The fullest of several cache files for one covid, measured as document
    body characters per page (app/ingestion/text_extract.assess's own yield
    measure, so a file that is all vendor page-stamps counts as empty rather
    than as text). Falls back to raw length when a file reports no page count.

    Single-file covids -- the overwhelming majority -- take the same path and
    get the same answer, so this is not a special case bolted on.
    """
    from app.ingestion.text_extract import assess

    best, best_yield = None, -1.0
    for name in sorted(filenames):
        try:
            with open(os.path.join(cache_dir, name), encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, ValueError):
            continue
        a = assess(cached.get("text") or "", cached.get("pages") or 0)
        score = a["chars_per_page"] if cached.get("pages") else float(a["content_chars"])
        if score > best_yield:
            best, best_yield = cached, score
    return best if best is not None else {}


def get_deed_text(session, covid: int, legal_description_raw: str | None = None) -> str:
    """The full OCR'd text of a covenant's own recorded document, preferred over
    covenant.legal_description_raw wherever the deed's actual field notes matter
    -- confirmed real and necessary on covid 4781, whose legal_description_raw is
    an ingestion-time SUMMARY literally containing the placeholder "[metes and
    bounds courses follow]" instead of the deed's own real courses (which do
    exist, complete, in the full textcache text). Falls back to
    legal_description_raw only when no cached document text is available.

    Lives here rather than in either caller because both app/gis/anchor_resolver.py
    (course extraction) and app/gis/classifier.py (adjoining-subdivision
    detection) need it, and anchor_resolver already imports classifier -- putting
    it in either one would be a circular import. This module already owns
    TEXTCACHE and imports nothing from app.gis.
    """
    doc = session.execute(
        text("SELECT relpath FROM covenant_document WHERE covid = :covid AND doc_type = 'original'"),
        {"covid": covid},
    ).fetchone()
    if doc and doc.relpath:
        cache_path = os.path.join(TEXTCACHE, f"{covid}_{os.path.basename(doc.relpath)}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                full_text = json.load(f).get("text")
            if full_text:
                return full_text
    return legal_description_raw or ""

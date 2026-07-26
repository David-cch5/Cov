"""Walk the covenant corpus, match each covid to its template and county, and surface
anything that needs review before field extraction runs. Deterministic, no LLM calls.
"""
import csv
import json
import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEXTCACHE = os.path.join(PROJECT_ROOT, "_textcache_final")
COVENANT_MATRIX_PATH = os.path.join(PROJECT_ROOT, "Covenant_Matrix", "covenant_matrix.json")
COVID_INDEX_PATH = os.path.join(PROJECT_ROOT, "_pilot", "covid_index.csv")

# Per BUILD_SPEC section 2: these covids have no PDF on file at all.
MISSING_PDF_COVIDS = {"2506", "3504", "3516", "7642"}


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
            cached = json.load(open(os.path.join(TEXTCACHE, text_files[0]), encoding="utf-8"))
            doc_text = cached.get("text")
            pages = cached.get("pages")
            ocr_flag = cached.get("ocr")
            vocab_score = cached.get("vocab_score")

        state_name = idx_row["state"]
        county_name = idx_row["county"]
        state_code = _state_code_for_name(session, state_name)
        county_fips = fips_lookup.get((state_code, county_name)) if state_code else None

        map_row = covid_map.get(covid)
        template_version_id = map_row["version_id"] if map_row else None
        template_confidence = map_row.get("confidence") if map_row else None

        reasons = []
        if doc_text is None:
            reasons.append("no cached OCR text found")
        if county_fips is None:
            reasons.append(f"county not resolved ({state_name}/{county_name})")
        if template_version_id is None:
            reasons.append("template not identified")
        elif not template_version_id.startswith("V"):
            reasons.append(f"template {template_version_id} is a review/unreadable-cluster bucket, not a real template")
        if vocab_score is None and doc_text is not None:
            reasons.append("no OCR vocab score computed -- quality unknown, not assumed good")
        elif vocab_score is not None and vocab_score < 0.85:
            reasons.append(f"low OCR vocab score ({vocab_score})")

        yield CovenantCandidate(
            covid=int(covid), relpath=relpath, state_name=state_name, county_name=county_name,
            county_fips=county_fips, template_version_id=template_version_id,
            template_confidence=template_confidence, text=doc_text, pages=pages,
            ocr=ocr_flag, vocab_score=vocab_score,
            needs_review=bool(reasons), review_reason="; ".join(reasons) if reasons else None,
        )

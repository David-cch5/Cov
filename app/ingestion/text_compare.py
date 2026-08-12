"""Compare the readings that exist for one covenant's legal descriptions.

Three readings exist per legal description and NONE is the referee: the original
OCR (`_textcache_final`), a fresh read, and the team's reviewed sheet
(`COV_EXHA_EXTRACT.xlsx`). Which one is right is decided per tract, by the land
-- covid 4981's Young Survey tract walks to 0.02 ft from the sheet and 115.13 ft
from the document's own OCR, the same 14 courses with one misread in the scan.

Two rules govern the comparison, both from expensive mistakes:

  MATCH ON CONTENT, NEVER ON POSITION. The sheet's rows for a covid are not in
  the document's tract order. Acreage plus survey/abstract identifies a tract;
  row number identifies nothing.

  AN ABSENT STRING IS EVIDENCE ABOUT OUR COPY, NOT ABOUT THE LAND. Covid 4981's
  "55.73" is present in the OCR as "55 73" -- reporting it missing would have
  been a statement about a lost decimal point dressed up as a finding about a
  tract. `mentions_acreage` therefore matches digits across a flexible
  separator, and a genuine absence is reported as "this COPY does not evidence
  it", which is a document-acquisition lead rather than a conclusion.

The output feeds anchoring directly: a tract whose reading closes tightly is one
worth placing, and a tract that closes at 1:25 is a reading problem to solve
before any GIS work starts.
"""
import re
from dataclasses import dataclass, field

from app.ingestion.exha_sheet import SheetRow, read_sheet

# "containing 11.878 acres", "11.878 acre tract", "11.878 acres of land"
_ACREAGE_RE = re.compile(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*acres?\b", re.IGNORECASE)
_STATED_CUE_RE = re.compile(
    r"(?:containing|contains|comprising|being)\s+(?:approximately\s+)?"
    r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*acres?", re.IGNORECASE)
_ABSTRACT_RE = re.compile(r"Abstract\s*(?:No\.?|Number)?\s*\.?\s*([A-Z]?-?\s?\d{1,5})",
                          re.IGNORECASE)
_SURVEY_RE = re.compile(
    r"([A-Z][A-Za-z.'\-]*(?:\s+[A-Z][A-Za-z.'\-]*){0,4})\s+Survey\b")
_LOT_BLOCK_RE = re.compile(r"\bLots?\s+([\w\-]+)[^.]{0,40}?\bBlock\s+([\w\-]+)", re.IGNORECASE)
_DECLARATION_RE = re.compile(r"\bWHEREAS\b")

# A traverse closing tighter than 1:10,000 is a boundary worth placing. Covid
# 4981's two readings sat either side of this by four orders of magnitude --
# 1:186,912 from the sheet, 1:25 from the document's own OCR -- and the second
# is a reading to fix, not a tract to anchor.
_ANCHORABLE_CLOSURE = 10_000


@dataclass
class TractFacts:
    """What can be read off one tract description, from any source."""

    stated_acres: float | None = None
    all_acreages: list[float] = field(default_factory=list)
    abstract: str | None = None
    survey: str | None = None
    lot_block: tuple[str, str] | None = None
    course_count: int = 0
    closure_ratio: float | None = None
    closure_error_ft: float | None = None
    area_acres: float | None = None
    chars: int = 0
    is_declaration: bool = False

    @property
    def content_key(self) -> tuple:
        """What identifies this tract across sources. Never the row number."""
        acres = round(self.stated_acres, 1) if self.stated_acres else None
        return (acres, _normalize_abstract(self.abstract))

    @property
    def closure_denominator(self) -> float | None:
        """Closure as surveyors state it: the N in 1:N.

        walk_traverse reports the raw fraction (error over perimeter), so
        covid 4981's Young tract comes back as 5.35e-06, not 186,912. Reading
        that number as the ratio makes a survey-grade traverse look like a
        closure of zero, which is how the first version of this module
        reported every tract in the portfolio as unanchorable.
        """
        if not self.closure_ratio:
            return None
        return 1.0 / self.closure_ratio

    @property
    def area_agrees(self) -> bool | None:
        """Does the traverse reproduce the acreage the deed states?"""
        if self.area_acres is None or not self.stated_acres:
            return None
        return abs(self.area_acres - self.stated_acres) / self.stated_acres < 0.01


def _normalize_abstract(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", value)
    return digits or None


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def tract_facts(text: str) -> TractFacts:
    """Read one tract description into comparable facts. Never raises."""
    from app.parsing.legal_description.metes_bounds import extract_courses, walk_traverse

    facts = TractFacts(chars=len(text or ""))
    if not text:
        return facts
    facts.is_declaration = bool(_DECLARATION_RE.search(text[:1500]))

    facts.all_acreages = [a for a in (_to_float(m.group(1)) for m in _ACREAGE_RE.finditer(text))
                          if a is not None]
    cue = _STATED_CUE_RE.search(text)
    if cue:
        facts.stated_acres = _to_float(cue.group(1))
    elif facts.all_acreages:
        # No "containing" clause: the tract's own acreage is the largest figure
        # recited, since the smaller ones are typically adjoiner or excepted
        # tracts named in passing.
        facts.stated_acres = max(facts.all_acreages)

    abstract = _ABSTRACT_RE.search(text)
    if abstract:
        facts.abstract = abstract.group(1).strip()
    survey = _SURVEY_RE.search(text)
    if survey:
        facts.survey = " ".join(survey.group(1).split())
    lot_block = _LOT_BLOCK_RE.search(text)
    if lot_block:
        facts.lot_block = (lot_block.group(1), lot_block.group(2))

    try:
        courses = extract_courses(text)
    except Exception:                                          # noqa: BLE001
        return facts
    facts.course_count = len(courses)
    if len(courses) >= 3:
        try:
            walked = walk_traverse(courses)
            facts.closure_ratio = walked.get("closure_ratio")
            facts.closure_error_ft = walked.get("closure_error_ft")
            facts.area_acres = walked.get("area_acres")
        except Exception:                                      # noqa: BLE001
            pass
    return facts


def mentions_acreage(text: str, acres: float) -> bool:
    """Is this acreage present in this copy, allowing for a lost decimal point?

    Covid 4981's "55.73" survives in the OCR as "55 73". Matching the digits
    across a flexible separator is the difference between a finding about the
    land and a finding about a scan.
    """
    if not text or not acres:
        return False
    whole, _, frac = f"{acres:.3f}".rstrip("0").rstrip(".").partition(".")
    if not frac:
        return re.search(rf"\b{re.escape(whole)}\b", text) is not None
    return re.search(rf"\b{re.escape(whole)}\s*[.,·]?\s*{re.escape(frac)}\b", text) is not None


def compare_covenant(covid: int, document_text: str | None = None,
                     sheet_rows: list[SheetRow] | None = None) -> dict:
    """Compare the sheet's tracts for one covenant against the document copy.

    Returns a per-tract reading with its closure, and two disagreement lists:
    tracts the sheet describes that this copy of the document does not evidence,
    and acreages the document recites that the sheet has no row for. Neither is
    a verdict -- the first is usually a bad scan, the second is usually an
    adjoiner or an excepted tract mentioned in passing -- but both are the
    places where a tract goes missing, which is what this is for.
    """
    rows = sheet_rows if sheet_rows is not None else [r for r in read_sheet() if r.covid == covid]
    if document_text is None:
        from app.db.session import get_session
        from app.ingestion.walk import get_deed_text
        with get_session() as session:
            document_text = get_deed_text(session, covid) or ""

    tracts, unevidenced = [], []
    for row in rows:
        if not row.is_tract:
            continue
        facts = tract_facts(row.text)
        evidenced = (mentions_acreage(document_text, facts.stated_acres)
                     if facts.stated_acres else None)
        entry = {
            "sheet_row": row.row_number, "facts": facts, "evidenced_in_document": evidenced,
            "declaration": facts.is_declaration,
        }
        tracts.append(entry)
        if evidenced is False and not facts.is_declaration:
            unevidenced.append(entry)

    sheet_acreages = {round(t["facts"].stated_acres, 2) for t in tracts
                      if t["facts"].stated_acres}
    document_only = sorted({round(a, 2) for a in
                            (_to_float(m.group(1)) for m in _ACREAGE_RE.finditer(document_text))
                            if a is not None and a >= 1.0} - sheet_acreages)

    return {
        "covid": covid, "sheet_tracts": tracts,
        "document_chars": len(document_text),
        "unevidenced_in_document": unevidenced,
        "acreages_only_in_document": document_only,
        "anchorable": [t for t in tracts
                       if (t["facts"].closure_denominator or 0) >= _ANCHORABLE_CLOSURE
                       and t["facts"].area_agrees],
    }


def sweep(covids: list[int] | None = None) -> dict:
    """Run the comparison across the sheet, reading only local files.

    No network, no LLM, no recorder or GIS traffic -- this is a text diff over
    files already on disk, which is why it can cover every covenant without
    touching the cost gate the full portfolio run is waiting on.
    """
    import glob
    import json
    import os

    from app.ingestion.exha_sheet import PROJECT_ROOT

    cache = {}
    for path in glob.glob(os.path.join(PROJECT_ROOT, "_textcache_final", "*.json")):
        try:
            with open(path, encoding="utf-8") as handle:
                blob = json.load(handle)
        except (OSError, ValueError):
            continue
        covid = blob.get("covid")
        if covid is None:
            continue
        cache.setdefault(int(covid), []).append(blob.get("text") or "")

    rows_by_covid: dict[int, list[SheetRow]] = {}
    for row in read_sheet():
        rows_by_covid.setdefault(row.covid, []).append(row)

    targets = covids if covids is not None else sorted(rows_by_covid)
    results, no_copy = {}, []
    for covid in targets:
        texts = cache.get(covid)
        if not texts:
            no_copy.append(covid)
            continue
        results[covid] = compare_covenant(covid, max(texts, key=len),
                                          rows_by_covid.get(covid, []))
    return {"results": results, "covids_without_a_document_copy": no_copy}

"""Drop-file intake: a PDF nobody has seen before becomes a covenant row.

This is the front door of the app's intended shape -- drop a covenant in and
everything runs. Everything downstream already existed; what did not was any
way in that does not go through the corpus. app/ingestion/walk.py's
iter_candidates resolves a covid against _pilot/covid_index.csv and
_textcache_final, so it can only ever process documents somebody already
catalogued and OCR'd. A dropped file has no index row, no cached text, no
covid, and no county.

Intake supplies exactly those four things and then hands off to the same
ingest_one the corpus path uses. One ingestion path, two ways to reach it.

COUNTY COMES FROM THE DOCUMENT, and it is measured rather than guessed at.
The winning signal is embarrassingly simple -- the most frequent county name
appearing next to the word "County" -- and it resolves all 28 covenants whose
county is independently known in this database, with nothing wrong and nothing
unresolved. Two traps had to be cleared first, both found by measurement:

  1. "filed in the Recorder's Office of Travis County, Texas" is TEMPLATE
     BOILERPLATE, present in 335 of 1,056 corpus documents including Colorado
     ones. Any naive match sends most of the portfolio to Travis County.

  2. The "STATE OF x / COUNTY OF y" acknowledgement block is NOT the recording
     county -- it is where the notary was standing. It also only appears in 239
     of 1,056 documents, and picks up "STATE OF ACKNOWLEDGMENT". Frequency
     across the whole document beats one authoritative-looking block.

State is resolved the same way and matters more than it looks: county names
repeat across states (Douglas in Colorado, Montgomery in Texas and four other
states), and the corpus is not confined to the two states seeded so far --
covid 2088 is Fairfield County, CONNECTICUT.

A county the reference tables do not know is NOT a guess to be made. It halts
with a reason naming what has to happen: seed the county, then discover its
parcel service and recorder. That is deliberate -- every downstream table is
county_fips-keyed, and inventing one would poison the lot.
"""
import os
import re
import shutil
from collections import Counter

from sqlalchemy import text

from app.db.session import get_session
from app.ingestion.text_extract import PAGE_DELIMITER, acquire_text, assess
from app.ingestion.walk import CovenantCandidate, PROJECT_ROOT

# Where dropped documents land, and where intake files them once read. Both
# app-owned: CLAUDE.md's data locations (<covid>/, _textcache*/, _pilot/,
# Covenant_Matrix/) are read-only source data and intake never writes into them.
DROP_DIR = os.path.join(PROJECT_ROOT, "_dropbox")
INTAKE_DIR = os.path.join(PROJECT_ROOT, "_intake")
INTAKE_TEXT_DIR = os.path.join(PROJECT_ROOT, "_intake_text")

# A minted covid must not collide with the legacy corpus, whose covids run to
# 9175 across both the <covid>/ folders and _textcache_final. Six figures puts
# minted ids unmistakably outside that space, so anyone reading a covid knows
# instantly whether it came from the original portfolio or arrived later.
MINTED_COVID_FLOOR = 100_000

# Template boilerplate, stripped before any county is counted. See the module
# docstring: 335 of 1,056 corpus documents contain this sentence, and it names
# Travis County regardless of where the land or the recording actually is.
_BOILERPLATE = re.compile(
    r"Recorder.{0,3}s\s+Office\s+of\s+Travis\s+County,?\s*(?:State\s+of\s+)?\w*",
    re.I,
)

_COUNTY_MENTION = re.compile(
    r"(?:County\s+of\s+([A-Z][A-Za-z]{2,15})|([A-Z][A-Za-z]{2,15})\s+County)", re.I)
_STATE_MENTION = re.compile(
    r"State\s+of\s+([A-Z][A-Za-z]{3,20}?)(?=[\s,\.\)§\|]|$)", re.I)

# Words that follow "County of" or precede "County" without being a county name.
_NOT_A_COUNTY = {
    "THE", "SAID", "THIS", "SUCH", "ANY", "EACH", "WHICH", "THAT", "AND", "OR",
    "CLERK", "RECORDS", "RECORD", "COURT", "TAX", "APPRAISAL", "DISTRICT",
    "OFFICIAL", "PUBLIC", "REAL", "DEED", "PLAT", "ACKNOWLEDGMENT", "SUBJECT",
}
_NOT_A_STATE = {"ACKNOWLEDGMENT", "INCORPORATION", "ORGANIZATION", "RESIDENCE",
                "TEXAS AX", "THE", "SAID", "THIS", "FACTS", "MIND", "TITLE"}


def resolve_jurisdiction(document_text: str) -> dict:
    """State and county as the document itself states them.

    Returns the winners plus the full tallies and the runner-up margin, because
    a caller deciding whether to trust this needs to see a decisive count
    rather than a bare name -- "MONTGOMERY 9 vs HARRIS 1" and "MONTGOMERY 2 vs
    HARRIS 2" are different situations and only the first should proceed
    unattended.
    """
    cleaned = _BOILERPLATE.sub(" ", document_text or "")

    counties: Counter = Counter()
    for m in _COUNTY_MENTION.finditer(cleaned):
        name = next(g for g in m.groups() if g).upper()
        if name not in _NOT_A_COUNTY:
            counties[name] += 1
    states: Counter = Counter()
    for m in _STATE_MENTION.finditer(cleaned):
        name = m.group(1).strip().upper()
        if name not in _NOT_A_STATE:
            states[name] += 1

    def _winner(tally: Counter) -> tuple[str | None, int, int]:
        top = tally.most_common(2)
        if not top:
            return None, 0, 0
        runner_up = top[1][1] if len(top) > 1 else 0
        return top[0][0], top[0][1], runner_up

    county, county_n, county_next = _winner(counties)
    state, state_n, state_next = _winner(states)
    return {
        "county_name": county, "county_count": county_n, "county_runner_up": county_next,
        "state_name": state, "state_count": state_n, "state_runner_up": state_next,
        "county_tally": dict(counties.most_common(6)),
        "state_tally": dict(states.most_common(4)),
    }


def resolve_county_fips(session, state_name: str | None, county_name: str | None) -> dict:
    """Map a stated state/county onto county_fips, or explain why not.

    Never invents one. covenant.county_fips is NOT NULL and every downstream
    table is keyed by it, so an unseeded county is a halt with instructions,
    not a value to approximate.
    """
    if not state_name or not county_name:
        missing = " and ".join(
            p for p, v in (("state", state_name), ("county", county_name)) if not v)
        return {"county_fips": None,
                "reason": f"could not read the {missing} from the document text"}

    state_code = session.execute(
        text("SELECT state_code FROM state WHERE state_name = :n"), {"n": state_name},
    ).scalar()
    if not state_code:
        return {"county_fips": None,
                "reason": f"state {state_name!r} is not in the state reference table -- "
                          f"seed it before this covenant can be processed"}

    fips = session.execute(
        text("SELECT county_fips FROM county WHERE state_code = :s AND county_name = :c"),
        {"s": state_code, "c": county_name},
    ).scalar()
    if not fips:
        return {"county_fips": None, "state_code": state_code,
                "reason": f"{county_name} County, {state_name} is not in the county reference "
                          f"table -- seed the county, then discover its parcel service and "
                          f"recorder before this covenant can be processed"}
    return {"county_fips": fips, "state_code": state_code, "reason": None}


def _covid_from_filename(filename: str) -> int | None:
    """The corpus convention: covid is the leading number of the file/folder
    name. Honoured when a dropped file follows it, so re-dropping a known
    covenant lands on its existing row instead of minting a duplicate."""
    m = re.match(r"(\d{2,6})[_\-. ]", os.path.basename(filename))
    return int(m.group(1)) if m else None


def assign_covid(session, pdf_path: str) -> dict:
    """The covid this document will be filed under, and how it was decided.

    A leading number in the filename is trusted only when it does not already
    belong to a DIFFERENT document -- otherwise two unrelated files named
    "3346_something.pdf" would silently overwrite each other's covenant.
    """
    claimed = _covid_from_filename(pdf_path)
    basename = os.path.basename(pdf_path)
    if claimed is not None:
        existing = session.execute(
            text("SELECT relpath FROM covenant_document WHERE covid = :c AND doc_type = 'original'"),
            {"c": claimed},
        ).fetchall()
        others = [r.relpath for r in existing
                  if os.path.basename(r.relpath or "") != basename]
        if not others:
            return {"covid": claimed, "how": "filename",
                    "note": f"filename claims covid {claimed}"
                            + (" (already on file, re-ingesting)" if existing else "")}
        return {"covid": _mint_covid(session), "how": "minted",
                "note": f"filename claims covid {claimed}, but that covid already holds a "
                        f"different document ({others[0]!r}) -- minted a new covid instead"}
    return {"covid": _mint_covid(session), "how": "minted",
            "note": "filename carries no covid"}


def _mint_covid(session) -> int:
    """Next covid at or above MINTED_COVID_FLOOR. Read under the caller's
    transaction; the pipeline runs one intake job per file at a time per the
    queue's path-uniqueness, so two mints cannot race for the same number."""
    highest = session.execute(
        text("SELECT max(covid) FROM covenant WHERE covid >= :floor"),
        {"floor": MINTED_COVID_FLOOR},
    ).scalar()
    return (highest + 1) if highest else MINTED_COVID_FLOOR


def _text_cache_path(covid: int, basename: str) -> str:
    return os.path.join(INTAKE_TEXT_DIR, f"{covid}_{basename}.json")


def read_or_acquire_text(covid: int, pdf_path: str, *, progress: bool = False) -> dict:
    """Text for a dropped document, cached so a re-run costs nothing.

    Mirrors _textcache_final's shape (relpath/filename/covid/pages/text plus
    this module's own measurements) so anything already reading that format
    reads these too -- including ocr_escalation.merge_escalated_pages, which
    needs the form-feed page delimiter acquire_text preserves.
    """
    import json

    basename = os.path.basename(pdf_path)
    cache_path = _text_cache_path(covid, basename)
    if os.path.exists(cache_path):
        with open(cache_path, encoding="utf-8") as f:
            cached = json.load(f)
        cached["from_cache"] = True
        return cached

    acquired = acquire_text(pdf_path, progress=progress)
    record = {
        "relpath": os.path.relpath(pdf_path, PROJECT_ROOT),
        "filename": basename,
        "covid": str(covid),
        "pages": acquired["pages"],
        "text": acquired["text"],
        "ocr": acquired["method"] == "tesseract",
        "method": acquired["method"],
        # Named legibility, never vocab_score: it is not on the same scale as
        # the corpus's own number (pearson r=0.25) and must not be compared to
        # it or substituted for it.
        "legibility": acquired["assessment"]["legibility"],
        "chars_per_page": acquired["assessment"]["chars_per_page"],
        "usable": acquired["assessment"]["usable"],
        "assessment_reasons": acquired["assessment"]["reasons"],
        "needs_escalation": acquired["needs_escalation"],
        "page_texts": {str(k): v for k, v in (acquired["page_texts"] or {}).items()},
        "from_cache": False,
    }
    os.makedirs(INTAKE_TEXT_DIR, exist_ok=True)
    with open(cache_path, "w", encoding="utf-8") as f:
        json.dump(record, f)
    return record


def file_document(covid: int, pdf_path: str) -> str:
    """Copy the dropped file into _intake/<covid>/ and return its project
    relative path. Copy, not move: the original stays put until the caller has
    committed, so a crash mid-intake cannot lose somebody's document."""
    target_dir = os.path.join(INTAKE_DIR, str(covid))
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(pdf_path))
    if os.path.abspath(target) != os.path.abspath(pdf_path):
        shutil.copy2(pdf_path, target)
    return os.path.relpath(target, PROJECT_ROOT)


def candidate_for_dropped_file(session, pdf_path: str, *, progress: bool = False) -> dict:
    """Turn a dropped PDF into a CovenantCandidate ingest_one can consume.

    Returns {"candidate": ..., "covid": ..., "notes": [...]} -- a candidate is
    always produced, carrying needs_review and a reason when something could
    not be resolved, because that is how the rest of the pipeline expects to
    hear about a problem. Nothing here raises on unreadable input; it reports.
    """
    notes: list[str] = []
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)
    if not pdf_path.lower().endswith(".pdf"):
        raise ValueError(f"intake handles PDFs; got {os.path.basename(pdf_path)!r}")

    assigned = assign_covid(session, pdf_path)
    covid = assigned["covid"]
    notes.append(assigned["note"])

    relpath = file_document(covid, pdf_path)
    filed_path = os.path.join(PROJECT_ROOT, relpath)

    acquired = read_or_acquire_text(covid, filed_path, progress=progress)
    reasons: list[str] = []
    if not acquired["usable"]:
        reasons.extend(acquired["assessment_reasons"])
        notes.append(f"free text acquisition ({acquired['method']}) did not produce usable "
                     f"text: {'; '.join(acquired['assessment_reasons'])}")
    else:
        notes.append(f"read via {acquired['method']}: {acquired['chars_per_page']} chars/page, "
                     f"legibility {acquired['legibility']}")

    jurisdiction = resolve_jurisdiction(acquired["text"])
    resolved = resolve_county_fips(session, jurisdiction["state_name"],
                                   jurisdiction["county_name"])
    if resolved["county_fips"]:
        notes.append(f"county from the document: {jurisdiction['county_name']} County, "
                     f"{jurisdiction['state_name']} -> {resolved['county_fips']} "
                     f"(named {jurisdiction['county_count']}x, runner-up "
                     f"{jurisdiction['county_runner_up']}x)")
    else:
        reasons.append(resolved["reason"])
        notes.append(f"county unresolved: {resolved['reason']} "
                     f"(tally {jurisdiction['county_tally']}, {jurisdiction['state_tally']})")

    candidate = CovenantCandidate(
        covid=covid,
        relpath=relpath,
        state_name=jurisdiction["state_name"],
        county_name=jurisdiction["county_name"],
        county_fips=resolved["county_fips"],
        # Template clustering is a corpus artefact (Covenant_Matrix's covid
        # map); a dropped document has no cluster assignment. extract_fields
        # already documents a fallback for exactly this -- extract from the raw
        # text directly -- so this is a known state, not a defect.
        template_version_id=None,
        template_confidence=None,
        text=acquired["text"] or None,
        pages=acquired["pages"],
        ocr=acquired["ocr"],
        # vocab_score stays None on purpose. It is the corpus's own measure and
        # this text was never scored on that scale; writing legibility into it
        # would make a readable document (legibility 0.43-0.99) look like a
        # failing 0.85 confidence gate and buy vision-OCR pages for nothing.
        vocab_score=None,
        legibility=acquired["legibility"],
        text_usable=acquired["usable"],
        needs_review=bool(reasons),
        review_reason="; ".join(reasons) if reasons else None,
    )
    return {"candidate": candidate, "covid": covid, "notes": notes,
            "acquired": acquired, "jurisdiction": jurisdiction}


def pending_drops(drop_dir: str = DROP_DIR) -> list[str]:
    """PDFs waiting in the drop folder, oldest first so a backlog is worked in
    the order it arrived."""
    if not os.path.isdir(drop_dir):
        return []
    paths = [os.path.join(drop_dir, n) for n in os.listdir(drop_dir)
             if n.lower().endswith(".pdf") and not n.startswith(".")]
    return sorted(paths, key=lambda p: (os.path.getmtime(p), p))


def already_ingested(session, pdf_path: str) -> int | None:
    """The covid a dropped file was already filed under, if any -- so the
    watcher can skip a file it has finished with instead of re-enqueueing it
    every scan. Matched on basename, which is what survives the copy into
    _intake/<covid>/."""
    basename = os.path.basename(pdf_path)
    return session.execute(
        text("""SELECT covid FROM covenant_document
                 WHERE doc_type = 'original'
                   AND split_part(relpath, '/', -1) = :b
                 ORDER BY covid LIMIT 1"""),
        {"b": basename},
    ).scalar()

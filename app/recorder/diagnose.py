"""Ties county_recorder_registry + the vendor adapters together into the
actual missing-Exhibit-A escalation described in this project's memory notes
(compare the recorder's own stated page count against our local PDF's page
count) -- turning what was, for covid 8386 and 7768 this session, a manual
browser investigation into a function callable directly.

Only vendors with a confirmed page-count-bearing document view can run this
check right now: Acclaim and AVA/Fidlar. PublicSearch's results grid has no
page-count column and no document-detail view was found/built for it yet --
that's reported as unsupported, not silently skipped or guessed at (see
county_recorder_registry.quirks for what each vendor's adapter actually
supports).

covenant.recording_instrument is NOT uniformly formatted across counties
(confirmed: Ellis's covid 8386 has "02530 0135 (File# 8386)" -- a book/page,
not the instrument number 1019671; Kerr's covid 7768 has "007803", missing
the "09-" prefix AVA's own instrument number "09-7803" needs) -- so this
takes search_name/instrument_number as explicit arguments rather than
trusting that field to be directly usable, and the caller (a human or a
future session) is expected to supply the right values, e.g. from the
declaration page's own text, the same way this session did it by hand.

Pass `covid` (+ optionally `county_fips`) to get retry-with-backoff and
durable job_queue failure logging (app/queue/job_queue.py -- shared with
app/gis/classifier.py, not recorder-specific) instead of a bare exception --
see that module's docstring for why this is deliberately NOT self-healing.
Without a covid (e.g. this module's own smoke test), a failure just raises
directly, since there's nothing to attach a job to and no real covenant
being blocked.
"""
import re

from sqlalchemy import text

from app.queue.job_queue import JobFailed, run_with_job_queue
from app.recorder.adapters import acclaim, ava_fidlar
from app.recorder.session import recorder_context

# Ellis's own recording_instrument extraction came back "02530 0135 (File#
# 8386)" -- a book/page, not the true instrument number 1019671 Acclaim's
# search actually needs. Only fire the automated Acclaim check when the
# extracted value looks like a clean instrument number (digits/hyphens only)
# rather than attempt it with data already known to be the wrong shape.
_CLEAN_INSTRUMENT_RE = re.compile(r"^[\d-]{5,}$")


def check_corpus_completeness(local_pages: int, vendor: str, base_url: str, *,
                               search_name: str | None = None,
                               instrument_number: str | None = None,
                               book: str | None = None, page: str | None = None,
                               covid: int | None = None,
                               county_fips: str | None = None) -> dict:
    """Compare `local_pages` (covenant_document.pages for the covid in
    question) against the county recorder's own stated page count for the
    matching document. Returns {"checked": False, "reason": ...} for a
    vendor/argument combination this function can't run yet, rather than
    raising -- callers should treat that the same as "couldn't verify," not
    as "no gap exists". A JobFailed can still propagate (see
    app.queue.job_queue) once retries are exhausted -- that's the one
    case this deliberately doesn't swallow into a dict, since it means the
    portal itself may have changed and a human needs to look."""
    if vendor == "acclaim_harris_recording_solutions":
        if not (search_name and instrument_number):
            return {"checked": False, "reason": "acclaim requires search_name and instrument_number"}

        def _call():
            with recorder_context() as context:
                return acclaim.get_document_detail(context, base_url, search_name, instrument_number)

        detail = _run(
            _call, job_type="recorder_acclaim_document_detail",
            county_fips=county_fips, covid=covid,
            payload={"base_url": base_url, "search_name": search_name, "instrument_number": instrument_number},
        )
        recorder_pages = detail["number_of_pages"] if detail else None
        if detail is None:
            return {"checked": False, "reason": f"no document matching instrument_number={instrument_number!r} found"}

    elif vendor == "fidlar_ava":
        if not ((book and page) or search_name):
            return {"checked": False, "reason": "fidlar_ava requires book+page or search_name"}

        def _call():
            with recorder_context() as context:
                if book and page:
                    return ava_fidlar.search(context, base_url, book=book, page=page)
                return ava_fidlar.search(context, base_url, last_name=search_name)

        result = _run(
            _call, job_type="recorder_ava_search",
            county_fips=county_fips, covid=covid,
            payload={"base_url": base_url, "book": book, "page": page, "search_name": search_name},
        )
        docs = result["documents"]
        if not docs or "page_count" not in docs[0]:
            return {"checked": False,
                    "reason": f"search returned {result['result_count']} result(s), none with a parsed page_count "
                              f"(only a single-result response includes it inline -- see adapter docstring)"}
        recorder_pages = docs[0]["page_count"]

    else:
        return {"checked": False, "reason": f"vendor {vendor!r} has no page-count-capable adapter function yet"}

    gap = recorder_pages - local_pages
    return {
        "checked": True,
        "local_pages": local_pages,
        "recorder_pages": recorder_pages,
        "missing_pages": gap,
        "corpus_gap_suspected": gap > 0,
    }


def maybe_flag_missing_exhibit(session, *, covid: int, county_fips: str,
                                declarant_name: str | None, book: str | None, page: str | None,
                                recording_instrument: str | None, local_pages: int | None) -> str | None:
    """Called from the ingestion pipeline right after field extraction, when
    a covenant's legal_description_type came back 'unknown' -- the exact
    signature of "Exhibit A referenced but the exhibit itself is missing or
    blank" that took a manual investigation to catch for Ellis (covid 8386)
    and Kerr (covid 7768). If this county's recorder portal is registered
    with a page-count-capable vendor and we have enough to attempt the
    check, runs it and returns a short note to append to review_reason.

    Returns None if the check couldn't even be attempted (no registry entry,
    unsupported vendor, missing/unusable search parameters, or local_pages
    unknown) -- silently doing nothing is correct there, since the existing
    "legal description type unknown" flag already covers it.

    This NEVER resolves the covenant or upgrades its status: a gap found
    here is a lead for a human or a future session to retrieve the actual
    missing pages (the same manual step this session did by hand), not an
    automatic fix. And "no gap found" doesn't mean "nothing's wrong" either
    -- Bexar's covid 2497 had no missing pages at all; the exhibit was
    genuinely left blank in the original 2009 recording, a real drafting
    defect this check can't distinguish from "still investigating" without
    a human reading the result. Every note this returns says so explicitly,
    because both directions here are hypotheses to confirm, not answers.
    """
    if local_pages is None:
        return None

    row = session.execute(
        text("SELECT base_url, quirks->>'vendor' AS vendor FROM county_recorder_registry WHERE county_fips = :cf"),
        {"cf": county_fips},
    ).fetchone()
    if row is None or row.vendor not in ("acclaim_harris_recording_solutions", "fidlar_ava"):
        return None

    search_name = declarant_name.split(",")[0].strip() if declarant_name else None
    if row.vendor == "fidlar_ava":
        if not (book and page):
            return None
        kwargs = {"book": book, "page": page}
    else:  # acclaim_harris_recording_solutions
        if not (search_name and recording_instrument and _CLEAN_INSTRUMENT_RE.match(recording_instrument)):
            return None
        kwargs = {"search_name": search_name, "instrument_number": recording_instrument}

    try:
        result = check_corpus_completeness(
            local_pages=local_pages, vendor=row.vendor, base_url=row.base_url,
            covid=covid, county_fips=county_fips, **kwargs,
        )
    except JobFailed as e:
        return (f"attempted an automated corpus-completeness check against the county recorder portal, "
                f"but it failed after retries (see job_queue.job_id={e.job_id} for details) -- needs manual follow-up")

    if not result["checked"]:
        return None  # couldn't run (no match, vendor gap, etc.) -- nothing worth adding to review_reason

    if result["corpus_gap_suspected"]:
        return (f"AUTOMATED CHECK (using extracted book/page/instrument -- not human-confirmed): "
                f"county recorder shows {result['recorder_pages']} pages vs our {result['local_pages']} -- "
                f"likely missing pages, needs manual retrieval of the gap before treating this as confirmed "
                f"(see how covid 8386 and 7768 were resolved).")
    return (f"AUTOMATED CHECK (using extracted book/page/instrument -- not human-confirmed): "
            f"county recorder shows the same {result['recorder_pages']} pages we have -- the missing legal "
            f"description is likely a genuine defect in the original recording, not a corpus gap "
            f"(see covid 2497 for a confirmed example of this).")


def _run(fn, *, job_type: str, county_fips: str | None, covid: int | None, payload: dict):
    """covid is what distinguishes a real covenant lookup (wants retries +
    a durable job_queue record on failure) from an ad hoc probe like this
    module's own smoke test (just wants a direct answer or a direct
    exception, no job_queue noise)."""
    if covid is None:
        return fn()
    return run_with_job_queue(fn, job_type=job_type, county_fips=county_fips, covid=covid, payload=payload)

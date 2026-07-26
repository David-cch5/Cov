"""Run ingestion for the sanctioned probe scope: Montgomery County TX + the 4-covenant
multi-county pilot. Not the full portfolio -- see CLAUDE.md's scope guardrail.

Each covenant commits independently (idempotent + resumable, per BUILD_SPEC): one
document failing (e.g. an API error) never blocks or rolls back the others.

Usage: python3 scripts/ingest_probe.py
"""
import sys

sys.path.insert(0, ".")

from app.db.session import get_session
from app.ingestion.walk import iter_candidates
from app.parsing.template_fields import extract_fields
from app.db.repository import upsert_contact, upsert_covenant, upsert_covenant_document, insert_source
from app.recorder.diagnose import maybe_flag_missing_exhibit

MONTGOMERY_TX_COVIDS = ["3346", "4781", "4440", "8245", "4780", "3194", "3297"]
PILOT_COVIDS = ["7029", "5340", "5835", "3428"]
# One covenant each from 10 further TX counties (Bexar, Denton, Collin, Harris, Kerr,
# Travis, Nueces, Webb, Ellis, Hunt) -- picked as the cleanest-OCR (highest vocab_score)
# covenant in each county from _pilot/covid_index.csv, to broaden the small multi-county
# sample per CLAUDE.md's scope guardrail without approaching the full portfolio.
EXPANDED_TX_SAMPLE_COVIDS = ["2497", "7938", "4955", "7991", "7768", "7994", "5963", "2340", "8386", "5346"]


def ingest_one(session, c) -> None:
    # covenant row must exist first -- covenant_document.covid is a FK to it.
    upsert_covenant(
        session, covid=c.covid, county_fips=c.county_fips, declarant_raw=None,
        declarant_contact_id=None, fee_percent=None, term_description=None,
        recording_instrument=None, recording_date=None, book=None, page=None,
        template_version_id=c.template_version_id if c.template_version_id and
            c.template_version_id.startswith("V") else None,
        stated_acreage=None, legal_description_raw=None,
        legal_description_type=None, exemptions_raw=None, fee_due_days=None,
        status="needs_review" if c.needs_review else "ingested",
        review_reason=c.review_reason, source_id=None,
    )

    if c.relpath:
        doc_source_id = insert_source(
            session, source_type="textcache_ocr", reference=c.relpath,
            confidence=c.vocab_score,
        )
        upsert_covenant_document(
            session, relpath=c.relpath, covid=c.covid, doc_type="original",
            pages=c.pages, ocr_engine="tesseract", vocab_score=c.vocab_score,
            confidence=c.vocab_score, source_id=doc_source_id,
        )

    if c.needs_review:
        print(f"  needs_review: {c.review_reason}")
        return

    fields = extract_fields(c.text, c.template_version_id)
    print(f"  extracted: declarant={fields.get('declarant_name')!r} "
          f"fee%={fields.get('fee_percent')} confidence={fields.get('confidence')}")
    if fields.get("extraction_notes"):
        print(f"  extraction_notes: {fields['extraction_notes']}")

    extraction_source_id = insert_source(
        session, source_type="textcache_ocr", reference=c.relpath,
        engine="claude-sonnet-5", confidence=fields.get("confidence"),
    )

    declarant_contact_id = None
    if fields.get("declarant_name"):
        declarant_contact_id = upsert_contact(
            session, name_raw=fields["declarant_name"],
            mailing_address=fields.get("declarant_address"),
            contact_type=fields.get("declarant_type"),
            source_id=extraction_source_id,
        )

    status = "parsed"
    review_reason = None
    confidence = fields.get("confidence") or 0
    if confidence < 0.7:
        status = "needs_review"
        review_reason = f"low extraction confidence ({confidence})"

    if fields.get("legal_description_type") == "unknown":
        # The exact signature of "Exhibit A referenced but missing/blank" this
        # project hit repeatedly (Ellis covid 8386, Kerr covid 7768) -- worth
        # an automated first look via the county recorder portal rather than
        # waiting for a human to notice and escalate manually, the way those
        # two were originally caught.
        status = "needs_review"
        base_reason = review_reason or "legal description type could not be determined from the extracted text"
        note = maybe_flag_missing_exhibit(
            session, covid=c.covid, county_fips=c.county_fips,
            declarant_name=fields.get("declarant_name"),
            book=fields.get("book"), page=fields.get("page"),
            recording_instrument=fields.get("recording_instrument"),
            local_pages=c.pages,
        )
        review_reason = f"{base_reason}; {note}" if note else base_reason
        if note:
            print(f"  recorder check: {note}")

    upsert_covenant(
        session, covid=c.covid, county_fips=c.county_fips,
        declarant_raw=fields.get("declarant_name"),
        declarant_contact_id=declarant_contact_id,
        fee_percent=fields.get("fee_percent"),
        term_description=fields.get("term_description"),
        recording_instrument=fields.get("recording_instrument"),
        recording_date=fields.get("recording_date"),
        book=fields.get("book"), page=fields.get("page"),
        template_version_id=c.template_version_id,
        stated_acreage=fields.get("stated_acreage"),
        legal_description_raw=fields.get("legal_description_raw"),
        legal_description_type=fields.get("legal_description_type"),
        exemptions_raw=fields.get("exemptions_raw"),
        fee_due_days=fields.get("fee_due_days"),
        status=status, review_reason=review_reason,
        source_id=extraction_source_id,
    )


def run(covids: list[str]) -> None:
    with get_session() as lookup_session:
        candidates = list(iter_candidates(lookup_session, covids))

    succeeded, failed = [], []
    for c in candidates:
        print(f"--- covid {c.covid} ---")
        try:
            with get_session() as session:
                ingest_one(session, c)
            succeeded.append(c.covid)
        except Exception as exc:
            print(f"  FAILED: {exc}")
            failed.append((c.covid, str(exc)))

    print(f"\n{len(succeeded)} succeeded, {len(failed)} failed")
    for covid, err in failed:
        print(f"  {covid}: {err}")


if __name__ == "__main__":
    run(MONTGOMERY_TX_COVIDS + PILOT_COVIDS + EXPANDED_TX_SAMPLE_COVIDS)

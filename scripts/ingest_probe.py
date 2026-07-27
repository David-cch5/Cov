"""Run ingestion for the sanctioned probe scope: Montgomery County TX + the 4-covenant
multi-county pilot. Not the full portfolio -- see CLAUDE.md's scope guardrail.

Each covenant commits independently (idempotent + resumable, per BUILD_SPEC): one
document failing (e.g. an API error) never blocks or rolls back the others.

Usage: python3 scripts/ingest_probe.py
"""
import re
import sys
from datetime import date

from sqlalchemy import text

sys.path.insert(0, ".")

from app.db.session import get_session
from app.ingestion.walk import iter_candidates
from app.parsing.template_fields import extract_fields
from app.db.repository import (
    upsert_contact, upsert_covenant, upsert_covenant_beneficiary, upsert_covenant_document,
    upsert_covenant_trustee, insert_source,
)
from app.recorder.diagnose import maybe_flag_missing_exhibit

# Statuses a later pipeline stage (GIS classification, chain-of-title) could have set --
# re-running ingestion must never regress one of these back to 'parsed' just because
# ingestion itself is clean this time; something else may still need attention.
_DO_NOT_REGRESS_STATUSES = {"gis_classified", "reconciled", "title_in_progress", "done", "needs_review"}


def _merge_ingestion_note(existing_reason: str | None, ingestion_note: str | None) -> str:
    """Ingestion is meant to be re-runnable (this file's own docstring:
    "idempotent + resumable"), but a bare status/review_reason overwrite on
    every run isn't actually idempotent once OTHER pipeline stages
    (GIS classification, chain-of-title) have added their own notes to the
    same covenant -- confirmed the hard way: re-running ingest_one() on an
    already-progressed covid 2497 silently wiped its "RE-VERIFIED" acreage-
    reconciliation note. Same "tagged note, safely replaceable, never
    touches other stages' notes" pattern as app/title/chain.py's
    _update_covenant_gap_notes: only ingestion's own tagged section gets
    replaced here, whatever else is in review_reason is left untouched."""
    reason = existing_reason or ""
    reason = re.sub(r";?\s*INGESTION-STAGE \(automated[^)]*\):.*$", "", reason).strip("; ").strip()
    if ingestion_note:
        note = f"INGESTION-STAGE (automated, {date.today().isoformat()}): {ingestion_note}"
        reason = f"{reason}; {note}" if reason else note
    return reason

MONTGOMERY_TX_COVIDS = ["3346", "4781", "4440", "8245", "4780", "3194", "3297"]
PILOT_COVIDS = ["7029", "5340", "5835", "3428"]
# One covenant each from 10 further TX counties (Bexar, Denton, Collin, Harris, Kerr,
# Travis, Nueces, Webb, Ellis, Hunt) -- picked as the cleanest-OCR (highest vocab_score)
# covenant in each county from _pilot/covid_index.csv, to broaden the small multi-county
# sample per CLAUDE.md's scope guardrail without approaching the full portfolio.
EXPANDED_TX_SAMPLE_COVIDS = ["2497", "7938", "4955", "7991", "7768", "7994", "5963", "2340", "8386", "5346"]


def ingest_one(session, c) -> None:
    # Fetched BEFORE anything else writes to this row: the whole point is to see the
    # state any LATER pipeline stage (GIS classification, chain-of-title) left this
    # covenant in, so nothing below can clobber it -- see _merge_ingestion_note's
    # docstring for the real incident (a bare overwrite here once silently wiped a
    # prior acreage-reconciliation note on a re-run of this exact function).
    existing = session.execute(
        text("SELECT status, review_reason FROM covenant WHERE covid = :covid"), {"covid": c.covid},
    ).fetchone()

    if existing is None:
        # covenant row must exist first -- covenant_document.covid is a FK to it. Only
        # done for a covid never seen before; an existing row is left alone here and
        # updated properly (merged, not overwritten) further down.
        upsert_covenant(
            session, covid=c.covid, county_fips=c.county_fips, declarant_raw=None,
            declarant_contact_id=None, fee_percent=None, term_description=None,
            recording_instrument=None, recording_date=None, book=None, page=None,
            template_version_id=c.template_version_id if c.template_version_id and
                c.template_version_id.startswith("V") else None,
            stated_acreage=None, legal_description_raw=None,
            legal_description_type=None, exemptions_raw=None, fee_due_days=None,
            status="needs_review" if c.needs_review else "ingested",
            review_reason=_merge_ingestion_note(None, c.review_reason), source_id=None,
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
        if existing is not None:
            # a re-run that still can't get past the walk-step check (bad OCR, no
            # template match) -- merge its note in rather than leave the row exactly
            # as some earlier, possibly stale run last left it.
            merged = _merge_ingestion_note(existing.review_reason, c.review_reason)
            if existing.status not in _DO_NOT_REGRESS_STATUSES or existing.status == "needs_review":
                session.execute(
                    text("UPDATE covenant SET status = 'needs_review', review_reason = :r, "
                         "updated_at = now() WHERE covid = :covid"),
                    {"r": merged or None, "covid": c.covid},
                )
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

    # Trustee/Beneficiaries: every covenant's own text names these (fees are paid TO the
    # Trustee, for the Beneficiaries, per each template's own AMOUNT DUE section) but
    # covenant_trustee/covenant_beneficiary have existed since 0001_initial_schema with
    # nothing populating them. effective_date is temporal-keyed (see
    # upsert_covenant_trustee's docstring) -- the covenant's own recording_date, since
    # this is the trustee/beneficiary structure as of the original Declaration.
    effective_date = fields.get("recording_date")
    if fields.get("trustee_name"):
        if effective_date:
            trustee_contact_id = upsert_contact(
                session, name_raw=fields["trustee_name"], mailing_address=fields.get("trustee_address"),
                source_id=extraction_source_id,
            )
            upsert_covenant_trustee(
                session, covid=c.covid, effective_date=effective_date,
                contact_id=trustee_contact_id, source_id=extraction_source_id,
            )
        else:
            print(f"  trustee named ({fields['trustee_name']!r}) but no recording_date extracted -- "
                  f"can't set covenant_trustee's effective_date, skipped")

    beneficiaries = fields.get("beneficiaries") or []
    if beneficiaries:
        if effective_date:
            seq, total_pct = 0, 0.0
            for b in beneficiaries:
                pct = b.get("percentage_interest")
                if pct is None:
                    continue  # named but percentage illegible/not stated -- never guess it
                seq += 1
                total_pct += pct
                beneficiary_contact_id = upsert_contact(
                    session, name_raw=b["name"], mailing_address=b.get("address"),
                    source_id=extraction_source_id,
                )
                upsert_covenant_beneficiary(
                    session, covid=c.covid, beneficiary_seq=seq, effective_date=effective_date,
                    contact_id=beneficiary_contact_id, percentage_interest=pct,
                    source_id=extraction_source_id,
                )
            # a 1% band allows for ordinary rounding in the recited percentages themselves,
            # not a substitute for exact-match verification.
            if seq < len(beneficiaries) or abs(total_pct - 100.0) > 1.0:
                status = "needs_review"
                note = (f"beneficiary percentages recorded sum to {total_pct:.1f}% across {seq} of "
                        f"{len(beneficiaries)} named beneficiaries -- list is likely incomplete "
                        f"(OCR/extraction limitation), do not treat as exhaustive")
                review_reason = f"{review_reason}; {note}" if review_reason else note
                print(f"  beneficiary check: {note}")
        else:
            print(f"  {len(beneficiaries)} beneficiaries named but no recording_date extracted -- "
                  f"can't set covenant_beneficiary's effective_date, skipped")

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

    # `existing` was fetched at the very top of this function, before anything in this
    # call had a chance to touch the row -- merge ingestion's own tagged note into it
    # rather than overwrite; status only advances forward, never regresses a covenant
    # that's already progressed past ingestion's own view, unless ingestion itself
    # found a fresh problem just now.
    merged_review_reason = _merge_ingestion_note(existing.review_reason if existing else None, review_reason)
    if review_reason:
        final_status = "needs_review"
    elif existing and existing.status in _DO_NOT_REGRESS_STATUSES:
        final_status = existing.status
    else:
        final_status = status

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
        status=final_status, review_reason=merged_review_reason or None,
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

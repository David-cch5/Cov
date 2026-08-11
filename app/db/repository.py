"""Idempotent upsert helpers, keyed on each table's natural key (or, for tables with
no natural key -- contact, source -- a best-effort match before insert / an
always-insert provenance log, respectively). Plain parameterized SQL, not an ORM --
the query set here is small enough that Core/ORM machinery would add indirection
without adding safety.
"""
import json
import re
from datetime import datetime, timezone

from sqlalchemy import text


def _normalize_name(name: str) -> str:
    """Strip punctuation and collapse whitespace so trivial extraction variance
    ("L.P." vs "LP." vs "LP,") doesn't produce duplicate contact rows for the
    same real-world party."""
    return re.sub(r"\s+", " ", re.sub(r"[.,]", "", name)).strip().upper()


def upsert_contact(session, name_raw: str, mailing_address: str | None = None,
                    contact_type: str | None = None, source_id: int | None = None) -> int:
    normalized = _normalize_name(name_raw)
    existing = session.execute(
        text("SELECT contact_id FROM contact WHERE name_normalized = :n"), {"n": normalized},
    ).fetchone()
    if existing:
        # Backfill anything newly learned -- never overwrite a known value with a
        # weaker one, but a later, better-informed pass shouldn't be silently ignored.
        session.execute(
            text("""
                UPDATE contact SET
                    mailing_address = COALESCE(mailing_address, :addr),
                    contact_type = COALESCE(contact_type, :ctype)
                WHERE contact_id = :id
            """),
            {"addr": mailing_address, "ctype": contact_type, "id": existing.contact_id},
        )
        return existing.contact_id

    row = session.execute(
        text("""
            INSERT INTO contact (name_raw, name_normalized, mailing_address, contact_type, source_id)
            VALUES (:name, :normalized, :addr, :ctype, :source_id)
            RETURNING contact_id
        """),
        {
            "name": name_raw, "normalized": normalized, "addr": mailing_address,
            "ctype": contact_type, "source_id": source_id,
        },
    ).fetchone()
    return row.contact_id


def insert_source(session, source_type: str, reference: str, engine: str | None = None,
                   is_estimated: bool = False, confidence: float | None = None) -> int:
    row = session.execute(
        text("""
            INSERT INTO source (source_type, reference, engine, is_estimated, confidence, retrieved_at)
            VALUES (:source_type, :reference, :engine, :is_estimated, :confidence, :retrieved_at)
            RETURNING source_id
        """),
        {
            "source_type": source_type, "reference": reference, "engine": engine,
            "is_estimated": is_estimated, "confidence": confidence,
            "retrieved_at": datetime.now(timezone.utc),
        },
    ).fetchone()
    return row.source_id


def upsert_covenant_trustee(session, covid: int, effective_date: str, contact_id: int,
                             source_id: int | None) -> None:
    """effective_date is the date this trustee designation took effect --
    the covenant's own recording_date for the trustee named in the original
    Declaration. A later Trustee-successor filing (Section 12/13-style
    "Trustee Rights"/"Trustee Duties" clauses -- not yet extracted anywhere
    in this pipeline) would get its own row with a later effective_date,
    which is why this is a temporal key (covid, effective_date), not a
    plain one-row-per-covid table."""
    session.execute(
        text("""
            INSERT INTO covenant_trustee (covid, effective_date, contact_id, source_id)
            VALUES (:covid, :effective_date, :contact_id, :source_id)
            ON CONFLICT (covid, effective_date) DO UPDATE SET
                contact_id = EXCLUDED.contact_id, source_id = EXCLUDED.source_id
        """),
        {"covid": covid, "effective_date": effective_date, "contact_id": contact_id, "source_id": source_id},
    )


def upsert_covenant_beneficiary(session, covid: int, beneficiary_seq: int, effective_date: str,
                                 contact_id: int, percentage_interest: float, source_id: int | None) -> None:
    """Same temporal-key reasoning as upsert_covenant_trustee: a later
    Beneficiary Sale/Assignment filing changes who holds a share without
    changing the covenant itself, so this is (covid, beneficiary_seq,
    effective_date), not a plain one-row-per-beneficiary table."""
    session.execute(
        text("""
            INSERT INTO covenant_beneficiary (
                covid, beneficiary_seq, effective_date, contact_id, percentage_interest, source_id
            ) VALUES (
                :covid, :beneficiary_seq, :effective_date, :contact_id, :percentage_interest, :source_id
            )
            ON CONFLICT (covid, beneficiary_seq, effective_date) DO UPDATE SET
                contact_id = EXCLUDED.contact_id,
                percentage_interest = EXCLUDED.percentage_interest,
                source_id = EXCLUDED.source_id
        """),
        {
            "covid": covid, "beneficiary_seq": beneficiary_seq, "effective_date": effective_date,
            "contact_id": contact_id, "percentage_interest": percentage_interest, "source_id": source_id,
        },
    )


def upsert_fee_collection(session, county_fips: str, instrument_number: str, recording_date: str,
                           parcel_apn: str, collection_seq: int, fee_percent_applied: float,
                           base_amount: float | None, due_date: str | None, status: str,
                           notes: str | None, source_id: int | None) -> None:
    """Only the fields app/title/fee_compute.py actually establishes at
    computation time (the base obligation) are set here -- invoiced_amount/
    invoice_date/collected_amount/collected_date/payer_contact_id/
    remittance_reference/collectibility_* belong to later stages (invoicing,
    collection, a statute-of-limitations check) this project hasn't built
    yet, and are left at their schema defaults rather than guessed."""
    session.execute(
        text("""
            INSERT INTO fee_collection (
                county_fips, instrument_number, recording_date, parcel_apn, collection_seq,
                fee_percent_applied, base_amount, due_date, status, notes, source_id
            ) VALUES (
                :county_fips, :instrument_number, :recording_date, :parcel_apn, :collection_seq,
                :fee_percent_applied, :base_amount, :due_date, :status, :notes, :source_id
            )
            ON CONFLICT (county_fips, instrument_number, recording_date, parcel_apn, collection_seq)
            DO UPDATE SET
                fee_percent_applied = EXCLUDED.fee_percent_applied,
                base_amount = EXCLUDED.base_amount,
                due_date = EXCLUDED.due_date,
                status = EXCLUDED.status,
                notes = EXCLUDED.notes,
                source_id = EXCLUDED.source_id
        """),
        {
            "county_fips": county_fips, "instrument_number": instrument_number,
            "recording_date": recording_date, "parcel_apn": parcel_apn, "collection_seq": collection_seq,
            "fee_percent_applied": fee_percent_applied, "base_amount": base_amount,
            "due_date": due_date, "status": status, "notes": notes, "source_id": source_id,
        },
    )


def upsert_covenant_document(session, relpath: str, covid: int, doc_type: str,
                              pages: int | None, ocr_engine: str | None,
                              vocab_score: float | None, confidence: float | None,
                              source_id: int | None) -> None:
    session.execute(
        text("""
            INSERT INTO covenant_document (relpath, covid, doc_type, pages, ocr_engine, vocab_score, confidence, source_id)
            VALUES (:relpath, :covid, :doc_type, :pages, :ocr_engine, :vocab_score, :confidence, :source_id)
            ON CONFLICT (relpath) DO UPDATE SET
                pages = EXCLUDED.pages, ocr_engine = EXCLUDED.ocr_engine,
                vocab_score = EXCLUDED.vocab_score, confidence = EXCLUDED.confidence,
                source_id = EXCLUDED.source_id
        """),
        {
            "relpath": relpath, "covid": covid, "doc_type": doc_type, "pages": pages,
            "ocr_engine": ocr_engine, "vocab_score": vocab_score, "confidence": confidence,
            "source_id": source_id,
        },
    )


def upsert_parcel(session, county_fips: str, apn: str, owner_name_raw: str | None,
                   situs_address: str | None, acreage: float | None, geojson: dict | None,
                   source_id: int | None, city: str | None = None, zip_code: str | None = None,
                   recited_legal_description: str | None = None,
                   geometry_vintage: str | None = None) -> None:
    """Upsert a parcel, SNAPSHOTTING the outgoing row into parcel_history first
    whenever geometry, acreage or owner actually changed.

    A superseded boundary is evidence, not garbage. Dallas's 2019 layer put 6,001
    sq ft of covid 4956's land in the neighbouring parcel; current CAD geometry
    assigns it to the covenanted one. The DIFFERENCE is the record of a 2017
    conveyance reaching the parcel fabric, and a covenant runs with the land -- so
    which land was encumbered WHEN is the substance of the job, not metadata.
    Overwriting in place destroyed the only evidence a boundary had moved at all.

    Only real changes are recorded. An unchanged re-sync writes no history row, so
    the table stays a record of what happened rather than a log of how often the
    pipeline ran. Geometry equality is ST_OrderingEquals, deliberately strict:
    ST_Equals treats a re-noded but spatially identical polygon as unchanged,
    which is true geometrically and false as provenance -- the vertices came from
    a different published layer and that is worth keeping.
    """
    # NEVER let older geometry overwrite current geometry. A multi-layer adapter
    # falls back to its archival layer when the current one has no row for an
    # account, and without this guard the parcel flaps between vintages on
    # successive syncs -- each flap writing a 'replat' history row for a boundary
    # that never moved, and leaving whichever ran last in charge of the answer.
    # Observed exactly that on Dallas 24049800010010100 during a test run.
    #
    # An adapter that reports NO vintage is not "older": single-layer counties
    # pass None and their one layer IS current. Only an explicit archival label
    # loses to a stored 'current'.
    if geometry_vintage not in (None, "current"):
        stored_vintage = session.execute(
            text("SELECT geometry_vintage FROM parcel WHERE county_fips = :c AND apn = :a"),
            {"c": county_fips, "a": apn},
        ).scalar()
        if stored_vintage == "current":
            geojson, acreage, geometry_vintage = None, None, stored_vintage

    # Snapshot BEFORE the upsert, in the same transaction, so a crash cannot
    # leave new geometry with no record of what it replaced.
    session.execute(
        text("""
            INSERT INTO parcel_history (county_fips, apn, captured_at, owner_name_raw, acreage,
                                        geom, change_reason, source_id)
            SELECT p.county_fips, p.apn, now(), p.owner_name_raw, p.acreage, p.geom,
                   -- change_reason is a CODED vocabulary from 0001_initial_schema
                   -- (initial / replat / ownership_change / monitor_diff), not prose.
                   -- Geometry moving is the strongest signal and wins: a boundary
                   -- revision is what 'replat' names, whether the county reached it
                   -- by an actual replat or by reflecting a conveyance in its fabric.
                   -- The narrative belongs in the source row, which records exactly
                   -- which published layer each snapshot came from.
                   CASE
                     WHEN (p.geom IS NULL) <> (:geojson IS NULL)
                          OR (p.geom IS NOT NULL AND :geojson IS NOT NULL
                              AND NOT ST_OrderingEquals(
                                  p.geom, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)))
                       THEN 'replat'
                     WHEN p.owner_name_raw IS DISTINCT FROM CAST(:owner_name_raw AS text)
                       THEN 'ownership_change'
                     ELSE 'monitor_diff'
                   END AS change_reason,
                   p.source_id
              FROM parcel p
             WHERE p.county_fips = :county_fips AND p.apn = :apn
               AND (
                    (p.geom IS NULL) <> (:geojson IS NULL)
                 OR (p.geom IS NOT NULL AND :geojson IS NOT NULL
                     AND NOT ST_OrderingEquals(p.geom,
                             ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326)))
                 -- Compared at the COLUMN'S OWN SCALE. parcel.acreage is
                 -- numeric(12,3), so storing 0.9045038901 keeps 0.905; comparing
                 -- that against the next sync's unrounded 0.9045038901 is always
                 -- "different" and every re-sync wrote a spurious history row.
                 -- Caught by watching the row count go 3 -> 6 on an unchanged
                 -- re-sync: history has to record what happened to the land, not
                 -- how often the pipeline ran.
                 OR p.acreage IS DISTINCT FROM CAST(:acreage AS numeric(12,3))
                 OR p.owner_name_raw IS DISTINCT FROM CAST(:owner_name_raw AS text)
               )
            ON CONFLICT (county_fips, apn, captured_at) DO NOTHING
        """),
        {"county_fips": county_fips, "apn": apn, "acreage": acreage,
         "owner_name_raw": owner_name_raw, "geometry_vintage": geometry_vintage,
         "geojson": json.dumps(geojson) if geojson else None},
    )
    # recited_legal_description is never overwritten with NULL on a later sync that
    # happens not to carry it (not every adapter/query passes it) -- COALESCE keeps
    # whatever was last actually known, same "don't regress a real fact to unknown"
    # convention already used elsewhere in this module.
    session.execute(
        text("""
            INSERT INTO parcel (county_fips, apn, owner_name_raw, situs_address, city, zip_code, acreage, geom,
                                 recited_legal_description, geometry_vintage, last_synced_at, source_id)
            VALUES (
                :county_fips, :apn, :owner_name_raw, :situs_address, :city, :zip_code, :acreage,
                ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326), :recited_legal_description,
                :geometry_vintage, now(), :source_id
            )
            ON CONFLICT (county_fips, apn) DO UPDATE SET
                owner_name_raw = EXCLUDED.owner_name_raw,
                situs_address = EXCLUDED.situs_address,
                city = EXCLUDED.city,
                zip_code = EXCLUDED.zip_code,
                -- COALESCE so a deliberate decline above (older vintage refused)
                -- leaves the good values in place instead of nulling them.
                acreage = COALESCE(EXCLUDED.acreage, parcel.acreage),
                geom = COALESCE(EXCLUDED.geom, parcel.geom),
                recited_legal_description = COALESCE(EXCLUDED.recited_legal_description, parcel.recited_legal_description),
                geometry_vintage = COALESCE(EXCLUDED.geometry_vintage, parcel.geometry_vintage),
                last_synced_at = now(),
                source_id = EXCLUDED.source_id
        """),
        {
            "county_fips": county_fips, "apn": apn, "owner_name_raw": owner_name_raw,
            "situs_address": situs_address, "city": city, "zip_code": zip_code, "acreage": acreage,
            "geojson": json.dumps(geojson) if geojson else None, "source_id": source_id,
            "recited_legal_description": recited_legal_description,
            "geometry_vintage": geometry_vintage,
        },
    )


def upsert_plat(session, county_fips: str, subdivision_name: str, section: str,
                 lookup_status: str, recording_instrument: str | None, recording_date,
                 book_volume_page: str | None, abstract_name: str | None, source_id: int | None) -> int:
    """One row per real plat filing (a subdivision's own section/phase), or a
    single lookup_status='not_found' row (section='') recording that a real
    recorder-portal search was tried and came up empty -- so a later run
    never re-searches a subdivision this project has already asked about.
    Returns plat_id."""
    row = session.execute(
        text("""
            INSERT INTO plat (county_fips, subdivision_name, section, lookup_status,
                               recording_instrument, recording_date, book_volume_page,
                               abstract_name, source_id, updated_at)
            VALUES (:county_fips, :subdivision_name, :section, :lookup_status,
                    :recording_instrument, :recording_date, :book_volume_page,
                    :abstract_name, :source_id, now())
            ON CONFLICT (county_fips, subdivision_name, section) DO UPDATE SET
                lookup_status = EXCLUDED.lookup_status,
                recording_instrument = EXCLUDED.recording_instrument,
                recording_date = EXCLUDED.recording_date,
                book_volume_page = EXCLUDED.book_volume_page,
                abstract_name = EXCLUDED.abstract_name,
                source_id = EXCLUDED.source_id,
                updated_at = now()
            RETURNING plat_id
        """),
        {
            "county_fips": county_fips, "subdivision_name": subdivision_name, "section": section,
            "lookup_status": lookup_status, "recording_instrument": recording_instrument,
            "recording_date": recording_date, "book_volume_page": book_volume_page,
            "abstract_name": abstract_name, "source_id": source_id,
        },
    ).fetchone()
    return row.plat_id


def upsert_transfer(session, county_fips: str, instrument_number: str, covid: int, tract_no: int,
                     parcel_county_fips: str | None, parcel_apn: str | None,
                     prior_county_fips: str | None, prior_instrument_number: str | None,
                     instrument_type: str | None, recording_date: str | None,
                     book: str | None, page: str | None,
                     grantor_contact_id: int | None, grantee_contact_id: int | None,
                     consideration_amount: float | None, legal_description_snapshot: str | None,
                     recorder_source_id: int | None, review_flag: bool, review_reason: str | None,
                     exemption_category: str | None, exemption_basis: str | None,
                     exemption_confidence: float | None, instrument_number_type: str = "modern_instrument",
                     consideration_source_id: int | None = None) -> None:
    """consideration_source_id is deliberately separate from
    recorder_source_id: the latter is the provenance of the transfer
    record itself (which recorder/CAD index found this grantor/grantee/
    date), while a disclosure-state deed's stated price can come from a
    different source (e.g. a separate OCR/vision read of that specific
    deed's image) -- whether that price is actual or estimated is then
    just source.is_estimated on whichever source this points to, not a
    separate flag here."""
    session.execute(
        text("""
            INSERT INTO transfer (
                county_fips, instrument_number, instrument_number_type, covid, tract_no,
                parcel_county_fips, parcel_apn, prior_county_fips, prior_instrument_number,
                instrument_type, recording_date, book, page,
                grantor_contact_id, grantee_contact_id, consideration_amount, consideration_source_id,
                legal_description_snapshot, recorder_source_id,
                review_flag, review_reason, exemption_category, exemption_basis, exemption_confidence
            ) VALUES (
                :county_fips, :instrument_number, :instrument_number_type, :covid, :tract_no,
                :parcel_county_fips, :parcel_apn, :prior_county_fips, :prior_instrument_number,
                :instrument_type, :recording_date, :book, :page,
                :grantor_contact_id, :grantee_contact_id, :consideration_amount, :consideration_source_id,
                :legal_description_snapshot, :recorder_source_id,
                :review_flag, :review_reason, :exemption_category, :exemption_basis, :exemption_confidence
            )
            ON CONFLICT (county_fips, instrument_number, recording_date, parcel_apn) DO UPDATE SET
                instrument_number_type = EXCLUDED.instrument_number_type,
                covid = EXCLUDED.covid, tract_no = EXCLUDED.tract_no,
                parcel_county_fips = EXCLUDED.parcel_county_fips,
                prior_county_fips = EXCLUDED.prior_county_fips,
                prior_instrument_number = EXCLUDED.prior_instrument_number,
                instrument_type = EXCLUDED.instrument_type,
                book = EXCLUDED.book, page = EXCLUDED.page,
                grantor_contact_id = EXCLUDED.grantor_contact_id,
                grantee_contact_id = EXCLUDED.grantee_contact_id,
                consideration_amount = EXCLUDED.consideration_amount,
                consideration_source_id = EXCLUDED.consideration_source_id,
                legal_description_snapshot = EXCLUDED.legal_description_snapshot,
                recorder_source_id = EXCLUDED.recorder_source_id,
                review_flag = EXCLUDED.review_flag, review_reason = EXCLUDED.review_reason,
                exemption_category = EXCLUDED.exemption_category,
                exemption_basis = EXCLUDED.exemption_basis,
                exemption_confidence = EXCLUDED.exemption_confidence,
                -- re-upserting means a walk's CURRENT real_links include this key again --
                -- un-supersede it even if a prior walk had marked it superseded (see
                -- migration 0031 / chain.py's _finalize).
                superseded_at = NULL
        """),
        {
            "county_fips": county_fips, "instrument_number": instrument_number,
            "instrument_number_type": instrument_number_type, "covid": covid, "tract_no": tract_no,
            "parcel_county_fips": parcel_county_fips, "parcel_apn": parcel_apn,
            "prior_county_fips": prior_county_fips, "prior_instrument_number": prior_instrument_number,
            "instrument_type": instrument_type, "recording_date": recording_date,
            "book": book, "page": page,
            "grantor_contact_id": grantor_contact_id, "grantee_contact_id": grantee_contact_id,
            "consideration_amount": consideration_amount, "consideration_source_id": consideration_source_id,
            "legal_description_snapshot": legal_description_snapshot,
            "recorder_source_id": recorder_source_id,
            "review_flag": review_flag, "review_reason": review_reason,
            "exemption_category": exemption_category, "exemption_basis": exemption_basis,
            "exemption_confidence": exemption_confidence,
        },
    )


def upsert_covenant(session, covid: int, county_fips: str | None, declarant_raw: str | None,
                     declarant_contact_id: int | None, fee_percent: float | None,
                     term_description: str | None, recording_instrument: str | None,
                     recording_date: str | None, book: str | None, page: str | None,
                     template_version_id: str | None, stated_acreage: float | None,
                     legal_description_raw: str | None, legal_description_type: str | None,
                     exemptions_raw: str | None, fee_due_days: int | None,
                     status: str, review_reason: str | None, source_id: int | None) -> None:
    session.execute(
        text("""
            INSERT INTO covenant (
                covid, county_fips, declarant_raw, declarant_contact_id, fee_percent,
                term_description, recording_instrument, recording_date, book, page,
                template_version_id, stated_acreage, legal_description_raw,
                legal_description_type, exemptions_raw, fee_due_days, status,
                review_reason, source_id, created_at, updated_at
            ) VALUES (
                :covid, :county_fips, :declarant_raw, :declarant_contact_id, :fee_percent,
                :term_description, :recording_instrument, :recording_date, :book, :page,
                :template_version_id, :stated_acreage, :legal_description_raw,
                :legal_description_type, :exemptions_raw, :fee_due_days, :status,
                :review_reason, :source_id, now(), now()
            )
            ON CONFLICT (covid) DO UPDATE SET
                county_fips = EXCLUDED.county_fips,
                declarant_raw = EXCLUDED.declarant_raw,
                declarant_contact_id = EXCLUDED.declarant_contact_id,
                fee_percent = EXCLUDED.fee_percent,
                term_description = EXCLUDED.term_description,
                recording_instrument = EXCLUDED.recording_instrument,
                recording_date = EXCLUDED.recording_date,
                book = EXCLUDED.book,
                page = EXCLUDED.page,
                template_version_id = EXCLUDED.template_version_id,
                stated_acreage = EXCLUDED.stated_acreage,
                legal_description_raw = EXCLUDED.legal_description_raw,
                legal_description_type = EXCLUDED.legal_description_type,
                exemptions_raw = EXCLUDED.exemptions_raw,
                fee_due_days = EXCLUDED.fee_due_days,
                status = EXCLUDED.status,
                review_reason = EXCLUDED.review_reason,
                source_id = EXCLUDED.source_id,
                updated_at = now()
        """),
        {
            "covid": covid, "county_fips": county_fips, "declarant_raw": declarant_raw,
            "declarant_contact_id": declarant_contact_id, "fee_percent": fee_percent,
            "term_description": term_description, "recording_instrument": recording_instrument,
            "recording_date": recording_date, "book": book, "page": page,
            "template_version_id": template_version_id, "stated_acreage": stated_acreage,
            "legal_description_raw": legal_description_raw,
            "legal_description_type": legal_description_type, "exemptions_raw": exemptions_raw,
            "fee_due_days": fee_due_days, "status": status, "review_reason": review_reason,
            "source_id": source_id,
        },
    )

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
                   source_id: int | None, city: str | None = None, zip_code: str | None = None) -> None:
    session.execute(
        text("""
            INSERT INTO parcel (county_fips, apn, owner_name_raw, situs_address, city, zip_code, acreage, geom, last_synced_at, source_id)
            VALUES (
                :county_fips, :apn, :owner_name_raw, :situs_address, :city, :zip_code, :acreage,
                ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326), now(), :source_id
            )
            ON CONFLICT (county_fips, apn) DO UPDATE SET
                owner_name_raw = EXCLUDED.owner_name_raw,
                situs_address = EXCLUDED.situs_address,
                city = EXCLUDED.city,
                zip_code = EXCLUDED.zip_code,
                acreage = EXCLUDED.acreage,
                geom = EXCLUDED.geom,
                last_synced_at = now(),
                source_id = EXCLUDED.source_id
        """),
        {
            "county_fips": county_fips, "apn": apn, "owner_name_raw": owner_name_raw,
            "situs_address": situs_address, "city": city, "zip_code": zip_code, "acreage": acreage,
            "geojson": json.dumps(geojson) if geojson else None, "source_id": source_id,
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

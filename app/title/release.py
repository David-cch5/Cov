"""Covenant releases: termination and buyout.

A covenant can stop applying to land without ever having been wrong about it.
Two situations do that, for different reasons:

  termination  an instrument terminates the covenant as to some or all of the
               land, including the declarant's own reserved right to terminate
  buyout       the fee obligation is bought out, after which the covenant is no
               longer enforced against that land

WHAT MAKES THIS DIFFERENT FROM AN EXCLUSION
app/gis/classifier.py's exclude_non_tract_parcels says a parcel was NEVER part of
a tract -- a geometric statement, retroactive, and parcel_covenant_exclusion has
no date column because it does not need one. A release says the parcel WAS
encumbered and stopped being so on a date. Recording one as the other would erase
the period the covenant genuinely ran, and with it the basis for every fee
already collected in that period.

So a released parcel STAYS in the census and keeps its history. Nothing before
the effective date changes: earlier transfers keep their fee_collection rows, and
a fee already taken remains correctly taken, because it was owed when it was
taken. Only transfers on or after the effective date become exempt.

That is the same shape as the pre_effective_date exemption at the other end of a
covenant's life, which is why this reports through exemption_category rather than
inventing a parallel mechanism: everything downstream that already understands an
exempt transfer -- fee_compute, payoff statements, estoppel certificates --
understands a released one for free.
"""
from datetime import date

from sqlalchemy import text

RELEASE_TYPES = ("termination", "buyout")
SCOPES = ("covenant", "partial")

# Which exemption a release produces. Kept distinct rather than collapsed into
# one "released" code: an estoppel or payoff statement has to say WHY the
# obligation ended, and a bought-out parcel is not the same fact as a terminated
# one.
EXEMPTION_FOR_TYPE = {"termination": "post_termination", "buyout": "post_buyout"}


def record_release(
    session, covid: int, release_type: str, effective_date: date,
    scope: str = "covenant", parcels: list[tuple[str, str]] | None = None,
    recording_instrument: str | None = None, recording_date: date | None = None,
    consideration_amount: float | None = None, notes: str | None = None,
    source_id: int | None = None,
) -> dict:
    """Record a termination or buyout. `parcels` is [(county_fips, apn), ...]
    and is required for scope='partial'.

    A partial release naming no parcels is refused rather than written: it would
    read as releasing nothing, or as releasing everything, depending on which
    query happened to look at it, and neither is a thing anyone meant.
    """
    if release_type not in RELEASE_TYPES:
        raise ValueError(f"release_type must be one of {RELEASE_TYPES}, got {release_type!r}")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {SCOPES}, got {scope!r}")
    if scope == "partial" and not parcels:
        raise ValueError("a partial release must name the parcels it releases")
    if scope == "covenant" and parcels:
        raise ValueError("a covenant-wide release cannot also name individual parcels -- "
                         "use scope='partial' if only some land is released")

    release_id = session.execute(
        text("""
            INSERT INTO covenant_release (covid, release_type, scope, effective_date,
                                          recording_instrument, recording_date,
                                          consideration_amount, notes, source_id)
            VALUES (:covid, :release_type, :scope, :effective_date, :instrument,
                    :recording_date, :consideration, :notes, :source_id)
            RETURNING release_id
        """),
        {"covid": covid, "release_type": release_type, "scope": scope,
         "effective_date": effective_date, "instrument": recording_instrument,
         "recording_date": recording_date, "consideration": consideration_amount,
         "notes": notes, "source_id": source_id},
    ).scalar()

    for county_fips, apn in (parcels or []):
        session.execute(
            text("""
                INSERT INTO covenant_release_parcel (release_id, county_fips, apn)
                VALUES (:release_id, :county_fips, :apn) ON CONFLICT DO NOTHING
            """),
            {"release_id": release_id, "county_fips": county_fips, "apn": apn},
        )
    return {"release_id": release_id, "covid": covid, "release_type": release_type,
            "scope": scope, "effective_date": effective_date,
            "parcels": len(parcels or [])}


def release_for_transfer(session, covid: int, county_fips: str, apn: str,
                         recording_date: date) -> dict | None:
    """The release that exempts this transfer, or None.

    A transfer is released only when it is recorded ON OR AFTER the effective
    date AND its parcel is in scope. Earlier transfers are deliberately NOT
    matched -- that is the whole point: the fee was owed when it was taken, and
    a later release does not claw it back.

    Where several releases could apply, the EARLIEST effective one wins: the
    obligation ended the first time it ended, and a subsequent instrument cannot
    make it end later.
    """
    row = session.execute(
        text("""
            SELECT r.release_id, r.release_type, r.scope, r.effective_date,
                   r.recording_instrument, r.consideration_amount
            FROM covenant_release r
            WHERE r.covid = :covid
              AND r.effective_date <= :recording_date
              AND (r.scope = 'covenant'
                   OR EXISTS (SELECT 1 FROM covenant_release_parcel p
                              WHERE p.release_id = r.release_id
                                AND p.county_fips = :county_fips AND p.apn = :apn))
            ORDER BY r.effective_date, r.release_id
            LIMIT 1
        """),
        {"covid": covid, "recording_date": recording_date,
         "county_fips": county_fips, "apn": apn},
    ).mappings().first()
    if row is None:
        return None
    return {**dict(row), "exemption_category": EXEMPTION_FOR_TYPE[row["release_type"]]}


def released_parcels(session, covid: int, as_of: date | None = None) -> dict:
    """Every parcel of a covenant released as of a date, and by what.

    Reported per parcel rather than as a flat list because a payoff or estoppel
    statement has to name the instrument and the date, not merely say "released".
    A covenant-wide release covers every parcel in the census, so those are
    expanded here rather than left implicit.
    """
    rows = session.execute(
        text("""
            SELECT DISTINCT ON (pc.county_fips, pc.apn)
                   pc.county_fips, pc.apn, r.release_id, r.release_type,
                   r.effective_date, r.recording_instrument, r.consideration_amount
            FROM covenant_release r
            JOIN parcel_covenant pc ON pc.covid = r.covid
            LEFT JOIN covenant_release_parcel p
                   ON p.release_id = r.release_id
                  AND p.county_fips = pc.county_fips AND p.apn = pc.apn
            WHERE r.covid = :covid
              AND (:as_of IS NULL OR r.effective_date <= :as_of)
              AND (r.scope = 'covenant' OR p.release_id IS NOT NULL)
            ORDER BY pc.county_fips, pc.apn, r.effective_date, r.release_id
        """),
        {"covid": covid, "as_of": as_of},
    ).mappings().all()
    return {(r["county_fips"], r["apn"]): dict(r) for r in rows}


def apply_releases_to_transfers(session, covid: int) -> dict:
    """Stamp post_termination / post_buyout onto every transfer a release
    exempts, so fee computation and everything downstream see it through the
    exemption machinery they already use.

    Only transfers with NO exemption category yet are touched. An existing
    exemption is an independent finding about that transfer -- a foreclosure, a
    spousal transfer -- and is not overwritten just because the land was later
    released; the transfer was exempt for its own reason at the time.
    """
    result = session.execute(
        text("""
            UPDATE transfer t
            SET exemption_category = CASE r.release_type
                                       WHEN 'termination' THEN 'post_termination'
                                       ELSE 'post_buyout' END,
                exemption_basis = 'covenant_release ' || r.release_id
                                  || ' (' || r.release_type || ', effective '
                                  || r.effective_date || ')',
                exemption_confidence = 1.0
            FROM covenant_release r
            WHERE t.covid = :covid
              AND t.exemption_category IS NULL
              AND t.superseded_at IS NULL
              AND r.covid = t.covid
              AND r.effective_date <= t.recording_date
              AND (r.scope = 'covenant'
                   OR EXISTS (SELECT 1 FROM covenant_release_parcel p
                              WHERE p.release_id = r.release_id
                                AND p.county_fips = t.parcel_county_fips
                                AND p.apn = t.parcel_apn))
            RETURNING t.instrument_number
        """),
        {"covid": covid},
    ).fetchall()
    return {"covid": covid, "transfers_exempted": len(result)}

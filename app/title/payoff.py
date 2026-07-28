"""Payoff/resale-certificate generation -- the piece app/title/fee_compute.py's
own docstring explicitly defers: "Interest / payoff-statement computation is
NOT done here -- covenant.fee_payoff_statement already exists for that
(computed fresh at request time...)." Nothing had generated one until now.

The gap migration 0029 (parcel_lineage) was built to close: a fee owed on a
bulk/tract-level transfer attaches to the LAND under this covenant family's
own lien language, not to whatever apn happens to exist today. If that tract
was later split/replatted/merged (app/gis/monitor.py now records exactly
this in parcel_lineage), a payoff request against one of today's descendant
lots has to walk BACKWARD through that lineage to find whichever ancestor
apn(s) actually carry the unpaid fee_collection row -- the descendant lot's
own apn never appears on that original transfer directly.

Interest accrues simple (not compounded), from the fee's own due_date (per
fee_compute.py's convention: recording_date, or +fee_due_days if the
template grants a grace period) through the requested good_through_date, at
covenant_template.unpaid_interest_percent annual -- the only interest rate
this project has actually extracted from a template so far. A fee with no
known base_amount (a non-disclosure-state covenant with no price on file)
gets no payoff statement at all, same "never estimate a price -- leave it
null rather than guessed" discipline compute_fee_for_transfer already
follows; there is nothing to compute a payoff FROM without one.
"""
from datetime import date

from sqlalchemy import text

from app.db.repository import insert_source

# Only a fee_collection row still genuinely outstanding needs a payoff computed --
# 'paid'/'waived' need nothing, and 'uncollectible' is a separate, already-decided outcome.
_UNRESOLVED_FEE_STATUSES = ("owed", "invoiced", "partial", "delinquent", "lien_filed")


def find_lineage_ancestors(session, county_fips: str, apn: str) -> list[tuple[str, str]]:
    """Every ancestor (county_fips, apn) reachable by walking parcel_lineage's
    parent_* links upward from the given lot, however many splits/merges
    removed -- not just the immediate parent. A lot can have more than one
    ancestor if a merge happened anywhere in its history (parcel_lineage is a
    plain edge list, not a strict tree -- see migration 0029's own
    docstring). Cycle-guarded (a visited-apns array plus a hard depth cap) in
    case lineage data is ever malformed; a genuine ancestor chain should
    never actually need it."""
    rows = session.execute(
        text("""
            WITH RECURSIVE lineage_walk AS (
                SELECT county_fips, apn, parent_county_fips, parent_apn, 1 AS depth,
                       ARRAY[county_fips || ':' || apn] AS visited
                FROM parcel_lineage WHERE county_fips = :cf AND apn = :apn
                UNION ALL
                SELECT pl.county_fips, pl.apn, pl.parent_county_fips, pl.parent_apn, lw.depth + 1,
                       lw.visited || (pl.county_fips || ':' || pl.apn)
                FROM parcel_lineage pl
                JOIN lineage_walk lw ON pl.county_fips = lw.parent_county_fips AND pl.apn = lw.parent_apn
                WHERE NOT (pl.parent_county_fips || ':' || pl.parent_apn) = ANY(lw.visited)
                  AND lw.depth < 50
            )
            SELECT DISTINCT parent_county_fips AS county_fips, parent_apn AS apn FROM lineage_walk
        """),
        {"cf": county_fips, "apn": apn},
    ).fetchall()
    return [(r.county_fips, r.apn) for r in rows]


def generate_payoff_statement(session, county_fips: str, apn: str,
                               requested_by_contact_id: int | None = None,
                               good_through_date: date | None = None) -> list[dict]:
    """One payoff statement per still-outstanding fee obligation found on the
    queried lot itself OR any ancestor reached via parcel_lineage (there can
    be more than one, e.g. two merged tracts each with their own unpaid fee
    history). Returns an empty list if nothing outstanding is found -- not
    an error; most lots owe nothing."""
    good_through = good_through_date or date.today()
    lots_to_check = [(county_fips, apn)] + find_lineage_ancestors(session, county_fips, apn)

    statements = []
    for cf, a in lots_to_check:
        obligations = session.execute(
            text("""
                SELECT fc.instrument_number, fc.recording_date, fc.parcel_apn, fc.collection_seq,
                       fc.base_amount, fc.fee_percent_applied, fc.due_date,
                       t.covid, ct.unpaid_interest_percent
                FROM fee_collection fc
                JOIN transfer t ON t.county_fips = fc.county_fips AND t.instrument_number = fc.instrument_number
                                AND t.recording_date = fc.recording_date AND t.parcel_apn = fc.parcel_apn
                JOIN covenant c ON c.covid = t.covid
                LEFT JOIN covenant_template ct ON ct.template_version_id = c.template_version_id
                WHERE fc.county_fips = :cf AND fc.parcel_apn = :apn
                  AND fc.status = ANY(:statuses) AND fc.collectibility_status = 'collectible'
            """),
            {"cf": cf, "apn": a, "statuses": list(_UNRESOLVED_FEE_STATUSES)},
        ).fetchall()

        for ob in obligations:
            if ob.base_amount is None or ob.unpaid_interest_percent is None:
                # no known price, or no known interest rate for this template -- never
                # guessed, so there is nothing to compute a payoff from for this row.
                continue

            principal = float(ob.base_amount) * float(ob.fee_percent_applied) / 100
            accrual_start = ob.due_date or ob.recording_date
            days_outstanding = max((good_through - accrual_start).days, 0)
            annual_rate_percent = float(ob.unpaid_interest_percent)
            per_diem = principal * (annual_rate_percent / 100) / 365
            accrued_interest = per_diem * days_outstanding
            total = principal + accrued_interest

            source_id = insert_source(
                session, source_type="estimate_derivation",
                reference=(
                    f"app/title/payoff.py: payoff requested for lot ({county_fips}, {apn})"
                    + (f", traced via parcel_lineage to ancestor obligation ({cf}, {a})" if (cf, a) != (county_fips, apn) else "")
                ),
                is_estimated=False, confidence=1.0,
            )

            statement_seq = session.execute(
                text("""
                    SELECT COALESCE(MAX(statement_seq), 0) + 1 AS n FROM fee_payoff_statement
                    WHERE county_fips = :cf AND instrument_number = :inst AND recording_date = :rd
                      AND parcel_apn = :apn AND collection_seq = :seq
                """),
                {"cf": cf, "inst": ob.instrument_number, "rd": ob.recording_date, "apn": a, "seq": ob.collection_seq},
            ).fetchone().n

            session.execute(
                text("""
                    INSERT INTO fee_payoff_statement (
                        county_fips, instrument_number, recording_date, parcel_apn, collection_seq, statement_seq,
                        principal_amount, interest_rate_annual, accrual_start_date, good_through_date,
                        accrued_interest_amount, per_diem_amount, total_payoff_amount,
                        requested_by_contact_id, source_id
                    ) VALUES (
                        :cf, :inst, :rd, :apn, :seq, :stmt_seq,
                        :principal, :rate, :accrual_start, :good_through,
                        :accrued, :per_diem, :total, :requested_by, :source_id
                    )
                """),
                {
                    "cf": cf, "inst": ob.instrument_number, "rd": ob.recording_date, "apn": a, "seq": ob.collection_seq,
                    "stmt_seq": statement_seq, "principal": principal, "rate": annual_rate_percent,
                    "accrual_start": accrual_start, "good_through": good_through,
                    "accrued": accrued_interest, "per_diem": per_diem, "total": total,
                    "requested_by": requested_by_contact_id, "source_id": source_id,
                },
            )

            statements.append({
                "ancestor_county_fips": cf, "ancestor_apn": a,
                "instrument_number": ob.instrument_number, "recording_date": str(ob.recording_date),
                "collection_seq": ob.collection_seq, "statement_seq": statement_seq, "covid": ob.covid,
                "principal_amount": principal, "accrued_interest_amount": accrued_interest,
                "per_diem_amount": per_diem, "total_payoff_amount": total,
                "good_through_date": str(good_through),
            })

    return statements

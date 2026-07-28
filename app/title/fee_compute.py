"""Fee computation (BUILD_SPEC.md Sec. 5/7): given a covid's already-
identified transfer rows (app/title/chain.py), works out what fee -- if
any -- is owed on each non-exempt transfer and writes covenant.fee_collection
rows.

Deliberately a separate module from chain.py, not folded into its own
walk: chain.py decides whether a transfer is exempt or fee-owed
(conservatively defaulting to fee-owed whenever it can't confirm a real
exemption -- see chain.py's own docstring, "never silently assumed either
way"); this step takes that decision at face value and does the
arithmetic. Each is independently callable and re-runnable, the same way
ingestion/GIS-classification/chain-walking are each their own callable
stage rather than auto-cascaded from one another.

Fee AMOUNT requires an ACTUAL disclosed price (transfer.consideration_amount).
Estimating a price for a transfer with no known price (typically a
non-disclosure-state covenant, e.g. every Texas covenant so far) is
explicitly deferred -- a separate project, per stakeholder direction. A
fee-owed transfer with no known price still gets a fee_collection row
(fee_percent_applied is real and known), just with base_amount left null
rather than guessed -- so nothing downstream mistakes "not priced yet" for
"the fee is zero."

due_date: every template read so far (V01, V02, V03, V11, V12, ...)
states the fee is due "contemporaneous with... a Conveyance" -- the
transfer's own recording_date -- unless covenant.fee_due_days states an
actual grace period (rare; null on every covenant extracted this session).

Interest / payoff-statement computation is NOT done here --
covenant.fee_payoff_statement already exists for that (computed fresh at
request time from fee_collection's base_amount + fee_percent_applied +
the covenant_template's own unpaid_interest_percent, never a stale stored
balance); this step only establishes the base obligation a payoff
statement would later reference.
"""
from datetime import date, timedelta

from sqlalchemy import text

from app.db.repository import insert_source, upsert_fee_collection


def _clear_stale_fee_collection(session, county_fips: str, instrument_number: str,
                                 recording_date: date, parcel_apn: str) -> None:
    """A transfer confirmed exempt must have NO fee_collection row -- but an
    earlier computation, before this exemption was recognized (e.g. before
    a later chain.py classification fix), may have left one behind.
    Confirmed real: covid 3297's parcel 93070 had a stale status='owed'
    fee_collection row survive a prior compute_fee_for_transfer call from
    before chain.py stopped forcing review_flag=True on a confirmed
    pre_effective_date exemption just because its chain's declarant-link
    was unconfirmed. Only clears rows with no actual collection activity
    recorded -- never silently removes evidence of a real invoiced/
    collected fee, which would need human handling, not a silent delete."""
    session.execute(
        text("""
            DELETE FROM fee_collection
            WHERE county_fips = :cf AND instrument_number = :inst
              AND recording_date = :rd AND parcel_apn = :apn
              AND invoiced_amount IS NULL AND collected_amount IS NULL
        """),
        {"cf": county_fips, "inst": instrument_number, "rd": recording_date, "apn": parcel_apn},
    )


def compute_fee_for_transfer(session, county_fips: str, instrument_number: str,
                              recording_date: date, parcel_apn: str) -> dict:
    """One transfer -> at most one fee_collection row (collection_seq=1 --
    a later re-invoice/correction would use a higher seq, not handled
    here)."""
    row = session.execute(
        text("""
            SELECT t.covid, t.exemption_category, t.review_flag, t.review_reason,
                   t.consideration_amount, t.consideration_source_id,
                   c.fee_percent, c.fee_due_days, c.template_version_id
            FROM transfer t JOIN covenant c ON c.covid = t.covid
            WHERE t.county_fips = :cf AND t.instrument_number = :inst
              AND t.recording_date = :rd AND t.parcel_apn = :apn
        """),
        {"cf": county_fips, "inst": instrument_number, "rd": recording_date, "apn": parcel_apn},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"no transfer found for ({county_fips}, {instrument_number}, {recording_date}, {parcel_apn})")

    if row.exemption_category is not None and not row.review_flag:
        _clear_stale_fee_collection(session, county_fips, instrument_number, recording_date, parcel_apn)
        return {"fee_owed": False, "reason": f"confirmed exempt ({row.exemption_category})"}

    fee_percent = row.fee_percent
    fee_percent_source = "covenant's own extracted fee_percent"
    if fee_percent is None:
        template_row = session.execute(
            text("SELECT standard_fee_percent FROM covenant_template WHERE template_version_id = :t"),
            {"t": row.template_version_id},
        ).fetchone()
        fee_percent = template_row.standard_fee_percent if template_row else None
        fee_percent_source = "template's standard_fee_percent (covenant's own extraction had none)"
    if fee_percent is None:
        return {"fee_owed": None, "reason": "no fee percent known (neither the covenant's own extraction "
                                             "nor its template's standard_fee_percent) -- can't compute"}

    base_amount = row.consideration_amount  # None if unknown -- never estimated here
    due_date = recording_date + timedelta(days=row.fee_due_days) if row.fee_due_days else recording_date

    notes = None
    if row.review_flag:
        notes = f"underlying transfer classification is unconfirmed: {row.review_reason}"

    # The fee_percent_applied/base_amount are themselves a derivation from already-sourced
    # data (the covenant's own extraction + the transfer's own consideration_amount, whose
    # provenance is transfer.consideration_source_id already) -- is_estimated reflects
    # whether a real dollar figure could actually be derived, not a guessed one.
    source_id = insert_source(
        session, source_type="estimate_derivation",
        reference=f"app/title/fee_compute.py ({fee_percent_source})",
        is_estimated=base_amount is None, confidence=1.0 if base_amount is not None else None,
    )

    upsert_fee_collection(
        session, county_fips=county_fips, instrument_number=instrument_number,
        recording_date=recording_date, parcel_apn=parcel_apn, collection_seq=1,
        fee_percent_applied=fee_percent, base_amount=base_amount,
        due_date=str(due_date), status="owed", notes=notes, source_id=source_id,
    )

    amount_due = float(base_amount) * float(fee_percent) / 100 if base_amount is not None else None
    return {
        "fee_owed": True,
        "fee_percent_applied": float(fee_percent),
        "base_amount": float(base_amount) if base_amount is not None else None,
        "amount_due": amount_due,
        "due_date": str(due_date),
        "needs_review": row.review_flag,
    }


def compute_fees_for_covid(session, covid: int, tract_no: int = 1) -> dict:
    """Every CURRENT transfer row app/title/chain.py has already written for
    this covid/tract -- one result per (county_fips, instrument_number,
    recording_date, parcel_apn). Excludes superseded_at rows (migration
    0031): a re-walk that finds a different chain marks its predecessor's
    rows superseded rather than deleting them (real fee_collection history
    can hang off a transfer row), so this must not recompute a fee against
    a conveyance the walker no longer believes is real."""
    transfers = session.execute(
        text("""
            SELECT county_fips, instrument_number, recording_date, parcel_apn
            FROM transfer WHERE covid = :covid AND tract_no = :tract_no AND superseded_at IS NULL
        """),
        {"covid": covid, "tract_no": tract_no},
    ).fetchall()

    results = {}
    for t in transfers:
        key = f"{t.instrument_number}/{t.parcel_apn}"
        results[key] = compute_fee_for_transfer(
            session, t.county_fips, t.instrument_number, t.recording_date, t.parcel_apn,
        )
    return results

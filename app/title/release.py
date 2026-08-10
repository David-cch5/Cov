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

So a released parcel STAYS in the census and keeps its history. Nothing on or
before the effective date changes: earlier transfers keep their fee_collection
rows, and a fee already taken remains correctly taken, because it was owed when
it was taken.

A RELEASE OPERATES PROSPECTIVELY. Per these covenants' own terms a termination
takes effect after it is recorded, so only transfers recorded strictly AFTER the
effective date are exempt, and effective_date defaults to the recording date
rather than being supplied independently. A transfer recorded the SAME DAY is
left owed and flagged: which instrument came first that day is decided by
recording sequence, which this system does not model.

That is the same shape as the pre_effective_date exemption at the other end of a
covenant's life, which is why this reports through exemption_category rather than
inventing a parallel mechanism: everything downstream that already understands an
exempt transfer -- fee_compute, payoff statements, estoppel certificates --
understands a released one for free.
"""
from datetime import date

from sqlalchemy import text

RELEASE_TYPES = ("termination", "buyout")
EFFECTS = ("prospective", "void_ab_initio")
SCOPES = ("covenant", "partial")

# Which exemption a release produces. Kept distinct rather than collapsed into
# one "released" code: an estoppel or payoff statement has to say WHY the
# obligation ended, and a bought-out parcel is not the same fact as a terminated
# one.
EXEMPTION_FOR_TYPE = {"termination": "post_termination", "buyout": "post_buyout"}


def record_release(
    session, covid: int, release_type: str, effective_date: date | None = None,
    scope: str = "covenant", parcels: list[tuple[str, str]] | None = None,
    recording_instrument: str | None = None, recording_date: date | None = None,
    consideration_amount: float | None = None, notes: str | None = None,
    source_id: int | None = None, retroactive_basis: str | None = None,
    effect: str = "prospective", settles_prior_fees: bool = False,
    settlement_note: str | None = None, execution_date: date | None = None,
    acknowledgement_required: bool = False, acknowledged_date: date | None = None,
    no_intervening_conveyance_affidavit: bool = False,
    terminates_instrument: str | None = None,
    referenced_instruments: list[str] | None = None,
    terminated_under: str | None = None,
) -> dict:
    """Record a termination or buyout. `parcels` is [(county_fips, apn), ...]
    and is required for scope='partial'.

    TWO EFFECTS, BOTH REAL AND BOTH RECORDED (see migration 0038):

      effect='prospective'     ends the obligation from effective_date forward.
                               effective_date defaults to recording_date, per
                               these covenants' own terms, and cannot precede it.
      effect='void_ab_initio'  the covenant is void "as if it had never been
                               recorded" -- it reaches back to inception, so
                               effective_date is not what governs and every
                               transfer is released.

    A void_ab_initio release is licensed by the sworn statement these instruments
    carry that nothing was conveyed since the covenant was filed -- no intervening
    conveyance, so no accrued fee to void. Pass
    no_intervening_conveyance_affidavit=True when the instrument contains it.
    Without it, release_for_transfer still reports the release but marks
    needs_review, because voiding a fee that was actually collected is not
    something to do silently.

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

    if settles_prior_fees and release_type != "buyout":
        raise ValueError(
            "only a buyout can settle fees accrued before it -- a termination pays for "
            "nothing. A validly terminated covenant had no prior sales (which is what these "
            "instruments swear to), so there are no accrued fees for it to settle; if prior "
            "fees exist, that is a conflict to resolve, not something to mark settled"
        )
    if effect not in EFFECTS:
        raise ValueError(f"effect must be one of {EFFECTS}, got {effect!r}")
    if effect == "void_ab_initio" and not (no_intervening_conveyance_affidavit or retroactive_basis):
        raise ValueError(
            "a void_ab_initio release reaches back to the covenant's inception, so it needs "
            "either no_intervening_conveyance_affidavit=True (the sworn statement these "
            "instruments carry that nothing was conveyed since filing, which is what makes "
            "reaching back safe) or retroactive_basis quoting the language relied on"
        )

    if effective_date is None:
        if recording_date is None:
            raise ValueError("a release needs either an effective_date or a recording_date "
                             "to derive one from -- it cannot take effect on no date at all")
        effective_date = recording_date
    # Reaching back has exactly ONE expression: effect='void_ab_initio'. A
    # "prospective" release dated before its own recording is a contradiction, not
    # a variant, and the schema refuses it too (migration 0038's CHECK) -- so there
    # is no second path here that could disagree with the database.
    if effect == "prospective" and recording_date is not None and effective_date < recording_date:
        raise ValueError(
            f"effective_date {effective_date} precedes recording_date {recording_date} on a "
            f"PROSPECTIVE release. If the instrument really reaches back, record it as "
            f"effect='void_ab_initio'; if it does not, this date is wrong. Fees already owed "
            f"must not be voided by an unexplained date"
        )
    if retroactive_basis:
        notes = f"RETROACTIVE BASIS: {retroactive_basis}" + (f" | {notes}" if notes else "")

    release_id = session.execute(
        text("""
            INSERT INTO covenant_release (covid, release_type, scope, effective_date,
                                          recording_instrument, recording_date,
                                          consideration_amount, notes, source_id,
                                          effect, execution_date, acknowledgement_required,
                                          acknowledged_date,
                                          no_intervening_conveyance_affidavit,
                                          terminates_instrument, referenced_instruments,
                                          terminated_under, settles_prior_fees,
                                          settlement_note)
            VALUES (:covid, :release_type, :scope, :effective_date, :instrument,
                    :recording_date, :consideration, :notes, :source_id,
                    :effect, :execution_date, :ack_required, :ack_date, :affidavit,
                    :terminates, :referenced, :under, :settles, :settlement_note)
            RETURNING release_id
        """),
        {"covid": covid, "release_type": release_type, "scope": scope,
         "effective_date": effective_date, "instrument": recording_instrument,
         "recording_date": recording_date, "consideration": consideration_amount,
         "notes": notes, "source_id": source_id, "effect": effect,
         "execution_date": execution_date, "ack_required": acknowledgement_required,
         "ack_date": acknowledged_date, "affidavit": no_intervening_conveyance_affidavit,
         "terminates": terminates_instrument, "referenced": referenced_instruments,
         "under": terminated_under, "settles": settles_prior_fees,
         "settlement_note": settlement_note},
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
            "scope": scope, "effect": effect, "effective_date": effective_date,
            "parcels": len(parcels or [])}


def release_for_transfer(session, covid: int, county_fips: str, apn: str,
                         recording_date: date) -> dict | None:
    """The release that exempts this transfer, or None.

    A transfer is released only when it is recorded strictly AFTER the effective
    date and its parcel is in scope, because these covenants terminate
    prospectively. Earlier transfers are deliberately NOT matched -- that is the
    whole point: the fee was owed when it was taken, and a later release does not
    claw it back.

    A transfer recorded on the SAME DAY as the release is not auto-released. Which
    came first is decided by recording sequence within that day, which this system
    does not model, and the conservative direction is to leave the fee owed and
    let a human read the instrument numbers. Such a transfer comes back with
    same_day=True and needs_review=True rather than silently either way.

    Where several releases could apply, the EARLIEST effective one wins: the
    obligation ended the first time it ended, and a subsequent instrument cannot
    make it end later.
    """
    row = session.execute(
        text("""
            SELECT r.release_id, r.release_type, r.scope, r.effect, r.effective_date,
                   r.recording_instrument, r.consideration_amount,
                   r.acknowledgement_required, r.acknowledged_date,
                   r.no_intervening_conveyance_affidavit
            FROM covenant_release r
            WHERE r.covid = :covid
              -- void_ab_initio reaches the covenant's whole life, so no date test
              AND (r.effect = 'void_ab_initio' OR r.effective_date <= :recording_date)
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
    void_ab_initio = row["effect"] == "void_ab_initio"
    # Same-day only arises for a prospective release; a void one does not turn on
    # the date at all.
    same_day = (not void_ab_initio) and row["effective_date"] == recording_date

    # Two things make a release report but not silently apply. A retroactive
    # release lacking the sworn no-conveyance statement may be voiding a fee that
    # was really collected. And a termination that is only "fully effective" once
    # the Trustee acknowledges it is not effective while that acknowledgement is
    # unexecuted -- the Williamson County instrument on hand is in exactly that
    # state, blank day and no signature.
    unsworn_retroactive = void_ab_initio and not row["no_intervening_conveyance_affidavit"]
    acknowledgement_pending = bool(row["acknowledgement_required"]) and row["acknowledged_date"] is None

    reasons = []
    if same_day:
        reasons.append("transfer recorded the same day as the release -- recording sequence decides")
    if unsworn_retroactive:
        reasons.append("void_ab_initio without the sworn no-intervening-conveyance statement")
    if acknowledgement_pending:
        reasons.append("acknowledgement required by the instrument is unexecuted")

    return {**dict(row),
            "exemption_category": EXEMPTION_FOR_TYPE[row["release_type"]],
            "void_ab_initio": void_ab_initio,
            "same_day": same_day,
            "needs_review": bool(reasons),
            "review_reasons": reasons,
            "releases_transfer": not reasons}


def settle_prior_fees(session, release_id: int) -> dict:
    """Discharge the fees a buyout's consideration covered, by LINKING them to it.

    Nothing is deleted and no amount is rewritten. Each affected fee_collection row
    keeps its base_amount, its fee_percent_applied and what it was owed on, and
    gains settled_by_release_id plus status='paid' -- because it was paid, as part
    of a larger consideration, not forgone. status='waived' would say the opposite.

    Only rows with no collection activity of their own are touched. A fee already
    invoiced or partly collected has a payment history that a buyout cannot silently
    absorb; those are returned as `conflicts` for a human to reconcile against the
    agreement.

    Refuses unless the release is a buyout with settles_prior_fees set, because
    whether the consideration covered prior fees is a term of the agreement and
    cannot be inferred from the instrument's existence.
    """
    release = session.execute(
        text("""SELECT release_id, covid, release_type, settles_prior_fees, effective_date, scope
                FROM covenant_release WHERE release_id = :rid"""),
        {"rid": release_id},
    ).mappings().first()
    if release is None:
        raise ValueError(f"no covenant_release {release_id}")
    if release["release_type"] != "buyout" or not release["settles_prior_fees"]:
        raise ValueError(
            f"release {release_id} is a {release['release_type']} with "
            f"settles_prior_fees={release['settles_prior_fees']} -- only a buyout whose "
            f"agreement covered prior fees can settle them"
        )

    scoped = """
        AND (:scope = 'covenant'
             OR EXISTS (SELECT 1 FROM covenant_release_parcel p
                        WHERE p.release_id = :rid
                          AND p.county_fips = fc.county_fips AND p.apn = fc.parcel_apn))
    """
    conflicts = session.execute(
        text(f"""
            SELECT fc.county_fips, fc.instrument_number, fc.recording_date, fc.parcel_apn,
                   fc.status, fc.invoiced_amount, fc.collected_amount
            FROM fee_collection fc
            JOIN transfer t ON t.county_fips = fc.county_fips
                           AND t.instrument_number = fc.instrument_number
                           AND t.recording_date = fc.recording_date
                           AND t.parcel_apn = fc.parcel_apn
            WHERE t.covid = :covid AND fc.recording_date < :effective
              AND fc.settled_by_release_id IS NULL
              AND (fc.invoiced_amount IS NOT NULL OR fc.collected_amount IS NOT NULL)
              {scoped}
        """),
        {"covid": release["covid"], "effective": release["effective_date"],
         "rid": release_id, "scope": release["scope"]},
    ).mappings().all()

    settled = session.execute(
        text(f"""
            UPDATE fee_collection fc
            SET settled_by_release_id = :rid, status = 'paid'
            WHERE fc.settled_by_release_id IS NULL
              AND fc.invoiced_amount IS NULL AND fc.collected_amount IS NULL
              AND fc.recording_date < :effective
              AND EXISTS (SELECT 1 FROM transfer t
                          WHERE t.county_fips = fc.county_fips
                            AND t.instrument_number = fc.instrument_number
                            AND t.recording_date = fc.recording_date
                            AND t.parcel_apn = fc.parcel_apn
                            AND t.covid = :covid)
              {scoped}
            RETURNING fc.instrument_number
        """),
        {"covid": release["covid"], "effective": release["effective_date"],
         "rid": release_id, "scope": release["scope"]},
    ).fetchall()
    return {"release_id": release_id, "settled": len(settled),
            "conflicts": [dict(c) for c in conflicts]}


def termination_fee_conflicts(session, covid: int) -> list[dict]:
    """Fees accrued before a TERMINATION, which should not exist.

    A validly terminated covenant had no prior sales -- the instruments say so
    under oath. So a fee_collection row predating a termination means either the
    termination is not valid as to that land or the fee record is wrong. Either way
    it is a real conflict, and reporting it is the only honest thing to do with it:
    a termination pays for nothing, so it cannot settle these.
    """
    rows = session.execute(
        text("""
            SELECT r.release_id, r.recording_instrument, r.effective_date, r.effect,
                   fc.county_fips, fc.parcel_apn, fc.instrument_number, fc.recording_date,
                   fc.status, fc.base_amount, fc.collected_amount
            FROM covenant_release r
            JOIN transfer t ON t.covid = r.covid
            JOIN fee_collection fc ON fc.county_fips = t.county_fips
                                  AND fc.instrument_number = t.instrument_number
                                  AND fc.recording_date = t.recording_date
                                  AND fc.parcel_apn = t.parcel_apn
            WHERE r.covid = :covid AND r.release_type = 'termination'
              AND t.recording_date < r.effective_date
              AND (r.scope = 'covenant'
                   OR EXISTS (SELECT 1 FROM covenant_release_parcel p
                              WHERE p.release_id = r.release_id
                                AND p.county_fips = t.parcel_county_fips
                                AND p.apn = t.parcel_apn))
            ORDER BY fc.recording_date
        """),
        {"covid": covid},
    ).mappings().all()
    return [dict(r) for r in rows]


def is_fully_released(session, covid: int, as_of: date | None = None) -> dict | None:
    """The release that has ended this covenant ENTIRELY, or None.

    A fully released covenant is HISTORIC. It is worth recording -- the record is
    why anyone can later show the land was once encumbered and no longer is -- but
    it is not worth researching. No chain-of-title walk, no GIS anchoring, no LLM
    escalation, because every one of those costs money to establish facts about an
    obligation that no longer exists. The expensive entrypoints check this and skip
    by default; a caller who genuinely wants the work done passes an explicit
    override rather than getting it by accident.

    Only scope='covenant' counts. A partial release, however large, leaves land
    still encumbered and the covenant still worth working.

    A release that needs review does NOT make a covenant historic: an unexecuted
    acknowledgement or a retroactive release with no sworn no-conveyance statement
    is exactly the situation where the covenant might still be live, and skipping
    research on that basis would be assuming the answer.
    """
    row = session.execute(
        text("""
            SELECT release_id, release_type, effect, scope, effective_date,
                   recording_instrument, recording_date,
                   acknowledgement_required, acknowledged_date,
                   no_intervening_conveyance_affidavit
            FROM covenant_release
            WHERE covid = :covid AND scope = 'covenant'
              AND (:as_of IS NULL OR effective_date <= :as_of)
              AND (NOT acknowledgement_required OR acknowledged_date IS NOT NULL)
              AND (effect = 'prospective' OR no_intervening_conveyance_affidavit)
            ORDER BY effective_date, release_id
            LIMIT 1
        """),
        {"covid": covid, "as_of": as_of},
    ).mappings().first()
    return dict(row) if row else None


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

    Same-day transfers are deliberately NOT stamped here, for the reason
    release_for_transfer gives: recording sequence decides, and this settles it
    the conservative way by leaving the fee owed for a human to resolve.
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
              AND (r.effect = 'void_ab_initio'
                   OR r.effective_date < t.recording_date)  -- strictly after; see release_for_transfer
              AND (r.effect = 'prospective' OR r.no_intervening_conveyance_affidavit)
              AND (NOT r.acknowledgement_required OR r.acknowledged_date IS NOT NULL)
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

"""When each parcel was formed, read from the instrument that formed it.

A parcel is not a thing that has always existed. A plat subdivides raw acreage
into lots on a recorded date; a deed splits a tract on a recorded date. That date
is the parcel's formation, and it is the axis every "what did this land look like
then" question turns on -- including whether a covenant's fee applied to a lot
that did not yet exist.

READ, NEVER INFERRED. The only formation date this module will write is one
standing on a recorded instrument. It will not fall back to a parcel's first-seen
date, its appraisal year, or the covenant's own recording date. Roughly 3,000 of
this project's parcels are raw abstract-survey tracts with no formation event to
read, and their formed_date stays NULL -- which is the correct answer, not a gap
to fill. A fabricated formation date sitting under a fee calculation is the
expensive kind of wrong.

The existing covid 4440 platting map is the standard this follows: a lot flips
from raw to platted at its own real recorded plat date, and a parcel with no real
date never flips no matter where the time slider sits.
"""
from sqlalchemy import text


def derive_formation_from_plats(session, county_fips: str | None = None) -> dict:
    """Populate parcel formation from the plat each parcel already belongs to.

    Deterministic and free: parcel.plat_id is set by the classifier when a
    parcel's own recited legal description matches a plat's subdivision and
    section, and that plat carries its real recording_date and
    recording_instrument from the county clerk's own index. No new lookup, no
    inference -- just reading a citation the database already holds.

    Only plats whose lookup actually succeeded count. A `lookup_status
    <> 'found'` row records that a subdivision was searched for and NOT located,
    which is evidence of absence, not a date.
    """
    result = session.execute(
        text("""
            UPDATE parcel p
               SET formed_date = pl.recording_date,
                   formed_by_instrument = pl.recording_instrument,
                   formation_source = 'plat'
              FROM plat pl
             WHERE p.plat_id = pl.plat_id
               AND pl.lookup_status = 'found'
               AND pl.recording_date IS NOT NULL
               AND pl.recording_instrument IS NOT NULL
               AND (:county_fips IS NULL OR p.county_fips = :county_fips)
               -- Idempotent: only write where it would actually change something,
               -- so a re-run is free and does not churn updated_at.
               AND (p.formed_date IS DISTINCT FROM pl.recording_date
                    OR p.formed_by_instrument IS DISTINCT FROM pl.recording_instrument
                    OR p.formation_source IS DISTINCT FROM 'plat')
        """),
        {"county_fips": county_fips},
    )
    written = result.rowcount

    counts = session.execute(
        text("""
            SELECT count(*) FILTER (WHERE formed_date IS NOT NULL) AS formed,
                   count(*) FILTER (WHERE formed_date IS NULL AND plat_id IS NOT NULL) AS platted_no_date,
                   count(*) FILTER (WHERE plat_id IS NULL) AS no_plat,
                   count(*) AS total
              FROM parcel
             WHERE (:county_fips IS NULL OR county_fips = :county_fips)
        """),
        {"county_fips": county_fips},
    ).fetchone()
    return {
        "written": written, "formed": counts.formed,
        "platted_but_no_plat_date": counts.platted_no_date,
        "no_plat_reference": counts.no_plat, "total": counts.total,
    }


def formation_timeline(session, covid: int, tract_no: int | None = None) -> list[dict]:
    """Every formation event for a covenant's own encumbered parcels, in date
    order -- the series a time-slider map animates.

    Excludes parcels a human has excluded from the census
    (parcel_covenant_exclusion), because a digitization sliver appearing on a
    timeline is noise that looks like land.
    """
    rows = session.execute(
        text("""
            SELECT p.formed_date, p.formed_by_instrument, p.formation_source,
                   pl.subdivision_name, pl.section, count(*) AS parcels,
                   round(sum(p.acreage)::numeric, 3) AS acreage
              FROM parcel_covenant pc
              JOIN parcel p USING (county_fips, apn)
              LEFT JOIN plat pl ON pl.plat_id = p.plat_id
             WHERE pc.covid = :covid
               AND (:tract_no IS NULL OR pc.tract_no = :tract_no)
               AND pc.run_seq = (SELECT max(run_seq) FROM parcel_covenant
                                  WHERE covid = :covid AND tract_no = pc.tract_no)
               AND NOT EXISTS (SELECT 1 FROM parcel_covenant_exclusion x
                                WHERE x.county_fips = pc.county_fips AND x.apn = pc.apn
                                  AND x.covid = pc.covid AND x.tract_no = pc.tract_no)
               AND p.formed_date IS NOT NULL
             GROUP BY 1, 2, 3, 4, 5
             ORDER BY p.formed_date, pl.subdivision_name, pl.section
        """),
        {"covid": covid, "tract_no": tract_no},
    ).fetchall()
    return [dict(r._mapping) for r in rows]


# Sentinel dates a county index uses to mean "unknown", and the floor below which no
# Texas subdivision plat exists. Duplicated from app/gis/plat_tracking.py on purpose:
# that module guards them at PARSE time, and this one checks what actually landed in
# the table, whichever path wrote it. A parse-time guard cannot see a row written by
# resolve_subdivision_plat_tract, and that is where two of the three real errors came
# from.
_SENTINEL_DATES = ("1800-01-01", "1900-01-01", "1899-12-31")
_EARLIEST_PLAUSIBLE_YEAR = 1850

# How far before its subdivision's own earliest filing a lot's date may sit before it
# is a finding. Zero would flag ordinary noise: a subdivision's first section and a
# same-day amending plat can differ by a day in how a county indexes them.
_EARLY_TOLERANCE_DAYS = 2


def check_formation_date_plausibility(session, covid: int | None = None) -> dict:
    """Report formation dates that cannot be true. Changes nothing.

    WHY THIS EXISTS. Three wrong plat dates reached this database and every one was
    caught only because a human noticed the YEAR looked wrong -- Nueces' 1/1/1800
    sentinel, Collin's 1/1/1900 on a subdivision platted in 2004, and doc 46201's
    2008-10-16 sitting on 11 lots of a subdivision whose earliest real filing is 2013.
    The last one survived a correction pass because I fixed one row and never looked
    for a duplicate. A formation date is what a fee accrues from, so "somebody will
    notice" is not a control.

    Four findings, each one a real error class already seen:

      sentinel_date     an index's placeholder written as a date. Cheap to check and
                        the only one already guarded upstream.
      before_subdivision  a lot dated before its own subdivision's earliest recorded
                        plat. This is what caught unit 4A: land cannot be platted into
                        a subdivision that did not yet exist.
      conveyed_before_formed  a parcel with a recorded transfer PREDATING its formation
                        date. A lot cannot be sold before the plat created it, so
                        something is misplaced -- and it is not always the date. The
                        first two this check found (Collin 2766013/2766016, platted
                        2017-10-03, transfers dated 2011-01-04) are PRE-PLAT
                        conveyances of the ancestor tract, recorded against the lot's
                        own APN because that is the only place chain.py had to put
                        them. Those belong on an ancestor node of the tract spine
                        (app/title/tract_spine.py), and once recorded there the
                        transfer is EXPLAINED and drops out of this finding. What is
                        left is genuinely unaccounted for: either the date is wrong or
                        the chain is attached to the wrong parcel.
      future_date       a formation date after today.

    Deliberately no auto-repair. Each of these has more than one possible cause (the
    wrong plat matched, the right plat mis-dated, the chain attached to the wrong
    parcel), and picking one would be the same guess that produced the errors.
    """
    scope = "AND EXISTS (SELECT 1 FROM parcel_covenant pc WHERE pc.county_fips = p.county_fips " \
            "AND pc.apn = p.apn AND pc.covid = :covid)" if covid is not None else ""
    params = {"covid": covid} if covid is not None else {}

    sentinel = session.execute(text(f"""
        SELECT p.county_fips, p.apn, p.formed_date, p.formed_by_instrument
          FROM parcel p
         WHERE p.formed_date IS NOT NULL {scope}
           AND (p.formed_date IN ('{"','".join(_SENTINEL_DATES)}')
                OR EXTRACT(YEAR FROM p.formed_date) < {_EARLIEST_PLAUSIBLE_YEAR})
         ORDER BY p.formed_date LIMIT 200
    """), params).fetchall()

    # A subdivision's earliest filing, over every plat row this project holds for it,
    # compared on the same normalised name plat_link matches with -- so "HEIGHTS AT
    # WESTRIDGE" and "HEIGHTS WESTRIDGE" are one subdivision, not two.
    before_sub = session.execute(text(f"""
        WITH earliest AS (
            SELECT county_fips, subdivision_name, min(recording_date) AS first_filing
              FROM plat
             WHERE lookup_status = 'found' AND recording_date IS NOT NULL
             GROUP BY county_fips, subdivision_name
        )
        SELECT p.county_fips, p.apn, p.formed_date, p.formed_by_instrument,
               pl.subdivision_name, e.first_filing,
               (e.first_filing - p.formed_date) AS days_early
          FROM parcel p
          JOIN plat pl ON pl.plat_id = p.plat_id
          JOIN earliest e ON e.county_fips = pl.county_fips
                         AND e.subdivision_name = pl.subdivision_name
         WHERE p.formed_date IS NOT NULL {scope}
           AND p.formed_date < e.first_filing - {_EARLY_TOLERANCE_DAYS}
         ORDER BY (e.first_filing - p.formed_date) DESC LIMIT 200
    """), params).fetchall()

    # A PRE-PLAT CONVEYANCE STOPS BEING A FINDING ONCE THE SPINE EXPLAINS IT. The lot
    # genuinely did not exist in 2011; the deed conveyed the ANCESTOR tract. Recording
    # that ancestry (app/title/tract_spine.py record_split) is the fix, and this query
    # recognises it: a transfer whose instrument appears on any ancestor of the
    # parcel's own spine node is accounted for. Anything left is still unexplained --
    # either the date is wrong or the chain is on the wrong parcel.
    conveyed_early = session.execute(text(f"""
        WITH RECURSIVE ancestry AS (
            SELECT n.node_id AS leaf, n.county_fips, n.apn, n.parent_node_id,
                   n.split_instrument_number, 0 AS depth
              FROM tract_node n WHERE n.apn IS NOT NULL
            UNION ALL
            SELECT a.leaf, a.county_fips, a.apn, p.parent_node_id,
                   p.split_instrument_number, a.depth + 1
              FROM ancestry a JOIN tract_node p ON p.node_id = a.parent_node_id
             WHERE a.depth < 40
        )
        SELECT p.county_fips, p.apn, p.formed_date, p.formed_by_instrument,
               min(t.recording_date) AS earliest_transfer,
               count(*) AS transfers_before
          FROM parcel p
          JOIN transfer t ON t.parcel_county_fips = p.county_fips AND t.parcel_apn = p.apn
         WHERE p.formed_date IS NOT NULL AND t.recording_date < p.formed_date {scope}
           AND NOT EXISTS (
               SELECT 1 FROM ancestry a
                WHERE a.county_fips = p.county_fips AND a.apn = p.apn
                  AND a.split_instrument_number = t.instrument_number)
         GROUP BY 1, 2, 3, 4
         ORDER BY min(t.recording_date) LIMIT 200
    """), params).fetchall()

    future = session.execute(text(f"""
        SELECT p.county_fips, p.apn, p.formed_date, p.formed_by_instrument
          FROM parcel p
         WHERE p.formed_date > current_date {scope}
         ORDER BY p.formed_date DESC LIMIT 200
    """), params).fetchall()

    def as_dicts(rows, cause=None):
        out = []
        for r in rows:
            row = dict(r._mapping)
            if cause:
                row["check_first"] = cause
            out.append(row)
        return out
    findings = {
        "sentinel_date": as_dicts(sentinel),
        "before_subdivision": as_dicts(before_sub),
        "conveyed_before_formed": as_dicts(
            conveyed_early,
            "whether these transfers are PRE-PLAT conveyances of the ancestor tract "
            "attributed to this lot's APN -- if so the date is right and the transfers "
            "belong on a tract_spine ancestor node, not on this parcel"),
        "future_date": as_dicts(future),
    }
    total = sum(len(v) for v in findings.values())
    return {"covid": covid, "checked": session.execute(text(
                f"SELECT count(*) FROM parcel p WHERE p.formed_date IS NOT NULL {scope}"),
                params).scalar(),
            "implausible": total, "plausible": total == 0, **findings}

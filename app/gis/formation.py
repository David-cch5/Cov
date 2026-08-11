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

"""Monitor a metes-and-bounds tract's own residual (unaccounted) acreage for a
new plat -- CLAUDE.md's own description of this system: "...and monitor
remaining raw acreage for new plats." Only meaningful for metes_and_bounds_
traverse tracts: a current_parcel_match tract's geom IS the union of its own
matched parcels by construction, so it has no independent residual to watch
(residual_geom stays NULL for it, always).

Distinct from classify_metes_and_bounds_tract's initial pass: this only looks
for parcels NOT already linked to the tract (parcel_covenant), classifies just
those, and re-derives residual_geom/classified_acreage from the full,
cumulative set of matched parcels across every run (old + newly found). Each
newly-found parcel that already existed in the parcel table (matched to some
other tract, e.g. an adjoining covenant) gets a parcel_history snapshot of
its pre-overwrite state (change_reason='monitor_diff') -- both monitor_run's
residual_acreage_before/after and parcel_history's 'monitor_diff' reason have
existed in the schema since migration 0001 but were never written until now.

Detecting a replat of an ALREADY-classified interior/boundary parcel -- its
own shape changing, as opposed to a brand-new parcel appearing on raw acreage
-- is a related but separate concern, not attempted here.
"""
from sqlalchemy import text

from app.db.repository import insert_source, upsert_parcel
from app.gis.classifier import COUNTY_ADAPTERS
from app.queue.job_queue import run_with_job_queue

_VALID_RUN_TYPES = ("scheduled", "manual")


def monitor_tract_for_new_plats(session, covid: int, tract_no: int = 1, run_type: str = "manual") -> dict:
    if run_type not in _VALID_RUN_TYPES:
        raise ValueError(f"run_type must be one of {_VALID_RUN_TYPES}, got {run_type!r}")

    row = session.execute(
        text("""
            SELECT c.county_fips, t.boundary_resolution_method,
                   ST_XMin(t.geom) AS xmin, ST_YMin(t.geom) AS ymin,
                   ST_XMax(t.geom) AS xmax, ST_YMax(t.geom) AS ymax,
                   ST_Area(t.residual_geom::geography) / 4046.8564224 AS residual_acreage_before
            FROM tract t JOIN covenant c ON c.covid = t.covid
            WHERE t.covid = :covid AND t.tract_no = :tract_no
        """), {"covid": covid, "tract_no": tract_no},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} tract {tract_no} not found")
    if row.boundary_resolution_method != "metes_and_bounds_traverse":
        raise RuntimeError(
            f"covid {covid} tract {tract_no} is {row.boundary_resolution_method!r} -- only "
            f"metes_and_bounds_traverse tracts have an independent residual to monitor"
        )

    county_fips = row.county_fips
    already_classified = {
        r.apn for r in session.execute(
            text("SELECT DISTINCT apn FROM parcel_covenant WHERE covid = :covid AND tract_no = :tract_no"),
            {"covid": covid, "tract_no": tract_no},
        ).fetchall()
    }
    if not already_classified:
        raise RuntimeError(
            f"covid {covid} tract {tract_no} has never been classified -- run "
            f"classify_metes_and_bounds_tract first"
        )

    adapter = COUNTY_ADAPTERS.get(county_fips)
    if adapter is None:
        raise RuntimeError(f"no GIS adapter registered for county_fips={county_fips}")

    envelope = {"xmin": row.xmin, "ymin": row.ymin, "xmax": row.xmax, "ymax": row.ymax,
                "spatialReference": {"wkid": 4326}}
    # Same bbox-prefilter-then-true-polygon-test discipline as classify_metes_and_bounds_
    # tract's initial pass -- a new candidate here is only ever confirmed a real match by
    # the ST_Contains/ST_Intersects query below, never by bbox membership alone.
    candidates = run_with_job_queue(
        lambda: list(adapter.iter_parcels(geometry=envelope)),
        job_type="gis_monitor_spatial_query", county_fips=county_fips, covid=covid,
        payload={"adapter": adapter.__name__, "base_url": adapter.BASE_URL, "envelope": envelope, "run_type": run_type},
    )
    new_candidates = [p for p in candidates if p["apn"] not in already_classified]

    run_seq = session.execute(
        text("SELECT COALESCE(MAX(run_seq), 0) + 1 AS n FROM monitor_run WHERE covid = :covid"),
        {"covid": covid},
    ).fetchone().n
    before = float(row.residual_acreage_before or 0)

    if not new_candidates:
        session.execute(
            text("""
                INSERT INTO monitor_run (covid, run_seq, run_type, new_parcels_found,
                                          residual_acreage_before, residual_acreage_after, status)
                VALUES (:covid, :run_seq, :run_type, 0, :before, :before, 'ok')
            """),
            {"covid": covid, "run_seq": run_seq, "run_type": run_type, "before": before},
        )
        return {"new_parcels_found": 0, "run_seq": run_seq,
                "residual_acreage_before": before, "residual_acreage_after": before}

    gis_source_id = insert_source(
        session, source_type="gis_api", reference=f"{adapter.BASE_URL} (monitoring re-check)", confidence=None,
    )

    # Capture pre-existence in `parcel` BEFORE upsert_parcel overwrites anything in place --
    # but which of these matter is only known once the true spatial test below runs. Most
    # bbox candidates never actually intersect this tract at all (that's expected -- the bbox
    # is only a network-efficiency prefilter, same discipline as classify_metes_and_bounds_
    # tract's own initial pass), so writing parcel_history for every bbox neighbor already on
    # file would be noise, not signal -- restricted below to genuinely tract-matched apns only.
    existing_apns = {
        r.apn for r in session.execute(
            text("SELECT apn FROM parcel WHERE county_fips = :county_fips AND apn = ANY(:apns)"),
            {"county_fips": county_fips, "apns": [p["apn"] for p in new_candidates]},
        ).fetchall()
    }
    for p in new_candidates:
        upsert_parcel(
            session, county_fips=p["county_fips"], apn=p["apn"], owner_name_raw=p["owner_name_raw"],
            situs_address=p["situs_address"], city=p.get("city"), zip_code=p.get("zip_code"),
            acreage=p["acreage"], geojson=p["geojson"], source_id=gis_source_id,
        )

    matched = session.execute(
        text("""
            SELECT p.apn,
                   ST_Contains(t.geom, p.geom) AS is_interior,
                   ST_Area(ST_Intersection(t.geom, p.geom)::geography)
                       / NULLIF(ST_Area(p.geom::geography), 0) AS overlap_fraction
            FROM parcel p, tract t
            WHERE p.county_fips = :county_fips AND p.apn = ANY(:apns)
              AND t.covid = :covid AND t.tract_no = :tract_no
              AND ST_Intersects(t.geom, p.geom)
        """),
        {"county_fips": county_fips, "apns": [p["apn"] for p in new_candidates], "covid": covid, "tract_no": tract_no},
    ).fetchall()

    # Only a parcel that's genuinely relevant to THIS tract (passed the true spatial test
    # above) AND was already on file before this re-check (e.g. matched to a different,
    # adjoining tract) gets a parcel_history snapshot -- its parcel-table row as of just
    # before this run's own upsert_parcel call overwrote it above.
    for m in matched:
        if m.apn in existing_apns:
            session.execute(
                text("""
                    INSERT INTO parcel_history (county_fips, apn, owner_name_raw, acreage, geom, change_reason, source_id)
                    SELECT county_fips, apn, owner_name_raw, acreage, geom, 'monitor_diff', source_id
                    FROM parcel WHERE county_fips = :county_fips AND apn = :apn
                """),
                {"county_fips": county_fips, "apn": m.apn},
            )

    session.execute(
        text("""
            INSERT INTO monitor_run (covid, run_seq, run_type, new_parcels_found, residual_acreage_before, status)
            VALUES (:covid, :run_seq, :run_type, :n, :before, 'ok')
        """),
        {"covid": covid, "run_seq": run_seq, "run_type": run_type, "n": len(matched), "before": before},
    )

    for m in matched:
        classification = "interior" if m.is_interior else "boundary"
        confidence = 1.0 if m.is_interior else float(m.overlap_fraction or 0)
        session.execute(
            text("""
                INSERT INTO parcel_covenant (county_fips, apn, covid, tract_no, run_seq, classification,
                                              overlap_fraction, confidence, rationale)
                VALUES (:county_fips, :apn, :covid, :tract_no, :run_seq, :classification,
                        :overlap_fraction, :confidence, :rationale)
                ON CONFLICT (county_fips, apn, covid, tract_no, run_seq) DO NOTHING
            """),
            {
                "county_fips": county_fips, "apn": m.apn, "covid": covid, "tract_no": tract_no,
                "run_seq": run_seq, "classification": classification,
                "overlap_fraction": float(m.overlap_fraction) if m.overlap_fraction is not None else None,
                "confidence": confidence,
                "rationale": (
                    f"parcel geometry {'fully within' if m.is_interior else 'partially overlaps'} the "
                    f"tract's own metes-and-bounds polygon -- found by a monitoring re-check, not the "
                    f"tract's initial classification"
                ),
            },
        )

    # Same real-residual recomputation as classify_metes_and_bounds_tract, but against the
    # FULL cumulative parcel_covenant set for this tract (every run_seq, not just this one) --
    # a monitoring pass only adds parcels, it never re-derives from scratch.
    session.execute(
        text("""
            WITH matched_union AS (
                SELECT ST_Union(p.geom) AS geom
                FROM parcel p
                JOIN parcel_covenant pc ON pc.county_fips = p.county_fips AND pc.apn = p.apn
                WHERE pc.covid = :covid AND pc.tract_no = :tract_no
            )
            UPDATE tract SET
                residual_geom = ST_Multi(ST_Difference(tract.geom, matched_union.geom)),
                classified_acreage = (
                    ST_Area(tract.geom::geography) - ST_Area(ST_Difference(tract.geom, matched_union.geom)::geography)
                ) / 4046.8564224,
                source_id = :source_id, updated_at = now()
            FROM matched_union
            WHERE tract.covid = :covid AND tract.tract_no = :tract_no
        """),
        {"covid": covid, "tract_no": tract_no, "source_id": gis_source_id},
    )

    residual_after = session.execute(
        text("SELECT ST_Area(residual_geom::geography) / 4046.8564224 AS r FROM tract WHERE covid = :covid AND tract_no = :tract_no"),
        {"covid": covid, "tract_no": tract_no},
    ).fetchone().r
    session.execute(
        text("UPDATE monitor_run SET residual_acreage_after = :after WHERE covid = :covid AND run_seq = :run_seq"),
        {"after": residual_after, "covid": covid, "run_seq": run_seq},
    )

    return {
        "new_parcels_found": len(matched),
        "run_seq": run_seq,
        "residual_acreage_before": before,
        "residual_acreage_after": float(residual_after or 0),
    }

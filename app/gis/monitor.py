"""Monitor a metes-and-bounds tract's own residual (unaccounted) acreage for a
new plat -- CLAUDE.md's own description of this system: "...and monitor
remaining raw acreage for new plats." Only meaningful for metes_and_bounds_
traverse tracts: a current_parcel_match tract's geom IS the union of its own
matched parcels by construction, so it has no independent residual to watch
(residual_geom stays NULL for it, always).

Two distinct events, both found by the same live re-check against the
tract's bbox:

1. A brand-new parcel appears where nothing was classified before (raw
   acreage getting its first plat). Classified the same way as classify_
   metes_and_bounds_tract's initial pass.

2. A previously-classified parcel's own apn no longer appears in the live
   roll at all -- since a classified parcel by definition intersects the
   tract (entirely inside the bbox), its continued existence with the same
   geometry would still show up in the SAME bbox query. Disappearing means
   the county retired/replaced that apn: a real split, merge, or
   renumbering. This is exactly the gap migration 0029 (parcel_lineage)
   named the Monitor to close: "a fee owed on a bulk/tract-level transfer...
   If [the buyer] later splits that tract, plats it into 100 lots... there
   is no way to walk from a current lot back to an ancestral tract's
   fee_collection rows." A retired apn is snapshotted into parcel_history
   (change_reason='replat'), correlated by real spatial overlap against its
   own last-known geometry to whichever new candidate(s) now cover that
   footprint, and recorded as parcel_lineage edges (parent = retired apn,
   child = each overlapping new apn) -- both parcel_history's 'replat'
   reason and the whole parcel_lineage table have existed in the schema
   since migrations 0001/0029 but nothing had ever written to them. A
   retirement with no overlapping successor at all, or an overlap pattern
   too tangled to confidently call split vs. merge (lineage_type='unknown'),
   is flagged on the covenant for human review rather than guessed --
   CLAUDE.md's own "never fabricate... low confidence... goes to human
   review" principle applies exactly as much to a lineage edge as to any
   other datum. A retired parcel's parcel_covenant row is always removed
   (it is no longer a current lot), independent of whether its lineage was
   confidently resolved.
"""
import re
from datetime import date

from sqlalchemy import text

from app.db.repository import insert_source, upsert_parcel
from app.gis.classifier import COUNTY_ADAPTERS
from app.queue.job_queue import run_with_job_queue

_VALID_RUN_TYPES = ("scheduled", "manual")


def _flag_for_review(session, covid: int, note: str) -> None:
    """Same tagged-note merge pattern as reconcile.py's own covenant.status
    update -- only this stage's own MONITOR-STAGE section is ever replaced,
    any other stage's still-open note is left untouched, and a covenant
    already past this point (title_in_progress/done) is never regressed."""
    existing = session.execute(
        text("SELECT status, review_reason FROM covenant WHERE covid = :covid"), {"covid": covid},
    ).fetchone()
    reason = existing.review_reason or ""
    reason = re.sub(r";?\s*MONITOR-STAGE \(automated[^)]*\):.*$", "", reason).strip("; ").strip()
    tagged = f"MONITOR-STAGE (automated, {date.today().isoformat()}): {note}"
    reason = f"{reason}; {tagged}" if reason else tagged
    status = existing.status if existing.status in ("title_in_progress", "done") else "needs_review"
    session.execute(
        text("UPDATE covenant SET status = :status, review_reason = :reason, updated_at = now() WHERE covid = :covid"),
        {"status": status, "reason": reason, "covid": covid},
    )


def _classify_lineage_type(parent_fanout: dict, child_fanin: dict, parent_apn: str, child_apn: str) -> str:
    p, c = parent_fanout[parent_apn], child_fanin[child_apn]
    if p == 1 and c == 1:
        return "replat"  # a clean one-to-one correspondence -- a renumbering/boundary correction, not a real split
    if p > 1 and c == 1:
        return "subdivision_split"
    if c > 1 and p == 1:
        return "merge"
    return "unknown"  # a genuinely tangled many-to-many overlap -- not confidently callable either way


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
    candidate_apns = {p["apn"] for p in candidates}
    new_candidates = [p for p in candidates if p["apn"] not in already_classified]
    retired_apns = already_classified - candidate_apns

    run_seq = session.execute(
        text("SELECT COALESCE(MAX(run_seq), 0) + 1 AS n FROM monitor_run WHERE covid = :covid"),
        {"covid": covid},
    ).fetchone().n
    before = float(row.residual_acreage_before or 0)

    if not new_candidates and not retired_apns:
        session.execute(
            text("""
                INSERT INTO monitor_run (covid, run_seq, run_type, new_parcels_found,
                                          residual_acreage_before, residual_acreage_after, status)
                VALUES (:covid, :run_seq, :run_type, 0, :before, :before, 'ok')
            """),
            {"covid": covid, "run_seq": run_seq, "run_type": run_type, "before": before},
        )
        return {"new_parcels_found": 0, "retired_parcels_found": 0, "lineage_edges_written": 0,
                "run_seq": run_seq, "residual_acreage_before": before, "residual_acreage_after": before}

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
    } if new_candidates else set()
    for p in new_candidates:
        upsert_parcel(
            session, county_fips=p["county_fips"], apn=p["apn"], owner_name_raw=p["owner_name_raw"],
            situs_address=p["situs_address"], city=p.get("city"), zip_code=p.get("zip_code"),
            acreage=p["acreage"], geojson=p["geojson"], source_id=gis_source_id,
        )

    # -- Retirements: snapshot each retired apn's last-known state, then correlate it by
    # real spatial overlap (against that last-known geometry -- still sitting untouched in
    # `parcel`, since a retired apn is never among new_candidates and so is never upserted
    # above) to whichever just-upserted new candidate(s) now cover its old footprint. --
    lineage_edges_written = 0
    unresolved_retirements = []
    if retired_apns:
        for apn in retired_apns:
            session.execute(
                text("""
                    INSERT INTO parcel_history (county_fips, apn, owner_name_raw, acreage, geom, change_reason, source_id)
                    SELECT county_fips, apn, owner_name_raw, acreage, geom, 'replat', source_id
                    FROM parcel WHERE county_fips = :county_fips AND apn = :apn
                """),
                {"county_fips": county_fips, "apn": apn},
            )

        new_apns = [p["apn"] for p in new_candidates]
        overlap_pairs = session.execute(
            text("""
                SELECT retired.apn AS parent_apn, new_p.apn AS child_apn
                FROM parcel retired
                JOIN parcel new_p ON new_p.county_fips = retired.county_fips
                WHERE retired.county_fips = :county_fips AND retired.apn = ANY(:retired_apns)
                  AND new_p.apn = ANY(:new_apns)
                  AND ST_Intersects(retired.geom, new_p.geom)
            """),
            {"county_fips": county_fips, "retired_apns": list(retired_apns), "new_apns": new_apns},
        ).fetchall() if new_apns else []

        parent_fanout, child_fanin = {}, {}
        for pair in overlap_pairs:
            parent_fanout[pair.parent_apn] = parent_fanout.get(pair.parent_apn, 0) + 1
            child_fanin[pair.child_apn] = child_fanin.get(pair.child_apn, 0) + 1

        ambiguous_pairs = []
        for pair in overlap_pairs:
            lineage_type = _classify_lineage_type(parent_fanout, child_fanin, pair.parent_apn, pair.child_apn)
            if lineage_type == "unknown":
                ambiguous_pairs.append((pair.parent_apn, pair.child_apn))
            session.execute(
                text("""
                    INSERT INTO parcel_lineage (county_fips, apn, parent_county_fips, parent_apn,
                                                 lineage_type, effective_date, source_id)
                    VALUES (:county_fips, :child_apn, :county_fips, :parent_apn, :lineage_type, :today, :source_id)
                    ON CONFLICT (county_fips, apn, parent_county_fips, parent_apn) DO NOTHING
                """),
                {"county_fips": county_fips, "child_apn": pair.child_apn, "parent_apn": pair.parent_apn,
                 "lineage_type": lineage_type, "today": date.today(), "source_id": gis_source_id},
            )
            lineage_edges_written += 1

        resolved_parents = set(parent_fanout.keys())
        unresolved_retirements = sorted(retired_apns - resolved_parents)

        session.execute(
            text("DELETE FROM parcel_covenant WHERE covid = :covid AND tract_no = :tract_no AND apn = ANY(:apns)"),
            {"covid": covid, "tract_no": tract_no, "apns": list(retired_apns)},
        )

        if unresolved_retirements:
            _flag_for_review(
                session, covid,
                f"tract {tract_no}: parcel(s) {', '.join(unresolved_retirements)} were previously classified "
                f"but no longer appear in {adapter.__name__}'s live roll, with no overlapping successor "
                f"parcel found within the same bounding box -- needs human review to confirm what happened "
                f"to this land (a merge into a parcel outside this bbox, a data gap, etc.)",
            )
        if ambiguous_pairs:
            detail = "; ".join(f"{p} -> {c}" for p, c in ambiguous_pairs)
            _flag_for_review(
                session, covid,
                f"tract {tract_no}: retired parcel(s) overlap new parcel(s) in a pattern too tangled to "
                f"confidently call a split or a merge ({detail}) -- recorded in parcel_lineage as 'unknown', "
                f"needs human review",
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
    ).fetchall() if new_candidates else []

    # Only a parcel that's genuinely relevant to THIS tract (passed the true spatial test
    # above) AND was already on file before this re-check (e.g. matched to a different,
    # adjoining tract -- NOT a retirement's own successor, which gets its parcel_lineage edge
    # above instead of this generic monitor_diff note) gets a plain parcel_history snapshot.
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
    # FULL cumulative, CURRENT parcel_covenant set for this tract (every remaining run_seq,
    # now correctly excluding any apn just retired above).
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
        "retired_parcels_found": len(retired_apns),
        "lineage_edges_written": lineage_edges_written,
        "unresolved_retirements": unresolved_retirements,
        "run_seq": run_seq,
        "residual_acreage_before": before,
        "residual_acreage_after": float(residual_after or 0),
    }

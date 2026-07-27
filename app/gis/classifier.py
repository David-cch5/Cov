"""Resolve a covenant's tract boundary and classify parcels against it.

For subdivision_plat descriptions this is a union of already-existing county parcel
geometry, not a from-scratch computation -- the county already surveyed each lot when
its plat was recorded. This also gives the boundary a durable, name-independent anchor:
once tract.geom is set, later monitor runs re-query by spatial overlap against whatever
the county's current parcel layer shows, regardless of any later replat/rename -- see
the discussion this was designed around.
"""
import json

from sqlalchemy import text

from app.db.repository import insert_source, upsert_parcel
from app.gis.adapters import (
    bexar_tx, collin_tx, dallas_tx, denton_tx, douglas_co, harris_tx, hunt_tx, kerr_tx, llano_tx,
    montgomery_tx, nueces_tx, tarrant_tx, travis_tx,
)
from app.queue.job_queue import run_with_job_queue

COUNTY_ADAPTERS = {
    "48339": montgomery_tx,
    "48439": tarrant_tx,
    "48113": dallas_tx,
    "48299": llano_tx,
    "48029": bexar_tx,
    "48201": harris_tx,
    "48453": travis_tx,
    "48085": collin_tx,
    "48355": nueces_tx,
    "48121": denton_tx,
    "48231": hunt_tx,
    "48265": kerr_tx,
    "08035": douglas_co,
}

# Adapters without a structured Lot/Block field can only text-match the legal
# description, so a "match" there is inherently less certain than Montgomery's
# exact attribute filter -- cap confidence accordingly rather than reporting
# false certainty on a fuzzy match. Llano/Bexar/Nueces/Hunt/Kerr have nominal
# tract_or_lot/Block-style columns, but they're inconsistently populated (see
# each adapter's docstring), so they belong in this set too despite the schema
# looking structured. Harris/Collin/Denton/Travis have genuinely reliable
# structured lot/tract columns and are NOT in this set. Douglas Co (CO) only
# has a structured Block field (no Lot field at all -- see douglas_co.py), so
# it belongs here too.
_TEXT_MATCH_ONLY_COUNTIES = {"48439", "48113", "48299", "48029", "48355", "48453", "48231", "48265", "08035"}


def resolve_subdivision_plat_tract(session, covid: int, tract_no: int = 1) -> dict:
    row = session.execute(
        text("SELECT county_fips, legal_description_parsed FROM covenant WHERE covid = :covid"),
        {"covid": covid},
    ).fetchone()
    if row is None or row.legal_description_parsed is None:
        raise RuntimeError(f"covid {covid} has no legal_description_parsed to resolve from")

    county_fips = row.county_fips
    parsed = row.legal_description_parsed
    adapter = COUNTY_ADAPTERS.get(county_fips)
    if adapter is None:
        raise RuntimeError(f"no GIS adapter registered for county_fips={county_fips}")

    # a Texas-abstract deed names a unique abstract code (e.g. "A0166") --
    # prefer matching on that directly, when the adapter supports it, over a
    # name-text search: the code is unambiguous where a survey grantee's name
    # (used as the "subdivision name" for this legal_description_type) is not.
    use_abstract_code = bool(parsed.get("abstract_code") and hasattr(adapter, "query_by_abstract_code"))
    if use_abstract_code:
        def _call():
            return adapter.query_by_abstract_code(parsed["abstract_code"], parsed["lots"])
    else:
        def _call():
            return adapter.query_by_subdivision_and_lots(parsed["subdivision_name"], parsed["lots"], block=parsed.get("block"))

    # The live network call (an ArcGIS REST query) gets the same
    # retry-with-backoff + durable job_queue failure logging as the
    # recorder-portal adapters -- it's the exact same class of problem (a
    # third-party endpoint that can be transiently slow/unreachable or
    # genuinely down/changed), just over a stable REST API instead of a
    # rendered page. A covenant with no parcels matched is NOT retried here
    # -- that's a real "nothing found" result, not a network failure, and
    # retrying it would just ask the same well-formed query five more times.
    parcels = run_with_job_queue(
        _call, job_type="gis_classifier_query", county_fips=county_fips, covid=covid,
        payload={
            "adapter": adapter.__name__, "base_url": adapter.BASE_URL,
            "method": "query_by_abstract_code" if use_abstract_code else "query_by_subdivision_and_lots",
            "abstract_code": parsed.get("abstract_code"), "subdivision_name": parsed.get("subdivision_name"),
            "lots": parsed.get("lots"), "block": parsed.get("block"),
        },
    )
    if not parcels:
        raise RuntimeError(f"no parcels matched subdivision={parsed['subdivision_name']!r} lots={parsed['lots']}")

    requested_lots = set(parsed["lots"])
    matched_lots = {p["lot"] for p in parcels if p["lot"]}
    missing_lots = sorted(requested_lots - matched_lots, key=lambda x: (len(x), x))

    match_rate = (len(matched_lots) / len(requested_lots)) if requested_lots else None
    text_match_only = county_fips in _TEXT_MATCH_ONLY_COUNTIES
    confidence = (match_rate * 0.7) if (match_rate is not None and text_match_only) else match_rate

    gis_source_id = insert_source(
        session, source_type="gis_api", reference=adapter.BASE_URL, confidence=confidence,
    )

    apns = []
    for p in parcels:
        upsert_parcel(
            session, county_fips=p["county_fips"], apn=p["apn"], owner_name_raw=p["owner_name_raw"],
            situs_address=p["situs_address"], city=p.get("city"), zip_code=p.get("zip_code"),
            acreage=p["acreage"], geojson=p["geojson"], source_id=gis_source_id,
        )
        apns.append(p["apn"])

    # SUM(acreage) is the county's own attribute where populated; some
    # counties (e.g. Harris, for smaller/commercial parcels) leave it NULL
    # even though the parcel has real geometry, so fall back to computing
    # acreage from the unioned geometry itself rather than leaving a real,
    # geometrically-resolved tract with no acreage to reconcile against.
    session.execute(
        text("""
            INSERT INTO tract (covid, tract_no, geom, classified_acreage, boundary_resolution_method, source_id, updated_at)
            SELECT :covid, :tract_no, ST_Multi(ST_Union(geom)),
                   COALESCE(SUM(acreage), ST_Area(ST_Union(geom)::geography) / 4046.8564224),
                   'current_parcel_match', :source_id, now()
            FROM parcel WHERE county_fips = :county_fips AND apn = ANY(:apns)
            ON CONFLICT (covid, tract_no) DO UPDATE SET
                geom = EXCLUDED.geom, classified_acreage = EXCLUDED.classified_acreage,
                boundary_resolution_method = EXCLUDED.boundary_resolution_method,
                source_id = EXCLUDED.source_id, updated_at = now()
        """),
        {"covid": covid, "tract_no": tract_no, "county_fips": county_fips, "apns": apns, "source_id": gis_source_id},
    )

    run_seq = session.execute(
        text("SELECT COALESCE(MAX(run_seq), 0) + 1 AS n FROM monitor_run WHERE covid = :covid"),
        {"covid": covid},
    ).fetchone().n

    session.execute(
        text("""
            INSERT INTO monitor_run (covid, run_seq, run_type, new_parcels_found, status)
            VALUES (:covid, :run_seq, 'initial', :n, 'ok')
        """),
        {"covid": covid, "run_seq": run_seq, "n": len(parcels)},
    )

    match_method = "text match against legal description" if text_match_only else "exact county GIS attribute filter"
    for p in parcels:
        parcel_confidence = (1.0 if p["lot"] in requested_lots else 0.5) * (0.7 if text_match_only else 1.0)
        session.execute(
            text("""
                INSERT INTO parcel_covenant (county_fips, apn, covid, tract_no, run_seq, classification, confidence, rationale)
                VALUES (:county_fips, :apn, :covid, :tract_no, :run_seq, 'interior', :confidence, :rationale)
                ON CONFLICT (county_fips, apn, covid, tract_no, run_seq) DO NOTHING
            """),
            {
                "county_fips": p["county_fips"], "apn": p["apn"], "covid": covid, "tract_no": tract_no,
                "run_seq": run_seq, "confidence": parcel_confidence,
                "rationale": f"lot {p['lot']} matched subdivision '{parsed['subdivision_name']}' via {match_method}",
            },
        )

    return {
        "matched_parcels": len(parcels),
        "requested_lots": len(requested_lots),
        "missing_lots": missing_lots,
        "run_seq": run_seq,
    }

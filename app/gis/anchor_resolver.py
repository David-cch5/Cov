"""Deterministic orchestrator for metes-and-bounds tract anchoring.

Tries, in order: the free/deterministic techniques already built in
app/gis/state_plane_anchor.py -> an LLM-driven agentic search
(app/llm/anchor_agent.py) at Opus 5, then Fable 5, if none of those succeed
or pass their own sanity check -> app/gis/geocode_anchor.py's rough
approximate-placement tier as the final safety net when even that doesn't
produce a confident, validated anchor.

This is the orchestrator app/parsing/legal_description/metes_bounds.py's own
docstring used to reference (as "resolve_metes_and_bounds_tract in
classifier.py") -- that function was never actually built; every anchor in
this project before this point was resolved by a human hand-picking a
technique and writing a one-off script per covenant (Ellis, Montgomery,
Collin, Nueces). Nothing here replaces that judgment -- it automates exactly
the sequence a careful human already followed, with the LLM tiers standing
in for the parts that previously needed a person driving an interactive
session, and a strict, independent verification gate before anything from
those tiers is ever trusted (see _verify_llm_anchor: real closure/acreage
recomputation and a real live-parcel dry run, never the model's own
self-reported confidence number alone).
"""
import json
import re

from pyproj import CRS
from sqlalchemy import text

from app.config import LLM_MODEL_HARD, LLM_MODEL_HARDEST
from app.db.repository import insert_source
from app.db.review_notes import merge_tagged_note
from app.gis.classifier import COUNTY_ADAPTERS, classify_metes_and_bounds_tract
from app.ingestion.walk import get_deed_text
from app.gis.geocode_anchor import resolve_metes_bounds_approximate
from app.gis.ngs import NgsNamedMarkUnresolved, NgsUnanswered, find_monuments
from app.gis.state_plane_anchor import (
    EPSG_BY_TX_ZONE,
    anchor_by_adjoining_plat,
    anchor_by_ngs_monument_tie,
    traverse_to_geojson_state_plane,
)
from app.llm.anchor_agent import escalate_anchor_to_llm
from app.title.release import is_fully_released
from app.parsing.legal_description.metes_bounds import (
    _COURSE_RE,
    extract_point_of_beginning,
    walk_traverse,
)
from app.parsing.legal_description.adjoiners import (
    adjoiner_name_key,
    courses_running_with_adjoiner,
)
from app.parsing.legal_description.monument_ties import extract_ngs_monument_ties
from app.parsing.legal_description.metes_bounds_llm import extract_courses_with_escalation
from app.queue.job_queue import JobFailed

# Only the counties this project has actually confirmed a real Texas State
# Plane zone for (covid 3194/Montgomery used Central per
# state_plane_anchor.py's own docstring; covid 5838/Nueces used South,
# verified this session against NGS's own published grid coordinates) -- an
# unlisted county returns "zone unknown" rather than guessing one, per
# CLAUDE.md's never-fabricate rule. Extend as more covenants confirm a zone.
_KNOWN_TX_ZONES = {
    "48339": "central",  # Montgomery
    "48355": "south",    # Nueces
}

_STATE_PLANE_COORD_RE = re.compile(
    r"X\s*=\s*([\d,]+\.?\d*)[^\d]+Y\s*=\s*([\d,]+\.?\d*)", re.IGNORECASE,
)

# See _ngs_search_bbox: wider is not safer here, it is broken.
_NGS_BBOX_BUFFER_DEG = 0.05

_MIN_CONFIDENCE_TO_AUTOCOMMIT = 0.75
# More than this far off the deed's own stated acreage means the candidate
# doesn't reconcile, regardless of what confidence it was reported with.
_MAX_ACREAGE_DEVIATION = 0.05


def _try_stated_coordinate(county_fips: str, legal_description_raw: str, courses: list) -> dict | None:
    """Tier 0a: the deed states its own Point of Beginning as a real State
    Plane coordinate. No judgment involved -- if the coordinate is there and
    the zone is known, this is pure projection math, not a guess."""
    zone = _KNOWN_TX_ZONES.get(county_fips)
    if zone is None:
        return None
    m = _STATE_PLANE_COORD_RE.search(legal_description_raw or "")
    if not m:
        return None
    if not courses:
        # Reachable now that course extraction failing hard (see
        # resolve_metes_and_bounds_anchor's own try/except around
        # extract_courses_with_escalation) no longer aborts the whole
        # function -- nothing to walk without any courses.
        return None
    origin_x = float(m.group(1).replace(",", ""))
    origin_y = float(m.group(2).replace(",", ""))
    traverse = walk_traverse(courses)
    geojson = traverse_to_geojson_state_plane(traverse["vertices"], origin_x, origin_y, EPSG_BY_TX_ZONE[zone])
    return {
        "geojson": geojson, "method": "stated_coordinate", "confidence": 0.95,
        "reasoning": f"deed states its own POB as State Plane X={origin_x}, Y={origin_y} "
                     f"(Texas {zone.replace('_', ' ')} zone, EPSG {EPSG_BY_TX_ZONE[zone]})",
    }


def _ngs_search_bbox(session, county_fips: str) -> dict | None:
    """Where to look for the deed's monuments: the county's own already-loaded
    parcel extent, buffered a little.

    The buffer is deliberately SMALL. A tie runs thousands of feet -- covid
    5838's longest is 6,093 ft, about 0.017 degrees -- so 0.05 degrees (roughly
    3.4 miles) is already generous, and going wider actively breaks the search:
    NGS's bounds endpoint silently caps at NGS_BOUNDS_RESULT_CAP marks, and the
    same Nueces box buffered by 0.25 degrees returns 500 marks containing
    NEITHER monument this deed names, where the unbuffered extent returns 415
    containing both. find_monuments raises on that case rather than reporting a
    published monument as missing.

    Returns None when the county has no parcels loaded yet, which correctly
    skips this tier rather than searching blind."""
    row = session.execute(
        text("""
            SELECT ST_XMin(e) AS min_lon, ST_XMax(e) AS max_lon,
                   ST_YMin(e) AS min_lat, ST_YMax(e) AS max_lat
            FROM (SELECT ST_Extent(geom) AS e FROM parcel WHERE county_fips = :fips) x
        """), {"fips": county_fips},
    ).fetchone()
    if row is None or row.min_lon is None:
        return None
    buf = _NGS_BBOX_BUFFER_DEG
    return {"min_lon": row.min_lon - buf, "max_lon": row.max_lon + buf,
            "min_lat": row.min_lat - buf, "max_lat": row.max_lat + buf}


def _try_ngs_monument_tie(session, county_fips: str, deed_text: str, courses: list) -> dict | None:
    """Tier 0b: the deed ties its Point of Beginning to a published National
    Geodetic Survey control monument -- "a National Geodetic Survey monument
    stamped \"SF-010\" bears North 14 01'24\" East 4708.73 feet". That is a real,
    free, survey-grade georeference: reversing the deed's own bearing and
    distance from the monument's published State Plane coordinates puts the
    corner on the ground, with no parcel fitting and no rotation solve (a deed
    reciting ties this way is working on the grid, so its bearings ARE grid
    azimuths -- see app/gis/state_plane_anchor.py's own notes).

    ONLY ties attached to the POINT OF BEGINNING are used, and that restriction
    is the point. A tie names the corner it runs from ("from which north corner
    of this tract"), and mapping a named corner onto a particular traverse
    vertex is exactly the judgment call 0c and 0d below decline to automate. A
    tie sitting in the opening BEGINNING clause, before the first THENCE, is
    unambiguously vertex 0. Anything later is left to Tier 1, which has the
    tools to reason about it.

    That rule is deliberately strict enough to decline covid 5838 tract 1: its
    own POB is an iron bolt reciting no monument at all, and all twelve of that
    deed's ties belong to its SAVE AND EXCEPT tracts further down the document.
    Anchoring tract 1 off one of those would be wrong, and this returns None
    rather than reaching for them.

    Confidence reflects what was actually checked: 0.95 when two ties to two
    monuments cross-check against the monuments' own published separation, 0.80
    for a lone tie carrying only the zone check. Either way the candidate still
    has to survive _attempt_and_verify's live classification before it commits.
    """
    if not courses:
        return None
    # The boundary starts at the first real COURSE, not the first "thence"
    # token. That distinction is load-bearing: covid 5838's 3.103 ac tract
    # reaches its tie through a two-leg offset that says "...105.56 feet and
    # thence North 35 24'54" East 50.00 feet and from which north corner of this
    # tract, a National Geodetic Survey monument..." -- the word appears INSIDE
    # the tie description, before the tie itself. _COURSE_RE requires a full
    # bearing, distance and "to"/"for" terminator, so it does not match that leg
    # and the tie is correctly still read as belonging to the Point of Beginning.
    first_course = _COURSE_RE.search(deed_text)
    if first_course is None:
        return None
    pob_ties = [t for t in extract_ngs_monument_ties(deed_text) if t.position < first_course.start()]
    if not pob_ties:
        return None

    bbox = _ngs_search_bbox(session, county_fips)
    if bbox is None:
        return None
    try:
        wanted = {t.designation for t in pob_ties}
        monuments = find_monuments(wanted, bbox)
        if not monuments:
            # THIS deed names those marks, which find_monuments does not know.
            # An empty result is therefore not "no tie here" -- it is a question
            # NGS did not answer -- and the difference is ~$45-50, since
            # declining walks down to Opus and Fable for what NGS gives free.
            # Raised here rather than in the client because the evidence that
            # the monument exists is the deed's own recital, which only this
            # tier is holding.
            raise NgsNamedMarkUnresolved(
                f"the deed recites monument(s) {sorted(wanted)} at its Point of Beginning, "
                f"but the NGS search of {bbox} resolved none of them -- retry, or narrow "
                f"the search area; do not read this as 'no NGS tie available'")
            # Defensive only. find_monuments now raises (NgsUnanswered family) on
            # every way a named mark can fail to resolve -- empty response,
            # truncated result, unparseable datasheet, or a result set that
            # simply lacks it -- precisely because reaching this line used to
            # mean "no tie here" and sent the covenant to the paid tiers. This
            # tract's deed names a monument, so an empty answer is never a
            # finding about the land.
            return None
        placed = anchor_by_ngs_monument_tie(walk_traverse(courses)["vertices"], pob_ties, monuments)
    except NgsUnanswered:
        # NOT the same as "no tie here", and the difference decides money. This
        # deed names an NGS monument, so a free, published, survey-grade answer
        # exists; NGS merely failed to hand it over. Falling through would walk
        # down the tiers to Opus and Fable and spend ~$45-50 answering a question
        # NGS answers for nothing -- so this propagates instead, and the queue
        # retries the covenant later with the tier still available.
        raise
    except Exception as exc:                  # noqa: BLE001
        # Anything else -- an unresolvable tie, a datasheet that will not parse --
        # is a genuine "this tier cannot place it", and falling through to the
        # next tier is right.
        print(f"  [anchor_resolver] NGS monument tier cannot place this tract: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return None

    zone_error = placed.get("zone_check_ft")
    if zone_error is None or zone_error >= 1.0:
        # The zone mapping is checkable against the monument's own datasheet and
        # is off by miles when wrong -- never place on one that doesn't reproduce.
        return None

    cross = placed.get("cross_check")
    corrected = (cross or {}).get("corrected")
    detail = (f"cross-checked against a second monument tie" if cross and cross.get("agree")
              else f"cross-checked after correcting the {corrected['tie']} tie's {corrected['field']} "
                   f"quadrant letter" if corrected
              else "single tie, no second monument to cross-check")
    return {
        "geojson": placed["geojson"],
        "method": "ngs_monument_tie",
        "confidence": 0.95 if placed["verified"] else 0.80,
        "reasoning": (f"deed ties its Point of Beginning to NGS monument {placed['monument']} "
                      f"(PID {placed['pid']}) bearing {placed['tie_used']}; placed on EPSG "
                      f"{placed['epsg']}, monument reprojects onto its own datasheet grid "
                      f"coordinates to {zone_error:.3f} ft; {detail}"),
    }


def _try_sibling_tract_tie(session, covid: int, tract_no: int) -> dict | None:
    """Tier 0c: not automated in this pass. A sibling tract being already
    anchored is a necessary precondition, but identifying WHICH course/
    vertex of THIS tract ties to which real corner of the sibling is a
    judgment call this function deliberately does not attempt -- that's
    exactly the class of problem Tier 1 (the LLM tiers) exists for."""
    return None


_ADJOINER_BUFFER_FT = 45.0        # dissolve lot lines and fill streets: a plat's
                                  # outer line is not carried by any one tax parcel
_MIN_ADJOINER_PARCELS = 8         # fewer than this is not a subdivision footprint
_MAX_ADJOINER_PARCELS = 4000


def _state_plane_epsg_for_county(session, county_fips: str) -> tuple[int, str] | None:
    """The State Plane zone to work in, or None rather than a guess.

    The zone is load-bearing here for a reason that is easy to miss: the deed's
    bearings are the PLAT's bearings, which are grid bearings on the surveyor's
    own zone. Fitting in a different projection tilts the traverse by the
    convergence difference -- around a degree a couple of degrees off the central
    meridian -- and anchor_by_adjoining_plat's rotation probe would then refuse a
    perfectly good tie. Getting it wrong costs a false refusal, not a wrong
    answer, but a tier that refuses everything is not a tier.

    A hand-registered zone wins. Otherwise it is derived from the county's own
    loaded parcels against each zone's PUBLISHED area of use, and accepted only
    when exactly one zone contains them -- Texas' zones are latitude bands whose
    published extents overlap, so Montgomery and Travis sit in two at once and
    fall through to the registry rather than being assigned by a coin toss.
    """
    zone = _KNOWN_TX_ZONES.get(county_fips)
    if zone is not None:
        return EPSG_BY_TX_ZONE[zone], f"registered zone (Texas {zone.replace('_', ' ')})"
    row = session.execute(text("""
        SELECT ST_X(ST_Centroid(e)) AS lon, ST_Y(ST_Centroid(e)) AS lat
          FROM (SELECT ST_Extent(geom) AS e FROM parcel WHERE county_fips = :fips) x
    """), {"fips": county_fips}).fetchone()
    if row is None or row.lon is None:
        return None
    hits = []
    for name, epsg in EPSG_BY_TX_ZONE.items():
        use = CRS.from_epsg(epsg).area_of_use
        if use and use.west <= row.lon <= use.east and use.south <= row.lat <= use.north:
            hits.append((epsg, name))
    if len(hits) != 1:
        return None
    epsg, name = hits[0]
    return epsg, (f"derived: the county's parcels fall in exactly one zone's published "
                  f"area of use (Texas {name.replace('_', ' ')}, EPSG {epsg})")


def _adjoiner_footprint(session, county_fips: str, name: str, epsg: int,
                        buffer_ft: float = _ADJOINER_BUFFER_FT):
    """The adjoining plat's outer boundary, in State Plane feet, or None.

    Matched on adjoiner_name_key rather than the string: the deed's spelling and
    the CAD's rarely agree ("The Heights at West ridge Phase I" against "HEIGHTS
    AT WESTRIDGE PHASE I THE"). The county's own parcels are tried first because
    they are free and already local; the live layer is queried only when the plat
    is not one this covenant's census already pulled -- which is the usual case,
    since an ADJOINING subdivision is by definition not the encumbered one.
    """
    from shapely import wkt as shapely_wkt

    key = adjoiner_name_key(name)
    if len(key) < 8:
        return None
    tokens = sorted(re.findall(r"[A-Za-z]{4,}", name.upper()), key=len, reverse=True)
    if not tokens:
        return None
    probe = tokens[0]

    def _footprint_from(rows):
        """rows: (subdivision_text, geojson-or-None, wkb-or-None)."""
        kept = [r for r in rows
                if adjoiner_name_key(str(r[0] or "").split(",")[0]) == key]
        if not (_MIN_ADJOINER_PARCELS <= len(kept) <= _MAX_ADJOINER_PARCELS):
            return None, len(kept)
        session.execute(text("CREATE TEMP TABLE IF NOT EXISTS _adjoiner(g geometry)"))
        session.execute(text("TRUNCATE _adjoiner"))
        for sub, gj, geom_wkt in kept:
            if gj:
                session.execute(text(
                    "INSERT INTO _adjoiner VALUES "
                    "(ST_Transform(ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326), :e))"),
                    {"g": json.dumps(gj), "e": epsg})
            else:
                session.execute(text(
                    "INSERT INTO _adjoiner VALUES "
                    "(ST_Transform(ST_GeomFromText(:g, 4326), :e))"), {"g": geom_wkt, "e": epsg})
        # A buffer of zero means "the union's own outer line". ST_Union already
        # dissolves the shared lot lines inside a subdivision; the buffer exists
        # only to bridge STREET gaps, which are separate parcels. So it is needed
        # when a deed crosses a street and actively harmful when a deed runs along
        # an outer boundary: 45 ft of smoothing erases covid 4981 tract 3's 3.89 ft
        # ell jog along Heights at Westridge Phase III's west line, which is
        # precisely the feature the tie is meant to land on.
        outline = session.execute(text(
            "SELECT ST_AsText(CASE WHEN :b > 0 "
            "THEN ST_Buffer(ST_Buffer(ST_Union(g), :b), -:b) ELSE ST_Union(g) END) "
            "FROM _adjoiner"),
            {"b": buffer_ft}).scalar()
        session.execute(text("DROP TABLE IF EXISTS _adjoiner"))
        return (shapely_wkt.loads(outline) if outline else None), len(kept)

    # THE LIVE LAYER FIRST, and the local table only as a fallback. `parcel` is a
    # census of ENCUMBERED land, so for an ADJOINING plat -- which by definition is
    # not the encumbered land -- it holds whatever happened to fall inside a
    # covenant's footprint and nothing more. Covid 4981 tract 3 tied to Heights at
    # Westridge Phase III, of which our table holds 34 lots against the plat's ~290;
    # that fragment cleared the minimum count, the fit ran against a third of a
    # subdivision, and the tie was refused at a 114 ft residual. A fragment is worse
    # than nothing here, because it looks like an answer.
    adapter = COUNTY_ADAPTERS.get(county_fips)
    field = getattr(adapter, "FIELD_MAPPING", {}).get("abs_sub_name") if adapter else None
    if field:
        try:
            live = [(p.get("recited_legal_description"), p.get("geojson"), None)
                    for p in adapter.iter_parcels(where=f"UPPER({field}) LIKE '%{probe}%'")
                    if p.get("geojson")]
        except Exception as exc:                                   # noqa: BLE001
            print(f"  [anchor_resolver] live parcel query for {name!r} failed "
                  f"({type(exc).__name__}); falling back to the local census",
                  flush=True)
            live = []
        poly, kept = _footprint_from(live)
        if poly is not None:
            return poly

    local = session.execute(text("""
        SELECT recited_legal_description, ST_AsText(geom)
          FROM parcel
         WHERE county_fips = :fips AND geom IS NOT NULL
           AND upper(recited_legal_description) LIKE :probe
    """), {"fips": county_fips, "probe": f"%{probe}%"}).fetchall()
    poly, kept = _footprint_from([(r[0], None, r[1]) for r in local])
    return poly


def _try_parcel_tie(session, covid: int, tract_no: int, county_fips: str,
                    deed_text: str, courses: list) -> dict | None:
    """Tier 0d: the deed's Point of Beginning is a corner of a recorded plat the
    county publishes, and its opening courses run WITH that plat's line.

    Every part of this is read rather than inferred. The plat is the one the POB
    names; the contact courses are the ones the deed says run with it; the fit is
    a translation only, because a deed running with a plat's boundary is on that
    plat's bearings. `anchor_by_adjoining_plat` re-derives all of it and refuses
    on a short contact run, an overlap into the plat, a bad area, or bearings
    that turn out not to be the plat's -- so this returns None on anything it
    cannot stand behind, and the covenant falls through to the paid tiers only
    when the free reading genuinely does not hold.
    """
    if not courses:
        return None
    contact = courses_running_with_adjoiner(deed_text, len(courses))
    if contact is None:
        return None
    zone = _state_plane_epsg_for_county(session, county_fips)
    if zone is None:
        return None
    epsg, zone_basis = zone

    # Try the exact outer line first, then the street-bridged one. Which is right
    # depends on whether the deed runs ALONG the plat's boundary or ACROSS its
    # streets, and the deed does not say -- so both are offered to the fit, and
    # its own checks decide. Ordered exact-first so a boundary-running deed is
    # never judged against a smoothed corner.
    attempts = []
    for buffer_ft in (0.0, _ADJOINER_BUFFER_FT):
        footprint = _adjoiner_footprint(session, county_fips, contact["adjoiner"], epsg,
                                        buffer_ft=buffer_ft)
        if footprint is None or footprint.is_empty:
            continue
        attempts.append((buffer_ft, footprint))
    if not attempts:
        return None

    stated = session.execute(text(
        "SELECT stated_acreage FROM tract WHERE covid = :c AND tract_no = :t"),
        {"c": covid, "t": tract_no}).scalar()
    placed, buffer_used = None, None
    for buffer_ft, footprint in attempts:
        trial = anchor_by_adjoining_plat(
            walk_traverse(courses)["vertices"], footprint, contact["contact_indices"],
            epsg=epsg, stated_acres=float(stated) if stated else None)
        if trial.get("anchored"):
            placed, buffer_used = trial, buffer_ft
            break
        print(f"  [anchor_resolver] adjoining-plat tier declines against the "
              f"{'exact outer line' if not buffer_ft else f'{buffer_ft:.0f} ft street-bridged'} "
              f"footprint: {trial.get('reason')}", flush=True)
    if placed is None:
        return None

    return {
        "geojson": placed["geojson"], "method": "adjoining_plat_tie", "confidence": 0.9,
        "reasoning": (
            f"deed's POB is a corner of {contact['adjoiner']}, and courses "
            f"{contact['contact_courses']} run with that plat's line. Fitted against the "
            f"county's published footprint ("
            f"{'exact outer line' if not buffer_used else f'{buffer_used:.0f} ft street-bridged'}"
            f") on {zone_basis}: residual "
            f"{placed['rms_ft']:.2f} ft over {placed['contact_span_ft']:.0f} ft of "
            f"frontage, {placed['overlap_sqft']:,.0f} sq ft overlap, area "
            f"{placed['area_acres']:.3f} ac, best rotation "
            f"{placed['best_rotation_deg']:+.2f} deg (grid holds)"),
    }


def _polygon_area_acres(session, geojson: dict) -> float | None:
    try:
        return session.execute(
            text("SELECT ST_Area(ST_SetSRID(ST_GeomFromGeoJSON(:g), 4326)::geography) / 4046.8564224"),
            {"g": json.dumps(geojson)},
        ).scalar()
    except Exception:
        return None


def _verify_llm_anchor(session, covid: int, county_fips: str, llm_result: dict) -> dict | None:
    """Never trust an LLM's own self-reported confidence or geometry as-is.
    Independently recompute the polygon's real area (PostGIS, not the
    model's own arithmetic) and confirm it reconciles with the deed's own
    stated acreage; the actual "does this find real parcels" check happens
    later, inside the same savepoint as the tentative tract.geom write (see
    resolve_metes_and_bounds_anchor), by re-running this project's own
    classify_metes_and_bounds_tract -- deterministic code, not a second LLM
    opinion. Returns a candidate dict ready to attempt-and-verify, or None
    if it fails even this first, cheaper check."""
    geojson_str = llm_result.get("anchor_geojson")
    if not geojson_str:
        return None
    try:
        geojson = json.loads(geojson_str) if isinstance(geojson_str, str) else geojson_str
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(geojson, dict) or "coordinates" not in geojson:
        return None

    area_acres = _polygon_area_acres(session, geojson)
    if not area_acres or area_acres <= 0:
        return None

    stated = session.execute(
        text("SELECT stated_acreage FROM covenant WHERE covid = :covid"), {"covid": covid},
    ).scalar()
    if stated:
        deviation = abs(area_acres - float(stated)) / float(stated)
        if deviation > _MAX_ACREAGE_DEVIATION:
            return None

    confidence = min(float(llm_result.get("confidence") or 0.0), 0.95)
    return {
        "geojson": geojson, "method": f"llm_{llm_result.get('method') or 'other'}",
        "confidence": confidence, "reasoning": llm_result.get("reasoning"),
        "area_acres": area_acres,
    }


def _attempt_and_verify(session, covid: int, tract_no: int, county_fips: str, candidate: dict) -> dict | None:
    """Tentatively write candidate.geojson to tract.geom inside a SAVEPOINT,
    then run the project's own real, deterministic
    classify_metes_and_bounds_tract against it -- the actual proof that this
    anchor finds real, currently-existing parcels, not a mocked check. Rolls
    back and returns None if classification finds nothing (the same signal
    classify_metes_and_bounds_tract's own error messages already call out as
    "may be mis-anchored") or if confidence doesn't clear the auto-commit
    bar; keeps the write and returns a summary otherwise."""
    if candidate["confidence"] < _MIN_CONFIDENCE_TO_AUTOCOMMIT:
        return None

    source_id = insert_source(
        session, source_type="gis_api",
        reference=f"anchor_resolver tier={candidate['method']}",
        confidence=candidate["confidence"],
    )
    try:
        with session.begin_nested():
            session.execute(
                text("""
                    INSERT INTO tract (covid, tract_no, geom, boundary_resolution_method, source_id, updated_at)
                    VALUES (:covid, :tract_no, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326),
                            'metes_and_bounds_traverse', :source_id, now())
                    ON CONFLICT (covid, tract_no) DO UPDATE SET
                        geom = EXCLUDED.geom, boundary_resolution_method = EXCLUDED.boundary_resolution_method,
                        source_id = EXCLUDED.source_id, updated_at = now()
                """),
                {"covid": covid, "tract_no": tract_no, "geojson": json.dumps(candidate["geojson"]),
                 "source_id": source_id},
            )
            classify_result = classify_metes_and_bounds_tract(session, covid=covid, tract_no=tract_no)
    except RuntimeError:
        # classify_metes_and_bounds_tract's own "nothing in bbox" / "nothing
        # intersects" errors are exactly the mis-anchored-tract signal its
        # own docstring calls out -- this candidate doesn't get to commit.
        return None

    # Confirmed real gap, not a hypothetical: a live run against covid 4781
    # committed a genuine, validated anchor but left the covenant's own
    # review_reason carrying geocode_anchor.py's stale "not yet anchored to a
    # real surveyed position" note verbatim -- misleading to any human or
    # future automated re-run that reads it. Strip that specific note (never
    # touching any OTHER note already there, e.g. classify's own invalid-
    # geometry flag, which is still legitimate) and replace it with a real
    # record of what actually happened, same tagged-note-replacement pattern
    # already used throughout this codebase (ingest_probe.py, chain.py).
    existing = session.execute(
        text("SELECT review_reason FROM covenant WHERE covid = :covid"), {"covid": covid},
    ).scalar() or ""
    stale_note_re = re.compile(
        r";?\s*Metes-and-bounds tract shape validated[^;]*not a confirmed boundary\.", re.IGNORECASE,
    )
    cleaned = stale_note_re.sub("", existing).strip("; ").strip()
    new_note = (
        f"ANCHOR RESOLVED (automated, tier={candidate['method']}, confidence={candidate['confidence']:.2f}): "
        f"tract {tract_no} anchored to a real, independently-verified position and spatially classified "
        f"against live parcel data."
    )
    session.execute(
        text("UPDATE covenant SET review_reason = :reason, updated_at = now() WHERE covid = :covid"),
        {"covid": covid, "reason": f"{cleaned}; {new_note}" if cleaned else new_note},
    )

    return {
        "tier": candidate["method"], "committed": True, "confidence": candidate["confidence"],
        "classify_result": classify_result, "reasoning": candidate.get("reasoning"),
    }


def _get_deed_text(session, covid: int, legal_description_raw: str | None) -> str:
    """Thin alias kept so this module's own call sites read unchanged -- the
    implementation moved to app/ingestion/walk.py once classifier.py needed the
    same full-document text (for adjoining-subdivision detection) and couldn't
    import it from here without a cycle."""
    return get_deed_text(session, covid, legal_description_raw)


def resolve_metes_and_bounds_anchor(session, covid: int, tract_no: int = 1,
                                    research_released: bool = False) -> dict:
    """Tiered anchor resolution: deterministic techniques first, then Opus 5,
    then Fable 5, then the existing rough-placement fallback. Returns a dict
    describing which tier succeeded (or that every tier fell through to the
    approximate-placement safety net) -- never raises just because a
    confident anchor couldn't be found; that is itself a legitimate, correct
    outcome per CLAUDE.md's own rule, not something to force a guess for.
    """
    # A fully released covenant is historic: worth recording, not worth
    # researching. Anchoring is the most expensive thing this project does -- the
    # LLM tiers below are budget-capped precisely because they cost real money --
    # and spending it to locate land whose covenant no longer exists is pure waste.
    # research_released=True is the deliberate override for when it is wanted
    # anyway; nothing reaches the paid tiers by accident.
    if not research_released:
        released = is_fully_released(session, covid)
        if released is not None:
            return {"covid": covid, "tract_no": tract_no, "committed": False,
                    "tier": "skipped_released",
                    "reason": (f"covenant fully released by {released['release_type']} "
                               f"{released['recording_instrument'] or released['release_id']} "
                               f"effective {released['effective_date']} -- historic, so no "
                               f"anchoring research is done. Pass research_released=True to "
                               f"override."),
                    "release": released, "llm_usage": {}}

    row = session.execute(
        text("SELECT county_fips, legal_description_raw, stated_acreage FROM covenant WHERE covid = :covid"),
        {"covid": covid},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} not found")
    county_fips = row.county_fips
    legal_description_raw = _get_deed_text(session, covid, row.legal_description_raw)

    try:
        courses, extraction_diag = extract_courses_with_escalation(legal_description_raw)
    except JobFailed as e:
        # Confirmed real, not hypothetical: two live runs (covid 3346 tract 2,
        # covid 4780 tract 1) crashed HERE with a raw traceback from an
        # exhausted-retries API failure, before Tier 0's own free/deterministic
        # techniques -- which need no LLM call at all -- ever got a chance to
        # run. run_with_job_queue already wrote a durable job_queue row for
        # this; the fix is to not let that turn into an uncaught crash of the
        # whole covenant's resolution attempt. Tier 1 (the agentic search)
        # doesn't depend on this pre-extracted course list either -- it reads
        # the deed text directly and walks its own courses via walk_courses --
        # so this can still proceed to both tiers, only skipping the one Tier
        # 0 technique (_try_stated_coordinate) that needs one.
        print(f"  [anchor_resolver] covid={covid} tract={tract_no} course extraction failed hard "
              f"(job_id={e.job_id}): {e.original_exception} -- proceeding without pre-extracted courses", flush=True)
        courses, extraction_diag = [], {"tier": "extraction_failed_hard", "error": str(e.original_exception)}

    # Tracked from here, not just around the Opus/Fable anchor tiers below:
    # extract_courses_with_escalation can itself escalate to Sonnet/Opus to
    # read the courses, a real cost incurred even when Tier 0's deterministic
    # anchoring succeeds immediately afterward -- every return path below
    # carries this running total, never just the anchor-tier portion of it.
    llm_usage_totals = {
        "input_tokens": 0, "output_tokens": 0,
        "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
    }
    for key in llm_usage_totals:
        llm_usage_totals[key] += (extraction_diag.get("usage") or {}).get(key, 0)

    if not courses and extraction_diag.get("tier") != "extraction_failed_hard":
        raise RuntimeError(
            f"covid {covid}: no metes-and-bounds courses extracted from the deed's own text, even "
            f"after LLM-escalated extraction (diagnostics: {extraction_diag})"
        )

    for attempt_fn, args in [
        (_try_stated_coordinate, (county_fips, legal_description_raw, courses)),
        (_try_ngs_monument_tie, (session, county_fips, legal_description_raw, courses)),
        (_try_sibling_tract_tie, (session, covid, tract_no)),
        (_try_parcel_tie, (session, covid, tract_no, county_fips,
                           legal_description_raw, courses)),
    ]:
        candidate = attempt_fn(*args)
        if candidate is None:
            continue
        result = _attempt_and_verify(session, covid, tract_no, county_fips, candidate)
        if result is not None:
            print(f"  [anchor_resolver] covid={covid} tract={tract_no} total llm_usage={llm_usage_totals}", flush=True)
            return {**result, "llm_usage": llm_usage_totals}

    best_llm_geojson = None  # kept for the approximate-placement fallback, even if never confident enough
    # Real total cost of the LLM tiers is the sum across whichever attempts
    # actually ran (Opus alone, or Opus + Fable) -- each escalate_anchor_to_llm
    # call reports its own turn-summed usage; nothing here estimates a dollar
    # figure, since cache-read/write are billed at different per-model rates
    # this function has no business guessing at.
    for model in (LLM_MODEL_HARD, LLM_MODEL_HARDEST):
        try:
            llm_result = escalate_anchor_to_llm(covid, tract_no, model)
        except JobFailed as e:
            # Confirmed real on covid 3346 tract 1: a genuinely capped/looping
            # agentic run raises JobFailed (see anchor_agent.py's own "capped"
            # branch) rather than returning a plain "could not anchor" result
            # -- and this loop, uncaught, let that crash the entire function
            # instead of falling through to the next tier or the approximate-
            # placement safety net below, contradicting this function's own
            # "never raises" docstring above. run_with_job_queue already wrote
            # a durable job_queue row for this; treat it exactly like a tier
            # that ran and honestly reported it couldn't anchor.
            print(f"  [anchor_resolver] covid={covid} tract={tract_no} model={model} tier failed hard "
                  f"(job_id={e.job_id}): {e.original_exception}", flush=True)
            continue
        for key in llm_usage_totals:
            llm_usage_totals[key] += (llm_result.get("usage") or {}).get(key, 0)
        if llm_result.get("anchor_geojson") and best_llm_geojson is None:
            best_llm_geojson = llm_result["anchor_geojson"]
        if not llm_result.get("anchored"):
            continue  # this tier's own agent honestly reported it couldn't confidently anchor
        candidate = _verify_llm_anchor(session, covid, county_fips, llm_result)
        if candidate is None:
            continue  # reported confident, but failed independent verification -- try the next tier
        result = _attempt_and_verify(session, covid, tract_no, county_fips, candidate)
        if result is not None:
            print(f"  [anchor_resolver] covid={covid} tract={tract_no} total llm_usage={llm_usage_totals}", flush=True)
            return {**result, "llm_usage": llm_usage_totals}

    # Nothing validated at any tier -- fall through to the existing rough-
    # placement safety net, using the best LLM-suggested position (even if
    # unconfirmed) rather than nothing at all, when one exists.
    anchor_lat = anchor_lon = None
    best_llm_geojson_parsed = None
    if best_llm_geojson:
        try:
            best_llm_geojson_parsed = (
                json.loads(best_llm_geojson) if isinstance(best_llm_geojson, str) else best_llm_geojson
            )
            pts = [pt for poly in best_llm_geojson_parsed["coordinates"] for ring in poly for pt in ring]
            anchor_lon = sum(p[0] for p in pts) / len(pts)
            anchor_lat = sum(p[1] for p in pts) / len(pts)
        except (json.JSONDecodeError, TypeError, KeyError, ZeroDivisionError):
            anchor_lat = anchor_lon = None
            best_llm_geojson_parsed = None

    print(f"  [anchor_resolver] covid={covid} tract={tract_no} total llm_usage={llm_usage_totals}", flush=True)

    if anchor_lat is None:
        # Confirmed real on covid 3346: without stripping a PRIOR exhausted
        # note first (the way _attempt_and_verify's own note-replacement
        # already does above), a repeated run appends a fresh copy every
        # time rather than replacing it -- review_reason there had this same
        # note duplicated three times over from three separate runs.
        existing = session.execute(
            text("SELECT review_reason FROM covenant WHERE covid = :covid"), {"covid": covid},
        ).scalar() or ""
        # Confirmed real on covid 3346: anchoring on the literal "cache-read."
        # tail only matches a note that HAS the token-burn sentence appended --
        # when repeated runs stack several copies of this note, only the LAST
        # one in the whole string carries that suffix, so a non-global-enough
        # pattern like the old one here left every earlier copy (ending in
        # just "...tie point.") behind, uncleaned, after a repeated run.
        # `[^;]*?` (non-greedy) lets each occurrence match up to WHICHEVER of
        # the two valid endings comes first, so re.sub's own default global
        # replacement correctly strips every stacked copy, not just the last.
        # merge_tagged_note replaces this note however its body happens to end,
        # so the old two-alternative ending pattern (and its stacking bug) is
        # no longer load-bearing.
        cleaned = merge_tagged_note(existing, "ANCHOR ESCALATION EXHAUSTED")
        new_note = (
            "ANCHOR ESCALATION EXHAUSTED (automated): deterministic techniques, Opus 5, and "
            "Fable 5 all failed to produce even a rough candidate position for this tract's "
            "metes-and-bounds description -- needs a human to locate a real tie point. "
            f"Tokens burned across both attempts: {llm_usage_totals['input_tokens']} in / "
            f"{llm_usage_totals['output_tokens']} out / "
            f"{llm_usage_totals['cache_creation_input_tokens']} cache-write / "
            f"{llm_usage_totals['cache_read_input_tokens']} cache-read."
        )
        session.execute(
            text("""
                UPDATE covenant SET status = 'needs_review', review_reason = :reason, updated_at = now()
                WHERE covid = :covid
            """),
            {"covid": covid, "reason": f"{cleaned}; {new_note}" if cleaned else new_note},
        )
        return {"tier": "exhausted", "committed": False, "llm_usage": llm_usage_totals}

    approx_result = resolve_metes_bounds_approximate(
        session, covid=covid, course_text=legal_description_raw or "",
        anchor_lat=anchor_lat, anchor_lon=anchor_lon, tract_no=tract_no,
        confidence=0.15,
        anchor_notes=(
            "position suggested by an LLM escalation tier that did not clear the auto-commit "
            "confidence bar or failed independent verification -- unconfirmed, needs human review"
        ),
        # Confirmed real on covid 3346 tract 2: "llm_suggested_unconfirmed" was
        # never a real value -- tract.tract_approximate_geom_method_check only
        # allows 'geocoded_point_of_beginning' / 'matched_parcel_corner' /
        # 'other' (checked via pg_get_constraintdef, not guessed). This code
        # path had apparently never actually been exercised successfully
        # before now (every prior attempt against 3346/4780/4781 died earlier
        # in the pipeline). The real detail (which tier, confidence, why it
        # didn't validate) is already in anchor_notes/approximate_geom_notes;
        # 'other' is the honest fit from the schema's actual vocabulary --
        # this is neither a free-text geocode nor a matched-parcel-corner tie.
        method="other",
        # Confirmed real on covid 3346 tract 2: without this, the shape gets
        # re-derived from course_text (the covenant's FULL raw text, spanning
        # every tract) via a second, separate, non-tract-aware extraction call
        # -- discarding whatever tract-specific shape the LLM tier itself may
        # have already correctly worked out (it reads full_ocr_text directly
        # and reasons about which THENCE calls belong to which tract; the
        # regex/LLM course extraction above and this fallback's own internal
        # one have no such awareness). Reusing the tier's own reported anchor_
        # geojson directly is strictly more likely to be right, and free.
        precomputed_geojson=best_llm_geojson_parsed,
    )
    return {"tier": "geocode_approximate", "committed": False, "approx_result": approx_result,
            "llm_usage": llm_usage_totals}

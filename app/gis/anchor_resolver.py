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
import os
import re

from sqlalchemy import text

from app.config import LLM_MODEL_HARD, LLM_MODEL_HARDEST
from app.db.repository import insert_source
from app.gis.classifier import COUNTY_ADAPTERS, classify_metes_and_bounds_tract
from app.ingestion.walk import TEXTCACHE
from app.gis.geocode_anchor import resolve_metes_bounds_approximate
from app.gis.state_plane_anchor import EPSG_BY_TX_ZONE, traverse_to_geojson_state_plane
from app.llm.anchor_agent import escalate_anchor_to_llm
from app.parsing.legal_description.metes_bounds import extract_point_of_beginning, walk_traverse
from app.parsing.legal_description.metes_bounds_llm import extract_courses_with_escalation

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
    origin_x = float(m.group(1).replace(",", ""))
    origin_y = float(m.group(2).replace(",", ""))
    traverse = walk_traverse(courses)
    geojson = traverse_to_geojson_state_plane(traverse["vertices"], origin_x, origin_y, EPSG_BY_TX_ZONE[zone])
    return {
        "geojson": geojson, "method": "stated_coordinate", "confidence": 0.95,
        "reasoning": f"deed states its own POB as State Plane X={origin_x}, Y={origin_y} "
                     f"(Texas {zone.replace('_', ' ')} zone, EPSG {EPSG_BY_TX_ZONE[zone]})",
    }


def _try_sibling_tract_tie(session, covid: int, tract_no: int) -> dict | None:
    """Tier 0b: not automated in this pass. A sibling tract being already
    anchored is a necessary precondition, but identifying WHICH course/
    vertex of THIS tract ties to which real corner of the sibling is a
    judgment call this function deliberately does not attempt -- that's
    exactly the class of problem Tier 1 (the LLM tiers) exists for."""
    return None


def _try_parcel_tie(session, county_fips: str, legal_description_raw: str, courses: list) -> dict | None:
    """Tier 0c: not automated in this pass, for the same reason as 0b.
    Confirmed real by this project's own experience: my own manual attempt
    at exactly this kind of tie on covid 5838 accepted a wrong correspondence
    (a 606 ft real edge forced to match a 719.14 ft recited course, a 16%
    mismatch) before stopping to check the fit's own implied scale factor.
    Automating "which adjoiner citation matches which course" reliably needs
    the same kind of judgment -- left to Tier 1, which has real tools
    (query_gis_parcels, solve_anchor_similarity) to do this properly and a
    hard length_ratio sanity check it's instructed to respect."""
    return None


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
    """Prefer the full OCR'd document text over covenant.legal_description_raw
    for course extraction -- confirmed real and necessary on covid 4781:
    legal_description_raw there is an ingestion-time SUMMARY that literally
    contains the placeholder text "[metes and bounds courses follow]" instead
    of the deed's own real field notes (which do exist, complete, with 8 real
    THENCE calls including a curve, in the full textcache text). Falls back
    to legal_description_raw only when no cached document text is available."""
    doc = session.execute(
        text("SELECT relpath FROM covenant_document WHERE covid = :covid AND doc_type = 'original'"),
        {"covid": covid},
    ).fetchone()
    if doc and doc.relpath:
        cache_path = os.path.join(TEXTCACHE, f"{covid}_{os.path.basename(doc.relpath)}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                full_text = json.load(f).get("text")
            if full_text:
                return full_text
    return legal_description_raw or ""


def resolve_metes_and_bounds_anchor(session, covid: int, tract_no: int = 1) -> dict:
    """Tiered anchor resolution: deterministic techniques first, then Opus 5,
    then Fable 5, then the existing rough-placement fallback. Returns a dict
    describing which tier succeeded (or that every tier fell through to the
    approximate-placement safety net) -- never raises just because a
    confident anchor couldn't be found; that is itself a legitimate, correct
    outcome per CLAUDE.md's own rule, not something to force a guess for.
    """
    row = session.execute(
        text("SELECT county_fips, legal_description_raw, stated_acreage FROM covenant WHERE covid = :covid"),
        {"covid": covid},
    ).fetchone()
    if row is None:
        raise RuntimeError(f"covid {covid} not found")
    county_fips = row.county_fips
    legal_description_raw = _get_deed_text(session, covid, row.legal_description_raw)

    courses, extraction_diag = extract_courses_with_escalation(legal_description_raw)
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

    if not courses:
        raise RuntimeError(
            f"covid {covid}: no metes-and-bounds courses extracted from the deed's own text, even "
            f"after LLM-escalated extraction (diagnostics: {extraction_diag})"
        )

    for attempt_fn, args in [
        (_try_stated_coordinate, (county_fips, legal_description_raw, courses)),
        (_try_sibling_tract_tie, (session, covid, tract_no)),
        (_try_parcel_tie, (session, county_fips, legal_description_raw, courses)),
    ]:
        candidate = attempt_fn(*args)
        if candidate is None:
            continue
        result = _attempt_and_verify(session, covid, tract_no, county_fips, candidate)
        if result is not None:
            print(f"  [anchor_resolver] covid={covid} tract={tract_no} total llm_usage={llm_usage_totals}")
            return {**result, "llm_usage": llm_usage_totals}

    best_llm_geojson = None  # kept for the approximate-placement fallback, even if never confident enough
    # Real total cost of the LLM tiers is the sum across whichever attempts
    # actually ran (Opus alone, or Opus + Fable) -- each escalate_anchor_to_llm
    # call reports its own turn-summed usage; nothing here estimates a dollar
    # figure, since cache-read/write are billed at different per-model rates
    # this function has no business guessing at.
    for model in (LLM_MODEL_HARD, LLM_MODEL_HARDEST):
        llm_result = escalate_anchor_to_llm(covid, tract_no, model)
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
            print(f"  [anchor_resolver] covid={covid} tract={tract_no} total llm_usage={llm_usage_totals}")
            return {**result, "llm_usage": llm_usage_totals}

    # Nothing validated at any tier -- fall through to the existing rough-
    # placement safety net, using the best LLM-suggested position (even if
    # unconfirmed) rather than nothing at all, when one exists.
    anchor_lat = anchor_lon = None
    if best_llm_geojson:
        try:
            geojson = json.loads(best_llm_geojson) if isinstance(best_llm_geojson, str) else best_llm_geojson
            pts = [pt for poly in geojson["coordinates"] for ring in poly for pt in ring]
            anchor_lon = sum(p[0] for p in pts) / len(pts)
            anchor_lat = sum(p[1] for p in pts) / len(pts)
        except (json.JSONDecodeError, TypeError, KeyError, ZeroDivisionError):
            anchor_lat = anchor_lon = None

    print(f"  [anchor_resolver] covid={covid} tract={tract_no} total llm_usage={llm_usage_totals}")

    if anchor_lat is None:
        session.execute(
            text("""
                UPDATE covenant SET status = 'needs_review', review_reason =
                    CASE WHEN review_reason IS NULL OR review_reason = '' THEN :note
                         ELSE review_reason || '; ' || :note END,
                    updated_at = now()
                WHERE covid = :covid
            """),
            {"covid": covid, "note": (
                "ANCHOR ESCALATION EXHAUSTED (automated): deterministic techniques, Opus 5, and "
                "Fable 5 all failed to produce even a rough candidate position for this tract's "
                "metes-and-bounds description -- needs a human to locate a real tie point. "
                f"Tokens burned across both attempts: {llm_usage_totals['input_tokens']} in / "
                f"{llm_usage_totals['output_tokens']} out / "
                f"{llm_usage_totals['cache_creation_input_tokens']} cache-write / "
                f"{llm_usage_totals['cache_read_input_tokens']} cache-read."
            )},
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
        method="llm_suggested_unconfirmed",
    )
    return {"tier": "geocode_approximate", "committed": False, "approx_result": approx_result,
            "llm_usage": llm_usage_totals}

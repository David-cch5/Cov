"""Agentic LLM escalation for metes-and-bounds anchor resolution -- Tier 1/2
of app.gis.anchor_resolver's tiered anchor pipeline (deterministic techniques
-> Opus 5 -> Fable 5 -> geocode_anchor.py's approximate-placement fallback).

Built with the Claude API's beta Tool Runner (client.beta.messages.tool_runner
+ @beta_tool), not a single forced-tool-choice call like this project's other
LLM call sites (app/parsing/template_fields.py, metes_bounds_llm.py, etc.) --
resolving a genuinely hard anchor needs a real agentic loop, not a bigger
prompt. Confirmed necessary, not a design preference: manually resolving
covid 5838's anchor this way took 51 tool calls and ~53 minutes of real
investigative work (NGS monument lookups the agent had to discover for
itself, several iterative geometric solves, live GIS queries refined across
multiple hypotheses) -- nothing a single-shot prompt could reproduce.

Every tool below is read-only and wraps code that already exists and is
already tested elsewhere in this project -- there is no filesystem/bash
sandbox and no DB-write tool of any kind. The model can only investigate and
report a recommendation via report_anchor_conclusion; app/gis/anchor_resolver.py
is the only thing that ever writes to the database, and only after
independently re-verifying the recommendation's own math (recomputed
closure/area, a real spatial intersection dry-run) -- never trusting a
self-reported confidence number, per CLAUDE.md's own never-fabricate rule.
"""
import json
import math
import os
import re
import time

import anthropic
import requests
from anthropic import beta_tool
from sqlalchemy import text

from app.config import ANTHROPIC_API_KEY
from app.db.session import get_session
from app.gis.classifier import COUNTY_ADAPTERS
from app.gis.state_plane_anchor import FT_PER_DEG_LAT, apply_similarity_complex, solve_similarity_leastsquares
from app.ingestion.walk import TEXTCACHE
from app.parsing.legal_description.metes_bounds import Course, walk_traverse
from app.queue.job_queue import run_with_job_queue

NGS_BOUNDS_URL = "https://geodesy.noaa.gov/api/nde/bounds"
NGS_DATASHEET_URL = "https://geodesy.noaa.gov/cgi-bin/ds_mark.prl"
_NGS_PRE_BLOCK_RE = re.compile(r"<pre>(.*?)</pre>", re.DOTALL | re.IGNORECASE)

# The real cost risk here is a stuck/looping agentic run, not any single
# request -- these are the per-covenant ceilings the user asked for ("cost
# isn't an issue, but not into the thousands per run"). Confirmed against
# covid 5838's own real run (51 tool-call turns, ~53 minutes) with headroom;
# not a documented SDK feature (unlike Task Budgets, whose interaction with
# the beta Tool Runner isn't documented, this is enforced directly in the
# loop below so it can't silently no-op on an unsupported combination).
MAX_ITERATIONS = 80
MAX_WALL_CLOCK_SECONDS = 90 * 60


# Hard caps confirmed necessary by a real failure, not a hypothetical: a
# first live run against covid 4781 hit "prompt is too long: 1152408 tokens >
# 1000000 maximum" after just a few query_gis_parcels calls, each returning
# full polygon geometry for up to 200 parcels -- nothing was bounding total
# conversation size. Attribute-only browsing has no such cost; geometry is
# only ever needed for a small number of specific candidate ties, never a
# bulk listing, so it's capped hard regardless of what's asked for.
_MAX_PARCELS_PER_QUERY = 50
_MAX_PARCELS_WITH_GEOMETRY = 5


def _tool_error(exc: Exception) -> str:
    """Every tool below returns this instead of letting an exception escape.
    Confirmed necessary, not defensive-programming theater: a real live run
    crashed the ENTIRE process (not just that one tool call) when a live
    ArcGIS query failed, because an uncaught exception inside a @beta_tool
    function propagates out of the whole tool_runner loop rather than
    becoming a normal error tool_result the model could see and adapt to."""
    return json.dumps({"error": f"{type(exc).__name__}: {exc}"})


@beta_tool
def query_gis_parcels(
    county_fips: str, where: str = "1=1", envelope: dict | None = None, include_geometry: bool = False,
) -> str:
    """Query live county parcel GIS data. Returns up to 50 matching parcels'
    attributes (APN, owner, acreage, legal description) -- NOT their full
    polygon geometry by default, which is expensive and rarely needed for
    browsing/searching. Pass include_geometry=True only once you've narrowed
    down to a small number of specific candidate parcels you actually need
    real corner coordinates for (geometry is capped to the first 5 results
    regardless, to bound cost).

    Args:
        county_fips: 5-digit county FIPS code (e.g. "48355" for Nueces, TX).
        where: A SQL-style WHERE clause using the COUNTY'S OWN NATIVE field
            names (e.g. "UPPER(legal_desc) LIKE '%BOONE%'" for Nueces) --
            NOT this tool's own output keys like "owner_name_raw", which are
            normalized names this tool produces, not real underlying fields
            you can query by. If unsure of a county's real field names, query
            with where="1=1" and an envelope first and read what comes back
            in recited_legal_description/owner_name_raw to learn the data,
            then narrow with a real spatial envelope rather than guessing at
            field names.
        envelope: Optional {"xmin","ymin","xmax","ymax"} in WGS84 lon/lat to
            restrict results to a bounding box (e.g. a candidate anchor area)
            -- this is usually more reliable than guessing at WHERE field names.
        include_geometry: Only set True for a narrow query you expect to
            match a handful of parcels; see above.
    """
    try:
        adapter = COUNTY_ADAPTERS.get(county_fips)
        if adapter is None or not hasattr(adapter, "iter_parcels"):
            return json.dumps({"error": f"no GIS adapter with iter_parcels for county_fips={county_fips!r}"})
        geom = {**envelope, "spatialReference": {"wkid": 4326}} if envelope else None
        parcels = list(adapter.iter_parcels(where=where, geometry=geom, max_records=_MAX_PARCELS_PER_QUERY))
        out = []
        for i, p in enumerate(parcels):
            row = {
                "apn": p["apn"], "owner_name_raw": p["owner_name_raw"], "acreage": p.get("acreage"),
                "recited_legal_description": p.get("recited_legal_description"),
            }
            if include_geometry and i < _MAX_PARCELS_WITH_GEOMETRY:
                row["geojson"] = p.get("geojson")
            out.append(row)
        result = {"parcels": out, "total_matched": len(parcels)}
        if include_geometry and len(parcels) > _MAX_PARCELS_WITH_GEOMETRY:
            result["note"] = (
                f"geometry included for only the first {_MAX_PARCELS_WITH_GEOMETRY} of {len(parcels)} "
                f"matches -- narrow your `where`/envelope to the specific parcel(s) you need geometry "
                f"for if the one you want wasn't included"
            )
        return json.dumps(result)
    except Exception as exc:
        return _tool_error(exc)


@beta_tool
def query_ngs_datasheet(pid: str | None = None, bbox: dict | None = None) -> str:
    """Look up National Geodetic Survey (NGS) control monuments -- real,
    published, survey-grade reference points. Useful when a deed ties its
    Point of Beginning (or an adjoining survey's own corner) to a named
    monument (e.g. "SF-010", "KNOLL 1934").

    Args:
        pid: An exact NGS Permanent Identifier (PID), e.g. "AH1674", to fetch
            that monument's full datasheet (stamping, datum, coordinates,
            order/accuracy). Pass this OR bbox, not both.
        bbox: {"min_lat","max_lat","min_lon","max_lon"} in WGS84 degrees to
            search for monuments within an area -- returns PIDs + stampings
            + coordinates for you to identify candidates by name, then fetch
            each one's full datasheet individually by pid.
    """
    try:
        if pid:
            resp = requests.get(NGS_DATASHEET_URL, params={"PidBox": pid}, timeout=20)
            resp.raise_for_status()
            m = _NGS_PRE_BLOCK_RE.search(resp.text)
            return m.group(1).strip() if m else resp.text
        if bbox:
            resp = requests.get(NGS_BOUNDS_URL, params={
                "minlon": bbox["min_lon"], "maxlon": bbox["max_lon"],
                "minlat": bbox["min_lat"], "maxlat": bbox["max_lat"],
            }, timeout=20)
            resp.raise_for_status()
            return resp.text
        return json.dumps({"error": "must pass either pid or bbox"})
    except Exception as exc:
        return _tool_error(exc)


@beta_tool
def solve_anchor_similarity(local_ties: list[list[float]], real_ties: list[list[float]], anchor_lat: float) -> str:
    """Solve a 2D similarity transform (rotation + uniform scale + translation)
    mapping local traverse coordinates (feet, arbitrary origin) onto real-world
    ties, and report the scale (length_ratio) so the correspondence can be
    sanity-checked. length_ratio should be within a percent or two of 1.0 --
    a value like 0.84 or 1.19 means the tie points are NOT the same physical
    corners, and the correspondence must be rejected and a different tie
    sought, never forced through.

    Args:
        local_ties: [[x_ft, y_ft], ...] -- at least 2 points from your own
            walked local traverse (see walk_courses).
        real_ties: [[lon, lat], ...] -- the SAME physical corners' real-world
            coordinates, in the same order as local_ties.
        anchor_lat: Approximate latitude of the site, for the flat-earth
            feet-per-degree approximation (accurate enough at a single
            tract's own extent).
    """
    try:
        ft_per_deg_lon = FT_PER_DEG_LAT * math.cos(math.radians(anchor_lat))
        origin_lon, origin_lat = real_ties[0]
        real_ties_ft = [
            ((lon - origin_lon) * ft_per_deg_lon, (lat - origin_lat) * FT_PER_DEG_LAT)
            for lon, lat in real_ties
        ]
        local_pts = [tuple(p) for p in local_ties]
        a, b = solve_similarity_leastsquares(local_pts, real_ties_ft)
        transformed = apply_similarity_complex(local_pts, a, b)
        residuals_ft = [math.hypot(tx - rx, ty - ry) for (tx, ty), (rx, ry) in zip(transformed, real_ties_ft)]
        return json.dumps({
            "length_ratio": abs(a), "rotation_degrees": math.degrees(math.atan2(a.imag, a.real)),
            "residuals_ft": residuals_ft,
        })
    except Exception as exc:
        return _tool_error(exc)


@beta_tool
def walk_courses(courses: list[dict]) -> str:
    """Walk a metes-and-bounds traverse (a list of THENCE bearing/distance
    calls) and return the closed polygon's local vertices (feet, origin at
    the first vertex), perimeter, closure error, closure ratio, and enclosed
    area in acres -- pure deterministic COGO math, never a guess.

    Args:
        courses: [{"ns": "North"|"South", "degrees": int, "minutes": int,
            "seconds": float, "ew": "East"|"West", "distance_ft": float}, ...]
            in true traversal order, starting from the Point of Beginning.
    """
    try:
        course_objs = [
            Course(ns=c["ns"], degrees=c["degrees"], minutes=c["minutes"], seconds=c["seconds"],
                   ew=c["ew"], distance_ft=c["distance_ft"])
            for c in courses
        ]
        result = walk_traverse(course_objs)
        return json.dumps({
            "vertices_ft": result["vertices"], "perimeter_ft": result["perimeter_ft"],
            "closure_error_ft": result["closure_error_ft"], "closure_ratio": result["closure_ratio"],
            "area_acres": result["area_acres"],
        })
    except Exception as exc:
        return _tool_error(exc)


@beta_tool
def get_covenant_context(covid: int) -> str:
    """Read-only lookup of a covenant's own recorded fields and its full
    OCR'd deed text. This is the ONLY way to read covenant data in this
    task -- there is no tool to write to the database; report your findings
    via report_anchor_conclusion instead.

    Args:
        covid: The covenant's own ID number.
    """
    try:
        with get_session() as session:
            row = session.execute(text("""
                SELECT county_fips, declarant_raw, stated_acreage, legal_description_raw,
                       legal_description_type, recording_date, recording_instrument
                FROM covenant WHERE covid = :covid
            """), {"covid": covid}).fetchone()
            if row is None:
                return json.dumps({"error": f"covid {covid} not found"})
            doc = session.execute(text("""
                SELECT relpath FROM covenant_document WHERE covid = :covid AND doc_type = 'original'
            """), {"covid": covid}).fetchone()

        full_text = None
        if doc and doc.relpath:
            # _textcache_final's own naming convention: "<covid>_<basename(relpath)>.json"
            # (confirmed against covid 5838: relpath "5838/5838_D18386.pdf" -> cache file
            # "5838_5838_D18386.pdf.json") -- not the relpath string itself.
            cache_path = os.path.join(TEXTCACHE, f"{covid}_{os.path.basename(doc.relpath)}.json")
            if os.path.exists(cache_path):
                with open(cache_path) as f:
                    full_text = json.load(f).get("text")

        return json.dumps({
            "county_fips": row.county_fips, "declarant_raw": row.declarant_raw,
            "stated_acreage": float(row.stated_acreage) if row.stated_acreage is not None else None,
            "legal_description_raw": row.legal_description_raw,
            "legal_description_type": row.legal_description_type,
            "recording_date": row.recording_date.isoformat() if row.recording_date else None,
            "recording_instrument": row.recording_instrument,
            "full_ocr_text": full_text,
        })
    except Exception as exc:
        return _tool_error(exc)


def _build_system_prompt(covid: int) -> str:
    return (
        f"You are resolving the Point-of-Beginning anchor for a metes-and-bounds covenant "
        f"tract (covid {covid}) that this project's own deterministic code could not anchor "
        "confidently on its own. The project's own non-negotiable rule: NEVER fabricate or "
        "guess a coordinate, distance, or bearing. If you cannot find a real, defensible "
        "anchor after genuinely trying every real angle -- a stated coordinate in the deed "
        "itself, a tie to an already-anchored sibling tract, a tie to a named adjoining "
        "platted parcel, a tie to a real NGS survey monument, a tie to a real, independently "
        "mapped road or right-of-way -- say so plainly. That is a correct, useful outcome, "
        "not a failure on your part, and a human reviewer would much rather see an honest "
        "'could not confidently anchor' than a forced guess.\n\n"
        "Start with get_covenant_context to read the covenant's own recorded fields and its "
        "full deed text. Its Exhibit A may describe more than one distinct tract/parcel in the "
        "same document -- sometimes explicitly as 'TRACT I'/'TRACT II', but sometimes only by "
        "an individually named parcel (e.g. 'Parcel 1201', 'Parcel 1209', 'Phase IV') with no "
        "Roman-numeral tract label at all. In that case, tract_no refers to that tract/parcel's "
        "position in the ORDER the document introduces them (tract_no=1 is whichever is "
        "described first, tract_no=2 the second, etc.) -- identify all of them before deciding "
        "which one you're resolving, and state plainly in your reasoning which specific named "
        "parcel/tract you determined tract_no to be, so that choice itself can be checked "
        "against the deed rather than assumed correct. Use query_gis_parcels to find real, "
        "currently-existing parcels (by "
        "name, survey abstract citation, or a bounding-box spatial query). Use "
        "query_ngs_datasheet if the deed or an adjoining survey ties to a named monument. Use "
        "walk_courses to check any traverse's closure before trusting it -- a real, correct "
        "traverse closes tightly and its area matches the deed's own stated acreage. Use "
        "solve_anchor_similarity to register a local traverse onto real-world coordinates via "
        "2 or more tie points, and take its reported length_ratio seriously: a value more than "
        "a few percent off from 1.0 means your tie-point correspondence is wrong, not that the "
        "county's own data is imprecise -- reject it and look for a different tie rather than "
        "forcing it through.\n\n"
        "Be economical with query_gis_parcels: it returns attributes only by default and caps "
        "results at 50 rows -- pass include_geometry=True only once you've narrowed to the "
        "specific parcel(s) you actually need real corner coordinates for (it's capped to the "
        "first 5 results with geometry regardless, so a broad query won't get you what you "
        "want anyway). Its `where` clause needs the county's own native GIS field names, not "
        "this tool's own output keys (e.g. owner_name_raw is this tool's normalized name for "
        "the data, not a real field you can filter by) -- if unsure of the real field names, "
        "query broadly once to see what comes back, or prefer a spatial envelope over guessing "
        "a field name. Every tool below returns a plain {\"error\": ...} string on failure "
        "instead of raising -- if you see one, that's real, current information to react to "
        "(e.g. retry with a different where-clause or county_fips), not a sign to give up.\n\n"
        "When you are done -- whether you succeeded or could not confidently anchor -- call "
        "report_anchor_conclusion exactly once, as your last action."
    )


def escalate_anchor_to_llm(covid: int, tract_no: int, model: str) -> dict:
    """Runs the agentic anchor-resolution harness at the given model tier
    (app.gis.anchor_resolver calls this once with LLM_MODEL_HARD/Opus 5, and
    again with LLM_MODEL_HARDEST/Fable 5 if Opus's own result doesn't
    independently validate). Returns the model's own self-reported
    conclusion -- never trusted as-is by the caller; app.gis.anchor_resolver
    is responsible for independently re-verifying the closure/area/real-
    parcel-match before ever committing anything to the database. The
    returned dict always carries a "usage" key (summed input/output/cache
    tokens across every turn of this attempt) so real token burn is knowable
    after the fact, not just inferred from elapsed time.
    """
    result_holder: list[dict] = []

    # cache_control on the LAST tool in the tools list (below) caches this
    # entire tool array as a single static prefix -- confirmed necessary, not
    # a micro-optimization: a real run burns 17-39 tool_runner turns, and
    # every turn resends the full system prompt + all 6 tool schemas
    # unchanged (they never vary by covid/tract_no). Without this, that
    # static prefix was being paid for in full on every single turn -- every
    # anchor_agent log this session showed cache_write=0/cache_read=0.
    @beta_tool(cache_control={"type": "ephemeral"})
    def report_anchor_conclusion(
        anchored: bool, confidence: float, reasoning: str,
        anchor_geojson: str | None = None, method: str | None = None,
    ) -> str:
        """Call this ONCE, as your final action, to report your conclusion.
        Do not call any other tool after this one.

        Args:
            anchored: True if you found a real, defensible anchor; False if
                you could not, after genuinely trying (a legitimate, correct
                outcome -- never force a guess just to make this True).
            confidence: 0.0-1.0. Should reflect independent cross-checks
                (e.g. a distance computed two different ways agreeing to a
                fraction of a foot), not just how much effort you spent.
            reasoning: Your full reasoning and the evidence for it, in
                enough detail that a human or another process can
                independently verify your closure/area numbers and your
                tie-point choices.
            anchor_geojson: Required if anchored=True. A GeoJSON MultiPolygon
                (WGS84 lon/lat) for the resolved tract boundary, as a JSON
                string.
            method: Required if anchored=True. One of "stated_coordinate",
                "sibling_tract_tie", "parcel_tie", "ngs_monument_tie", or
                "other".
        """
        result_holder.append({
            "anchored": anchored, "confidence": confidence, "reasoning": reasoning,
            "anchor_geojson": anchor_geojson, "method": method,
        })
        return "recorded"

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    tools = [
        query_gis_parcels, query_ngs_datasheet, solve_anchor_similarity,
        walk_courses, get_covenant_context, report_anchor_conclusion,
    ]

    def _call() -> dict:
        start = time.monotonic()
        iterations = 0
        capped = False
        last_message = None
        # Real billed cost of a multi-turn tool_runner conversation is the SUM
        # of every turn's own usage, not just the last one -- each turn resends
        # the whole growing transcript as input, so input/cache tokens are
        # billed fresh (or at the cache rate) on every single turn. Confirmed
        # against the runner's own internal compaction check (_beta_runner.py),
        # which reads exactly these four fields off each yielded message.
        usage_totals = {
            "input_tokens": 0, "output_tokens": 0,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }
        runner = client.beta.messages.tool_runner(
            model=model,
            max_tokens=16000,
            output_config={"effort": "high"},
            # Second cache_control breakpoint: tools (marked above) + this
            # system block together form the whole static per-turn prefix.
            system=[{
                "type": "text", "text": _build_system_prompt(covid),
                "cache_control": {"type": "ephemeral"},
            }],
            tools=tools,
            messages=[{"role": "user", "content": (
                f"Resolve the metes-and-bounds anchor for covid {covid}, tract {tract_no}. "
                "Start with get_covenant_context."
            )}],
        )
        for message in runner:
            iterations += 1
            last_message = message
            usage = getattr(message, "usage", None)
            if usage is not None:
                usage_totals["input_tokens"] += usage.input_tokens or 0
                usage_totals["output_tokens"] += usage.output_tokens or 0
                usage_totals["cache_creation_input_tokens"] += usage.cache_creation_input_tokens or 0
                usage_totals["cache_read_input_tokens"] += usage.cache_read_input_tokens or 0
            if result_holder:
                break
            elapsed = time.monotonic() - start
            if elapsed > MAX_WALL_CLOCK_SECONDS or iterations > MAX_ITERATIONS:
                capped = True
                break

        print(
            f"  [anchor_agent] covid={covid} tract={tract_no} model={model} "
            f"iterations={iterations} elapsed={time.monotonic() - start:.0f}s "
            f"tokens_in={usage_totals['input_tokens']} tokens_out={usage_totals['output_tokens']} "
            f"cache_write={usage_totals['cache_creation_input_tokens']} "
            f"cache_read={usage_totals['cache_read_input_tokens']}"
        )

        if result_holder:
            return {**result_holder[-1], "usage": usage_totals}

        if capped:
            # A genuinely runaway/looping run (still going at the iteration or
            # wall-clock ceiling) is the one case still worth treating as a
            # hard failure -- run_with_job_queue's retry is a reasonable
            # response to THAT.
            raise RuntimeError(
                "anchor-resolution agent hit its iteration/wall-clock cap without calling "
                f"report_anchor_conclusion (iterations={iterations}, "
                f"elapsed={time.monotonic() - start:.0f}s, model={model}, usage={usage_totals})"
            )

        # Confirmed real, not hypothetical: Fable 5 twice ran to a natural
        # stop (no more tool calls, well under either cap) without ever
        # calling report_anchor_conclusion -- just stopped mid-narration.
        # Treating this the same as the capped case and retrying from scratch
        # is both expensive (a full multi-minute rerun) and pointless (it
        # failed the identical way twice in a row). The model finishing on
        # its own, this far from either bound, with no anchor reported is
        # itself the honest signal this whole harness is built to accept --
        # synthesize the equivalent of a plain "could not confidently anchor"
        # report from whatever it last said, rather than forcing a costly
        # retry that has no reason to behave differently.
        last_text = ""
        if last_message is not None:
            last_text = "".join(
                block.text for block in last_message.content if getattr(block, "type", None) == "text"
            )
        return {
            "anchored": False, "confidence": 0.0, "method": None, "anchor_geojson": None,
            "usage": usage_totals,
            "reasoning": (
                "agent finished without calling report_anchor_conclusion -- treated as an implicit "
                f"'could not confidently anchor' (iterations={iterations}, "
                f"elapsed={time.monotonic() - start:.0f}s, model={model}). Its last message: "
                + (last_text[:2000] if last_text else "(no text content)")
            ),
        }

    # max_attempts=2 (not this module's default 5): a fresh retry restarts the
    # ENTIRE multi-minute agentic run from scratch, which is only worth doing
    # once for something like a transient 529 at the very start -- not worth
    # repeating 5x on an already-expensive run the way a quick network call is.
    return run_with_job_queue(
        _call, job_type="llm_escalation_anchor", covid=covid,
        payload={"tract_no": tract_no, "model": model},
        max_attempts=2, backoff_seconds=(30,),
    )

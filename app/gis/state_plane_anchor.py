"""Confirmed placement for metes-and-bounds tracts whose field notes state an
actual State Plane coordinate for the Point of Beginning and reference State
Plane bearings (not a locally-assumed bearing system) -- e.g. covid 3194's
Tract II gives "X = 3,732,239.49 and Y = 10,104,887.10" on the Texas State
Plane Central Zone, NAD83. That is a real surveyed tie point, not a guess:
converting it through the standard projection and translating the traverse
(whose bearings are already grid-referenced, so no rotation is needed) is
confirmed geometry, not an approximation -- it goes in tract.geom, not
approximate_geom.

Also supports the adjoining-tract case: when a second tract in the same
covenant doesn't state its own coordinate but shares two corners (by matching
bearing+distance) with an already-anchored tract, a 2-point similarity
transform (rotation + translation, no scaling -- both are in feet) registers
its local traverse onto the real world too. Still deterministic geometry, per
CLAUDE.md's "deterministic code for exact work" -- no guessing involved, just
solved from the shared corners.

And the adjoining-PLAT case (anchor_by_adjoining_plat, at the foot of this file):
when the deed's Point of Beginning is a corner of a recorded plat the county
publishes, and its opening courses run WITH that plat's line, the tract can be
placed against the published fabric with no rotation to solve. Generalised out
of the script that anchored covid 4981; see the long comment there for the four
plausible-looking inference shortcuts it does NOT take, each of which produced a
confident answer over 1,000 ft from the truth.
"""
import math

from pyproj import Transformer

from app.gis.ngs import normalize_designation

# Texas State Plane, NAD83, US survey feet -- extend as new zones are needed.
EPSG_BY_TX_ZONE = {
    "north": 2275,
    "north_central": 2276,
    "central": 2277,
    "south_central": 2278,
    "south": 2279,
}


def state_plane_to_lonlat(x: float, y: float, epsg: int) -> tuple[float, float]:
    transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    lon, lat = transformer.transform(x, y)
    return lon, lat


def traverse_to_geojson_state_plane(
    vertices_ft: list[tuple[float, float]], origin_x: float, origin_y: float, epsg: int,
) -> dict:
    """Translate a local (feet, arbitrary-origin) traverse -- whose bearings are
    already State Plane grid-referenced -- onto real State Plane coordinates by
    simple offset (no rotation needed), then project every vertex to lon/lat."""
    transformer = Transformer.from_crs(f"EPSG:{epsg}", "EPSG:4326", always_xy=True)
    origin_local_x, origin_local_y = vertices_ft[0]
    ring = []
    for x, y in vertices_ft:
        real_x = origin_x + (x - origin_local_x)
        real_y = origin_y + (y - origin_local_y)
        lon, lat = transformer.transform(real_x, real_y)
        ring.append([lon, lat])
    # walk_traverse's last vertex is the raw traverse close -- off from vertices[0] by the
    # real (sub-survey-tolerance) closure error, which walk_traverse reports separately as
    # closure_ratio. GEOS requires a ring's start/end points to be bit-identical, not just
    # close, so snap the ring shut here rather than storing an unparseable geometry.
    ring[-1] = ring[0]
    return {"type": "MultiPolygon", "coordinates": [[ring]]}


def solve_2point_similarity(
    local_p1: tuple[float, float], local_p2: tuple[float, float],
    real_p1: tuple[float, float], real_p2: tuple[float, float],
) -> tuple[float, float]:
    """Solve the rotation (radians) and length_ratio (should be ~1.0 -- both
    coordinate sets are in feet) of the rigid-body transform that maps
    local_p1->real_p1 and local_p2->real_p2 exactly (no scaling in principle;
    length_ratio is returned so the caller can sanity-check it's close to 1,
    which is the tell that the two corners really are the same physical
    points). Used to register a tract's own local traverse onto real State
    Plane coordinates via two corners it shares with an already-anchored
    adjoining tract. Apply with apply_similarity_transform, passing local_p1
    and real_p1 as the origin pair."""
    local_dx, local_dy = local_p2[0] - local_p1[0], local_p2[1] - local_p1[1]
    real_dx, real_dy = real_p2[0] - real_p1[0], real_p2[1] - real_p1[1]
    local_len = math.hypot(local_dx, local_dy)
    real_len = math.hypot(real_dx, real_dy)
    length_ratio = real_len / local_len
    rotation = math.atan2(real_dy, real_dx) - math.atan2(local_dy, local_dx)
    return rotation, length_ratio


def solve_similarity_leastsquares(
    local_points: list[tuple[float, float]], real_points: list[tuple[float, float]],
) -> tuple[complex, complex]:
    """Closed-form least-squares fit of a 2D similarity transform (rotation +
    uniform scale + translation) mapping local_points onto real_points, using
    all pairs at once rather than just two -- more robust than
    solve_2point_similarity when three or more corners are shared with an
    already-anchored reference (e.g. several consecutive platted lots along
    one edge of a metes-and-bounds tract). Returns (a, b) such that
    real ~= a*complex(*local) + b for each pair; abs(a) should come out very
    close to 1.0 (both coordinate sets are in feet, no real scale change) --
    a large deviation means the point correspondence is wrong. Apply with
    apply_similarity_complex."""
    local_c = [complex(*p) for p in local_points]
    real_c = [complex(*p) for p in real_points]
    n = len(local_c)
    local_mean = sum(local_c) / n
    real_mean = sum(real_c) / n
    numerator = sum((l - local_mean).conjugate() * (r - real_mean) for l, r in zip(local_c, real_c))
    denominator = sum(abs(l - local_mean) ** 2 for l in local_c)
    a = numerator / denominator
    b = real_mean - a * local_mean
    return a, b


def apply_similarity_complex(vertices_ft: list[tuple[float, float]], a: complex, b: complex) -> list[tuple[float, float]]:
    out = []
    for x, y in vertices_ft:
        real = a * complex(x, y) + b
        out.append((real.real, real.imag))
    return out


def apply_similarity_transform(
    vertices_ft: list[tuple[float, float]], rotation: float, length_ratio: float,
    local_origin: tuple[float, float], real_origin: tuple[float, float],
) -> list[tuple[float, float]]:
    cos_r, sin_r = math.cos(rotation), math.sin(rotation)
    out = []
    for x, y in vertices_ft:
        dx, dy = x - local_origin[0], y - local_origin[1]
        rx = (dx * cos_r - dy * sin_r) * length_ratio
        ry = (dx * sin_r + dy * cos_r) * length_ratio
        out.append((real_origin[0] + rx, real_origin[1] + ry))
    return out


FT_PER_DEG_LAT = 364000.0  # matches app/gis/geocode_anchor.py's own constant


def traverse_to_geojson_via_parcel_ties(
    vertices_ft: list[tuple[float, float]],
    local_ties_ft: list[tuple[float, float]],
    real_ties_lonlat: list[tuple[float, float]],
    anchor_lat: float,
) -> dict:
    """Anchor a metes-and-bounds traverse using real corners of already-
    platted, already-existing parcels the deed's own courses explicitly tie
    to (e.g. "passing... the common corner of Lot 527 and 528") -- a THIRD
    anchoring path alongside a stated State Plane POB coordinate and a
    shared-corner registration onto an already-anchored sibling tract.

    Confirmed real: covid 8245's (Charles Eisterwall Survey) original
    tract.geom -- built from an incomplete _textcache_final copy of its
    deed's Exhibit A, missing the opening courses -- was shifted enough to
    spatially miss its own two real parcels entirely and instead catch 8
    unrelated ones. Re-derived from the deed's complete metes-and-bounds
    text and anchored to 4 real corners of the adjoining Oak Ridge North
    Sec. 5 lots that deed's own course explicitly ties to (matching corners
    found by exact coincidence between adjoining parcels' own polygon
    vertices, then a least-squares fit) -- corrected classified_acreage
    landed within 0.03% of the deed's own stated acreage, up from a tract
    that had matched zero of its real parcels.

    Real drawback, not just a formality: this only ever anchors as
    accurately as the REFERENCE parcels' own GIS geometry. County assessor/
    GIS parcel layers are typically digitized for tax purposes, not survey-
    grade -- they can drift a few feet from the true legal boundary, and if
    the reference parcels were themselves resurveyed or replatted after this
    deed's own date, their current corners may not sit exactly where they
    did when the deed was written. A tight least-squares residual confirms
    internal consistency (the tie points really do form a rigid, undistorted
    set), not that the reference geometry itself is accurate -- there's no
    independent way to detect a systematic error in the county's own data
    from this fit alone. Only usable, too, when the deed actually ties to an
    identifiable already-platted lot; plenty of metes-and-bounds tracts
    don't.

    local_ties_ft / real_ties_lonlat must correspond position-for-position
    (the Nth local tie is the same physical corner as the Nth real tie) and
    should be at least 2, ideally more per solve_similarity_leastsquares'
    own over-determined-fit robustness -- e.g. computed by interpolating
    along a straight course at the deed's own stated cumulative tie
    distances, matched against real corners shared consecutively between
    adjoining platted lots. anchor_lat is used only for the local flat-earth
    degree-per-foot approximation (fine at a single tract's extent, same
    approximation app/gis/geocode_anchor.py's own traverse_to_geojson uses).
    """
    ft_per_deg_lon = FT_PER_DEG_LAT * math.cos(math.radians(anchor_lat))
    origin_lon, origin_lat = real_ties_lonlat[0]
    real_ties_ft = [
        ((lon - origin_lon) * ft_per_deg_lon, (lat - origin_lat) * FT_PER_DEG_LAT)
        for lon, lat in real_ties_lonlat
    ]

    a, b = solve_similarity_leastsquares(local_ties_ft, real_ties_ft)
    transformed_ft = apply_similarity_complex(vertices_ft, a, b)

    ring = []
    for x, y in transformed_ft:
        lon = origin_lon + x / ft_per_deg_lon
        lat = origin_lat + y / FT_PER_DEG_LAT
        ring.append([lon, lat])
    ring.append(ring[0])  # GEOS requires a bit-identical closed ring, not just close
    return {"type": "MultiPolygon", "coordinates": [[ring]]}


# --- NGS monument-tie anchoring -------------------------------------------
# The third deterministic, free anchoring technique, alongside the stated
# coordinate above and the parcel/sibling ties below. Confirmed real on covid
# 5838 (Nueces), whose six SAVE AND EXCEPT tracts each tie a named corner to the
# published monuments SF 010 and KNOLL; four of the six reconstruct the true
# monument-to-monument vector to 0.42 ft in 5,590 ft and 0.007 degrees.
#
# Why this needs no rotation solve: a deed reciting monument ties in this form is
# working on the State Plane grid, so its bearings ARE grid azimuths. That is not
# assumed -- it is exactly what the two-tie cross-check below measures. Once the
# corner is placed, traverse_to_geojson_state_plane translates the traverse onto
# it unrotated.

_TIE_AGREE_FT = 2.0        # residual on a ~5,600 ft inter-monument vector
_TIE_AGREE_DEG = 0.05


def _tie_vector_ft(tie) -> tuple[float, float]:
    """(east, north) offset FROM the tract corner TO the monument."""
    az = math.radians(tie.azimuth_degrees)
    return tie.distance_ft * math.sin(az), tie.distance_ft * math.cos(az)


def verify_monument_zone(monument) -> float | None:
    """Reproject the monument's own published lat/lon into the State Plane zone
    its own datasheet names, and return how far that lands from the datasheet's
    own published grid coordinates.

    This is the guard on the zone mapping: a wrong zone is off by miles, so a
    sub-foot agreement proves NGS_SPC_ZONE_TO_EPSG's entry for this zone is
    right. Returns None when the datasheet carried no grid coordinates."""
    if monument.epsg is None or monument.spc_north_sft is None:
        return None
    transformer = Transformer.from_crs("EPSG:4269", f"EPSG:{monument.epsg}", always_xy=True)
    east, north = transformer.transform(monument.lon, monument.lat)
    return math.hypot(east - monument.spc_east_sft, north - monument.spc_north_sft)


def cross_check_monument_ties(tie_a, mon_a, tie_b, mon_b) -> dict:
    """Rebuild the monument-to-monument vector from the deed's OWN two ties and
    compare it against the monuments' published grid coordinates.

    Both ties start at the same tract corner, so subtracting them cancels that
    unknown corner entirely: what is left depends only on the two readings, and
    is checkable against published truth without placing anything first.

    When the two disagree, the quadrant-letter hypothesis is tested explicitly:
    if reversing exactly ONE letter of ONE tie reconciles them, that tie carries
    the same East/West defect repair_quadrant_by_closure recovers in the courses,
    and the OTHER tie is the sound one. Confirmed real on covid 5838, whose
    5.800 and 0.554 acre tracts recite KNOLL as bearing South 20°44'36" WEST
    where the geometry requires East -- distance agreeing to 0.03 ft and the
    angle to under three arc-minutes, so only the letter is wrong.
    """
    ax, ay = _tie_vector_ft(tie_a)
    bx, by = _tie_vector_ft(tie_b)
    deed_e, deed_n = ax - bx, ay - by          # b -> a, corner cancels
    true_e = mon_a.spc_east_sft - mon_b.spc_east_sft
    true_n = mon_a.spc_north_sft - mon_b.spc_north_sft

    def residual(de, dn):
        d_err = math.hypot(de, dn) - math.hypot(true_e, true_n)
        a_err = (math.degrees(math.atan2(de, dn)) - math.degrees(math.atan2(true_e, true_n))) % 360
        return d_err, (a_err - 360 if a_err > 180 else a_err)

    dist_err, az_err = residual(deed_e, deed_n)
    if abs(dist_err) <= _TIE_AGREE_FT and abs(az_err) <= _TIE_AGREE_DEG:
        return {"agree": True, "distance_error_ft": dist_err, "azimuth_error_deg": az_err,
                "trusted": None, "corrected": None}

    repairs = []
    for label, tie, other in (("b", tie_b, tie_a), ("a", tie_a, tie_b)):
        for field in ("ns", "ew"):
            flipped = tie.flipped(field)
            fx, fy = _tie_vector_ft(flipped)
            ox, oy = _tie_vector_ft(other)
            de, dn = (ox - fx, oy - fy) if label == "b" else (fx - ox, fy - oy)
            d_err, a_err = residual(de, dn)
            if abs(d_err) <= _TIE_AGREE_FT and abs(a_err) <= _TIE_AGREE_DEG:
                repairs.append((label, field, d_err, a_err))

    if len(repairs) == 1:
        label, field, d_err, a_err = repairs[0]
        return {"agree": False, "distance_error_ft": dist_err, "azimuth_error_deg": az_err,
                "trusted": "a" if label == "b" else "b",
                "corrected": {"tie": label, "field": field,
                              "distance_error_ft": d_err, "azimuth_error_deg": a_err}}
    return {"agree": False, "distance_error_ft": dist_err, "azimuth_error_deg": az_err,
            "trusted": None, "corrected": None}


def anchor_by_ngs_monument_tie(vertices_ft, ties, monuments, corner_index: int = 0) -> dict:
    """Place a local, grid-referenced traverse using a deed's NGS monument tie.

    vertices_ft -- walk_traverse output, local feet.
    ties        -- MonumentTie list for THIS tract (see extract_ngs_monument_ties).
    monuments   -- {normalized designation: NgsMonument} (see find_monuments).
    corner_index-- which traverse vertex the ties run from; 0 (the Point of
                   Beginning) in every deed seen so far, but stated explicitly
                   rather than assumed, because a deed may tie any named corner.

    Returns the placement plus everything needed to judge it -- which tie was
    used, the zone check, and the two-tie cross-check. Raises ValueError only
    when there is nothing usable to place from; a tie that cannot be
    cross-checked still places, flagged `verified=False`, so the caller decides
    rather than this silently committing an unverified anchor.
    """
    usable = [(t, monuments[key]) for t in ties
              if (key := normalize_designation(t.designation)) in monuments
              and monuments[key].spc_north_sft is not None]
    if not usable:
        raise ValueError(f"no tie resolved to a monument with grid coordinates: "
                         f"{[t.designation for t in ties]}")

    check = None
    if len(usable) >= 2:
        (ta, ma), (tb, mb) = usable[0], usable[1]
        check = cross_check_monument_ties(ta, ma, tb, mb)
        if check["agree"]:
            usable.sort(key=lambda pair: pair[1].quality_rank, reverse=True)
        elif check["trusted"] in ("a", "b"):
            keep = ta if check["trusted"] == "a" else tb
            usable = [p for p in usable if p[0] is keep] + [p for p in usable if p[0] is not keep]
        else:
            # Nothing distinguishes them -- fall back to the better-published
            # monument (a later NAD83 realization, mark reported found).
            usable.sort(key=lambda pair: pair[1].quality_rank, reverse=True)

    tie, monument = usable[0]
    zone_error_ft = verify_monument_zone(monument)
    east, north = _tie_vector_ft(tie)
    corner_east = monument.spc_east_sft - east      # reverse: tie points AT the monument
    corner_north = monument.spc_north_sft - north

    local = list(vertices_ft)
    if corner_index:
        cx, cy = local[corner_index]
        local = [(x - cx + local[0][0], y - cy + local[0][1]) for x, y in local]

    return {
        "geojson": traverse_to_geojson_state_plane(local, corner_east, corner_north, monument.epsg),
        "monument": monument.designation, "pid": monument.pid, "epsg": monument.epsg,
        "tie_used": f"{tie.ns} {tie.degrees:.0f}-{tie.minutes:02.0f}-{tie.seconds:02.0f} "
                    f"{tie.ew} {tie.distance_ft:.2f} ft",
        "corner": tie.corner, "zone_check_ft": zone_error_ft, "cross_check": check,
        "verified": bool(check and (check["agree"] or check["trusted"]))
                    and (zone_error_ft is not None and zone_error_ft < 1.0),
    }


# --- adjoining-plat anchoring ----------------------------------------------
#
# The technique that placed covid 4981's Andrew S. Young Survey tract, generalised
# out of the one-off script that did it. It applies whenever a deed's Point of
# Beginning is a corner OF a recorded plat the county already publishes, which is
# the ordinary case for suburban acreage carved out of, or adjoining, a
# subdivision:
#
#   "BEGINNING at a 1/2 inch iron rod found at the Easterly Northeast corner of
#    The Heights at Westridge Phase I ... a point in the West line of
#    Independence Parkway; THENCE, South 88 degrees 11 minutes 53 seconds West,
#    ... WITH A NORTH LINE OF SAID The Heights at West ridge Phase I ..."
#
# Two things make it deterministic rather than a search for a resemblance.
#
# NO ROTATION IS SOLVED. A deed running WITH a plat's boundary is on that plat's
# basis of bearings, so the only unknown is a translation -- two parameters,
# recovered by fit rather than assumed. The assumption is not taken on faith:
# `rotation_probe_deg` re-fits at a spread of small rotations and REFUSES if a
# rotated fit is materially better, the signature of a plat whose bearings are
# not grid-referenced.
#
# THE CONTACT COURSES ARE READ FROM THE DEED, NOT GUESSED. `contact_indices`
# names the vertices the deed itself says lie on the adjoiner, and it is a
# required argument with no default. Three attempts at inferring them instead
# all failed against 4981, each in a way worth keeping:
#
#   scoring every vertex (distance capped so far ones don't drag the fit)
#     preferred placements INSIDE the plat, where everything is near some
#     boundary -- 2,338 ft off
#   scoring the best run of 4 consecutive vertices fitted four SHORT closing
#     courses onto a lot line, residual 0.10 ft, 1,379 ft off
#   requiring that run to span 200 ft moved it to a long STRAIGHT run, which
#     slides along a straight boundary fitting perfectly the whole way
#
# All three are the same error: inferring from geometry alone a fact the document
# states outright. The deed says which courses run with the plat; a search that
# re-derives it can only be less reliable than reading it, and CLAUDE.md already
# says to read the full legal description before reaching for GIS trial-and-error.

_CONTACT_TOL_FT = 15.0          # within this, a vertex counts as ON the adjoiner
_MIN_CONTACT_VERTICES = 3       # a LINE of contact -- one touching corner proves nothing
_MIN_CONTACT_SPAN_FT = 200.0    # and a run long enough to fix position along it
_MAX_OVERLAP_SQFT = 2000.0      # a tract cannot lie on top of the plat it adjoins
_MAX_RMS_FT = 15.0              # measured against a tax-parcel fabric, not a survey plat
_MAX_SEEDS = 40000            # every boundary vertex of a big plat, not a sample
_MAX_REFINED_CANDIDATES = 800  # bound on the work, not a shortlist of the promising
_REFINE_PASSES = ((10.0, 20.0), (2.0, 4.0), (0.4, 0.8), (0.05, 0.1))


class _BoundaryIndex:
    """Nearest-segment lookup over the adjoiner's boundary.

    The fit evaluates tens of thousands of candidate placements, each measuring
    the contact vertices against the plat. Straight `boundary.distance(point)`
    is linear in the plat's vertex count -- on an 82-acre union of 398 tax
    parcels that turns the search into hundreds of millions of segment tests.
    An R-tree over the individual segments makes each lookup logarithmic.
    """

    def __init__(self, polygon):
        from shapely import STRtree
        from shapely.geometry import LineString

        geoms = getattr(polygon, "geoms", None) or [polygon]
        self.segments = []
        for g in geoms:
            for ring in [g.exterior] + list(g.interiors):
                pts = list(ring.coords)
                self.segments.extend(LineString([pts[i], pts[i + 1]])
                                     for i in range(len(pts) - 1))
        self.tree = STRtree(self.segments)

    def distance(self, point) -> float:
        return self.segments[self.tree.nearest(point)].distance(point)


def _boundary_seed_points(polygon) -> list[tuple[float, float]]:
    """Every distinct boundary vertex of the adjoiner, decimated to a bounded
    count. These are the candidate corners the deed's POB might name.

    Enumerating them is the point. Naming a corner in English ("the Easterly
    Northeast corner") is not the same as knowing which vertex of a published
    polygon it is, and picking one by eye is what went wrong twice on 4981 --
    first the northernmost point of the plat's east extreme, which put the tract
    ON TOP of the plat, then a bearing check against that same wrong corner,
    which missed by 20 degrees and was blamed on the plat's bearings.
    """
    geoms = getattr(polygon, "geoms", None) or [polygon]
    coords = []
    for g in geoms:
        for ring in [g.exterior] + list(g.interiors):
            coords.extend(list(ring.coords)[:-1])
    if len(coords) > _MAX_SEEDS:
        step = len(coords) / _MAX_SEEDS
        coords = [coords[int(i * step)] for i in range(_MAX_SEEDS)]
    return coords


def _rotate(ring, degrees: float):
    if not degrees:
        return list(ring)
    theta = math.radians(degrees)
    c, s = math.cos(theta), math.sin(theta)
    ox, oy = ring[0]
    return [(ox + (x - ox) * c - (y - oy) * s, oy + (x - ox) * s + (y - oy) * c)
            for x, y in ring]


def _contact_rms(ring, dx, dy, index, contact_indices) -> float:
    from shapely.geometry import Point

    return math.sqrt(sum(index.distance(Point(ring[i][0] + dx, ring[i][1] + dy)) ** 2
                         for i in contact_indices) / len(contact_indices))


def _contact_span_ft(ring, contact_indices) -> float:
    ordered = sorted(contact_indices)
    return sum(math.dist(ring[ordered[k]], ring[ordered[k + 1]])
               for k in range(len(ordered) - 1))


def _fit_translation(ring, adjoiner, index, seeds, contact_indices, max_overlap_sqft,
                     prepared_interior) -> tuple | None:
    """Coarse multi-seed pass, then local refinement of the best few. Returns
    (dx, dy, rms, overlap_sqft), or None if no placement stayed out of the
    adjoiner."""
    from shapely.geometry import Point, Polygon

    def outside(dx, dy) -> bool:
        return not any(prepared_interior.contains(Point(x + dx, y + dy)) for x, y in ring)

    def admissible(dx, dy) -> bool:
        """Exterior AND not overlapping. Overlap has to constrain the SEARCH, not
        just filter its output: refinement minimises the residual, and it can
        always shave a little off by sliding the tract into the plat. On 4981 the
        true corner refined itself straight through the overlap cap and was then
        thrown away, handing the answer to a worse-fitting placement 1,194 ft off."""
        if not outside(dx, dy):
            return False
        return (Polygon([(x + dx, y + dy) for x, y in ring])
                .intersection(adjoiner).area <= max_overlap_sqft)

    # Rank every candidate corner on the cheap contact residual FIRST, and only
    # then pay for the interior test. Decimating the seed list instead is what
    # kept 4981's true corner out of the running: an 82-acre plat has thousands
    # of boundary vertices, sampling 600 of them missed the one the deed names,
    # and no local refinement recovers a corner it never started near.
    ox, oy = ring[contact_indices[0]]
    ranked = sorted((_contact_rms(ring, sx - ox, sy - oy, index, contact_indices),
                     sx - ox, sy - oy) for sx, sy in seeds)
    # Refine EVERY candidate, and judge NONE of them before refining. Two
    # filters that seemed obviously safe each discarded 4981's true corner:
    # keeping only the top 20 by coarse residual (it ranked below that, yet
    # refined to 6.36 ft against the winner's 7.51 ft), and requiring the raw
    # seed to sit outside the plat (a corner of the plat, with the traverse
    # hung off it unrefined, has a vertex inside -- of course it does). A seed
    # is a starting point, not a placement; admissibility is enforced on where
    # the refinement LANDS.
    coarse = ranked[:_MAX_REFINED_CANDIDATES]

    best = None
    for _, dx0, dy0 in coarse:
        dx, dy = dx0, dy0
        for step, span in _REFINE_PASSES:
            n = int(span / step)
            options = [(_contact_rms(ring, dx + i * step, dy + j * step, index, contact_indices),
                        dx + i * step, dy + j * step)
                       for i in range(-n, n + 1) for j in range(-n, n + 1)
                       if admissible(dx + i * step, dy + j * step)]
            if options:
                _, dx, dy = min(options)
        overlap = Polygon([(x + dx, y + dy) for x, y in ring]).intersection(adjoiner).area
        # Overlap is a hard fact about the placement, not a tie-breaker: a tract
        # cannot lie on top of the plat it adjoins, however well its edges line up.
        if overlap > max_overlap_sqft:
            continue
        rms = _contact_rms(ring, dx, dy, index, contact_indices)
        if best is None or rms < best[2]:
            best = (dx, dy, rms, overlap)
    return best


def anchor_by_adjoining_plat(
    vertices_ft: list[tuple[float, float]], adjoiner_polygon,
    contact_indices: list[int], *,
    epsg: int | None = None, stated_acres: float | None = None,
    max_overlap_sqft: float = _MAX_OVERLAP_SQFT, contact_tol_ft: float = _CONTACT_TOL_FT,
    min_contact_vertices: int = _MIN_CONTACT_VERTICES,
    min_contact_span_ft: float = _MIN_CONTACT_SPAN_FT, max_rms_ft: float = _MAX_RMS_FT,
    rotation_probe_deg: float = 2.0, rotation_probe_step: float = 0.25,
    max_rotation_deg: float = 0.5,
) -> dict:
    """Anchor a local traverse against an adjoining plat's published footprint.

    `vertices_ft` is a walk_traverse traverse in feet whose bearings are on the
    adjoiner's own basis; `adjoiner_polygon` is a shapely polygon of the
    adjoining plat in the SAME State Plane feet CRS (`epsg`, needed only to
    return lon/lat). `contact_indices` are the traverse vertices the DEED says
    lie on that plat -- vertex i is the start of course i, so a deed whose first
    four courses run with the adjoiner gives [0, 1, 2, 3, 4].

    Returns a dict that always says whether it anchored and, when it did not,
    why -- never a forced placement. `anchored` True means every check passed: a
    long enough line of contact, no overlap into the plat, a residual consistent
    with a tax-parcel fabric, an area agreeing with the deed's stated acreage
    when one is given, and no rotation fitting better than grid.
    """
    from shapely.geometry import Point, Polygon
    from shapely.prepared import prep

    out = {"method": "adjoining_plat_tie", "anchored": False, "reason": None,
           "epsg": epsg, "stated_acres": stated_acres,
           "contact_indices": list(contact_indices)}
    if len(vertices_ft) < 4 or adjoiner_polygon is None or adjoiner_polygon.is_empty:
        out["reason"] = "need a closed traverse and a non-empty adjoiner footprint"
        return out

    ring = list(vertices_ft[:-1]) if vertices_ft[0] == vertices_ft[-1] else list(vertices_ft)
    contact_indices = sorted({i for i in contact_indices if 0 <= i < len(ring)})
    if len(contact_indices) < min_contact_vertices:
        out["reason"] = (f"{len(contact_indices)} contact vertex/vertices named -- at least "
                         f"{min_contact_vertices} are needed to fix a line, not a point")
        return out
    span = _contact_span_ft(ring, contact_indices)
    out["contact_span_ft"] = span
    if span < min_contact_span_ft:
        out["reason"] = (f"the courses running with the adjoiner span {span:.0f} ft, under the "
                         f"{min_contact_span_ft:.0f} ft needed to fix position along its line")
        return out

    index = _BoundaryIndex(adjoiner_polygon)
    seeds = _boundary_seed_points(adjoiner_polygon)
    if not seeds:
        out["reason"] = "adjoiner footprint has no boundary vertices to try as corners"
        return out
    # A vertex this far inside the plat is not "on its line" by any tolerance.
    # Eroding first means a vertex sitting ON the boundary never reads as interior.
    interior = prep(adjoiner_polygon.buffer(-contact_tol_ft))

    fit = _fit_translation(ring, adjoiner_polygon, index, seeds, contact_indices,
                           max_overlap_sqft, interior)
    if fit is None:
        out["reason"] = ("every candidate corner placed the tract inside the adjoining plat "
                         "or overlapping it -- no exterior placement found")
        return out
    dx, dy, rms, overlap = fit

    # Is grid north actually right? Re-fit at a spread of small rotations; if one
    # fits materially better, this plat's bearings are not grid-referenced and a
    # translation-only anchor would be wrong however tidy its residual looks. The
    # probe re-seeds from the solved placement rather than re-running every corner:
    # the question is whether THIS placement wants to be rotated.
    probe = [(rms, 0.0)]
    steps = int(rotation_probe_deg / rotation_probe_step)
    seed_xy = [(ring[contact_indices[0]][0] + dx, ring[contact_indices[0]][1] + dy)]
    for k in range(-steps, steps + 1):
        theta = k * rotation_probe_step
        if theta == 0:
            continue
        r = _fit_translation(_rotate(ring, theta), adjoiner_polygon, index, seed_xy,
                             contact_indices, max_overlap_sqft, interior)
        if r is not None:
            probe.append((r[2], theta))
    best_rms, best_theta = min(probe)

    placed = Polygon([(x + dx, y + dy) for x, y in ring])
    contact = sum(1 for x, y in ring
                  if index.distance(Point(x + dx, y + dy)) <= contact_tol_ft)
    area_acres = placed.area / 43560.0
    out.update({
        "dx": dx, "dy": dy, "rms_ft": rms,
        "pob_xy": (ring[0][0] + dx, ring[0][1] + dy),
        "contact_vertices": contact, "total_vertices": len(ring),
        "overlap_sqft": overlap, "area_acres": area_acres,
        "rotation_probe_deg": rotation_probe_deg, "best_rotation_deg": best_theta,
        "rotation_rms_ft": best_rms,
    })
    if stated_acres:
        out["area_delta_pct"] = (area_acres - stated_acres) / stated_acres * 100.0

    if abs(best_theta) > max_rotation_deg and best_rms < rms * 0.7:
        out["reason"] = (f"a {best_theta:+.2f} deg rotation fits materially better "
                         f"({best_rms:.2f} ft vs {rms:.2f} ft) -- this plat's bearings are not "
                         f"grid-referenced, so a translation-only tie does not apply")
        return out
    if rms > max_rms_ft:
        out["reason"] = f"fit residual {rms:.2f} ft exceeds {max_rms_ft:.0f} ft"
        return out
    if overlap > max_overlap_sqft:
        out["reason"] = (f"placement overlaps the adjoiner by {overlap:,.0f} sq ft -- a tract "
                         f"cannot lie on top of the plat it adjoins")
        return out
    if stated_acres and abs(out["area_delta_pct"]) > 3.0:
        out["reason"] = (f"area {area_acres:.3f} ac is {out['area_delta_pct']:+.2f}% off the "
                         f"deed's stated {stated_acres} ac")
        return out

    out["anchored"] = True
    if epsg:
        out["geojson"] = traverse_to_geojson_state_plane(
            list(vertices_ft), ring[0][0] + dx, ring[0][1] + dy, epsg)
    return out

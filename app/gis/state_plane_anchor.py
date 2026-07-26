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
"""
import math

from pyproj import Transformer

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

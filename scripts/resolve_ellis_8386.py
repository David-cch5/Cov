"""One-off resolution for covid 8386 (Ellis County, "Red Oak Coyote Ridge, Ltd.").

The recorded instrument (Book 2530, Page 135-152 / Instrument No. 1019671, Ellis
County) is 18 pages -- our local corpus copy (8386/8386_D1372.pdf) only has the
first 11 (the Declaration text through the notary acknowledgment). Pages 12-18
(Exhibit A) were missing from our records entirely; recovered by viewing the
Ellis County Clerk's own Acclaim document image viewer directly (guest access,
no login required) at ellisccktxpublicsearch.us, which shows the true 18-page
document (36 scanned images -- every page duplicated) including the exhibit
pages our copy lacks. See CLAUDE.md's "no Exhibit A" escalation path.

Courses below were hand-transcribed directly from those page images (not OCR,
not LLM-extracted) since a human/vision reading of the source is already the
most reliable input available -- re-routing through the regex or LLM extractor
would only add a second lossy step. Every course's math (arc length vs radius +
delta, for the 8 curve calls in Tract 1) was independently checked against
R*delta(rad); one apparent transcription slip (course #6's arc length) was
corrected from the misread "2008.74" to "208.74" ft on that basis -- flagged in
the notes, not silently fixed.

Encumbered land is 3 tracts:
  Tract 1: 70.44 ac metes-and-bounds, Abstract 836 (George C. Parks Survey).
           Traverse closes essentially exactly (closure error 0.004 ft over an
           8159 ft perimeter, computed area 70.34 ac vs deed-stated 70.44 ac,
           -0.15%) -- high confidence the transcription is correct. Resolved
           to tract.approximate_geom (Nominatim road-name anchor; Ellis County
           has no queryable parcel API to tie the POB to a real corner).
  Tract 2: 1.62 ac metes-and-bounds, Abstract 836, owned (at recording) by
           HTJ & C Trust rather than the declarant directly. THE TRAVERSE DOES
           NOT CLOSE: 194.7 ft error over an 892 ft perimeter (ratio ~1:5).
           Re-read the source image twice, independently, digit by digit --
           both reads agree on every bearing/distance, and systematically
           trying single-field sign/value flips never reproduces a clean
           closure -- so this is not a transcription error on our end; it
           looks like a genuine drafting/closure error in the 2010 recorded
           exhibit itself (or an error in the underlying deed it ties to,
           Volume 1045 Page 1932). NOT geometrized -- writing any polygon from
           a traverse this far out of closure would be fabricating a shape,
           not representing one. Flagged for human review; would need the
           referenced deed or an actual survey to resolve.
  Tract 3: specific platted lots, Hickory Creek Estates Ph I (Cabinet H, Slides
           130-132) -- NOT resolved here: Ellis CAD (elliscad.com) has no
           publicly queryable ArcGIS REST service (custom Google-Maps-based
           viewer instead; esearch.elliscad.com refused connection), so no GIS
           adapter exists for Ellis County yet. Left un-geometried pending
           either a working Ellis CAD API or a manual county-provided extract.
           One lot/block entry in the county's own certified copy is redacted/
           illegible (between "17B" and "24B") -- recorded as a gap, not guessed.

Only Tract 1 gets a tract-table row: the table's own
tract_geom_or_approximate_geom_present CHECK constraint requires every row to
carry at least one geometry, and neither Tract 2 (bad closure) nor Tract 3 (no
GIS source at all) has one we can honestly write -- both are documented in
covenant.review_reason instead of forced into a row with fabricated placement.

Usage: python3 scripts/resolve_ellis_8386.py
"""
import sys

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import get_session
from app.gis.geocode_anchor import geocode, traverse_to_geojson
from app.parsing.legal_description.metes_bounds import Course, walk_traverse

COVID = 8386

# --- Tract 1: 70.44 acre tract, Abstract 836 -------------------------------
TRACT1_COURSES = [
    Course("South", 0, 10, 4, "East", 1421.30),
    Course("South", 0, 17, 53, "West", 724.82),
    Course("North", 90, 0, 0, "East", 161.02),
    Course("South", 0, 26, 20, "West", 519.71),
    Course("North", 89, 31, 14, "West", 198.86),
    Course("North", 84, 16, 30, "West", 208.45, is_curve=True, radius_ft=1140.00,
           delta_deg=10 + 29/60 + 28/3600, curve_direction="right",
           arc_length_ft=208.74),  # transcribed "2008.74" -- corrected: R*delta(rad) = 1140*0.18309 = 208.7 ft, consistent with 208.74, not 2008.74
    Course("North", 0, 27, 17, "East", 126.26),
    Course("North", 89, 31, 20, "West", 132.89),
    Course("South", 0, 31, 51, "West", 93.25),
    Course("North", 63, 50, 1, "West", 329.25, is_curve=True, radius_ft=1140.00,
           delta_deg=16 + 36/60 + 21/3600, curve_direction="right", arc_length_ft=330.40),
    Course("North", 55, 30, 39, "West", 622.73),
    Course("North", 60, 47, 46, "West", 292.92, is_curve=True, radius_ft=1590.00,
           delta_deg=10 + 34/60 + 14/3600, curve_direction="left", arc_length_ft=293.34),
    Course("North", 25, 21, 37, "East", 311.57),
    Course("North", 5, 26, 5, "East", 381.70, is_curve=True, radius_ft=560.00,
           delta_deg=39 + 51/60 + 4/3600, curve_direction="left", arc_length_ft=389.50),
    Course("North", 14, 29, 28, "West", 251.31),
    Course("North", 5, 38, 47, "East", 440.67, is_curve=True, radius_ft=640.00,
           delta_deg=40 + 16/60 + 31/3600, curve_direction="right", arc_length_ft=449.88),
    Course("North", 25, 47, 3, "East", 392.85),
    Course("North", 19, 21, 37, "East", 217.05, is_curve=True, radius_ft=970.00,
           delta_deg=12 + 50/60 + 51/3600, curve_direction="left", arc_length_ft=217.50),
    # "THENCE along the north line ... the following bearing and distances:"
    Course("South", 75, 39, 0, "East", 60.02),
    Course("South", 13, 37, 56, "West", 22.05),
    Course("South", 75, 41, 6, "East", 106.00),
    Course("South", 14, 53, 21, "West", 25.27),
    Course("South", 74, 28, 24, "East", 20.00),
    Course("North", 52, 42, 29, "East", 48.35, is_curve=True, radius_ft=40.00,
           delta_deg=74 + 21/60 + 48/3600, curve_direction="right", arc_length_ft=51.92),
    Course("North", 89, 53, 23, "East", 676.64),
    Course("South", 39, 8, 46, "East", 31.32),
    Course("North", 50, 51, 14, "East", 50.00),
    Course("North", 39, 8, 46, "West", 33.02),
    Course("North", 19, 39, 25, "West", 83.41, is_curve=True, radius_ft=125.00,
           delta_deg=38 + 58/60 + 42/3600, curve_direction="right", arc_length_ft=85.04),
    Course("North", 0, 10, 4, "West", 16.39),
    Course("North", 89, 49, 56, "East", 160.00),
]
TRACT1_STATED_ACRES = 70.44

# --- Tract 2: 1.62 acre tract, Abstract 836 (owned by HTJ & C Trust) -------
TRACT2_COURSES = [
    Course("North", 87, 3, 28, "East", 139.73),
    Course("South", 0, 13, 6, "East", 260.96),
    Course("South", 89, 46, 54, "West", 206.23),
    Course("North", 26, 56, 6, "West", 284.72),
]
EXHIBIT_A_TEXT = """\
TRACT 1 (70.44 acres):
Being a 70.44 acre tract of land situated in the George C. Parks Survey, Abstract No. 836,
in the City of Red Oak, Ellis County, Texas, and being that same tract of land described in
deed to Red Oak Coyote Ridge, LTD as recorded in Volume 2038, Page 1358, of the Deed Records
of Ellis County, Texas, and being more particularly described by metes and bounds as follows:

BEGINNING at a point found at the northeast corner of said Red Oak Coyote Ridge, LTD tract,
and being the southeast corner of Highland Meadows No. 1 Addition, an addition in the City of
Red Oak, according to the plat thereof recorded in Cabinet A, Slide 432 & 441, of the Plat
Records of Ellis County, Texas, and being in the west line of a tract of land described in
deed to William Lynch as recorded in Volume 564, Page 919, of the Deed Records of Ellis
County, Texas;

THENCE S00°10'04"E, along the east line of said Red Oak Coyote Ridge, LTD tract, and the west
line of said Lynch tract, a distance of 1421.30 feet to a point for corner, said point being
the southwest corner of said Lynch tract;

THENCE S00°17'53"W, along the east line of said Red Oak Coyote Ridge, LTD tract, a distance
of 724.82 feet to a point for corner;

THENCE N90°00'00"E, along the easterly lie of said Red Oak Coyote Ridge, LTD tract, a
distance of 161.02 feet to a point for corner, said point being the northwest corner of a
tract of land described in deed to Red Oak Apartment as recorded in Volume 637, Page 494, of
the Deed Records of Ellis County, Texas;

THENCE S00°26'20"W, along the easterly line of said Red Oak Coyote Ridge, LTD tract and the
west line of said Red Oak Apartments tract, a distance of 519.71 feet to a point for corner,
said point being the southeast corner of said Red Oak Coyote Ridge, LTD tract, and being in
the north right-of-way line of Red Oak Road;

THENCE N89°31'14"W, along the south line of said Red Oak Coyote Ridge, LTD tract and the
north right-of-way line of said Red Oak Road, a distance of 198.86 feet to a point for
corner, said point being the beginning of a curve to the right having a radius of 1140.00,
and a delta angle of 10°29'28";

THENCE along the south line of said Red Oak Coyote Ridge, LTD tract and the north
right-of-way line of said Red Oak Road and said curve to the right, an arc distance of
208.74 feet [transcribed as "2008.74"; corrected via R*delta(rad)=1140*0.18309=208.7 ft,
consistent with 208.74], and a chord bearing and distance of N84°16'30"W, 208.45 feet to a
point for corner;

THENCE N00°27'17"E, a distance of 126.26 feet to a point for corner;

THENCE N89°31'20"W, a distance of 132.89 feet to a point for corner;

THENCE S00°31'51"W, a distance of 93.25 feet to a point for corner in the south line of said
Red Oak Coyote Ridge, LTD tract, and the north right-of-way line of said Red Oak Road, and
being the beginning of a curve to the right having a radius of 1140.00, and a delta angle of
16°36'21";

THENCE along the south line of said Red Oak Coyote Ridge, LTD tract, and the north
right-of-way line of said Red Oak Road and along said curve to the right an arc distance of
330.40 feet and a chord bearing and distance of N63°50'01"W, 329.25 feet to a point for
corner;

THENCE N55°30'39"W, along the south line of said Red Oak Coyote Ridge, LTD tract, and the
north right-of-way line of said Red Oak Road, a distance of 622.73 feet to a point for
corner, said point being the beginning of a curve to the left having a radius of 1590.00
feet, and a delta angle of 10°34'14";

THENCE along the south line of said Red Oak Coyote Ridge, LTD tract, and the north
right-of-way line of said Red Oak Road an arc distance of 293.34 feet, and a chord bearing
and distance of N60°47'46"W, 292.92 feet to a point for corner said point being the southwest
corner of said Red Oak Coyote Ridge, LTD tract;

THENCE N25°21'37"E, along the west line of said Red Oak Coyote Ridge, LTD tract, a distance
of 311.57 feet to a point for corner, said point being the beginning of a curve to the left
having a radius of 560.00, a delta angle of 39°51'04";

THENCE along the west line of said Red Oak Coyote Ridge, LTD tract and said curve to the
left, an arc distance of 389.50 feet, and a chord bearing and distance of N05°26'05"E, 381.70
feet to a point for corner;

THENCE N14°29'28"W, along the west line of said Red Oak Coyote Ridge, LTD tract, a distance
of 251.31 feet to a point for corner, said point being the beginning of a curve to the right
having a radius of 640.00 feet, and a delta angle of 40°16'31";

THENCE along the west line of said Red Oak Coyote Ridge, LTD tract and said curve to the
right, passing the southeast corner of a tract of land described in deed to MEF 94 LTD, a
recorded in Volume 1059, Page 833, of the Deed Records of Ellis County, Texas, an arc
distance of 449.88 feet and a chord bearing and distance of N05°38'47"E, 440.67 feet to a
point for corner;

THENCE N25°47'03"E, along the west line of said Red Oak Coyote Ridge, LTD tract and the east
line of said MEF 94 LTD tract, a distance of 392.85 feet to a point for corner, said point
being the beginning of a curve to the left having a radius of 970.00 feet, and a delta angle
of 12°50'51";

THENCE along the west line of said Red Oak Coyote Ridge, LTD tract and the east line of said
MEF 94 LTD tract and said curve to the left an arc distance of 217.50 feet and a chord
bearing and distance of N19°21'37"E, 217.05 feet to a point for corner, said point being the
northwest corner of said Red Oak Coyote Ridge, LTD tract, same being the southwest corner of
said Highland Meadows No. 1 Addition;

THENCE along the north line of said Red Oak Coyote Ridge, LTD tract and the south line of
said Highland Meadows No. 1 Addition the following bearing and distances:
S75°39'00"E, a distance of 60.02 feet to a point for corner;
S13°37'56"W, a distance of 22.05 feet to a point for corner;
S75°41'06"E, a distance of 106.00 feet to a point for corner;
S14°53'21"W, a distance of 25.27 feet to a point for corner;
S74°28'24"E, a distance of 20.00 feet to a point for corner, said point being the beginning
  of a curve to the right having a radius of 40.00 feet and a delta angle of 74°21'48", an
  arc distance of 51.92 feet and a chord bearing and distance of N52°42'29"E, 48.35 feet to a
  point for corner;
N89°53'23"E, a distance of 676.64 feet to a point for corner;
S39°08'46"E, a distance of 31.32 feet to a point for corner;
N50°51'14"E, a distance of 50.00 feet to a point for corner;
N39°08'46"W, a distance of 33.02 feet to a point for corner, said point being the beginning
  of a curve to the right having a radius of 125.00 feet, and a delta angle of 38°58'42", an
  arc distance of 85.04 feet, a chord bearing and distance of N19°39'25"W, 83.41 feet to a
  point for corner;
N00°10'04"W, a distance of 16.39 feet to a point for corner;
N89°49'56"E, a distance of 160.00 feet to the POINT OF BEGINNING and containing 3,068,546
  square feet or 70.44 acres of land.

TRACT 2 (1.62 acres):
Being a 1.62 acre tract of land situated in the George C. Parks Survey, Abstract No. 836, in
the City of Red Oak, Ellis County, Texas, and being that same tract of land described in deed
to HTJ & C TRUST as recorded in Volume 1045, Page 1932, of the Deed Records of Ellis County,
Texas, and being more particularly described by metes and bounds as follows:

BEGINNING at a point found at the northwest corner of said HTJ & C TRUST tract, and being the
northeast corner of 1.19 acre tract of land described in deed to Reginald E. Culpepper,
according to the deed thereof recorded in Volume 650, Page 457, of the Deed Records of Ellis
County, Texas, and being in the south right-of-way line of F.M. 664 (Ovilla Road);

THENCE N87°03'28"E, along the north line of said HTJ & C TRUST tract, and the south
right-of-way line of said F.M. 664 (Ovilla Road), a distance of 139.73 feet to a point for
corner, said point being the northeast corner of said HTJ & C TRUST tract;

THENCE S00°13'06"E, along the east line of said HTJ & C TRUST tract, passing the northwest
corner of Lot 1, Block F, of Highland Meadows No. 1 Addition, an addition on the City of Red
Oak according to the plat thereof recorded in Cabinet A, Slide 432, of the Plat Records of
Ellis County, Texas, and continuing along said HTJ & C TRUST tract and said Highland Meadows
No. 1 Addition a total distance of 260.96 feet to a point for corner at the southeast corner
of said HTJ & C TRUST tract, same being the northeast corner of Lot 5, Block A, of said
Highland Meadows No. 1 Addition;

THENCE S89°46'54"W, along the south line of said HTJ & C TRUST tract, and the north line of
said Lot 5 and Lot 4, Block A, of said Highland Meadows No. 1 Addition, a distance of 206.23
feet to a point for corner, said point being the southwest corner of said HTJ & C TRUST
tract, same being the southeast corner of said called 1.19 acre tract;

THENCE N26°56'06"W, along the west line of said HTJ & C TRUST tract and the east line of said
called 1.19 acre tract, a distance of 284.72 feet to the POINT OF BEGINNING and containing
70,481 square feet or 1.62 acres of land.

TRACT 3 (platted lots -- "MULTI LTS AND BLKS"):
Hickory Creek Estates, Phase I, an addition in the City of Red Oak, Ellis County, Texas
according to the plat thereof recorded at Cabinet H, Slides 130-132 of the Plat Records of
Ellis County, Texas.

Lot and Block:
1A, 2A, 3A, 4A, 5A, 6A, 68A, 1B, 2B, 11B, 12B, 17B, [ONE ENTRY REDACTED/ILLEGIBLE IN COUNTY'S
OWN CERTIFIED COPY -- between 17B and 24B], 24B

Page 17 of 18 (book page 0151) is a graphical survey exhibit titled "EXHIBIT OF HICKORY CREEK
ESTATES" by Register Engineering Corporation / Haynes Park Group, Midlothian, Texas, dated
8/16/2010, matching the traverse calls above (Ovilla Road / F.M. 664, Red Oak Road).

SOURCE: Ellis County Clerk Acclaim public-search document viewer
(ellisccktxpublicsearch.us/AcclaimWeb), Instrument No. 1019671, Book 2530 Page 135-152 (18
stamped pages / 36 scanned images), filed 09/08/2010. Our locally-held PDF
(8386/8386_D1372.pdf) contains only the first 11 pages (0135-0145) -- the Declaration text up
through the notary acknowledgment -- and is missing the final 7 pages (0146-0152) which
contain all of Exhibit A (both metes-and-bounds tracts, the platted-lot list, the survey
exhibit, and the clerk's filing certification). Confirmed via the Document Details panel's
"Number Of Pages: 36" field (image count; 18 unique stamped pages after accounting for
duplicate-scan images) vs our local copy's 11 pages.
"""


def resolve_tract(session, tract_no: int, courses: list[Course], stated_acres: float,
                   anchor_query: str, source_url: str) -> dict:
    traverse = walk_traverse(courses)
    candidates = geocode(anchor_query)
    if not candidates:
        raise RuntimeError(f"geocode returned no candidates for {anchor_query!r}")
    anchor = candidates[0]
    geojson = traverse_to_geojson(traverse["vertices"], anchor["lat"], anchor["lon"])

    closure_ratio = traverse["closure_ratio"]
    closure_display = f"1:{round(1 / closure_ratio)}" if closure_ratio else "n/a"
    area_diff_pct = 100 * (traverse["area_acres"] - stated_acres) / stated_acres
    n_curves = sum(1 for c in courses if c.is_curve)
    notes = (
        f"Hand-transcribed (not OCR/LLM) from Ellis County Clerk Acclaim document image "
        f"viewer, Instrument No. 1019671, Book 2530 (missing from our local PDF corpus -- "
        f"see script docstring). {len(courses)} courses ({n_curves} curves, walked via their "
        f"chord bearing/distance -- true boundary bows out slightly along those edges, "
        f"corner points are exact as transcribed). Closure ratio {closure_display}, computed "
        f"area {traverse['area_acres']:.2f} acres vs deed-stated {stated_acres} acres "
        f"({area_diff_pct:+.2f}%). NOT anchored to a surveyed position or parcel corner -- "
        f"Ellis County (elliscad.com) has no publicly queryable GIS/ArcGIS service, so this "
        f"is a Nominatim road-name geocode only: {anchor_query!r} -> {anchor['display_name']}."
    )
    print(f"tract {tract_no}: {len(courses)} courses, closure {closure_display}, "
          f"area {traverse['area_acres']:.3f} ac (deed states {stated_acres} ac, "
          f"{area_diff_pct:+.2f}%), anchor={anchor['display_name']}")

    session.execute(
        text("""
            INSERT INTO tract (covid, tract_no, geom, approximate_geom, approximate_geom_method,
                                approximate_geom_confidence, approximate_geom_notes,
                                classified_acreage, source_id, updated_at)
            VALUES (:covid, :tract_no, NULL, ST_SetSRID(ST_GeomFromGeoJSON(:geojson), 4326),
                    'geocoded_point_of_beginning', 0.15, :notes, :acreage, :source_id, now())
            ON CONFLICT (covid, tract_no) DO UPDATE SET
                geom = NULL, approximate_geom = EXCLUDED.approximate_geom,
                approximate_geom_method = EXCLUDED.approximate_geom_method,
                approximate_geom_confidence = EXCLUDED.approximate_geom_confidence,
                approximate_geom_notes = EXCLUDED.approximate_geom_notes,
                classified_acreage = EXCLUDED.classified_acreage,
                source_id = EXCLUDED.source_id, updated_at = now()
        """),
        {"covid": COVID, "tract_no": tract_no, "geojson": __import__("json").dumps(geojson),
         "notes": notes, "acreage": traverse["area_acres"], "source_id": source_id(session, source_url)},
    )
    return traverse


def source_id(session, url: str) -> int:
    return session.execute(
        text("""
            INSERT INTO source (source_type, reference, confidence, retrieved_at)
            VALUES ('recorder_portal', :url, 0.9, now())
            RETURNING source_id
        """),
        {"url": url},
    ).scalar_one()


def check_closure_only(courses: list[Course], label: str) -> dict:
    """Report closure without writing anything -- used for Tract 2, whose
    traverse fails closure badly enough (see module docstring) that no
    geometry should be persisted at all."""
    traverse = walk_traverse(courses)
    cr = traverse["closure_ratio"]
    print(f"{label}: {len(courses)} courses, perimeter={traverse['perimeter_ft']:.2f}ft, "
          f"closure_error={traverse['closure_error_ft']:.2f}ft, "
          f"ratio=1:{round(1/cr) if cr else 'n/a'}, computed_area={traverse['area_acres']:.3f}ac "
          f"-- NOT written to DB (closure failure, see docstring)")
    return traverse


def main() -> None:
    source_url = ("https://ellisccktxpublicsearch.us/AcclaimWeb/Document/DocDetails "
                  "(Instrument No. 1019671, Book 2530 Pg 135-152, filed 09/08/2010)")
    with get_session() as session:
        t1 = resolve_tract(session, 1, TRACT1_COURSES, TRACT1_STATED_ACRES,
                            "East Red Oak Road, Red Oak, TX 75154", source_url)
        t2 = check_closure_only(TRACT2_COURSES, "tract 2 (1.62ac, HTJ & C Trust)")

        session.execute(
            text("""
                UPDATE covenant SET
                    legal_description_type = 'metes_bounds',
                    legal_description_raw = :legal_description_raw,
                    status = 'needs_review',
                    review_reason = :reason
                WHERE covid = :covid
            """),
            {
                "covid": COVID,
                "legal_description_raw": EXHIBIT_A_TEXT,
                "reason": (
                    "RESOLVED-PARTIAL (2026-07-24): the recorded instrument (Instr. No. 1019671, Book "
                    "2530 Pg 135-152, Ellis County) is 18 pages -- our local PDF (8386_D1372.pdf) only "
                    "had the first 11 and was missing all of Exhibit A. Recovered the complete legal "
                    "description by viewing the Ellis County Clerk's own Acclaim document image viewer "
                    "directly (guest access, ellisccktxpublicsearch.us) -- see legal_description_raw. "
                    "Encumbered land is 3 tracts, not one deed-stated total acreage: "
                    "(1) 70.44-ac metes-and-bounds, Abstract 836 -- RESOLVED to tract_no=1, COGO traverse "
                    "closes almost exactly (0.004 ft error / 8159 ft perimeter), geocoded-approximate "
                    "placement only (no Ellis GIS parcel-corner tie available -- see tract.approximate_geom_notes). "
                    "(2) 1.62-ac metes-and-bounds, Abstract 836, owned by HTJ & C Trust at recording -- "
                    "NOT RESOLVED: its traverse fails closure by 194.7 ft over an 892 ft perimeter "
                    f"(computed area {t2['area_acres']:.2f} ac vs deed-stated 1.62 ac). Re-read the source "
                    "image twice independently, both reads agree, and no single-field sign/value "
                    "correction reproduces closure -- looks like a genuine error in the 2010 exhibit "
                    "itself or in the deed it ties to (Vol 1045 Pg 1932), not a transcription mistake "
                    "here. No geometry written (see courses in this covenant's legal_description_raw). "
                    "(3) 13 named lots in Hickory Creek Estates Ph I (Cabinet H, Slides 130-132) -- NOT "
                    "resolved: Ellis County (elliscad.com) has no publicly queryable GIS/ArcGIS service "
                    "(custom Google-Maps viewer; esearch.elliscad.com refused connection), so no ellis_tx "
                    "adapter exists; one lot/block entry is redacted/illegible in the county's own "
                    "certified copy. Still needs_review pending: human review of tract 2's source deed, "
                    "an Ellis CAD data source for tract 3, and a real GIS/parcel anchor (rather than a "
                    "road-name geocode) for tract 1."
                ),
            },
        )
    print(f"\ndone. tract1 area={t1['area_acres']:.3f} ac (written); "
          f"tract2 area={t2['area_acres']:.3f} ac (NOT written, bad closure); "
          f"tract3 not geometrized (no Ellis GIS source)")


if __name__ == "__main__":
    main()

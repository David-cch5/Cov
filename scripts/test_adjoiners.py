"""Smoke test for app/parsing/legal_description/adjoiners.py and the veto it
supplies to classifier.py's own sliver-cluster detection.

The whole point of this module is a distinction that is easy to get backwards,
so the tests are built around this project's own real counter-examples rather
than synthetic prose: a subdivision merely NAMED in a metes-and-bounds deed is
NOT automatically an adjoiner, because a tract can be platted into one (covid
4781's Watermark) or literally BE a lot in one (covid 5838's Gulfside Estates).
Getting that backwards would exclude genuinely encumbered land.

Runs against the real cached deed text for covenants already in this project's
corpus -- no network, no LLM.

Usage: python3 scripts/test_adjoiners.py
"""
import sys
from types import SimpleNamespace

sys.path.insert(0, ".")

from app.db.session import get_session
from app.gis.classifier import _detect_sliver_subdivision_clusters
from app.ingestion.walk import get_deed_text
from app.parsing.legal_description.adjoiners import extract_adjoining_subdivisions


def _deed_text(covid: int) -> str:
    with get_session() as session:
        return get_deed_text(session, covid)


def _roles(covid: int) -> dict[str, str]:
    return {e["subdivision"]: e["role"] for e in extract_adjoining_subdivisions(_deed_text(covid))}


def test_adjoining_role_covid_8534() -> None:
    """The case this module was built for: covid 8534's Exhibit A names Forman
    Williamsburg Square exactly once -- "in the South line of Forman
    Williamsburg Square as recorded in Cabinet R, Page 318" -- a pure boundary
    tie, never land being conveyed."""
    roles = _roles(8534)
    assert roles.get("FORMAN WILLIAMSBURG SQUARE") == "adjoining", roles
    print("PASS: adjoiners (covid 8534) -> Forman Williamsburg Square read as 'adjoining' "
          "from its boundary-tie phrasing")


def test_derivation_role_vetoes_covid_4781_watermark() -> None:
    """covid 4781: "...the said 10.780 acres having been platted as WATERMARK
    SECTION ONE, PHASE ONE UNIT DEVELOPMENT..." -- Watermark was platted OUT OF
    this tract, so its lots are the encumbered land. Must read 'derivation'.
    Palm Beach Estates, named only as the bearing-basis line, must not."""
    roles = _roles(4781)
    assert roles.get("WATERMARK SECTION ONE, PHASE ONE UNIT DEVELOPMENT") == "derivation", roles
    assert roles.get("PALM BEACH ESTATES") == "adjoining", roles
    print("PASS: adjoiners (covid 4781) -> Watermark (platted out of the tract) reads "
          "'derivation'; Palm Beach Estates (bearing basis) reads 'adjoining'")


def test_derivation_wins_over_boundary_ties_covid_5838() -> None:
    """covid 5838 is the strongest counter-example in the corpus: the tract IS
    lots in Gulfside Estates ("being all of Lot Three (3), Block One (1), Gulf
    Side Estates Subdivision"), AND the deed separately ties boundaries to
    NEIGHBOURING lots of that same subdivision. A mention-only or
    nearest-cue-only rule would call it adjoining and exclude the subject land
    itself -- derivation must win globally. Both of the county's own real
    spellings appear in this one deed."""
    roles = _roles(5838)
    gulfside = {k: v for k, v in roles.items() if "GULF" in k}
    assert gulfside, roles
    assert all(v == "derivation" for v in gulfside.values()), gulfside
    assert roles.get("KINGSTONE BEACH") == "adjoining", roles
    print(f"PASS: adjoiners (covid 5838) -> derivation wins globally for {sorted(gulfside)} "
          "despite boundary-tie mentions of the same subdivision; Kingstone Beach stays 'adjoining'")


def test_no_subdivisions_named_is_empty_not_a_guess() -> None:
    """covid 3194/4440 (Montgomery): real metes-and-bounds deeds that cite no
    platted subdivision at all. An empty list, never an invented one."""
    for covid in (3194, 4440):
        assert extract_adjoining_subdivisions(_deed_text(covid)) == [], covid
    assert extract_adjoining_subdivisions(None) == []
    assert extract_adjoining_subdivisions("") == []
    print("PASS: adjoiners -> a deed naming no platted subdivision returns [] rather than "
          "guessing, and None/empty input is handled")


def _parcel(apn: str, legal: str, overlap: float):
    return SimpleNamespace(apn=apn, recited_legal_description=legal,
                           overlap_fraction=overlap, is_interior=False)


def test_sliver_cluster_evidence_and_veto() -> None:
    """The three outcomes classifier.py's own flag has to distinguish, driven
    entirely by the deed-text role: corroborated, geometry-only, and vetoed.
    The vetoed case reproduces covid 4781's real shape -- lots of a subdivision
    the tract was itself platted into, clipping the polygon at the same low
    overlap fraction as a genuine adjoiner would."""
    matched = [
        _parcel("A1", "FORMAN WILLIAMSBURG SQUARE PH II BLK A LOT 8", 0.14),
        _parcel("A2", "FORMAN WILLIAMSBURG SQUARE PH II BLK A LOT 9", 0.17),
        _parcel("B1", "HERCULES WEST ADDITION PHASE 2B BLK 22 LOT 6", 0.06),
        _parcel("B2", "HERCULES WEST ADDITION PHASE 2B BLK 22 LOT 7", 0.21),
        _parcel("C1", "WATERMARK SECTION ONE BLK 1 LOT 1", 0.11),
        _parcel("C2", "WATERMARK SECTION ONE BLK 1 LOT 2", 0.19),
        # a genuine straddling parcel -- high overlap, must never be flagged
        _parcel("D1", "SHERMAN CROSSING PHASE 2B BLK A LOT 1", 0.88),
        _parcel("D2", "SHERMAN CROSSING PHASE 2B BLK A LOT 2", 0.91),
    ]
    deed = [
        {"subdivision": "FORMAN WILLIAMSBURG SQUARE", "role": "adjoining", "context": ""},
        {"subdivision": "WATERMARK SECTION ONE, PHASE ONE UNIT DEVELOPMENT", "role": "derivation", "context": ""},
    ]
    got = {g["subdivision"]: g["evidence"] for g in _detect_sliver_subdivision_clusters(matched, deed)}
    # Group keys have a trailing generic descriptor stripped, so "HERCULES WEST
    # ADDITION" keys as "HERCULES WEST" -- deliberate, see _GENERIC_DESCRIPTOR_RE.
    assert got.get("FORMAN WILLIAMSBURG SQUARE") == "deed_names_as_adjoining", got
    assert got.get("HERCULES WEST") == "geometry_only", got
    assert "WATERMARK SECTION ONE" not in got, f"derivation veto failed -- would have excluded encumbered land: {got}"
    assert "SHERMAN CROSSING" not in got, got
    print("PASS: sliver clusters -> deed-corroborated vs geometry-only evidence separated, a "
          "derivation-role subdivision is vetoed entirely, and a genuine high-overlap "
          "straddling parcel is never flagged")


def test_sliver_veto_survives_county_spelling_differences() -> None:
    """The CAD's own spelling rarely matches the deed's exactly. covid 5838's
    deed says "Gulf Side Estates Subdivision"; Nueces CAD says "GULFSIDE
    ESTATES". The veto has to survive that, or it silently stops protecting the
    subject land."""
    matched = [
        _parcel("E1", "GULFSIDE ESTATES BLK 1 LOT 4", 0.12),
        _parcel("E2", "GULFSIDE ESTATES BLK 1 LOT 6", 0.22),
    ]
    deed = [{"subdivision": "GULF SIDE ESTATES SUBDIVISION", "role": "derivation", "context": ""}]
    assert _detect_sliver_subdivision_clusters(matched, deed) == [], "spelling variance broke the veto"
    print("PASS: sliver clusters -> the derivation veto still matches across the deed's own "
          "'Gulf Side Estates Subdivision' vs the CAD's 'GULFSIDE ESTATES'")


def test_sliver_detection_without_deed_text_still_works() -> None:
    """Deed text is an enhancement, not a dependency -- a covenant with no
    cached document text still gets the original purely-geometric flag."""
    matched = [
        _parcel("F1", "SOME ADDITION BLK 1 LOT 1", 0.10),
        _parcel("F2", "SOME ADDITION BLK 1 LOT 2", 0.20),
    ]
    got = _detect_sliver_subdivision_clusters(matched, [])
    assert len(got) == 1 and got[0]["evidence"] == "geometry_only", got
    print("PASS: sliver clusters -> still flags geometrically when the deed text yields nothing")


if __name__ == "__main__":
    test_adjoining_role_covid_8534()
    test_derivation_role_vetoes_covid_4781_watermark()
    test_derivation_wins_over_boundary_ties_covid_5838()
    test_no_subdivisions_named_is_empty_not_a_guess()
    test_sliver_cluster_evidence_and_veto()
    test_sliver_veto_survives_county_spelling_differences()
    test_sliver_detection_without_deed_text_still_works()
    print("\nall adjoiner smoke tests passed")

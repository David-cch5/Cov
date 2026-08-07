"""National Geodetic Survey control-monument lookup.

Deeds in this corpus routinely tie a tract corner to a published NGS monument
("a National Geodetic Survey monument stamped \"SF-010\" bears North 14°01'24\"
East 4708.73 feet"). That is a real, free, survey-grade georeference: the
monument's position is published to centimetre accuracy, so reversing the deed's
own bearing and distance puts the corner on the ground exactly -- no parcel
fitting, no geocoding, no LLM.

This module is the read-only lookup half; app/gis/state_plane_anchor.py's
anchor_by_ngs_monument_tie does the placement.

Two NGS endpoints, both public and unauthenticated:
  * /api/nde/bounds       -- every mark in a lat/lon box (discovery)
  * /cgi-bin/ds_mark.prl  -- one mark's full datasheet (authoritative)

The datasheet is what gets parsed, never the bounds summary: only the datasheet
states the State Plane zone, the published grid northing/easting, the grid scale
factor and the convergence angle -- the values that let a grid-referenced deed
traverse be walked directly, with no rotation solve.
"""
import re
from dataclasses import dataclass

import requests

NGS_BOUNDS_URL = "https://geodesy.noaa.gov/api/nde/bounds"
NGS_DATASHEET_URL = "https://geodesy.noaa.gov/cgi-bin/ds_mark.prl"

# NGS datasheet State Plane zone codes -> EPSG (NAD83, US survey feet).
# Deliberately keyed on the datasheet's own spelling so the mapping is checkable
# against the sheet rather than inferred from the county.
NGS_SPC_ZONE_TO_EPSG = {
    "TX N": 2275, "TX NC": 2276, "TX C": 2277, "TX SC": 2278, "TX S": 2279,
}

# "AH1137;SPC TX S  -17,179,470.57  1,440,942.49  sFT  0.99998992  +0 38 28.7"
# The dash after the zone is a column separator, not a sign.
_SPC_SFT_RE = re.compile(
    r";SPC\s+(?P<zone>[A-Z]{2}(?:\s+[A-Z]{1,2})?)\s*-\s*"
    r"(?P<north>[\d,]+\.\d+)\s+(?P<east>[\d,]+\.\d+)\s+sFT"
    r"\s+(?P<scale>[\d.]+)\s+(?P<conv>[+-]\s*\d+\s+\d+\s+[\d.]+)"
)
_POSITION_RE = re.compile(
    r"NAD 83\((?P<realization>\d{4})\)\s+POSITION-\s*"
    r"(?P<lat_d>\d+)\s+(?P<lat_m>\d+)\s+(?P<lat_s>[\d.]+)\(N\)\s+"
    r"(?P<lon_d>\d+)\s+(?P<lon_m>\d+)\s+(?P<lon_s>[\d.]+)\(W\)"
)
_DESIGNATION_RE = re.compile(r"DESIGNATION\s*-\s*(?P<name>.+)")
_CONDITION_RE = re.compile(r"CONDITION\s*-\s*(?P<cond>.+)")


@dataclass(frozen=True)
class NgsMonument:
    designation: str
    pid: str
    lat: float
    lon: float
    realization: int            # NAD83 adjustment year -- 2011 beats 1993
    spc_zone: str | None
    spc_north_sft: float | None
    spc_east_sft: float | None
    convergence_deg: float | None
    grid_scale: float | None
    condition: str | None

    @property
    def epsg(self) -> int | None:
        return NGS_SPC_ZONE_TO_EPSG.get(self.spc_zone or "")

    @property
    def quality_rank(self) -> tuple:
        """Higher sorts first. A later NAD83 realization is a genuinely better
        position, and a mark reported found beats one reported missing -- both
        are stated on the datasheet, so this is read, never guessed."""
        return (self.realization, 1 if (self.condition or "").upper().startswith("GOOD") else 0)


def normalize_designation(name: str) -> str:
    """Deeds and NGS spell the same mark differently -- a deed's stamping
    "SF-010" is NGS designation "SF 010". Collapse to a single canonical form so
    they compare equal, while keeping distinct marks distinct: "KNOLL",
    "KNOLL ECC" and "KNOLL RM 2" must NOT collide, since they are three
    different physical monuments metres apart."""
    return re.sub(r"[^A-Z0-9]+", " ", (name or "").upper()).strip()


def _dms(d: str, m: str, s: str) -> float:
    return float(d) + float(m) / 60.0 + float(s) / 3600.0


def parse_datasheet(text: str) -> dict:
    """Pull the position, State Plane grid coordinates, scale and convergence
    out of a raw NGS datasheet."""
    out: dict = {}
    if (m := _DESIGNATION_RE.search(text)):
        out["designation"] = m.group("name").strip()
    if (m := _CONDITION_RE.search(text)):
        out["condition"] = m.group("cond").strip()
    if (m := _POSITION_RE.search(text)):
        out["realization"] = int(m.group("realization"))
        out["lat"] = _dms(m.group("lat_d"), m.group("lat_m"), m.group("lat_s"))
        out["lon"] = -_dms(m.group("lon_d"), m.group("lon_m"), m.group("lon_s"))
    if (m := _SPC_SFT_RE.search(text)):
        out["spc_zone"] = re.sub(r"\s+", " ", m.group("zone")).strip()
        out["spc_north_sft"] = float(m.group("north").replace(",", ""))
        out["spc_east_sft"] = float(m.group("east").replace(",", ""))
        out["grid_scale"] = float(m.group("scale"))
        sign, dd, mm, ss = re.match(r"([+-])\s*(\d+)\s+(\d+)\s+([\d.]+)", m.group("conv")).groups()
        out["convergence_deg"] = (1 if sign == "+" else -1) * _dms(dd, mm, ss)
    return out


def fetch_datasheet(pid: str, timeout: int = 30) -> str:
    resp = requests.get(NGS_DATASHEET_URL, params={"PidBox": pid}, timeout=timeout)
    resp.raise_for_status()
    return resp.text


def find_monuments(designations, bbox: dict, timeout: int = 45) -> dict:
    """Resolve deed monument stampings to full NgsMonument records.

    `bbox` is {min_lat,max_lat,min_lon,max_lon} -- a search area, typically the
    envelope of the covenant's county or an already-anchored sibling tract,
    generously buffered. A tie can run several thousand feet, so the monument is
    often well outside the tract itself.

    Matching is on the NORMALIZED designation and must be exact: a near-match is
    a different monument, not the same one spelled loosely.
    """
    wanted = {normalize_designation(d) for d in designations}
    resp = requests.get(NGS_BOUNDS_URL, params={
        "minlon": bbox["min_lon"], "maxlon": bbox["max_lon"],
        "minlat": bbox["min_lat"], "maxlat": bbox["max_lat"],
    }, timeout=timeout)
    resp.raise_for_status()

    found: dict[str, NgsMonument] = {}
    for mark in resp.json():
        key = normalize_designation(mark.get("name"))
        if key not in wanted or key in found:
            continue
        sheet = parse_datasheet(fetch_datasheet(mark["pid"], timeout=timeout))
        if "lat" not in sheet:
            continue
        found[key] = NgsMonument(
            designation=sheet.get("designation") or mark.get("name") or "",
            pid=mark["pid"], lat=sheet["lat"], lon=sheet["lon"],
            realization=sheet.get("realization") or 0,
            spc_zone=sheet.get("spc_zone"), spc_north_sft=sheet.get("spc_north_sft"),
            spc_east_sft=sheet.get("spc_east_sft"),
            convergence_deg=sheet.get("convergence_deg"), grid_scale=sheet.get("grid_scale"),
            condition=sheet.get("condition") or mark.get("condition"),
        )
    return found

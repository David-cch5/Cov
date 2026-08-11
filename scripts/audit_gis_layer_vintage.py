"""Audit every registered county parcel layer for staleness, and record the verdict.

Run because one stale layer produced a wrong answer. Dallas's Tax_Parcels_2019
was last edited 2020-07-01 and its geometry is wrong about boundaries that have
since moved -- on covid 4956 by 6,001 sq ft, swapped between two parcels, enough
to make the covenant look as though it encumbered a neighbour's land. Every other
adapter was discovered the same way and could have been pointing at a dated layer
the same way, so all thirteen were checked rather than assumed.

Three signals, in order of directness:

  1. editingInfo.dataLastEditDate -- the service saying when its data last
     changed. Ten of thirteen counties publish it.
  2. a vintage FIELD in the data (APPRAISALYEAR, prop_val_yr, tax_year), whose
     maximum says which appraisal year the layer holds. This is how Bexar and
     Harris were cleared.
  3. nothing at all -- Travis publishes neither. Reported as UNVERIFIABLE rather
     than assumed current, because "no evidence of staleness" is not evidence of
     currency and this project does not get to guess about acreage.

The verdict lands in county_gis_registry.quirks->'vintage_audit', so a later run
can tell what changed since, and nobody has to re-derive it.

Usage: python3 scripts/audit_gis_layer_vintage.py [--record]
"""
import argparse
import datetime
import json
import sys

import requests

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import get_session

# Beyond this, a layer is stale enough that boundaries may have moved under us.
# 18 months rather than a year: county fabrics are republished on annual
# appraisal cycles, so a 13-month-old layer is normal and a 19-month-old one is
# a cycle behind.
STALE_DAYS = 545
AGING_DAYS = 270

VINTAGE_FIELD_HINTS = ("YEAR", "YR", "APPRAISAL", "TAX_YEAR", "VINTAGE")


def _epoch_date(ms):
    if not ms:
        return None
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).date()


def _max_vintage_field(url: str, fields: list[str]) -> tuple[str, object] | None:
    """The highest value of any year-ish field -- signal 2. Tried only when the
    service publishes no editingInfo, since a field maximum is weaker evidence
    (it says which appraisal year the ATTRIBUTES describe, not when the geometry
    was last edited)."""
    for name in [f for f in fields if any(h in f.upper() for h in VINTAGE_FIELD_HINTS)]:
        try:
            r = requests.get(f"{url}/query", params={
                "where": "1=1", "outFields": name, "orderByFields": f"{name} DESC",
                "resultRecordCount": 1, "returnGeometry": "false", "f": "json"}, timeout=60).json()
            feats = r.get("features") or []
            if feats:
                value = feats[0]["attributes"].get(name)
                if value not in (None, ""):
                    return name, value
        except requests.RequestException:
            continue
    return None


def audit_layer(url: str) -> dict:
    try:
        meta = requests.get(url, params={"f": "json"}, timeout=45).json()
    except requests.RequestException as e:
        return {"verdict": "unreachable", "evidence": f"{type(e).__name__}: {e}"}
    if meta.get("error"):
        return {"verdict": "unreachable", "evidence": str(meta["error"])[:200]}

    fields = [f["name"] for f in meta.get("fields", [])]
    editing = meta.get("editingInfo") or {}
    edited = _epoch_date(editing.get("dataLastEditDate") or editing.get("lastEditDate"))

    if edited:
        days = (datetime.date.today() - edited).days
        verdict = ("stale" if days > STALE_DAYS else "aging" if days > AGING_DAYS else "current")
        return {"verdict": verdict, "layer": meta.get("name"), "last_edited": str(edited),
                "age_days": days, "evidence": f"editingInfo dataLastEditDate {edited}"}

    found = _max_vintage_field(url, fields)
    if found:
        name, value = found
        return {"verdict": "current", "layer": meta.get("name"),
                "evidence": f"no editingInfo; max({name}) = {value}"}

    return {"verdict": "unverifiable", "layer": meta.get("name"),
            "evidence": ("no editingInfo and no vintage field -- currency cannot be established "
                         "from the service. Not assumed current: verify against a known parcel "
                         "before trusting acreage.")}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--record", action="store_true",
                        help="write each verdict to county_gis_registry.quirks->vintage_audit")
    args = parser.parse_args(argv)

    with get_session() as session:
        rows = [(r[0], r[1], r[2]) for r in session.execute(text("""
            SELECT county_fips, base_url,
                   -- A county may deliberately read GEOMETRY from a different,
                   -- current layer while keeping the older one for attributes it
                   -- is the only source of (Dallas). Auditing base_url alone would
                   -- then report a permanent false alarm, and an audit that cries
                   -- wolf every run is an audit nobody reads.
                   (SELECT sl->>'current_geometry_url' FROM jsonb_array_elements(superseded_layers) sl
                     WHERE sl->>'current_geometry_url' IS NOT NULL LIMIT 1) AS geometry_url
              FROM county_gis_registry ORDER BY county_fips"""))]

    print(f"{'county':<9}{'layer':<26}{'verdict':<15}evidence")
    results = {}
    for fips, url, geometry_url in rows:
        if geometry_url:
            geom = audit_layer(geometry_url)
            attrs = audit_layer(url)
            r = {**geom,
                 "verdict": geom["verdict"],
                 "evidence": (f"GEOMETRY from a separate current layer ({geom['evidence']}); "
                              f"attributes still from {str(attrs.get('layer'))} "
                              f"({attrs['evidence']}) -- deliberate split, see superseded_layers"),
                 "layer": f"{geom.get('layer')} + {attrs.get('layer')}"}
        else:
            r = audit_layer(url)
        results[fips] = r
        print(f"{fips:<9}{str(r.get('layer'))[:25]:<26}{r['verdict']:<15}{r['evidence'][:70]}")

    if args.record:
        with get_session() as session:
            for fips, r in results.items():
                quirks = session.execute(
                    text("SELECT quirks FROM county_gis_registry WHERE county_fips = :c"),
                    {"c": fips}).scalar() or {}
                if isinstance(quirks, str):
                    quirks = json.loads(quirks)
                quirks["vintage_audit"] = {**r, "audited_at": datetime.date.today().isoformat()}
                session.execute(
                    text("UPDATE county_gis_registry SET quirks = (:q)::jsonb, "
                         "last_verified_at = now() WHERE county_fips = :c"),
                    {"q": json.dumps(quirks), "c": fips})
        print(f"\nrecorded {len(results)} verdict(s) to county_gis_registry.quirks->vintage_audit")

    bad = {f: r for f, r in results.items() if r["verdict"] in ("stale", "unreachable")}
    if bad:
        print(f"\nNEEDS ATTENTION: {sorted(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())

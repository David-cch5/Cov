"""Generate a play/drag formation map for any covenant — the covid 4440 pattern, generalised.

The covid 4440 platting map was hand-built for one covenant. This produces the
same thing for any covid from the database, so nobody hand-writes another.

WHAT IT SHOWS. Every encumbered parcel as a real polygon, positioned
geographically inside its tract, coloured by whether it existed yet at the
selected date. Drag the slider or press play and lots appear at their own real
recorded plat date, read from parcel.formed_date — which stands on a recorded
instrument or is NULL (see app/gis/formation.py).

THE RULE THIS INHERITS FROM 4440, and the reason the map is trustworthy: a parcel
with no real formation date NEVER appears as formed, wherever the slider sits. It
is drawn as still-raw ground throughout. An animation that quietly filled those
in would look better and assert something nobody read.

Design follows the existing 4440 artifacts rather than inventing a second visual
language for the same subject: the same paper/ink palette, Georgia display face,
monospace for figures, and the same play/slider/readout control row.

Usage:
  python3 scripts/make_formation_map.py <covid> [--out FILE] [--simplify-ft 10]
"""
import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, ".")

from sqlalchemy import text

from app.db.session import get_session

VIEW = 1000  # viewBox side; geometry is fitted into this square per tract


def _covenant(session, covid: int) -> dict:
    row = session.execute(
        text("""SELECT c.covid, c.county_fips, c.declarant_raw, c.stated_acreage,
                       c.recording_instrument, c.recording_date, co.county_name, co.state_code
                  FROM covenant c JOIN county co USING (county_fips)
                 WHERE c.covid = :covid"""), {"covid": covid}).fetchone()
    if row is None:
        raise SystemExit(f"covid {covid} not found")
    return dict(row._mapping)


def _tracts(session, covid: int, simplify_ft: float) -> list[dict]:
    """Tract outline plus every parcel in its census, as screen coordinates.

    Projected per tract with a single uniform scale so shapes are not distorted,
    and simplified for rendering only -- the stored geometry is untouched. Y is
    flipped because SVG counts downward and latitude counts up.
    """
    rows = session.execute(
        text("""
            SELECT t.tract_no,
                   round(t.stated_acreage::numeric, 2) AS stated_acreage,
                   round((ST_Area(t.geom::geography) / 4046.8564224)::numeric, 2) AS geom_acreage,
                   -- Extent of the tract AND its census parcels, not the tract
                   -- alone. 84 of covid 3028's 714 parcels straddle the traverse
                   -- boundary (correctly -- they are real boundary parcels), and
                   -- fitting the viewBox to the tract alone drew them off-canvas
                   -- where they looked deleted rather than straddling.
                   ST_XMin(e.env) AS xmin, ST_YMin(e.env) AS ymin,
                   ST_XMax(e.env) AS xmax, ST_YMax(e.env) AS ymax,
                   ST_AsGeoJSON(ST_SimplifyPreserveTopology(t.geom, :tol)) AS outline
              FROM tract t
              CROSS JOIN LATERAL (
                  SELECT ST_Extent(g)::geometry AS env FROM (
                      SELECT t.geom AS g
                      UNION ALL
                      SELECT p.geom FROM parcel_covenant pc
                        JOIN parcel p USING (county_fips, apn)
                       WHERE pc.covid = t.covid AND pc.tract_no = t.tract_no
                         AND pc.run_seq = (SELECT max(run_seq) FROM parcel_covenant
                                            WHERE covid = t.covid AND tract_no = t.tract_no)
                         AND p.geom IS NOT NULL
                  ) parts
              ) e
             WHERE t.covid = :covid AND t.geom IS NOT NULL AND e.env IS NOT NULL
             ORDER BY t.tract_no"""),
        {"covid": covid, "tol": simplify_ft / 364000.0},   # feet -> ~degrees
    ).fetchall()

    out = []
    for t in rows:
        parcels = session.execute(
            text("""
                SELECT p.apn, p.formed_date, p.formed_by_instrument,
                       pl.subdivision_name, pl.section,
                       round(p.acreage::numeric, 3) AS acreage,
                       -- PreserveTopology, not plain ST_Simplify: the plain one returns
                       -- NULL when it collapses a small polygon entirely, and a parcel
                       -- must never disappear from a census map because the renderer
                       -- over-simplified it. Confirmed on covid 3028's smallest lots.
                       ST_AsGeoJSON(ST_SimplifyPreserveTopology(p.geom, :tol)) AS geom
                  FROM parcel_covenant pc
                  JOIN parcel p USING (county_fips, apn)
                  LEFT JOIN plat pl ON pl.plat_id = p.plat_id
                 WHERE pc.covid = :covid AND pc.tract_no = :tract_no
                   AND pc.run_seq = (SELECT max(run_seq) FROM parcel_covenant
                                      WHERE covid = :covid AND tract_no = :tract_no)
                   AND p.geom IS NOT NULL
                   -- Human-confirmed exclusions never appear: a digitization
                   -- sliver on a timeline is noise that looks like land.
                   AND NOT EXISTS (SELECT 1 FROM parcel_covenant_exclusion x
                                    WHERE x.county_fips = pc.county_fips AND x.apn = pc.apn
                                      AND x.covid = pc.covid AND x.tract_no = pc.tract_no)"""),
            {"covid": covid, "tract_no": t.tract_no, "tol": simplify_ft / 364000.0},
        ).fetchall()
        if not parcels:
            continue

        span_x, span_y = (t.xmax - t.xmin) or 1e-9, (t.ymax - t.ymin) or 1e-9
        # One scale for both axes, corrected for longitude convergence, so the
        # tract is not stretched. cos(lat) at the tract's own middle.
        import math
        lat_mid = (t.ymin + t.ymax) / 2
        kx = math.cos(math.radians(lat_mid))
        scale = min(VIEW / (span_x * kx), VIEW / span_y)
        ox = (VIEW - span_x * kx * scale) / 2
        oy = (VIEW - span_y * scale) / 2

        def project(coords):
            return [[round(ox + (x - t.xmin) * kx * scale, 1),
                     round(VIEW - oy - (y - t.ymin) * scale, 1)] for x, y in coords]

        def rings(geojson_text):
            g = json.loads(geojson_text)
            polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
            return [project(ring) for poly in polys for ring in poly[:1]]

        straddling = session.execute(
            text("""SELECT count(*) FROM parcel p
                     WHERE p.county_fips = (SELECT county_fips FROM covenant WHERE covid = :covid)
                       AND p.apn = ANY(:apns) AND p.geom IS NOT NULL
                       AND NOT ST_Within(p.geom, (SELECT geom FROM tract
                                                   WHERE covid = :covid AND tract_no = :tract_no))"""),
            {"covid": covid, "tract_no": t.tract_no, "apns": [p.apn for p in parcels]},
        ).scalar()

        out.append({
            "tract_no": t.tract_no,
            "straddling": straddling,
            "stated_acreage": float(t.stated_acreage) if t.stated_acreage else None,
            "geom_acreage": float(t.geom_acreage),
            "outline": rings(t.outline),
            "parcels": [{
                "a": p.apn,
                "d": p.formed_date.isoformat() if p.formed_date else None,
                "i": p.formed_by_instrument,
                "s": p.subdivision_name,
                "n": p.section,
                "ac": float(p.acreage) if p.acreage else None,
                "r": rings(p.geom),
            } for p in parcels],
        })
    return out


HTML = """<meta charset="UTF-8">
<title>Covenant {covid} — Parcel Formation Map</title>
<style>
  :root{{
    --paper:#f6f2e8; --paper-2:#efe8d8; --ink:#241f19; --ink-soft:#5a5147;
    --raw:#c9ad74; --raw-line:#a68a4e; --formed:#3c5a42; --formed-line:#28402c;
    --undated:#b6aa93; --undated-line:#93876f;
    --accent:#1f6f6b; --rule:#d8cdb2; --rule-strong:#b9ab86; --tract-outline:#241f19;
    --font-display: Georgia, "Times New Roman", serif;
    --font-body: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    --font-mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  }}
  @media (prefers-color-scheme: dark){{
    :root{{
      --paper:#181510; --paper-2:#211d17; --ink:#ece4d4; --ink-soft:#b4a890;
      --raw:#8a6f45; --raw-line:#6b5433; --formed:#6f9a72; --formed-line:#4c6f4e;
      --undated:#5d5442; --undated-line:#7d7159;
      --accent:#4fb3ad; --rule:#3a3225; --rule-strong:#544a35; --tract-outline:#ece4d4;
    }}
  }}
  :root[data-theme="dark"]{{
    --paper:#181510; --paper-2:#211d17; --ink:#ece4d4; --ink-soft:#b4a890;
    --raw:#8a6f45; --raw-line:#6b5433; --formed:#6f9a72; --formed-line:#4c6f4e;
    --undated:#5d5442; --undated-line:#7d7159;
    --accent:#4fb3ad; --rule:#3a3225; --rule-strong:#544a35; --tract-outline:#ece4d4;
  }}
  :root[data-theme="light"]{{
    --paper:#f6f2e8; --paper-2:#efe8d8; --ink:#241f19; --ink-soft:#5a5147;
    --raw:#c9ad74; --raw-line:#a68a4e; --formed:#3c5a42; --formed-line:#28402c;
    --undated:#b6aa93; --undated-line:#93876f;
    --accent:#1f6f6b; --rule:#d8cdb2; --rule-strong:#b9ab86; --tract-outline:#241f19;
  }}
  *{{box-sizing:border-box}}
  body{{margin:0;background:var(--paper);color:var(--ink);font-family:var(--font-body);
       line-height:1.5;-webkit-font-smoothing:antialiased}}
  .wrap{{max-width:1040px;margin:0 auto;padding:2.5rem 1.5rem 4rem}}
  header{{margin-bottom:1.5rem;border-bottom:2px solid var(--ink);padding-bottom:1.25rem}}
  .eyebrow{{font-family:var(--font-mono);font-size:.72rem;letter-spacing:.12em;
            text-transform:uppercase;color:var(--ink-soft);margin:0 0 .4rem}}
  h1{{font-family:var(--font-display);font-weight:400;font-size:2rem;margin:0 0 .5rem;
      text-wrap:balance}}
  .sub{{margin:0;color:var(--ink-soft);font-size:.95rem;max-width:66ch}}
  .facts{{display:flex;flex-wrap:wrap;gap:1.75rem;margin-top:1.1rem;
          font-family:var(--font-mono);font-size:.82rem}}
  .facts .k{{color:var(--ink-soft);font-size:.7rem;text-transform:uppercase;
             letter-spacing:.08em;display:block}}
  .facts .v{{font-size:1rem;font-variant-numeric:tabular-nums;display:block}}
  .legend{{display:flex;gap:1.5rem;align-items:center;margin:1.4rem 0 .5rem;
           font-size:.82rem;color:var(--ink-soft);flex-wrap:wrap}}
  .legend .swatch{{display:inline-block;width:.85em;height:.85em;border-radius:2px;
                   margin-right:.4em;vertical-align:-.1em}}
  section.tract{{margin-top:2.5rem}}
  .tract h2{{font-family:var(--font-display);font-weight:400;font-size:1.3rem;margin:0 0 .15rem}}
  .tract .meta{{color:var(--ink-soft);font-size:.85rem;margin:0 0 1rem}}
  .map-frame{{background:var(--paper-2);border:1px solid var(--rule-strong);border-radius:6px;
              padding:.9rem 1rem 1rem;position:relative}}
  svg.map{{display:block;width:100%;height:auto;background:var(--paper-2)}}
  .parcel{{stroke-width:.6;transition:fill .25s ease,stroke .25s ease;cursor:pointer}}
  .parcel.raw{{fill:var(--raw);stroke:var(--raw-line)}}
  .parcel.formed{{fill:var(--formed);stroke:var(--formed-line)}}
  .parcel.undated{{fill:var(--undated);stroke:var(--undated-line)}}
  .parcel:hover{{stroke-width:1.8;stroke:var(--accent)}}
  .tract-outline{{fill:none;stroke:var(--tract-outline);stroke-width:2}}
  .controls{{display:flex;align-items:center;gap:.85rem;margin-top:.85rem;flex-wrap:wrap}}
  .play-btn{{font-family:var(--font-mono);font-size:.8rem;background:var(--formed);
             color:var(--paper-2);border:none;border-radius:4px;padding:.4rem .8rem;
             cursor:pointer;flex:none}}
  .play-btn:hover{{opacity:.88}}
  .play-btn:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
  input[type=range]{{flex:1;min-width:12rem;accent-color:var(--formed)}}
  input[type=range]:focus-visible{{outline:2px solid var(--accent);outline-offset:3px}}
  .date-readout{{font-family:var(--font-mono);font-size:.85rem;min-width:9.5em;
                 text-align:right;font-variant-numeric:tabular-nums}}
  .stats-row{{display:flex;gap:1.75rem;margin-top:.7rem;font-family:var(--font-mono);
              font-size:.78rem;color:var(--ink-soft);flex-wrap:wrap}}
  .stats-row b{{color:var(--ink);font-variant-numeric:tabular-nums}}
  .tooltip{{position:absolute;pointer-events:none;background:var(--ink);color:var(--paper);
            font-family:var(--font-body);font-size:.78rem;padding:.4rem .6rem;border-radius:4px;
            opacity:0;transition:opacity .1s;max-width:230px;z-index:5;line-height:1.4}}
  .tooltip b{{display:block;font-size:.82rem;margin-bottom:.15rem}}
  footer{{margin-top:3rem;padding-top:1.25rem;border-top:1px solid var(--rule);
          font-size:.78rem;color:var(--ink-soft);max-width:70ch}}
  footer p{{margin:.3rem 0}}
  @media (prefers-reduced-motion: reduce){{
    .parcel{{transition:none}}
  }}
</style>
<div class="wrap">
  <header>
    <p class="eyebrow">Covid {covid} · {county_name} County, {state_code}</p>
    <h1>{headline}</h1>
    <p class="sub">Every shape is a real parcel polygon from {county_name}'s own GIS, placed
      geographically inside the covenant's tract. Drag the slider or press play to watch lots
      come into existence on their own recorded plat dates — each read from a real recorded
      instrument, never from when this project happened to notice them.</p>
    <div class="facts">{facts}</div>
  </header>
  <div class="legend">
    <span><span class="swatch" style="background:var(--formed)"></span>Formed as of selected date</span>
    <span><span class="swatch" style="background:var(--raw)"></span>Not yet formed</span>
    <span><span class="swatch" style="background:var(--undated)"></span>No recorded formation date — never shown as formed</span>
  </div>
{sections}
  <div class="tooltip" id="tooltip"></div>
  <footer>
    <p><strong>Provenance.</strong> Formation dates come from <code>parcel.formed_date</code>,
      populated only from a plat this project located in the county clerk's own index — its real
      recording date and instrument number. Geometry is simplified for on-screen rendering
      (~{simplify_ft} ft); the stored parcel records are untouched.</p>
    <p><strong>{undated_total}</strong> of {parcel_total} parcels have no recorded formation
      date — raw abstract-survey tracts and unresolved subdivision references. They are drawn
      in grey and never turn green, whatever the slider says. Filling them in would look
      tidier and assert something nobody read.</p>
    <p>Parcels a human excluded from the census (digitization slivers, adjoiners the deed never
      conveys) are absent entirely.</p>
    <p>Acreage figures count each parcel in FULL, including boundary parcels that straddle the
      tract line, so they can exceed the tract's own {tract_acreage_note}. They are parcel
      acreage, not acreage inside the tract — the reconciliation check is what compares those.</p>
  </footer>
</div>
<script>
const DATA = {data_json};
const T0 = "{t0}", T1 = "{t1}";
const tip = document.getElementById("tooltip");
const ms0 = new Date(T0).getTime(), ms1 = new Date(T1).getTime();

function ringPath(r){{ return "M" + r.map(p => p[0] + "," + p[1]).join("L") + "Z"; }}

DATA.forEach(tract => {{
  const svg = document.getElementById("map" + tract.tract_no);
  let out = "";
  tract.parcels.forEach((p, i) => {{
    const cls = p.d === null ? "undated" : "raw";
    out += `<path class="parcel ${{cls}}" data-i="${{i}}" data-t="${{tract.tract_no}}" d="${{p.r.map(ringPath).join(" ")}}"/>`;
  }});
  out += `<path class="tract-outline" d="${{tract.outline.map(ringPath).join(" ")}}"/>`;
  svg.innerHTML = out;

  const slider = document.getElementById("slider" + tract.tract_no);
  const readout = document.getElementById("readout" + tract.tract_no);
  const stats = document.getElementById("stats" + tract.tract_no);
  const paths = svg.querySelectorAll(".parcel");

  function dateFor(v){{ return new Date(ms0 + (ms1 - ms0) * (v / 1000)); }}

  function render(v){{
    const now = dateFor(v), iso = now.toISOString().slice(0, 10);
    let formed = 0, formedAc = 0, undated = 0, undatedAc = 0;
    tract.parcels.forEach((p, i) => {{
      const el = paths[i];
      if (p.d === null){{ undated++; undatedAc += p.ac || 0; return; }}
      const isFormed = p.d <= iso;
      if (isFormed){{ formed++; formedAc += p.ac || 0; }}
      const want = isFormed ? "parcel formed" : "parcel raw";
      if (el.getAttribute("class") !== want) el.setAttribute("class", want);
    }});
    readout.textContent = iso;
    const dated = tract.parcels.length - undated;
    stats.innerHTML =
      `<span>formed <b>${{formed}}</b> / ${{dated}} dated parcels</span>` +
      // "parcel acreage", not tract acreage: a boundary parcel is counted in
      // FULL here, so these can exceed the tract's own area. Labelling this
      // "platted acreage" would read as acreage inside the tract, which it is not.
      `<span>parcel acreage formed <b>${{formedAc.toFixed(1)}}</b></span>` +
      `<span>no recorded date <b>${{undated}}</b> (${{undatedAc.toFixed(1)}} ac)</span>`;
  }}

  slider.addEventListener("input", () => {{ stop(); render(+slider.value); }});

  let timer = null;
  const btn = document.querySelector(`.play-btn[data-target="${{tract.tract_no}}"]`);
  function stop(){{ if (timer){{ clearInterval(timer); timer = null; btn.textContent = "▶ Play"; }} }}
  btn.addEventListener("click", () => {{
    if (timer){{ stop(); return; }}
    if (+slider.value >= 1000) slider.value = 0;
    btn.textContent = "❙❙ Pause";
    timer = setInterval(() => {{
      const next = Math.min(1000, +slider.value + 4);
      slider.value = next; render(next);
      if (next >= 1000) stop();
    }}, 40);
  }});

  svg.addEventListener("mousemove", e => {{
    const el = e.target.closest(".parcel");
    if (!el){{ tip.style.opacity = 0; return; }}
    const p = tract.parcels[+el.dataset.i];
    tip.innerHTML = `<b>${{p.s ? p.s + (p.n ? " Sec " + p.n : "") : "No subdivision reference"}}</b>` +
      `APN ${{p.a}}<br>${{p.ac != null ? p.ac.toFixed(3) + " ac" : "acreage unknown"}}<br>` +
      (p.d ? `formed ${{p.d}}<br>instrument ${{p.i}}` : "no recorded formation date");
    const box = svg.getBoundingClientRect();
    tip.style.left = (e.clientX - box.left + 14) + "px";
    tip.style.top = (e.clientY - box.top + 14) + "px";
    tip.style.opacity = 1;
  }});
  svg.addEventListener("mouseleave", () => {{ tip.style.opacity = 0; }});

  render(0);
}});
</script>
"""

SECTION = """  <section class="tract" id="tract{tract_no}">
    <h2>Tract {tract_no}{acreage_label}</h2>
    <p class="meta">{parcel_count} parcels in the census{straddle}{first_plat}</p>
    <div class="map-frame">
      <svg class="map" id="map{tract_no}" viewBox="0 0 {view} {view}" preserveAspectRatio="xMidYMid meet"></svg>
      <div class="controls">
        <button class="play-btn" data-target="{tract_no}">▶ Play</button>
        <input type="range" id="slider{tract_no}" min="0" max="1000" value="0"
               aria-label="Tract {tract_no} formation date">
        <span class="date-readout" id="readout{tract_no}">{t0}</span>
      </div>
      <div class="stats-row" id="stats{tract_no}"></div>
    </div>
  </section>
"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("covid", type=int)
    ap.add_argument("--out", default=None)
    ap.add_argument("--simplify-ft", type=float, default=10.0)
    args = ap.parse_args(argv)

    with get_session() as session:
        cov = _covenant(session, args.covid)
        tracts = _tracts(session, args.covid, args.simplify_ft)
    if not tracts:
        raise SystemExit(f"covid {args.covid}: no tract with geometry and a parcel census")

    dated = [p["d"] for t in tracts for p in t["parcels"] if p["d"]]
    if not dated:
        raise SystemExit(f"covid {args.covid}: no parcel has a recorded formation date -- "
                         f"run app/gis/formation.derive_formation_from_plats first")
    parcel_total = sum(len(t["parcels"]) for t in tracts)
    undated_total = sum(1 for t in tracts for p in t["parcels"] if not p["d"])

    # Baseline is the covenant's own recording where known, so "before any of this
    # was platted" is a real moment rather than an arbitrary start.
    t0 = min(dated)
    if cov["recording_date"]:
        t0 = min(t0, cov["recording_date"].isoformat())
    t0 = (t0[:4] + "-01-01")
    t1 = max(max(dated), date.today().isoformat())

    facts = "".join(
        f'<div><span class="k">{k}</span><span class="v">{v}</span></div>'
        for k, v in [
            ("Declarant", (cov["declarant_raw"] or "—")[:34]),
            ("Stated acreage", f'{cov["stated_acreage"]:,.2f} ac' if cov["stated_acreage"] else "—"),
            ("Tracts mapped", str(len(tracts))),
            ("Parcels", f"{parcel_total:,}"),
            ("Formation dates", str(len(sorted(set(dated))))),
        ])

    sections = ""
    for t in tracts:
        first = sorted([p for p in t["parcels"] if p["d"]], key=lambda p: p["d"])
        first_note = ""
        if first:
            f0 = first[0]
            first_note = (f'. First formed: {f0["s"] or "?"}'
                          f'{" Sec " + f0["n"] if f0["n"] else ""} — {f0["d"]}')
        acreage_label = f' — {t["geom_acreage"]:,.2f} ac' if t["geom_acreage"] else ""
        straddle = (f', {t["straddling"]:,} of them straddling the tract boundary'
                    if t.get("straddling") else "")
        sections += SECTION.format(
            tract_no=t["tract_no"], view=VIEW, t0=t0, acreage_label=acreage_label,
            parcel_count=f'{len(t["parcels"]):,}', straddle=straddle, first_plat=first_note)

    html = HTML.format(
        covid=args.covid, county_name=cov["county_name"], state_code=cov["state_code"],
        headline="The lots themselves, coming into existence",
        facts=facts, sections=sections, data_json=json.dumps(tracts, separators=(",", ":")),
        t0=t0, t1=t1, simplify_ft=int(args.simplify_ft),
        parcel_total=f"{parcel_total:,}", undated_total=f"{undated_total:,}",
        tract_acreage_note=" + ".join(f'{t["geom_acreage"]:,.2f} ac' for t in tracts))

    out = args.out or os.path.join(
        os.environ.get("SCRATCH", "."), f"covid{args.covid}_formation_map.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"wrote {out} ({len(html):,} bytes)")
    print(f"  {len(tracts)} tract(s), {parcel_total:,} parcels, "
          f"{len(set(dated))} distinct formation dates, {t0} .. {t1}")
    print(f"  {undated_total:,} parcel(s) with no recorded formation date -- drawn grey, never formed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

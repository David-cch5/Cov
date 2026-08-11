"""Flask routes for the navigation app. Thin: every query lives in queries.py.

Read-only by construction -- there is no route that writes. Navigating the system
of record must never be the thing that changed it.
"""
import os

from flask import Flask, abort, redirect, render_template, request, send_file, url_for

from app.db.session import get_session
from app.web import queries

app = Flask(__name__, template_folder=os.path.join(os.path.dirname(__file__), "templates"))


@app.route("/")
def home():
    with get_session() as session:
        return render_template("home.html", covenants=queries.covenant_list(session),
                               coverage=queries.lineage_coverage(session))


@app.route("/covenant/<int:covid>")
def covenant(covid: int):
    with get_session() as session:
        cov = queries.covenant(session, covid)
        if cov is None:
            abort(404)
        censuses = {t["tract_no"]: queries.tract_parcels(session, covid, t["tract_no"])
                    for t in cov["tracts"]}
        from app.gis.formation_map import artifact_path
        has_map = os.path.exists(artifact_path(covid))
        return render_template("covenant.html", cov=cov, censuses=censuses, has_map=has_map)


@app.route("/parcel/<county_fips>/<apn>")
def parcel(county_fips: str, apn: str):
    with get_session() as session:
        p = queries.parcel(session, county_fips, apn)
        if p is None:
            abort(404)
        return render_template("parcel.html", p=p)


@app.route("/covenant/<int:covid>/map")
def covenant_map(covid: int):
    """Serve the covenant's formation map, built by the publish_map stage.

    Served rather than regenerated on request: building one walks every parcel's
    geometry, which is not something a page load should do. If it is missing, the
    stage either has not run or reported nothing to draw -- and the covenant page
    only links here when the file exists, so a 404 means it went missing since.
    """
    from app.gis.formation_map import artifact_path

    path = artifact_path(covid)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="text/html")


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return redirect(url_for("home"))
    with get_session() as session:
        results = queries.search(session, q)
    # One unambiguous hit goes straight there -- typing a covid should not make
    # you click a single-row result list.
    if len(results["covenants"]) == 1 and not results["parcels"]:
        return redirect(url_for("covenant", covid=results["covenants"][0]["covid"]))
    if len(results["parcels"]) == 1 and not results["covenants"]:
        hit = results["parcels"][0]
        return redirect(url_for("parcel", county_fips=hit["county_fips"], apn=hit["apn"]))
    return render_template("search.html", q=q, results=results)


@app.template_filter("num")
def _num(v, places=2):
    if v is None:
        return "—"
    try:
        return f"{float(v):,.{places}f}"
    except (TypeError, ValueError):
        return str(v)


@app.template_filter("dash")
def _dash(v):
    return "—" if v in (None, "") else v

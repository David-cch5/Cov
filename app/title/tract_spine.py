"""Walk a covenant's land forward from its own tract, one deed at a time.

The question this answers is the one the whole project exists for: given a lot
somebody is selling today, WHICH covenant encumbers it and how did the land get from
that covenant's original tract to this lot -- and, just as important, how much of the
original tract nobody has sold yet.

WHY NOT parcel_lineage. That table keys a split on the parent's APN, and Texas
counties do not keep retired APNs: the parent is exactly the row the county deletes.
It can therefore only record splits this project OBSERVES (app/gis/monitor.py), which
is why it holds nothing after 66 monitor runs. This spine is keyed on its own minted
nodes instead, so it can be built backwards out of deeds that are all still on
record.

TWO CHILDREN PER SPLIT, ALWAYS. A deed conveying part of a tract creates the piece
CONVEYED and the piece RETAINED. The remainder has no instrument of its own -- nobody
records a document for land they kept -- so a document-driven walk skips it, and then
cannot say what is left. record_split refuses to create one without the other.

ACREAGE IS A LEDGER (migration 0046), because one deed can convey two tracts, one
encumbered and one not. What the instrument recites is 'stated'; what lies inside
this covenant's tract is 'encumbered', and only that is safe to bill from. When they
disagree, reconcile() reports it and changes nothing: the cause is usually a
conveyance that reached outside the tract, and the fix is to read the deed.
"""
from __future__ import annotations

import re

from sqlalchemy import text

ACREAGE_BASES = ("stated", "encumbered", "derived", "gis")

# How far a disagreement between two bases can go before it is worth a human's
# attention. Deeds round, surveys differ, and a CAD's acreage is its own estimate --
# so a hundredth of an acre is noise. A tenth is somebody's land.
ACREAGE_TOLERANCE_ACRES = 0.05


def _label_for(covid: int, tract_no: int, path: str | None) -> str:
    """The readable half of a node's identity: "48339-4780-T1" for a root, then the
    split path beneath it ("...T1.2.1"). Regenerable on purpose -- the surrogate
    node_id is what anything else points at."""
    root = f"{covid}-T{tract_no}"
    return root if not path else f"{root}.{path}"


def create_root(session, covid: int, tract_no: int, *, source_id: int | None = None,
                stated_acreage: float | None = None,
                encumbered_acreage: float | None = None) -> int:
    """The spine's starting point: the covenant's own encumbered tract.

    Idempotent -- a covenant tract has exactly one root (enforced by
    tract_node_one_root_per_tract), and calling this twice returns the same node
    rather than raising, because the pipeline re-runs."""
    existing = session.execute(
        text("""SELECT node_id FROM tract_node
                 WHERE covid = :covid AND tract_no = :tract_no AND disposition = 'root'"""),
        {"covid": covid, "tract_no": tract_no}).scalar()
    if existing is not None:
        return existing

    node_id = session.execute(
        text("""INSERT INTO tract_node (node_label, covid, tract_no, disposition, source_id)
                VALUES (:label, :covid, :tract_no, 'root', :source_id)
                RETURNING node_id"""),
        {"label": _label_for(covid, tract_no, None), "covid": covid,
         "tract_no": tract_no, "source_id": source_id}).scalar()
    for basis, acres in (("stated", stated_acreage), ("encumbered", encumbered_acreage)):
        if acres is not None:
            set_acreage(session, node_id, basis, acres, source_id=source_id)
    return node_id


def set_acreage(session, node_id: int, basis: str, acreage: float, *,
                source_id: int | None = None, note: str | None = None) -> None:
    """Record one measurement on one basis. Replaces that basis only -- never the
    others, because their disagreement is the signal."""
    if basis not in ACREAGE_BASES:
        raise ValueError(f"unknown acreage basis {basis!r}; expected one of {ACREAGE_BASES}")
    if acreage is None or float(acreage) < 0:
        raise ValueError(f"acreage must be a non-negative number, got {acreage!r}")
    session.execute(
        text("""INSERT INTO tract_node_acreage (node_id, basis, acreage, source_id, note)
                VALUES (:node_id, :basis, :acreage, :source_id, :note)
                ON CONFLICT (node_id, basis) DO UPDATE SET
                    acreage = EXCLUDED.acreage, source_id = EXCLUDED.source_id,
                    note = EXCLUDED.note, recorded_at = now()"""),
        {"node_id": node_id, "basis": basis, "acreage": acreage,
         "source_id": source_id, "note": note})


def acreages(session, node_id: int) -> dict:
    return {r.basis: float(r.acreage) for r in session.execute(
        text("SELECT basis, acreage FROM tract_node_acreage WHERE node_id = :n"),
        {"n": node_id}).fetchall()}


def _next_child_path(session, parent_node_id: int) -> str:
    """Children are numbered in the order their splits are recorded, under the
    parent's own path -- so a label states the whole descent, not just the depth."""
    row = session.execute(
        text("""SELECT node_label, covid, tract_no FROM tract_node WHERE node_id = :n"""),
        {"n": parent_node_id}).fetchone()
    if row is None:
        raise ValueError(f"no tract_node {parent_node_id}")
    prefix = f"{row.covid}-T{row.tract_no}"
    parent_path = row.node_label[len(prefix):].lstrip(".")
    # Numbered by the child PATHS already in use, not by distinct instruments: two
    # plat rows legitimately share one recording instrument (a lot-keyed row and a
    # section-keyed row from the same filing), so counting instruments handed both
    # groups the same step and the second insert collided on tract_node_label_unique,
    # aborting the whole back-fill.
    used = session.execute(
        text("""SELECT DISTINCT node_label FROM tract_node WHERE parent_node_id = :n"""),
        {"n": parent_node_id}).fetchall()
    steps = set()
    for row in used:
        tail = row.node_label[len(prefix):].lstrip(".")
        segment = tail[len(parent_path):].lstrip(".") if parent_path else tail
        head = segment.split(".")[0]
        # LEADING digits only. Concatenating every digit in the segment turned a
        # platted label like "1P709" into 1709, so the next sibling was numbered 6123
        # -- a label that states nothing about the descent it is supposed to describe.
        leading = re.match(r"\d+", head)
        if leading:
            steps.add(int(leading.group(0)))
    step = (max(steps) + 1) if steps else 1
    return f"{parent_path}.{step}" if parent_path else str(step)


def record_split(session, parent_node_id: int, *, county_fips: str,
                 instrument_number: str, recording_date=None,
                 conveyed_stated_acreage: float | None = None,
                 conveyed_encumbered_acreage: float | None = None,
                 source_id: int | None = None,
                 conveyed_apn: str | None = None) -> dict:
    """One deed, two children: the piece conveyed and the piece retained.

    Both or neither. A split that records only what was sold leaves the remainder
    invisible, and the remainder is what says how much of the covenant's land is
    still unsold -- so this function is the only way to add a node, and it always
    adds the pair.

    The retained piece gets a 'derived' acreage (parent minus conveyed) ONLY when the
    conveyance is known to have stayed inside the tract -- that is, when the caller
    supplied an encumbered figure equal to the stated one, or supplied only one of
    them. When a deed conveyed more than this tract holds, subtracting its total
    would invent a negative remainder, so nothing is derived and reconcile() reports
    it instead.
    """
    parent = session.execute(
        text("""SELECT node_id, covid, tract_no, node_label FROM tract_node
                 WHERE node_id = :n"""), {"n": parent_node_id}).fetchone()
    if parent is None:
        raise ValueError(f"no tract_node {parent_node_id} to split")
    if not instrument_number or not str(instrument_number).strip():
        raise ValueError("a split needs the instrument that made it")

    path = _next_child_path(session, parent_node_id)
    made = {}
    for disposition, suffix in (("conveyed", "C"), ("retained", "R")):
        made[disposition] = session.execute(
            text("""INSERT INTO tract_node
                        (node_label, covid, tract_no, parent_node_id, disposition,
                         split_county_fips, split_instrument_number, split_recording_date,
                         county_fips, apn, source_id)
                    VALUES (:label, :covid, :tract_no, :parent, :disposition,
                            :cf, :instrument, :recorded, :apn_cf, :apn, :source_id)
                    RETURNING node_id"""),
            {"label": _label_for(parent.covid, parent.tract_no, f"{path}{suffix}"),
             "covid": parent.covid, "tract_no": parent.tract_no,
             "parent": parent_node_id, "disposition": disposition,
             "cf": county_fips, "instrument": str(instrument_number).strip(),
             "recorded": recording_date,
             "apn_cf": county_fips if (disposition == "conveyed" and conveyed_apn) else None,
             "apn": conveyed_apn if disposition == "conveyed" else None,
             "source_id": source_id}).scalar()

    conveyed = made["conveyed"]
    if conveyed_stated_acreage is not None:
        set_acreage(session, conveyed, "stated", conveyed_stated_acreage, source_id=source_id)
    if conveyed_encumbered_acreage is not None:
        set_acreage(session, conveyed, "encumbered", conveyed_encumbered_acreage,
                    source_id=source_id)

    # EVERY retained sibling is recomputed, not just the new one. Splitting a
    # 100-acre parent by 40 and then by 30 left the first remainder reading 60 while
    # the second read 30 -- 70 conveyed plus 90 retained out of 100 acres, and
    # reconcile could not see it because it sums only what left the parent.
    for sibling in session.execute(
            text("""SELECT node_id FROM tract_node
                     WHERE parent_node_id = :p AND disposition = 'retained'"""),
            {"p": parent_node_id}).fetchall():
        _derive_retained(session, parent_node_id, sibling.node_id, source_id)
    return made


def _derive_retained(session, parent_node_id: int, retained_node_id: int,
                     source_id: int | None) -> None:
    """Remainder = the parent's encumbered acreage minus everything conveyed out of
    it, and only when every conveyance says how much of ITSELF was encumbered.

    A deed that conveyed an encumbered tract and an unencumbered one together states
    an acreage larger than this covenant's land, so subtracting it would manufacture
    a remainder that is too small -- or negative. Rather than guess the split, this
    derives nothing and leaves reconcile() to say why.
    """
    parent_acres = acreages(session, parent_node_id)
    basis = "encumbered" if "encumbered" in parent_acres else "stated"
    if basis not in parent_acres:
        return  # nothing to derive from

    siblings = session.execute(
        text("""SELECT n.node_id FROM tract_node n
                 WHERE n.parent_node_id = :p
                   AND n.disposition IN ('conveyed', 'platted')"""),
        {"p": parent_node_id}).fetchall()
    total_out = 0.0
    for sib in siblings:
        sib_acres = acreages(session, sib.node_id)
        if "encumbered" in sib_acres:
            total_out += sib_acres["encumbered"]
        elif "stated" in sib_acres:
            total_out += sib_acres["stated"]
        else:
            return  # a conveyance of unknown size makes the remainder unknowable

    remainder = round(parent_acres[basis] - total_out, 3)
    if remainder < 0:
        return  # more conveyed than the parent held: a finding, not a derivation
    set_acreage(session, retained_node_id, "derived", remainder, source_id=source_id,
                note=f"parent {basis} {parent_acres[basis]} less {total_out} conveyed")


def attach_parcel(session, node_id: int, county_fips: str, apn: str, *,
                  plat_id: int | None = None, gis_acreage: float | None = None,
                  source_id: int | None = None) -> None:
    """A node acquires its APN at the leaf, when its owner becomes identifiable or it
    is platted -- the parcel must already exist in `parcel` (foreign key), because an
    APN this project has never read is not evidence of anything."""
    session.execute(
        text("""UPDATE tract_node SET county_fips = :cf, apn = :apn,
                       plat_id = COALESCE(:plat_id, plat_id), updated_at = now()
                 WHERE node_id = :n"""),
        {"cf": county_fips, "apn": apn, "plat_id": plat_id, "n": node_id})
    if gis_acreage is not None:
        set_acreage(session, node_id, "gis", gis_acreage, source_id=source_id)


def sync_acreage_from_gis(session, covid: int, tract_no: int = 1) -> dict:
    """Measure every APN-bearing node from geometry: its own area, and how much of it
    lies inside this covenant's tract.

    'gis'        the parcel's whole area, from the county's own polygon
    'encumbered' the area of the INTERSECTION with the tract -- what a fee accrues on

    ONLY THE ANCHORED POLYGON IS USED. `tract.geom` is a boundary resolved from the
    deed's own metes and bounds and tied to real coordinates. `tract.approximate_geom`
    is the shape-valid, position-unconfirmed fallback from app/gis/geocode_anchor.py,
    and intersecting a parcel with a polygon that is merely the right SHAPE in roughly
    the right PLACE would manufacture an encumbered acreage -- a number that looks
    like evidence and would go straight under a fee. A tract with no real geom gets
    no encumbered figure at all, and says so.

    A parcel whose intersection exceeds its own area is impossible and is reported
    rather than written: it means one of the two geometries is invalid.
    """
    tract = session.execute(
        text("""SELECT geom IS NOT NULL AS anchored, approximate_geom IS NOT NULL AS approx
                  FROM tract WHERE covid = :covid AND tract_no = :tract_no"""),
        {"covid": covid, "tract_no": tract_no}).fetchone()
    if tract is None:
        raise ValueError(f"no tract for covid {covid} tract {tract_no}")
    if not tract.anchored:
        return {"covid": covid, "tract_no": tract_no, "measured": 0, "skipped_no_tract_geom": True,
                "reason": ("this tract has only an approximate boundary, so no encumbered "
                           "acreage can be measured from it"
                           if tract.approx else "this tract has no boundary at all"),
                "impossible": []}

    rows = session.execute(text("""
        SELECT n.node_id,
               round((ST_Area(p.geom::geography) / 4046.8564224)::numeric, 3) AS own_acres,
               round((ST_Area(ST_Intersection(p.geom, t.geom)::geography)
                      / 4046.8564224)::numeric, 3) AS inside_acres
          FROM tract_node n
          JOIN parcel p ON p.county_fips = n.county_fips AND p.apn = n.apn
          JOIN tract t ON t.covid = n.covid AND t.tract_no = n.tract_no
         WHERE n.covid = :covid AND n.tract_no = :tract_no AND n.apn IS NOT NULL
           AND p.geom IS NOT NULL AND t.geom IS NOT NULL
           AND ST_IsValid(p.geom) AND ST_IsValid(t.geom)
    """), {"covid": covid, "tract_no": tract_no}).fetchall()

    measured, impossible = 0, []
    for r in rows:
        own, inside = float(r.own_acres), float(r.inside_acres)
        if inside - own > ACREAGE_TOLERANCE_ACRES:
            impossible.append({"node_id": r.node_id, "own_acres": own, "inside_acres": inside})
            continue
        set_acreage(session, r.node_id, "gis", own,
                    note="parcel polygon area, county GIS")
        # A DEED-DERIVED ENCUMBERED FIGURE IS NOT OVERWRITTEN. If record_split already
        # recorded what an instrument said lies inside this tract, replacing it with the
        # measured intersection destroys exactly the disagreement the ledger exists to
        # surface -- a deed reciting 30 encumbered acres against a measured 24.5 would
        # leave no trace of the 5.5-acre gap. The measurement is still available as
        # 'gis'; the conflict is flagged on the node instead of resolved silently.
        existing = acreages(session, r.node_id)
        if "encumbered" in existing and abs(existing["encumbered"] - inside) > ACREAGE_TOLERANCE_ACRES:
            session.execute(
                text("""UPDATE tract_node SET review_reason = :why, updated_at = now()
                         WHERE node_id = :n"""),
                {"n": r.node_id,
                 "why": (f"deed-derived encumbered {existing['encumbered']} ac vs measured "
                         f"intersection {inside} ac -- read the deed; the measurement is "
                         f"recorded as 'gis' and the deed's figure is kept")})
        elif "encumbered" not in existing:
            set_acreage(session, r.node_id, "encumbered", inside,
                        note="intersection with this covenant's anchored tract boundary")
        measured += 1

    unmeasurable = session.execute(text("""
        SELECT count(*) FROM tract_node n
          LEFT JOIN parcel p ON p.county_fips = n.county_fips AND p.apn = n.apn
         WHERE n.covid = :covid AND n.tract_no = :tract_no AND n.apn IS NOT NULL
           AND (p.geom IS NULL OR NOT ST_IsValid(p.geom))
    """), {"covid": covid, "tract_no": tract_no}).scalar()
    return {"covid": covid, "tract_no": tract_no, "measured": measured,
            "skipped_no_tract_geom": False,
            "parcels_without_usable_geometry": unmeasurable,
            "impossible": impossible}


def backfill_from_plats(session, covid: int, tract_no: int = 1, *,
                        source_id: int | None = None) -> dict:
    """Build the spine that the record already supports: the tract, the lots a plat
    made out of it, and the raw acreage those plats left behind.

    This is the only split event back-fillable today. The transfers on record are
    LOT-level deed histories -- each conveying a whole lot, not part of a tract -- so
    they say nothing about how the tract itself was divided. What does say it is
    parcel.plat_id and parcel.formed_by_instrument: the recorded plat that created
    each lot, already established and dated (app/gis/plat_link.py).

    One 'retained' node per plat carries what that filing left unplatted, so the
    remainder is a node in the spine rather than only a number on the tract. The node
    is always created -- the plat did leave a remainder -- but its ACREAGE is derived
    only where the parent's encumbered total is known and every sibling is measured,
    the same refusal as record_split. On a tract whose census still holds unplatted
    parcels that derivation is unavailable, and an unmeasured remainder is the honest
    answer rather than arithmetic on an incomplete set.

    Idempotent: a parcel appears once per tract (tract_node_one_node_per_parcel_per_tract).
    """
    root = create_root(session, covid, tract_no, source_id=source_id)
    lots = session.execute(text("""
        SELECT DISTINCT p.county_fips, p.apn, p.plat_id, p.formed_by_instrument,
                        p.formed_date, pl.recording_instrument
          FROM parcel_covenant pc
          JOIN parcel p ON p.county_fips = pc.county_fips AND p.apn = pc.apn
          JOIN plat pl ON pl.plat_id = p.plat_id
         WHERE pc.covid = :covid AND pc.tract_no = :tract_no
           -- Latest classification run only, as every other consumer of this table
           -- does (app/pipeline/stages.py, app/web/queries.py, app/gis/formation.py).
           -- Without it a parcel matched in run 3 and dropped in run 5 -- exactly what
           -- re-anchoring a tract produces -- still gets a node, and then a measured
           -- encumbered acreage that a fee would accrue on.
           AND pc.run_seq = (SELECT max(run_seq) FROM parcel_covenant
                              WHERE covid = pc.covid AND tract_no = pc.tract_no)
           AND p.plat_id IS NOT NULL AND p.formed_by_instrument IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM tract_node n
                            WHERE n.covid = pc.covid AND n.tract_no = pc.tract_no
                              AND n.county_fips = p.county_fips AND n.apn = p.apn)
         ORDER BY p.formed_date, p.apn
    """), {"covid": covid, "tract_no": tract_no}).fetchall()

    by_plat: dict[tuple, list] = {}
    for lot in lots:
        by_plat.setdefault((lot.plat_id, lot.formed_by_instrument, lot.formed_date), []).append(lot)

    made = 0
    for (plat_id, instrument, formed), group in sorted(by_plat.items(), key=lambda kv: str(kv[0][2])):
        path = _next_child_path(session, root)
        for i, lot in enumerate(group, start=1):
            session.execute(text("""
                INSERT INTO tract_node
                    (node_label, covid, tract_no, parent_node_id, disposition,
                     split_county_fips, split_instrument_number, split_recording_date,
                     county_fips, apn, plat_id, source_id)
                VALUES (:label, :covid, :tract_no, :root, 'platted',
                        :cf, :instrument, :recorded, :cf, :apn, :plat_id, :source_id)
            """), {"label": _label_for(covid, tract_no, f"{path}P{i}"), "covid": covid,
                   "tract_no": tract_no, "root": root, "cf": lot.county_fips,
                   "instrument": instrument, "recorded": formed, "apn": lot.apn,
                   "plat_id": plat_id, "source_id": source_id})
            made += 1

    # A NODE'S plat_id FOLLOWS ITS PARCEL. plat_link legitimately re-points a parcel at
    # a different filing (a mis-keyed lot row corrected to its real phase plat), and a
    # node recorded before that keeps the old plat_id -- which then blocks deleting the
    # superseded plat row and, worse, makes the node cite a filing its parcel no longer
    # claims. Re-synced on every back-fill so the spine cannot drift from the census.
    session.execute(text("""
        UPDATE tract_node n SET plat_id = p.plat_id, updated_at = now()
          FROM parcel p
         WHERE p.county_fips = n.county_fips AND p.apn = n.apn
           AND n.covid = :covid AND n.tract_no = :tract_no
           AND n.apn IS NOT NULL AND p.plat_id IS NOT NULL
           AND (n.plat_id IS DISTINCT FROM p.plat_id)
    """), {"covid": covid, "tract_no": tract_no})

    # THE REMAINDER EACH PLAT LEFT RAW, ensured over EVERY plat event on this tract --
    # not only ones this call added. Creating it inside the insert loop meant an
    # already-back-filled tract got none, because that loop only sees parcels without
    # nodes: 11 tracts came out with 5,800 platted lots and zero remainders, so "how
    # much of this covenant's land is still unplatted" -- the quantity
    # app/gis/monitor.py watches and the reason this spine exists -- still had nowhere
    # to live.
    remainders = 0
    events = session.execute(text("""
        SELECT split_instrument_number AS instrument, min(split_recording_date) AS recorded,
               min(split_county_fips) AS cf, min(node_label) AS sample_label
          FROM tract_node
         WHERE parent_node_id = :root AND disposition = 'platted'
         GROUP BY split_instrument_number
    """), {"root": root}).fetchall()
    for ev in events:
        # The platted children of one filing share a path segment ("...T1.3P18"); the
        # remainder is that segment's own R sibling ("...T1.3R").
        prefix = f"{covid}-T{tract_no}"
        segment = ev.sample_label[len(prefix):].lstrip(".").split("P")[0]
        label = _label_for(covid, tract_no, f"{segment}R")
        made_one = session.execute(text("""
            INSERT INTO tract_node
                (node_label, covid, tract_no, parent_node_id, disposition,
                 split_county_fips, split_instrument_number, split_recording_date, source_id)
            VALUES (:label, :covid, :tract_no, :root, 'retained',
                    :cf, :instrument, :recorded, :source_id)
            ON CONFLICT DO NOTHING
            RETURNING node_id
        """), {"label": label, "covid": covid, "tract_no": tract_no, "root": root,
               "cf": ev.cf, "instrument": ev.instrument, "recorded": ev.recorded,
               "source_id": source_id}).scalar()
        if made_one is not None:
            remainders += 1

    for sibling in session.execute(
            text("""SELECT node_id FROM tract_node
                     WHERE parent_node_id = :p AND disposition = 'retained'"""),
            {"p": root}).fetchall():
        _derive_retained(session, root, sibling.node_id, source_id)

    return {"covid": covid, "tract_no": tract_no, "root_node_id": root,
            "plat_events": len(by_plat), "lots_added": made,
            "remainder_nodes": remainders}


def walk_down(session, node_id: int) -> list[dict]:
    """Every descendant, breadth-first, with its depth -- the covenant -> child ->
    child's child -> current lot traversal. Cycle-guarded: a spine should be a tree,
    and a cycle is a bug that must not hang a page render."""
    rows = session.execute(text("""
        WITH RECURSIVE descent AS (
            SELECT n.*, 0 AS depth, ARRAY[n.node_id] AS seen
              FROM tract_node n WHERE n.node_id = :root
            UNION ALL
            SELECT c.*, d.depth + 1, d.seen || c.node_id
              FROM tract_node c JOIN descent d ON c.parent_node_id = d.node_id
             WHERE NOT c.node_id = ANY(d.seen) AND d.depth < 40
        )
        SELECT node_id, node_label, disposition, depth, split_instrument_number,
               split_recording_date, county_fips, apn, plat_id
          FROM descent ORDER BY depth, node_label
    """), {"root": node_id}).fetchall()
    return [dict(r._mapping) for r in rows]


def walk_up(session, node_id: int) -> list[dict]:
    """From a lot back to the covenant's original tract -- the return leg, and the
    one a payoff statement needs: this lot came from that tract, by these deeds."""
    rows = session.execute(text("""
        WITH RECURSIVE ascent AS (
            SELECT n.*, 0 AS steps, ARRAY[n.node_id] AS seen
              FROM tract_node n WHERE n.node_id = :leaf
            UNION ALL
            SELECT p.*, a.steps + 1, a.seen || p.node_id
              FROM tract_node p JOIN ascent a ON a.parent_node_id = p.node_id
             WHERE NOT p.node_id = ANY(a.seen) AND a.steps < 40
        )
        SELECT node_id, node_label, disposition, steps, split_instrument_number,
               split_recording_date, covid, tract_no
          FROM ascent ORDER BY steps
    """), {"leaf": node_id}).fetchall()
    return [dict(r._mapping) for r in rows]


def node_for_parcel(session, county_fips: str, apn: str) -> list[dict]:
    """Which spine node(s) is this parcel? More than one is possible and legitimate:
    a parcel can sit under two covenants' tracts."""
    rows = session.execute(
        text("""SELECT node_id, node_label, covid, tract_no FROM tract_node
                 WHERE county_fips = :cf AND apn = :apn ORDER BY covid, tract_no"""),
        {"cf": county_fips, "apn": apn}).fetchall()
    return [dict(r._mapping) for r in rows]


def reconcile(session, covid: int, tract_no: int = 1) -> dict:
    """Report where a tract's own arithmetic does not hold. Changes nothing.

    Three findings, each with a real cause worth chasing rather than smoothing:

      over_conveyed     children convey more than the parent held. The commonest
                        cause is the one this design was corrected for: a deed that
                        conveyed an encumbered tract AND an unencumbered one, whose
                        stated acreage is therefore not this covenant's acreage.
      basis_conflict    two bases disagree beyond tolerance on the same node -- e.g.
                        a CAD's gis acreage against the deed's stated figure.
      unmeasured        a conveyance with no acreage on any basis, which makes every
                        remainder downstream of it unknowable.
    """
    nodes = session.execute(
        text("""SELECT node_id, node_label, disposition, parent_node_id,
                       split_instrument_number
                  FROM tract_node WHERE covid = :covid AND tract_no = :tract_no"""),
        {"covid": covid, "tract_no": tract_no}).fetchall()
    by_id = {n.node_id: n for n in nodes}
    acres = {n.node_id: acreages(session, n.node_id) for n in nodes}

    over_conveyed, basis_conflict, unmeasured = [], [], []
    children: dict[int, list] = {}
    for n in nodes:
        if n.parent_node_id is not None:
            children.setdefault(n.parent_node_id, []).append(n)

    # A BOUNDARY PARCEL IS SUPPOSED TO DISAGREE WITH ITSELF. 'gis' is the whole
    # parcel and 'encumbered' is the part inside the tract, so on a lot straddling the
    # tract line they differ BY DESIGN -- that difference is the measurement, not a
    # fault. Confirmed on covid 4440: all 26 flagged nodes were classified 'boundary',
    # one of them 56% inside. So that pair is only compared where the two should
    # agree: a parcel classified 'interior' lies wholly within the tract, and there a
    # gap between its own area and the intersection means one geometry is wrong.
    interior = {r.apn for r in session.execute(
        text("""SELECT DISTINCT pc.apn FROM parcel_covenant pc
                 WHERE pc.covid = :covid AND pc.tract_no = :tract_no
                   AND pc.classification = 'interior'
                   AND pc.run_seq = (SELECT max(run_seq) FROM parcel_covenant
                                      WHERE covid = pc.covid AND tract_no = pc.tract_no)"""),
        {"covid": covid, "tract_no": tract_no}).fetchall()}
    node_apn = {n.node_id: n.apn for n in session.execute(
        text("""SELECT node_id, apn FROM tract_node
                 WHERE covid = :covid AND tract_no = :tract_no"""),
        {"covid": covid, "tract_no": tract_no}).fetchall()}

    for node_id, own in acres.items():
        pair = [(b, v) for b, v in own.items() if b in ("stated", "encumbered", "gis")]
        is_interior = node_apn.get(node_id) in interior
        for i, (b1, v1) in enumerate(pair):
            for b2, v2 in pair[i + 1:]:
                if {b1, b2} == {"gis", "encumbered"} and not is_interior:
                    continue  # a boundary lot: the gap IS the part outside the tract
                if abs(v1 - v2) > ACREAGE_TOLERANCE_ACRES:
                    basis_conflict.append({
                        "node_id": node_id, "node_label": by_id[node_id].node_label,
                        "bases": (b1, b2), "acreages": (v1, v2),
                        "difference_acres": round(abs(v1 - v2), 3)})
        node = by_id[node_id]
        if node.disposition == "conveyed" and not own:
            unmeasured.append({"node_id": node_id, "node_label": node.node_label,
                               "instrument": node.split_instrument_number})

    for parent_id, kids in children.items():
        parent = acres.get(parent_id, {})
        held = parent.get("encumbered", parent.get("stated"))
        if held is None:
            continue
        out = 0.0
        for kid in kids:
            # 'platted' counts as land that left the parent, same as 'conveyed'.
            # Checking only 'conveyed' predated migration 0047 and made every
            # back-filled tract reconcile by construction: with nothing but platted
            # children, `out` stayed 0.0 and no excess could ever be reported.
            if kid.disposition not in ("conveyed", "platted"):
                continue
            kid_acres = acres.get(kid.node_id, {})
            out += kid_acres.get("encumbered", kid_acres.get("stated", 0.0))
        if out - held > ACREAGE_TOLERANCE_ACRES:
            over_conveyed.append({
                "node_id": parent_id, "node_label": by_id[parent_id].node_label,
                "parent_held_acres": held, "conveyed_out_acres": round(out, 3),
                "excess_acres": round(out - held, 3),
                "likely_cause": "a deed conveying this tract together with land "
                                "outside it -- its stated acreage is about the deed, "
                                "not about this covenant's land"})

    return {"covid": covid, "tract_no": tract_no, "nodes": len(nodes),
            "over_conveyed": over_conveyed, "basis_conflict": basis_conflict,
            "unmeasured_conveyances": unmeasured,
            "reconciles": not (over_conveyed or basis_conflict or unmeasured)}

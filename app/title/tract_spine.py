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
    used = session.execute(
        text("""SELECT count(DISTINCT split_instrument_number) FROM tract_node
                 WHERE parent_node_id = :n"""), {"n": parent_node_id}).scalar() or 0
    step = used + 1
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

    _derive_retained(session, parent_node_id, made["retained"], source_id)
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
                 WHERE n.parent_node_id = :p AND n.disposition = 'conveyed'"""),
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

    for node_id, own in acres.items():
        pair = [(b, v) for b, v in own.items() if b in ("stated", "encumbered", "gis")]
        for i, (b1, v1) in enumerate(pair):
            for b2, v2 in pair[i + 1:]:
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
            if kid.disposition != "conveyed":
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

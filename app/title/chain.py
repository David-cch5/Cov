"""Chain-of-title walker (BUILD_SPEC.md Sec. 4/5): given a covid, finds every
recorded conveyance affecting EVERY parcel matched to its tract at or after
the covenant's own recording date, links grantor->grantee in sequence per
parcel, and classifies each real Transfer of Title as fee-exempt or
fee-owed against that covenant's own template_version_id exemption rules
(covenant_template_exemption) -- writing durable covenant.transfer rows,
one set per parcel (the widened (county_fips, instrument_number,
recording_date, parcel_apn) key exists precisely because a single
instrument routinely conveys a whole group of lots at once -- confirmed
directly: covid 3595's 6 lots share the exact same 2 historical sales).

THREE DATA SOURCES, IN PRIORITY ORDER -- learned the hard way on covid 2497
(Bexar): the first version of this walker only searched the county CLERK's
recorder portal (grantor/grantee name search, app/recorder/adapters/
publicsearch.py) and came up with a chain whose last known holder didn't
match the parcel's current owner of record. Checking the county APPRAISAL
DISTRICT's own website (a third system, distinct from both the ArcGIS
layer county_gis_registry otherwise points at and the recorder portal in
county_recorder_registry) turned up two better sources, both recorded as
quirks on county_gis_registry:

  - cad_deed_history_url (migration 0019, confirmed for Bexar): some CADs
    ("Harris Govern" PACS) publish their own indexed deed history per
    property as a plain unauthenticated JSON GET
    (app/title/cad_deed_history.py) -- complete, deterministic, no
    name-search ambiguity, though Texas being non-disclosure means no sale
    price. It revealed a foreclosure and a subsequent resale that neither
    an address-text search nor a per-grantee name search on the recorder
    portal ever surfaced.
  - cad_sales_data_url (migration 0022, confirmed for Douglas County, CO):
    a full-disclosure state's assessor sales-history table
    (app/title/co_assessor_sales.py) -- same kind of complete, per-parcel,
    correctly-ordered history, but with an ACTUAL disclosed SALE_PRICE
    included, captured directly into transfer.consideration_amount.

Both CAD paths already separate real conveyances from everything else, so
no DOC-TYPE classification is needed there. Only when NEITHER quirk is
present does this fall back to the recorder-portal name-walk
(_walk_via_recorder_portal) built for the first county tried, which is
real but demonstrably less complete (see its own docstring).

EXEMPTION CLASSIFICATION is deliberately conservative in every path: only
two categories are auto-detected --
  - pre_effective_date: the transfer's own recording_date is before the
    covenant's own template's cutoff (covenant_template_exemption,
    checked once per walk via _fetch_template_exemption_rules -- a fixed
    calendar date for most templates, e.g. V01's 2013-01-01, or the
    covenant's own recording_date for the spousal-family templates).
    Checked first, ahead of the other two, since it's a plain date
    comparison that doesn't depend on any name-matching heuristic.
  - declarant_sale: grantor name matches the covenant's own declarant_raw
    (an identity check any of the three sources can establish with
    confidence).
  - foreclosure: the CAD deed history's own deed_type_cd/deed_type_desc
    says so (only available via that one path -- the recorder portal's
    DOC TYPE vocabulary doesn't reliably distinguish this, and it hasn't
    come up yet in the assessor sales-data path).
Three templates (V02, V03, V12 -- migration 0030, confirmed directly in
each one's own real text) require a Grantor's affidavit filed in the OPR
before death_probate/foreclosure/affiliate_transaction/trustee_unidentified
actually apply -- a category match under one of those templates still gets
recorded (a real, positive signal), but with review_flag=True and half
confidence rather than the usual full 1.0, since no index alone can
confirm the affidavit was filed (see _affidavit_gate_note).
Every other category in covenant_template_exemption (spousal, death/
probate, government/nonprofit, beneficiary/trustee conveyances, ...) needs
the deed's own recitals or outside corroboration a bare grantor/grantee
name can't establish -- an unclassified transfer is recorded fee-owed with
exemption_category=NULL and review_flag=True, never silently assumed
either way. CLAUDE.md: never fabricate title data.

Also flags -- rather than silently accepting -- any parcel whose walk ends
on a holder that doesn't match its current owner of record (rare with a
CAD-backed path, since that's usually the same data the current-owner
field is itself derived from, but still checked as a cheap, real
correctness guard). One combined note covers every gapped parcel on the
covenant's own review_reason -- see _update_covenant_gap_notes.
"""
import re
from datetime import date, datetime, timezone

from sqlalchemy import text

from app.db.repository import insert_source, upsert_contact, upsert_transfer
from app.queue.job_queue import run_with_job_queue
from app.recorder.adapters import publicsearch
from app.recorder.session import recorder_context
from app.title import cad_deed_history, co_assessor_sales

MAX_HOPS = 10

CONVEYANCE_DOC_TYPES = {
    "DEED", "WARRANTY DEED", "GENERAL WARRANTY DEED", "SPECIAL WARRANTY DEED",
    "QUITCLAIM DEED", "QUIT CLAIM DEED", "TRUSTEE'S DEED", "TRUSTEE DEED",
    "EXECUTOR'S DEED", "EXECUTORS DEED", "ADMINISTRATOR'S DEED", "GIFT DEED",
    "DEED OF GIFT", "CORRECTION DEED", "SHERIFF'S DEED", "SHERIFFS DEED", "TAX DEED",
}
NON_CONVEYANCE_DOC_TYPES = {
    "DECLARATION", "DEED OF TRUST", "RELEASE", "PARTIAL RELEASE", "RELEASE OF LIEN",
    "RELEASE OF STATE TAX LIEN", "MODIFICATION", "UCC 1 REAL PROPERTY", "UCC", "UCC1",
    "LIEN", "MECHANICS LIEN", "MECHANIC LIEN AFFIDAVIT", "JUDGMENT", "AFFIDAVIT",
    "NOTICE", "EASEMENT", "AGREEMENT", "APPOINTMENT", "ASSIGNMENT", "BOND",
    "CERTIFIED COPY", "EXTENSION", "LIS PENDENS", "RESOLUTION",
}
FORECLOSURE_DEED_TYPE_MARKERS = {"FC", "FORECLOSURE"}

# a trailing "w/ vendor's lien" (or "with vendor's lien") is a financing detail, not a
# distinct instrument type -- confirmed real on covid 3297/Montgomery: "WARRANTY DEED
# W/VENDORS LIEN" is a genuine conveyance (same as the already-recognized "WARRANTY
# DEED", just noting the seller retained a lien for owner/builder financing, extremely
# common in Texas) but didn't match CONVEYANCE_DOC_TYPES' exact strings at all, so it
# was silently dropped as if it were a non-conveyance.
_VENDORS_LIEN_SUFFIX_RE = re.compile(r"\s*(W/|WITH)\s*VENDOR'?S?\s*LIEN\s*$")


def _normalize_doc_type(doc_type: str) -> str:
    return _VENDORS_LIEN_SUFFIX_RE.sub("", doc_type).strip()


# names like "HFG-CENTERRA DEVELOPMENT, LP., A TEXAS LIMITED PARTNERSHIP" or
# "EFRAIM ABRAMOFF, AN INDIVIDUAL RESIDING IN BEXAR COUNTY" carry a
# descriptive suffix after the first comma that a recorder/CAD index's bare
# grantor/grantee field never includes -- strip it before comparing.
def _core_name(name: str) -> set[str]:
    core = (name or "").split(",")[0]
    core = re.sub(r"[^\w\s]", " ", core).upper()
    return set(core.split())


def _names_match(name_a: str, name_b: str) -> bool:
    a, b = _core_name(name_a), _core_name(name_b)
    if not a or not b:
        return False
    return a == b or a.issubset(b) or b.issubset(a)


def _parse_lots(lot_str: str | None) -> set[str]:
    if not lot_str or lot_str.strip().upper() == "N/A":
        return set()
    return {tok.strip() for tok in lot_str.split(",") if tok.strip()}


def _parse_lot_range(low: str | None, high: str | None) -> set[str]:
    """Montgomery's own quirk: a single LOW LOT/HIGH LOT pair spanning a
    range (e.g. a subdivision plat or a bulk deed covering several lots at
    once), rather than the single "LOT" column Denton/Nueces/Collin/Bexar
    all expose. Expanded to every lot in the (inclusive) range when both
    ends are plain integers; otherwise left as the two endpoints so an
    exact-match comparison still works even though a real range can't be
    inferred (e.g. "5A"-style lot numbers)."""
    low = (low or "").strip()
    high = (high or "").strip()
    low = "" if low.upper() == "N/A" else low
    high = "" if high.upper() == "N/A" else high
    if not low and not high:
        return set()
    low = low or high
    high = high or low
    if low == high:
        return {low}
    if low.isdigit() and high.isdigit():
        lo, hi = sorted((int(low), int(high)))
        return {str(n) for n in range(lo, hi + 1)}
    return {low, high}


def _row_lots(row: dict) -> set[str]:
    """Lot numbers for a recorder-portal result row. Most GovOS PublicSearch
    counties (Denton, Nueces, Collin, Bexar) expose a single "LOT" column;
    Montgomery's instance instead exposes "HIGH LOT"/"LOW LOT" and never a
    "LOT" key at all -- confirmed real on covid 4780. Before this, every
    Montgomery row's row.get("LOT") returned None, _parse_lots(None) was
    always the empty set, and _matches_anchor's "both sides must be
    non-empty to reject" guard let every row through unfiltered -- i.e. lot-
    based candidate filtering was silently a no-op for this county."""
    if "LOT" in row:
        return _parse_lots(row.get("LOT"))
    if "HIGH LOT" in row or "LOW LOT" in row:
        return _parse_lot_range(row.get("LOW LOT"), row.get("HIGH LOT"))
    return set()


def _blocks_match(block_a: str | None, block_b: str | None) -> bool:
    a = (block_a or "").strip().upper()
    b = (block_b or "").strip().upper()
    if not a or a == "N/A" or not b or b == "N/A":
        return True  # can't compare -- don't let a missing field reject a real match
    return a == b


def _anchor_lot_is_unreliable(anchor: dict) -> bool:
    """Confirmed real on covid 3297/Montgomery: the anchor (the covenant's
    own DECLARATION) is indexed with HIGH LOT/LOW LOT both "263", but its own
    COMMENT reads "L263 GLENEAGLES S4A A583 ET AL" -- the recorder's index
    only captured the FIRST lot mentioned in a subdivision-wide document's
    legal description, "ET AL" signaling there's (many) more. Using that
    single indexed lot as a strict filter silently rejected every other
    real lot the same declaration actually covers."""
    return "ET AL" in (anchor.get("COMMENT") or "").upper()


def _subdivisions_match(subdivision_a: str | None, subdivision_b: str | None) -> bool:
    """Coarser fallback correlator for when lot-based matching can't be
    trusted (see _anchor_lot_is_unreliable) -- keyword-subset comparison,
    not a literal phrase match, since spelling varies even within this
    vendor's own data (e.g. "CRECENT COVE" vs "CRESCENT COVE", confirmed
    real on covid 4780's own recorder index)."""
    a = set((subdivision_a or "").upper().split())
    b = set((subdivision_b or "").upper().split())
    if not a or a == {"N/A"} or not b or b == {"N/A"}:
        return True  # can't compare -- don't let a missing field reject a real match
    return a <= b or b <= a


_DIRECTIONAL_PREFIXES = {"N", "S", "E", "W", "NE", "NW", "SE", "SW"}


def _address_seed(situs: str) -> str:
    """The recorder-portal quick search appears to match the OCR/index text
    as an exact phrase (confirmed on covid 3297/Montgomery: "16790 THRASHER"
    -- house number + street name, skipping the directional -- returns 0
    rows, while "16790 N THRASHER" -- the real phrase as it appears in the
    document -- returns 3, including the actual target deed). A plain first-
    two-tokens seed silently breaks for any situs address with a directional
    prefix (house number + "N"/"S"/"E"/"W" is a near-useless second token,
    e.g. "16790 N"), so a third token is pulled in whenever the second one is
    a directional abbreviation."""
    tokens = situs.split()
    if len(tokens) >= 2 and tokens[1].rstrip(".,").upper() in _DIRECTIONAL_PREFIXES:
        return " ".join(tokens[:3])
    return " ".join(tokens[:2])


def _parse_slash_date(s: str) -> date | None:
    try:
        return datetime.strptime(s.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def _parse_epoch_ms_date(ms) -> date | None:
    if ms is None:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).date()


def _fetch_template_exemption_rules(session, template_version_id: str | None) -> dict[str, dict]:
    """Every exemption rule this covenant's own template defines, keyed by
    category_code (covenant_template_exemption, migration 0001) --
    cutoff_date/cutoff_basis/clause_reference/requires_grantor_affidavit
    per category. Empty dict if the template isn't known -- never assume a
    rule that isn't actually seeded."""
    if not template_version_id:
        return {}
    rows = session.execute(
        text("""
            SELECT category_code, cutoff_date, cutoff_basis, clause_reference, requires_grantor_affidavit
            FROM covenant_template_exemption
            WHERE template_version_id = :tvid
        """), {"tvid": template_version_id},
    ).fetchall()
    return {
        r.category_code: {
            "cutoff_date": r.cutoff_date, "cutoff_basis": r.cutoff_basis,
            "clause_reference": r.clause_reference, "requires_grantor_affidavit": r.requires_grantor_affidavit,
        }
        for r in rows
    }


def _affidavit_gate_note(category: str | None, rules: dict[str, dict]) -> str | None:
    """Found in real text while reviewing V02/V03/V12 (migration 0030,
    these three templates' own Section 6 EXEMPTIONS clause, last sentence):
    death_probate/foreclosure/affiliate_transaction/trustee_unidentified
    only actually apply under those templates if the Grantor filed a
    supporting affidavit in the OPR -- something no recorder/CAD index
    (what every walker here works from) can confirm either way. A category
    match under one of those three templates is a real, positive signal,
    just not a confirmable one -- so this doesn't blank out the category,
    it flags it."""
    if category is None:
        return None
    rule = rules.get(category)
    if rule and rule.get("requires_grantor_affidavit"):
        return (f"this template requires a Grantor's affidavit (filed in the OPR) to confirm the "
                f"{category!r} exemption actually applies -- not verifiable from index/deed-history "
                f"data alone, needs manual confirmation the affidavit was filed")
    return None


def _classify_pre_effective_date(recorded: date, covenant, rules: dict[str, dict]) -> tuple[str | None, str | None]:
    """Every walker already skips any transfer recorded before the
    covenant's own recording_date entirely (it happened before the
    covenant existed, not a transfer this covenant's exemptions are even
    about) -- so the cutoff_basis='recording_date' branch below can never
    actually fire under that skip and is kept only for correctness/
    documentation, not because it's reachable today. What this exists for
    is cutoff_basis='fixed_date' templates whose fixed cutoff falls AFTER
    the covenant's own recording date (V01's real gap: recorded
    2009-09-18, cutoff 2013-01-01) -- a real, several-year window where an
    otherwise-unclassifiable transfer is actually exempt."""
    rule = rules.get("pre_effective_date")
    if rule is None or rule["cutoff_date"] is None and rule["cutoff_basis"] == "fixed_date":
        return None, None
    if rule["cutoff_basis"] == "fixed_date" and recorded < rule["cutoff_date"]:
        return "pre_effective_date", (
            f"transfer recorded {recorded} is before this template's fixed {rule['cutoff_date']} "
            f"cutoff (clause {rule['clause_reference']})"
        )
    if rule["cutoff_basis"] == "recording_date" and recorded < covenant.recording_date:
        return "pre_effective_date", (
            f"transfer recorded {recorded} is before the covenant's own recording date "
            f"{covenant.recording_date} (clause {rule['clause_reference']})"
        )
    return None, None


def walk_chain_of_title(session, covid: int, tract_no: int = 1, max_parcels: int | None = None) -> dict:
    """Walks every parcel matched to this covid/tract -- not just one -- since
    a single recorded instrument routinely conveys a whole group of lots at
    once (confirmed directly: covid 3595's 6 lots share the exact same 2
    historical sales). Each parcel gets its own transfer rows (the widened
    (county_fips, instrument_number, recording_date, parcel_apn) key exists
    exactly so that's possible without a PK collision), but the source
    lookup itself is done once per unique data source, not once per parcel,
    when the source supports a bulk query (the CO assessor path).

    max_parcels caps how many of the matched parcels actually get walked --
    for a deliberately scoped test against a covenant with more lots than
    wanted (same "cap the test, don't process the whole thing" instruction
    this project has followed since the Colorado pilot). None (the
    default) walks every matched parcel, unchanged from before this
    parameter existed. Parcels are ordered by apn first so which ones get
    walked under a cap is deterministic, not whatever order Postgres
    happens to return."""
    covenant = session.execute(
        text("""
            SELECT county_fips, declarant_raw, recording_instrument, recording_date, template_version_id
            FROM covenant WHERE covid = :covid
        """), {"covid": covid},
    ).fetchone()
    if covenant is None:
        raise RuntimeError(f"covid {covid} not found")

    parcels = session.execute(
        text("""
            SELECT p.county_fips, p.apn, p.owner_name_raw, p.situs_address
            FROM parcel_covenant pc JOIN parcel p ON p.county_fips = pc.county_fips AND p.apn = pc.apn
            WHERE pc.covid = :covid AND pc.tract_no = :tract_no
            ORDER BY p.apn
        """), {"covid": covid, "tract_no": tract_no},
    ).fetchall()
    if max_parcels is not None:
        parcels = parcels[:max_parcels]
    if not parcels:
        return {"walked": False, "reason": f"covid {covid} tract {tract_no} has no matched parcels to walk toward"}

    gis_registry = session.execute(
        text("SELECT quirks FROM county_gis_registry WHERE county_fips = :cf"), {"cf": covenant.county_fips},
    ).fetchone()
    quirks = (gis_registry.quirks or {}) if gis_registry else {}
    cad_sales_url = quirks.get("cad_sales_data_url")
    cad_url = quirks.get("cad_deed_history_url")
    rules = _fetch_template_exemption_rules(session, covenant.template_version_id)

    if cad_sales_url:
        method = "assessor_sales_data"
        sales_by_apn: dict[str, list[dict]] = {}
        rows = run_with_job_queue(
            lambda: co_assessor_sales.fetch_sales_history(cad_sales_url, [p.apn for p in parcels]),
            job_type="title_assessor_sales_data", county_fips=covenant.county_fips, covid=covid,
            payload={"cad_sales_url": cad_sales_url, "account_numbers": [p.apn for p in parcels]},
        )
        for row in rows:
            sales_by_apn.setdefault(row["ACCOUNT_NO"], []).append(row)
        chains_by_apn = {p.apn: _walk_via_assessor_sales_data(covenant, sales_by_apn.get(p.apn, []),
                                                               rules) for p in parcels}
    elif cad_url:
        method = "cad_deed_history"
        chains_by_apn = {p.apn: _walk_via_cad_deed_history(covenant, p, cad_url, rules)
                         for p in parcels}
    else:
        recorder_registry = session.execute(
            text("SELECT base_url, quirks->>'vendor' AS vendor FROM county_recorder_registry WHERE county_fips = :cf"),
            {"cf": covenant.county_fips},
        ).fetchone()
        if recorder_registry is None or recorder_registry.vendor != "govos_publicsearch":
            vendor = recorder_registry.vendor if recorder_registry else None
            return {"walked": False, "reason": f"no cad_deed_history_url/cad_sales_data_url for this county, and "
                                                f"its recorder vendor {vendor!r} is not wired for a name-walk "
                                                f"either (only govos_publicsearch is)"}
        method = "recorder_portal_name_walk"
        # looked up once per covenant, not once per parcel -- it's the identical document
        # every time (the covenant's own recording_instrument), and a per-parcel lookup was
        # both wasteful (one redundant live search per extra parcel) and a real reliability
        # problem: confirmed directly (covid 4780 and 3297), a transient live-site failure on
        # any ONE of those otherwise-identical lookups was enough to flag that one parcel
        # "could not re-locate the covenant's own recorded instrument" while its siblings
        # succeeded on the exact same document.
        anchor = _lookup_recorder_anchor(covid, covenant, recorder_registry.base_url)
        if anchor is None:
            chains_by_apn = {p.apn: [{
                "ambiguous_split": True,
                "review_reason": f"could not re-locate the covenant's own recorded instrument "
                                  f"({covenant.recording_instrument!r}) to establish a lot/block anchor",
            }] for p in parcels}
        else:
            chains_by_apn = {p.apn: _walk_via_recorder_portal(session, covid, covenant, p, recorder_registry,
                                                               rules, anchor) for p in parcels}

    source_id = insert_source(
        session,
        source_type="assessor_api" if method in ("cad_deed_history", "assessor_sales_data") else "recorder_portal",
        reference=method, confidence=None,
    )

    results = {}
    for p in parcels:
        results[p.apn] = _finalize(session, covid, tract_no, covenant, p, chains_by_apn[p.apn], method, source_id)

    _update_covenant_gap_notes(session, covid, results)

    return {
        "walked": True,
        "method": method,
        "template_version_id": covenant.template_version_id,
        "parcel_count": len(parcels),
        "parcels": results,
    }


def _update_covenant_gap_notes(session, covid: int, results: dict) -> None:
    """One combined note covering every parcel with an unresolved gap,
    replacing whatever this function wrote on a prior run -- same "append a
    hedged note, never silently resolve" convention as
    app/recorder/diagnose.py's maybe_flag_missing_exhibit. Clears itself
    entirely once no parcel has a gap (e.g. a better source resolved it),
    so a fixed problem doesn't linger in review_reason."""
    gapped = {apn: r["gap_note"] for apn, r in results.items() if r["gap_note"]}

    existing = session.execute(
        text("SELECT review_reason FROM covenant WHERE covid = :covid"), {"covid": covid},
    ).fetchone()
    reason = existing.review_reason or ""
    # matches to end-of-string, not the next ";" -- a gap note's own text can
    # itself contain a semicolon, and this note is always appended last.
    reason = re.sub(r";?\s*CHAIN-OF-TITLE GAP \(automated[^)]*\):.*$", "", reason).strip("; ").strip()
    if gapped:
        detail = "; ".join(f"{apn}: {note}" for apn, note in gapped.items())
        note = f"CHAIN-OF-TITLE GAP (automated, {date.today().isoformat()}): {detail}"
        reason = f"{reason}; {note}" if reason else note
    if reason != (existing.review_reason or ""):
        session.execute(
            text("UPDATE covenant SET review_reason = :r, updated_at = now() WHERE covid = :covid"),
            {"r": reason or None, "covid": covid},
        )


def _walk_via_assessor_sales_data(covenant, sale_rows: list[dict], rules: dict | None) -> list[dict]:
    """Colorado (disclosure state): the assessor's own sales-history table
    already gives a complete, per-parcel, correctly-ordered transfer
    history including an ACTUAL sale price -- no name-search walk needed,
    and (unlike Bexar's CAD deed history) SALE_PRICE is a real disclosed
    number, not absent. Captured directly into consideration_amount."""
    dated = []
    for row in sale_rows:
        recorded = _parse_epoch_ms_date(row.get("SALE_DATE"))
        if recorded is not None:
            dated.append((recorded, row))
    dated.sort(key=lambda t: t[0])

    chain = []
    prior_instrument_number = None
    for recorded, row in dated:
        if recorded < covenant.recording_date:
            continue
        instrument_number = (row.get("RECORDING_NO") or "").strip()
        grantor, grantee = row.get("GRANTOR"), row.get("GRANTEE")
        if not instrument_number or not grantor or not grantee:
            chain.append({
                "ambiguous_split": True,
                "recording_date": str(recorded),
                "candidates": [row],
                "review_reason": f"assessor sales-data has a post-covenant entry with no usable recording "
                                  f"number/grantor/grantee ({row!r}) -- needs manual review rather than a guess",
            })
            continue

        category, basis = _classify_pre_effective_date(recorded, covenant, rules)
        if category is None and _names_match(grantor, covenant.declarant_raw):
            category, basis = "declarant_sale", "grantor matches covenant's own declarant"
        sale_price = row.get("SALE_PRICE")

        chain.append({
            "instrument_number": instrument_number,
            "recording_date": str(recorded),
            "grantor": grantor,
            "grantee": grantee,
            "doc_type": row.get("DEED_TYPE"),
            "book": (row.get("BOOK") or "").strip() or None if row.get("BOOK") else None,
            "page": (row.get("PAGE") or "").strip() or None if row.get("PAGE") else None,
            "prior_instrument_number": prior_instrument_number,
            "exemption_category": category,
            "exemption_basis": basis,
            "review_flag": category is None,
            "review_reason": None if category else (
                "exemption category not auto-classifiable from assessor sales-data alone (grantor/grantee "
                "names and deed type only) -- needs manual review of the deed's own recitals"
            ),
            # actual, disclosed price -- Colorado is a full-disclosure state, this is not an estimate.
            "consideration_amount": float(sale_price) if sale_price is not None else None,
        })
        prior_instrument_number = instrument_number

    return chain


def _walk_via_cad_deed_history(covenant, parcel, cad_url: str, rules: dict | None) -> list[dict]:
    deeds = run_with_job_queue(
        lambda: cad_deed_history.fetch_deed_history(cad_url, parcel.apn),
        job_type="title_cad_deed_history", county_fips=covenant.county_fips, covid=None,
        payload={"cad_url": cad_url, "prop_id": parcel.apn},
    )
    dated = []
    for d in deeds:
        recorded = _parse_slash_date(d.get("deed_dt", ""))
        if recorded is not None:
            dated.append((recorded, d))
    dated.sort(key=lambda t: t[0])

    chain = []
    prior_instrument_number = None
    for recorded, d in dated:
        if recorded < covenant.recording_date:
            continue
        instrument_number = (d.get("deed_num") or "").strip()
        grantor, grantee = d.get("grantor"), d.get("grantee")
        if not instrument_number or instrument_number == "0" or not grantor or not grantee:
            chain.append({
                "ambiguous_split": True,
                "recording_date": str(recorded),
                "candidates": [d],
                "review_reason": f"CAD deed history has a post-covenant entry with no usable instrument "
                                  f"number/grantor/grantee ({d!r}) -- needs manual review rather than a guess",
            })
            continue

        deed_type_cd = (d.get("deed_type_cd") or "").strip().upper()
        deed_type_desc = (d.get("deed_type_desc") or "").strip().upper()
        is_foreclosure = deed_type_cd in FORECLOSURE_DEED_TYPE_MARKERS or deed_type_desc in FORECLOSURE_DEED_TYPE_MARKERS

        category, basis = _classify_pre_effective_date(recorded, covenant, rules)
        if category is None and is_foreclosure:
            category, basis = "foreclosure", "CAD deed history's own deed type marks this a foreclosure"
        if category is None and _names_match(grantor, covenant.declarant_raw):
            category, basis = "declarant_sale", "grantor matches covenant's own declarant"

        affidavit_note = _affidavit_gate_note(category, rules)
        if affidavit_note:
            review_reason = affidavit_note
        elif category is None:
            review_reason = ("exemption category not auto-classifiable from CAD deed history alone "
                              "(grantor/grantee names and deed type only) -- needs manual review of the "
                              "deed's own recitals")
        else:
            review_reason = None

        chain.append({
            "instrument_number": instrument_number,
            "recording_date": str(recorded),
            "grantor": grantor,
            "grantee": grantee,
            "doc_type": d.get("deed_type_desc"),
            "book": (d.get("deed_book_id") or "").strip() or None,
            "page": (d.get("deed_book_page") or "").strip() or None,
            "prior_instrument_number": prior_instrument_number,
            "exemption_category": category,
            "exemption_basis": basis,
            "review_flag": category is None or affidavit_note is not None,
            "review_reason": review_reason,
            # a category match under an affidavit-gated template (migration 0030) is a real
            # signal, just not a confirmable one from index data alone -- half confidence,
            # not the usual full 1.0 for an auto-classified category.
            "exemption_confidence": 0.5 if affidavit_note else (1.0 if category else None),
        })
        prior_instrument_number = instrument_number

    return chain


def _lookup_recorder_anchor(covid: int, covenant, base_url: str) -> dict | None:
    """The covenant's own recorded instrument, re-located via the recorder
    portal to establish a lot/block anchor for every parcel in the tract --
    looked up once per covenant (see walk_chain_of_title's own comment on
    why this isn't done once per parcel)."""
    def _call():
        with recorder_context() as context:
            return publicsearch.search_by_document_number(context, base_url, covenant.recording_instrument)
    return run_with_job_queue(_call, job_type="title_chain_doc_lookup", county_fips=covenant.county_fips,
                               covid=covid, payload={"base_url": base_url, "doc_number": covenant.recording_instrument})


def _walk_via_recorder_portal(session, covid: int, covenant, parcel, registry,
                               rules: dict | None, anchor: dict) -> list[dict]:
    """Fallback for counties without a cad_deed_history_url. Hand-verified
    against covid 2497 (Bexar) before that source was found: a name search
    for a prolific individual (the declarant here has hundreds of unrelated
    documents across 50 years) misses the actual target within the
    portal's ~50-row result cap. An address-based full-text search
    reliably surfaces the *first* conveyance, but a distinct entity name
    (the grantee of that conveyance) searches far more precisely and
    completely than a person's name does -- so this walks forward hop by
    hop, re-searching each newly discovered grantee's own name, exactly
    how a human title examiner works a grantee index. Known to be
    incomplete in ways the CAD path is not (see this module's docstring).

    The seed pool is searched by the covenant's own declarant name FIRST,
    then by the parcel's address -- confirmed real on covid 3297/Montgomery
    (Gleneagles): the declarant almost never conveys directly to an
    individual lot buyer, instead bulk-selling entire sections to one or
    more intermediate developers/builders (a different one per section, in
    Gleneagles's case) who then build and sell to end buyers. Declarant-
    name search is tried first because it's the more direct source when it
    works at all; address search still runs afterward since it reliably
    surfaces the *recent* end of the chain even when the declarant's own
    bulk sale is buried past this vendor's ~50-row cap.

    Even with both seeds, hop 1 (the declarant's own conveyance) is often
    simply not in the index results -- see _walk_hop1_candidates for how
    that's handled without silently returning an empty chain."""
    base_url = registry.base_url

    def _search(query: str) -> list[dict]:
        def _call():
            with recorder_context() as context:
                return publicsearch.search_by_name(context, base_url, query)
        return run_with_job_queue(_call, job_type="title_chain_search", county_fips=covenant.county_fips,
                                   covid=covid, payload={"base_url": base_url, "query": query})

    anchor_lots = set() if _anchor_lot_is_unreliable(anchor) else _row_lots(anchor)
    anchor_block = anchor.get("BLOCK")
    anchor_subdivision = anchor.get("SUBDIVISION")

    def _matches_anchor(row: dict) -> bool:
        row_lots = _row_lots(row)
        if anchor_lots and row_lots and not (anchor_lots & row_lots):
            return False
        if not anchor_lots and not _subdivisions_match(anchor_subdivision, row.get("SUBDIVISION")):
            # lot-based matching is unavailable (either genuinely no lot data, or
            # discarded as unreliable) -- fall back to subdivision as a coarser but
            # still-real correlator, rather than letting every row through unfiltered.
            return False
        return _blocks_match(anchor_block, row.get("BLOCK"))

    candidate_pool: dict[str, dict] = {}
    situs = parcel.situs_address
    seed_queries = [covenant.declarant_raw]
    address_seed = _address_seed(situs) if situs else None
    if address_seed and address_seed != covenant.declarant_raw:
        seed_queries.append(address_seed)
    for q in seed_queries:
        for row in _search(q):
            doc_num = row.get("DOC NUMBER")
            if doc_num and _matches_anchor(row):
                candidate_pool[doc_num] = row
    anchor_doc_num = anchor.get("DOC NUMBER")
    if anchor_doc_num:
        candidate_pool[anchor_doc_num] = anchor

    chain = []
    current_holder = covenant.declarant_raw
    consumed = {covenant.recording_instrument}
    prior_instrument_number = None

    for _hop in range(MAX_HOPS):
        conveyance_candidates, declarant_link_unconfirmed = _walk_hop1_candidates(
            candidate_pool, consumed, current_holder, covenant, is_first_hop=(_hop == 0),
        )

        if not conveyance_candidates:
            break

        conveyance_candidates.sort(key=lambda t: t[0])
        earliest_date = conveyance_candidates[0][0]
        same_day = [r for d, r in conveyance_candidates if d == earliest_date]
        if len(same_day) > 1 and len({r.get("GRANTEE") for r in same_day}) > 1:
            chain.append({
                "ambiguous_split": True,
                "recording_date": str(earliest_date),
                "candidates": same_day,
                "review_reason": "multiple conveyances from the same grantor on the same date to "
                                  "different grantees -- likely a split/partial conveyance; needs "
                                  "manual review rather than an arbitrary pick",
            })
            break

        next_row = same_day[0]
        doc_num = next_row["DOC NUMBER"]
        category, basis = _classify_pre_effective_date(earliest_date, covenant, rules)
        if category is None and _names_match(next_row.get("GRANTOR", ""), covenant.declarant_raw):
            category, basis = "declarant_sale", "grantor matches covenant's own declarant"

        review_reason = None if category else (
            "exemption category not auto-classifiable from recorder index alone (grantor/grantee "
            "names only) -- needs manual review of the deed's own recitals"
        )
        if declarant_link_unconfirmed:
            unconfirmed_note = (
                f"this is the earliest post-covenant conveyance found for this lot/block, but its "
                f"grantor ({next_row.get('GRANTOR')!r}) does not match the covenant's own declarant "
                f"({covenant.declarant_raw!r}) -- the declarant likely sold through an intermediate "
                f"bulk buyer/builder not surfaced by the declarant-name or address-seeded search "
                f"(confirmed real pattern: covid 3297/Gleneagles was bulk-sold in sections through "
                f"multiple different intermediate developers). An earlier, fee-relevant hop between "
                f"the declarant and this holder may exist and isn't captured here -- needs manual "
                f"review, not assumed complete."
            )
            review_reason = f"{review_reason}; {unconfirmed_note}" if review_reason else unconfirmed_note

        chain.append({
            "instrument_number": doc_num,
            "recording_date": str(earliest_date),
            "grantor": next_row.get("GRANTOR"),
            "grantee": next_row.get("GRANTEE"),
            "doc_type": next_row.get("DOC TYPE"),
            "book": None, "page": None,
            "prior_instrument_number": prior_instrument_number,
            "exemption_category": category,
            "exemption_basis": basis,
            # declarant_link_unconfirmed does NOT force review_flag here: it can only
            # co-occur with category in {None, "pre_effective_date"} (declarant_sale
            # requires a grantor match, which is exactly what's absent when this flag is
            # set) -- pre_effective_date is a pure recording-date comparison, unaffected
            # by whether the grantor is confirmed to trace back to the declarant. The
            # caveat is still recorded in review_reason for a human reviewer, but doesn't
            # override an otherwise fully-confirmed exemption into a false "fee owed."
            "review_flag": category is None,
            "review_reason": review_reason,
        })
        consumed.add(doc_num)
        prior_instrument_number = doc_num
        current_holder = next_row.get("GRANTEE")

        for row in _search(current_holder):
            d = row.get("DOC NUMBER")
            if d and _matches_anchor(row):
                candidate_pool.setdefault(d, row)

    return chain


def _walk_hop1_candidates(candidate_pool: dict[str, dict], consumed: set[str], current_holder: str,
                           covenant, is_first_hop: bool) -> tuple[list[tuple[date, dict]], bool]:
    """Real conveyances in the pool, recorded on/after the covenant's own
    recording_date, whose grantor matches current_holder -- the normal,
    fully-confirmed case. On hop 1 only, if nothing matches the declarant
    directly (common: see _walk_via_recorder_portal's own docstring), falls
    back to the earliest real conveyance found for this lot/block at all,
    regardless of grantor -- real, index-sourced data, honestly flagged as
    an unconfirmed chain start, beats silently returning an empty chain."""
    def _real_conveyances() -> list[tuple[date, dict]]:
        found = []
        for doc_num, row in candidate_pool.items():
            if doc_num in consumed:
                continue
            doc_type = _normalize_doc_type((row.get("DOC TYPE") or "").strip().upper())
            recorded = _parse_slash_date(row.get("RECORDED DATE", ""))
            if recorded is None or recorded < covenant.recording_date:
                continue
            if doc_type not in CONVEYANCE_DOC_TYPES:
                continue
            found.append((recorded, row))
        return found

    strict = [(d, row) for d, row in _real_conveyances() if _names_match(row.get("GRANTOR", ""), current_holder)]
    if strict or not is_first_hop:
        return strict, False

    fallback = _real_conveyances()
    return fallback, bool(fallback)


def _finalize(session, covid: int, tract_no: int, covenant, parcel, chain: list[dict], method: str,
              source_id: int) -> dict:
    real_links = [link for link in chain if not link.get("ambiguous_split")]
    ambiguous = [link for link in chain if link.get("ambiguous_split")]

    for link in real_links:
        grantor_id = upsert_contact(session, link["grantor"], source_id=source_id)
        grantee_id = upsert_contact(session, link["grantee"], source_id=source_id)
        consideration_amount = link.get("consideration_amount")
        upsert_transfer(
            session, county_fips=parcel.county_fips, instrument_number=link["instrument_number"],
            covid=covid, tract_no=tract_no,
            parcel_county_fips=parcel.county_fips, parcel_apn=parcel.apn,
            prior_county_fips=parcel.county_fips if link["prior_instrument_number"] else None,
            prior_instrument_number=link["prior_instrument_number"],
            instrument_type=link["doc_type"], recording_date=link["recording_date"],
            book=link.get("book"), page=link.get("page"),
            grantor_contact_id=grantor_id, grantee_contact_id=grantee_id,
            consideration_amount=consideration_amount, legal_description_snapshot=None,
            recorder_source_id=source_id,
            # the same assessor query that found this transfer also carried its actual
            # (not estimated) price -- one source covers both, per app/db/repository.py's
            # upsert_transfer docstring on why the two source ids are kept distinct in general.
            consideration_source_id=source_id if consideration_amount is not None else None,
            review_flag=link["review_flag"], review_reason=link["review_reason"],
            exemption_category=link["exemption_category"], exemption_basis=link["exemption_basis"],
            # walkers that need a non-default confidence (e.g. an affidavit-gated category,
            # migration 0030) set it explicitly; otherwise the usual full/none default.
            exemption_confidence=link.get(
                "exemption_confidence", 1.0 if link["exemption_category"] else None
            ),
        )

    current_holder = real_links[-1]["grantee"] if real_links else covenant.declarant_raw
    current_owner = parcel.owner_name_raw
    holder_matches_current_owner = bool(current_owner) and _names_match(current_holder, current_owner)

    gap_note = None if (holder_matches_current_owner or ambiguous) else (
        f"chain walk's last known grantee ({current_holder!r}) does not match the parcel's current "
        f"owner of record per the county appraisal district ({current_owner!r}) -- could be an "
        f"unrecorded/missed transfer, an entity name change with no actual Transfer of Title (and "
        f"therefore no fee), or a stale CAD record; needs human review, not assumed"
    )

    return {
        "chain": real_links,
        "ambiguous": ambiguous,
        "final_holder_found": current_holder,
        "current_owner_of_record": current_owner,
        "holder_matches_current_owner": holder_matches_current_owner,
        "gap_note": gap_note,
    }

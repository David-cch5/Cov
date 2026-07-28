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

# Both sets confirmed real by directly sampling live recorder-portal data across every
# GovOS PublicSearch county this project currently has a recorder-portal process for
# (Bexar's own Advanced Search "Document Types" filter list, plus real DOC TYPE values
# seen in actual search results from Collin, Denton, Montgomery, and Nueces) -- not
# guessed. CONVEYANCE_DOC_TYPES is deliberately the SMALLER, higher-confidence set:
# never fabricate a Transfer of Title by assuming a type is a conveyance just because
# it's absent from NON_CONVEYANCE_DOC_TYPES (see _unrecognized_doc_type_flags).
CONVEYANCE_DOC_TYPES = {
    "DEED", "WARRANTY DEED", "GENERAL WARRANTY DEED", "SPECIAL WARRANTY DEED",
    "QUITCLAIM DEED", "QUIT CLAIM DEED", "TRUSTEE'S DEED", "TRUSTEE DEED",
    "EXECUTOR'S DEED", "EXECUTORS DEED", "ADMINISTRATOR'S DEED", "GIFT DEED",
    "DEED OF GIFT", "CORRECTION DEED", "SHERIFF'S DEED", "SHERIFFS DEED", "TAX DEED",
    # foreclosure-related deeds -- real conveyances of title, not merely foreclosure
    # PROCESS documents (see FORECLOSURE_DEED_TYPE_MARKERS below for classification).
    "SUBSTITUTE TRUSTEE'S DEED", "SUBSTITUTE TRUSTEES DEED",
    "TRUSTEE'S/SUBSTITUTE TRUSTEE'S DEED",  # confirmed real, Denton
    "DEED IN LIEU OF FORECLOSURE",  # confirmed real, Denton (11 occurrences)
}
NON_CONVEYANCE_DOC_TYPES = {
    "DECLARATION", "DEED OF TRUST", "DEED OF TRUST SECURED", "RELEASE", "PARTIAL RELEASE",
    "RELEASE OF LIEN", "RELEASE OF STATE TAX LIEN", "MODIFICATION", "UCC 1 REAL PROPERTY",
    "UCC", "UCC1", "UCC RP", "LIEN", "MECHANICS LIEN", "MECHANIC LIEN AFFIDAVIT", "JUDGMENT",
    "AFFIDAVIT", "NOTICE", "EASEMENT", "AGREEMENT", "APPOINTMENT", "ASSIGNMENT", "BOND",
    "CERTIFIED COPY", "EXTENSION", "LIS PENDENS", "RESOLUTION",
    # confirmed real via Bexar's own Advanced Search "Document Types" filter list (the
    # county clerk's own canonical category names) -- covers a lot of document types
    # this project hadn't encountered in a live search result yet, so wasn't previously
    # classified either way; none of these convey title.
    "ACKNOWLEDGEMENT", "ADDENDUM", "AFFIDAVIT RESTITUTION LIEN", "AMENDMENT", "APPLICATION",
    "ARTICLES OF INC", "ASSIGNMENT OF JUDGMENT", "ASSIGNMENT SECURED", "ASSUMPTION",
    "BILL OF SALE", "BOND TO INDEMNIFY", "CANCELLATION", "CANCELLATION OF JUDGMENT",
    "CERTIFICATE", "CHILD SUPPORT LN", "CONDOMINIUM ASSOCIATION MANAGEMENT CERTIFICATE",
    "CONDOMINIUM PLAN", "CONFLICT OF INTEREST QUESTIONNAIRE", "CORPORATE",
    "DEED AFFIDAVIT", "DECREE", "DESIGNATION", "DISMISSAL", "HOMESTEAD AFFIDAVIT",
    "HOSPITAL LIEN", "LANDLORD LIEN", "LEASE", "LETTERS", "LEVY", "LOAN",
    "MASTER MORTGAGE", "MEMORANDUM", "MISCELLANEOUS", "MORTGAGE",
    "NOTE", "ORDER", "ORDINANCE", "PARTIAL ASSIGNMENT OF JUDGMENT",
    "PARTIAL RELEASE OF JUDGMENT", "PARTIAL TRANSFER OF JUDGMENT", "POWER OF ATTORNEY",
    "POWER OF ATTORNEY SECURED", "PROBATE", "PUBLIC WEIGHER BOND", "RECONVEYANCE",
    "REFERENCE SHEET", "REINSTATEMENT", "RELEASE OF HL", "RELEASE OF JUDGMENT",
    "REMOVAL", "RENEWAL", "REPLACEMENT", "RESCISSION", "RESIGNATION", "RESTRICTIONS",
    "RETAIL INST CONTRACT", "REVOCATION", "RIGHT OF WAY AGREEMENT", "SATISFACTION",
    "SATISFACTION OF JUDGMENT", "STATEMENT", "SUBORDINATION", "SUBSTITUTION",
    "TERMINATION", "TRANSCRIPT OF JUDGMENT", "TRANSFER OF JUDGMENT", "TRUST",
    "VARIANCE", "WAIVER", "WATER PERMIT", "WATER PERMIT MAPS", "WATER RIGHTS/PERMIT",
    "WILL & TESTAMENT", "FEDERAL TAX LIENS", "STATE TAX LIENS", "STATE TAX LIEN",
    # confirmed real, Collin/Denton/Nueces samples.
    "TRUST AGREEMENT", "TRUST AGREEMENT/DECLARATION", "MEMORANDUM OF TRUST AGREEMENT",
    "TERMINATION OF TRUST", "APPOINTMENT OF TRUSTEE/SUBSTITUTE TRUSTEE",
    "RESIGNATION OF TRUSTEE", "LEASE TERMINATION", "HOMESTEAD AFFIDAVIT/DECLARATION/DESIGNATION",
    "ASSUMED NAME", "PLAT", "PLAT INFORMATION", "FINANCING STATEMENT",
    "FINANCING STATEMENT - NON STND", "HOMEOWNERS ASSOC DOCS",
    # a document that voids a PRIOR foreclosure sale -- doesn't itself convey title, but
    # a real signal that an earlier recorded conveyance in the chain may need re-review
    # (the same "a later document can retroactively change an earlier one's effect"
    # pattern already seen with covenant terminations -- not yet acted on automatically,
    # just correctly kept out of the conveyance chain rather than mistaken for one).
    "DECLARATION OF INVALIDITY OF FORECLOSURE SALE",
    # confirmed real, Montgomery/covid 3297 -- this vendor's own spelling/formatting
    # varies even for a type already covered under a different exact string (singular
    # vs plural, a dropped "OF") -- surfaced by _unrecognized_doc_type_flags rather than
    # silently dropped, exactly as designed.
    "UCC6 TERMINATION", "POWER ATTORNEY", "MECHANIC LIEN",
    "ABSTRACT OF JUDGMENT", "CERTIFIED COPY DIVORCE",
}

# GovOS PublicSearch's own DOC TYPE vocabulary for foreclosure-related deeds -- an
# actual Transfer of Title (already in CONVEYANCE_DOC_TYPES above), just one this
# project can classify "foreclosure" with confidence from the type name alone, the
# same way FORECLOSURE_DEED_TYPE_MARKERS does for Harris Govern PACS's own vocabulary
# (deed_type_cd/deed_type_desc) in _walk_via_cad_deed_history. Deliberately NOT
# including "TAX DEED" here -- a tax-sale foreclosure is a real but distinct concept
# this project hasn't confirmed the templates treat the same way.
GOVOS_FORECLOSURE_DEED_TYPES = {
    "TRUSTEE'S DEED", "TRUSTEE DEED", "SUBSTITUTE TRUSTEE'S DEED", "SUBSTITUTE TRUSTEES DEED",
    "TRUSTEE'S/SUBSTITUTE TRUSTEE'S DEED", "SHERIFF'S DEED", "SHERIFFS DEED",
    "DEED IN LIEU OF FORECLOSURE",
}
FORECLOSURE_DEED_TYPE_MARKERS = {"FC", "FORECLOSURE"}

# Per direct guidance (confirmed independently: Texas real property is conveyed via
# specifically-named deeds -- warranty, special warranty, quitclaim, trustee's, etc. --
# not a generically-labeled "Conveyance"), a bare "CONVEYANCE" DOC TYPE in a Texas
# county's own index is usually NOT a Transfer of Title of the encumbered property at
# all. It's commonly an assignment of some OTHER interest that still references the same
# lot/block in its legal description -- most relevant here, an assignment of the
# covenant's OWN beneficiary/trustee interest (who has the right to collect the transfer
# fee), not a sale of the land. Confirmed real and consistent with this: covid 3297's own
# "CONVEYANCE" document (FCP Holdings I LLC -> Cinco West Development LLC) didn't
# connect to the rest of that lot's real title chain at all. Deliberately kept OUT of
# CONVEYANCE_DOC_TYPES (never assumed a real transfer) and OUT of NON_CONVEYANCE_DOC_TYPES
# (never silently dropped either -- it could genuinely matter for tracking who holds the
# covenant's own benefit) -- flagged for manual review instead, state-scoped since this is
# specifically a Texas recording-practice fact, not a general one.
TX_AMBIGUOUS_CONVEYANCE_TYPES = {"CONVEYANCE"}

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
            SELECT c.county_fips, c.declarant_raw, c.recording_instrument, c.recording_date,
                   c.template_version_id, co.state_code
            FROM covenant c JOIN county co ON co.county_fips = c.county_fips
            WHERE c.covid = :covid
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
        category, basis, affidavit_note = _classify_recorder_portal_link(
            next_row.get("DOC TYPE"), next_row.get("GRANTOR", ""), earliest_date, covenant, rules,
        )
        review_reason = affidavit_note if affidavit_note else (None if category else (
            "exemption category not auto-classifiable from recorder index alone (grantor/grantee "
            "names only) -- needs manual review of the deed's own recitals"
        ))
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
            # declarant_link_unconfirmed does NOT itself force review_flag: it can only
            # co-occur with category in {None, "pre_effective_date", "foreclosure"} (never
            # "declarant_sale", which requires the very grantor match that's absent when
            # this flag is set) -- pre_effective_date is a pure recording-date fact, and a
            # foreclosure classification comes from the deed's own DOC TYPE, neither
            # affected by whether the grantor is confirmed to trace back to the declarant.
            # The caveat is still recorded in review_reason for a human reviewer, but
            # doesn't override an otherwise fully-confirmed exemption into a false "fee
            # owed." affidavit_note (migration 0030's V02/V03/V12 gate) is the one thing
            # that DOES still force review, same as _walk_via_cad_deed_history.
            "review_flag": category is None or affidavit_note is not None,
            # a category match under an affidavit-gated template is a real signal, just
            # not a confirmable one from index data alone -- half confidence, not the
            # usual full 1.0 for an auto-classified category (mirrors the CAD deed
            # history path's own exemption_confidence handling).
            "exemption_confidence": 0.5 if affidavit_note else (1.0 if category else None),
            "review_reason": review_reason,
        })
        consumed.add(doc_num)
        prior_instrument_number = doc_num
        current_holder = next_row.get("GRANTEE")

        for row in _search(current_holder):
            d = row.get("DOC NUMBER")
            if d and _matches_anchor(row):
                candidate_pool.setdefault(d, row)

    chain.extend(_unrecognized_doc_type_flags(candidate_pool, consumed, covenant))
    return chain


def _classify_recorder_portal_link(doc_type: str | None, grantor: str, recorded: date,
                                    covenant, rules: dict[str, dict] | None) -> tuple[str | None, str | None, str | None]:
    """Exemption classification for one recorder-portal conveyance, checked
    in the same priority order as every other walker in this module:
    pre_effective_date (a plain date comparison, checked first since it
    doesn't depend on any name/type heuristic), then foreclosure (this
    vendor's own DOC TYPE naming a foreclosure-related deed -- confirmed
    real across Bexar/Collin/Denton/Nueces, see GOVOS_FORECLOSURE_DEED_TYPES),
    then declarant_sale (grantor matches the covenant's own declarant).
    Returns (category, basis, affidavit_note) -- affidavit_note is the same
    migration-0030 V02/V03/V12 gate _walk_via_cad_deed_history already
    applies, non-None only when the matched category is one of the four
    that template family requires a Grantor's affidavit for."""
    category, basis = _classify_pre_effective_date(recorded, covenant, rules)
    doc_type_normalized = _normalize_doc_type((doc_type or "").strip().upper())
    if category is None and doc_type_normalized in GOVOS_FORECLOSURE_DEED_TYPES:
        category, basis = "foreclosure", "recorder index's own DOC TYPE marks this a foreclosure-related deed"
    if category is None and _names_match(grantor, covenant.declarant_raw):
        category, basis = "declarant_sale", "grantor matches covenant's own declarant"
    affidavit_note = _affidavit_gate_note(category, rules or {})
    return category, basis, affidavit_note


def _unrecognized_doc_type_flags(candidate_pool: dict[str, dict], consumed: set[str], covenant) -> list[dict]:
    """Anything left in the candidate pool that's neither a recognized
    conveyance nor a recognized non-conveyance is a real gap in this
    project's own type vocabulary, not something to silently treat as non-
    conveyance -- exactly the failure mode that missed "WARRANTY DEED
    W/VENDORS LIEN" on covid 3297 before that variant was catalogued (see
    CONVEYANCE_DOC_TYPES' own comment). Since candidate_pool is already
    filtered to rows matching this parcel's own lot/block/subdivision
    (_matches_anchor), an unrecognized type found here is likely actually
    about this property, not unrelated noise -- flagged the same way an
    ambiguous same-day split already is (never silently dropped, never
    silently assumed a conveyance either), so a genuinely new county's
    vocabulary surfaces for review instead of quietly under-counting real
    Transfers of Title. TX_AMBIGUOUS_CONVEYANCE_TYPES gets its own more
    specific note (a known reason for the ambiguity), rather than the
    generic "totally unrecognized" one, when the covenant is in Texas."""
    flags = []
    for doc_num, row in candidate_pool.items():
        if doc_num in consumed:
            continue
        doc_type = _normalize_doc_type((row.get("DOC TYPE") or "").strip().upper())
        if not doc_type or doc_type in CONVEYANCE_DOC_TYPES or doc_type in NON_CONVEYANCE_DOC_TYPES:
            continue
        recorded = _parse_slash_date(row.get("RECORDED DATE", ""))
        if recorded is None or recorded < covenant.recording_date:
            continue
        if covenant.state_code == "TX" and doc_type in TX_AMBIGUOUS_CONVEYANCE_TYPES:
            review_reason = (
                f"document type {row.get('DOC TYPE')!r} is a generic Texas recorder label that's "
                f"usually NOT a Transfer of Title of the encumbered property -- real Texas "
                f"conveyances use a specifically-named deed instead. This is commonly an "
                f"assignment of some other interest (e.g. the covenant's own beneficiary/trustee "
                f"interest, i.e. who has the right to collect the transfer fee, not a sale of the "
                f"land) that still references the same lot/block in its legal description. Needs a "
                f"human to read the actual document before treating it as either a real conveyance "
                f"or unrelated noise."
            )
        else:
            review_reason = (
                f"document type {row.get('DOC TYPE')!r} is not in this project's known "
                f"conveyance/non-conveyance vocabulary -- could be a real Transfer of Title this "
                f"walk would otherwise silently miss; needs manual classification, not assumed "
                f"either way."
            )
        flags.append({
            "ambiguous_split": True,
            "recording_date": str(recorded),
            "candidates": [row],
            "review_reason": review_reason,
        })
    return flags


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


def _mark_superseded_transfers(session, covid: int, tract_no: int, parcel, real_links: list[dict]) -> None:
    """Confirmed real (covid 3297, parcel 93070, multiple times this
    session): a re-walk that finds a DIFFERENT chain for this parcel (a
    newly-recognized doc type, a corrected anchor match, ...) left the
    PREVIOUS walk's transfer rows behind with no way to tell they're no
    longer current. Never deleted (see migration 0031's own docstring on
    why: real fee_collection history can hang off a transfer row) -- just
    marked superseded_at, and upsert_transfer already clears it back to
    NULL if a later walk re-confirms the same (instrument_number,
    recording_date) key.

    Deliberately a no-op when real_links is empty: an empty result here is
    indistinguishable from "genuinely nothing to find" and "this
    particular run hit a transient failure" (confirmed real: a live
    recorder-portal anchor lookup randomly failed this session on an
    otherwise-fine covenant) -- superseding every prior row on an empty
    result would risk real data loss on exactly the kind of flaky run this
    project has already hit."""
    if not real_links:
        return
    current_keys = {(link["instrument_number"], link["recording_date"]) for link in real_links}
    existing = session.execute(
        text("""
            SELECT instrument_number, recording_date FROM transfer
            WHERE covid = :covid AND tract_no = :tract_no
              AND parcel_county_fips = :county_fips AND parcel_apn = :apn
              AND superseded_at IS NULL
        """),
        {"covid": covid, "tract_no": tract_no, "county_fips": parcel.county_fips, "apn": parcel.apn},
    ).fetchall()
    for row in existing:
        if (row.instrument_number, str(row.recording_date)) in current_keys:
            continue
        session.execute(
            text("""
                UPDATE transfer SET superseded_at = now()
                WHERE covid = :covid AND tract_no = :tract_no
                  AND parcel_county_fips = :county_fips AND parcel_apn = :apn
                  AND instrument_number = :inst AND recording_date = :rd
            """),
            {"covid": covid, "tract_no": tract_no, "county_fips": parcel.county_fips,
             "apn": parcel.apn, "inst": row.instrument_number, "rd": row.recording_date},
        )


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
    _mark_superseded_transfers(session, covid, tract_no, parcel, real_links)

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

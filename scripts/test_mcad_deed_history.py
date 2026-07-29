"""Smoke test for app/title/mcad_deed_history.py -- Montgomery CAD's own
per-parcel Deed History table, confirmed live to give a complete 14-hop
chain of title back to 1996 for a real parcel (APN 41116) that this
project's own recorder-portal name-search couldn't reconstruct at all (the
covenant's declarant name "ANANTA LLC" differs from the actual grantor on
the real conveyances, "ANANTA PARTNERS LLC").

The parsing logic (_parse_deed_history) is tested directly against a real
captured text sample -- no network, fast, exact expected structure. The
live fetch_deed_history tests hit the real site, matching this project's
established convention for recorder/CAD-portal adapters (see
scripts/test_recorder_adapters.py's own docstring): these are real
third-party frontends, not a stable API this project controls, so a smoke
test against the real site is how a future markup change gets caught.

Usage: python3 scripts/test_mcad_deed_history.py
"""
import sys

sys.path.insert(0, ".")

from app.recorder.session import recorder_context
from app.title.mcad_deed_history import _parse_deed_history, fetch_deed_history

MCAD_BASE_URL = "https://mcad-tx.org"

# Real text captured live (2026-07-29) from MCAD's own rendered page for APN 41116,
# after the AG-Grid scroll-into-view fix -- exercises both trailing-field shapes
# (modern instrument-numbered deeds with blank book/volume/page, and older
# book/volume/page deeds with blank instrument), plus the DELETED row MCAD itself
# flags for a mis-associated instrument.
_REAL_CAPTURED_TEXT = """Some header content before the table
Deed History
Deed Date\tType\tDescription\tGrantor/Seller\tGrantee/Buyer\tBook ID\tVolume\tPage\tInstrument
2026-03-27\tWD\tWarnty Deed\tCOXCO8 LLC\tHENDAYA CAPITAL LLC\t\t\t\t2026031777
2021-11-30\tSWD\tSpcl W/deed\tCK PROPERTIES LLC\tCOXCO8 LLC\t\t\t\t2021168409
2017-10-05\tSWD\tSpcl W/deed\tBAYROCK CENTRAL LLC\tCK PROPERTIES LLC\t\t\t\t2017090250
2016-08-10\tSWD\tSpcl W/deed\tBAYROCK INVESTMENT CO\tBAYROCK CENTRAL LLC\t\t\t\t2016071940
2015-11-25\tSWD\tSpcl W/deed\tD3 SHENANDOAH LLC\tBAYROCK INVESTMENT CO\t\t\t\t2015115888
2015-04-22\tDELETED\tDeleted Transfer\tANANTA PARTNERS, LLC\tWOODLANDS MEDICAL PROPERTIES LP\t\t\t\t2015037102
2015-03-05\tSWD\tSpcl W/deed\tANANTA PARTNERS, LLC\tD3 SHENANDOAH LLC\t\t\t\t2015020775
2010-02-02\tSTD\tSub Tr Deed\tALORE, LP\tANANTA PARTNERS, LLC\t\t\t\t2010008311
2003-12-11\tSWD\tSpcl W/deed\tRIVERSTONE CENTER LLC\tALORE, LP\t502.10\t\t1838\t
2001-03-09\tWDV\tW/d & V/ln\tOWEN, RIGBY, Jr\tRIVERSTONE CENTER LLC\t846.00\t\t0080\t
1996-04-04\tSWD\tSpcl W/deed\tTIMBER RIDGE PRESBYTERIAN CHURCH\tOWEN, RIGBY, Jr\t147.00\t\t1060\t
Address\t123 Main St
Mailing\tPO Box 1
Phone\t(555) 555-5555"""


def test_parse_deed_history_real_captured_text() -> None:
    rows = _parse_deed_history(_REAL_CAPTURED_TEXT)
    assert len(rows) == 11, rows

    newest = rows[0]
    assert newest == {
        "deed_date": "2026-03-27", "deed_type": "WD", "description": "Warnty Deed",
        "grantor": "COXCO8 LLC", "grantee": "HENDAYA CAPITAL LLC",
        "book": None, "volume": None, "page": None, "instrument": "2026031777",
    }, newest

    oldest = rows[-1]
    assert oldest == {
        "deed_date": "1996-04-04", "deed_type": "SWD", "description": "Spcl W/deed",
        "grantor": "TIMBER RIDGE PRESBYTERIAN CHURCH", "grantee": "OWEN, RIGBY, Jr",
        "book": "147.00", "volume": None, "page": "1060", "instrument": None,
    }, oldest

    deleted = [r for r in rows if r["deed_type"] == "DELETED"]
    assert len(deleted) == 1 and deleted[0]["instrument"] == "2015037102", deleted

    assert all("Address" not in r["grantor"] and "Mailing" not in r["grantor"] for r in rows), rows
    print("PASS: _parse_deed_history -> all 11 real rows parsed correctly, footer content excluded")


def test_parse_deed_history_no_table() -> None:
    assert _parse_deed_history("some page with no deed history section at all") == []
    print("PASS: _parse_deed_history -> returns [] when no Deed History section is present")


def test_fetch_deed_history_live_montgomery_41116() -> None:
    """APN 41116: the parcel whose declarant-name mismatch broke the
    recorder-portal name-search entirely. MCAD's own account-indexed history
    reconstructs the full chain back to the 1996 church-to-Owen conveyance."""
    with recorder_context() as context:
        rows = fetch_deed_history(context, MCAD_BASE_URL, "41116")
    assert len(rows) == 11, rows
    assert rows[0]["deed_date"] == "2026-03-27", rows[0]
    assert rows[0]["grantee"] == "HENDAYA CAPITAL LLC", rows[0]
    assert rows[-1]["deed_date"] == "1996-04-04", rows[-1]
    assert rows[-1]["grantor"] == "TIMBER RIDGE PRESBYTERIAN CHURCH", rows[-1]
    grantors = {r["grantor"] for r in rows}
    assert "ANANTA PARTNERS, LLC" in grantors, rows
    print(f"PASS: fetch_deed_history (live, APN 41116) -> {len(rows)} rows, chain reaches 1996")


def test_fetch_deed_history_live_montgomery_451910() -> None:
    """APN 451910: covid 8245's other real dominant parcel (99.1% overlap) --
    a much shorter, 2-hop chain confirming the adapter isn't a one-parcel
    fluke."""
    with recorder_context() as context:
        rows = fetch_deed_history(context, MCAD_BASE_URL, "451910")
    assert len(rows) == 2, rows
    assert rows[0]["grantee"] == "26710 I 45 NORTH LLC", rows[0]
    assert rows[1]["grantor"] == "ANANTA PARTNERS, LLC", rows[1]
    print(f"PASS: fetch_deed_history (live, APN 451910) -> {len(rows)} rows")


def test_fetch_deed_history_live_not_found() -> None:
    """A nonexistent account number returns [] -- not found is not an error."""
    with recorder_context() as context:
        rows = fetch_deed_history(context, MCAD_BASE_URL, "00000000000")
    assert rows == [], rows
    print("PASS: fetch_deed_history (live, bogus APN) -> [] rather than raising")


if __name__ == "__main__":
    test_parse_deed_history_real_captured_text()
    test_parse_deed_history_no_table()
    test_fetch_deed_history_live_montgomery_41116()
    test_fetch_deed_history_live_montgomery_451910()
    test_fetch_deed_history_live_not_found()
    print("\nall mcad_deed_history smoke tests passed")

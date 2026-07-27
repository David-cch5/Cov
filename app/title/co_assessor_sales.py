"""Colorado county-assessor sales-history lookup -- confirmed live for
Douglas County (county_gis_registry.quirks->>'cad_sales_data_url'), a
plain unauthenticated ArcGIS FeatureServer table (not a Playwright-rendered
portal): SALE_DATE, SALE_PRICE, DEED_TYPE, GRANTOR, GRANTEE, BOOK, PAGE,
RECORDING_NO per ACCOUNT_NO.

This is the reason Douglas County was picked as the disclosure-state
price-extraction test case: SALE_PRICE here is an ACTUAL recorded
consideration (Colorado is full-disclosure), not an estimate -- unlike
Bexar's CAD deed history (Texas, non-disclosure: grantor/grantee/date/deed
type only, no price at all).

Queried in bulk across every account number in a tract at once (one
FeatureServer query, not one per parcel) since a single recorded
instrument routinely covers every lot in a subdivision phase -- confirmed
directly: covid 3595's two real historical sales (recording numbers
9220140 and 2021070554) are identical across all 6 of its parcels.
"""
import requests

DEFAULT_TIMEOUT = 30


def _sql_quote_list(values: list[str]) -> str:
    escaped = [v.replace("'", "''") for v in values]
    return ",".join(f"'{v}'" for v in escaped)


def fetch_sales_history(base_url: str, account_numbers: list[str]) -> list[dict]:
    """Returns every sale record for any of the given account numbers, e.g.
    {"ACCOUNT_NO": "R0334407", "SALE_DATE": 707097600000 (epoch ms),
    "SALE_PRICE": 1805000, "DEED_TYPE": "Quit Claim", "GRANTOR": ...,
    "GRANTEE": ..., "BOOK": ..., "PAGE": ..., "RECORDING_NO": "9220140"}."""
    resp = requests.get(
        f"{base_url}/query",
        params={
            "where": f"ACCOUNT_NO IN ({_sql_quote_list(account_numbers)})",
            "outFields": "*", "f": "json",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"assessor sales-data query error: {data['error']}")
    return [f["attributes"] for f in data.get("features", [])]

"""County appraisal district (CAD) deed-history lookup -- "Harris Govern"
PACS platform (`hgo.harrisgovern.com/<county>/...`), confirmed live for
Bexar (BCAD's own site links to it as "NEW Property Search", distinct from
both the ArcGIS REST layer county_gis_registry points at and the county
CLERK's recorder portal in county_recorder_registry -- three genuinely
different systems that happen to describe the same real-world property).

This is a MUCH better chain-of-title source than reconstructing history by
walking grantor/grantee name searches against a recorder portal (see
app/title/chain.py's docstring for why that approach is noisy/incomplete):
the CAD maintains its own indexed deed-history table per property, seeded
directly from the county clerk's recordings, and exposes it as a plain,
unauthenticated JSON GET -- no Playwright, no name-search ambiguity, no
50-row cap. Found by hand while investigating why a recorder-portal-based
chain walk for covid 2497 (Bexar) turned up a holder that didn't match the
CAD's current owner: the CAD's own deed history revealed a foreclosure
(Oggnim LLC -> BHA Bandera Road LLC) and a subsequent resale (-> GS
Ventures Group LLC) that neither an address-text search nor a per-grantee
name search on the recorder portal ever surfaced.

Only confirmed for Bexar so far. "Harris Govern" (a CAD software vendor,
unrelated to Harris County despite the name) is used by other Texas CADs
too, so other counties may expose the same API shape at their own
`hgo.harrisgovern.com/<county>/...` host -- worth checking before assuming
this is Bexar-specific, but not verified here.
"""
import requests

DEFAULT_TIMEOUT = 30


def fetch_deed_history(base_url: str, prop_id: str) -> list[dict]:
    """Returns every recorded deed for this property, newest first (seq_num
    descending from the CAD's own indexing) -- each row has deed_dt,
    deed_type_cd, deed_type_desc, grantor, grantee, deed_book_id,
    deed_book_page, deed_num (the recording instrument number, '0' for a
    pre-modern-index entry with none on file)."""
    resp = requests.get(
        f"{base_url}/api/property/property-details/property-deed-history",
        params={"propertyId": prop_id}, timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()

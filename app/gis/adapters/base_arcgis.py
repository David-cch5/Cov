"""Generic ArcGIS REST FeatureServer query helper. Per-county quirks (pagination
cap, whether returnGeometry needs to be false for bulk attribute pulls, etc.) live
in each county's county_gis_registry row, not hardcoded here.
"""
import requests

DEFAULT_TIMEOUT = 60  # some county-hosted (non-ArcGIS-Online) servers, e.g. Travis's, are noticeably slower


def query_features(base_url: str, where: str = "1=1", out_fields: str = "*",
                    return_geometry: bool = True, out_sr: int = 4326,
                    result_offset: int | None = None, result_record_count: int | None = None,
                    geometry: dict | None = None) -> dict:
    """One page of results from an ArcGIS FeatureServer layer's /query endpoint."""
    params = {
        "where": where,
        "outFields": out_fields,
        "returnGeometry": str(return_geometry).lower(),
        "outSR": out_sr,
        "f": "json",
    }
    if result_offset is not None:
        params["resultOffset"] = result_offset
    if result_record_count is not None:
        params["resultRecordCount"] = result_record_count
    if geometry is not None:
        params["geometry"] = geometry
        params["geometryType"] = "esriGeometryEnvelope"
        params["spatialRel"] = "esriSpatialRelIntersects"
        params["inSR"] = out_sr

    resp = requests.get(f"{base_url}/query", params=params, timeout=DEFAULT_TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"ArcGIS query error: {data['error']}")
    return data


def iter_all_features(base_url: str, where: str = "1=1", out_fields: str = "*",
                       return_geometry: bool = True, out_sr: int = 4326,
                       page_size: int = 1000, max_records: int | None = None,
                       geometry: dict | None = None):
    """Paginate through every feature matching `where`, respecting the layer's own
    maxRecordCount by requesting page_size at a time. Stops early at max_records if given
    (use this for probing -- never pull a whole county's parcel roll without a reason)."""
    offset = 0
    fetched = 0
    while True:
        page = query_features(
            base_url, where=where, out_fields=out_fields, return_geometry=return_geometry,
            out_sr=out_sr, result_offset=offset, result_record_count=page_size, geometry=geometry,
        )
        features = page.get("features", [])
        if not features:
            break
        for feat in features:
            yield feat
            fetched += 1
            if max_records is not None and fetched >= max_records:
                return
        if not page.get("exceededTransferLimit") and len(features) < page_size:
            break
        offset += len(features)


def esri_rings_to_geojson_multipolygon(rings: list) -> dict:
    """Each ring becomes its own polygon in the MultiPolygon -- doesn't reconstruct
    holes from Esri's clockwise/counter-clockwise ring-orientation convention. Fine
    for the large majority of single-part parcels; a rare multi-ring parcel with an
    interior exclusion would render as an extra disjoint piece rather than a hole.
    Revisit if that turns out to matter for reconciliation.
    """
    return {"type": "MultiPolygon", "coordinates": [[ring] for ring in rings]}

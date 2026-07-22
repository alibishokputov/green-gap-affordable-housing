"""Parcel-scale environmental exposure for the labelled multifamily buildings.

Extracts, per building footprint, the same environmental measures the areal
pipeline computes for census units, but at the building scale so housing types can
be compared directly:

    canopy  Chesapeake 1 m tree-canopy % over the footprint (exactextract)
    lst     summer Landsat LST/NDVI/NDBI over the footprint (GEE stack)
    flood   FEMA NFHL flood-zone overlap (if an NFHL layer is present)

Run::

    uv run python -m greengap.parcel_env              # footprint extraction
    uv run python -m greengap.parcel_env --buffer 30  # footprint + 30 m ring
    uv run python -m greengap.parcel_env --force      # recompute

Design
------
Reuses ``greengap.dataset``'s raster constants and canopy arithmetic so the parcel
and areal measures are defined identically - a block-group canopy % and a building
canopy % differ only in the polygon, not the definition. The building footprint is
already a stable extraction target (median ~2,675 m²); ``--buffer`` optionally adds
a ring to capture the immediate green context around the building rather than only
its roof and pavement.

FEMA flood is a vector overlay, not a raster. It activates only when an NFHL layer
is dropped at ``data/external/fema_nfhl.gpkg``; absent that, flood columns are left
null and a warning is logged (same pattern as the missing DC NHPD extract).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import geopandas as gpd
from loguru import logger
import pandas as pd
import typer

from greengap.config import EXTERNAL_DATA_DIR, PROCESSED_DATA_DIR
from greengap.dataset import (
    LANDSAT_BANDS,
    LANDSAT_STACK,
    STATE_FIPS,
    STATES,
    _resolve_raster,
    derive_canopy_columns,
)
from greengap.housing_type import labelled_path

app = typer.Typer(help=__doc__, no_args_is_help=True)

CORRIDOR_CRS = "EPSG:26985"
FEMA_NFHL = EXTERNAL_DATA_DIR / "fema_nfhl.gpkg"
# NFHL zones designating the 1%-annual-chance (100-yr) floodplain.
FEMA_HIGH_RISK_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}

# FEMA National Flood Hazard Layer, "Flood Hazard Zones" (layer 28). SFHA_TF = 'T'
# marks the Special Flood Hazard Area (the 1%-annual-chance floodplain).
FEMA_REST = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28"
# Study-area bounding box in WGS84 (6 MD counties + DC), padded slightly.
FEMA_BBOX = (-77.35, 38.65, -76.35, 39.72)


def parcel_env_path() -> Path:
    return PROCESSED_DATA_DIR / "mf_buildings_env.parquet"


# --------------------------------------------------------------------------- #
# Canopy (1 m Chesapeake land cover), per building, per state raster           #
# --------------------------------------------------------------------------- #
def _canopy_for_state(fips: str, buildings: gpd.GeoDataFrame) -> pd.DataFrame:
    from exactextract import exact_extract
    import rasterio

    sub = buildings[buildings["state"] == STATES[fips]]
    if sub.empty:
        return pd.DataFrame(columns=["building_id"])

    raster_path = _resolve_raster(fips)
    with rasterio.open(raster_path) as src:
        raster_crs = src.crs
    sub = sub.to_crs(raster_crs)

    logger.info(f"canopy[{STATES[fips]}]: extracting for {len(sub):,} buildings at 1 m")
    result = exact_extract(
        raster_path, sub, ["unique", "frac"],
        include_cols=["building_id"], output="pandas", progress=True,
    )

    from greengap.dataset import LC_CLASSES

    rows = []
    for _, r in result.iterrows():
        shares = dict(zip((int(v) for v in r["unique"]), (float(f) for f in r["frac"])))
        row = {"building_id": r["building_id"]}
        for value, name in LC_CLASSES.items():
            row[f"frac_{name}"] = shares.get(value, 0.0)
        rows.append(row)

    frac = derive_canopy_columns(pd.DataFrame(rows))
    return frac[["building_id", "canopy_pct", "natural_canopy_pct", "land_coverage"]]


def compute_canopy(buildings: gpd.GeoDataFrame) -> pd.DataFrame:
    frames = [_canopy_for_state(fips, buildings) for fips in STATE_FIPS]
    return pd.concat([f for f in frames if len(f)], ignore_index=True)


# --------------------------------------------------------------------------- #
# Landsat summer stack, per building                                           #
# --------------------------------------------------------------------------- #
def compute_landsat(buildings: gpd.GeoDataFrame) -> pd.DataFrame:
    from exactextract import exact_extract
    import rasterio

    if not LANDSAT_STACK.exists():
        raise FileNotFoundError(f"Landsat stack not found: {LANDSAT_STACK}")

    with rasterio.open(LANDSAT_STACK) as src:
        stack_crs = src.crs
        band_names = list(src.descriptions) or LANDSAT_BANDS
    b = buildings.to_crs(stack_crs)

    logger.info(f"landsat: extracting {band_names} for {len(b):,} buildings (30 m)")
    res = exact_extract(
        str(LANDSAT_STACK), b, ["mean"],
        include_cols=["building_id"], output="pandas", progress=True,
    )
    out = pd.DataFrame({"building_id": res["building_id"].values})
    mean_cols = [c for c in res.columns if c.endswith("mean")]
    for col, name in zip(mean_cols, band_names):
        out[f"mean_{name.lower()}"] = res[col].astype("float64").values
    return out


# --------------------------------------------------------------------------- #
# FEMA flood zone overlay                                                       #
# --------------------------------------------------------------------------- #
def fetch_fema(force: bool = False) -> Path:
    """Download NFHL flood-hazard zones for the study-area bbox to a GeoPackage.

    Pulls only Special Flood Hazard Area polygons (``SFHA_TF='T'``, the
    1%-annual-chance floodplain) within the corridor bounding box, paging on
    ``resultOffset`` against the service's ``maxRecordCount``.
    """
    if FEMA_NFHL.exists() and not force:
        logger.info(f"fema: cached -> {FEMA_NFHL}")
        return FEMA_NFHL

    xmin, ymin, xmax, ymax = FEMA_BBOX
    # FEMA returns HTTP 500 on large geojson pages for spatial queries (the polygon
    # payloads are big); 100 features/page is the largest that responds reliably.
    frames, offset, page = [], 0, 100
    while True:
        params = {
            "where": "SFHA_TF='T'",
            "geometry": f"{xmin},{ymin},{xmax},{ymax}",
            "geometryType": "esriGeometryEnvelope",
            "inSR": 4326,
            "spatialRel": "esriSpatialRelIntersects",
            "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF",
            "outSR": 4326,
            "resultOffset": offset,
            "resultRecordCount": page,
            "f": "geojson",
        }
        url = f"{FEMA_REST}/query?" + urlencode(params)
        # FEMA's server throws 500s mid-pagination - sometimes transient, sometimes
        # a specific page that never serves. Retry a few times; if a page still
        # fails, fall to a smaller page to step over the bad record rather than
        # abandoning the whole download.
        gj = None
        for attempt in range(4):
            try:
                with urlopen(Request(url, headers={"User-Agent": "greengap"}), timeout=180) as r:
                    gj = json.load(r)
                break
            except Exception as exc:  # noqa: BLE001 - FEMA 500s are the expected case
                logger.warning(f"fema: offset {offset:,} failed ({exc}); retry {attempt + 1}")
        if gj is None:
            if page > 10:
                page = max(10, page // 5)  # shrink and retry the same offset
                logger.warning(f"fema: shrinking page to {page} to step over offset {offset:,}")
                continue
            logger.warning(f"fema: skipping unrecoverable record at offset {offset:,}")
            offset += 1
            page = 100
            continue

        feats = gj.get("features", [])
        if not feats:
            break
        frames.append(gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326"))
        offset += len(feats)
        logger.info(f"fema: {offset:,} SFHA polygons pulled")
        if len(feats) < page:
            break
        page = 100  # restore full page after any shrink

    nfhl = pd.concat(frames, ignore_index=True)
    nfhl = gpd.GeoDataFrame(nfhl, geometry="geometry", crs="EPSG:4326")
    FEMA_NFHL.parent.mkdir(parents=True, exist_ok=True)
    nfhl.to_file(FEMA_NFHL, driver="GPKG")
    logger.success(f"fema: {len(nfhl):,} SFHA polygons -> {FEMA_NFHL}")
    return FEMA_NFHL


def compute_flood(buildings: gpd.GeoDataFrame) -> pd.DataFrame:
    """Flag buildings whose footprint intersects a high-risk NFHL flood zone."""
    ids = buildings[["building_id"]].copy()
    if not FEMA_NFHL.exists():
        logger.warning(
            f"flood: no NFHL layer at {FEMA_NFHL}; flood columns left null. Run "
            "'python -m greengap.parcel_env fetch-fema' to enable the flood overlay."
        )
        ids["in_floodplain"] = pd.NA
        return ids

    nfhl = gpd.read_file(FEMA_NFHL).to_crs(CORRIDOR_CRS)
    # The fetched layer is already restricted to SFHA polygons; a zone filter is a
    # belt-and-braces guard for a hand-supplied layer that isn't pre-filtered.
    zone_col = next((c for c in nfhl.columns if c.upper() in ("FLD_ZONE", "ZONE")), None)
    if zone_col is not None and "SFHA_TF" not in nfhl.columns:
        nfhl = nfhl[nfhl[zone_col].isin(FEMA_HIGH_RISK_ZONES)]

    hit = gpd.sjoin(
        buildings[["building_id", "geometry"]], nfhl[["geometry"]],
        how="inner", predicate="intersects",
    )["building_id"].unique()
    ids["in_floodplain"] = ids["building_id"].isin(set(hit))
    logger.info(f"flood: {int(ids['in_floodplain'].sum()):,}/{len(ids):,} in the 100-yr floodplain")
    return ids


# --------------------------------------------------------------------------- #
# Build                                                                        #
# --------------------------------------------------------------------------- #
def build(force: bool = False, buffer: float = 0.0) -> gpd.GeoDataFrame:
    """Join canopy + LST + flood onto the labelled buildings."""
    path = parcel_env_path()
    if path.exists() and not force:
        logger.info(f"parcel_env: cached -> {path}")
        return gpd.read_parquet(path)

    buildings = gpd.read_parquet(labelled_path()).to_crs(CORRIDOR_CRS)
    extract_geom = buildings.copy()
    if buffer > 0:
        # Extract over the footprint plus a ring for the building's green context.
        extract_geom["geometry"] = extract_geom.geometry.buffer(buffer)
        logger.info(f"parcel_env: extracting over footprint + {buffer:g} m buffer")

    canopy = compute_canopy(extract_geom)
    landsat = compute_landsat(extract_geom)
    flood = compute_flood(buildings)  # overlay uses the footprint, not the buffer

    out = (
        buildings.merge(canopy, on="building_id", how="left")
        .merge(landsat, on="building_id", how="left")
        .merge(flood, on="building_id", how="left")
    )
    out = gpd.GeoDataFrame(out, geometry="geometry", crs=CORRIDOR_CRS)

    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path)
    logger.success(
        f"parcel_env: {len(out):,} buildings with canopy/LST"
        f"{'/flood' if FEMA_NFHL.exists() else ''} -> {path}"
    )
    return out


# --------------------------------------------------------------------------- #
# Dashboard export                                                             #
# --------------------------------------------------------------------------- #
# Columns the housing-type dashboard reads; kept slim so the WASM bundle stays
# small. Geometry is exported as the building CENTROID (point), not the polygon:
# 5,364 polygons would bloat the Pyodide payload, and the type map only needs a
# located dot per building.
DASHBOARD_COLS = [
    "building_id", "state", "jurisdiction", "housing_type",
    "units", "value_per_unit", "year_built",
    "canopy_pct", "natural_canopy_pct", "mean_lst", "mean_ndvi", "in_floodplain",
]


def export_dashboard() -> Path:
    """Write a slim building-centroid GeoJSON for the housing-type dashboard."""
    b = build()
    pts = b[DASHBOARD_COLS].copy()
    pts["geometry"] = b.geometry.centroid
    pts = gpd.GeoDataFrame(pts, geometry="geometry", crs=b.crs).to_crs("EPSG:4326")
    pts["housing_type"] = pts["housing_type"].astype("string")

    out = PROCESSED_DATA_DIR.parent.parent / "app" / "buildings_types.geojson"
    pts.to_file(out, driver="GeoJSON")
    logger.success(f"export: {len(pts):,} building centroids -> {out}")
    return out


@app.command("fetch-fema")
def fetch_fema_cmd(force: bool = typer.Option(False, help="Re-download instead of using cache.")):
    """Download FEMA NFHL flood zones (SFHA) for the study-area bbox."""
    fetch_fema(force=force)


@app.command("build")
def build_cmd(
    force: bool = typer.Option(False, help="Recompute instead of using cache."),
    buffer: float = typer.Option(0.0, help="Metres to buffer footprints before extraction."),
):
    """Extract parcel-scale environmental exposure for the labelled buildings."""
    build(force=force, buffer=buffer)


@app.command("export-dashboard")
def export_dashboard_cmd():
    """Write the slim building-centroid GeoJSON the housing-type dashboard reads."""
    export_dashboard()


if __name__ == "__main__":
    app()

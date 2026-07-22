"""Harmonised parcel table for the study corridor: Maryland + District of Columbia.

Two assessment systems, two keys, one output. Maryland keys on ``ACCTID``; DC keys
on ``SSL``. Both are reduced to a common per-parcel schema (assessed values, a few
building/land characteristics, geometry, a multifamily flag) and unioned into a
single GeoDataFrame with ``parcel_uid = "MD:"+ACCTID`` / ``"DC:"+SSL``.

Pipeline (each step caches to ``data/`` so it only runs once):

    dc-fetch    pull DC assessor tables from the Open Data DC REST API -> data/raw
    md          read the MdProperty View gdb, subset to the 6 MD jurisdictions
    dc          join DC value + building + geometry tables on SSL
    build       filter each side to multifamily, harmonise, union -> data/processed

Run::

    uv run python -m greengap.assessor dc-fetch      # one-time REST pull
    uv run python -m greengap.assessor build         # full parcel table
    uv run python -m greengap.assessor build --force # rebuild the union only

Scope
-----
This is the **parcel-level** design. It is separate from ``greengap.dataset``,
which aggregates canopy/LST/LIHTC to census areal units. The two share nothing but
the study area; keep them apart so the areal robustness runs and the parcel
analysis do not entangle.

Maryland source
---------------
``data/raw/February_2026_Parcels.zip`` is the statewide **MdProperty View**
geodatabase (Feb 2026). It already carries the SDAT assessment fields *inline*
beside parcel geometry, so the Core+Building+Land join described in the extraction
plan is unnecessary for these variables: one layer (``md_property_view`` for
points, ``parcel_polygons`` for polygons) yields ACCTID, assessed land/improvement/
total, year built, structure sqft, building units, land use, and sales in one read.

DC source
---------
DC assessed values are **not** in the CAMA files. CAMA Commercial/Residential/
Condominium carry building characteristics and the last sale only. Assessed
land/improvement/total live in **ITSPE FACTS** (``APPRAISED_VALUE_CURRENT_*``),
the clean SSL-keyed value table. The pipeline therefore treats ITSPE FACTS as the
DC hub and joins CAMA Commercial to it for multifamily building detail. Pulling
only the CAMA files would leave every DC parcel with a null assessed value.
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

from greengap.config import PROCESSED_DATA_DIR, RAW_DATA_DIR

app = typer.Typer(help=__doc__, no_args_is_help=True)

# Single projected CRS for the whole corridor (incl. DC): NAD83 Maryland State Plane.
CORRIDOR_CRS = "EPSG:26985"

# --------------------------------------------------------------------------- #
# Maryland                                                                     #
# --------------------------------------------------------------------------- #
MD_GDB = RAW_DATA_DIR / "February_2026_Parcels.zip"
MD_GDB_VSI = f"/vsizip/{MD_GDB}/February_2026_Parcels.gdb"
MD_POLY_LAYER = "parcel_polygons"

# JURSCODE values for the six Maryland study jurisdictions.
MD_STUDY_JURS = {
    "BACI": "Baltimore City",
    "BACO": "Baltimore County",
    "ANNE": "Anne Arundel",
    "HOWA": "Howard",
    "MONT": "Montgomery",
    "PRIN": "Prince George's",
}

# Native MdProperty View fields we carry. Assessed values are SDAT's "new full
# market" land/improvement/total (NFM*). CONSIDR1 is the last-sale consideration.
MD_FIELDS = [
    "ACCTID", "JURSCODE", "CT2020", "BG2020",
    "LU", "DESCLU", "STRUBLDG", "DESCBLDG",
    "NFMLNDVL", "NFMIMPVL", "NFMTTLVL",
    "YEARBLT", "SQFTSTRC", "BLDG_UNITS", "BLDG_STORY",
    "LANDAREA", "ACRES",
    "TRADATE", "CONSIDR1",
    "OWNADD1",  # owner mailing address; SDAT withholds owner names, this proxies ownership
    "SDATDATE", "MDPVDATE",
]

# Multifamily rental identification (Maryland).
#   LU == 'M'  -> SDAT "Apartments" land use, the primary signal.
#   Structure  -> commercial building codes for apartment/multi-residence stock
#                 that can sit under a non-'M' land use (e.g. mixed use).
# A units floor is applied on the structure-code path only, to avoid pulling in
# single 2-4 unit buildings the study treats as small residential.
MD_MF_LAND_USE = {"M"}
MD_MF_STRUCT = {"C101", "C113", "C307", "C179"}  # Apartment, Multiple Residence, Res Apt Units, Mixed Res/Retail
MD_MF_STRUCT_MIN_UNITS = 5

# --------------------------------------------------------------------------- #
# District of Columbia (Open Data DC REST)                                     #
# --------------------------------------------------------------------------- #
DC_REST = (
    "https://maps2.dcgis.dc.gov/dcgis/rest/services/"
    "DCGIS_DATA/Property_and_Land/MapServer"
)
DC_RAW_DIR = RAW_DATA_DIR / "dc_assessor"

# One entry per REST layer/table we pull. ``geom`` marks the parcel geometry layer.
DC_SOURCES = {
    # Owner Polygons (Common Ownership Layer) is DC's assessment-parcel fabric: one
    # SSL-keyed polygon layer carrying geometry, the current assessed values
    # (NEWLAND/NEWIMPR/NEWTOTAL), USECODE, land area, and last sale inline - the true
    # analog of Maryland's MdProperty View. Tax Lots (id 39) is a recordation layer
    # that matches only ~14% of assessed SSLs, so it is not used for geometry.
    "owner_polygons": {"id": 40, "geom": True},   # geometry + values + use code (hub)
    "cama_commercial": {"id": 23, "geom": False},  # multifamily building characteristics
    "usecode": {"id": 54, "geom": False},       # use-code lookup (crosswalk)
    "sales": {"id": 57, "geom": False},         # full sales history
}

# DC multifamily-rental use codes (see USECODE lookup, table 54).
#   Purpose-built rental apartments and generic multifamily.
DC_MF_USECODES = {"002", "021", "022", "029"}
#   Small multifamily / conversions / cooperatives. Kept separate so a sensitivity
#   run can widen or narrow the multifamily definition without editing the core set.
DC_MF_USECODES_BROAD = {"023", "024", "025", "026", "027", "028", "214"}
# Condominium codes are per-unit ownership, not rental stock -> excluded entirely.


# --------------------------------------------------------------------------- #
# Cache paths                                                                  #
# --------------------------------------------------------------------------- #
def dc_raw_path(name: str) -> Path:
    ext = "gpkg" if DC_SOURCES[name]["geom"] else "parquet"
    return DC_RAW_DIR / f"{name}.{ext}"


def md_parcels_path() -> Path:
    return PROCESSED_DATA_DIR / "md_parcels.parquet"


def dc_parcels_path() -> Path:
    return PROCESSED_DATA_DIR / "dc_parcels.parquet"


def parcels_path() -> Path:
    return PROCESSED_DATA_DIR / "parcels_mf.parquet"


# --------------------------------------------------------------------------- #
# DC: fetch from the REST API                                                  #
# --------------------------------------------------------------------------- #
def _rest_count(layer_id: int) -> int:
    url = f"{DC_REST}/{layer_id}/query?" + urlencode(
        {"where": "1=1", "returnCountOnly": "true", "f": "json"}
    )
    with urlopen(Request(url, headers={"User-Agent": "greengap"}), timeout=120) as r:
        return int(json.load(r)["count"])


def _rest_max_record_count(layer_id: int) -> int:
    url = f"{DC_REST}/{layer_id}?f=json"
    with urlopen(Request(url, headers={"User-Agent": "greengap"}), timeout=120) as r:
        return int(json.load(r).get("maxRecordCount") or 1000)


def _fetch_rest_layer(layer_id: int, want_geom: bool) -> gpd.GeoDataFrame:
    """Pull an entire ArcGIS REST layer/table by paging on ``resultOffset``.

    GDAL will not treat a bare MapServer layer URL as a feature service, so the
    paging is explicit here. The page size is pinned to the service's own
    ``maxRecordCount``: requesting more silently returns only that many rows per
    page, and advancing the offset by the requested (larger) amount would skip
    every block in between - a silent truncation. The offset instead advances by
    the number of rows actually returned, and paging stops on the first short page.
    """
    import pyogrio

    total = _rest_count(layer_id)
    page = _rest_max_record_count(layer_id)
    logger.info(f"dc-fetch: layer {layer_id} has {total:,} records (page={page})")

    # Page until the authoritative record count is reached, not until a short page:
    # a single transient empty/short response would otherwise end paging early and
    # silently truncate. Each page is retried a few times before giving up.
    frames, offset = [], 0
    while offset < total:
        params = {
            "where": "1=1",
            "outFields": "*",
            "resultOffset": offset,
            "resultRecordCount": page,
            "f": "json",
        }
        params["returnGeometry"] = "true" if want_geom else "false"
        if want_geom:
            params["outSR"] = 26985  # request geometry already in the corridor CRS
        # Force the ESRIJSON driver: with outFields=* GDAL otherwise routes the
        # URL through /vsicurl/ and fails to sniff the format.
        url = f"ESRIJSON:{DC_REST}/{layer_id}/query?" + urlencode(params)

        chunk = None
        for attempt in range(4):
            chunk = pyogrio.read_dataframe(url, read_geometry=want_geom)
            if len(chunk) > 0:
                break
            logger.warning(
                f"dc-fetch: layer {layer_id} empty page at offset {offset:,} "
                f"(attempt {attempt + 1}); retrying"
            )
        if chunk is None or len(chunk) == 0:
            raise RuntimeError(
                f"dc-fetch: layer {layer_id} returned no rows at offset {offset:,} "
                f"of {total:,} after retries; aborting rather than truncating"
            )
        frames.append(chunk)
        offset += len(chunk)
        logger.info(f"dc-fetch: layer {layer_id} {offset:,}/{total:,}")

    out = pd.concat(frames, ignore_index=True)
    if len(out) != total:
        logger.warning(
            f"dc-fetch: layer {layer_id} pulled {len(out):,} rows but service "
            f"reports {total:,} - possible truncation"
        )
    if want_geom:
        out = gpd.GeoDataFrame(out, geometry="geometry", crs=CORRIDOR_CRS)
    return out


def fetch_dc(force: bool = False) -> None:
    """Download every DC assessor source to ``data/raw/dc_assessor`` once."""
    DC_RAW_DIR.mkdir(parents=True, exist_ok=True)
    for name, spec in DC_SOURCES.items():
        path = dc_raw_path(name)
        if path.exists() and not force:
            logger.info(f"dc-fetch: cached -> {path}")
            continue
        gdf = _fetch_rest_layer(spec["id"], spec["geom"])
        if spec["geom"]:
            gdf.to_file(path, driver="GPKG")
        else:
            pd.DataFrame(gdf).to_parquet(path)
        logger.success(f"dc-fetch: {name} -> {len(gdf):,} rows -> {path}")


# --------------------------------------------------------------------------- #
# Maryland: read + filter                                                      #
# --------------------------------------------------------------------------- #
def load_md_parcels(force: bool = False) -> gpd.GeoDataFrame:
    """Read the MdProperty View polygons for the six study jurisdictions.

    Reads polygons (not points) so parcel area and buffers are available downstream.
    The read is filtered to the study jurisdictions via an attribute WHERE clause so
    the full 2.4 M-row statewide file never lands in memory at once.
    """
    path = md_parcels_path()
    if path.exists() and not force:
        logger.info(f"md: cached -> {path}")
        return gpd.read_parquet(path)

    import pyogrio

    jurs = "','".join(MD_STUDY_JURS)
    where = f"JURSCODE IN ('{jurs}')"
    logger.info(f"md: reading {MD_POLY_LAYER} where {where}")
    gdf = pyogrio.read_dataframe(
        MD_GDB_VSI, layer=MD_POLY_LAYER, columns=MD_FIELDS, where=where
    )
    gdf = gdf.set_crs(CORRIDOR_CRS, allow_override=True)  # gdb is already 26985
    logger.info(f"md: {len(gdf):,} parcels across {gdf['JURSCODE'].nunique()} jurisdictions")

    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(path)
    logger.success(f"md: {len(gdf):,} parcels -> {path}")
    return gdf


def flag_md_multifamily(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Add ``is_multifamily`` and drop non-multifamily rows (Maryland)."""
    units = pd.to_numeric(gdf["BLDG_UNITS"], errors="coerce").fillna(0)
    by_lu = gdf["LU"].isin(MD_MF_LAND_USE)
    by_struct = gdf["STRUBLDG"].isin(MD_MF_STRUCT) & (units >= MD_MF_STRUCT_MIN_UNITS)
    mf = by_lu | by_struct
    logger.info(
        f"md: multifamily {int(mf.sum()):,} "
        f"(LU='M' {int(by_lu.sum()):,}, struct-code {int((by_struct & ~by_lu).sum()):,})"
    )
    return gdf.loc[mf].copy()


# --------------------------------------------------------------------------- #
# DC: join + filter                                                           #
# --------------------------------------------------------------------------- #
def load_dc_parcels(force: bool = False) -> gpd.GeoDataFrame:
    """Join DC value + building + geometry tables on SSL into one parcel frame."""
    path = dc_parcels_path()
    if path.exists() and not force:
        logger.info(f"dc: cached -> {path}")
        return gpd.read_parquet(path)

    for name in ("owner_polygons", "cama_commercial"):
        if not dc_raw_path(name).exists():
            raise FileNotFoundError(
                f"DC source {name} missing ({dc_raw_path(name)}). "
                "Run: uv run python -m greengap.assessor dc-fetch"
            )

    polys = gpd.read_file(dc_raw_path("owner_polygons"))
    comm = pd.read_parquet(dc_raw_path("cama_commercial"))

    # SSL is the key everywhere; keep it a string and never numeric (suffixes).
    for df in (polys, comm):
        df["SSL"] = df["SSL"].astype("string").str.strip()

    # Owner Polygons can carry historic duplicates per SSL; keep the current record.
    polys = polys[polys.geometry.notna()]
    if "ISHISTORIC" in polys.columns:
        polys = polys[polys["ISHISTORIC"].fillna(0).astype(int) == 0]
    polys = polys.drop_duplicates("SSL")

    # CAMA Commercial can hold several building rows per SSL; keep the largest by
    # unit count so a multi-building complex contributes one representative record.
    comm["NUM_UNITS"] = pd.to_numeric(comm["NUM_UNITS"], errors="coerce")
    comm = comm.sort_values("NUM_UNITS", ascending=False).drop_duplicates("SSL")
    comm_cols = ["SSL", "NUM_UNITS", "AYB", "EYB", "YR_RMDL", "LIVING_GBA", "STRUCT_CL_D"]

    gdf = polys.merge(comm[comm_cols], on="SSL", how="left")

    # Owner Polygons carries only the use CODE; attach the human-readable label from
    # the USECODE lookup so the harmonised table has a use description like Maryland.
    if dc_raw_path("usecode").exists():
        uc = pd.read_parquet(dc_raw_path("usecode"))[["CODE", "DESCRIPTION"]]
        uc["CODE"] = uc["CODE"].astype("string").str.strip()
        gdf["USECODE"] = gdf["USECODE"].astype("string").str.strip()
        gdf = gdf.merge(uc.rename(columns={"DESCRIPTION": "USECODE_DESC"}),
                        left_on="USECODE", right_on="CODE", how="left")

    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=polys.crs).to_crs(CORRIDOR_CRS)

    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(path)
    logger.success(f"dc: {len(gdf):,} SSL parcels (Owner Polygons) -> {path}")
    return gdf


def flag_dc_multifamily(gdf: gpd.GeoDataFrame, broad: bool = False) -> gpd.GeoDataFrame:
    """Add ``is_multifamily`` and drop non-multifamily rows (DC)."""
    codes = set(DC_MF_USECODES)
    if broad:
        codes |= DC_MF_USECODES_BROAD
    use = gdf["USECODE"].astype("string").str.strip()
    mf = use.isin(codes)
    logger.info(
        f"dc: multifamily {int(mf.sum()):,} across use codes {sorted(codes)}"
        + (" (broad)" if broad else "")
    )
    return gdf.loc[mf].copy()


# --------------------------------------------------------------------------- #
# Harmonisation                                                                #
# --------------------------------------------------------------------------- #
# Target schema. Each field maps to one MD source column and one DC source column;
# see references/tax-assessor-extraction.md for the appendix crosswalk table.
def harmonise_md(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    out = gpd.GeoDataFrame(
        {
            "parcel_uid": "MD:" + gdf["ACCTID"].astype("string"),
            "jurisdiction": gdf["JURSCODE"].map(MD_STUDY_JURS),
            "state": "MD",
            "native_key": gdf["ACCTID"].astype("string"),
            "assessed_land": pd.to_numeric(gdf["NFMLNDVL"], errors="coerce"),
            "assessed_improvement": pd.to_numeric(gdf["NFMIMPVL"], errors="coerce"),
            "assessed_total": pd.to_numeric(gdf["NFMTTLVL"], errors="coerce"),
            "year_built": pd.to_numeric(gdf["YEARBLT"], errors="coerce"),
            "units": pd.to_numeric(gdf["BLDG_UNITS"], errors="coerce"),
            "building_area": pd.to_numeric(gdf["SQFTSTRC"], errors="coerce"),
            "lot_area": pd.to_numeric(gdf["LANDAREA"], errors="coerce"),
            "use_code": gdf["LU"].astype("string"),
            "use_desc": gdf["DESCLU"].astype("string"),
            "owner": gdf["OWNADD1"].astype("string"),  # mailing address (no owner name in SDAT bulk)
            "sale_price": pd.to_numeric(gdf["CONSIDR1"], errors="coerce"),
            "sale_date": pd.to_datetime(gdf["TRADATE"], errors="coerce"),
        },
        geometry=gdf.geometry,
        crs=gdf.crs,
    )
    return out


def harmonise_dc(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    out = gpd.GeoDataFrame(
        {
            "parcel_uid": "DC:" + gdf["SSL"].astype("string"),
            "jurisdiction": "District of Columbia",
            "state": "DC",
            "native_key": gdf["SSL"].astype("string"),
            "assessed_land": pd.to_numeric(gdf["NEWLAND"], errors="coerce"),
            "assessed_improvement": pd.to_numeric(gdf["NEWIMPR"], errors="coerce"),
            "assessed_total": pd.to_numeric(gdf["NEWTOTAL"], errors="coerce"),
            "year_built": pd.to_numeric(gdf["AYB"], errors="coerce"),
            "units": pd.to_numeric(gdf["NUM_UNITS"], errors="coerce"),
            "building_area": pd.to_numeric(gdf["LIVING_GBA"], errors="coerce"),
            "lot_area": pd.to_numeric(gdf["LANDAREA"], errors="coerce"),
            "use_code": gdf["USECODE"].astype("string"),
            "use_desc": gdf.get("USECODE_DESC", pd.Series(pd.NA, index=gdf.index)).astype("string"),
            "owner": gdf["OWNERNAME"].astype("string"),
            "sale_price": pd.to_numeric(gdf["SALEPRICE"], errors="coerce"),
            "sale_date": pd.to_datetime(gdf["SALEDATE"], errors="coerce"),
        },
        geometry=gdf.geometry,
        crs=gdf.crs,
    )
    return out


HARMONISED_COLS = [
    "parcel_uid", "jurisdiction", "state", "native_key",
    "assessed_land", "assessed_improvement", "assessed_total",
    "year_built", "units", "building_area", "lot_area",
    "use_code", "use_desc", "owner", "sale_price", "sale_date", "geometry",
]


# --------------------------------------------------------------------------- #
# QA                                                                           #
# --------------------------------------------------------------------------- #
def run_qa(gdf: gpd.GeoDataFrame) -> None:
    """QA checks from the extraction plan; warnings, not hard failures."""
    dups = int(gdf["parcel_uid"].duplicated().sum())
    if dups:
        logger.warning(f"qa: {dups} duplicate parcel_uid (must be 0)")

    for state, sub in gdf.groupby("state"):
        n = len(sub)
        logger.info(f"qa[{state}]: {n:,} multifamily parcels")
        for col in ("assessed_total", "year_built", "units", "geometry"):
            null = int(sub[col].isna().sum())
            logger.info(f"qa[{state}]:   null {col}: {null:,} ({null / n:.1%})")

        # assessed_total ~= land + improvement (within rounding), where all present
        v = sub.dropna(subset=["assessed_land", "assessed_improvement", "assessed_total"])
        gap = (v["assessed_land"] + v["assessed_improvement"] - v["assessed_total"]).abs()
        n_off = int((gap > 1).sum())
        if n_off:
            logger.warning(f"qa[{state}]: {n_off:,} rows where land+impr != total (>$1)")

        yb = pd.to_numeric(sub["year_built"], errors="coerce")
        n_yb = int(((yb > 0) & ((yb < 1750) | (yb > 2026))).sum())
        if n_yb:
            logger.warning(f"qa[{state}]: {n_yb:,} implausible year_built")

        area = sub.geometry.area
        n_area = int(((area <= 0) | (area > 5_000_000)).sum())
        if n_area:
            logger.warning(f"qa[{state}]: {n_area:,} parcels with zero/absurd geometry area")


# --------------------------------------------------------------------------- #
# Build                                                                        #
# --------------------------------------------------------------------------- #
def build_parcels(
    force: bool = False, force_upstream: bool = False, broad_dc: bool = False
) -> gpd.GeoDataFrame:
    """Filter each side to multifamily, harmonise, union, QA, cache."""
    path = parcels_path()
    if path.exists() and not force and not force_upstream:
        logger.info(f"build: cached -> {path}")
        return gpd.read_parquet(path)

    md = flag_md_multifamily(load_md_parcels(force=force_upstream))
    dc = flag_dc_multifamily(load_dc_parcels(force=force_upstream), broad=broad_dc)

    md_h = harmonise_md(md)[HARMONISED_COLS]
    dc_h = harmonise_dc(dc)[HARMONISED_COLS]

    gdf = pd.concat([md_h, dc_h], ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=CORRIDOR_CRS)

    # Tax-exempt / zero-value parcels: keep the row but null the value features so a
    # placeholder zero never enters the value distribution.
    zero_val = gdf["assessed_total"].fillna(0) <= 0
    if int(zero_val.sum()):
        logger.info(f"build: nulling assessed values for {int(zero_val.sum()):,} zero/exempt parcels")
        for col in ("assessed_land", "assessed_improvement", "assessed_total"):
            gdf.loc[zero_val, col] = pd.NA

    run_qa(gdf)

    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(path)
    logger.success(
        f"build: {len(gdf):,} multifamily parcels "
        f"(MD {int((gdf['state'] == 'MD').sum()):,}, DC {int((gdf['state'] == 'DC').sum()):,}) "
        f"-> {path}"
    )
    return gdf


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
@app.command("dc-fetch")
def dc_fetch(force: bool = typer.Option(False, help="Re-download instead of using cache.")):
    """Pull the DC assessor tables from the Open Data DC REST API."""
    fetch_dc(force=force)


@app.command()
def md(force: bool = typer.Option(False, help="Re-read the gdb instead of using cache.")):
    """Read + cache the Maryland study-area parcels."""
    load_md_parcels(force=force)


@app.command()
def dc(force: bool = typer.Option(False, help="Rejoin instead of using cache.")):
    """Join + cache the DC parcels (needs dc-fetch first)."""
    load_dc_parcels(force=force)


@app.command()
def build(
    force: bool = typer.Option(False, help="Rebuild the union (fast); keeps cached sides."),
    force_upstream: bool = typer.Option(False, help="Also re-read MD gdb / rejoin DC."),
    broad_dc: bool = typer.Option(False, help="Widen DC multifamily to small/conversion/coop codes."),
):
    """Build the harmonised multifamily parcel table."""
    build_parcels(force=force, force_upstream=force_upstream, broad_dc=broad_dc)


if __name__ == "__main__":
    app()

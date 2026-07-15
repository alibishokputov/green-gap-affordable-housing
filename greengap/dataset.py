"""Build the tract-level analysis table: tree canopy % x LIHTC low-income units.

Pipeline (each step caches to ``data/`` so it only runs once):

    tracts   TIGER census tracts for the study state(s)      -> data/interim/
    lihtc    LIHTC project points aggregated to tracts       -> data/interim/
    canopy   Chesapeake 1 m land-cover class fractions       -> data/interim/
    build    join the three into the analysis table          -> data/processed/

Run::

    uv run python -m greengap.dataset build            # everything (cached)
    uv run python -m greengap.dataset build --force    # recompute from scratch
    uv run python -m greengap.dataset canopy --counties "Baltimore city"  # subset

Notes
-----
* **Maryland only.** The bundled ``LIHTC.csv`` contains MD projects exclusively
  (948 rows, 0 in DC), so the study area here is MD. Add a DC extract and the
  ``STATE_FIPS`` list below to widen it.
* **Canopy definition.** The Chesapeake 13-class land cover splits tree canopy
  across four values (3 = Tree Canopy, 10/11/12 = canopy over structures /
  other impervious / roads). Urban-tree-canopy convention counts all four;
  using only class 3 undercounts canopy in dense urban tracts. We store *every*
  class fraction so any definition can be derived later, and expose two ready
  columns: ``canopy_pct`` (share of land area) and ``canopy_pct_total``
  (share of total tract area, water included).
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from loguru import logger
import pandas as pd
import typer

from greengap.config import EXTERNAL_DATA_DIR, INTERIM_DATA_DIR, PROCESSED_DATA_DIR, RAW_DATA_DIR

app = typer.Typer(help=__doc__, no_args_is_help=True)

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #
STATES = {"24": "MD", "11": "DC"}  # FIPS -> USPS, the study area
STATE_FIPS = list(STATES)
TIGER_YEAR = 2022
TIGER_URL = "https://www2.census.gov/geo/tiger/TIGER{year}/TRACT/tl_{year}_{fips}_tract.zip"
TIGER_COUNTY_URL = "https://www2.census.gov/geo/tiger/TIGER{year}/COUNTY/tl_{year}_us_county.zip"

# Chesapeake Bay Program 13-class land cover (see references/metadata_2022-Edition.xml).
LC_CLASSES: dict[int, str] = {
    1: "water",
    2: "emergent_wetlands",
    3: "tree_canopy",
    4: "scrub_shrub",
    5: "low_vegetation",
    6: "barren",
    7: "impervious_structures",
    8: "other_impervious",
    9: "impervious_roads",
    10: "tree_canopy_over_structures",
    11: "tree_canopy_over_other_impervious",
    12: "tree_canopy_over_impervious_roads",
}

# A tract's canopy % is only trustworthy if the raster actually classified its
# land. Allow a small gap against the Census' land share (edge effects, vintage
# differences in the shoreline) but reject tracts that are materially unclassified.
CANOPY_COVERAGE_TOLERANCE = 0.05  # max (Census land share - classified land share)
MIN_LAND_COVERAGE = 0.01  # a tract needs >1% classified land at all

# Urban tree canopy = canopy anywhere, including overhanging impervious surfaces.
CANOPY_VALUES = [3, 10, 11, 12]
# Canopy not overhanging impervious surfaces (conservative "green space" reading).
NATURAL_CANOPY_VALUES = [3]
WATER_VALUES = [1]

# ---- LIHTC source -------------------------------------------------------- #
# We read HUD's *national* database (LIHTCPUB.xlsx inside lihtc.zip) rather than
# the bundled LIHTC.csv, because that CSV is a Maryland-only extract: it has 948
# MD rows and zero DC rows, which would silently make a "MD + DC" study MD-only.
# The national file carries both (MD 948, DC 268) with an identical schema.
#
# openpyxl cannot parse HUD's export (it emits a `synchVertical` attribute that
# openpyxl rejects), hence the calamine engine.
LIHTC_ZIP = RAW_DATA_DIR / "lihtc.zip"
LIHTC_MEMBER = "LIHTCPUB.xlsx"
LIHTC_ENGINE = "calamine"

# HUD ships pre-cleaned unit columns (suffix "r") beside the raw ones. Raw
# `li_units` has 27 nulls in MD alone, and pandas' sum() skips them silently -
# a ~2,200-unit undercount. `li_unitr`/`n_unitsr` are fully populated and are
# what HUD's data dictionary says to analyse.
LIHTC_COLS = {
    "state": "proj_st",
    "id": "hud_id",
    "lat": "latitude",
    "lon": "longitude",
    "low_income": "li_unitr",  # imputed, 0 nulls
    "total": "n_unitsr",  # imputed, 0 nulls
}

# ---- Land cover rasters (one per state; Chesapeake ships them separately) -- #
LC_RASTERS: dict[str, tuple[Path, str]] = {
    "24": (EXTERNAL_DATA_DIR / "md_lc_2018_2022-Edition.zip", "md_lc_2018_2022-Edition.tif"),
    "11": (EXTERNAL_DATA_DIR / "dc_lc_2017_2022-Edition.zip", "dc_lc_2017_2022-Edition.tif"),
}

TRACTS_PATH = INTERIM_DATA_DIR / "tracts.parquet"
LIHTC_TRACT_PATH = INTERIM_DATA_DIR / "lihtc_by_tract.parquet"
ANALYSIS_PATH = PROCESSED_DATA_DIR / "tract_canopy_lihtc.parquet"


def canopy_path(fips: str) -> Path:
    """Canopy cache is per-state: each state has its own 1 m raster, and a full
    Maryland extraction takes ~20 minutes - we never want to redo it to add DC."""
    return INTERIM_DATA_DIR / f"canopy_by_tract_{fips}.parquet"


# --------------------------------------------------------------------------- #
# Steps                                                                        #
# --------------------------------------------------------------------------- #
def load_tracts(force: bool = False) -> gpd.GeoDataFrame:
    """Census tracts for ``STATE_FIPS``, downloaded from TIGER and cached."""
    if TRACTS_PATH.exists() and not force:
        logger.info(f"tracts: cached -> {TRACTS_PATH}")
        return gpd.read_parquet(TRACTS_PATH)

    frames = []
    for fips in STATE_FIPS:
        url = TIGER_URL.format(year=TIGER_YEAR, fips=fips)
        logger.info(f"tracts: downloading {url}")
        frames.append(gpd.read_file(url))

    tracts = pd.concat(frames, ignore_index=True)
    tracts = gpd.GeoDataFrame(tracts, geometry="geometry", crs=frames[0].crs)
    tracts = tracts[["GEOID", "NAMELSAD", "STATEFP", "COUNTYFP", "ALAND", "AWATER", "geometry"]]

    # Tract NAMELSAD is only "Census Tract 101" - join the county layer so the
    # dashboard can offer a human-readable county filter. Use the county's
    # NAMELSAD, not NAME: in Maryland the independent Baltimore city (FIPS 510)
    # and Baltimore County (FIPS 005) share NAME="Baltimore", which would
    # silently merge two distinct jurisdictions into one filter entry.
    logger.info("tracts: joining county names")
    counties = gpd.read_file(TIGER_COUNTY_URL.format(year=TIGER_YEAR))
    counties = counties[counties["STATEFP"].isin(STATE_FIPS)][["STATEFP", "COUNTYFP", "NAMELSAD"]]
    counties = counties.rename(columns={"NAMELSAD": "county"})
    tracts = tracts.merge(counties, on=["STATEFP", "COUNTYFP"], how="left")

    TRACTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tracts.to_parquet(TRACTS_PATH)
    logger.success(
        f"tracts: {len(tracts)} features across {tracts['county'].nunique()} counties "
        f"-> {TRACTS_PATH}"
    )
    return tracts


def load_lihtc_by_tract(force: bool = False) -> pd.DataFrame:
    """Aggregate LIHTC project points to tracts (sum of units, project count)."""
    if LIHTC_TRACT_PATH.exists() and not force:
        logger.info(f"lihtc: cached -> {LIHTC_TRACT_PATH}")
        return pd.read_parquet(LIHTC_TRACT_PATH)

    src = f"/vsizip/{LIHTC_ZIP}/{LIHTC_MEMBER}"
    df = pd.read_excel(f"zip://{LIHTC_MEMBER}::{LIHTC_ZIP}", engine=LIHTC_ENGINE)
    logger.info(f"lihtc: {len(df):,} national rows from {LIHTC_ZIP.name}/{LIHTC_MEMBER}")

    missing = [c for c in LIHTC_COLS.values() if c not in df.columns]
    if missing:
        raise KeyError(f"LIHTC source {src} is missing expected column(s): {missing}")

    wanted = set(STATES.values())
    df = df[df[LIHTC_COLS["state"]].isin(wanted)]
    by_state = df[LIHTC_COLS["state"]].value_counts().to_dict()
    logger.info(f"lihtc: {len(df)} rows in study area {sorted(wanted)} -> {by_state}")

    lat, lon = LIHTC_COLS["lat"], LIHTC_COLS["lon"]
    n_missing = int(df[lat].isna().sum())
    if n_missing:
        logger.warning(
            f"lihtc: dropping {n_missing} project(s) with no coordinates "
            f"({n_missing / len(df):.1%} of study-area rows) - they cannot be mapped."
        )
    df = df.dropna(subset=[lat, lon])

    points = gpd.GeoDataFrame(
        df,
        geometry=gpd.points_from_xy(df[lon], df[lat]),
        crs="EPSG:4326",
    )

    tracts = load_tracts()
    points = points.to_crs(tracts.crs)
    joined = gpd.sjoin(points, tracts[["GEOID", "geometry"]], how="inner", predicate="within")

    dropped = len(points) - len(joined)
    if dropped:
        logger.warning(f"lihtc: {dropped} point(s) fell outside the tract layer.")

    for key in ("low_income", "total"):
        col = LIHTC_COLS[key]
        n_null = int(joined[col].isna().sum())
        if n_null:
            logger.warning(f"lihtc: {n_null} null(s) in HUD column {col!r}")

    agg = (
        joined.groupby("GEOID")
        .agg(
            lihtc_projects=(LIHTC_COLS["id"], "count"),
            lihtc_units_low_income=(LIHTC_COLS["low_income"], "sum"),
            lihtc_units_total=(LIHTC_COLS["total"], "sum"),
        )
        .reset_index()
    )

    LIHTC_TRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    agg.to_parquet(LIHTC_TRACT_PATH)
    logger.success(
        f"lihtc: {len(joined)} projects -> {len(agg)} tracts with LIHTC "
        f"-> {LIHTC_TRACT_PATH}"
    )
    return agg


def _resolve_raster(fips: str, raster: Path | None = None) -> str:
    """Path to a state's land-cover raster.

    Prefers an already-extracted local copy (fast random access); otherwise
    falls back to reading inside the zip via GDAL's ``/vsizip/`` handler, which
    works but is slower for many windowed reads.
    """
    if raster is not None:
        return str(raster)
    zip_path, member = LC_RASTERS[fips]
    if not zip_path.exists():
        raise FileNotFoundError(f"Land-cover raster for {STATES[fips]} not found: {zip_path}")
    return f"/vsizip/{zip_path}/{member}"


def derive_canopy_columns(frac: pd.DataFrame) -> pd.DataFrame:
    """Derive canopy percentages from per-class area fractions.

    Kept separate from the raster extraction so the (load-bearing) definitional
    arithmetic is unit-testable without touching a 2 GB raster.

    Denominators are computed from *classified* area only. The class fractions do
    not always sum to 1: a handful of tracts straddle the edge of the Chesapeake
    raster (Bay / Atlantic / state line) and are partly nodata - one Maryland
    tract is only 52% covered. Dividing those by a hard-coded 1.0 would silently
    halve their canopy, so ``data_coverage`` is the real denominator.

    * ``canopy_pct``       - canopy as a share of classified **land** (water and
      nodata both excluded). A tract half-covered by the Bay should not read as
      half as green as an otherwise identical inland tract.
    * ``canopy_pct_total`` - canopy as a share of all classified area (water in).
    * ``data_coverage``    - share of the tract with any land-cover class; drop
      or flag low values before drawing conclusions.
    """
    out = frac.copy()
    frac_cols = [f"frac_{name}" for name in LC_CLASSES.values()]

    canopy = out[[f"frac_{LC_CLASSES[v]}" for v in CANOPY_VALUES]].sum(axis=1)
    natural = out[[f"frac_{LC_CLASSES[v]}" for v in NATURAL_CANOPY_VALUES]].sum(axis=1)
    water = out[[f"frac_{LC_CLASSES[v]}" for v in WATER_VALUES]].sum(axis=1)
    classified = out[frac_cols].sum(axis=1)

    land = (classified - water).clip(lower=1e-9)  # no land (all water/nodata) -> 0%
    denom_total = classified.clip(lower=1e-9)

    out["canopy_pct"] = (canopy / land) * 100
    out["canopy_pct_total"] = (canopy / denom_total) * 100
    out["natural_canopy_pct"] = (natural / land) * 100
    out["water_pct"] = (water / denom_total) * 100
    out["data_coverage"] = classified
    out["land_coverage"] = classified - water
    return out


def compute_canopy_state(
    fips: str,
    force: bool = False,
    raster: Path | None = None,
    counties: list[str] | None = None,
) -> pd.DataFrame:
    """Per-tract land-cover class fractions for one state's 1 m raster.

    Uses ``exactextract`` (exact polygon/pixel intersection, not centroid
    sampling), so partial boundary pixels are weighted correctly.
    """
    path = canopy_path(fips)
    if path.exists() and not force and counties is None:
        logger.info(f"canopy[{STATES[fips]}]: cached -> {path}")
        return pd.read_parquet(path)

    from exactextract import exact_extract
    import rasterio

    tracts = load_tracts()
    tracts = tracts[tracts["STATEFP"] == fips]
    if counties:
        tracts = tracts[tracts["county"].str.contains("|".join(counties), case=False, na=False)]
        if tracts.empty:
            raise ValueError(
                f"No {STATES[fips]} tracts matched county filter {counties}. "
                "Filter matches the county name (e.g. 'Baltimore city', 'Montgomery')."
            )
        logger.info(f"canopy[{STATES[fips]}]: subset to {len(tracts)} tracts matching {counties}")

    raster_path = _resolve_raster(fips, raster)
    with rasterio.open(raster_path) as src:
        raster_crs = src.crs
        logger.info(f"canopy[{STATES[fips]}]: raster crs={raster_crs} res={src.res}")

    # Equal-area CRS -> pixel counts are directly proportional to ground area.
    tracts = tracts.to_crs(raster_crs)

    logger.info(
        f"canopy[{STATES[fips]}]: extracting class fractions for {len(tracts)} tracts "
        "at 1 m (slow for Maryland)"
    )
    result = exact_extract(
        raster_path,
        tracts,
        ["unique", "frac"],
        include_cols=["GEOID"],
        output="pandas",
        progress=True,
    )

    rows = []
    for _, r in result.iterrows():
        values, fracs = r["unique"], r["frac"]
        shares = dict(zip((int(v) for v in values), (float(f) for f in fracs)))
        row = {"GEOID": r["GEOID"]}
        for value, name in LC_CLASSES.items():
            row[f"frac_{name}"] = shares.get(value, 0.0)
        rows.append(row)

    out = derive_canopy_columns(pd.DataFrame(rows))

    if counties is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(path)
        logger.success(f"canopy[{STATES[fips]}]: {len(out)} tracts -> {path}")
    else:
        logger.success(f"canopy[{STATES[fips]}]: {len(out)} tracts (subset; not cached)")
    return out


def compute_canopy(force: bool = False, rasters: dict[str, Path] | None = None) -> pd.DataFrame:
    """Canopy fractions for every state in the study area, concatenated."""
    rasters = rasters or {}
    frames = [
        compute_canopy_state(fips, force=force, raster=rasters.get(fips))
        for fips in STATE_FIPS
    ]
    return pd.concat(frames, ignore_index=True)


def build_analysis_table(force: bool = False, force_upstream: bool = False) -> gpd.GeoDataFrame:
    """Join tracts + canopy + LIHTC into the final analysis table.

    ``force`` only rebuilds the join (seconds). ``force_upstream`` additionally
    re-runs the source steps - including the ~20-minute Maryland raster
    extraction - so it is deliberately opt-in rather than implied by ``force``.
    """
    if ANALYSIS_PATH.exists() and not force and not force_upstream:
        logger.info(f"build: cached -> {ANALYSIS_PATH}")
        return gpd.read_parquet(ANALYSIS_PATH)

    tracts = load_tracts(force=force_upstream)
    canopy = compute_canopy(force=force_upstream)
    lihtc = load_lihtc_by_tract(force=force_upstream)

    gdf = tracts.merge(canopy, on="GEOID", how="left").merge(lihtc, on="GEOID", how="left")

    # Tracts with no LIHTC project are true zeros, not missing data.
    for col in ["lihtc_projects", "lihtc_units_low_income", "lihtc_units_total"]:
        gdf[col] = gdf[col].fillna(0).astype(int)

    # ---- Flag tracts whose canopy % rests on too little classified land ----
    # The Chesapeake land cover leaves military installations as nodata (Aberdeen
    # Proving Ground, Joint Base Andrews). One Harford County tract is 47% land
    # per the Census but only 0.08% classified, so its canopy % is computed from a
    # sliver and lands at a meaningless 82%. Compare classified land against the
    # Census' own land share and null out the canopy of tracts that fall short,
    # rather than let them render as confident outliers.
    tiger_land_share = gdf["ALAND"] / (gdf["ALAND"] + gdf["AWATER"]).clip(lower=1)
    coverage_gap = tiger_land_share - gdf["land_coverage"]
    gdf["canopy_reliable"] = (coverage_gap <= CANOPY_COVERAGE_TOLERANCE) & (
        gdf["land_coverage"] > MIN_LAND_COVERAGE
    )
    n_bad = int((~gdf["canopy_reliable"]).sum())
    if n_bad:
        logger.warning(
            f"build: {n_bad} tract(s) have insufficient classified land "
            f"(likely nodata over military land); their canopy is set to NaN. "
            f"GEOIDs: {gdf.loc[~gdf['canopy_reliable'], 'GEOID'].tolist()}"
        )
        for col in ["canopy_pct", "canopy_pct_total", "natural_canopy_pct"]:
            gdf.loc[~gdf["canopy_reliable"], col] = pd.NA

    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=tracts.crs).to_crs("EPSG:4326")

    ANALYSIS_PATH.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(ANALYSIS_PATH)
    logger.success(
        f"build: {len(gdf)} tracts "
        f"({(gdf['lihtc_units_low_income'] > 0).sum()} with LIHTC units, "
        f"{int(gdf['canopy_reliable'].sum())} with reliable canopy) -> {ANALYSIS_PATH}"
    )
    return gdf


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
@app.command()
def tracts(force: bool = typer.Option(False, help="Re-download instead of using cache.")):
    """Download + cache the TIGER census tracts."""
    load_tracts(force=force)


@app.command()
def lihtc(force: bool = typer.Option(False, help="Recompute instead of using cache.")):
    """Aggregate LIHTC points to tracts."""
    load_lihtc_by_tract(force=force)


@app.command()
def canopy(
    state: str = typer.Option(None, help="Limit to one state FIPS (24=MD, 11=DC)."),
    force: bool = typer.Option(False, help="Recompute instead of using cache."),
    raster: Path = typer.Option(None, help="Extracted .tif (faster than reading the zip)."),
    counties: list[str] = typer.Option(None, help="Subset to matching county names (testing)."),
):
    """Compute per-tract land-cover class fractions from the 1 m raster(s)."""
    if state:
        if state not in STATES:
            raise typer.BadParameter(f"Unknown state FIPS {state!r}; expected one of {STATE_FIPS}")
        compute_canopy_state(
            state,
            force=force,
            raster=raster,
            counties=list(counties) if counties else None,
        )
        return
    if raster or counties:
        raise typer.BadParameter("--raster/--counties require --state (they are per-state).")
    compute_canopy(force=force)


@app.command()
def build(
    force: bool = typer.Option(False, help="Rebuild the join (fast); keeps cached sources."),
    force_upstream: bool = typer.Option(
        False, help="Also re-run tracts/lihtc/canopy - re-extracts the MD raster (~20 min)."
    ),
):
    """Build the full tract-level analysis table."""
    build_analysis_table(force=force, force_upstream=force_upstream)


if __name__ == "__main__":
    app()

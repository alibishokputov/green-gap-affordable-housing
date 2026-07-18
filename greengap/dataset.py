"""Build the analysis table: tree canopy % + summer LST x LIHTC low-income units.

Pipeline (each step caches to ``data/`` so it only runs once):

    boundaries  TIGER census units (block groups by default; tracts optional)
    lihtc       LIHTC project points aggregated to those units
    canopy      Chesapeake 1 m land-cover class fractions per unit (exactextract)
    landsat     Summer LST/NDVI/NDBI/albedo per unit from the GEE stack raster
    build       join everything into the analysis table -> data/processed/

Run::

    uv run python -m greengap.dataset build              # block groups (default)
    uv run python -m greengap.dataset build --geog tract # coarser robustness run
    uv run python -m greengap.dataset build --force      # rebuild the join only
    uv run python -m greengap.dataset canopy --geog bg --state 24 --counties "Baltimore city"

Unit of analysis
----------------
Default is the **census block group** (~4,650 in MD + DC), the finest areal unit
that still carries ACS demographics natively. ``--geog tract`` reruns the whole
pipeline at tract level (~1,681 units) for a coarser sensitivity check. Anything
finer than a block group (parcel / LIHTC-point + buffer) is a different, non-areal
design and lives outside this module.

Data notes
----------
* **LIHTC** comes from HUD's *national* database (``data/raw/lihtc.zip``), not the
  bundled ``LIHTC.csv`` (which is Maryland-only). See ``LIHTC_COLS``.
* **Canopy** counts all four Chesapeake tree-canopy classes (3/10/11/12) over
  classified *land* area; see ``derive_canopy_columns``.
* **LST** is Landsat thermal, natively ~100 m (resampled to 30 m). A block-group
  mean spans enough native thermal pixels to be stable; do not read it as 30 m.
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
# Study area + geography                                                       #
# --------------------------------------------------------------------------- #
STATES = {"24": "MD", "11": "DC"}  # FIPS -> USPS, the study area
STATE_FIPS = list(STATES)
TIGER_YEAR = 2022
TIGER_COUNTY_URL = "https://www2.census.gov/geo/tiger/TIGER{year}/COUNTY/tl_{year}_us_county.zip"

# Available areal units. Block group is the default (finest areal unit that still
# carries ACS demographics); tract is kept for a coarser robustness comparison.
# Both TIGER layers share the same columns (GEOID, NAMELSAD, STATEFP, COUNTYFP,
# ALAND, AWATER, geometry), so the rest of the pipeline is geography-agnostic.
GEOG_LEVELS = {
    "bg": {
        "url": "https://www2.census.gov/geo/tiger/TIGER{year}/BG/tl_{year}_{fips}_bg.zip",
        "label": "block group",
    },
    "tract": {
        "url": "https://www2.census.gov/geo/tiger/TIGER{year}/TRACT/tl_{year}_{fips}_tract.zip",
        "label": "census tract",
    },
}
DEFAULT_GEOG = "bg"


def _check_geog(geog: str) -> str:
    if geog not in GEOG_LEVELS:
        raise ValueError(f"unknown geography {geog!r}; expected one of {list(GEOG_LEVELS)}")
    return geog


# --------------------------------------------------------------------------- #
# Land cover (canopy)                                                          #
# --------------------------------------------------------------------------- #
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

# A unit's canopy % is only trustworthy if the raster actually classified its
# land. Allow a small gap against the Census' land share (edge effects, vintage
# differences in the shoreline) but reject units that are materially unclassified.
CANOPY_COVERAGE_TOLERANCE = 0.05  # max (Census land share - classified land share)
MIN_LAND_COVERAGE = 0.01  # a unit needs >1% classified land at all

# Urban tree canopy = canopy anywhere, including overhanging impervious surfaces.
CANOPY_VALUES = [3, 10, 11, 12]
# Canopy not overhanging impervious surfaces (conservative "green space" reading).
NATURAL_CANOPY_VALUES = [3]
WATER_VALUES = [1]

# Land-cover rasters (one per state; Chesapeake ships them separately).
LC_RASTERS: dict[str, tuple[Path, str]] = {
    "24": (EXTERNAL_DATA_DIR / "md_lc_2018_2022-Edition.zip", "md_lc_2018_2022-Edition.tif"),
    "11": (EXTERNAL_DATA_DIR / "dc_lc_2017_2022-Edition.zip", "dc_lc_2017_2022-Edition.tif"),
}

# --------------------------------------------------------------------------- #
# Landsat summer-environment stack (from greengap.gee, EPSG:26918, 30 m)       #
# --------------------------------------------------------------------------- #
LANDSAT_STACK = EXTERNAL_DATA_DIR / "gee" / "MD_DC_summer_2021_2023_stack.tif"
LANDSAT_CLEAR = EXTERNAL_DATA_DIR / "gee" / "MD_DC_summer_2021_2023_CLEAR_OBS.tif"
LANDSAT_BANDS = ["LST", "NDVI", "NDBI", "NDWI", "ALBEDO"]  # order matches the stack

# --------------------------------------------------------------------------- #
# LIHTC source                                                                 #
# --------------------------------------------------------------------------- #
# HUD's *national* database (LIHTCPUB.xlsx inside lihtc.zip), not the bundled
# LIHTC.csv: that CSV is a Maryland-only extract (948 MD rows, 0 DC), which would
# silently make a "MD + DC" study MD-only. The national file carries both (MD 948,
# DC 268). openpyxl cannot parse HUD's export (a `synchVertical` attribute), hence
# the calamine engine.
LIHTC_ZIP = RAW_DATA_DIR / "lihtc.zip"
LIHTC_MEMBER = "LIHTCPUB.xlsx"
LIHTC_ENGINE = "calamine"

# HUD ships pre-cleaned unit columns (suffix "r") beside the raw ones. Raw
# `li_units` has 27 nulls in MD alone, and pandas' sum() skips them silently -
# a ~2,200-unit undercount. `li_unitr`/`n_unitsr` are fully populated.
LIHTC_COLS = {
    "state": "proj_st",
    "id": "hud_id",
    "lat": "latitude",
    "lon": "longitude",
    "low_income": "li_unitr",  # imputed, 0 nulls
    "total": "n_unitsr",  # imputed, 0 nulls
}


# --------------------------------------------------------------------------- #
# Cache paths (all geography-scoped so bg and tract runs coexist)              #
# --------------------------------------------------------------------------- #
def boundaries_path(geog: str) -> Path:
    return INTERIM_DATA_DIR / f"boundaries_{geog}.parquet"


def lihtc_path(geog: str) -> Path:
    return INTERIM_DATA_DIR / f"lihtc_by_{geog}.parquet"


def canopy_path(geog: str, fips: str) -> Path:
    """Per-state canopy cache. Per-state because each state has its own 1 m raster
    and a full Maryland extraction takes ~20-30 minutes - never redo it to add DC."""
    return INTERIM_DATA_DIR / f"canopy_{geog}_{fips}.parquet"


def landsat_path(geog: str) -> Path:
    return INTERIM_DATA_DIR / f"landsat_by_{geog}.parquet"


def analysis_path(geog: str) -> Path:
    return PROCESSED_DATA_DIR / f"{geog}_analysis.parquet"


# --------------------------------------------------------------------------- #
# Steps                                                                        #
# --------------------------------------------------------------------------- #
def load_boundaries(geog: str = DEFAULT_GEOG, force: bool = False) -> gpd.GeoDataFrame:
    """TIGER census units for ``STATE_FIPS`` at the chosen geography, cached."""
    _check_geog(geog)
    path = boundaries_path(geog)
    if path.exists() and not force:
        logger.info(f"boundaries[{geog}]: cached -> {path}")
        return gpd.read_parquet(path)

    frames = []
    for fips in STATE_FIPS:
        url = GEOG_LEVELS[geog]["url"].format(year=TIGER_YEAR, fips=fips)
        logger.info(f"boundaries[{geog}]: downloading {url}")
        frames.append(gpd.read_file(url))

    gdf = pd.concat(frames, ignore_index=True)
    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=frames[0].crs)
    gdf = gdf[["GEOID", "NAMELSAD", "STATEFP", "COUNTYFP", "ALAND", "AWATER", "geometry"]]

    # NAMELSAD alone is just "Block Group 2" - join county names so the dashboard
    # can offer a human-readable filter. Use the county's NAMELSAD, not NAME: in
    # Maryland, independent Baltimore city (FIPS 510) and Baltimore County (005)
    # share NAME="Baltimore" and would collapse into one jurisdiction.
    logger.info(f"boundaries[{geog}]: joining county names")
    counties = gpd.read_file(TIGER_COUNTY_URL.format(year=TIGER_YEAR))
    counties = counties[counties["STATEFP"].isin(STATE_FIPS)][["STATEFP", "COUNTYFP", "NAMELSAD"]]
    counties = counties.rename(columns={"NAMELSAD": "county"})
    gdf = gdf.merge(counties, on=["STATEFP", "COUNTYFP"], how="left")

    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(path)
    logger.success(
        f"boundaries[{geog}]: {len(gdf)} units across {gdf['county'].nunique()} "
        f"counties -> {path}"
    )
    return gdf


def load_lihtc_by_area(geog: str = DEFAULT_GEOG, force: bool = False) -> pd.DataFrame:
    """Aggregate LIHTC project points to the chosen census units."""
    _check_geog(geog)
    path = lihtc_path(geog)
    if path.exists() and not force:
        logger.info(f"lihtc[{geog}]: cached -> {path}")
        return pd.read_parquet(path)

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
        df, geometry=gpd.points_from_xy(df[lon], df[lat]), crs="EPSG:4326"
    )

    units = load_boundaries(geog)
    points = points.to_crs(units.crs)
    joined = gpd.sjoin(points, units[["GEOID", "geometry"]], how="inner", predicate="within")

    dropped = len(points) - len(joined)
    if dropped:
        logger.warning(f"lihtc: {dropped} point(s) fell outside the {geog} layer.")

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

    path.parent.mkdir(parents=True, exist_ok=True)
    agg.to_parquet(path)
    logger.success(
        f"lihtc[{geog}]: {len(joined)} projects -> {len(agg)} units with LIHTC -> {path}"
    )
    return agg


def _resolve_raster(fips: str, raster: Path | None = None) -> str:
    """Path to a state's land-cover raster: an extracted .tif if given, else the
    zip member via GDAL's ``/vsizip/`` handler (works, but slower for windows)."""
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
    not always sum to 1: a handful of units straddle the edge of the Chesapeake
    raster (Bay / Atlantic / state line) and are partly nodata. Dividing those by
    a hard-coded 1.0 would silently halve their canopy, so ``data_coverage`` is
    the real denominator.

    * ``canopy_pct``       - canopy as a share of classified **land** (water and
      nodata both excluded). A unit half-covered by the Bay should not read as
      half as green as an otherwise identical inland unit.
    * ``canopy_pct_total`` - canopy as a share of all classified area (water in).
    * ``data_coverage``    - share of the unit with any land-cover class.
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
    geog: str = DEFAULT_GEOG,
    force: bool = False,
    raster: Path | None = None,
    counties: list[str] | None = None,
) -> pd.DataFrame:
    """Per-unit land-cover class fractions for one state's 1 m raster.

    Uses ``exactextract`` (exact polygon/pixel intersection, not centroid
    sampling), so partial boundary pixels are weighted correctly.
    """
    _check_geog(geog)
    path = canopy_path(geog, fips)
    if path.exists() and not force and counties is None:
        logger.info(f"canopy[{geog},{STATES[fips]}]: cached -> {path}")
        return pd.read_parquet(path)

    from exactextract import exact_extract
    import rasterio

    units = load_boundaries(geog)
    units = units[units["STATEFP"] == fips]
    if counties:
        units = units[units["county"].str.contains("|".join(counties), case=False, na=False)]
        if units.empty:
            raise ValueError(
                f"No {STATES[fips]} {geog} units matched county filter {counties}. "
                "Filter matches the county name (e.g. 'Baltimore city', 'Montgomery')."
            )
        logger.info(f"canopy[{geog},{STATES[fips]}]: subset to {len(units)} units {counties}")

    raster_path = _resolve_raster(fips, raster)
    with rasterio.open(raster_path) as src:
        raster_crs = src.crs
        logger.info(f"canopy[{geog},{STATES[fips]}]: raster crs={raster_crs} res={src.res}")

    # Equal-area CRS -> pixel counts are directly proportional to ground area.
    units = units.to_crs(raster_crs)

    logger.info(
        f"canopy[{geog},{STATES[fips]}]: extracting class fractions for {len(units)} "
        "units at 1 m (slow for Maryland)"
    )
    result = exact_extract(
        raster_path, units, ["unique", "frac"],
        include_cols=["GEOID"], output="pandas", progress=True,
    )

    rows = []
    for _, r in result.iterrows():
        shares = dict(zip((int(v) for v in r["unique"]), (float(f) for f in r["frac"])))
        row = {"GEOID": r["GEOID"]}
        for value, name in LC_CLASSES.items():
            row[f"frac_{name}"] = shares.get(value, 0.0)
        rows.append(row)

    out = derive_canopy_columns(pd.DataFrame(rows))

    if counties is None:
        path.parent.mkdir(parents=True, exist_ok=True)
        out.to_parquet(path)
        logger.success(f"canopy[{geog},{STATES[fips]}]: {len(out)} units -> {path}")
    else:
        logger.success(f"canopy[{geog},{STATES[fips]}]: {len(out)} units (subset; not cached)")
    return out


def compute_canopy(
    geog: str = DEFAULT_GEOG, force: bool = False, rasters: dict[str, Path] | None = None
) -> pd.DataFrame:
    """Canopy fractions for every state in the study area, concatenated."""
    rasters = rasters or {}
    frames = [
        compute_canopy_state(fips, geog=geog, force=force, raster=rasters.get(fips))
        for fips in STATE_FIPS
    ]
    return pd.concat(frames, ignore_index=True)


def compute_landsat_stats(geog: str = DEFAULT_GEOG, force: bool = False) -> pd.DataFrame:
    """Per-unit mean of the summer Landsat bands (LST/NDVI/NDBI/albedo) + clear-obs.

    Reads the GEE stack (``greengap.gee``), already at 30 m in EPSG:26918.
    ``exactextract`` area-weights partial boundary pixels and skips the raster's
    nodata (masked cloud/water), so a unit's mean reflects only valid pixels.
    ``mean_clear_obs`` is the reliability companion (analogous to canopy's
    ``data_coverage``): low values mean few clear looks fed the median.
    """
    _check_geog(geog)
    path = landsat_path(geog)
    if path.exists() and not force:
        logger.info(f"landsat[{geog}]: cached -> {path}")
        return pd.read_parquet(path)

    if not LANDSAT_STACK.exists():
        raise FileNotFoundError(
            f"Landsat stack not found: {LANDSAT_STACK}\n"
            "Run the Earth Engine export first: uv run python -m greengap.gee download-local"
        )

    from exactextract import exact_extract
    import rasterio

    units = load_boundaries(geog)
    with rasterio.open(LANDSAT_STACK) as src:
        stack_crs = src.crs
        band_names = list(src.descriptions) or LANDSAT_BANDS
    units_r = units.to_crs(stack_crs)

    logger.info(f"landsat[{geog}]: mean of {band_names} for {len(units_r)} units (30 m)")
    res = exact_extract(
        str(LANDSAT_STACK), units_r, ["mean"],
        include_cols=["GEOID"], output="pandas", progress=True,
    )

    # exactextract names multiband outputs "band_<i>_mean" (1-indexed); map each
    # to its band name so column names are stable regardless of that convention.
    out = pd.DataFrame({"GEOID": res["GEOID"].values})
    mean_cols = [c for c in res.columns if c.endswith("mean")]
    if len(mean_cols) != len(band_names):
        raise RuntimeError(
            f"landsat: expected {len(band_names)} mean columns, got {mean_cols}"
        )
    for col, name in zip(mean_cols, band_names):
        out[f"mean_{name.lower()}"] = res[col].astype("float64").values

    # Clear-observation count per unit (reliability).
    if LANDSAT_CLEAR.exists():
        clr = exact_extract(
            str(LANDSAT_CLEAR), units_r, ["mean"],
            include_cols=["GEOID"], output="pandas",
        )
        cc = [c for c in clr.columns if c.endswith("mean")][0]
        out["mean_clear_obs"] = clr[cc].astype("float64").values

    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path)
    logger.success(f"landsat[{geog}]: {len(out)} units -> {path}")
    return out


def build_analysis_table(
    geog: str = DEFAULT_GEOG, force: bool = False, force_upstream: bool = False
) -> gpd.GeoDataFrame:
    """Join boundaries + canopy + Landsat + LIHTC into the analysis table.

    ``force`` rebuilds only the join (seconds). ``force_upstream`` additionally
    re-runs the source steps - including the ~20-30 min raster extraction - so it
    is deliberately opt-in rather than implied by ``force``.
    """
    _check_geog(geog)
    path = analysis_path(geog)
    if path.exists() and not force and not force_upstream:
        logger.info(f"build[{geog}]: cached -> {path}")
        return gpd.read_parquet(path)

    units = load_boundaries(geog, force=force_upstream)
    canopy = compute_canopy(geog, force=force_upstream)
    landsat = compute_landsat_stats(geog, force=force_upstream)
    lihtc = load_lihtc_by_area(geog, force=force_upstream)

    gdf = (
        units.merge(canopy, on="GEOID", how="left")
        .merge(landsat, on="GEOID", how="left")
        .merge(lihtc, on="GEOID", how="left")
    )

    # Units with no LIHTC project are true zeros, not missing data.
    for col in ["lihtc_projects", "lihtc_units_low_income", "lihtc_units_total"]:
        gdf[col] = gdf[col].fillna(0).astype(int)

    # ---- Flag units whose canopy % rests on too little classified land ----
    # The Chesapeake land cover leaves military installations as nodata (Aberdeen
    # Proving Ground, Joint Base Andrews). Such a unit can be mostly land per the
    # Census yet almost entirely unclassified, producing a canopy % from a sliver.
    # Compare classified land against the Census' own land share and null out the
    # canopy of units that fall short, rather than let them render as outliers.
    tiger_land_share = gdf["ALAND"] / (gdf["ALAND"] + gdf["AWATER"]).clip(lower=1)
    coverage_gap = tiger_land_share - gdf["land_coverage"]
    gdf["canopy_reliable"] = (coverage_gap <= CANOPY_COVERAGE_TOLERANCE) & (
        gdf["land_coverage"] > MIN_LAND_COVERAGE
    )
    n_bad = int((~gdf["canopy_reliable"]).sum())
    if n_bad:
        logger.warning(
            f"build[{geog}]: {n_bad} unit(s) have insufficient classified land "
            "(likely nodata over military land); their canopy is set to NaN."
        )
        for col in ["canopy_pct", "canopy_pct_total", "natural_canopy_pct"]:
            gdf.loc[~gdf["canopy_reliable"], col] = pd.NA

    gdf = gpd.GeoDataFrame(gdf, geometry="geometry", crs=units.crs).to_crs("EPSG:4326")

    path.parent.mkdir(parents=True, exist_ok=True)
    gdf.to_parquet(path)
    logger.success(
        f"build[{geog}]: {len(gdf)} units "
        f"({int((gdf['lihtc_units_low_income'] > 0).sum())} with LIHTC units, "
        f"{int(gdf['canopy_reliable'].sum())} with reliable canopy) -> {path}"
    )
    return gdf


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
_GEOG_OPT = typer.Option(DEFAULT_GEOG, help="Areal unit: 'bg' (block group) or 'tract'.")


@app.command()
def boundaries(
    geog: str = _GEOG_OPT,
    force: bool = typer.Option(False, help="Re-download instead of using cache."),
):
    """Download + cache the TIGER census units."""
    load_boundaries(geog=geog, force=force)


@app.command()
def lihtc(
    geog: str = _GEOG_OPT,
    force: bool = typer.Option(False, help="Recompute instead of using cache."),
):
    """Aggregate LIHTC points to the census units."""
    load_lihtc_by_area(geog=geog, force=force)


@app.command()
def canopy(
    geog: str = _GEOG_OPT,
    state: str = typer.Option(None, help="Limit to one state FIPS (24=MD, 11=DC)."),
    force: bool = typer.Option(False, help="Recompute instead of using cache."),
    raster: Path = typer.Option(None, help="Extracted .tif (faster than reading the zip)."),
    counties: list[str] = typer.Option(None, help="Subset to matching county names (testing)."),
):
    """Compute per-unit land-cover class fractions from the 1 m raster(s)."""
    _check_geog(geog)
    if state:
        if state not in STATES:
            raise typer.BadParameter(f"Unknown state FIPS {state!r}; expected one of {STATE_FIPS}")
        compute_canopy_state(
            state, geog=geog, force=force, raster=raster,
            counties=list(counties) if counties else None,
        )
        return
    if raster or counties:
        raise typer.BadParameter("--raster/--counties require --state (they are per-state).")
    compute_canopy(geog=geog, force=force)


@app.command()
def landsat(
    geog: str = _GEOG_OPT,
    force: bool = typer.Option(False, help="Recompute instead of using cache."),
):
    """Compute per-unit summer LST/NDVI/NDBI/albedo from the GEE stack."""
    compute_landsat_stats(geog=geog, force=force)


@app.command()
def build(
    geog: str = _GEOG_OPT,
    force: bool = typer.Option(False, help="Rebuild the join (fast); keeps cached sources."),
    force_upstream: bool = typer.Option(
        False, help="Also re-run boundaries/lihtc/canopy/landsat (re-extracts rasters)."
    ),
):
    """Build the full analysis table at the chosen geography."""
    build_analysis_table(geog=geog, force=force, force_upstream=force_upstream)


if __name__ == "__main__":
    app()

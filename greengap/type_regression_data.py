"""Assemble the building-level frame for the type-contrast regression and dashboard.

Joins the environmentally-labeled buildings (``greengap.parcel_env``) to their
block-group ACS covariates (``greengap.acs``) into one analysis frame, cached to
``data/processed/mf_buildings_analysis.parquet``. This is the single table both the
regression and the enhanced dashboard read.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from loguru import logger
import pandas as pd
import typer

from greengap.acs import attach_acs
from greengap.config import PROCESSED_DATA_DIR, PROJ_ROOT
from greengap.parcel_env import build as build_env

app = typer.Typer(help=__doc__, no_args_is_help=True)

APP_DIR = PROJ_ROOT / "app"

# Point-layer columns the enhanced dashboard reads (building centroids). lihtc/section8
# drive the LIHTC diamond marker; in_floodplain marks buildings whose footprint
# intersects the FEMA 1%-annual-chance floodplain (SFHA).
POINT_COLS = [
    "building_id", "state", "jurisdiction", "housing_type",
    "units", "value_per_unit", "year_built",
    "canopy_pct", "natural_canopy_pct", "mean_lst", "mean_ndvi",
    "lihtc", "section8", "in_floodplain",
]


def analysis_frame_path() -> Path:
    return PROCESSED_DATA_DIR / "mf_buildings_analysis.parquet"


def load_regression_frame(force: bool = False) -> gpd.GeoDataFrame:
    """Buildings + environment + ACS covariates + block-group rent, one row per building."""
    path = analysis_frame_path()
    if path.exists() and not force:
        return gpd.read_parquet(path)

    from greengap.rent import rent_bg_path

    buildings = build_env()
    joined = attach_acs(buildings)  # adds GEOID

    # Attach the block-group rent measures (observed contract rent context) for the
    # value-vs-rent validation. Rent is a neighborhood measure, not a building rent.
    if rent_bg_path().exists():
        rent = pd.read_parquet(rent_bg_path())
        joined = joined.merge(rent, on="GEOID", how="left")
    else:
        logger.warning("analysis frame: rent_bg not built; run 'python -m greengap.rent build'")

    path.parent.mkdir(parents=True, exist_ok=True)
    joined.to_parquet(path)
    logger.success(f"analysis frame: {len(joined):,} buildings -> {path}")
    return joined


def export_points() -> Path:
    """Write the enhanced building-centroid GeoJSON the dashboard map reads."""
    b = load_regression_frame()
    pts = b[POINT_COLS].copy()
    pts["geometry"] = b.geometry.centroid
    pts = gpd.GeoDataFrame(pts, geometry="geometry", crs=b.crs).to_crs("EPSG:4326")
    pts["housing_type"] = pts["housing_type"].astype("string")
    for c in ("canopy_pct", "natural_canopy_pct", "mean_lst", "mean_ndvi", "value_per_unit"):
        pts[c] = pts[c].round(2)
    out = APP_DIR / "buildings_types.geojson"
    pts.to_file(out, driver="GeoJSON")
    logger.success(f"export: {len(pts):,} building points -> {out}")
    return out


# Block-group demographic and rent context surfaced on the map (tooltips + green-gap
# table). Already pulled per block group by greengap.acs / greengap.rent; joined here
# by GEOID at export time so the areal bg_analysis table stays untouched.
BG_DEMOG_COLS = [
    "median_income", "poverty_rate", "pct_bachelors_plus",
    "pct_white_nh", "pct_black_nh", "pct_hispanic",
]
BG_RENT_COLS = ["median_gross_rent", "affordable_rent_share"]


def export_bg_choropleth() -> Path:
    """Write a block-group GeoJSON: environmental measures, units by housing type, and
    census demographic/economic context.

    The environmental backdrop and geometry come from the areal analysis table
    (``bg_analysis.parquet``); per-type unit counts are aggregated from the labeled
    buildings; ACS demographics and rent are joined by GEOID from the cached
    ``acs_bg`` / ``rent_bg`` tables. This is the block-group map's data.
    """
    from greengap.acs import acs_bg_path
    from greengap.rent import rent_bg_path

    b = load_regression_frame()
    counts = (
        b.dropna(subset=["GEOID"])
        .assign(u=b["units"].fillna(0))
        .pivot_table(index="GEOID", columns="housing_type", values="u",
                     aggfunc="sum", observed=True, fill_value=0)
    )
    counts.columns = [f"units_{c}" for c in counts.columns]
    counts = counts.reset_index()

    bg = gpd.read_parquet(PROCESSED_DATA_DIR / "bg_analysis.parquet")
    keep = ["GEOID", "county", "canopy_pct", "natural_canopy_pct", "mean_lst",
            "mean_ndvi", "lihtc_units_low_income", "lihtc_units_total",
            "lihtc_projects", "geometry"]
    bg = bg[[c for c in keep if c in bg.columns]].copy()
    bg = bg.merge(counts, on="GEOID", how="left")

    # Join ACS demographics and block-group rent by GEOID. Warn (do not fail) if a
    # source is missing, matching load_regression_frame's handling of rent.
    for path, cols, name in (
        (acs_bg_path(), BG_DEMOG_COLS, "acs_bg"),
        (rent_bg_path(), BG_RENT_COLS, "rent_bg"),
    ):
        if path.exists():
            src = pd.read_parquet(path)
            bg = bg.merge(src[["GEOID", *[c for c in cols if c in src.columns]]],
                          on="GEOID", how="left")
        else:
            logger.warning(f"export_bg_choropleth: {name} missing ({path}); "
                           "demographics/rent omitted from the block-group map")

    for c in [c for c in bg.columns if c.startswith("units_")]:
        bg[c] = bg[c].fillna(0).astype(int)
    round_cols = ["canopy_pct", "natural_canopy_pct", "mean_lst", "mean_ndvi",
                  "poverty_rate", "pct_bachelors_plus", "pct_white_nh",
                  "pct_black_nh", "pct_hispanic", "affordable_rent_share"]
    for c in round_cols:
        if c in bg.columns:
            bg[c] = bg[c].astype(float).round(1)
    for c in ("median_income", "median_gross_rent"):
        if c in bg.columns:
            bg[c] = bg[c].round(0)

    # Simplify for a lighter WASM payload (display only), matching the areal build.
    bg["geometry"] = bg.geometry.simplify(0.0003, preserve_topology=True)
    bad = ~bg.geometry.is_valid
    if bad.any():
        bg.loc[bad, "geometry"] = bg.loc[bad, "geometry"].make_valid()

    out = APP_DIR / "bg_types.geojson"
    bg.to_file(out, driver="GeoJSON")
    logger.success(f"export: {len(bg):,} block groups -> {out}")
    return out


@app.command("build")
def build_cmd(force: bool = typer.Option(False, help="Rebuild the joined frame.")):
    """Build the building + environment + ACS analysis frame."""
    load_regression_frame(force=force)


# Environmental measures the dashboard shows.
ENV_MEASURES = {
    "canopy_pct": "Tree canopy % (all)",
    "natural_canopy_pct": "Tree canopy % (natural)",
    "mean_lst": "Summer surface temp (°C)",
    "mean_ndvi": "NDVI (vegetation)",
}


def export_stats() -> Path:
    """Precompute the descriptive statistics the dashboard reports.

    Correlations only (no regression): the building-level canopy-by-type medians and
    the block-group-level environment-vs-LIHTC correlation, side by side, so the
    ecological difference between the two scales is shown honestly. Also the
    value-vs-rent validation of the NOAH label. Computed here and shipped as JSON so
    the browser app stays light.
    """
    import json

    from scipy.stats import spearmanr

    b = load_regression_frame()
    bg = gpd.read_parquet(PROCESSED_DATA_DIR / "bg_analysis.parquet")

    payload = {"env_measures": ENV_MEASURES, "building_medians": {}, "bg_corr": {},
               "paradox": {}, "rent_validation": {}, "flood_share": {}}

    # Building-level: median environment by housing type, per measure.
    for m in ENV_MEASURES:
        payload["building_medians"][m] = {
            t: round(float(v), 2) if pd.notna(v) else None
            for t, v in b.groupby("housing_type", observed=True)[m].median().items()
        }

    # Block-group-level: correlation of each environmental measure with LIHTC units.
    for m in ENV_MEASURES:
        d = bg.dropna(subset=[m, "lihtc_units_low_income"])
        rho, p = spearmanr(d[m], d["lihtc_units_low_income"])
        payload["bg_corr"][m] = {"rho": round(float(rho), 3), "p": float(p), "n": int(len(d))}

    # The canopy paradox, quantified: LIHTC concentrates in lower-canopy block groups
    # even though LIHTC buildings are greener than market-rate multifamily.
    d = bg.dropna(subset=["canopy_pct"])
    with_l = d[d["lihtc_units_low_income"] > 0]["canopy_pct"]
    without_l = d[d["lihtc_units_low_income"] == 0]["canopy_pct"]
    payload["paradox"] = {
        "bg_canopy_with_lihtc": round(float(with_l.mean()), 1),
        "bg_canopy_without_lihtc": round(float(without_l.mean()), 1),
        "building_canopy_lihtc": payload["building_medians"]["canopy_pct"].get("subsidized"),
        "building_canopy_market": payload["building_medians"]["canopy_pct"].get("market_rate"),
    }

    # Value-vs-rent validation: NOAH (value-defined) buildings should sit in block
    # groups where more units actually rent affordably.
    if "affordable_rent_share" in b.columns:
        payload["rent_validation"] = {
            t: round(float(v), 1) if pd.notna(v) else None
            for t, v in b.groupby("housing_type", observed=True)["affordable_rent_share"]
            .median().items()
        }

    # Flood exposure: share of each housing type whose footprint intersects the FEMA
    # 1%-annual-chance floodplain (SFHA). A coarse binary flag; counts thin out by type.
    if "in_floodplain" in b.columns:
        fl = b.dropna(subset=["in_floodplain"])
        payload["flood_share"] = {
            "overall": round(float(fl["in_floodplain"].mean()) * 100, 1),
            "by_type": {
                t: {"pct": round(float(v.mean()) * 100, 1), "n": int(v.sum())}
                for t, v in fl.groupby("housing_type", observed=True)["in_floodplain"]
            },
        }

    out = APP_DIR / "type_stats.json"
    out.write_text(json.dumps(payload))
    logger.success(f"export: descriptive stats -> {out}")
    return out


@app.command("export")
def export_cmd():
    """Export dashboard layers: building points, block-group choropleth, stats."""
    export_points()
    export_bg_choropleth()
    export_stats()


if __name__ == "__main__":
    app()

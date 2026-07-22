"""Assemble the building-level frame for the type-contrast regression and dashboard.

Joins the environmentally-labelled buildings (``greengap.parcel_env``) to their
block-group ACS covariates (``greengap.acs``) into one analysis frame, cached to
``data/processed/mf_buildings_analysis.parquet``. This is the single table both the
regression and the enhanced dashboard read.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from loguru import logger
import typer

from greengap.acs import attach_acs
from greengap.config import PROCESSED_DATA_DIR, PROJ_ROOT
from greengap.parcel_env import build as build_env

app = typer.Typer(help=__doc__, no_args_is_help=True)

APP_DIR = PROJ_ROOT / "app"

# Point-layer columns the enhanced dashboard reads (building centroids). Flood is
# omitted while the FEMA NFHL pull is only partial.
POINT_COLS = [
    "building_id", "state", "jurisdiction", "housing_type",
    "units", "value_per_unit", "year_built",
    "canopy_pct", "natural_canopy_pct", "mean_lst", "mean_ndvi",
]


def analysis_frame_path() -> Path:
    return PROCESSED_DATA_DIR / "mf_buildings_analysis.parquet"


def load_regression_frame(force: bool = False) -> gpd.GeoDataFrame:
    """Buildings + environment + ACS covariates, one row per building."""
    path = analysis_frame_path()
    if path.exists() and not force:
        return gpd.read_parquet(path)

    buildings = build_env()
    joined = attach_acs(buildings)

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


def export_bg_choropleth() -> Path:
    """Write a block-group GeoJSON: environmental measures + units by housing type.

    The environmental backdrop and geometry come from the areal analysis table
    (``bg_analysis.parquet``); the per-type unit counts are aggregated from the
    labelled buildings. This is the choropleth-with-type-layers tab's data.
    """
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
            "mean_ndvi", "lihtc_units_low_income", "geometry"]
    bg = bg[[c for c in keep if c in bg.columns]].copy()
    bg = bg.merge(counts, on="GEOID", how="left")
    for c in [c for c in bg.columns if c.startswith("units_")]:
        bg[c] = bg[c].fillna(0).astype(int)
    for c in ("canopy_pct", "natural_canopy_pct", "mean_lst", "mean_ndvi"):
        if c in bg.columns:
            bg[c] = bg[c].astype(float).round(2)

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


# Outcomes precomputed for the dashboard's analysis tab. statsmodels is not
# available under Pyodide, so the regression is run here and shipped as JSON.
REG_OUTCOMES = {
    "canopy_pct": "Tree canopy % (all)",
    "natural_canopy_pct": "Tree canopy % (natural)",
    "mean_lst": "Summer surface temp (°C)",
    "mean_ndvi": "NDVI (vegetation)",
}


def export_regression() -> Path:
    """Precompute the type-contrast gaps for every outcome -> app/type_regression.json."""
    import json

    from greengap.type_regression import run_all

    df = load_regression_frame()
    payload = {"outcomes": REG_OUTCOMES, "results": {}}
    for outcome in REG_OUTCOMES:
        tab = run_all(df, outcome)
        payload["results"][outcome] = tab.round(3).to_dict(orient="records")

    out = APP_DIR / "type_regression.json"
    out.write_text(json.dumps(payload))
    logger.success(f"export: regression for {len(REG_OUTCOMES)} outcomes -> {out}")
    return out


@app.command("export")
def export_cmd():
    """Export dashboard layers: building points, block-group choropleth, regression."""
    export_points()
    export_bg_choropleth()
    export_regression()


if __name__ == "__main__":
    app()

"""ACS block-group covariates for the parcel-scale housing-type analysis.

Pulls a compact set of 2022 ACS 5-year block-group variables from the Census API
and joins them to each multifamily building via the building's block group. These
are the neighbourhood controls for the adjusted type-contrast regression: income,
poverty, education, race/ethnicity, and population density.

Run::

    uv run python -m greengap.acs build    # -> data/interim/acs_bg.parquet

Requires ``CENSUS_API_KEY`` in the environment (.env). Block-group ACS queries are
rejected without a key.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import urlopen

import geopandas as gpd
from loguru import logger
import pandas as pd
import typer

from greengap.config import INTERIM_DATA_DIR

app = typer.Typer(help=__doc__, no_args_is_help=True)

ACS_YEAR = 2022
ACS_BASE = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"
STATE_COUNTIES = {
    "11": ["001"],  # DC
    # 6 MD study counties: Anne Arundel 003, Baltimore Co 005, Baltimore City 510,
    # Howard 027, Montgomery 031, Prince George's 033.
    "24": ["003", "005", "027", "031", "033", "510"],
}

# ACS variables pulled raw; derived shares are computed after the pull.
ACS_VARS = {
    "B19013_001E": "median_income",       # median household income
    # C17002 (income-to-poverty ratio) is the block-group poverty table; B17001 is
    # not published at block-group level (returns null). Below poverty = ratio < 1.0.
    "C17002_001E": "pov_denom",           # poverty universe
    "C17002_002E": "pov_under_50",        # ratio < 0.50
    "C17002_003E": "pov_50_99",           # ratio 0.50-0.99
    "B15003_001E": "edu_denom",           # population 25+
    "B15003_022E": "edu_ba",              # bachelor's
    "B15003_023E": "edu_ma",              # master's
    "B15003_024E": "edu_prof",            # professional
    "B15003_025E": "edu_phd",             # doctorate
    "B03002_001E": "race_denom",          # total
    "B03002_003E": "white_nh",            # white non-Hispanic
    "B03002_004E": "black_nh",            # Black non-Hispanic
    "B03002_012E": "hispanic",            # Hispanic any race
    "B01003_001E": "population",          # total population
}


def acs_bg_path() -> Path:
    return INTERIM_DATA_DIR / "acs_bg.parquet"


def _fetch_county(state: str, county: str, key: str) -> pd.DataFrame:
    var_list = ",".join(ACS_VARS)
    url = (
        f"{ACS_BASE}?get={var_list}&for=block%20group:*"
        f"&in=state:{state}&in=county:{county}&in=tract:*&key={key}"
    )
    rows = json.load(urlopen(url, timeout=120))
    header, data = rows[0], rows[1:]
    df = pd.DataFrame(data, columns=header)
    df["GEOID"] = df["state"] + df["county"] + df["tract"] + df["block group"]
    return df


def fetch_acs(force: bool = False) -> pd.DataFrame:
    """Pull + cache the ACS block-group variables for the study area."""
    path = acs_bg_path()
    if path.exists() and not force:
        logger.info(f"acs: cached -> {path}")
        return pd.read_parquet(path)

    key = os.environ.get("CENSUS_API_KEY")
    if not key:
        raise RuntimeError("CENSUS_API_KEY not set (add it to .env).")

    frames = [
        _fetch_county(state, county, key)
        for state, counties in STATE_COUNTIES.items()
        for county in counties
    ]
    df = pd.concat(frames, ignore_index=True)
    for code in ACS_VARS:
        df[ACS_VARS[code]] = pd.to_numeric(df[code], errors="coerce")

    out = pd.DataFrame({"GEOID": df["GEOID"]})
    out["median_income"] = df["median_income"].where(df["median_income"] >= 0)  # -666666666 = NA
    pov_below = df["pov_under_50"] + df["pov_50_99"]
    out["poverty_rate"] = (pov_below / df["pov_denom"].replace(0, pd.NA)) * 100
    edu_ba_plus = df[["edu_ba", "edu_ma", "edu_prof", "edu_phd"]].sum(axis=1)
    out["pct_bachelors_plus"] = (edu_ba_plus / df["edu_denom"].replace(0, pd.NA)) * 100
    out["pct_white_nh"] = (df["white_nh"] / df["race_denom"].replace(0, pd.NA)) * 100
    out["pct_black_nh"] = (df["black_nh"] / df["race_denom"].replace(0, pd.NA)) * 100
    out["pct_hispanic"] = (df["hispanic"] / df["race_denom"].replace(0, pd.NA)) * 100
    out["population"] = df["population"]

    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_parquet(path)
    logger.success(f"acs: {len(out):,} block groups -> {path}")
    return out


def attach_acs(buildings: gpd.GeoDataFrame, force: bool = False) -> gpd.GeoDataFrame:
    """Join ACS block-group covariates + population density onto the buildings.

    Buildings are placed in a block group by spatial join to the TIGER 2022 layer
    (the same boundaries the areal pipeline uses), then merged with the ACS pull.
    Population density (persons / km²) uses the TIGER land area ``ALAND``.
    """
    from greengap.dataset import load_boundaries

    acs = fetch_acs(force=force)
    bg = load_boundaries("bg")[["GEOID", "ALAND", "geometry"]].to_crs(buildings.crs)

    # Join on the building centroid, not the footprint: a footprint that straddles a
    # block-group line fails a `within` test and would be dropped, but its centroid
    # always falls in exactly one block group.
    pts = buildings.copy()
    pts["geometry"] = pts.geometry.centroid
    tagged = gpd.sjoin(
        pts[["building_id", "geometry"]], bg, how="left", predicate="within"
    ).drop(columns=["index_right"])[["building_id", "GEOID", "ALAND"]]

    joined = buildings.merge(tagged, on="building_id", how="left").merge(
        acs, on="GEOID", how="left"
    )

    # Persons per km² of block-group land area.
    joined["pop_density"] = joined["population"] / (joined["ALAND"] / 1e6).replace(0, pd.NA)

    n_nobg = int(joined["GEOID"].isna().sum())
    if n_nobg:
        logger.warning(f"acs: {n_nobg} building(s) fell outside every block group")
    logger.info(
        f"acs: joined covariates; median-income coverage "
        f"{joined['median_income'].notna().mean():.1%}"
    )
    return joined


@app.command("build")
def build_cmd(force: bool = typer.Option(False, help="Re-pull from the Census API.")):
    """Pull + cache the ACS block-group covariates."""
    fetch_acs(force=force)


if __name__ == "__main__":
    app()

"""Label multifamily parcels by housing type: subsidized / NOAH / market-rate.

Builds on ``data/processed/parcels_mf.parquet`` (see ``greengap.assessor``) in four
stages, each cached to ``data/``:

    buildings   dissolve multi-parcel complexes into building-level records
    subsidy     flag buildings that intersect LIHTC / NHPD (drop from NOAH pool)
    label       split the unsubsidized pool by assessed value/unit -> NOAH vs market
    build       write the labeled building table -> data/processed/

Run::

    uv run python -m greengap.housing_type build
    uv run python -m greengap.housing_type buildings   # just the aggregation

Why building-level
------------------
A single apartment complex is often split across several assessor parcels: a
parcel that carries the units and value, plus contiguous sub-parcels (garages,
common areas, phased buildings) that report 1 unit or 0 units. Left as parcels,
2,237 of the 11,089 MF parcels report exactly 1 unit yet carry a median ~$85k and
a max ~$85M - value-per-unit is meaningless on them, and any NOAH threshold on
value/unit would mislabel them wholesale. Aggregation to the physical building is
the measurement prerequisite for every downstream step.

Aggregation rule
----------------
Primary key is **spatial contiguity**: parcels whose polygons touch (Queen
adjacency) within a jurisdiction are one building group. This targets the actual
failure mode - a complex physically split into adjacent sub-parcels. Owner mailing
address (MD) / owner name (DC) is *not* used to merge (management-company addresses
are shared across unrelated properties and would over-merge); it is retained only
as a descriptor. Within a group, values/units/areas are summed, year_built is the
max (newest structure), and the dominant use code is kept.
"""

from __future__ import annotations

from pathlib import Path

import geopandas as gpd
from loguru import logger
import numpy as np
import pandas as pd
import typer

from greengap.assessor import parcels_path
from greengap.config import EXTERNAL_DATA_DIR, PROCESSED_DATA_DIR, RAW_DATA_DIR

app = typer.Typer(help=__doc__, no_args_is_help=True)

CORRIDOR_CRS = "EPSG:26985"

# --------------------------------------------------------------------------- #
# Subsidy sources                                                              #
# --------------------------------------------------------------------------- #
# HUD LIHTC national database (same file greengap.dataset uses).
LIHTC_ZIP = RAW_DATA_DIR / "lihtc.zip"
LIHTC_MEMBER = "LIHTCPUB.xlsx"
LIHTC_ENGINE = "calamine"
LIHTC_COLS = {"id": "hud_id", "state": "proj_st", "lat": "latitude", "lon": "longitude"}

# NHPD (National Housing Preservation Database) - subsidized & at-risk stock.
# The national extract covers MD and DC (2,454 properties); only currently ACTIVE
# subsidies count as subsidized - an inactive/expired NHPD property has lost its
# subsidy and is, if anything, NOAH again.
NHPD_NATIONAL = EXTERNAL_DATA_DIR / "National Housing Properties (1).xlsx"
NHPD_ACTIVE_STATUS = "Active"

# A parcel is treated as subsidized if a subsidy point falls within this distance
# of its polygon. Points are geocoded to rooftop/parcel centroid, so a small
# tolerance absorbs geocoding slack without reaching neighbouring parcels.
SUBSIDY_SNAP_M = 30.0

# --------------------------------------------------------------------------- #
# NOAH threshold                                                               #
# --------------------------------------------------------------------------- #
# NOAH = unsubsidized multifamily whose assessed value per unit falls at or below an
# affordability cutoff. The cutoff is AMI-anchored (see greengap.noah_threshold): an
# affordable rent at a target AMI level, converted to an affordable assessed value
# per unit through a gross rent multiplier and a per-state assessment ratio measured
# from the study's own multifamily sales. Three AMI levels are stored so the label's
# sensitivity to the affordability line is visible; 60% AMI (the LIHTC standard) is
# the default. This replaces the earlier arbitrary value/unit quantiles.
NOAH_AMI_VARIANTS = {"ami50": "ami50", "ami60": "ami60", "ami80": "ami80"}
DEFAULT_NOAH_VARIANT = "ami60"

# Value/unit above this is not a real per-unit assessment: it flags a building
# whose value sits on one parcel while its unit count sits on a non-contiguous
# sibling that did not merge, leaving a multi-million "1-unit" record. No genuine
# apartment reaches this, so such buildings are typed 'unknown', not market-rate.
VALUE_PER_UNIT_CEILING = 2_000_000.0


# --------------------------------------------------------------------------- #
# Cache paths                                                                  #
# --------------------------------------------------------------------------- #
def buildings_path() -> Path:
    return PROCESSED_DATA_DIR / "mf_buildings.parquet"


def labeled_path() -> Path:
    return PROCESSED_DATA_DIR / "mf_buildings_labeled.parquet"


# --------------------------------------------------------------------------- #
# Stage 1: building-level aggregation                                          #
# --------------------------------------------------------------------------- #
def _building_groups(gdf: gpd.GeoDataFrame) -> np.ndarray:
    """Connected-component id per parcel from Queen contiguity, within jurisdiction.

    Two parcels join the same building group iff they share a boundary or vertex
    and sit in the same jurisdiction. Returns an integer group id aligned to
    ``gdf`` order.
    """
    from libpysal.weights import Queen
    from scipy.sparse.csgraph import connected_components

    group = np.empty(len(gdf), dtype=np.int64)
    next_id = 0
    for _, idx in gdf.groupby("jurisdiction").groups.items():
        sub = gdf.loc[idx]
        if len(sub) == 1:
            group[gdf.index.get_indexer(idx)] = next_id
            next_id += 1
            continue
        w = Queen.from_dataframe(sub, use_index=False, silence_warnings=True)
        n_comp, labels = connected_components(w.sparse, directed=False)
        group[gdf.index.get_indexer(idx)] = labels + next_id
        next_id += n_comp
    return group


def _dominant(s: pd.Series) -> object:
    m = s.dropna()
    return m.mode().iloc[0] if not m.empty else pd.NA


def aggregate_buildings(force: bool = False) -> gpd.GeoDataFrame:
    """Dissolve contiguous same-jurisdiction MF parcels into building records."""
    path = buildings_path()
    if path.exists() and not force:
        logger.info(f"buildings: cached -> {path}")
        return gpd.read_parquet(path)

    parcels = gpd.read_parquet(parcels_path())
    parcels = parcels[parcels.geometry.notna()].reset_index(drop=True)
    if parcels.crs is None or parcels.crs.to_epsg() != 26985:
        parcels = parcels.to_crs(CORRIDOR_CRS)

    logger.info(f"buildings: grouping {len(parcels):,} parcels by contiguity")
    parcels["building_id"] = _building_groups(parcels)

    grouped = parcels.groupby("building_id")
    agg = grouped.agg(
        state=("state", "first"),
        jurisdiction=("jurisdiction", "first"),
        n_parcels=("parcel_uid", "size"),
        parcel_uids=("parcel_uid", lambda s: ";".join(s.astype(str))),
        assessed_land=("assessed_land", "sum"),
        assessed_improvement=("assessed_improvement", "sum"),
        assessed_total=("assessed_total", "sum"),
        units=("units", "sum"),
        building_area=("building_area", "sum"),
        lot_area=("lot_area", "sum"),
        year_built=("year_built", "max"),
        use_code=("use_code", _dominant),
        use_desc=("use_desc", _dominant),
        owner=("owner", _dominant),
        sale_price=("sale_price", "max"),
        sale_date=("sale_date", "max"),
    )
    geom = grouped.geometry.apply(lambda g: g.union_all())
    buildings = gpd.GeoDataFrame(agg, geometry=geom, crs=CORRIDOR_CRS).reset_index()

    # Value per unit, the affordability proxy. Only defined where units > 0; a
    # building with no unit count cannot be placed on the NOAH/market axis.
    u = buildings["units"].where(buildings["units"] > 0)
    buildings["value_per_unit"] = buildings["assessed_total"] / u

    n_multi = int((buildings["n_parcels"] > 1).sum())
    logger.success(
        f"buildings: {len(parcels):,} parcels -> {len(buildings):,} buildings "
        f"({n_multi:,} span >1 parcel); value/unit defined for "
        f"{int(buildings['value_per_unit'].notna().sum()):,}"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    buildings.to_parquet(path)
    return buildings


# --------------------------------------------------------------------------- #
# Stage 2: subsidy flag                                                        #
# --------------------------------------------------------------------------- #
def _load_lihtc_points() -> gpd.GeoDataFrame:
    df = pd.read_excel(f"zip://{LIHTC_MEMBER}::{LIHTC_ZIP}", engine=LIHTC_ENGINE)
    df = df[df[LIHTC_COLS["state"]].isin(["MD", "DC"])]
    df = df.dropna(subset=[LIHTC_COLS["lat"], LIHTC_COLS["lon"]])
    pts = gpd.GeoDataFrame(
        df[[LIHTC_COLS["id"]]],
        geometry=gpd.points_from_xy(df[LIHTC_COLS["lon"]], df[LIHTC_COLS["lat"]]),
        crs="EPSG:4326",
    ).to_crs(CORRIDOR_CRS)
    logger.info(f"subsidy: {len(pts):,} LIHTC points (MD+DC)")
    return pts


def _load_nhpd_points() -> gpd.GeoDataFrame:
    """Active-subsidy NHPD property points for MD + DC, with program-type flags.

    Carries which programs each property holds so the subsidy set can be split:
    ``nhpd_is_lihtc`` marks NHPD records that are LIHTC (the overlap to dedupe
    against the HUD LIHTC file), and ``nhpd_section8`` marks project-based
    Section 8.
    """
    if not NHPD_NATIONAL.exists():
        logger.warning(f"subsidy: no NHPD file at {NHPD_NATIONAL}; NHPD flags empty.")
        return gpd.GeoDataFrame(geometry=[], crs=CORRIDOR_CRS)

    df = pd.read_excel(NHPD_NATIONAL)
    df = df[df["State"].isin(["MD", "DC"])]
    active = df[df["PropertyStatus"] == NHPD_ACTIVE_STATUS].dropna(
        subset=["Latitude", "Longitude"]
    ).copy()

    n_lihtc = pd.to_numeric(active["NumberActiveLihtc"], errors="coerce").fillna(0)
    n_s8 = pd.to_numeric(active["NumberActiveSection8"], errors="coerce").fillna(0)
    out = gpd.GeoDataFrame(
        {
            "nhpd_is_lihtc": (n_lihtc > 0).to_numpy(),
            "nhpd_section8": (n_s8 > 0).to_numpy(),
        },
        geometry=gpd.points_from_xy(active["Longitude"], active["Latitude"]),
        crs="EPSG:4326",
    ).to_crs(CORRIDOR_CRS)
    logger.info(
        f"subsidy: {len(out):,} active NHPD points "
        f"({int(out['nhpd_is_lihtc'].sum())} LIHTC, {int(out['nhpd_section8'].sum())} Section 8)"
    )
    return out


def _flag_near(buildings: gpd.GeoDataFrame, points: gpd.GeoDataFrame) -> pd.Series:
    """Boolean per building: any point within ``SUBSIDY_SNAP_M`` of its footprint."""
    if points.empty:
        return pd.Series(False, index=buildings.index)
    near = gpd.sjoin_nearest(
        buildings[["building_id", "geometry"]], points[["geometry"]],
        how="left", max_distance=SUBSIDY_SNAP_M, distance_col="_d",
    )
    hit = set(near.loc[near["_d"].notna(), "building_id"])
    return buildings["building_id"].isin(hit)


def flag_subsidized(buildings: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Attach the subsidy flags, kept as separate, non-double-counted measures.

    Three flags, because the two programs have different data quality and the
    analysis wants them apart:

    * ``lihtc``            - within 30 m of a HUD LIHTC record. The trusted measure.
    * ``nhpd_other``       - within 30 m of an active NHPD record that is *not* LIHTC
                             (project-based Section 8, public housing, Section 202/236,
                             etc.). NHPD's own LIHTC flag is used to remove the overlap,
                             so a LIHTC-in-NHPD property is not counted twice.
    * ``section8``         - within 30 m of an active project-based Section 8 property.
    * ``subsidized``       - the operational subsidy flag for the NOAH split. Set to
                             ``lihtc`` alone (LIHTC is the trusted program). NHPD adds
                             coverage but its data quality is weaker, so it is kept as a
                             separate ``subsidized_broad`` measure, not folded into the
                             default.
    * ``subsidized_broad`` - ``lihtc`` OR ``nhpd_other`` (the combined, deduped set).
    """
    out = buildings.copy()
    lihtc = _load_lihtc_points()
    nhpd = _load_nhpd_points()

    out["lihtc"] = _flag_near(out, lihtc)
    # Split NHPD by program so the LIHTC overlap is removed and Section 8 is isolated.
    nhpd_non_lihtc = nhpd[~nhpd["nhpd_is_lihtc"]] if len(nhpd) else nhpd
    nhpd_s8 = nhpd[nhpd["nhpd_section8"]] if len(nhpd) else nhpd
    out["nhpd_other"] = _flag_near(out, nhpd_non_lihtc)
    out["section8"] = _flag_near(out, nhpd_s8)

    out["subsidized"] = out["lihtc"]  # default = LIHTC only (trusted measure)
    out["subsidized_broad"] = out["lihtc"] | out["nhpd_other"]

    logger.info(
        f"subsidy: LIHTC {int(out['lihtc'].sum()):,}; "
        f"+NHPD(non-LIHTC) {int(out['nhpd_other'].sum()):,}; "
        f"Section 8 {int(out['section8'].sum()):,}; "
        f"broad (LIHTC+NHPD) {int(out['subsidized_broad'].sum()):,}"
    )
    return out


# --------------------------------------------------------------------------- #
# Stage 3: NOAH vs market-rate                                                 #
# --------------------------------------------------------------------------- #
def label_types(buildings: gpd.GeoDataFrame, parcels: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Assign ``housing_type`` and per-AMI-variant NOAH flags.

    Subsidized buildings are typed first and removed from the NOAH/market split.
    Among the unsubsidized with a trustworthy value/unit, the AMI-anchored cutoff
    (per jurisdiction, per AMI level) places each building at/below (NOAH) or above
    (market-rate) the affordability line. ``parcels`` supplies the sales used to
    estimate the assessment ratio inside the cutoff. ``housing_type`` uses
    ``DEFAULT_NOAH_VARIANT``.
    """
    from greengap.noah_threshold import AMI_LEVELS, cutoff_map

    out = buildings.copy()

    # An implausibly high value/unit means the unit count is not trustworthy for
    # this building (value and units on separate, unmerged parcels). Exclude it
    # from the affordability axis entirely rather than call it market-rate.
    placeable = (
        out["value_per_unit"].notna()
        & (out["value_per_unit"] > 0)
        & (out["value_per_unit"] <= VALUE_PER_UNIT_CEILING)
    )
    n_ceiling = int((out["value_per_unit"] > VALUE_PER_UNIT_CEILING).sum())
    if n_ceiling:
        logger.info(
            f"label: {n_ceiling} building(s) above the ${VALUE_PER_UNIT_CEILING:,.0f}/unit "
            "ceiling treated as unplaceable (untrustworthy unit count)"
        )

    pool = ~out["subsidized"] & placeable
    for variant, level in AMI_LEVELS.items():
        col = f"noah_{variant}"
        cuts = cutoff_map(parcels, level)  # {jurisdiction -> cutoff}
        cutoff = out["jurisdiction"].map(cuts)
        out[col] = (out["value_per_unit"] <= cutoff).where(pool).astype("boolean")
        by_state = {
            s: round(cuts[j]) for s, j in
            (("DC", "District of Columbia"), ("MD", "Montgomery"))
        }
        logger.info(f"noah[{variant}] cutoff/unit: " + ", ".join(
            f"{s}=${c:,}" for s, c in by_state.items()))

    default = f"noah_{DEFAULT_NOAH_VARIANT}"
    unsub = ~out["subsidized"]
    htype = pd.Series("unknown", index=out.index, dtype="object")  # default: unplaceable
    htype[out["subsidized"]] = "subsidized"
    htype[unsub & placeable] = "market_rate"  # placeable & above the NOAH line
    htype[unsub & placeable & (out[default] == True)] = "noah"  # noqa: E712 (boolean mask)
    out["housing_type"] = pd.Categorical(
        htype, categories=["subsidized", "noah", "market_rate", "unknown"]
    )

    counts = out["housing_type"].value_counts().to_dict()
    logger.success(f"label: housing_type -> {counts}")
    return out


# --------------------------------------------------------------------------- #
# Build                                                                        #
# --------------------------------------------------------------------------- #
def build(force: bool = False, force_upstream: bool = False) -> gpd.GeoDataFrame:
    path = labeled_path()
    if path.exists() and not force and not force_upstream:
        logger.info(f"housing_type: cached -> {path}")
        return gpd.read_parquet(path)

    buildings = aggregate_buildings(force=force_upstream)
    buildings = flag_subsidized(buildings)
    parcels = gpd.read_parquet(parcels_path())  # sales -> assessment ratio in the cutoff
    labeled = label_types(buildings, parcels)

    path.parent.mkdir(parents=True, exist_ok=True)
    labeled.to_parquet(path)
    logger.success(f"housing_type: {len(labeled):,} labeled buildings -> {path}")
    return labeled


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
@app.command()
def buildings(force: bool = typer.Option(False, help="Recompute the aggregation.")):
    """Dissolve MF parcels into building records."""
    aggregate_buildings(force=force)


@app.command("build")
def build_cmd(
    force: bool = typer.Option(False, help="Relabel (fast); keep the cached buildings."),
    force_upstream: bool = typer.Option(False, help="Also redo the building aggregation."),
):
    """Build the labeled multifamily building table."""
    build(force=force, force_upstream=force_upstream)


if __name__ == "__main__":
    app()
